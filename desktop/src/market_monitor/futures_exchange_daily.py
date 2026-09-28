"""Source-isolated exchange daily turnover observations for contract heat."""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence

from .futures import contract_delivery_month
from .storage import MarketStore


ADAPTER = "akshare.get_futures_daily"
DCE_ADAPTER = "dce.official.dayQuotes"
TURNOVER_UNIT = "CNY_10K"
TURNOVER_SIDEDNESS = "ONE_SIDED"
EXCHANGES = ("SHFE", "INE", "DCE", "CZCE", "GFEX", "CFFEX")
AUDITED_TURNOVER_EXCHANGES = frozenset({"SHFE", "INE", "CZCE", "GFEX", "CFFEX"})
SOURCE_URLS = {
    "SHFE": "https://www.shfe.com.cn/reports/tradedata/dailyandweeklydata/?query_params=kx",
    "INE": "https://www.ine.cn/reports/tradedata/dailyandweeklydata/?query_params=kx",
    "DCE": "http://www.dce.com.cn/dcereport/publicweb/dailystat/dayQuotes",
    "CZCE": "https://www.czce.com.cn/cn/jysj/mrhq/H077003001index_1.htm",
    "GFEX": "http://www.gfex.com.cn/gfex/rihq/hqsj_tjsj.shtml",
    "CFFEX": "https://www.cffex.com.cn/cn/rtj.html",
}
_CONTRACT = re.compile(r"^(?P<product>[A-Z]+)\d{3,4}$")

DailyFetcher = Callable[[str, str], Any]


def _number(value: Any, *, positive: bool = False) -> float | None:
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        return None
    return number


def _date_text(value: Any) -> str:
    text = str(value or "").strip().replace("-", "")[:8]
    return text if len(text) == 8 and text.isdigit() else ""


def normalize_exchange_daily(
    records: Sequence[Mapping[str, Any]], *, exchange: str, trading_day: str,
    retrieved_at: str, payload_sha256: str, adapter: str = ADAPTER,
) -> list[dict[str, Any]]:
    """Validate one official daily table without guessing missing turnover."""

    venue = exchange.upper()
    if venue not in EXCHANGES:
        raise ValueError(f"unsupported futures exchange: {exchange}")
    if venue not in AUDITED_TURNOVER_EXCHANGES:
        raise ValueError("DCE official daily turnover unit and sidedness need a successful source audit")
    day = date.fromisoformat(trading_day)
    if day < date(2026, 1, 1):
        raise ValueError("pre-2026 exchange turnover sidedness requires a separate historical audit")
    selected: dict[str, dict[str, Any]] = {}
    for record in records:
        if _date_text(record.get("date")) != day.strftime("%Y%m%d"):
            continue
        source_code = str(record.get("symbol") or "").strip().upper()
        matched = _CONTRACT.fullmatch(source_code)
        if matched is None:
            continue
        delivery = contract_delivery_month(
            source_code, venue, reference_day=day, last_trading_day=day
        )
        if delivery is None or delivery < day.replace(day=1):
            continue
        close = _number(record.get("close"), positive=True)
        turnover = _number(record.get("turnover"))
        if close is None or turnover is None:
            continue
        product = matched.group("product")
        code = f"{product}{delivery.year % 100:02d}{delivery.month:02d}"
        if code in selected:
            raise ValueError(f"duplicate official contract row: {venue}.{code} on {trading_day}")
        selected[code] = {
            "trade_date": trading_day,
            "exchange": venue,
            "contract_code": code,
            "product_code": product,
            "close": close,
            "settlement": _number(record.get("settle"), positive=True),
            "volume": _number(record.get("volume")),
            "open_interest": _number(record.get("open_interest")),
            "turnover_raw": turnover,
            "turnover_cny": turnover * 10_000,
            "turnover_unit": TURNOVER_UNIT,
            "turnover_sidedness": TURNOVER_SIDEDNESS,
            "source_url": SOURCE_URLS[venue],
            "adapter": adapter,
            "payload_sha256": payload_sha256,
            "retrieved_at": retrieved_at,
            "quality_status": "PASS",
        }
    return [selected[code] for code in sorted(selected)]


def _fetch(exchange: str, trading_day: str) -> Any:
    if exchange == "DCE":
        return _fetch_dce_official_json(trading_day)
    import akshare as ak

    compact = trading_day.replace("-", "")
    return ak.get_futures_daily(start_date=compact, end_date=compact, market=exchange)


