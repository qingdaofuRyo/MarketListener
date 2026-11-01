"""Exact-current-day broker observations of exchange speculative standards.

These facts stay outside official settlement coverage. A product-level scalar
explicitly labelled speculative applies equally to both directions; that scope
is retained as evidence instead of inventing separately published side fields.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import requests

from .futures_settlement_parameters import _atomic_bytes, _save_meta

PARSER_VERSION = "current-exchange-standard-backup-v1"
SOURCE_KIND = "THIRD_PARTY_CURRENT_EXCHANGE_STANDARD"
QUALITY_STATUS = "BACKUP_VERIFIED"
RATE_SCOPE = "SINGLE_PRODUCT_SPECULATIVE_STANDARD_FOR_BOTH_SIDES"
TABLE = "futures_settlement_parameter_backup_observation"
BRONZE_ROOT = Path("bronze") / "futures_settlement_parameter_backup"
_PRODUCTS = frozenset({"IF", "IH", "IC", "IM", "T", "TF", "TS", "TL"})
_COLUMNS = (
    "trade_date", "exchange", "contract_code", "product_code", "spec_long_rate",
    "spec_short_rate", "source_kind", "quality_status", "source_url",
    "payload_sha256", "retrieved_at", "parser_version", "source_margin_unit",
    "spec_rate_raw", "margin_rate_scope", "scope_evidence_json",
)


def ensure_schema(connection: Any) -> None:
    connection = getattr(connection, "connection", connection)
    connection.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
        trade_date VARCHAR NOT NULL, exchange VARCHAR NOT NULL,
        contract_code VARCHAR NOT NULL, product_code VARCHAR NOT NULL,
        spec_long_rate DOUBLE NOT NULL, spec_short_rate DOUBLE NOT NULL,
        source_kind VARCHAR NOT NULL, quality_status VARCHAR NOT NULL,
        source_url VARCHAR NOT NULL, payload_sha256 VARCHAR NOT NULL,
        retrieved_at VARCHAR NOT NULL, parser_version VARCHAR NOT NULL,
        source_margin_unit VARCHAR NOT NULL, spec_rate_raw VARCHAR NOT NULL,
        margin_rate_scope VARCHAR NOT NULL, scope_evidence_json VARCHAR NOT NULL,
        PRIMARY KEY(trade_date, exchange, contract_code, source_url))""")


def _source_url(value: str) -> str:
    parts = urlsplit(value)
    if (parts.scheme != "https" or parts.hostname != "www.minfutures.com"
            or parts.username or parts.query or parts.fragment
            or not re.fullmatch(r"/main/a/20\d{6}/\d+\.shtml", parts.path)):
        raise ValueError("backup source must be a dated Minfutures original")
    return value


def _grid(table: Any) -> list[list[str]]:
    """Expand structural spans before deciding which column is speculative."""
    occupied: dict[tuple[int, int], str] = {}
    rows = []
    for row_index, tr in enumerate(table.find_all("tr")):
        column = 0
        for cell in tr.find_all(["td", "th"], recursive=False):
            while (row_index, column) in occupied:
                column += 1
            row_span, column_span = int(cell.get("rowspan", 1)), int(cell.get("colspan", 1))
            if not 1 <= row_span <= 500 or not 1 <= column_span <= 30:
                raise ValueError("invalid backup table span")
            text = cell.get_text(" ", strip=True)
            for y in range(row_index, row_index + row_span):
                for x in range(column, column + column_span):
                    occupied[y, x] = text
            column += column_span
        width = max((x for y, x in occupied if y == row_index), default=-1) + 1
        rows.append([occupied.get((row_index, x), "") for x in range(width)])
    return rows


def _valid_rate(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= 1)


