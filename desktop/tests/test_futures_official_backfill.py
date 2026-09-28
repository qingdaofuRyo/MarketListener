from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
import json
from pathlib import Path
from time import perf_counter
import zipfile

import pytest
import requests
from fastapi.testclient import TestClient

from market_monitor.futures_calendar import sync_futures_trading_calendar
from market_monitor.collector import run_official_member_rank_backfill
from market_monitor.futures_official_backfill import (
    HostRateLimiter,
    RequestBudgetExhausted,
    effective_request_policy,
    run_official_futures_backfill,
)
from market_monitor.futures_official_structure import run_official_product_structure_pipeline
from market_monitor.storage import MarketStore
from market_monitor.web_app import create_web_app


class _Response:
    def __init__(self, content: bytes, *, status_code: int = 200, content_type: str = "text/plain", headers=None) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = {"Content-Type": content_type, **(headers or {})}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []
        self.trust_env = True

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url))
        return self.response

    def close(self) -> None:
        pass


class _SequenceSession(_Session):
    def __init__(self, responses: list[_Response]) -> None:
        super().__init__(responses[0])
        self.responses = responses

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url))
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


class _CalendarSession(_Session):
    def __init__(self, failed_day: str | None = None, malformed_day: str | None = None) -> None:
        super().__init__(_Response(b""))
        self.failed_day = failed_day
        self.malformed_day = malformed_day

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url))
        compact = url.split("/")[-2]
        day = f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"
        if day == self.failed_day:
            return _Response(b"blocked", status_code=412, content_type="text/html")
        if day == self.malformed_day:
            return _Response(b"not a dated CZCE report", content_type="text/plain")
        return _Response(_czce_daily(day))


def _calendar(root: Path, *days: str) -> None:
    sync_futures_trading_calendar(root, fetcher=lambda: [[day] for day in days])


def _czce_daily(day: str) -> bytes:
    compact = day.replace("-", "")
    return (
        f"郑州商品交易所期货每日行情表({day})\n"
        "合约代码|昨结算|今开盘|最高价|最低价|今收盘|今结算|涨跌1|涨跌2|成交量(手)|持仓量|增减量|成交额(万元)|交割结算价\n"
        f"AP{compact[2:4]}10|6000|6010|6100|5900|6050|6040|50|40|200|300|10|1208.0|0\n"
    ).encode("utf-8")


def _cffex_monthly_archive(*days: str) -> bytes:
    result = BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        for day in days:
            archive.writestr(
                f"{day.replace('-', '')}_1.csv",
                "合约代码,收盘价,结算价,成交量,持仓量,成交额\n"
                "IF2610,4000,4010,100,200,800000\n",
            )
    return result.getvalue()


def test_official_backfill_is_calendar_bound_resumable_and_archives_raw_bytes(tmp_path: Path) -> None:
    _calendar(tmp_path, "2026-09-23", "2026-09-24")
    session = _Session(_Response(_czce_daily("2026-09-24")))

    result = run_official_futures_backfill(
        tmp_path, start_day="2026-09-23", end_day="2026-09-24",
        exchanges=("CZCE",), min_interval_seconds=0, session=session,
    )
    assert result["status"] == "PARTIAL"  # 9/23 fixture is intentionally dated 9/24.
    assert len(session.calls) == 2

    store = MarketStore(tmp_path)
    try:
        rows = store.list_futures_official_daily()
        coverage = store.list_futures_official_daily_coverage()
    finally:
        store.close()
    assert rows[0]["contract_code"] == "AP2610"
    assert {row["trade_date"]: row["status"] for row in coverage} == {
        "2026-09-23": "FAILED", "2026-09-24": "PASS",
    }
    archive = next((tmp_path / "bronze" / "official_futures_daily_czce").glob("*.json"))
    archived = json.loads(archive.read_text(encoding="utf-8"))
    assert archived["payloadSha256"]
    assert archived["contentBase64"]

    repeated = run_official_futures_backfill(
        tmp_path, start_day="2026-09-24", end_day="2026-09-24",
        exchanges=("CZCE",), min_interval_seconds=0, session=session,
    )
    assert repeated["status"] == "PASS"
    assert repeated["attempts"] == 0
    assert repeated["selectedTradingDays"] == 0
    assert len(session.calls) == 2


