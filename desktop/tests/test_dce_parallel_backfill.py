import json
import threading
import time

import pytest
from requests.exceptions import ConnectionError as RequestsConnectionError, Timeout as RequestsTimeout

from market_monitor import dce_parallel_backfill
from market_monitor.dce_authorized_backfill import ApiFailure, DceClient, backfill
from market_monitor.dce_parallel_backfill import DceClientPool
from market_monitor.futures_calendar import CALENDAR_PROVIDER, CALENDAR_SCHEMA
from market_monitor.storage import MarketStore


class FakeClient:
    interval = 2
    throttle_events = 0

    def __init__(self, barrier=None, fail=False):
        self.calls = []
        self.barrier, self.fail = barrier, fail
        self.closed = False
        self.cooldown_until = 0
        self.network_errors = []

    def cooldown_remaining(self):
        return max(0, self.cooldown_until - time.monotonic())

    def request(self, endpoint, body):
        if endpoint == "dayQuotes":
            payload = {"data": [{"contractId": c} for c in ("a2505", "b2505", "c2505", "m2505")]}
        else:
            self.calls.append(body["contractId"])
            if self.network_errors:
                raise self.network_errors.pop(0)
            if self.barrier is not None and len(self.calls) == 1:
                self.barrier.wait(timeout=5)
            if self.fail:
                self.cooldown_until = time.monotonic() + 60
                raise ApiFailure(501)
            payload = {"data": {"qtyFutureList": [], "sellFutureList": [], "buyFutureList": [
                {"rank": "1", "buyAbbr": "合成会员", "todayBuyQty": 10, "buySub": None}]}}
        return payload, json.dumps(payload).encode()

    def close(self):
        self.closed = True


@pytest.mark.parametrize("max_contracts", [None, 1])
def test_two_queues_keep_one_database_writer(tmp_path, monkeypatch, max_contracts):
    calendar = tmp_path / "state" / "cn_futures_trading_calendar.json"
    calendar.parent.mkdir()
    calendar.write_text(json.dumps({"schema": CALENDAR_SCHEMA, "provider": CALENDAR_PROVIDER,
        "market": "CN_FUTURE", "retrievedAt": "2026-10-02T00:00:00+08:00",
        "tradingDays": ["2025-01-02"]}), encoding="utf-8")
    writer_threads = []
    method = MarketStore.upsert_futures_member_position_ranks

    def record_writer(store, rows):
        writer_threads.append(threading.get_ident())
        return method(store, rows)

    monkeypatch.setattr(MarketStore, "upsert_futures_member_position_ranks", record_writer)
    barrier = threading.Barrier(1 if max_contracts == 1 else 2)
    clients = [FakeClient(barrier), FakeClient(barrier)]
    pool = DceClientPool(tmp_path, clients)
    try:
        result = backfill(tmp_path, pool, "2025-01-01", "2025-01-02", max_contracts=max_contracts)
        assert result["status"] == ("PASS" if max_contracts is None else "BATCH_LIMIT")
        assert result["workerCount"] == 2
        assert result["processedContracts"] == (4 if max_contracts is None else 1)
        if max_contracts is None:
            assert sorted(clients[0].calls + clients[1].calls) == ["a2505", "b2505", "c2505", "m2505"]
        else:
            state = json.loads((tmp_path / "state" / "dce_authorized_ranks" / "2025-01-02.json").read_text())
            assert state["status"] == "PARTIAL"
            assert len(state["completed"]) == 1
            assert len(clients[0].calls + clients[1].calls) == 1
        assert set(writer_threads) == {threading.get_ident()}
        assert len(list((tmp_path / "bronze" / "dce_parallel_prefetch").rglob("*.json"))) == (
            4 if max_contracts is None else 1
        )
    finally:
        pool.close()
    assert all(client.closed for client in clients)


def test_parallel_ranking_keeps_each_accounts_actual_transport_provenance(tmp_path):
    calendar = tmp_path / "state" / "cn_futures_trading_calendar.json"
    calendar.parent.mkdir()
    calendar.write_text(json.dumps({"schema": CALENDAR_SCHEMA, "provider": CALENDAR_PROVIDER,
        "market": "CN_FUTURE", "retrievedAt": "2026-10-02T00:00:00+08:00",
        "tradingDays": ["2025-01-02"]}), encoding="utf-8")
    clients = [FakeClient(), FakeClient()]
    clients[0].base_url = "http://www.dce.com.cn"
    clients[1].base_url = "https://www.dce.com.cn"
    pool = DceClientPool(tmp_path, clients)
    try:
        assert backfill(tmp_path, pool, "2025-01-02", "2025-01-02")["status"] == "PASS"
    finally:
        pool.close()
    for contract, scheme in (("a2505", "http"), ("b2505", "https")):
        ranking = json.loads((tmp_path / "bronze" / "dce_authorized_ranks" / "2025-01-02" /
                              f"{contract}.json").read_text(encoding="utf-8"))
        prefetched = json.loads((tmp_path / "bronze" / "dce_parallel_prefetch" / "2025-01-02" /
                                 f"{contract}.json").read_text(encoding="utf-8"))
        assert ranking["sourceUrl"].startswith(f"{scheme}://www.dce.com.cn/")
        assert prefetched["sourceUrl"] == ranking["sourceUrl"]
    store = MarketStore(tmp_path)
    try:
        rows = store.connection.execute("SELECT contract_code,source_url FROM futures_member_position_ranks "
                                        "WHERE exchange='DCE' AND contract_code IN ('A2505','B2505')").fetchall()
    finally:
        store.close()
    assert {contract: url.split(":", 1)[0] for contract, url in rows} == {"A2505": "http", "B2505": "https"}


