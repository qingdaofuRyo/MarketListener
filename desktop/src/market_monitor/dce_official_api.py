"""Two independent authorized DCE queues, with one formal data writer."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from requests import RequestException
from requests.exceptions import SSLError

from .atomic_files import replace_with_retry
from .dce_authorized_backfill import ApiFailure, DceClient, save_json, source_base
from .dce_credentials import load_accounts
from .futures_official_daily_sources import PARSER_VERSION as DAILY_VERSION, parse_official_daily
from .futures_settlement_parameters import PARSER_VERSION as MARGIN_VERSION, parse_dce


DAILY_PATH = "/dceapi/forward/publicweb/dailystat/dayQuotes"
MARGIN_PATH = "/dceapi/forward/publicweb/tradepara/futAndOptSettle"


def _directory(root: Path, kind: str, day: str) -> Path:
    if kind == "settlement":
        return root / "bronze" / "official_futures_settlement" / "DCE" / day
    return root / "bronze" / "dce_official_api" / "daily" / day


def _parse(kind: str, day: str, raw: bytes, meta: dict[str, Any]) -> list[dict[str, Any]]:
    if kind == "daily":
        return parse_official_daily(raw, exchange="DCE", trading_day=day,
                                    source_url=meta["source_url"], retrieved_at=meta["retrieved_at"])
    return parse_dce(raw, trading_day=day, source_url=meta["source_url"],
                     retrieved_at=meta["retrieved_at"], margin_unit="DECIMAL",
                     request_evidence=meta["request_evidence"])


def _cached(root: Path, kind: str, day: str) -> dict[str, Any] | None:
    directory = _directory(root, kind, day)
    try:
        meta = json.loads((directory / "result.json").read_text(encoding="utf-8"))
        if (meta.get("status") != "PASS" or meta.get("transport_kind") != "AUTHORIZED_OFFICIAL_API"
                or meta.get("parser_version") != (DAILY_VERSION if kind == "daily" else MARGIN_VERSION)):
            return None
        raw_name = str(meta.get("raw_file") or "")
        if not raw_name or Path(raw_name).name != raw_name:
            return None
        raw = (directory / raw_name).read_bytes()
        if sha256(raw).hexdigest() != meta.get("payload_sha256"):
            return None
        return {**meta, "rows": _parse(kind, day, raw, meta), "cached": True}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _fetch(root: Path, client: DceClient, label: str, kind: str, day: str) -> dict[str, Any]:
    body = {"varietyId": "all", "tradeDate": day.replace("-", ""), "tradeType": "1", "lang": "zh"}
    if kind == "daily":
        body.update(contractId="", optionSeries="", statisticsType="0")
        _, raw = client.request("dayQuotes", body)
        url = source_base(client) + DAILY_PATH
    else:
        _, raw = client.request("futAndOptSettle", body, namespace="tradepara")
        url = source_base(client) + MARGIN_PATH
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    submitted = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    evidence = {"method": "POST", "source_url": url, "post_data": submitted,
                "post_data_sha256": sha256(submitted.encode()).hexdigest(), "trade_date_verified": True,
                "transport_kind": "AUTHORIZED_OFFICIAL_API"}
    meta = {"kind": kind, "trade_date": day, "exchange": "DCE", "source_url": url,
            "retrieved_at": timestamp, "payload_sha256": sha256(raw).hexdigest(),
            "parser_version": DAILY_VERSION if kind == "daily" else MARGIN_VERSION,
            "request_evidence": evidence, "transport_kind": "AUTHORIZED_OFFICIAL_API",
            "margin_unit": "DECIMAL" if kind == "settlement" else None, "account_label": label}
    rows = _parse(kind, day, raw, meta)
    meta.update(status="PASS", record_count=len(rows), raw_file=meta["payload_sha256"] + ".raw")
    directory = _directory(root, kind, day)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / (meta["raw_file"] + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(raw)
        replace_with_retry(temporary, directory / meta["raw_file"])
    finally:
        temporary.unlink(missing_ok=True)
    save_json(directory / "result.json", meta)
    return {**meta, "rows": rows, "cached": False}


def capture_items(root: Path, items: list[tuple[str, str]], *, accounts=None, client_factory=None) -> dict[str, Any]:
    """A failed/throttled account cannot stop or delay the other queue."""
    root = Path(root)
    values = accounts if accounts is not None else load_accounts()
    if not values:
        return {"status": "UNCONFIGURED", "accounts": {}, "outcomes": []}
    if len(values) not in {1, 2}:
        raise ValueError("One or two authorized DCE accounts are required")
    factory = client_factory or DceClient
    pending = []
    cached = []
    for kind, day in list(dict.fromkeys(items)):
        if kind not in {"daily", "settlement"}:
            raise ValueError("Unsupported DCE source kind")
        result = _cached(root, kind, day)
        (cached if result is not None else pending).append(result if result is not None else (kind, day))

    def worker(index: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        label = "AB"[index]
        account = values[index]
        client = factory(root, account["api_key"], account["secret"], account_policy=(1, 3))
        assigned = pending[index::len(values)]
        result = []
        paused = None
        stats = {"status": "PASS", "successfulResponses": 0, "windowSeconds": 1,
                 "requestsPerWindow": 3, "dailyLimit": None,
                 "requestScheme": source_base(client).split(":", 1)[0] if assigned else None}
        try:
            for kind, day in assigned:
                if paused is not None:
                    result.append({"kind": kind, "trade_date": day, "status": "DEFERRED",
                                   "account_label": label, "error": paused})
                    continue
                try:
                    result.append(_fetch(root, client, label, kind, day))
                    stats["successfulResponses"] += 1
                except ApiFailure as error:
                    paused = f"API_CODE_{error.code}"
                    stats.update(status="COOLDOWN_WAIT" if error.code == 501 else "FAILED", apiCode=error.code)
                    result.append({"kind": kind, "trade_date": day, "status": "DEFERRED",
                                   "account_label": label, "error": paused})
                except (RequestException, ValueError, OSError) as error:
                    paused = "TLS_VERIFICATION_FAILED" if isinstance(error, SSLError) else type(error).__name__
                    stats.update(status="TLS_BLOCKED" if isinstance(error, SSLError) else "FAILED", error=paused)
                    if isinstance(error, RequestException) and not isinstance(error, SSLError):
                        stats["transportStatus"] = "NETWORK_ERROR"
                    result.append({"kind": kind, "trade_date": day, "status": "FAILED",
                                   "account_label": label, "error": paused})
        finally:
            policy = getattr(client, "account_policy", None)
            if policy:
                stats.update(windowSeconds=policy[0], requestsPerWindow=policy[1])
            client.close()
        return result, stats

    outcomes = list(cached)
    statuses = {}
    with ThreadPoolExecutor(max_workers=len(values), thread_name_prefix="dce-official-api") as pool:
        jobs = [pool.submit(worker, index) for index in range(len(values))]
        for index, job in enumerate(jobs):
            results, status = job.result()
            outcomes.extend(results)
            statuses["AB"[index]] = status
    transport_blocked = any(item.get("status") == "TLS_BLOCKED" for item in statuses.values())
    network_error = any(item.get("transportStatus") == "NETWORK_ERROR" for item in statuses.values())
    approved_http = any(item.get("requestScheme") == "http" for item in statuses.values())
    report = {"status": "TLS_BLOCKED" if transport_blocked else
              "PASS" if all(row["status"] == "PASS" for row in outcomes) else "PARTIAL",
              "transportStatus": "TLS_VERIFICATION_FAILED" if transport_blocked else
              "NETWORK_ERROR" if network_error else
              "USER_APPROVED_HTTP" if approved_http else "VERIFIED_OR_NOT_USED",
              "accounts": statuses, "outcomes": outcomes}
    save_json(root / "state" / "dce_official_api_status.json", {
        **report, "outcomes": [{key: value for key, value in row.items() if key != "rows"} for row in outcomes],
    })
    return report


def _covered_settlement_days(root: Path, days: list[str]) -> set[str]:
    """Reuse only matching facts, parser and preserved official originals."""
    from .storage import MarketStore

    store = MarketStore(root)
    try:
        records = store.connection.execute(
            """WITH facts AS (
                 SELECT trade_date,source_url,count(*) n,
                        count(DISTINCT payload_sha256) sha_n,min(payload_sha256) sha
                 FROM futures_settlement_parameter_official
                 WHERE exchange='DCE' AND parser_version=? AND quality_status='PASS'
                 GROUP BY 1,2
               )
               SELECT c.trade_date,c.payload_sha256 FROM futures_settlement_parameter_coverage c
               JOIN facts f USING(trade_date,source_url)
               WHERE c.exchange='DCE' AND c.status='PASS' AND c.parser_version=?
                 AND c.record_count=f.n AND c.usable_count=f.n AND f.sha_n=1
                 AND c.payload_sha256=f.sha AND length(c.payload_sha256)=64""",
            [MARGIN_VERSION, MARGIN_VERSION],
        ).fetchall()
    finally:
        store.close()
    expected = set(days)
    covered = set()
    for day, digest in records:
        day = str(day)
        if day not in expected:
            continue
        directory = _directory(root, "settlement", day)
        try:
            meta = json.loads((directory / "result.json").read_text(encoding="utf-8"))
            name = str(meta.get("raw_file") or "")
            if (name and Path(name).name == name and meta.get("payload_sha256") == digest
                    and sha256((directory / name).read_bytes()).hexdigest() == digest):
                covered.add(day)
        except (OSError, ValueError, TypeError):
            continue
    return covered


def update_official_dce_api(root: Path, days: list[str]) -> dict[str, Any]:
    """Only the calling backend writer promotes verified API originals."""
    from .futures_contract_heat_update import _missing_dce_days
    from .futures_settlement_parameters import import_partition
    from .storage import MarketStore

    accounts = load_accounts()
    if not accounts:
        return {"status": "UNCONFIGURED", "pendingDays": len(days)}
    daily_missing = _missing_dce_days(root, days)
    covered = _covered_settlement_days(root, days)
    items = [("daily", day) for day in daily_missing]
    items.extend(("settlement", day) for day in days if day not in covered)
    report = capture_items(root, items, accounts=accounts)
    imported = {"daily": 0, "settlement": 0}
    store = MarketStore(root)
    try:
        store.register_default_datasets()
        for result in sorted(report["outcomes"], key=lambda row: (row["trade_date"], row["kind"] != "daily")):
            if result["status"] != "PASS":
                continue
            rows = result["rows"]
            if result["kind"] == "daily":
                store.record_futures_official_daily(rows, {
                    "trade_date": result["trade_date"], "exchange": "DCE", "status": "PASS",
                    "adapter": "official-dce-daily-v2", "parser_version": DAILY_VERSION,
                    "record_count": len(rows), "usable_count": len(rows), "error": None,
                    "source_url": result["source_url"], "payload_sha256": result["payload_sha256"],
                    "retrieved_at": result["retrieved_at"],
                })
                imported["daily"] += len(rows)
            else:
                imported["settlement"] += import_partition(store.connection, rows, exchange="DCE",
                    trade_date=result["trade_date"], source_url=result["source_url"],
                    payload_sha256=result["payload_sha256"], retrieved_at=result["retrieved_at"])
    finally:
        store.close()
    # Official facts invalidate the financial evidence digest at publication.
    # They never change Silver Parquet or its K-line query index.
    daily_pending = _missing_dce_days(root, days)
    settlement_pending = sorted(set(days) - _covered_settlement_days(root, days))
    pending = set(daily_pending) | set(settlement_pending)
    return {"status": "TLS_BLOCKED" if report["status"] == "TLS_BLOCKED" else
            "PARTIAL" if pending else report["status"], "transportStatus": report["transportStatus"],
            "accounts": report["accounts"], "importedRows": imported,
            "pendingDays": len(pending), "pendingDailyDays": len(daily_pending),
            "pendingSettlementDays": len(settlement_pending)}
