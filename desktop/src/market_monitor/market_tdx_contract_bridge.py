"""Use unchanged TDX month prices to identify a continuous series' month.

The bridge is identity evidence only. Official same-day contract facts still
supply money, quantities and margins; revised exchange volume is not replaced.
"""
from __future__ import annotations

from collections import OrderedDict, defaultdict
import json
from pathlib import Path
import re
import threading
from typing import Any

import duckdb

from .market_data_version import market_data_version
from .market_official_futures_metrics import contract_identity
from .market_query_cache import get_kline_query_store

_CACHE: OrderedDict[tuple[str, str, str, str, str, str], dict[str, list[dict[str, Any]]]] = OrderedDict()
_LOCK = threading.RLock()


def tdx_contracts_by_day(root: Path, *, start: str, end: str,
                         exchange: str = "", product: str = "") -> dict[str, list[dict[str, Any]]]:
    """Read bounded, source-isolated daily month records, cached by revision."""
    if not (Path(root) / "silver").is_dir():
        return {}
    key = (str(root.resolve()), market_data_version(root), start, end, exchange, product)
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    from .web_api.common import load_inventory

    candidates = []
    for item in load_inventory(root).instruments.values():
        identity = str(item.get("instrumentId") or "")
        if (item.get("assetType") != "FUTURE" or item.get("seriesKind") != "CONTRACT"
                or not identity.upper().endswith(".TDX")):
            continue
        venue, code = contract_identity(item)
        match = re.fullmatch(r"([A-Z]+(?:-F)?)\d{4}", code)
        if not match or (exchange and venue != exchange) or (product and match[1] != product):
            continue
        candidates.append((identity, venue, match[1], code))
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if candidates:
        store = get_kline_query_store(root)
        store.ensure_ready()
        with duckdb.connect(str(store.path), read_only=True) as connection:
            connection.execute("CREATE TEMP TABLE _bridge_requested(instrument_id VARCHAR)")
            connection.executemany("INSERT INTO _bridge_requested VALUES (?)", [(row[0],) for row in candidates])
            files = [str(row[0]) for row in connection.execute(
                "SELECT DISTINCT file_path FROM instrument_file JOIN _bridge_requested USING(instrument_id) "
                "WHERE period='1d' AND substr(latest_bar_at,1,10)>=? AND substr(earliest_bar_at,1,10)<=?",
                [start, end],
            ).fetchall()]
        if files:
            identities = {row[0]: row[1:] for row in candidates}
            with duckdb.connect(":memory:") as connection:
                columns = {str(row[0]) for row in connection.execute(
                    "DESCRIBE SELECT * FROM read_parquet(?,union_by_name=true,hive_partitioning=false)",
                    [files],
                ).fetchall()}
                if "bar_period" in columns and "period" in columns:
                    period_column = "coalesce(p.bar_period,p.period)"
                elif "bar_period" in columns:
                    period_column = "p.bar_period"
                elif "period" in columns:
                    period_column = "p.period"
                else:
                    raise ValueError("TDX daily partition lacks a period column")
                connection.execute("CREATE TEMP TABLE _bridge_requested(instrument_id VARCHAR)")
                connection.executemany("INSERT INTO _bridge_requested VALUES (?)", [(row[0],) for row in candidates])
                rows = connection.execute(
                    f"SELECT p.instrument_id,substr(p.bar_open_time,1,10),arg_max(p.bar_json,"
                    "coalesce(json_extract_string(p.bar_json,'$.fetched_at'),'')) "
                    "FROM read_parquet(?,union_by_name=true,hive_partitioning=false) p "
                    "JOIN _bridge_requested USING(instrument_id) "
                    f"WHERE {period_column}='1d' AND substr(p.bar_open_time,1,10) BETWEEN ? AND ? GROUP BY 1,2",
                    [files, start, end],
                ).fetchall()
            for identity, day, payload in rows:
                try:
                    bar = json.loads(str(payload))
                except (ValueError, TypeError):
                    continue
                if not isinstance(bar, dict):
                    continue
                venue, member_product, code = identities[str(identity)]
                result[f"{venue}.{member_product}|{day}"].append({
                    **bar, "contractCode": code, "exchange": venue, "productCode": member_product,
                    "officialMetricsDate": str(day), "sourceInstrumentId": str(identity),
                })
    with _LOCK:
        _CACHE[key] = dict(result)
        while len(_CACHE) > 32:
            _CACHE.popitem(last=False)
    return dict(result)
