"""Shared helpers for the research-terminal API routers.

Everything here is a thin, read-only adapter over the local silver parquet
partitions and JSON/JSONL personal files.  No arbitrary SQL, shell commands or
third-party network calls are performed by these helpers.
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Mapping, Sequence

from market_monitor.market_data_version import market_data_version
from market_monitor.market_query_cache import KLineWindow, get_kline_query_store

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500
MAX_BARS = 5000
DEFAULT_QUERY_TIMEOUT_SECONDS = 5.0

_FILE_LOCK = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def beijing_text(value: Any) -> str | None:
    """Render a stored timestamp for user-facing Chinese local pages."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return parsed.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(value)[:19].replace("T", " ")


def _clean_value(value: Any) -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        rounded = round(value, 6)
        return int(rounded) if rounded.is_integer() and abs(rounded) < 2**53 else rounded
    if isinstance(value, dict):
        return {str(key): _clean_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_value(item) for item in value]
    return value


def clean(value: Any) -> Any:
    """Recursively remove NaN/Infinity and normalize floats for JSON output."""
    return _clean_value(value)


def json_dumps(value: Any) -> str:
    return json.dumps(clean(value), ensure_ascii=False, separators=(",", ":"))


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def save_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json_dumps(payload) + "\n", encoding="utf-8")
    with _FILE_LOCK:
        temporary.replace(path)
    return path


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except OSError:
        return rows
    return rows


def append_jsonl(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _FILE_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json_dumps(dict(payload)) + "\n")
    return path


def silver_partitions(data_root: Path) -> list[Path]:
    return sorted(Path(data_root).joinpath("silver").rglob("*.parquet"))


def silver_period_partitions(data_root: Path, period: str | None) -> list[Path]:
    """Return only physical partitions capable of containing ``period``."""

    if not period or not period.replace("_", "").isalnum():
        return silver_partitions(data_root)
    root = Path(data_root).joinpath("silver")
    matching = sorted(root.glob(f"market=*/asset_type=*/period={period}/year=*/*.parquet"))
    # Legacy/test stores may predate Hive-style period directories.
    return matching or silver_partitions(data_root)


def _inventory_key(data_root: Path) -> tuple[str, str]:
    """Use the producer-maintained revision instead of stat'ing every file."""

    return str(data_root), market_data_version(data_root)


@dataclass(frozen=True)
class SilverInventory:
    instruments: dict[str, dict[str, Any]]
    rows: int
    markets: dict[str, int]
    asset_types: dict[str, int]
    periods: list[str]
    latest_bar_at: str | None
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "instruments": len(self.instruments),
            "rows": self.rows,
            "markets": dict(self.markets),
            "assetTypes": dict(self.asset_types),
            "periods": list(self.periods),
            "latestBarAt": self.latest_bar_at,
            "generatedAt": self.generated_at,
        }


def _inventory_item(
    instrument_id: Any,
    market: Any,
    asset_type: Any,
    period: Any,
    bar_open_time: Any,
    bar_json: Any,
) -> tuple[str, dict[str, Any]]:
    key_id = str(instrument_id)
    try:
        payload = json.loads(str(bar_json))
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return key_id, {
        "instrumentId": key_id,
        "symbol": str(payload.get("symbol") or ""),
        "name": str(payload.get("name") or ""),
        "market": str(market or ""),
        "assetType": str(asset_type or ""),
        "period": str(period or ""),
        "lastClose": _clean_value(payload.get("close")),
        "lastOpenInterest": _clean_value(payload.get("open_interest")),
        "lastAmount": _clean_value(payload.get("amount")),
        "lastSettlement": _clean_value(payload.get("settlement")),
        "lastVolume": _clean_value(payload.get("volume")),
        "lastBarAt": str(bar_open_time or ""),
        "source": str(payload.get("source") or ""),
        "qualityStatus": str(payload.get("quality_status") or ""),
        "updatedAt": str(payload.get("fetched_at") or ""),
        "canonicalInstrumentId": str(payload.get("canonical_instrument_id") or key_id),
        "seriesKind": str(payload.get("series_kind") or ""),
        "productCode": str(payload.get("product_code") or ""),
        "exchange": str(payload.get("exchange") or ""),
        "sourceSymbol": str(payload.get("source_symbol") or ""),
        "actualSource": str(payload.get("actual_source") or payload.get("source") or ""),
        "availability": str(payload.get("availability") or "可用"),
        "unsupportedReason": payload.get("unsupported_reason"),
        "latestTimeText": beijing_text(bar_open_time),
    }


