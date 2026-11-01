"""Resumable CFFEX/SHFE official member rankings to Bronze, without Catalog."""

from __future__ import annotations

import argparse
import base64
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

from market_monitor.czce_html_rank_backfill import _read_json, _write_json
from market_monitor.futures_calendar import load_futures_trading_calendar
from market_monitor.futures_official_backfill import (
    GovernedRequestsSession,
    RequestBudgetExhausted,
    _process_file_lock,
)
from market_monitor.futures_official_member_sources import (
    DirectOfficialRankApi,
    cffex_rank_products,
    parse_cffex_rank_csv,
    parse_cffex_rank_xml,
    parse_shfe_rank_json,
)


SCHEMA = "official-member-rank-bronze-capture/v1"
ADAPTER = "official-member-rank-bronze-capture-v1"
STATE = Path("state") / "official_member_rank_bronze_capture"
URL_CFFEX_CSV = re.compile(r"/ccpm/(\d{4})(\d{2})/(\d{2})/([A-Z]+)_1\.csv$")
URL_CFFEX_XML = re.compile(r"/ccpm/(\d{4})(\d{2})/(\d{2})/([A-Z]+)\.xml$")
URL_SHFE = re.compile(r"/pm(\d{8})\.dat$")


def _shfe_success_code(payload: dict[str, Any]) -> bool:
    raw_code = payload.get("o_code")
    code = "" if raw_code is None else str(raw_code).strip()
    return code.zfill(4) == "0000"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _indexed_payloads(root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Index and revalidate available response manifests from prior runs."""

    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for directory in Path(root, "bronze").glob("official_futures_member_*"):
        if not directory.is_dir():
            continue
        for path in directory.glob("*.json"):
            manifest = _read_json(path)
            if not isinstance(manifest, dict):
                continue
            exchange = str(manifest.get("publisher") or "").upper()
            if exchange not in {"CFFEX", "SHFE"}:
                continue
            try:
                raw = base64.b64decode(manifest.get("contentBase64", ""), validate=True)
            except (ValueError, TypeError):
                continue
            from hashlib import sha256

            if sha256(raw).hexdigest() != manifest.get("payloadSha256"):
                continue
            source_url = str(manifest.get("sourceUrl") or "")
            if exchange == "CFFEX":
                csv_match = URL_CFFEX_CSV.search(source_url)
                xml_match = URL_CFFEX_XML.search(source_url.split("?", 1)[0])
                match = csv_match or xml_match
                if match is None:
                    continue
                compact_day = "".join(match.groups()[:3])
                product = match.group(4)
                try:
                    frames = (parse_cffex_rank_csv(raw, trading_day=compact_day)
                              if csv_match else
                              parse_cffex_rank_xml(raw, trading_day=compact_day, product=product))
                except (UnicodeError, ValueError, json.JSONDecodeError):
                    continue
                value = {"raw": raw, "manifest": manifest, "contracts": len(frames),
                         "rankRows": sum(len(frame) for frame in frames.values())}
            else:
                match = URL_SHFE.search(source_url)
                if match is None:
                    continue
                compact_day = match.group(1)
                try:
                    payload = json.loads(raw.decode("utf-8-sig"))
                    frames = parse_shfe_rank_json(raw)
                except (UnicodeError, ValueError, TypeError):
                    continue
                if payload.get("report_date") != compact_day or not _shfe_success_code(payload):
                    continue
                product = "ALL"
                value = {"raw": raw, "manifest": manifest, "contracts": len(frames),
                         "rankRows": sum(len(frame) for frame in frames.values())}
            indexed[(compact_day, product)] = value
    return indexed


def _save_payload(root: Path, exchange: str, day: str, item: dict[str, Any]) -> dict[str, Any]:
    response = item["response"]
    source_url = str(response["sourceUrl"])
    if exchange == "CFFEX":
        match = URL_CFFEX_CSV.search(source_url) or URL_CFFEX_XML.search(source_url.split("?", 1)[0])
        product = match.group(4) if match else "unknown"
    else:
        product = "ALL"
    directory = Path(root, "bronze", f"official_futures_member_{exchange.lower()}_2023_backfill")
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{day}_{product}_{response['payloadSha256'][:16]}.json"
    path = directory / name
    if not path.exists():
        _write_json(path, response)
    return {"path": path.relative_to(root).as_posix(), "sourceUrl": source_url,
            "payloadSha256": response["payloadSha256"], "contentType": response["contentType"],
            "retrievedAt": response["retrievedAt"], "product": product}


def _existing_day(index: dict[tuple[str, str], dict[str, Any]], day: str) -> dict[str, Any]:
    compact = day.replace("-", "")
    expected_products = cffex_rank_products(compact)
    cffex = {product: index[(compact, product)] for product in expected_products
             if (compact, product) in index}
    shfe = index.get((compact, "ALL"))
    return {"CFFEX": cffex, "SHFE": shfe}


def capture(
    root: Path, *, start_day: str, end_day: str, max_days: int | None,
    min_interval_seconds: float, daily_host_budget: int, dry_run: bool = False,
) -> dict[str, Any]:
    start, end = date.fromisoformat(start_day), date.fromisoformat(end_day)
    if start > end or max_days is not None and max_days < 1:
        raise ValueError("invalid member ranking capture range or max_days")
    if min_interval_seconds < 0.2 or daily_host_budget < 1:
        raise ValueError("request interval must be at least 0.2 seconds and budget positive")
    calendar = load_futures_trading_calendar(Path(root))
    if calendar is None:
        raise ValueError("verified futures trading calendar is missing")
    days = calendar.between(start.isoformat(), end.isoformat())
    if not days:
        raise ValueError("verified trading calendar has no days in range")

    state_dir = Path(root) / STATE
    progress: dict[str, Any] = {
        "schema": SCHEMA, "status": "RUNNING", "startDay": start.isoformat(),
        "endDay": end.isoformat(), "calendarDays": len(days), "completedDays": 0,
        "newDaysThisRun": 0, "authoritativeDataWritten": False, "updatedAt": _utc(),
    }
    with _process_file_lock(state_dir / "capture.lock"):
        indexed = _indexed_payloads(root)
        already_complete = [day for day in days if (
            len(_existing_day(indexed, day)["CFFEX"]) == len(cffex_rank_products(day.replace("-", "")))
            and _existing_day(indexed, day)["SHFE"] is not None
        )]
        pending = [day for day in days if day not in set(already_complete)]
        if dry_run:
            return {"status": "DRY_RUN", "calendarDays": len(days),
                    "verifiedExistingDays": len(already_complete),
                    "pendingDays": len(pending), "maxDays": max_days,
                    "firstPendingDay": pending[0] if pending else None,
                    "authoritativeDataWritten": False}
        progress["completedDays"] = len(already_complete)
        session = GovernedRequestsSession(
            root, min_interval_seconds=min_interval_seconds, daily_budget=daily_host_budget,
        )
        api = DirectOfficialRankApi(session=session)
        try:
            _write_json(state_dir / "progress.json", progress)
            selected = pending[:max_days] if max_days is not None else pending
            for day in selected:
                compact = day.replace("-", "")
                prior = _existing_day(indexed, day)
                day_manifest: dict[str, Any] = {
                    "schema": SCHEMA, "publisher": ["CFFEX", "SHFE"], "tradingDay": day,
                    "adapter": ADAPTER, "status": "RUNNING", "qualityStatus": "PENDING",
                    "authoritativeDataWritten": False, "requests": [], "updatedAt": _utc(),
                }
                try:
                    if prior["SHFE"] is not None:
                        shfe_summary = {key: prior["SHFE"][key] for key in ("contracts", "rankRows")}
                    else:
                        api.raw_artifacts.clear()
                        shfe_frames = api.get_shfe_rank_table(date=compact)
                        shfe_item = next(item for item in api.raw_artifacts
                                         if item["response"]["publisher"] == "SHFE")
                        payload = json.loads(base64.b64decode(
                            shfe_item["response"]["contentBase64"], validate=True
                        ).decode("utf-8-sig"))
                        if payload.get("report_date") != compact or not _shfe_success_code(payload):
                            raise ValueError("SHFE official report date or code differs")
                        saved = _save_payload(root, "SHFE", compact, shfe_item)
                        shfe_summary = {"contracts": len(shfe_frames),
                                        "rankRows": sum(len(frame) for frame in shfe_frames.values())}
                        day_manifest["requests"].append(saved)
                    if prior["CFFEX"] and len(prior["CFFEX"]) == len(cffex_rank_products(compact)):
                        cffex_summary = {
                            "products": sorted(prior["CFFEX"]),
                            "contracts": sum(item["contracts"] for item in prior["CFFEX"].values()),
                            "rankRows": sum(item["rankRows"] for item in prior["CFFEX"].values()),
                        }
                    else:
                        api.raw_artifacts.clear()
                        cffex_frames = api.get_cffex_rank_table(date=compact)
                        evidence = api.evidence["CFFEX"]
                        saved_products = set()
                        saved_items = [item for item in api.raw_artifacts
                                       if item["response"]["publisher"] == "CFFEX"]
                        for item in saved_items:
                            saved = _save_payload(root, "CFFEX", compact, item)
                            saved_products.add(saved["product"])
                            day_manifest["requests"].append(saved)
                        if evidence.get("quality_status") not in {"PASS", "NO_PUBLICATION"}:
                            raise ValueError(f"CFFEX source returned {evidence.get('quality_status')}")
                        expected = set(cffex_rank_products(compact))
                        no_publication = sorted(expected - saved_products)
                        cffex_summary = {
                            "products": sorted(saved_products), "noPublicationProducts": no_publication,
                            "contracts": len(cffex_frames),
                            "rankRows": sum(len(frame) for frame in cffex_frames.values()),
                        }
                    day_manifest.update(status="PASS_RAW", qualityStatus="PASS_RAW",
                                        sources={"CFFEX": cffex_summary, "SHFE": shfe_summary},
                                        updatedAt=_utc())
                    _write_json(state_dir / f"{compact}.json", day_manifest)
                    progress["completedDays"] += 1
                    progress["newDaysThisRun"] += 1
                    progress.update(currentDay=day, updatedAt=_utc())
                    _write_json(state_dir / "progress.json", progress)
                    print(json.dumps({"day": day, "status": "PASS_RAW",
                                      "cffexContracts": cffex_summary["contracts"],
                                      "shfeContracts": shfe_summary["contracts"]}, ensure_ascii=False), flush=True)
                except RequestBudgetExhausted as error:
                    day_manifest.update(status="BUDGET_PAUSED", qualityStatus="BUDGET_PAUSED",
                                        errorType=type(error).__name__)
                    _write_json(state_dir / f"{compact}.json", day_manifest)
                    progress.update(status="BUDGET_PAUSED", errorDay=day)
                    break
                except Exception as error:
                    day_manifest.update(status="FAILED", qualityStatus="FAILED",
                                        errorType=type(error).__name__, error=str(error)[:220])
                    _write_json(state_dir / f"{compact}.json", day_manifest)
                    progress.update(status="FAILED", errorDay=day, errorType=type(error).__name__)
                    break
            else:
                progress["status"] = "PASS_RAW" if len(selected) == len(pending) else "BATCH_LIMIT"
        finally:
            session.close()
            progress["updatedAt"] = _utc()
            _write_json(state_dir / "progress.json", progress)
    return progress


def main() -> int:
    parser = argparse.ArgumentParser(description="中金所/上期所排名官方原件回补（不写Catalog）")
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--start-day", default="2023-01-01")
    parser.add_argument("--end-day", default="2026-09-30")
    parser.add_argument("--max-days", type=int)
    parser.add_argument("--min-interval-seconds", type=float, default=1.0)
    parser.add_argument("--daily-host-budget", type=int, default=30000)
    parser.add_argument("--dry-run", action="store_true", help="只核对日历和已验证原件，不请求官网")
    args = parser.parse_args()
    result = capture(args.data_root, start_day=args.start_day, end_day=args.end_day,
                     max_days=args.max_days, min_interval_seconds=args.min_interval_seconds,
                     daily_host_budget=args.daily_host_budget, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if result["status"] in {"DRY_RUN", "PASS_RAW", "BATCH_LIMIT", "BUDGET_PAUSED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
