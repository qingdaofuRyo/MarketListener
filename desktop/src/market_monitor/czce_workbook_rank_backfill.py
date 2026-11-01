"""Resumable official CZCE daily member ranking workbooks to Bronze."""
from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from typing import Any
import zipfile

import requests

from .czce_html_rank_backfill import _read_json, _write_bytes, _write_json
from .czce_rank_workbook import parse_czce_rank_workbook_checked
from .futures_calendar import load_futures_trading_calendar
from .futures_member_positions import normalise_exchange_member_position_ranks
from .futures_member_trade_ranks import normalise_exchange_member_trade_ranks
from .futures_official_backfill import GovernedRequestsSession, RequestBudgetExhausted, _process_file_lock

SCHEMA = "czce-workbook-rank-capture/v1"
ADAPTER = "official-czce-rank-workbook-raw-v1"
URL = "https://www.czce.com.cn/cn/DFSStaticFiles/Future/{year}/{day}/FutureDataHolding.{extension}"
_BRONZE = Path("bronze") / "czce_workbook_rank_capture"
_STATE = Path("state") / "czce_workbook_rank_capture"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _source_url(day: str, extension: str | None = None) -> str:
    compact = day.replace("-", "")
    chosen = extension or ("xls" if day <= "2025-11-01" else "xlsx")
    return URL.format(year=compact[:4], day=compact, extension=chosen)


def _completed(root: Path, day: str) -> dict[str, Any] | None:
    record = _read_json(root / _BRONZE / f"{day}.json")
    url = record.get("sourceUrl") if record else None
    extension = str(url or "").rsplit(".", 1)[-1]
    raw_path = root / _BRONZE / f"{day}.{extension}"
    if (record is None or record.get("schema") != SCHEMA or record.get("status") != "PASS_RAW"
            or record.get("tradingDay") != day or url not in {_source_url(day, "xls"), _source_url(day, "xlsx")}
            or record.get("authoritativeDataWritten") is not False or not raw_path.is_file()):
        return None
    return record if sha256(raw_path.read_bytes()).hexdigest() == record.get("payloadSha256") else None


class CzceWorkbookClient:
    def __init__(self, root: Path, *, min_interval_seconds: float = 2,
                 session: requests.Session | None = None) -> None:
        self.governed = GovernedRequestsSession(root, min_interval_seconds=min_interval_seconds, session=session)

    def get(self, day: str) -> requests.Response:
        first_url = _source_url(day)
        response = self.governed.request("GET", first_url, timeout=20, allow_redirects=False)
        urls = [first_url]
        if response.status_code == 404:
            alternative = _source_url(day, "xlsx" if first_url.endswith(".xls") else "xls")
            response = self.governed.request("GET", alternative, timeout=20, allow_redirects=False)
            urls.append(alternative)
        response.source_url = urls[-1]
        response.attempted_urls = urls
        return response

    def close(self) -> None:
        self.governed.close()