def test_max_days_resumes_after_completed_or_failed_source_days(tmp_path: Path) -> None:
    days = ("2026-09-22", "2026-09-23", "2026-09-24")
    _calendar(tmp_path, *days)
    session = _CalendarSession(malformed_day=days[0])
    summaries = [
        run_official_futures_backfill(
            tmp_path, start_day=days[0], end_day=days[-1], exchanges=("CZCE",),
            max_days=1, min_interval_seconds=0, session=session,
        )
        for _ in range(3)
    ]
    assert [item["outcomes"][0]["tradingDay"] for item in summaries] == list(days)
    assert [item["outcomes"][0]["status"] for item in summaries] == ["FAILED", "PASS", "PASS"]
    assert len(session.calls) == 3
    store = MarketStore(tmp_path)
    try:
        rows = store.list_futures_official_daily()
        coverage = store.list_futures_official_daily_coverage()
    finally:
        store.close()
    assert rows[0]["contract_code"] == "AP2610"
    assert {row["trade_date"]: row["status"] for row in coverage} == {
        "2026-09-22": "FAILED", "2026-09-23": "PASS", "2026-09-24": "PASS",
    }


def test_http_412_is_not_retried_on_later_batches(tmp_path: Path) -> None:
    days = ("2026-09-22", "2026-09-23", "2026-09-24")
    _calendar(tmp_path, *days)
    session = _CalendarSession(failed_day=days[0])
    summaries = [
        run_official_futures_backfill(
            tmp_path, start_day=days[0], end_day=days[-1], exchanges=("CZCE",),
            max_days=1, min_interval_seconds=0, session=session,
        )
        for _ in range(3)
    ]
    assert [item["outcomes"][0]["tradingDay"] for item in summaries] == list(days)
    assert [item["outcomes"][0]["status"] for item in summaries] == ["PAUSED_HOST", "FAILED", "FAILED"]
    assert len(session.calls) == 1


def test_official_backfill_does_not_archive_a_challenge_page(tmp_path: Path) -> None:
    _calendar(tmp_path, "2026-09-24")
    session = _Session(_Response(b"<html>verification required</html>", content_type="text/html"))
    result = run_official_futures_backfill(
        tmp_path, start_day="2026-09-24", end_day="2026-09-24",
        exchanges=("CZCE",), min_interval_seconds=0, session=session,
    )
    assert result["status"] == "PARTIAL"
    assert not list((tmp_path / "bronze").rglob("*.json"))
    store = MarketStore(tmp_path)
    try:
        assert store.list_futures_official_daily() == []
        assert store.list_futures_official_daily_coverage()[0]["status"] == "FAILED"
    finally:
        store.close()


def test_official_backfill_retries_one_server_error_then_stops(tmp_path: Path) -> None:
    _calendar(tmp_path, "2026-09-24")
    session = _SequenceSession([
        _Response(b"temporary error", status_code=503),
        _Response(_czce_daily("2026-09-24")),
    ])
    result = run_official_futures_backfill(
        tmp_path, start_day="2026-09-24", end_day="2026-09-24",
        exchanges=("CZCE",), min_interval_seconds=0, session=session,
    )
    assert result["status"] == "PASS"
    assert len(session.calls) == 2


def test_cffex_monthly_archive_is_fetched_once_for_multiple_daily_facts(tmp_path: Path) -> None:
    _calendar(tmp_path, "2026-09-23", "2026-09-24")
    raw = _cffex_monthly_archive("2026-09-23", "2026-09-24")
    session = _Session(_Response(raw, content_type="application/zip"))
    result = run_official_futures_backfill(
        tmp_path, start_day="2026-09-23", end_day="2026-09-24",
        exchanges=("CFFEX",), min_interval_seconds=0, session=session,
    )
    assert result["status"] == "PASS"
    assert len(session.calls) == 1
    archives = list((tmp_path / "bronze" / "official_futures_cffex_archives").glob("*.zip"))
    assert len(archives) == 1
    assert archives[0].read_bytes() == raw
    store = MarketStore(tmp_path)
    try:
        rows = store.list_futures_official_daily()
    finally:
        store.close()
    assert [row["trade_date"] for row in rows] == ["2026-09-23", "2026-09-24"]


def test_cffex_archive_cache_is_keyed_by_compact_calendar_month(tmp_path: Path) -> None:
    days = ("2025-10-30", "2025-11-03")
    _calendar(tmp_path, *days)
    raw = _cffex_monthly_archive(*days)
    session = _Session(_Response(raw, content_type="application/zip"))
    result = run_official_futures_backfill(
        tmp_path, start_day=days[0], end_day=days[-1],
        exchanges=("CFFEX",), min_interval_seconds=0, session=session,
    )
    assert result["status"] == "PASS"
    assert len(session.calls) == 2
    names = {path.name for path in (tmp_path / "bronze" / "official_futures_cffex_archives").glob("*.zip")}
    assert any(name.startswith("202510-") for name in names)
    assert any(name.startswith("202511-") for name in names)


