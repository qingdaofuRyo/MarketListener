from pathlib import Path
from types import SimpleNamespace

import pytest

from market_monitor import market_continuous_futures_metrics as continuous


def month(code="JM2701", **overrides):
    return {"exchange": "DCE", "productCode": "JM", "contractCode": code,
            "officialMetricsDate": "2026-09-24", "open": 1503., "high": 1511.,
            "low": 1474.5, "close": 1509., "volume": 576325., "openInterest": 411211.,
            "notionalValue": 1000., "capitalDeposit": 100., "amount": 70.,
            "specMarginRateLong": .1, "specMarginRateShort": .1, "marginRateUsed": .1,
            "officialMetricsStatus": "PASS", "officialPayloadSha256": "a" * 64,
            "marginPayloadSha256": "b" * 64, **overrides}


def quote(**overrides):
    return {"trading_day": "2026-09-24", "open": 1503., "high": 1511.,
            "low": 1474.5, "close": 1509., "volume": 576325., "open_interest": 411211., **overrides}


def item(kind="MAIN"):
    return {"assetType": "FUTURE", "seriesKind": kind, "exchange": "DCE", "productCode": "JM",
            "actualSource": "通达信期货通", "storageInstrumentId": "CN.DCE.FUTURE.JM.MAIN.TDX"}


def test_tdx_main_matches_full_same_day_contract_not_largest_capital():
    rows = [month(), month("JM2705", open=1400, close=1400, capitalDeposit=10000)]
    source = quote()
    result = continuous.match_tdx_month(source, rows)
    assert result["underlyingContractCode"] == "JM2701"
    assert result["mappingStatus"] == "MATCHED"
    assert result["capitalDeposit"] == 100
    assert result["mappingAsOfDate"] == "2026-09-24"
    assert source == quote()


def test_price_only_match_missing_fingerprint_or_ambiguous_months_never_guesses():
    assert continuous.match_tdx_month(quote(), [month(officialMetricsDate="2026-09-30")])["mappingStatus"] == "UNMATCHED"
    result = continuous.match_tdx_month(quote(open_interest=None), [month()])
    assert result["mappingStatus"] == "UNMATCHED"
    assert result["capitalDeposit"] is None
    result = continuous.match_tdx_month(quote(), [month(), month("JM2705")])
    assert result["mappingStatus"] == "AMBIGUOUS"
    assert result["underlyingContractCode"] is None
    assert result["notionalValue"] is None


def test_tdx_month_mapping_keeps_a_verified_quantity_revision_unpublished():
    local = month("JM2701", tdxSourceInstrumentId="synthetic-month.TDX",
                  comparisonStatus="MISMATCH", capitalDeposit=None)
    official = month("JM2701", volume=576327, openInterest=411212, capitalDeposit=222)
    result = continuous.match_tdx_month(quote(), [local])
    assert result["mappingStatus"] == "MATCHED"
    assert result["underlyingContractCode"] == "JM2701"
    assert result["capitalDeposit"] is None
    assert result["mappingSourceInstrumentId"] == "synthetic-month.TDX"
    assert result["mappingRevisedFields"] == {}
    assert continuous.match_tdx_month(quote(), [official], [local])["mappingStatus"] == "UNMATCHED"
    assert continuous.match_tdx_month(quote(), [local, {**local, "contractCode": "JM2705"}])["mappingStatus"] == "AMBIGUOUS"


def test_weighted_money_is_sum_of_per_contract_not_a_weighted_price_margin():
    result = continuous.weighted_totals([month(), month("JM2705", notionalValue=2000,
        capitalDeposit=400, amount=100, specMarginRateLong=.2, specMarginRateShort=.2)], "2026-09-24")
    assert result["notionalValue"] == 3000
    assert result["capitalDeposit"] == 500
    assert result["amount"] == 170
    assert result["monthlyOpenInterest"] == 822422
    assert "openInterest" not in result  # The L9 quote keeps its own TDX quantity.
    assert result["marginRateUsed"] is None
    assert result["aggregateContractCodes"] == ["JM2701", "JM2705"]