def capture_czce_workbook_ranks(root: Path, client: CzceWorkbookClient, *, start_day: str = "2025-01-01",
                                end_day: str = "2026-09-30", max_days: int | None = None,
                                retry_missing: bool = False) -> dict[str, Any]:
    if date.fromisoformat(start_day) > date.fromisoformat(end_day) or (max_days is not None and max_days < 1):
        raise ValueError("invalid CZCE ranking range or max_days")
    root = Path(root)
    calendar = load_futures_trading_calendar(root)
    if calendar is None:
        raise ValueError("missing verified futures trading calendar")
    days = calendar.between(start_day, end_day)
    if not days:
        raise ValueError("no verified futures trading days in CZCE ranking range")
    state_dir = root / _STATE
    bronze_dir = root / _BRONZE
    progress = {"schema": SCHEMA, "publisher": "CZCE", "adapter": ADAPTER, "status": "RUNNING",
                "startDay": start_day, "endDay": end_day, "calendarDays": len(days),
                "completedDays": 0, "missingDays": 0, "newDaysThisRun": 0,
                "authoritativeDataWritten": False,
                "updatedAt": _timestamp()}
    with _process_file_lock(state_dir / "capture.lock"):
        try:
            for day in days:
                progress.update(currentDay=day, updatedAt=_timestamp())
                _write_json(state_dir / "progress.json", progress)
                complete = _completed(root, day)
                state_path = state_dir / f"{day}.json"
                if complete is not None:
                    if _read_json(state_path) != complete:
                        _write_json(state_path, complete)
                    progress["completedDays"] += 1
                    continue
                missing = _read_json(state_path)
                if (not retry_missing and missing is not None
                        and missing.get("status") == "MISSING_REPORT"
                        and missing.get("tradingDay") == day
                        and missing.get("authoritativeDataWritten") is False):
                    progress["missingDays"] += 1
                    continue
                if max_days is not None and progress["newDaysThisRun"] >= max_days:
                    progress["status"] = "BATCH_LIMIT"
                    break
                progress["newDaysThisRun"] += 1
                record = {"schema": SCHEMA, "publisher": "CZCE", "adapter": ADAPTER,
                          "tradingDay": day, "sourceUrl": _source_url(day), "retrievedAt": _timestamp(),
                          "status": "FAILED", "qualityStatus": "FAILED", "authoritativeDataWritten": False,
                          "httpStatus": None, "contentType": None, "payloadSha256": None}
                try:
                    response = client.get(day)
                    raw = bytes(response.content)
                    actual_url = str(getattr(response, "source_url", _source_url(day)))
                    if actual_url not in {_source_url(day, "xls"), _source_url(day, "xlsx")}:
                        raise ValueError("official response came from an unexpected path")
                    record.update(retrievedAt=_timestamp(), httpStatus=int(response.status_code),
                                  sourceUrl=actual_url,
                                  attemptedUrls=getattr(response, "attempted_urls", [actual_url]),
                                  contentType=str(response.headers.get("Content-Type") or "") or None,
                                  payloadSha256=sha256(raw).hexdigest(), bytes=len(raw))
                    if response.status_code == 404 and len(set(record["attemptedUrls"])) == 2:
                        record.update(status="MISSING_REPORT", qualityStatus="UNKNOWN",
                                      error="both official workbook formats returned HTTP 404")
                        _write_json(state_path, record)
                        progress["missingDays"] += 1
                        print(json.dumps({"day": day, "status": "MISSING_REPORT", "httpStatus": 404}), flush=True)
                        continue
                    if response.status_code != 200:
                        raise ValueError(f"official HTTP {response.status_code}")
                    if len(raw) > 16 * 1024 * 1024 or not (
                        zipfile.is_zipfile(BytesIO(raw)) or raw.startswith(bytes.fromhex("d0cf11e0a1b11ae1"))
                    ):
                        raise ValueError("official response is not a bounded XLS/XLSX workbook")
                    extension = actual_url.rsplit(".", 1)[-1]
                    raw_path = bronze_dir / f"{day}.{extension}"
                    _write_bytes(raw_path, raw)
                    record.update(status="PENDING_VALIDATION", qualityStatus="PENDING_VALIDATION",
                                  rawPath=raw_path.relative_to(root).as_posix())
                    _write_json(bronze_dir / f"{day}.json", record)
                    frames = parse_czce_rank_workbook_checked(raw, trading_day=day.replace("-", ""))
                    positions = normalise_exchange_member_position_ranks(
                        frames, exchange="CZCE", trading_day=day, source=ADAPTER,
                        collected_at=record["retrievedAt"])
                    trades = normalise_exchange_member_trade_ranks(
                        frames, exchange="CZCE", trading_day=day, source=ADAPTER,
                        collected_at=record["retrievedAt"])
                    record.update(status="PASS_RAW", qualityStatus="PASS_RAW",
                                  contractCount=len(frames), rankingRows=sum(len(f) for f in frames.values()),
                                  positionRows=len(positions), tradeRows=len(trades))
                    _write_json(bronze_dir / f"{day}.json", record)
                    _write_json(state_path, record)
                    progress["completedDays"] += 1
                    print(json.dumps({"day": day, "status": "PASS_RAW", "contracts": len(frames),
                                      "rankRows": record["rankingRows"]}, ensure_ascii=False), flush=True)
                except RequestBudgetExhausted as error:
                    record.update(status="BUDGET_PAUSED", qualityStatus="BUDGET_PAUSED",
                                  errorType=type(error).__name__)
                    _write_json(state_path, record)
                    progress.update(status="BUDGET_PAUSED", errorDay=day)
                    break
                except Exception as error:
                    record.update(status="FAILED", qualityStatus="FAILED", errorType=type(error).__name__,
                                  error=str(error)[:240] if isinstance(error, ValueError) else "official request or file operation failed")
                    if record.get("rawPath"):
                        _write_json(bronze_dir / f"{day}.json", record)
                    _write_json(state_path, record)
                    progress.update(status="FAILED", errorDay=day, errorType=type(error).__name__,
                                    httpStatus=record["httpStatus"])
                    break
            else:
                progress["status"] = "PARTIAL_RAW" if progress["missingDays"] else "PASS_RAW"
        finally:
            progress["updatedAt"] = _timestamp()
            _write_json(state_dir / "progress.json", progress)
    return progress


__all__ = ("CzceWorkbookClient", "capture_czce_workbook_ranks")
