import json
from datetime import date
from pathlib import Path

import pytest

from market_monitor.futures import (
    FuturesSeriesKind,
    SERIES_KINDS,
    FutureCapitalDeposit,
    build_weighted_series,
    contract_delivery_month,
    compute_future_capital_deposit,
    futures_product_name,
    is_expired_futures_contract,
    normalize_futures_bar,
    resolve_futures_contract_spec,
)


def test_product_name_is_loaded_from_external_contract_spec() -> None:
    assert futures_product_name("PS", "GFEX") == "多晶硅"
    assert futures_product_name("ZN", "SHFE") == "沪锌"
    assert futures_product_name("IF", "CFFEX") == "沪深300"


def test_capital_deposit_doubles_single_side_open_interest():
    common = dict(
        instrument_id="rb2610",
        trading_day=date(2026, 8, 7),
        price=3000.0,
        open_interest=1.0,
        contract_multiplier=10.0,
        margin_rate=0.10,
    )
    both_sides = compute_future_capital_deposit(**common, side_count=2)
    assert both_sides.value == pytest.approx(6000.0)
    single_side = compute_future_capital_deposit(**common, side_count=1)
    assert single_side.value == pytest.approx(3000.0)


def test_capital_deposit_record_keeps_formula_fields():
    deposit = FutureCapitalDeposit(
        instrument_id="rb2610",
        trading_day=date(2026, 8, 7),
        price=3000.0,
        open_interest=100.0,
        contract_multiplier=10.0,
        margin_rate=0.10,
        calculation_time="2026-08-07T15:00:00+08:00",
    )
    doc = deposit.to_dict()
    assert doc["value"] == pytest.approx(600000.0)
    assert doc["formula_version"] == "2026-01-v1"
    assert doc["contract_multiplier"] == 10.0
    assert doc["margin_rate"] == 0.10
    assert doc["open_interest"] == 100.0
    assert doc["calculation_time"] == "2026-08-07T15:00:00+08:00"
    assert doc["formula"] == "OI * contract_multiplier * price * margin_rate * side_count"


def test_capital_deposit_rejects_invalid_parameters():
    base = dict(
        instrument_id="rb2610",
        trading_day=date(2026, 8, 7),
        price=3000.0,
        open_interest=1.0,
        contract_multiplier=10.0,
        margin_rate=0.10,
    )
    with pytest.raises(ValueError, match="price"):
        compute_future_capital_deposit(**{**base, "price": 0})
    with pytest.raises(ValueError, match="open_interest"):
        compute_future_capital_deposit(**{**base, "open_interest": -1})
    with pytest.raises(ValueError, match="side_count"):
        compute_future_capital_deposit(**{**base, "side_count": 3})
    with pytest.raises(ValueError, match="contract_multiplier"):
        compute_future_capital_deposit(**{**base, "contract_multiplier": 0})
    with pytest.raises(ValueError, match="margin_rate"):
        compute_future_capital_deposit(**{**base, "margin_rate": 1.5})


