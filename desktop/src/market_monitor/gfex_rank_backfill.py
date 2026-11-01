"""Recoverable GFEX ranking capture to Bronze only, never a second DB writer."""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo

from .futures_calendar import load_futures_trading_calendar
from .atomic_files import replace_with_retry
from .futures_official_backfill import HostRateLimiter, RequestBudgetExhausted

import requests

BASE = "http://www.gfex.com.cn/u/"
VARIETIES = "interfacesWebVariety/loadList"
CONTRACTS = "interfacesWebTiMemberDealPosiQuotes/loadListContract_id"
RANKS = "interfacesWebTiMemberDealPosiQuotes/loadList"
CODE = re.compile(r"^[a-z]{1,3}\d{4}$")


def timestamp() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    replace_with_retry(temporary, path)


class GfexSourceError(RuntimeError):
    def __init__(self, http_status: int, api_code: Any = None, reason: str = "source rejected") -> None:
        self.http_status, self.api_code, self.reason = http_status, api_code, reason
        super().__init__(reason)


class GfexClient:
    def __init__(self, root: Path, cookie: str = "", interval: float = 2) -> None:
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "http://www.gfex.com.cn",
            "Referer": "http://www.gfex.com.cn/gfex/rcjccpm/hqsj_tjsj.shtml",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0",
            "X-Requested-With": "XMLHttpRequest",
        })
        if cookie:
            self.session.headers["Cookie"] = cookie
        self.limiter = HostRateLimiter(root, min_interval_seconds=interval)

    def fetch(self, endpoint: str, body: dict) -> tuple[list, dict]:
        self.limiter.acquire("www.gfex.com.cn")
        cooldown = 0
        try:
            response = self.session.post(BASE + endpoint, data=body, timeout=20, allow_redirects=False)
            if response.status_code == 429:
                try:
                    cooldown = max(120, float(response.headers.get("Retry-After") or 0))
                except ValueError:
                    cooldown = 120
        finally:
            self.limiter.complete("www.gfex.com.cn", retry_after_seconds=cooldown)
        if response.status_code != 200:
            raise GfexSourceError(response.status_code)
        try:
            payload = response.json()
        except ValueError as error:
            raise GfexSourceError(response.status_code, reason="non-JSON response; body not archived") from error
        if not isinstance(payload, dict) or str(payload.get("code")) != "0" or not isinstance(payload.get("data"), list):
            raise GfexSourceError(response.status_code, payload.get("code") if isinstance(payload, dict) else None)
        evidence = {"publisher": "GFEX", "adapter": "official-gfex-rank-raw-v1",
                    "sourceUrl": BASE + endpoint, "requestBody": body, "retrievedAt": timestamp(),
                    "payloadSha256": hashlib.sha256(response.content).hexdigest(),
                    "contentType": response.headers.get("Content-Type"),
                    "httpStatus": response.status_code, "apiCode": payload.get("code"),
                    "rawBase64": base64.b64encode(response.content).decode("ascii")}
        return payload["data"], evidence

    def close(self) -> None:
        self.session.headers.clear()
        self.session.cookies.clear()
        self.session.close()


def parse_varieties(rows: list) -> list[str]:
    products = []
    for row in rows:
        code = str(row.get("varietyId") or "").lower() if isinstance(row, dict) else ""
        if not re.fullmatch(r"[a-z]{1,3}", code):
            raise ValueError("Invalid official variety list")
        products.append(code)
    if not products or len(products) != len(set(products)):
        raise ValueError("Empty or duplicate variety list")
    return sorted(products)


def parse_contracts(rows: list, product: str) -> list[str]:
    codes = []
    for row in rows:
        if isinstance(row, dict):
            value = row.get("contract_id") or row.get("contractId")
            if value is None and len(row) == 1:
                value = next(iter(row.values()))
        else:
            value = row
        code = str(value or "").lower()
        if not CODE.fullmatch(code) or re.sub(r"\d", "", code) != product:
            raise ValueError("Invalid official ranking contract list")
        codes.append(code)
    if len(codes) != len(set(codes)):
        raise ValueError("Duplicate official contract")
    return sorted(codes)


def validate_rank_rows(rows: list) -> dict:
    ranks: set[int] = set()
    summaries = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid ranking row")
        rank = str(row.get("contractId") or "").strip()
        name = str(row.get("abbr") or "").strip()
        if rank in {"合计", "总计", "小计"} or name in {"合计", "总计", "小计"}:
            summaries += 1
            continue
        if not rank.isdigit() or int(rank) <= 0 or not name or int(rank) in ranks:
            raise ValueError("Invalid or duplicate published rank")
        qty = float(row["todayQty"])
        change = row.get("qtySub", row.get("todayQtyChg"))
        if not math.isfinite(qty) or qty < 0 or (change not in (None, "") and not math.isfinite(float(change))):
            raise ValueError("Invalid published quantity")
        ranks.add(int(rank))
    return {"publishedRows": len(ranks), "summaryRows": summaries,
            "status": "PASS" if ranks else "NO_PUBLISHED_ROWS"}


