from __future__ import annotations

from pathlib import Path
import json

import pytest

from market_monitor import market_official_futures_metrics as metrics


def _fact(**overrides):
    return {"trade_date": "2026-09-30", "settlement": 120, "open_interest": 100,
            "open_interest_sidedness": "ONE_SIDED", "turnover_cny": 0,
            "turnover_quality_status": "PASS", "turnover_sidedness": "ONE_SIDED", **overrides}


def test_speculative_capital_sums_both_sides_with_distinct_rates():
    result = metrics.metric_from_fact(_fact(), {"spec_long_rate": .1, "spec_short_rate": .12}, 10)
    assert result["notionalValue"] == 120_000
    assert result["capitalDeposit"] == 26_400
    assert result["capitalDepositLong"] == 12_000
    assert result["capitalDepositShort"] == 14_400
    assert result["amount"] == 0
    assert result["marginRateUsed"] == .22


def test_missing_or_unverified_inputs_never_fall_back_to_old_margin():
    result = metrics.metric_from_fact(_fact(open_interest_sidedness="TWO_SIDED"),
                                      {"spec_long_rate": .1, "spec_short_rate": .1}, 10)
    assert result["notionalValue"] is None
    assert result["capitalDeposit"] is None
    result = metrics.metric_from_fact(_fact(), {"spec_long_rate": .1}, 10)
    assert result["notionalValue"] == 120_000
    assert result["capitalDeposit"] is None
    assert result["marginRateUsed"] is None


def test_current_day_backup_is_visible_as_backup_with_both_sides():
    result = metrics.metric_from_fact(_fact(), {"spec_long_rate": .12, "spec_short_rate": .12,
        "source_kind": "THIRD_PARTY_CURRENT_EXCHANGE_STANDARD", "source_url": "https://example.test/dated-standard"}, 10)
    assert result["capitalDeposit"] == 28_800
    assert result["capitalDepositLong"] == result["capitalDepositShort"] == 14_400
    assert result["officialMetricsStatus"] == "BACKUP_VERIFIED"
    assert result["marginSourceKind"] == "THIRD_PARTY_CURRENT_EXCHANGE_STANDARD"


def test_published_zero_position_preserves_zero_notional_without_a_traded_price():
    result = metrics.metric_from_fact(_fact(open_interest=0, settlement=0),
                                      {"spec_long_rate": .1, "spec_short_rate": .1}, 10)
    assert result["notionalValue"] == result["capitalDeposit"] == result["amount"] == 0
    result = metrics.metric_from_fact(_fact(), {"spec_long_rate": 11, "spec_short_rate": 12}, 10)
    assert result["capitalDeposit"] is None
    zero = metrics.metric_from_fact(_fact(open_interest=0, settlement=None),
                                    {"spec_long_rate": .1, "spec_short_rate": .12}, None)
    assert zero["notionalValue"] == zero["capitalDeposit"] == 0


def test_monthly_average_and_legacy_czce_contract_identity():
    assert metrics.contract_identity({"exchange": "DCE", "symbol": "l2610F"}) == ("DCE", "L-F2610")
    assert metrics.contract_identity({"exchange": "CZCE", "symbol": "SR701", "lastBarAt": "2026-09-30"}) == ("CZCE", "SR2701")


@pytest.mark.parametrize("period", ["1m", "5m", "1h", "4h"])
def test_minute_metrics_are_explicitly_unavailable(tmp_path: Path, period: str):
    bars = [{"amount": 1234, "capital_deposit": 100, "notional_value": 999}]
    metrics.enrich_official_bars(tmp_path, bars, {"assetType": "FUTURE", "seriesKind": "CONTRACT"}, period)
    assert all(bars[0][field] is None for field in ("amount", "capital_deposit", "notional_value"))


def test_weekly_amount_missing_day_is_gap_and_snapshot_uses_period_end(tmp_path: Path, monkeypatch):
    class Calendar:
        def between(self, start, end):
            return ["2026-09-28", "2026-09-29", "2026-09-30"]
    values = {"2026-09-28": {"amount": 100}, "2026-09-30": {
        "amount": 300, "notionalValue": 120_000, "capitalDeposit": 12_000}}
    monkeypatch.setattr(metrics, "official_metrics", lambda *a, **k: values)
    monkeypatch.setattr(metrics, "load_futures_trading_calendar", lambda *a: Calendar())
    item = {"assetType": "FUTURE", "seriesKind": "CONTRACT", "exchange": "DCE", "symbol": "M2701"}
    bars = [{"trading_day": "2026-09-30", "amount": 999}]
    metrics.enrich_official_bars(tmp_path, bars, item, "1w")
    assert bars[0]["amount"] is None
    assert bars[0]["notional_value"] == 120_000
    assert bars[0]["capital_deposit"] == 12_000
    values["2026-09-29"] = {"amount": 200}
    metrics.enrich_official_bars(tmp_path, bars, item, "1w")
    assert bars[0]["amount"] == 600