def test_contract_spec_resolves_latest_effective_record(tmp_path: Path) -> None:
    config = tmp_path / "specs.json"
    config.write_text(
        json.dumps(
            {
                "products": {
                    "DCE.JM": {
                        "records": [
                            {"effectiveFrom": "2025-01-01", "contractMultiplier": 60, "marginRate": 0.12},
                            {"effectiveFrom": "2026-07-01", "contractMultiplier": 60, "marginRate": 0.2, "source": "交易所通知"},
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    old = resolve_futures_contract_spec("jm", "dce", date(2026, 6, 30), config_path=config)
    current = resolve_futures_contract_spec("JM", "DCE", date(2026, 8, 1), config_path=config)

    assert old.spec is not None and old.spec.margin_rate == pytest.approx(0.12)
    assert current.spec is not None and current.spec.margin_rate == pytest.approx(0.2)
    assert current.spec.effective_from == date(2026, 7, 1)
    assert current.spec.source == "交易所通知"


def test_contract_spec_reports_missing_rate_instead_of_guessing(tmp_path: Path) -> None:
    config = tmp_path / "specs.json"
    config.write_text(
        '{"products":{"DCE.JM":{"records":[{"effectiveFrom":"2026-01-01","contractMultiplier":60}]}}}',
        encoding="utf-8",
    )

    resolution = resolve_futures_contract_spec("JM", "DCE", date(2026, 8, 1), config_path=config)

    assert resolution.spec is None
    assert resolution.reason == "DCE.JM缺少保证金率"


def test_delivery_month_handles_four_digit_and_czce_three_digit_symbols() -> None:
    today = date(2026, 8, 22)
    assert contract_delivery_month("JM2505", "DCE", reference_day=today) == date(2025, 5, 1)
    assert is_expired_futures_contract("JM2505", "DCE", reference_day=today)
    assert not is_expired_futures_contract("JM2608", "DCE", reference_day=today)
    assert contract_delivery_month(
        "AP505", "CZCE", reference_day=today, last_trading_day=date(2025, 5, 10)
    ) == date(2025, 5, 1)
    assert contract_delivery_month(
        "AP705", "CZCE", reference_day=today, last_trading_day=date(2026, 8, 20)
    ) == date(2027, 5, 1)


def weighted_bar(day, code, close, oi, volume=10.0, amount=100.0):
    return {
        "instrument_key": {"country_or_market": "CN", "exchange": "SHFE", "asset_type": "FUTURE", "code": code},
        "trading_day": day,
        "period": "1d",
        "bar_open_time": f"{day}T09:00:00+08:00",
        "bar_close_time": f"{day}T15:00:00+08:00",
        "open": close - 1,
        "high": close + 1,
        "low": close - 2,
        "close": close,
        "volume": volume,
        "amount": amount,
        "open_interest": oi,
    }


def test_weighted_series_uses_open_interest_weights_and_sums():
    bars = {
        ("2026-08-07", "rb2610"): weighted_bar("2026-08-07", "rb2610", close=100.0, oi=3.0),
        ("2026-08-07", "rb2611"): weighted_bar("2026-08-07", "rb2611", close=120.0, oi=1.0),
    }
    output = build_weighted_series(bars, {"2026-08-07": ["rb2610", "rb2611"]}, instrument_id="RB")
    assert len(output) == 1
    bar = output[0]
    assert bar.close == pytest.approx(105.0)
    assert bar.open == pytest.approx(104.0)
    assert bar.high == pytest.approx(106.0)
    assert bar.low == pytest.approx(103.0)
    assert bar.open_interest == pytest.approx(4.0)
    assert bar.volume == pytest.approx(20.0)
    assert bar.amount == pytest.approx(200.0)
    assert bar.contracts == ("rb2610", "rb2611")
    assert bar.weights == pytest.approx((0.75, 0.25))
    as_bar = bar.to_bar("RB")
    assert as_bar["weighted_contracts"] == ["rb2610", "rb2611"]
    assert as_bar["weighted_algorithm_version"] == 1


def test_weighted_series_skips_missing_contracts_and_empty_days():
    bars = {("2026-08-07", "rb2610"): weighted_bar("2026-08-07", "rb2610", close=100.0, oi=3.0)}
    output = build_weighted_series(
        bars,
        {"2026-08-07": ["rb2610", "rb2611"], "2026-08-10": ["rb2610", "rb2611"]},
        instrument_id="RB",
    )
    assert len(output) == 1
    assert output[0].contracts == ("rb2610",)
    assert output[0].weights == pytest.approx((1.0,))


def test_normalize_futures_bar_marks_series_kind_and_roll_fields():
    row = {
        "code": "rb2610",
        "exchange": "SHFE",
        "trading_day": "2026-08-07",
        "bar_open_time": "2026-08-07T09:00:00+08:00",
        "bar_close_time": "2026-08-07T15:00:00+08:00",
        "open": 3000.0,
        "high": 3050.0,
        "low": 2980.0,
        "close": 3040.0,
        "volume": 100.0,
        "amount": 1e8,
        "open_interest": 1200.0,
    }
    main = normalize_futures_bar(row, FuturesSeriesKind.MAIN)
    assert main["futures_series_kind"] == "MAIN"
    assert main["is_roll_day"] is False
    assert main["roll_gap"] == 0.0
    contract = normalize_futures_bar(row, FuturesSeriesKind.CONTRACT)
    assert contract["futures_series_kind"] == "CONTRACT"
    assert "is_roll_day" not in contract
    weighted = normalize_futures_bar(row, FuturesSeriesKind.WEIGHTED)
    assert weighted["futures_series_kind"] == "WEIGHTED"
    index = normalize_futures_bar(row, FuturesSeriesKind.INDEX)
    assert index["futures_series_kind"] == "INDEX"
    assert SERIES_KINDS == {"CONTRACT", "MAIN", "WEIGHTED", "INDEX"}


def test_normalize_futures_bar_requires_ohlc_fields():
    with pytest.raises(KeyError):
        normalize_futures_bar({"code": "rb2610"}, FuturesSeriesKind.MAIN)