_inventory_cache: dict[tuple[str, str], SilverInventory] = {}
_INVENTORY_LOCK = threading.Lock()


def load_inventory(data_root: Path, *, max_instruments: int | None = None) -> SilverInventory:
    """Read the silver parquet partitions and return a compact instrument index.

    The index is cached by the producer-maintained data revision and served
    from the ordered K-line query store, avoiding a Parquet rescan per chart.
    """
    key = _inventory_key(data_root)
    cached = _inventory_cache.get(key)
    if cached is not None:
        return cached
    # The market view starts three requests concurrently.  Let one request
    # build the index and let the other two reuse it, rather than scanning all
    # parquet partitions three times at once.
    with _INVENTORY_LOCK:
        cached = _inventory_cache.get(key)
        if cached is not None:
            return cached
        rows = 0
        periods: set[str] = set()
        instruments: dict[str, dict[str, Any]] = {}
        latest_bar_at: str | None = None
        try:
            snapshot = get_kline_query_store(data_root).inventory_snapshot(max_instruments)
            rows = int(snapshot["rows"])
            latest_bar_at = snapshot["latestBarAt"]
            periods = {str(value) for value in snapshot["periods"] if value}
            for row in snapshot["items"]:
                key_id, item = _inventory_item(*row)
                instruments[key_id] = item
        except Exception:
            files = silver_partitions(data_root)
            if files:
                try:
                    import duckdb

                    connection = duckdb.connect(database=":memory:")
                    try:
                        source = repr([str(path) for path in files])
                        rows, latest_bar_at, period_values = connection.execute(
                            f"SELECT count(*), max(bar_open_time), list(DISTINCT period) "
                            f"FROM read_parquet({source})"
                        ).fetchone()
                        periods = {str(value) for value in (period_values or []) if value}
                        query = (
                            "WITH ranked AS ("
                            "SELECT instrument_id, market, asset_type, period, bar_open_time, bar_json, "
                            "row_number() OVER (PARTITION BY instrument_id ORDER BY bar_open_time DESC) AS rank "
                            f"FROM read_parquet({source})"
                            ") SELECT instrument_id, market, asset_type, period, bar_open_time, bar_json "
                            "FROM ranked WHERE rank = 1 ORDER BY instrument_id"
                        )
                        for instrument_id, market, asset_type, period, bar_open_time, bar_json in connection.execute(query).fetchall():
                            key_id, item = _inventory_item(
                                instrument_id, market, asset_type, period, bar_open_time, bar_json
                            )
                            instruments[key_id] = item
                    finally:
                        connection.close()
                except ImportError:
                    return SilverInventory({}, 0, {}, {}, [], None, now_iso())
                except Exception:
                    return SilverInventory({}, 0, {}, {}, [], None, now_iso())
        ordered_items = sorted(instruments.items())
        if max_instruments is not None:
            ordered_items = ordered_items[:max(1, int(max_instruments))]
        ordered = dict(ordered_items)
        markets: dict[str, int] = {}
        asset_types: dict[str, int] = {}
        for item in ordered.values():
            market = str(item.get("market") or "")
            asset_type = str(item.get("assetType") or "")
            markets[market] = markets.get(market, 0) + 1
            asset_types[asset_type] = asset_types.get(asset_type, 0) + 1
        inventory = SilverInventory(
            instruments=ordered,
            rows=int(rows or 0),
            markets=markets,
            asset_types=asset_types,
            periods=sorted(periods),
            latest_bar_at=str(latest_bar_at) if latest_bar_at else None,
            generated_at=now_iso(),
        )
        _inventory_cache.clear()
        _inventory_cache[key] = inventory
        return inventory