def test_weighted_gap_preserves_other_complete_metrics_and_real_zero():
    result = continuous.weighted_totals([month(), month("JM2705", capitalDeposit=None, amount=0)], "2026-09-24")
    assert result["capitalDeposit"] is None
    assert result["amount"] == 70
    assert result["notionalValue"] == 2000
    assert result["aggregateMissingContracts"]["capitalDeposit"] == ["JM2705"]
    assert result["officialMetricsStatus"] == "PARTIAL"


@pytest.mark.parametrize("row", [month(officialMetricsDate="2026-09-30"), month(productCode="J"), month()])
def test_weighted_rejects_mixed_day_product_and_duplicate_contract(row):
    with pytest.raises(ValueError):
        continuous.weighted_totals([month(), row], "2026-09-24")


def test_no_non_tdx_main_route_can_claim_a_verified_tdx_mapping(tmp_path):
    result = continuous.continuous_snapshot(tmp_path, {**item(), "actualSource": "other", "storageInstrumentId": "other"},
        quote(), {"DCE.JM|2026-09-24": [month()]})
    assert result["mappingStatus"] == "TDX_SOURCE_UNAVAILABLE"
    assert result["capitalDeposit"] is None


def test_daily_main_roll_and_week_money_use_daily_mapped_contracts(tmp_path: Path, monkeypatch):
    rows = {"DCE.JM|2026-09-21": [month(officialMetricsDate="2026-09-21")],
            "DCE.JM|2026-09-22": [month("JM2705", officialMetricsDate="2026-09-22", open=1550,
                  high=1551, low=1540, close=1545, volume=1000, openInterest=2000,
                  capitalDeposit=400, notionalValue=2000, amount=100)]}
    monkeypatch.setattr(continuous, "product_months", lambda *a, **k: rows)
    monkeypatch.setattr(continuous, "load_futures_trading_calendar", lambda root:
        SimpleNamespace(between=lambda start, end: ["2026-09-21", "2026-09-22"]))
    bars = [quote(trading_day="2026-09-21", bar_open_time="2026-09-21T09:00:00+08:00", bar_close_time="2026-09-21T15:00:00+08:00"),
            quote(trading_day="2026-09-22", bar_open_time="2026-09-22T09:00:00+08:00", bar_close_time="2026-09-22T15:00:00+08:00",
                  open=1550, high=1551, low=1540, close=1545, volume=1000, open_interest=2000)]
    result = continuous.aggregate_continuous_daily(tmp_path, bars, item(), "1w")[0]
    assert result["close"] == 1545
    assert result["underlying_contract_code"] == "JM2705"
    assert result["period_underlying_contract_codes"] == ["JM2701", "JM2705"]
    assert result["amount"] == 170
    assert result["capital_deposit"] == 400
    assert result["notional_value"] == 2000


def test_minute_series_retains_tdx_prices_and_hides_daily_money(tmp_path):
    rows = [quote(amount=100, capital_deposit=200, notional_value=300)]
    continuous.enrich_continuous_bars(tmp_path, rows, item("WEIGHTED"), "1h")
    assert rows[0]["close"] == 1509
    assert rows[0]["amount"] is rows[0]["capital_deposit"] is rows[0]["notional_value"] is None


def test_continuous_daily_change_uses_previous_settlement(tmp_path, monkeypatch):
    from market_monitor import market_tdx_futures_projection as projection

    rows = {"DCE.JM|2026-09-21": [month(officialMetricsDate="2026-09-21")],
            "DCE.JM|2026-09-22": [month(officialMetricsDate="2026-09-22", close=1510)]}
    monkeypatch.setattr(continuous, "product_months", lambda *args, **kwargs: rows)
    monkeypatch.setattr(projection, "load_futures_trading_calendar", lambda root:
                        SimpleNamespace(trading_days=("2026-09-21", "2026-09-22")))
    bars = [quote(trading_day="2026-09-21", settlement=1490),
            quote(trading_day="2026-09-22", close=1510, settlement=1500)]
    continuous.enrich_continuous_bars(tmp_path, bars, item(), "1d")
    assert bars[-1]["change"] == 20
    assert bars[-1]["pct_change"] == pytest.approx(100 * 20 / 1490)


