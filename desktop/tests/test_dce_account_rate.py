from datetime import datetime, timedelta
import json
from zoneinfo import ZoneInfo

from market_monitor.dce_account_rate import DceAccountRateLimiter
from market_monitor.dce_authorized_backfill import DceClient


class Clock:
    def __init__(self):
        self.current = datetime(2026, 10, 2, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.waits = []

    def now(self):
        return self.current

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.current += timedelta(seconds=seconds)


def test_three_account_leases_overlap_but_fourth_waits(tmp_path):
    clock = Clock()
    leases = [DceAccountRateLimiter(tmp_path, "synthetic-account-A", now=clock.now, sleep=clock.sleep)
              for _ in range(4)]
    for lease in leases[:3]:
        lease.acquire("www.dce.com.cn")
    assert all(lease.active for lease in leases[:3])
    leases[3].acquire("www.dce.com.cn")
    assert clock.waits == [1]
    assert json.loads(leases[0].path.read_text())["dayCount"] == 4
    for lease in leases:
        lease.complete("www.dce.com.cn")


def test_account_cooldown_does_not_pause_other_account(tmp_path):
    clock = Clock()
    first = DceAccountRateLimiter(tmp_path, "synthetic-account-A", now=clock.now, sleep=clock.sleep)
    second = DceAccountRateLimiter(tmp_path, "synthetic-account-B", now=clock.now, sleep=clock.sleep)
    first.acquire("www.dce.com.cn")
    first.complete("www.dce.com.cn", retry_after_seconds=120)
    second.acquire("www.dce.com.cn")
    second.complete("www.dce.com.cn")
    assert clock.waits == []
    first.acquire("www.dce.com.cn")
    assert clock.waits == [120]
    first.complete("www.dce.com.cn")


def test_account_policy_has_no_daily_limit_or_shared_budget_side_effect(tmp_path):
    clock = Clock()
    limiter = DceAccountRateLimiter(tmp_path, "synthetic-account-A", now=clock.now, sleep=clock.sleep)
    limiter.acquire("www.dce.com.cn")
    limiter.complete("www.dce.com.cn")
    data = json.loads(limiter.path.read_text())
    data["dayCount"] = 30000
    limiter.path.write_text(json.dumps(data))
    limiter.acquire("www.dce.com.cn")
    limiter.complete("www.dce.com.cn")
    data = json.loads(limiter.path.read_text())
    assert data["dayCount"] == 30001
    assert data["dailyLimit"] is None
    assert not (tmp_path / "state" / "official_futures_request_budget.json").exists()


def test_business_501_is_persisted_only_for_its_account(tmp_path, monkeypatch):
    first = DceClient(tmp_path, "synthetic-account-A", "synthetic-secret-A", account_policy=(1, 3))
    second = DceClient(tmp_path, "synthetic-account-B", "synthetic-secret-B", account_policy=(1, 3))

    class Response:
        status_code = 200
        headers = {}
        content = b'{"success":false,"code":501}'

        def json(self):
            return {"success": False, "code": 501}

    monkeypatch.setattr(first.http.session, "post", lambda *a, **kw: Response())
    try:
        assert first._post("/dceapi/cms/auth/accessToken", {}, auth=True)[0]["code"] == 501
        assert first.cooldown_remaining() > 119
        assert first.http.limiter.window_seconds == 2
        assert first.http.limiter.requests_per_window == 1
        resumed = DceClient(tmp_path, "synthetic-account-A", "synthetic-secret-A", account_policy=(1, 3))
        assert resumed.account_policy == (2, 1)
        resumed.close()
        assert second.cooldown_remaining() == 0
        assert second.http.limiter.window_seconds == 1
        assert second.http.limiter.requests_per_window == 3
        second.http.limiter.acquire("www.dce.com.cn")
        second.http.limiter.complete("www.dce.com.cn")
        assert not (tmp_path / "state" / "official_futures_request_budget.json").exists()
    finally:
        first.close()
        second.close()