def test_snapshot_builder_only_runs_once_across_same_version(tmp_path: Path):
    from market_monitor.market_list_snapshot import list_snapshot
    calls = []
    def build():
        calls.append(1)
        return [{"instrumentId": "CN.DCE.FUTURE.M2701", "amount": 0}]
    assert list_snapshot(tmp_path, "v1", "futures", build)[0]["amount"] == 0
    assert list_snapshot(tmp_path, "v1", "futures", build)[0]["amount"] == 0
    assert len(calls) == 1
    list_snapshot(tmp_path, "v2", "futures", build)
    assert len(calls) == 2


def test_official_database_selects_one_verified_source_and_exact_margin_day(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace
    from market_monitor.storage import MarketStore
    from market_monitor.futures_settlement_parameters import parse_shfe, import_partition
    from market_monitor.futures_official_daily_sources import PARSER_VERSION

    store = MarketStore(tmp_path)
    for adapter, settlement, retrieved in (("official-shfe-daily-v2", 100, "2026-09-30T08:00:00Z"),
                                           ("official-alt-daily-v2", 120, "2026-09-30T09:00:00Z")):
        row = {**_fact(settlement=settlement), "exchange": "SHFE", "contract_code": "CU2610",
               "product_code": "CU", "close": settlement, "volume": 5, "turnover_raw": 0,
               "turnover_unit": "CNY", "volume_sidedness": "ONE_SIDED", "source_url": "https://www.shfe.com.cn/data/sample.dat",
               "adapter": adapter, "payload_sha256": "a" * 64, "retrieved_at": retrieved,
               "quality_status": "PASS", "parser_version": PARSER_VERSION}
        store.record_futures_official_daily([row], {"trade_date": row["trade_date"], "exchange": "SHFE",
            "adapter": adapter, "source_url": row["source_url"], "payload_sha256": "a" * 64,
            "retrieved_at": retrieved, "parser_version": PARSER_VERSION, "status": "PASS",
            "record_count": 1, "usable_count": 1, "error": None})
    raw = json.dumps({"report_date": "20260929", "o_cursor": [{"INSTRUMENTID": "cu2610",
        "SPECLONGMARGINRATIO": .1, "SPECSHORTMARGINRATIO": .12}]}).encode()
    rows = parse_shfe(raw, trading_day="2026-09-29", source_url="https://www.shfe.com.cn/data/js20260929.dat",
                      retrieved_at="2026-09-29T08:00:00Z")
    import_partition(store.connection, rows, exchange="SHFE", trade_date="2026-09-29",
                     source_url=rows[0]["source_url"], payload_sha256=rows[0]["payload_sha256"],
                     retrieved_at=rows[0]["retrieved_at"])
    store.close()
    monkeypatch.setattr(metrics, "_registry", lambda *_: SimpleNamespace(resolve=lambda *a: SimpleNamespace(contract_multiplier=10)))
    result = metrics.official_metrics(tmp_path, exchange="SHFE", contract="CU2610", start="2026-09-30", end="2026-09-30")
    assert result["2026-09-30"]["notionalValue"] == 120_000
    assert result["2026-09-30"]["capitalDeposit"] is None
    version = metrics.refresh_financial_metrics_version(tmp_path)
    store = MarketStore(tmp_path)
    store.close()
    assert metrics.financial_metrics_version(tmp_path) == version
    raw = json.dumps({"report_date": "20260930", "o_cursor": [{"INSTRUMENTID": "cu2610",
        "SPECLONGMARGINRATIO": .1, "SPECSHORTMARGINRATIO": .12}]}).encode()
    rows = parse_shfe(raw, trading_day="2026-09-30", source_url="https://www.shfe.com.cn/data/js20260930.dat",
                      retrieved_at="2026-09-30T08:00:00Z")
    store = MarketStore(tmp_path)
    import_partition(store.connection, rows, exchange="SHFE", trade_date="2026-09-30",
                     source_url=rows[0]["source_url"], payload_sha256=rows[0]["payload_sha256"],
                     retrieved_at=rows[0]["retrieved_at"])
    store.close()
    metrics.refresh_financial_metrics_version(tmp_path)
    history = metrics.official_metrics(tmp_path, exchange="SHFE", contract="CU2610",
                                       start="2026-09-30", end="2026-09-30")
    latest = metrics.official_metrics(tmp_path, latest=True)
    all_contracts = metrics.official_metrics(tmp_path, start="2026-09-30", end="2026-09-30", by_contract=True)
    assert set(history) == {"2026-09-30"}
    assert set(latest) == {"SHFE.CU2610"}
    assert set(all_contracts) == {"SHFE.CU2610|2026-09-30"}
    assert history["2026-09-30"]["capitalDeposit"] == 26_400


def test_margin_report_day_cannot_bypass_later_effective_day(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace

    from market_monitor.futures_official_daily_sources import PARSER_VERSION
    from market_monitor.futures_settlement_parameters import import_partition, parse_cffex
    from market_monitor.storage import MarketStore

    store = MarketStore(tmp_path)
    try:
        for day in ("2026-09-21", "2026-09-22"):
            row = {**_fact(), "trade_date": day, "exchange": "CFFEX", "contract_code": "IF2610",
                   "product_code": "IF", "close": 120, "volume": 5,
                   "turnover_raw": 0, "turnover_unit": "CNY",
                   "volume_sidedness": "ONE_SIDED", "source_url": "http://www.cffex.com.cn/sj/fixture",
                   "adapter": "official-cffex-daily-v2", "payload_sha256": "a" * 64,
                   "retrieved_at": "2026-09-22T16:00:00+08:00", "quality_status": "PASS",
                   "parser_version": PARSER_VERSION}
            store.record_futures_official_daily([row], {
                "trade_date": day, "exchange": "CFFEX", "adapter": row["adapter"],
                "source_url": row["source_url"], "payload_sha256": row["payload_sha256"],
                "retrieved_at": row["retrieved_at"], "parser_version": PARSER_VERSION,
                "status": "PASS", "record_count": 1, "usable_count": 1, "error": None,
            })
        raw = ("期货合约结算业务参数表（20260921）\n"
               "期货合约,合约多头保证金标准,合约空头保证金标准\nIF2610,12%,12%\n").encode("gb18030")
        url = "http://www.cffex.com.cn/sj/jscs/202609/21/20260921_1.csv"
        rows = parse_cffex(raw, trading_day="2026-09-21", source_url=url,
                           retrieved_at="2026-09-21T16:00:00+08:00")
        rows[0].update(effective_from="2026-09-22", effective_to="2026-09-30",
                       interval_source_url="http://www.cffex.com.cn/jystz/fixture.html",
                       interval_payload_sha256="b" * 64)
        import_partition(store.connection, rows, exchange="CFFEX", trade_date="2026-09-21",
                         source_url=url, payload_sha256=rows[0]["payload_sha256"],
                         retrieved_at=rows[0]["retrieved_at"])
        from market_monitor.market_tdx_futures_projection import official_price_checks

        checks = official_price_checks(tmp_path, {("CFFEX", "IF2610", "2026-09-21")})
        assert checks["CFFEX", "IF2610", "2026-09-21"]["settlement"] == 120
    finally:
        store.close()
    monkeypatch.setattr(metrics, "_registry", lambda *_: SimpleNamespace(
        resolve=lambda *args: SimpleNamespace(contract_multiplier=300)))
    history = metrics.official_metrics(tmp_path, exchange="CFFEX", contract="IF2610",
                                       start="2026-09-21", end="2026-09-22")
    assert history["2026-09-21"]["capitalDeposit"] is None
    assert history["2026-09-22"]["capitalDeposit"] == 120 * 100 * 300 * .24
    previous_version = metrics.refresh_financial_metrics_version(tmp_path)
    store = MarketStore(tmp_path)
    store.connection.execute("UPDATE futures_settlement_parameter_official SET effective_to='2026-09-29'")
    store.close()
    metrics.refresh_financial_metrics_version(tmp_path)
    assert metrics.financial_metrics_version(tmp_path) != previous_version


def test_cffex_contract_metrics_use_latest_published_sheet_without_future_leak(tmp_path: Path, monkeypatch):
    from hashlib import sha256
    from types import SimpleNamespace

    from market_monitor.futures_official_daily_sources import PARSER_VERSION
    from market_monitor.futures_settlement_parameters import import_partition, parse_cffex
    from market_monitor.storage import MarketStore

    store = MarketStore(tmp_path)
    try:
        for day in ("2026-09-18", "2026-10-16"):
            fact = {**_fact(), "trade_date": day, "exchange": "CFFEX", "contract_code": "IF2611",
                    "product_code": "IF", "close": 120, "volume": 5, "turnover_raw": 0,
                    "turnover_unit": "CNY", "volume_sidedness": "ONE_SIDED",
                    "source_url": "http://www.cffex.com.cn/sj/fixture",
                    "adapter": "official-cffex-daily-v2", "payload_sha256": "a" * 64,
                    "retrieved_at": "2026-10-16T16:00:00+08:00", "quality_status": "PASS",
                    "parser_version": PARSER_VERSION}
            store.record_futures_official_daily([fact], {
                "trade_date": day, "exchange": "CFFEX", "adapter": fact["adapter"],
                "source_url": fact["source_url"], "payload_sha256": fact["payload_sha256"],
                "retrieved_at": fact["retrieved_at"], "parser_version": PARSER_VERSION,
                "status": "PASS", "record_count": 1, "usable_count": 1, "error": None,
            })
        for compact, rate in (("20260914", "10%"), ("20260921", "12%")):
            day = f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
            raw = (f"期货合约结算业务参数表（{compact}）\n"
                   "期货合约,合约多头保证金标准,合约空头保证金标准\n"
                   f"IF2611,{rate},{rate}\n").encode("gb18030")
            url = f"http://www.cffex.com.cn/sj/jscs/{compact[:6]}/{compact[6:]}/{compact}_1.csv"
            rows = parse_cffex(raw, trading_day=day, source_url=url,
                               retrieved_at="2026-10-16T16:00:00+08:00")
            import_partition(store.connection, rows, exchange="CFFEX", trade_date=day,
                             source_url=url, payload_sha256=sha256(raw).hexdigest(),
                             retrieved_at="2026-10-16T16:00:00+08:00")
    finally:
        store.close()
    monkeypatch.setattr(metrics, "_registry", lambda *_: SimpleNamespace(
        resolve=lambda *args: SimpleNamespace(contract_multiplier=300)))
    history = metrics.official_metrics(tmp_path, exchange="CFFEX", contract="IF2611",
                                       start="2026-09-18", end="2026-10-16")
    assert history["2026-09-18"]["capitalDeposit"] == 120 * 100 * 300 * .20
    assert history["2026-09-18"]["marginPublishedOn"] == "2026-09-14"
    assert history["2026-10-16"]["capitalDeposit"] == 120 * 100 * 300 * .24
    assert history["2026-10-16"]["marginPublishedOn"] == "2026-09-21"
    assert history["2026-10-16"]["marginCarriedForward"] is True


def test_global_sort_pagination_version_and_lazy_page_decorations(tmp_path: Path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from market_monitor.web_api import market

    app = FastAPI()
    app.state.data_root = tmp_path
    app.include_router(market.router)
    client = TestClient(app)
    rows = {f"CN.SSE.STOCK.60000{i}": {"instrumentId": f"CN.SSE.STOCK.60000{i}",
            "symbol": f"60000{i}", "name": f"样本{i}", "assetType": "STOCK", "market": "CN",
            "exchange": "SSE", "sourceCandidates": [], "amount": amount}
            for i, amount in enumerate((20, None, 50))}
    calls = []
    monkeypatch.setattr(market, "_logical_instruments", lambda root: rows)
    monkeypatch.setattr(market, "batch_daily_tails", lambda *a: {})
    def decorate(item, *args):
        calls.append(item["instrumentId"])
        return dict(item)
    monkeypatch.setattr(market, "_quote_item", decorate)
    first = client.get("/api/market/instruments", params={"pageSize": 1}).json()
    assert len(calls) == 1
    version = first["dataVersion"]
    client.get("/api/market/instruments", params={"pageSize": 1, "version": version})
    assert len(calls) == 1
    second = client.get("/api/market/instruments", params={"pageSize": 1, "page": 2, "version": version}).json()
    assert second["items"][0]["amount"] is None
    assert len(calls) == 2
    sorted_first = client.get("/api/market/instruments", params={"pageSize": 1, "sortField": "amount", "sortDirection": "desc"}).json()
    assert sorted_first["items"][0]["amount"] == 50
    sorted_last = client.get("/api/market/instruments", params={"pageSize": 1, "page": 3, "sortField": "amount", "sortDirection": "desc", "version": version}).json()
    assert sorted_last["items"][0]["amount"] is None
    mismatch = client.get("/api/market/instruments", params={"version": "expired-version"})
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"] == "行情数据版本已更新，请重新加载列表"
