from datetime import date, datetime, timezone

import pytest

from market_monitor.macro_series import (
    DEFAULT_MACRO_SERIES,
    MacroPoint,
    derive_series,
    macro_series_index,
    normalise_macro_point,
)


def test_default_macro_series_have_complete_registration():
    index = macro_series_index()
    assert len(DEFAULT_MACRO_SERIES) == 27
    assert len(index) == 27
    required = {
        "M0_MONEY_SUPPLY",
        "CN_IMPORT_USD_YOY",
        "CN_EXPORT_USD_YOY",
        "CN_TRADE_BALANCE_USD",
        "CN_RETAIL_SALES_YOY",
        "CN_RETAIL_SALES_MOM",
        "CN_FOREX_RESERVES",
        "M1_MONEY_SUPPLY",
        "M2_MONEY_SUPPLY",
        "DR007",
        "CPI",
        "PPI",
        "CPI_PPI_SPREAD",
        "PMI_MANUFACTURING",
        "PMI_CAIXIN_MANUFACTURING",
        "PMI_CAIXIN_SERVICES",
        "PMI_SERVICES",
        "US_NONFARM_PAYROLLS_SA",
        "CN10Y_YIELD",
        "USD_INDEX",
        "US10Y_YIELD",
        "VIX",
        "FED_FUNDS_RATE",
        "GOLD_SILVER_RATIO",
        "GOLD_OIL_RATIO",
        "BTC_USD",
        "ETH_USD",
    }
    assert set(index) == required
    for series in DEFAULT_MACRO_SERIES:
        assert series.series_id
        assert series.name
        assert series.frequency
        assert series.unit
        assert series.source
        assert series.definition
        assert series.calculation_method


def test_normalise_macro_point_rejects_unknown_or_invalid_values():
    with pytest.raises(ValueError, match="unknown macro series_id"):
        normalise_macro_point("NOT_A_SERIES", available_time="2026-08-01", value=1.0)
    with pytest.raises(ValueError, match="finite"):
        normalise_macro_point("CPI", available_time="2026-08-01", value=float("nan"))
    with pytest.raises(ValueError, match="finite"):
        normalise_macro_point("CPI", available_time="2026-08-01", value=float("inf"))
    with pytest.raises(ValueError, match="quality_status"):
        normalise_macro_point("CPI", available_time="2026-08-01", value=1.0, quality_status="WATCH")
    with pytest.raises(ValueError, match="available_time"):
        normalise_macro_point("CPI", available_time="", value=1.0)


def test_normalise_macro_point_accepts_date_and_keeps_registered_fields():
    point = normalise_macro_point(
        "CPI",
        available_time=date(2026, 7, 31),
        value=0.5,
        now=datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
    )
    assert isinstance(point, MacroPoint)
    assert point.available_time == "2026-07-31"
    assert point.series_id == "CPI"
    assert point.name == "居民消费价格指数同比"
    assert point.frequency == "MONTHLY"
    assert point.unit == "%"
    assert point.source == "国家统计局/akshare"
    assert point.quality_status == "PASS"
    assert point.fetched_at == "2026-08-01T12:00:00+00:00"


def _point(series_id, available_time, value):
    return normalise_macro_point(series_id, available_time=available_time, value=value)


def test_derive_series_subtracts_aligned_dates_only():
    cpi = [
        _point("CPI", "2026-05-31", 1.0),
        _point("CPI", "2026-06-30", 1.5),
        _point("CPI", "2026-07-31", 1.2),
    ]
    ppi = [
        _point("PPI", "2026-06-30", -0.8),
        _point("PPI", "2026-07-31", -0.5),
    ]
    output = derive_series(
        {"CPI": cpi, "PPI": ppi},
        derived_series_id="CPI_PPI_SPREAD",
        formula="A-B",
        now=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    by_time = {point.available_time: point.value for point in output}
    assert by_time == {"2026-06-30": pytest.approx(2.3), "2026-07-31": pytest.approx(1.7)}
    for point in output:
        assert point.series_id == "CPI_PPI_SPREAD"
        assert point.source == "local-derived"
        assert point.quality_status == "PASS"


def test_derive_series_skips_non_pass_and_zero_denominator():
    gold = [
        _point("GOLD_SILVER_RATIO", "2026-08-03", 80.0),
        _point("GOLD_SILVER_RATIO", "2026-08-04", 81.0),
    ]
    silver = [
        _point("GOLD_SILVER_RATIO", "2026-08-03", 0.0),
        _point("GOLD_SILVER_RATIO", "2026-08-04", 27.0),
    ]
    # Denominator zero on 08-03 must be skipped; both series use the same id
    # here only as a formula operand placeholder, so values stay separate.
    output = derive_series(
        {"GOLD": gold, "SILVER": silver},
        derived_series_id="GOLD_SILVER_RATIO",
        formula="A/B",
    )
    assert [point.value for point in output] == [pytest.approx(3.0)]
    assert [point.available_time for point in output] == ["2026-08-04"]


def test_derive_series_rejects_bad_formula_or_input_count():
    points = [_point("CPI", "2026-07-31", 1.0)]
    with pytest.raises(ValueError, match="formula"):
        derive_series({"CPI": points, "PPI": points}, derived_series_id="X", formula="A+B")
    with pytest.raises(ValueError, match="exactly two"):
        derive_series({"CPI": points}, derived_series_id="X", formula="A-B")