def test_rate_limiter_persists_host_budget_and_spacing(tmp_path: Path) -> None:
    fixed_now = datetime.fromisoformat("2026-09-27T12:00:00+08:00")
    sleeps: list[float] = []
    limiter = HostRateLimiter(
        tmp_path, min_interval_seconds=5, daily_budget=1,
        sleep=sleeps.append, now=lambda: fixed_now,
    )
    limiter.acquire("www.czce.com.cn")
    limiter.complete("www.czce.com.cn")
    with pytest.raises(RequestBudgetExhausted, match="请求上限"):
        limiter.acquire("www.czce.com.cn")
    assert not sleeps
    saved = json.loads((tmp_path / "state" / "official_futures_request_budget.json").read_text(encoding="utf-8"))
    assert saved["hosts"]["www.czce.com.cn"]["count"] == 1


def test_rate_limiter_enforces_minimum_elapsed_time_between_requests(tmp_path: Path) -> None:
    limiter = HostRateLimiter(tmp_path, min_interval_seconds=0.2, daily_budget=5)
    limiter.acquire("www.czce.com.cn")
    limiter.complete("www.czce.com.cn")
    started = perf_counter()
    limiter.acquire("www.dce.com.cn")
    elapsed = perf_counter() - started
    limiter.complete("www.dce.com.cn")
    assert elapsed >= 0.18


def test_daily_budget_is_shared_across_hosts(tmp_path: Path) -> None:
    limiter = HostRateLimiter(tmp_path, min_interval_seconds=0, daily_budget=1)
    limiter.acquire("www.czce.com.cn")
    limiter.complete("www.czce.com.cn")
    with pytest.raises(RequestBudgetExhausted, match="总请求上限"):
        limiter.acquire("www.dce.com.cn")


def test_request_policy_enforces_five_per_second_and_30000_global_daily_cap() -> None:
    assert effective_request_policy(0, 30_000) == (0.2, 30_000)
    assert effective_request_policy(1, 1_000) == (1, 1_000)
    with pytest.raises(ValueError, match="maximum daily request budget of 30000"):
        effective_request_policy(0.2, 30_001)


def test_member_rank_backfill_advances_past_failed_and_paused_sources(tmp_path: Path, monkeypatch) -> None:
    days = ("2026-09-22", "2026-09-23", "2026-09-24")
    _calendar(tmp_path, *days)
    calls: list[tuple[str, tuple[str, ...]]] = []
    source_venues = {
        "CFFEX": ("CFFEX",), "DCE": ("DCE",), "CZCE": ("CZCE",),
        "SHFE": ("SHFE", "INE"), "GFEX": ("GFEX",),
    }

    def sync(root: Path, *, trading_day: str, exchanges: tuple[str, ...], **kwargs):
        calls.append((trading_day, exchanges))
        store = MarketStore(root)
        try:
            rows = []
            for source in exchanges:
                failed = trading_day == days[0] and source in {"DCE", "GFEX"}
                message = (
                    "HTTPError: 412 Client Error" if source == "DCE"
                    else "GFEX response is not JSON (text/html)" if source == "GFEX"
                    else None
                ) if failed else None
                for venue in source_venues[source]:
                    rows.append({
                        "trading_day": trading_day, "exchange": venue,
                        "status": "FAILED" if failed else "PASS",
                        "contract_count": 0, "record_count": 0,
                        "source": f"official-{source.lower()}-member-ranking",
                        "error": message, "collected_at": "2026-09-27T00:00:00+00:00",
                        "publisher": venue, "source_url": None,
                        "adapter": f"official-{source.lower()}-rank-v1",
                        "retrieved_at": "2026-09-27T00:00:00+00:00",
                        "payload_sha256": None, "quality_status": "FAILED" if failed else "PASS",
                        "legacy_adapter": None,
                    })
            store.upsert_futures_member_position_coverage(rows)
        finally:
            store.close()
        status = "PARTIAL_FAILURE" if trading_day == days[0] else "PASS"
        return {"tasks": [{"status": status, "rows": 0}]}

    monkeypatch.setattr("market_monitor.collector.run_official_member_rank_sync", sync)
    summaries = [
        run_official_member_rank_backfill(
            tmp_path, start_day=days[0], end_day=days[-1], max_days=1,
            min_interval_seconds=0,
        )
        for _ in range(3)
    ]
    assert [call[0] for call in calls] == list(days)
    assert [summary["outcomes"][0]["tradingDay"] for summary in summaries] == list(days)
    assert calls[1][1] == ("CFFEX", "CZCE", "SHFE")
    store = MarketStore(tmp_path)
    try:
        day_two = store.list_futures_member_position_coverage(
            trading_day=days[1], commodity_only=False,
        )
    finally:
        store.close()
    assert {row["exchange"]: row["status"] for row in day_two if row["source"].startswith("official-")} == {
        "CFFEX": "PASS", "CZCE": "PASS", "SHFE": "PASS", "INE": "PASS",
        "DCE": "DEFERRED", "GFEX": "DEFERRED",
    }