def test_account_501_does_not_block_healthy_account(tmp_path):
    clients = [FakeClient(fail=True), FakeClient()]
    pool = DceClientPool(tmp_path, clients)
    try:
        iterator = pool.iter_rank_requests("2025-01-02", ["a2505", "b2505", "c2505", "m2505"])
        results = [next(iterator)[0] for _ in range(3)]
        assert results == ["b2505", "c2505", "m2505"]
        assert len(pool.futures) <= 2
        assert clients[0].calls == ["a2505"]
        assert pool.account_status["A"]["status"] == "COOLDOWN_WAIT"
        assert pool.account_status["B"]["successfulResponses"] == 3
    finally:
        pool.close()
    assert all(client.closed for client in clients)
    assert not (tmp_path / "state" / "dce_authorized_ranks").exists()


def test_persistent_501_stops_after_one_account_cooldown(tmp_path):
    client = FakeClient(fail=True)
    client.cooldown_remaining = lambda: 0
    pool = DceClientPool(tmp_path, [client])
    try:
        with pytest.raises(ApiFailure) as error:
            list(pool.iter_rank_requests("2025-01-02", ["a2505"]))
        assert error.value.code == 501
        assert client.calls == ["a2505", "a2505"]
        assert pool.account_status["A"]["throttleEvents"] == 2
    finally:
        pool.close()


def test_three_lanes_per_account_keep_healthy_group_running(tmp_path):
    clients = [FakeClient(fail=index < 3) for index in range(6)]
    pool = DceClientPool(tmp_path, clients, lanes_per_account=3)
    contracts = ["a2505", "b2505", "c2505", "m2505", "y2505", "p2505", "j2505", "i2505", "l2505"]
    try:
        iterator = pool.iter_rank_requests("2025-01-02", contracts)
        results = [next(iterator)[0] for _ in range(6)]
        assert set(results) == set(contracts[3:])
        assert len(pool.futures) <= 6
        assert pool.account_status["A"]["status"] == "COOLDOWN_WAIT"
        assert pool.account_status["B"]["successfulResponses"] == 6
        assert pool.account_status["A"]["concurrency"] == 3
        assert pool.worker_count == 6
    finally:
        pool.close()


def test_transient_network_retry_stays_on_original_account(tmp_path, monkeypatch):
    monkeypatch.setattr(dce_parallel_backfill, "NETWORK_RETRY_DELAYS_SECONDS", (0.0, 0.0))
    clients = [FakeClient(), FakeClient()]
    clients[0].network_errors = [RequestsConnectionError("synthetic disconnect"),
                                 RequestsTimeout("synthetic timeout")]
    pool = DceClientPool(tmp_path, clients)
    try:
        results = list(pool.iter_rank_requests("2025-01-02", ["a2505", "b2505"]))
        assert {contract for contract, _, _ in results} == {"a2505", "b2505"}
        assert clients[0].calls == ["a2505"] * 3
        assert clients[1].calls == ["b2505"]
        assert pool.account_status["A"]["successfulResponses"] == 1
        assert pool.account_status["B"]["successfulResponses"] == 1
    finally:
        pool.close()


def test_transient_network_retry_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(dce_parallel_backfill, "NETWORK_RETRY_DELAYS_SECONDS", (0.0, 0.0))
    client = FakeClient()
    client.network_errors = [RequestsTimeout("synthetic timeout") for _ in range(3)]
    pool = DceClientPool(tmp_path, [client])
    try:
        with pytest.raises(RequestsTimeout, match="synthetic timeout"):
            list(pool.iter_rank_requests("2025-01-02", ["a2505"]))
        assert client.calls == ["a2505"] * 3
        assert not list((tmp_path / "bronze").rglob("*.json"))
    finally:
        pool.close()


def test_network_retry_reacquires_account_window_lease(tmp_path, monkeypatch):
    monkeypatch.setattr(dce_parallel_backfill, "NETWORK_RETRY_DELAYS_SECONDS", (0.0, 0.0))
    client = DceClient(tmp_path, "synthetic-test-key", "synthetic-test-secret", account_policy=(1.0, 3))
    client.token, client.expires_at = "synthetic-test-token", time.monotonic() + 3600
    requests_made = []

    class Response:
        status_code = 200
        headers = {}
        content = b'{"success":true,"code":200,"data":{}}'

        def json(self):
            return {"success": True, "code": 200, "data": {}}

    def post(*args, **kwargs):
        requests_made.append((args, kwargs))
        if len(requests_made) < 3:
            raise RequestsConnectionError("synthetic disconnect")
        return Response()

    monkeypatch.setattr(client.http.session, "post", post)
    pool = DceClientPool(tmp_path, [client])
    try:
        payload, _ = pool._request_account(0, "memberDealPosi", {"contractId": "a2505"})
        assert payload["code"] == 200
        assert len(requests_made) == 3
        ledger_text = client.http.limiter.path.read_text(encoding="utf-8")
        ledger = json.loads(ledger_text)
        assert ledger["dayCount"] == 3
        assert len(ledger["requestStarts"]) == 3
        assert "synthetic-test-key" not in ledger_text
        assert "synthetic-test-secret" not in ledger_text
    finally:
        pool.close()


def test_non_network_api_failure_is_not_retried(tmp_path):
    client = FakeClient()
    pool = DceClientPool(tmp_path, [client])

    def reject(endpoint, body):
        client.calls.append(body["contractId"])
        raise ApiFailure(503)

    client.request = reject
    try:
        with pytest.raises(ApiFailure) as caught:
            list(pool.iter_rank_requests("2025-01-02", ["a2505"]))
        assert caught.value.code == 503
        assert client.calls == ["a2505"]
    finally:
        pool.close()