def parse_minfutures(raw: bytes, *, trading_day: str, source_url: str,
                    retrieved_at: str) -> list[dict[str, Any]]:
    day = date.fromisoformat(trading_day).isoformat()
    source_url = _source_url(source_url)
    datetime.fromisoformat(retrieved_at)
    if not raw or len(raw) > 2_000_000:
        raise ValueError("backup response is empty or too large")
    soup = BeautifulSoup(raw, "html.parser")
    expected_title = day.replace("-", "") + "期货保证金率及涨跌停板"
    if expected_title not in [tag.get_text(" ", strip=True) for tag in soup.find_all(["h1", "h2", "h3"])]:
        raise ValueError("backup title does not explicitly match trading date")
    tables = [table for table in soup.find_all("table")
              if "交易所保证金" in table.get_text() and "中金所" in table.get_text()]
    if len(tables) != 1:
        raise ValueError("backup must have one verified standards table")
    grid = _grid(tables[0])
    if (len(grid) < 3 or grid[0][:5] != ["交易所", "品种", "品种代码", "交易所保证金（%）", "交易所保证金（%）"]
            or grid[0][5:7] != ["公司保证金（%）", "公司保证金（%）"]
            or grid[1][3:7] != ["投机", "套保", "投机", "套保"]):
        raise ValueError("backup exchange speculative column is not verified")
    result = {}
    for values in grid[2:]:
        if len(values) < 14 or values[0] != "中金所" or values[2] not in _PRODUCTS:
            continue
        product, raw_rate = values[2], values[3]
        if product in result:
            raise ValueError("duplicate product backup standard")
        if any(value.strip() not in {"", "-", "—"} for value in values[8:14]):
            raise ValueError("CFFEX backup has unhandled contract exceptions")
        try:
            rate = float(raw_rate.strip().removesuffix("%")) / 100
        except ValueError:
            raise ValueError("invalid backup speculative percentage") from None
        if not _valid_rate(rate):
            raise ValueError("backup speculative rate outside [0, 1]")
        result[product] = {
            "trade_date": day, "exchange": "CFFEX", "product_code": product,
            "spec_long_rate": rate, "spec_short_rate": rate,
            "source_kind": SOURCE_KIND, "quality_status": QUALITY_STATUS,
            "source_url": source_url, "payload_sha256": sha256(raw).hexdigest(),
            "retrieved_at": retrieved_at, "parser_version": PARSER_VERSION,
            "source_margin_unit": "PERCENT", "spec_rate_raw": raw_rate,
            "margin_rate_scope": RATE_SCOPE,
            "scope_evidence_json": json.dumps({"title": expected_title,
                "column": "交易所保证金（%）/投机", "product_code": product,
                "contract_exceptions": [], "direction_scope": "single standard for both sides",
                "official_side_values_separately_published": False}, ensure_ascii=False, sort_keys=True),
        }
    if set(result) != _PRODUCTS:
        raise ValueError("backup lacks complete CFFEX product standards")
    return [result[key] for key in sorted(result)]