def _fetch_dce_official_json(trading_day: str) -> list[dict[str, Any]]:
    """Read the public DCE dayQuotes response; normalization remains unit-gated."""

    import requests

    response = requests.post(
        SOURCE_URLS["DCE"],
        json={
            "contractId": "", "lang": "zh", "optionSeries": "",
            "statisticsType": "0", "tradeDate": trading_day.replace("-", ""),
            "tradeType": "1", "varietyId": "all",
        },
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
        raise ValueError("DCE dayQuotes returned no data array")
    records: list[dict[str, Any]] = []
    for item in payload["data"]:
        if not isinstance(item, Mapping):
            continue
        records.append({
            **item,
            "date": trading_day.replace("-", ""),
            "symbol": item.get("contractId"),
            "settle": item.get("clearPrice"),
            "volume": item.get("volumn"),
            "open_interest": item.get("openInterest"),
        })
    return records


def collect_exchange_daily_turnover(
    data_root: Path, *, trading_day: str, exchanges: Sequence[str] = EXCHANGES,
    fetcher: DailyFetcher | None = None,
) -> dict[str, Any]:
    """Persist normalized official facts and per-exchange failures separately."""

    day = date.fromisoformat(trading_day).isoformat()
    selected_exchanges = tuple(dict.fromkeys(str(value).upper() for value in exchanges))
    if not selected_exchanges or any(value not in EXCHANGES for value in selected_exchanges):
        raise ValueError("exchanges must be a non-empty set of domestic futures exchanges")
    provider = fetcher or _fetch
    outcomes: list[dict[str, Any]] = []
    store = MarketStore(Path(data_root))
    try:
        for exchange in selected_exchanges:
            adapter = DCE_ADAPTER if exchange == "DCE" and fetcher is None else ADAPTER
            run_id = store.begin_run(f"OFFICIAL_FUTURES_DAILY_{exchange}")
            retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            rows: list[dict[str, Any]] = []
            payload_hash: str | None = None
            record_count = 0
            status = "FAILED"
            error: str | None = None
            try:
                frame = provider(exchange, day)
                to_records = getattr(frame, "to_dict", None)
                raw = to_records(orient="records") if callable(to_records) else frame
                if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
                    raise ValueError("provider did not return daily contract records")
                raw_rows = json.loads(json.dumps(raw, ensure_ascii=False, default=str))
                record_count = len(raw_rows)
                payload_bytes = json.dumps(
                    {"exchange": exchange, "tradingDay": day, "rows": raw_rows},
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")
                payload_hash = hashlib.sha256(payload_bytes).hexdigest()
                store.write_bronze(
                    run_id, f"official_futures_daily_{exchange.lower()}",
                    {"sourceUrl": SOURCE_URLS[exchange], "adapter": adapter,
                     "tradingDay": day, "payloadSha256": payload_hash, "rows": raw_rows},
                )
                rows = normalize_exchange_daily(
                    raw_rows, exchange=exchange, trading_day=day,
                    retrieved_at=retrieved_at, payload_sha256=payload_hash,
                    adapter=adapter,
                )
                if not rows:
                    raise ValueError("provider returned no valid dated contract turnover rows")
                status = "PASS"
            except Exception as failure:
                error = f"{type(failure).__name__}: {failure}"[:300]
                rows = []
            coverage = {
                "trade_date": day, "exchange": exchange, "status": status,
                "record_count": record_count, "usable_count": len(rows),
                "adapter": adapter, "source_url": SOURCE_URLS[exchange],
                "payload_sha256": payload_hash, "error": error,
                "retrieved_at": retrieved_at,
            }
            store.record_futures_official_daily(rows, coverage)
            store.finish_run(run_id, status, error)
            outcomes.append({
                "exchange": exchange, "status": status, "recordCount": record_count,
                "usableCount": len(rows), "error": error,
            })
    finally:
        store.close()
    return {
        "tradingDay": day,
        "status": "PASS" if all(item["status"] == "PASS" for item in outcomes) else "PARTIAL",
        "exchanges": outcomes,
    }


__all__ = (
    "ADAPTER", "DCE_ADAPTER", "EXCHANGES", "SOURCE_URLS",
    "collect_exchange_daily_turnover", "normalize_exchange_daily",
)
