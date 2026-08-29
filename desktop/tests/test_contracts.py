"""The desktop contract suite consumes the shared checked-in fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from market_monitor.contracts import ContractValidationError, validate_contract


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "contracts"
CASES = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_shared_contract_fixtures(case: dict[str, object]) -> None:
    fixture = FIXTURES / str(case["fixture"])
    document = json.loads(fixture.read_text(encoding="utf-8"))
    if case["valid"]:
        validate_contract(str(case["schema"]), document)
    else:
        with pytest.raises(ContractValidationError):
            validate_contract(str(case["schema"]), document)


def test_bar_contract_accepts_tdx_v2_provenance_and_extended_asset_type() -> None:
    validate_contract("bar.schema.json", {
        "schema_version": 1,
        "instrument_key": {
            "country_or_market": "CN",
            "exchange": "SSE",
            "asset_type": "CONVERTIBLE_BOND",
            "code": "110075",
        },
        "period": "1d",
        "trading_day": "2026-08-14",
        "bar_open_time": "2026-08-14T00:00:00+08:00",
        "bar_close_time": "2026-08-14T15:00:00+08:00",
        "timezone": "Asia/Shanghai",
        "open": 106.0,
        "high": 107.0,
        "low": 105.0,
        "close": 106.5,
        "volume": 10000.0,
        "amount": 1065000.0,
        "open_interest": None,
        "price_mode": "RAW",
        "source": {
            "provider": "tdx_local",
            "source_symbol": "110075",
            "retrieved_at": "2026-08-14T16:00:00+08:00",
        },
        "source_period": "1d",
        "quality_status": "PASS",
        "raw_open": 1060000,
        "raw_high": 1070000,
        "raw_low": 1050000,
        "raw_close": 1065000,
        "raw_volume": 1000,
        "raw_amount": 1065000,
        "price_scale": 10000,
        "volume_multiplier": 10,
        "volume_unit": "BOND_UNIT",
        "normalization_method": "vwap-within-ohlc",
        "normalization_status": "PASS",
        "normalization_version": "tdx-cn-v2",
    })
