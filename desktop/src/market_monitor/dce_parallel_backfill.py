"""Two bounded account queues, one authoritative ranking writer."""
from __future__ import annotations

from collections import deque
from concurrent.futures import CancelledError, FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import hashlib
import json
from pathlib import Path
import threading
import time

from requests.exceptions import ConnectionError as RequestsConnectionError, SSLError, Timeout as RequestsTimeout

from .dce_authorized_backfill import ApiFailure, CONTRACT, DceClient, now, ranking_variety, save_json, source_base

NETWORK_RETRY_DELAYS_SECONDS = (1.0, 2.0)
MAX_RATE_LIMIT_RETRIES = 1


class DceClientPool:
    """Overlap account pacing and writer work while keeping global request leases."""

    def __init__(self, root: Path, clients: list[DceClient], *, lanes_per_account: int = 1) -> None:
        if lanes_per_account < 1 or len(clients) % lanes_per_account or len(clients) // lanes_per_account not in (1, 2):
            raise ValueError("One or two account groups are required")
        self.root, self.clients = root, clients
        self.lanes_per_account = lanes_per_account
        self.account_count = len(clients) // lanes_per_account
        self.workers = [ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"dce-{i}") for i in range(len(clients))]
        self.pending: deque[str] = deque()
        self.futures: dict[str, tuple[Future, int]] = {}
        self.day = ""
        self.last_request_base_url = source_base(clients[0])
        self.worker_count = len(clients)
        self.stop = threading.Event()
        self.status_lock = threading.Lock()
        self.lane_status = [{"status": "READY", "successfulResponses": 0, "throttleEvents": 0} for _ in clients]
        for client in clients:
            limiter = getattr(getattr(client, "http", None), "limiter", None)
            if hasattr(limiter, "stop_event"):
                limiter.stop_event = self.stop

    def _label(self, index: int) -> str:
        return "AB"[index // self.lanes_per_account]

    @property
    def interval(self) -> float:
        return min(client.interval for client in self.clients)

    @property
    def throttle_events(self) -> int:
        with self.status_lock:
            return sum(status["throttleEvents"] for status in self.lane_status)

    @property
    def account_status(self) -> dict:
        with self.status_lock:
            statuses = [dict(status) for status in self.lane_status]
        result = {}
        for account in range(self.account_count):
            indices = range(account * self.lanes_per_account, (account + 1) * self.lanes_per_account)
            remaining = max(self.clients[i].cooldown_remaining() for i in indices)
            policy = getattr(self.clients[account * self.lanes_per_account], "account_policy", None)
            result["AB"[account]] = {
                "status": "COOLDOWN_WAIT" if remaining else "RUNNING",
                "successfulResponses": sum(statuses[i]["successfulResponses"] for i in indices),
                "throttleEvents": sum(statuses[i]["throttleEvents"] for i in indices),
                "cooldownRemainingSeconds": round(remaining, 1), "concurrency": self.lanes_per_account,
                "minIntervalSeconds": self.clients[account * self.lanes_per_account].interval,
                "windowSeconds": policy[0] if policy else None,
                "requestsPerWindow": policy[1] if policy else None,
                "dailyLimit": None if policy else 30000,
            }
        return result

    def prepare_rank_requests(self, day: str, contracts: list[str]) -> None:
        if self.futures:
            raise RuntimeError("Previous ranking futures have not been consumed")
        self.day = day
        self.pending = deque(contracts)
        for index in range(len(self.clients)):
            self._submit_next(index)

    def _submit_next(self, index: int) -> None:
        if self.pending and not self.stop.is_set():
            contract = self.pending.popleft()
            self.futures[contract] = (self.workers[index].submit(self._fetch_rank, index, self.day, contract), index)

    def _request_account(self, index: int, endpoint: str, body: dict) -> tuple[dict, bytes]:
        client = self.clients[index]
        network_failures = 0
        rate_failures = 0
        while not self.stop.is_set():
            remaining = client.cooldown_remaining()
            with self.status_lock:
                self.lane_status[index]["status"] = "COOLDOWN_WAIT" if remaining else "RUNNING"
            if remaining and self.stop.wait(remaining):
                raise CancelledError()
            with self.status_lock:
                self.lane_status[index]["status"] = "RUNNING"
            try:
                result = client.request(endpoint, body)
            except SSLError:
                with self.status_lock:
                    self.lane_status[index]["status"] = "TLS_BLOCKED"
                raise
            except (RequestsConnectionError, RequestsTimeout):
                if network_failures >= len(NETWORK_RETRY_DELAYS_SECONDS):
                    raise
                delay = NETWORK_RETRY_DELAYS_SECONDS[network_failures]
                network_failures += 1
                with self.status_lock:
                    self.lane_status[index]["status"] = "NETWORK_RETRY_WAIT"
                print(json.dumps({"event": "DCE_ACCOUNT_NETWORK_RETRY", "accountLabel": self._label(index),
                                  "attempt": network_failures + 1, "waitSeconds": delay, "updatedAt": now()}), flush=True)
                if self.stop.wait(delay):
                    raise CancelledError() from None
                continue
            except ApiFailure as error:
                if error.code != 501:
                    raise
                rate_failures += 1
                with self.status_lock:
                    self.lane_status[index]["status"] = "COOLDOWN_WAIT"
                    self.lane_status[index]["throttleEvents"] += 1
                print(json.dumps({"event": "DCE_ACCOUNT_COOLDOWN", "accountLabel": self._label(index),
                                  "cooldownSeconds": round(client.cooldown_remaining(), 1), "updatedAt": now()}), flush=True)
                if rate_failures > MAX_RATE_LIMIT_RETRIES:
                    raise
                if endpoint != "memberDealPosi":
                    raise
                continue
            with self.status_lock:
                self.lane_status[index]["successfulResponses"] += 1
            return result
        raise CancelledError()

    def _fetch_rank(self, index: int, day: str, contract: str) -> tuple[dict, bytes]:
        match = CONTRACT.fullmatch(contract)
        if match is None:
            raise ValueError("Invalid ordinary dated contract")
        payload, raw = self._request_account(index, "memberDealPosi", {
            "varietyId": ranking_variety(contract), "tradeDate": day.replace("-", ""),
            "contractId": contract, "tradeType": "1",
        })
        # Retain a successful prefetched response even if another account fails.
        # Each contract has one owner; only the main writer promotes its checkpoint.
        save_json(self.root / "bronze" / "dce_parallel_prefetch" / day / f"{contract}.json", {
            "tradingDay": day, "contractCode": contract, "accountLabel": self._label(index),
            "sourceUrl": source_base(self.clients[index]) + "/dceapi/forward/publicweb/dailystat/memberDealPosi",
            "retrievedAt": now(), "payloadSha256": hashlib.sha256(raw).hexdigest(),
            "qualityStatus": "PENDING_VALIDATION", "response": payload,
        })
        return payload, raw

    def request(self, endpoint: str, body: dict) -> tuple[dict, bytes]:
        if endpoint != "memberDealPosi":
            rate_failures = 0
            while not self.stop.is_set():
                index = min(range(len(self.clients)), key=lambda i: self.clients[i].cooldown_remaining())
                try:
                    result = self._request_account(index, endpoint, body)
                    self.last_request_base_url = source_base(self.clients[index])
                    return result
                except ApiFailure as error:
                    if error.code != 501:
                        raise
                    rate_failures += 1
                    if rate_failures > self.account_count * MAX_RATE_LIMIT_RETRIES:
                        raise
            raise CancelledError()
        contract = body["contractId"]
        future, index = self.futures[contract]
        result = future.result()
        self.last_request_base_url = source_base(self.clients[index])
        del self.futures[contract]
        self._submit_next(index)
        return result

    def iter_rank_requests(self, day: str, contracts: list[str], on_status=None):
        self.prepare_rank_requests(day, contracts)
        last_status = 0.0
        while self.futures:
            done, _ = wait([future for future, _ in self.futures.values()], timeout=1, return_when=FIRST_COMPLETED)
            if on_status is not None and time.monotonic() - last_status >= 1:
                on_status(self.account_status)
                last_status = time.monotonic()
            for future in done:
                contract, index = next((contract, index) for contract, (item, index) in self.futures.items()
                                       if item is future)
                result = future.result()
                self.last_request_base_url = source_base(self.clients[index])
                del self.futures[contract]
                self._submit_next(index)
                yield contract, *result

    def close(self) -> None:
        self.stop.set()
        for future, _ in self.futures.values():
            future.cancel()
        for worker in self.workers:
            worker.shutdown(wait=True, cancel_futures=True)
        for client in self.clients:
            client.close()
