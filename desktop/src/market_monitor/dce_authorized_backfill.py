"""Authorized DCE ranking backfill; plaintext credentials are never persisted."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import time
from requests.exceptions import SSLError
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .futures_calendar import load_futures_trading_calendar
from .atomic_files import replace_with_retry
from .dce_account_rate import DceAccountRateLimiter
from .futures_member_positions import _member_key, _member_name
from .futures_official_backfill import (
    DEFAULT_DAILY_REQUEST_BUDGET, MIN_REQUEST_INTERVAL_SECONDS,
    GovernedRequestsSession, RequestBudgetExhausted, effective_request_policy,
)
from .storage import MarketStore

# HTTPS remains the default. User-approved HTTP applies only to the two local
# accounts explicitly fingerprinted in an ignored runtime consent file.
BASE = "https://www.dce.com.cn"
HTTP_BASE = "http://www.dce.com.cn"
HTTP_CONSENT = Path("state") / "dce_authorized_http_consent.json"
HTTP_CONSENT_SCHEMA = "dce-authorized-http-consent/v1"
SOURCE = "official-dce-member-ranking"
ADAPTER = "official-dce-authorized-rank-v2"
CONTRACT = re.compile(r"^([a-z]{1,3})(\d{4})(F)?$", re.I)


class ApiFailure(RuntimeError):
    def __init__(self, code: int | None) -> None:
        self.code = code
        super().__init__(f"DCE API code={code}; no response secrets logged")


def now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    replace_with_retry(temporary, path)


def authorized_base(root: Path, key: str) -> str:
    """Select HTTP only for a fingerprinted account on the exact official host."""
    try:
        consent = json.loads((Path(root) / HTTP_CONSENT).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return BASE
    if not isinstance(consent, dict) or set(consent) != {
        "schema", "scheme", "host", "account_key_sha256"
    } or (consent["schema"], consent["scheme"], consent["host"]) != (
        HTTP_CONSENT_SCHEMA, "http", "www.dce.com.cn"
    ):
        return BASE
    fingerprints = consent["account_key_sha256"]
    if (not isinstance(fingerprints, list) or len(fingerprints) != 2
            or any(not isinstance(item, str) or re.fullmatch(r"[0-9a-f]{64}", item) is None
                   for item in fingerprints)
            or len(set(fingerprints)) != 2):
        return BASE
    return HTTP_BASE if hashlib.sha256(key.encode("utf-8")).hexdigest() in fingerprints else BASE


def source_base(client: object) -> str:
    """Return the transport of the response just consumed (also supports test clients)."""
    base = getattr(client, "last_request_base_url", getattr(client, "base_url", BASE))
    if base not in {BASE, HTTP_BASE}:
        raise ValueError("Invalid DCE source host or scheme")
    return base


class DceClient:
    def __init__(self, root: Path, key: str, secret: str, interval: float = 2,
                 *, adaptive_rate: bool = False, account_policy: tuple[float, int] | None = None) -> None:
        self.root = Path(root)
        self.key, self.secret = key, secret
        self.base_url = authorized_base(self.root, key)
        self.token = ""
        self.expires_at = 0.0
        self.interval, _ = effective_request_policy(interval, DEFAULT_DAILY_REQUEST_BUDGET)
        self.account_policy = account_policy
        if account_policy is not None:
            self.interval = 0.0
        self.last_completed_at = 0.0
        self.adaptive_rate = adaptive_rate
        self.throttle_events = 0
        self.cooldown_until = 0.0
        account_id = hashlib.sha256(key.encode()).hexdigest()[:20]
        self.account_limit_path = root / "state" / "dce_account_limits" / f"{account_id}.json"
        if self.account_limit_path.exists():
            state = json.loads(self.account_limit_path.read_text(encoding="utf-8"))
            remaining = (datetime.fromisoformat(state["pausedUntil"]) - datetime.now(ZoneInfo("Asia/Shanghai"))).total_seconds()
            self.cooldown_until = time.monotonic() + max(0, remaining)
        self.http = GovernedRequestsSession(root, min_interval_seconds=MIN_REQUEST_INTERVAL_SECONDS)
        if account_policy is not None:
            self.http.limiter = DceAccountRateLimiter(root, key, window_seconds=account_policy[0],
                                                      requests_per_window=account_policy[1])
            self.account_policy = (self.http.limiter.window_seconds, self.http.limiter.requests_per_window)

    def cooldown_remaining(self) -> float:
        if self.account_limit_path.exists():
            state = json.loads(self.account_limit_path.read_text(encoding="utf-8"))
            remaining = (datetime.fromisoformat(state["pausedUntil"]) - datetime.now(ZoneInfo("Asia/Shanghai"))).total_seconds()
            self.cooldown_until = max(self.cooldown_until, time.monotonic() + max(0, remaining))
        return max(0, self.cooldown_until - time.monotonic())

    def _post(self, path: str, body: dict, *, auth: bool = False) -> tuple[dict, bytes]:
        if (self.base_url not in {BASE, HTTP_BASE}
                or self.base_url != authorized_base(self.root, self.key)
                or re.fullmatch(r"/dceapi/(?:cms/auth/accessToken|forward/publicweb/"
                                r"(?:dailystat|tradepara)/[A-Za-z][A-Za-z0-9]*)", path) is None):
            raise RuntimeError("DCE 凭据请求仅限已授权的官方主机和接口")
        headers = {"apikey": self.key, "Content-Type": "application/json"}
        if not auth:
            headers.update(Authorization=f"Bearer {self.token}", tradeType="1", lang="zh")
        # Do not retry or follow redirects with credentials. The response alone
        # is retained only after its structure passes the requested endpoint gate.
        remaining = (0 if self.account_policy is not None else
                     max(self.cooldown_remaining(), self.interval - (time.monotonic() - self.last_completed_at)))
        if remaining > 0:
            time.sleep(remaining)
        self.http.limiter.acquire("www.dce.com.cn")
        if self.account_policy is not None:
            self.account_policy = (self.http.limiter.window_seconds, self.http.limiter.requests_per_window)
        cooldown = 0
        host_cooldown = 0
        try:
            response = self.http.session.post(self.base_url + path, headers=headers, json=body,
                                              timeout=20, allow_redirects=False)
            try:
                if response.status_code == 429:
                    cooldown = 120
                    cooldown = max(120, float(response.headers.get("Retry-After") or 0))
                    host_cooldown = cooldown
                elif response.json().get("code") == 501:
                    cooldown = 120
                    cooldown = max(120, float(response.headers.get("Retry-After") or 0))
            except (ValueError, AttributeError):
                if response.status_code == 429:
                    host_cooldown = cooldown
        finally:
            if cooldown and self.account_policy is not None:
                self.http.limiter.backoff()
                self.account_policy = (self.http.limiter.window_seconds, self.http.limiter.requests_per_window)
            self.http.limiter.complete("www.dce.com.cn", retry_after_seconds=(
                cooldown if self.account_policy is not None else host_cooldown))
            self.last_completed_at = time.monotonic()
            if cooldown:
                self.cooldown_until = self.last_completed_at + cooldown
                if not host_cooldown and self.account_policy is None:
                    paused_until = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(seconds=cooldown)
                    save_json(self.account_limit_path, {"pausedUntil": paused_until.isoformat(), "apiCode": 501})
        if response.status_code != 200:
            raise ApiFailure(response.status_code)
        try:
            payload = response.json()
        except ValueError as error:
            raise ApiFailure(None) from error
        if not isinstance(payload, dict):
            raise ApiFailure(None)
        return payload, bytes(response.content)

    def authenticate(self) -> None:
        payload, _ = self._post("/dceapi/cms/auth/accessToken", {"secret": self.secret}, auth=True)
        data = payload.get("data")
        if payload.get("success") is not True or payload.get("code") != 200 or not isinstance(data, dict):
            raise ApiFailure(payload.get("code"))
        token = data.get("token")
        ttl = float(data.get("expiresIn") or 0)
        if not isinstance(token, str) or not token or ttl <= 0:
            raise ApiFailure(None)
        self.token = token
        self.expires_at = time.monotonic() + ttl

    def request(self, endpoint: str, body: dict, *, namespace: str = "dailystat") -> tuple[dict, bytes]:
        if namespace not in {"dailystat", "tradepara"}:
            raise ValueError("Unsupported DCE API namespace")
        if not self.token or time.monotonic() >= self.expires_at - 60:
            self.authenticate()
        path = "/dceapi/forward/publicweb/" + namespace + "/" + endpoint
        for attempt in range(2):
            try:
                payload, raw = self._post(path, body)
            except ApiFailure as error:
                if error.code != 429 or not self._slow_after_throttle(attempt):
                    raise
                continue
            if payload.get("code") == 501 and self._slow_after_throttle(attempt):
                continue
            break
        if payload.get("code") == 402:
            self.authenticate()
            payload, raw = self._post(path, body)
        if payload.get("success") is not True or payload.get("code") != 200:
            raise ApiFailure(payload.get("code"))
        return payload, raw

    def _slow_after_throttle(self, attempt: int) -> bool:
        # One source-directed downgrade, never alternate accounts or immediate retries.
        if not self.adaptive_rate or attempt != 0 or self.interval >= 2:
            return False
        self.interval = 2
        self.throttle_events += 1
        remaining = self.cooldown_until - time.monotonic()
        print(json.dumps({"event": "DCE_RATE_DOWNGRADE", "minIntervalSeconds": self.interval,
                          "cooldownSeconds": max(0, round(remaining, 2)), "updatedAt": now()}), flush=True)
        if remaining > 0:
            time.sleep(remaining)
        return True

    def close(self) -> None:
        self.token = self.key = self.secret = ""
        self.http.close()


def contract_manifest(payload: dict) -> list[str]:
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise ValueError("Daily response lacks contract universe")
    contracts = {str(row.get("contractId") or "").strip() for row in data if isinstance(row, dict)}
    selected = set()
    for code in contracts:
        match = CONTRACT.fullmatch(code)
        if match and (not match[3] or match[1].lower() in {"l", "v", "pp"}):
            selected.add(match[1].lower() + match[2] + ("F" if match[3] else ""))
    selected = sorted(selected)
    if not selected:
        raise ValueError("Daily response has no ordinary dated contracts")
    return selected


def ranking_variety(contract: str) -> str:
    match = CONTRACT.fullmatch(contract)
    if not match:
        raise ValueError("Invalid dated futures contract")
    return match[1].lower() + ("f" if match[3] else "")


def parse_ranking(payload: dict, day: str, contract: str, digest: str, retrieved: str,
                  *, source_url: str = BASE + "/dceapi/forward/publicweb/dailystat/memberDealPosi"
                  ) -> tuple[list[dict], list[dict]]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Ranking response lacks data object")
    product = CONTRACT.fullmatch(contract)
    if product is None:
        raise ValueError("Invalid ordinary dated contract")
    returned = str(data.get("contractId") or "").strip()
    # This field can be the published header "期货公司会员", not an ID.
    if CONTRACT.fullmatch(returned) and returned.casefold() != contract.casefold():
        raise ValueError("DCE ranking response differs from requested contract")
    product_code = product[1].upper() + ("-F" if product[3] else "")
    canonical = product_code + product[2]
    shared = {"trading_day": day, "exchange": "DCE", "contract_code": canonical,
              "product_code": product_code, "source": SOURCE,
              "publisher": "DCE", "source_url": source_url,
              "adapter": ADAPTER, "retrieved_at": retrieved, "collected_at": retrieved,
              "payload_sha256": digest, "quality_status": "PASS", "legacy_adapter": None}
    positions, trades = [], []
    for field, name_field, value_field, change_field, side in (
        ("qtyFutureList", "qtyAbbr", "todayQty", "qtySub", None),
        ("buyFutureList", "buyAbbr", "todayBuyQty", "buySub", "LONG"),
        ("sellFutureList", "sellAbbr", "todaySellQty", "sellSub", "SHORT"),
    ):
        rows = data.get(field)
        if not isinstance(rows, list):
            raise ValueError(f"Missing ranking direction {field}")
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Invalid rank row")
            raw_rank = str(row.get("rank") or "").strip()
            if raw_rank in {"", "合计", "总计", "小计"}:
                continue
            if not raw_rank.isdigit() or int(raw_rank) <= 0:
                raise ValueError("Invalid published rank")
            rank = int(raw_rank)
            name = _member_name(row.get(name_field))
            if name in {"合计", "总计", "小计"}:
                continue
            value = float(row[value_field])
            change = float(row[change_field]) if row.get(change_field) not in (None, "") else None
            if not name or not math.isfinite(value) or value < 0 or (change is not None and not math.isfinite(change)):
                raise ValueError("Invalid published quantity")
            if rank in seen:
                raise ValueError("Duplicate direction rank")
            seen.add(rank)
            item = {**shared, "rank": rank, "member_name": name, "member_key": _member_key(name)}
            if side:
                positions.append({**item, "side": side, "position": value, "position_change": change})
            else:
                trades.append({**item, "volume": value, "volume_change": change})
    return positions, trades


def backfill(root: Path, client: DceClient, start: str, end: str, *, max_days: int | None = None,
             max_contracts: int | None = None) -> dict:
    if start > end or (max_days is not None and max_days < 1) or (max_contracts is not None and max_contracts < 1):
        raise ValueError("Invalid date range or max_days")
    calendar = load_futures_trading_calendar(root)
    if calendar is None:
        raise ValueError("Missing verified calendar")
    days = calendar.between(start, end)
    state_dir = root / "state" / "dce_authorized_ranks"
    raw_dir = root / "bronze" / "dce_authorized_ranks"
    store = MarketStore(root)
    report = {"status": "RUNNING", "startDay": start, "endDay": end, "calendarDays": len(days),
              "completedDays": 0, "processedContracts": 0, "positionRows": 0, "tradeRows": 0,
              "workerCount": getattr(client, "worker_count", 1), "updatedAt": now()}
    report_path = state_dir / "progress.json"
    processed_days = 0
    try:
        store.register_default_datasets()
        for day in days:
            state_path = state_dir / f"{day}.json"
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            if state.get("status") == "PASS" and state.get("adapter") == ADAPTER:
                report["completedDays"] += 1
                continue
            if max_days is not None and processed_days >= max_days:
                report["status"] = "BATCH_LIMIT"
                break
            processed_days += 1
            compact = day.replace("-", "")
            if not state.get("contracts") or state.get("adapter") != ADAPTER:
                payload, raw = client.request("dayQuotes", {"varietyId": "all", "tradeDate": compact,
                    "tradeType": "1", "lang": "zh", "statisticsType": 0})
                contracts = contract_manifest(payload)
                digest = hashlib.sha256(raw).hexdigest()
                save_json(raw_dir / day / "universe.json", {"tradingDay": day, "sourceUrl": source_base(client) +
                    "/dceapi/forward/publicweb/dailystat/dayQuotes", "retrievedAt": now(),
                    "payloadSha256": digest, "response": payload})
                completed = {code: entry for code, entry in state.get("completed", {}).items() if code in contracts}
                state = {"schema": "dce-authorized-rank-day/v2", "adapter": ADAPTER,
                         "tradingDay": day, "contracts": contracts,
                         "completed": completed, "status": "PARTIAL", "universeSha256": digest}
                save_json(state_path, state)
            pending = [contract for contract in state["contracts"] if contract not in state["completed"]]
            report.update(currentDay=day, dayTotalContracts=len(state["contracts"]),
                          dayCompletedContracts=len(state["completed"]),
                          dayPendingContracts=len(pending))
            if max_contracts is not None:
                remaining = max_contracts - report["processedContracts"]
                if remaining <= 0:
                    report["status"] = "BATCH_LIMIT"
                    break
                # Limit the worker pool's input before it prefetches. A
                # response from the one-time API must not be fetched beyond
                # this run's commit budget and then repeated on the next run.
                request_batch = pending[:remaining]
            else:
                request_batch = pending

            def update_accounts(status):
                report.update(accountStatus=status, currentDay=day, updatedAt=now())
                save_json(report_path, report)

            def sequential_responses():
                for contract in request_batch:
                    if max_contracts is not None and report["processedContracts"] >= max_contracts:
                        return
                    report.update(currentDay=day, currentContract=contract, updatedAt=now())
                    save_json(report_path, report)
                    product = ranking_variety(contract)
                    yield contract, *client.request("memberDealPosi", {
                        "varietyId": product, "tradeDate": compact, "contractId": contract, "tradeType": "1"})

            parallel_responses = getattr(client, "iter_rank_requests", None)
            responses = (parallel_responses(day, request_batch, on_status=update_accounts)
                         if parallel_responses is not None else sequential_responses())
            for contract, payload, raw in responses:
                if max_contracts is not None and report["processedContracts"] >= max_contracts:
                    report["status"] = "BATCH_LIMIT"
                    return report
                report.update(currentDay=day, currentContract=contract, updatedAt=now(),
                              minIntervalSeconds=getattr(client, "interval", None),
                              throttleEvents=getattr(client, "throttle_events", 0))
                save_json(report_path, report)
                digest, retrieved = hashlib.sha256(raw).hexdigest(), now()
                ranking_url = source_base(client) + "/dceapi/forward/publicweb/dailystat/memberDealPosi"
                positions, trades = parse_ranking(payload, day, contract, digest, retrieved,
                                                  source_url=ranking_url)
                save_json(raw_dir / day / f"{contract}.json", {"tradingDay": day, "contractCode": contract,
                    "sourceUrl": ranking_url,
                    "retrievedAt": retrieved, "payloadSha256": digest, "response": payload})
                # Checkpoint follows both idempotent fact writes; a crash in
                # between causes an upsert retry, never a missing second table.
                store.upsert_futures_member_position_ranks(positions)
                store.upsert_futures_member_trade_ranks(trades)
                state["completed"][contract] = {"status": "PASS" if positions or trades else "NO_PUBLISHED_ROWS",
                    "positionRows": len(positions), "tradeRows": len(trades), "payloadSha256": digest}
                save_json(state_path, state)
                report["processedContracts"] += 1
                report["positionRows"] += len(positions)
                report["tradeRows"] += len(trades)
                report.update(dayCompletedContracts=len(state["completed"]),
                              dayPendingContracts=len(state["contracts"]) - len(state["completed"]))
                count = sum(v["positionRows"] for v in state["completed"].values())
                complete = len(state["completed"]) == len(state["contracts"])
                store.upsert_futures_member_position_coverage([{"trading_day": day, "exchange": "DCE",
                    "status": "PASS" if complete and count else "PARTIAL", "contract_count": len(state["completed"]),
                    "record_count": count, "source": SOURCE, "error": None if complete else "逐合约回补尚未完成",
                    "collected_at": retrieved, "publisher": "DCE", "source_url": ranking_url, "adapter": ADAPTER,
                    "retrieved_at": retrieved, "payload_sha256": digest,
                    "quality_status": "PASS" if complete and count else "PARTIAL", "legacy_adapter": None}])
                print(json.dumps({"day": day, "contracts": f"{len(state['completed'])}/{len(state['contracts'])}",
                                  "positions": len(positions), "trades": len(trades)}, ensure_ascii=False), flush=True)
            if len(state["completed"]) != len(state["contracts"]):
                report["status"] = "BATCH_LIMIT"
                return report
            state["status"] = "PASS" if any(item["positionRows"] or item["tradeRows"] for item in state["completed"].values()) else "NO_PUBLISHED_ROWS"
            save_json(state_path, state)
            report["completedDays"] += 1
        else:
            report["status"] = "PASS"
    except RequestBudgetExhausted as error:
        report["status"] = "BUDGET_PAUSED"
        report["pauseReason"] = str(error)
    except Exception as error:
        report["status"] = "TLS_BLOCKED" if isinstance(error, SSLError) else "FAILED"
        report["errorType"] = "TLS_VERIFICATION_FAILED" if isinstance(error, SSLError) else type(error).__name__
        if isinstance(error, OSError) and error.filename:
            report["errorFile"] = Path(error.filename).name
        if isinstance(error, ApiFailure):
            report["apiCode"] = error.code
    finally:
        report["updatedAt"] = now()
        save_json(report_path, report)
        store.close()
    return report