def import_backup(connection: Any, rows: Sequence[Mapping[str, Any]],
                  official_contracts: Sequence[Mapping[str, Any]]) -> int:
    """Expand only to futures actually reported by the exchange that same day."""
    if not rows:
        return 0
    connection = getattr(connection, "connection", connection)
    day, url, digest = rows[0]["trade_date"], rows[0]["source_url"], rows[0]["payload_sha256"]
    date.fromisoformat(day)
    _source_url(url)
    if not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
        raise ValueError("backup requires original SHA")
    by_product = {}
    for row in rows:
        if (row.get("trade_date") != day or row.get("exchange") != "CFFEX"
                or row.get("source_url") != url or row.get("payload_sha256") != digest
                or row.get("source_kind") != SOURCE_KIND or row.get("quality_status") != QUALITY_STATUS
                or row.get("parser_version") != PARSER_VERSION or row.get("margin_rate_scope") != RATE_SCOPE
                or row.get("product_code") not in _PRODUCTS
                or not _valid_rate(row.get("spec_long_rate")) or row.get("spec_long_rate") != row.get("spec_short_rate")):
            raise ValueError("invalid source-isolated backup partition")
        if row["product_code"] in by_product:
            raise ValueError("duplicate backup product")
        by_product[row["product_code"]] = row
    expanded = {}
    for contract in official_contracts:
        if contract.get("exchange") != "CFFEX":
            continue
        if str(contract.get("trade_date") or contract.get("tradingDay") or "") != day:
            raise ValueError("backup cannot map another trading day's contracts")
        if contract.get("quality_status", contract.get("qualityStatus", "PASS")) != "PASS":
            continue
        code = str(contract.get("contract_code") or contract.get("contractCode") or "")
        product = str(contract.get("product_code") or contract.get("productCode") or "")
        if product not in by_product or not re.fullmatch(re.escape(product) + r"\d{4}", code):
            continue
        expanded[code] = {**by_product[product], "contract_code": code}
    if not expanded:
        raise ValueError("no same-day official futures for backup")
    ensure_schema(connection)
    connection.execute("BEGIN TRANSACTION")
    try:
        connection.execute(f"DELETE FROM {TABLE} WHERE trade_date=? AND source_url=?", [day, url])
        connection.executemany(f"INSERT INTO {TABLE} ({','.join(_COLUMNS)}) VALUES ({','.join('?' for _ in _COLUMNS)})",
                               [[row[column] for column in _COLUMNS] for row in expanded.values()])
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return len(expanded)


def current_backup_margins(connection: Any, day: str, completed_day: str) -> dict[tuple[str, str], dict[str, Any]]:
    """Return only current-day observations, after any complete official facts."""
    if day != completed_day:
        return {}
    connection = getattr(connection, "connection", connection)
    tables = {item[0] for item in connection.execute("SHOW TABLES").fetchall()}
    if TABLE not in tables:
        return {}
    official = set()
    if "futures_settlement_parameter_official" in tables:
        from .cffex_margin_asof import (
            applicable_publication_on_or_before,
            known_publication_days,
            verified_publications,
        )
        from .futures_settlement_parameters import PARSER_VERSION as OFFICIAL_PARSER

        official = set(connection.execute("""SELECT exchange,contract_code
            FROM futures_settlement_parameter_official WHERE quality_status='PASS'
            AND parser_version=? AND length(payload_sha256)=64
            AND spec_long_rate BETWEEN 0 AND 1 AND spec_short_rate BETWEEN 0 AND 1
            AND (trade_date=? OR (effective_from<=? AND effective_to>=?
                AND interval_source_url IS NOT NULL AND length(interval_payload_sha256)=64))""",
            [OFFICIAL_PARSER, day, day, day]).fetchall())
        publications = verified_publications(connection, OFFICIAL_PARSER)
        selected_day = applicable_publication_on_or_before(
            sorted({published for published, _, _ in publications}),
            known_publication_days(connection, OFFICIAL_PARSER), day,
        )
        if selected_day is not None:
            accepted = {(url, digest) for published, url, digest in publications
                        if published == selected_day}
            for contract, url, digest, start, end, interval_url, interval_sha in connection.execute(
                "SELECT contract_code,source_url,payload_sha256,effective_from,effective_to,"
                "interval_source_url,interval_payload_sha256 "
                "FROM futures_settlement_parameter_official WHERE exchange='CFFEX' AND trade_date=? "
                "AND quality_status='PASS' AND parser_version=? "
                "AND spec_long_rate BETWEEN 0 AND 1 AND spec_short_rate BETWEEN 0 AND 1",
                [selected_day, OFFICIAL_PARSER],
            ).fetchall():
                if (url, digest) in accepted and (interval_url is None or (
                    start <= day <= end and len(str(interval_sha or "")) == 64
                )):
                    official.add(("CFFEX", contract))
    cursor = connection.execute(f"SELECT {','.join(_COLUMNS)} FROM {TABLE} WHERE trade_date=? "
        "AND quality_status=? AND source_kind=? AND parser_version=? AND margin_rate_scope=? AND length(payload_sha256)=64 "
        "ORDER BY retrieved_at", [day, QUALITY_STATUS, SOURCE_KIND, PARSER_VERSION, RATE_SCOPE])
    result = {}
    for values in cursor.fetchall():
        row = dict(zip(_COLUMNS, values, strict=True))
        key = row["exchange"], row["contract_code"]
        if key not in official and _valid_rate(row["spec_long_rate"]) and _valid_rate(row["spec_short_rate"]):
            result[key] = row
    return result