def test_continuous_catalog_prefers_tdx_even_when_another_source_is_newer(tmp_path, monkeypatch):
    from market_monitor.web_api import market

    canonical = "CN.DCE.FUTURE.JM.MAIN"
    tdx = {**item(), "instrumentId": canonical + ".TDX", "canonicalInstrumentId": canonical,
           "lastBarAt": "2026-09-24T15:00:00+08:00", "source": "通达信期货通"}
    other = {**tdx, "instrumentId": canonical + ".OTHER", "actualSource": "AKShare",
             "source": "AKShare", "lastBarAt": "2026-09-30T15:00:00+08:00"}
    monkeypatch.setattr(market, "load_inventory", lambda root:
                        SimpleNamespace(instruments={"tdx": tdx, "other": other}))
    monkeypatch.setattr(market, "market_data_version", lambda root: "synthetic-tdx-priority")
    result = market._logical_instruments(tmp_path)[canonical]
    assert result["storageInstrumentId"] == tdx["instrumentId"]
    assert result["sourceCandidates"] == [tdx["instrumentId"]]
    assert result["availability"] == "数据滞后"


def test_month_history_groups_all_contracts_without_losing_earlier_days(tmp_path, monkeypatch):
    raw = {"DCE.JM2701|2026-09-23": month(officialMetricsDate="2026-09-23"),
           "DCE.JM2701|2026-09-24": month(), "DCE.JM2705|2026-09-24": month("JM2705")}
    observed = []
    monkeypatch.setattr(continuous, "financial_metrics_version", lambda root: "synthetic-history")

    def capture(root, **kwargs):
        observed.append(kwargs)
        return raw

    monkeypatch.setattr(continuous, "official_metrics", capture)
    result = continuous.product_months(tmp_path, exchange="DCE", product="JM",
                                       start="2026-09-23", end="2026-09-24")
    assert len(result["DCE.JM|2026-09-23"]) == 1
    assert len(result["DCE.JM|2026-09-24"]) == 2
    assert observed == [{"exchange": "DCE", "product": "JM", "start": "2026-09-23",
                         "end": "2026-09-24", "by_contract": True}]


def test_product_months_reuses_exact_window_until_financial_version_changes(tmp_path, monkeypatch):
    versions = ["synthetic-financial-v1", "synthetic-silver-v1"]
    calls = []
    monkeypatch.setattr(continuous, "financial_metrics_version", lambda root: versions[0])
    monkeypatch.setattr(continuous, "market_data_version", lambda root: versions[1])

    def read_official(root, **kwargs):
        calls.append(kwargs)
        return {"DCE.JM2701|2026-09-24": month()}

    monkeypatch.setattr(continuous, "official_metrics", read_official)
    first = continuous.product_months(tmp_path, exchange="DCE", product="JM",
                                      start="2026-09-24", end="2026-09-24")
    second = continuous.product_months(tmp_path, exchange="DCE", product="JM",
                                       start="2026-09-24", end="2026-09-24")
    assert first == second
    assert len(calls) == 1
    versions[0] = "synthetic-financial-v2"
    refreshed = continuous.product_months(tmp_path, exchange="DCE", product="JM",
                                           start="2026-09-24", end="2026-09-24")
    assert refreshed == first
    assert len(calls) == 2
    versions[1] = "synthetic-silver-v2"
    refreshed_silver = continuous.product_months(tmp_path, exchange="DCE", product="JM",
                                                start="2026-09-24", end="2026-09-24")
    assert refreshed_silver == first
    assert len(calls) == 3


@pytest.mark.parametrize("product", ["L", "PP", "V"])
def test_monthly_average_continuous_does_not_sum_ordinary_product(tmp_path, product):
    average = {**item("WEIGHTED"), "productCode": product, "symbol": product + "-FL9"}
    months = {f"DCE.{product}-F|2026-09-24": [month(product + "-F2610", productCode=product + "-F")],
              f"DCE.{product}|2026-09-24": [month(product + "2610", productCode=product, amount=900)]}
    result = continuous.continuous_snapshot(tmp_path, average, quote(), months)
    assert result["aggregateContractCodes"] == [product + "-F2610"]
    assert result["amount"] == 70