def read_bars(
    data_root: Path,
    instrument_id: str,
    *,
    period: str | None = None,
    limit: int = 1000,
    timeout_seconds: float = DEFAULT_QUERY_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """Return ascending K-line bars for one instrument from local silver parquet."""
    limit = max(1, min(int(limit), MAX_BARS))
    # The ordered DuckDB query store is the normal chart path.  Keep the
    # Parquet fallback for a damaged/missing cache so local data remains
    # readable rather than silently returning an empty chart.
    try:
        return get_kline_query_store(data_root).read_bars(instrument_id, period, limit)
    except Exception:
        pass
    files = silver_period_partitions(data_root, period)
    if not files:
        return []
    clauses = ["instrument_id = ?"]
    parameters: list[Any] = [instrument_id]
    if period:
        clauses.append("period = ?")
        parameters.append(period)
    where = " AND ".join(clauses)
    query = (
        f"SELECT bar_json FROM read_parquet({[str(path) for path in files]!r}) "
        f"WHERE {where} ORDER BY bar_open_time DESC LIMIT {limit}"
    )
    bars: list[dict[str, Any]] = []
    try:
        import duckdb

        connection = duckdb.connect(database=":memory:")
        try:
            for (bar_json,) in connection.execute(query, parameters).fetchall():
                try:
                    payload = json.loads(str(bar_json))
                except ValueError:
                    continue
                if not isinstance(payload, dict):
                    continue
                payload.setdefault("instrument_id", instrument_id)
                bars.append(clean(payload))
        finally:
            connection.close()
    except (ImportError, Exception):
        return []
    bars.sort(key=lambda bar: str(bar.get("bar_open_time") or ""))
    return bars[-limit:]


def read_bars_window(
    data_root: Path,
    instrument_id: str,
    *,
    period: str,
    start: int,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """Read a chronological K-line slice without the legacy ``MAX_BARS`` cap.

    The caller supplies a validated zero-based logical offset.  It is used by
    the chart workbench to represent the whole local history while only moving
    the selected viewport over the wire.
    """
    start = max(0, int(start))
    limit = max(1, min(int(limit), 5_000))
    try:
        return get_kline_query_store(data_root).read_window(instrument_id, period, start, limit)
    except Exception:
        pass
    files = silver_period_partitions(data_root, period)
    if not files:
        return [], 0
    source = [str(path) for path in files]
    count_query = f"SELECT count(*) FROM read_parquet({source!r}) WHERE instrument_id = ? AND period = ?"
    data_query = (
        f"SELECT bar_json FROM read_parquet({source!r}) WHERE instrument_id = ? AND period = ? "
        f"ORDER BY bar_open_time ASC LIMIT {limit} OFFSET {start}"
    )
    bars: list[dict[str, Any]] = []
    try:
        import duckdb

        connection = duckdb.connect(database=":memory:")
        try:
            total = int(connection.execute(count_query, [instrument_id, period]).fetchone()[0] or 0)
            for (bar_json,) in connection.execute(data_query, [instrument_id, period]).fetchall():
                try:
                    payload = json.loads(str(bar_json))
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    payload.setdefault("instrument_id", instrument_id)
                    bars.append(clean(payload))
        finally:
            connection.close()
    except (ImportError, Exception):
        return [], 0
    return bars, total


def read_bars_before(
    data_root: Path,
    instrument_id: str,
    *,
    period: str,
    before: str | None = None,
    limit: int = 120,
) -> KLineWindow:
    """Return recent/earlier bars using a stable time cursor.

    This is the preferred chart pagination API.  Unlike a logical offset, the
    cursor does not move when a producer appends a new bar and the database can
    seek directly into the ``instrument + period + time`` index.
    """

    limit = max(1, min(int(limit), 5_000))
    try:
        return get_kline_query_store(data_root).read_before(
            instrument_id,
            period,
            before=before,
            limit=limit,
        )
    except Exception:
        pass
    files = silver_period_partitions(data_root, period)
    if not files:
        return KLineWindow([], 0, before, False)
    source = [str(path) for path in files]
    cursor_clause = " AND bar_open_time < ?" if before else ""
    parameters: list[Any] = [instrument_id, period]
    if before:
        parameters.append(before)
    data_query = (
        f"SELECT bar_open_time, bar_json FROM read_parquet({source!r}) "
        f"WHERE instrument_id = ? AND period = ?{cursor_clause} "
        f"ORDER BY bar_open_time DESC LIMIT {limit + 1}"
    )
    count_query = (
        f"SELECT count(*) FROM read_parquet({source!r}) "
        "WHERE instrument_id = ? AND period = ?"
    )
    try:
        import duckdb

        connection = duckdb.connect(database=":memory:")
        try:
            total = int(connection.execute(count_query, [instrument_id, period]).fetchone()[0] or 0)
            rows = connection.execute(data_query, parameters).fetchall()
        finally:
            connection.close()
    except Exception:
        return KLineWindow([], 0, before, False)
    has_more = len(rows) > limit
    selected = rows[:limit]
    selected.reverse()
    bars: list[dict[str, Any]] = []
    for _opened, bar_json in selected:
        try:
            payload = json.loads(str(bar_json))
        except ValueError:
            continue
        if isinstance(payload, dict):
            payload.setdefault("instrument_id", instrument_id)
            bars.append(clean(payload))
    next_cursor = str(selected[0][0]) if selected else before
    return KLineWindow(bars, total, next_cursor, has_more)


def bar_bounds(data_root: Path, instrument_id: str, period: str) -> tuple[int, str | None, str | None]:
    """Return a period's count and endpoints without reading its K-line body."""

    try:
        return get_kline_query_store(data_root).bounds(instrument_id, period)
    except Exception:
        rows, total = read_bars_window(data_root, instrument_id, period=period, start=0, limit=1)
        tail, _ = read_bars_window(data_root, instrument_id, period=period, start=max(0, total - 1), limit=1)
        return total, (rows[0].get("bar_open_time") if rows else None), (tail[-1].get("bar_open_time") if tail else None)


def instrument_periods(data_root: Path, instrument_id: str) -> list[str]:
    """Read actual stored periods from the query index in one lookup."""

    try:
        return get_kline_query_store(data_root).periods(instrument_id)
    except Exception:
        return []


def bars_by_instrument(
    data_root: Path,
    *,
    period: str | None = None,
    limit_per_instrument: int = 500,
    max_instruments: int = 500,
) -> dict[str, list[dict[str, Any]]]:
    """Group local silver bars by instrument for strategy scans and dashboards."""
    files = silver_partitions(data_root)
    if not files:
        return {}
    parameters: list[Any] = []
    clause = ""
    if period:
        clause = "WHERE period = ?"
        parameters.append(period)
    query = (
        f"SELECT instrument_id, bar_json FROM read_parquet({[str(path) for path in files]!r}) {clause}"
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    try:
        import duckdb

        connection = duckdb.connect(database=":memory:")
        try:
            for instrument_id, bar_json in connection.execute(query, parameters).fetchall():
                try:
                    payload = json.loads(str(bar_json))
                except ValueError:
                    continue
                if not isinstance(payload, dict):
                    continue
                grouped.setdefault(str(instrument_id), []).append(clean(payload))
        finally:
            connection.close()
    except Exception:
        return {}
    for instrument_id, bars in grouped.items():
        bars.sort(key=lambda bar: str(bar.get("bar_open_time") or ""))
        grouped[instrument_id] = bars[-limit_per_instrument:]
    return dict(list(grouped.items())[:max_instruments])


def paginate(items: Sequence[Any], page: int, page_size: int) -> dict[str, Any]:
    total = len(items)
    start = (max(1, page) - 1) * page_size
    return {
        "items": list(items[start : start + page_size]),
        "total": total,
        "page": max(1, page),
        "pageSize": page_size,
    }


__all__ = (
    "DEFAULT_PAGE_SIZE",
    "MAX_BARS",
    "MAX_PAGE_SIZE",
    "SilverInventory",
    "append_jsonl",
    "bars_by_instrument",
    "bar_bounds",
    "clean",
    "json_dumps",
    "load_inventory",
    "load_json",
    "load_jsonl",
    "now_iso",
    "paginate",
    "read_bars",
    "read_bars_before",
    "read_bars_window",
    "instrument_periods",
    "save_json",
    "silver_period_partitions",
    "silver_partitions",
)