def sync_current_backup(data_root: Path, connection: Any, day: str, *, session: Any = None,
                        timeout: float = 20, latest_completed_day: str | None = None) -> dict[str, Any]:
    """Discover one explicitly dated current table; never backfill broker history."""
    from .market_official_futures_metrics import completed_day

    latest = latest_completed_day or completed_day(Path(data_root))
    if day != latest:
        return {"status": "SKIPPED_NON_CURRENT_DAY", "trade_date": day, "record_count": 0}
    connection = getattr(connection, "connection", connection)
    directory = Path(data_root) / BRONZE_ROOT / "MINFUTURES" / day
    meta_path = directory / "result.json"
    client = session or requests.Session()
    headers = {"User-Agent": "Mozilla/5.0"}
    raw = b""
    metadata = {}
    if meta_path.exists():
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            if (not re.fullmatch(r"[0-9a-f]{64}", str(metadata.get("payload_sha256") or ""))
                    or metadata.get("raw_file") != metadata["payload_sha256"] + ".raw"):
                raise ValueError("backup cached original filename differs from SHA")
            raw = (directory / metadata["raw_file"]).read_bytes()
            if metadata.get("trade_date") != day or sha256(raw).hexdigest() != metadata.get("payload_sha256"):
                raw = b""
        except (ValueError, OSError, KeyError):
            raw = b""
    if not raw:
        response = client.get("https://www.minfutures.com/main/home/index.shtml", headers=headers, timeout=timeout)
        if response.status_code != 200:
            raise ValueError(f"backup catalog HTTP {response.status_code}")
        title = day.replace("-", "") + "期货保证金率及涨跌停板"
        soup = BeautifulSoup(response.content, "html.parser")
        links = {_source_url(urljoin("https://www.minfutures.com", anchor["href"]))
                 for anchor in soup.select("a[href]") if anchor.get_text(" ", strip=True) == title}
        if len(links) != 1:
            raise ValueError("backup catalog has no unique current dated original")
        url = links.pop()
        response = client.get(url, headers=headers, timeout=timeout)
        if response.status_code != 200:
            raise ValueError(f"backup original HTTP {response.status_code}")
        raw = response.content
        metadata = {"trade_date": day, "source_url": url,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    rows = parse_minfutures(raw, trading_day=day, source_url=metadata["source_url"], retrieved_at=metadata["retrieved_at"])
    cursor = connection.execute("SELECT DISTINCT trade_date,exchange,contract_code,product_code,quality_status "
        "FROM futures_contract_daily_official WHERE trade_date=? AND exchange='CFFEX' AND quality_status='PASS'", [day])
    columns = [item[0] for item in cursor.description]
    contracts = [dict(zip(columns, values, strict=True)) for values in cursor.fetchall()]
    digest = sha256(raw).hexdigest()
    _atomic_bytes(directory / (digest + ".raw"), raw)
    count = import_backup(connection, rows, contracts)
    metadata.update(status=QUALITY_STATUS, source_kind=SOURCE_KIND, parser_version=PARSER_VERSION,
                    payload_sha256=digest, raw_file=digest + ".raw", record_count=count)
    _save_meta(meta_path, metadata)
    return metadata


__all__ = ("ensure_schema", "parse_minfutures", "import_backup", "current_backup_margins", "sync_current_backup")