def _record_official_rows(root: Path, *, partial_day: str | None = None) -> None:
    exchange_contracts = {
        "SHFE": ("CU", "CU2610"), "INE": ("SC", "SC2610"),
        "DCE": ("M", "M2610"), "CZCE": ("AP", "AP2610"),
        "GFEX": ("SI", "SI2610"), "CFFEX": ("IF", "IF2610"),
    }
    days = ("2026-09-24", "2026-09-25")
    store = MarketStore(root)
    try:
        for day in days:
            for exchange, (product, contract) in exchange_contracts.items():
                adapter = f"official-{exchange.lower()}-daily-v2"
                is_failed = day == partial_day and exchange == "DCE"
                row = {
                    "trade_date": day, "exchange": exchange, "contract_code": contract,
                    "product_code": product, "close": 100.0,
                    "settlement": None if exchange == "CFFEX" else 100.0,
                    "volume": 10.0, "open_interest": 100.0 if day == days[0] else 110.0,
                    "turnover_raw": None, "turnover_cny": None, "turnover_unit": None,
                    "turnover_sidedness": None, "source_url": f"https://{exchange.lower()}.example/official",
                    "adapter": adapter, "payload_sha256": "a" * 64,
                    "retrieved_at": "2026-09-27T00:00:00+00:00", "quality_status": "PASS",
                    "volume_sidedness": "ONE_SIDED", "open_interest_sidedness": "ONE_SIDED",
                    "turnover_quality_status": "UNVERIFIED", "parser_version": "official-futures-daily-v2",
                }
                coverage = {
                    "trade_date": day, "exchange": exchange,
                    "status": "FAILED" if is_failed else "PASS",
                    "record_count": 0 if is_failed else 1, "usable_count": 0 if is_failed else 1,
                    "adapter": adapter, "source_url": row["source_url"],
                    "payload_sha256": None if is_failed else "a" * 64,
                    "error": "fixture source failure" if is_failed else None,
                    "retrieved_at": row["retrieved_at"],
                }
                store.record_futures_official_daily([] if is_failed else [row], coverage)
    finally:
        store.close()


def test_official_structure_has_area_gaps_but_retains_verified_product_lines(tmp_path: Path) -> None:
    _calendar(tmp_path, "2026-09-24", "2026-09-25")
    _record_official_rows(tmp_path, partial_day="2026-09-25")
    oi = run_official_product_structure_pipeline(tmp_path, metric="open_interest")
    volume = run_official_product_structure_pipeline(tmp_path, metric="volume")
    assert oi["baselineDay"] == "2026-09-24"
    assert volume["coverageDays"] == 2

    client = TestClient(create_web_app(tmp_path), client=("127.0.0.1", 50000))
    payload = client.get(
        "/api/futures/structures/product-open-interest",
        params={"source": "official", "range": "all"},
    ).json()
    assert payload["formulaVersion"] == "futures-structure-oi-official-v1"
    assert payload["dates"] == ["2026-09-24", "2026-09-25"]
    assert payload["totals"][0] == 600
    assert payload["totals"][1] is None
    copper = next(item for item in payload["series"] if item["memberKey"] == "SHFE.CU")
    assert copper["values"] == [100, None]
    assert copper["lineValues"] == [100, 110]
    assert payload["coverage"][1]["missingExchanges"] == ["DCE"]

    volume_payload = client.get(
        "/api/futures/structures/product-volume", params={"source": "official", "range": "all"},
    ).json()
    assert volume_payload["metric"] == "tradedVolume"
    assert volume_payload["totals"] == [60, None]


def test_official_notional_reports_missing_multipliers_without_falling_back_to_tdx(tmp_path: Path) -> None:
    _calendar(tmp_path, "2026-09-24")
    _record_official_rows(tmp_path)
    summary = run_official_product_structure_pipeline(
        tmp_path, metric="notional", start_day="2026-09-24", end_day="2026-09-24",
    )
    assert summary["baselineDay"] == "2026-09-24"
    assert summary["baselineQuality"] == "PARTIAL"
    client = TestClient(create_web_app(tmp_path), client=("127.0.0.1", 50000))
    payload = client.get(
        "/api/futures/structures/product-notional",
        params={"source": "official", "range": "all"},
    ).json()
    assert payload["formulaVersion"] == "futures-structure-notional-official-v5"
    assert payload["baselineQualityStatus"] == "PARTIAL"
    assert payload["totals"] == [None]
    assert payload["coverage"][0]["missingSettlementCount"] == 1
    assert payload["coverage"][0]["missingMultiplierCount"] == 2