def capture(root: Path, client: GfexClient, start: str, end: str, max_days: int | None = None) -> dict:
    if start > end or (max_days is not None and max_days < 1):
        raise ValueError("Invalid range or batch size")
    calendar = load_futures_trading_calendar(root)
    if calendar is None:
        raise ValueError("Missing verified calendar")
    days = calendar.between(start, end)
    state_dir = root / "state" / "gfex_rank_capture"
    bronze_dir = root / "bronze" / "gfex_rank_capture"
    progress = {"status": "RUNNING", "startDay": start, "endDay": end, "calendarDays": len(days),
                "completedDays": 0, "directionRequestsThisRun": 0, "publishedRowsThisRun": 0,
                "authoritativeDataWritten": False, "pid": os.getpid(), "updatedAt": timestamp()}
    processed_days = 0
    try:
        # Refresh the official product catalogue once per invocation, not a
        # hand-maintained list. Each historical day keeps its own evidence.
        rows, evidence = client.fetch(VARIETIES, {})
        write_json(bronze_dir / "varieties-latest.json", evidence)
        products = parse_varieties(rows)
        for day in days:
            progress.update(currentDay=day, updatedAt=timestamp())
            state_path = state_dir / f"{day}.json"
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            if state.get("status") == "PASS_RAW" and state.get("products") == products:
                progress["completedDays"] += 1
                continue
            if max_days is not None and processed_days >= max_days:
                progress["status"] = "BATCH_LIMIT"
                break
            processed_days += 1
            state = state or {"schema": "gfex-rank-capture/v1", "tradingDay": day,
                             "products": products, "contractLists": {}, "directions": {}, "status": "PARTIAL"}
            state["products"] = products
            for product in products:
                progress.update(currentProduct=product, updatedAt=timestamp())
                write_json(state_dir / "progress.json", progress)
                if product not in state["contractLists"]:
                    rows, proof = client.fetch(CONTRACTS, {"variety": product, "trade_date": day.replace("-", "")})
                    write_json(bronze_dir / day / f"{product}-contracts.json", proof)
                    codes = parse_contracts(rows, product)
                    state["contractLists"][product] = {"contracts": codes, "payloadSha256": proof["payloadSha256"],
                        "status": "PASS" if codes else "NO_PUBLISHED_CONTRACTS"}
                    write_json(state_path, state)
                for contract in state["contractLists"][product]["contracts"]:
                    for direction in (1, 2, 3):
                        checkpoint = f"{contract}.{direction}"
                        if checkpoint in state["directions"]:
                            continue
                        progress.update(currentContract=contract, currentDirection=direction, updatedAt=timestamp())
                        write_json(state_dir / "progress.json", progress)
                        rows, proof = client.fetch(RANKS, {"trade_date": day.replace("-", ""), "trade_type": "0",
                            "variety": product, "contract_id": contract, "data_type": str(direction)})
                        raw_path = bronze_dir / day / f"{checkpoint}.json"
                        write_json(raw_path, {**proof, "qualityStatus": "PENDING_VALIDATION"})
                        validation = validate_rank_rows(rows)
                        write_json(raw_path, {**proof, "qualityStatus": validation["status"], "validation": validation})
                        state["directions"][checkpoint] = {**validation, "payloadSha256": proof["payloadSha256"]}
                        write_json(state_path, state)
                        progress["directionRequestsThisRun"] += 1
                        progress["publishedRowsThisRun"] += validation["publishedRows"]
            state["status"] = "PASS_RAW"
            state["retrievedAt"] = timestamp()
            write_json(state_path, state)
            progress["completedDays"] += 1
            write_json(state_dir / "progress.json", progress)
            print(json.dumps({"day": day, "status": "PASS_RAW", "publishedContracts":
                sum(len(v["contracts"]) for v in state["contractLists"].values()),
                "publishedRows": sum(v["publishedRows"] for v in state["directions"].values())}, ensure_ascii=False), flush=True)
        else:
            progress["status"] = "PASS_RAW"
    except RequestBudgetExhausted:
        progress["status"] = "BUDGET_PAUSED"
    except Exception as error:
        progress["status"] = "FAILED"
        progress["errorType"] = type(error).__name__
        if isinstance(error, OSError) and error.filename:
            progress["errorFile"] = Path(error.filename).name
        if isinstance(error, GfexSourceError):
            progress.update(httpStatus=error.http_status, apiCode=error.api_code, reason=error.reason)
    finally:
        progress["updatedAt"] = timestamp()
        write_json(state_dir / "progress.json", progress)
    return progress
