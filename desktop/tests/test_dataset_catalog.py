import pytest

from market_monitor.dataset_catalog import (
    BAR_FIELDS,
    DEFAULT_DATASETS,
    DatasetDefinition,
    dataset_index,
    validate_dataset_definition,
)


EXPECTED_IDS = {
    "CN_STOCK_BAR",
    "HK_STOCK_BAR",
    "STOCK_F10",
    "CN_ETF_BAR",
    "CN_INDEX_BAR",
    "HK_INDEX_BAR",
    "GLOBAL_INDEX_BAR",
    "FUTURE_CONTRACT_BAR",
    "FUTURE_MAIN_BAR",
    "FUTURE_SECONDARY_BAR",
    "FUTURE_WEIGHTED_BAR",
    "FUTURE_INDEX_BAR",
    "MACRO_SERIES",
    "DERIVED_METRIC",
    "STRATEGY_SIGNAL",
    "FUTURES_BREADTH",
    "FUTURES_LONG_SHORT_HEAT",
    "FUTURES_STRUCTURE_DAILY",
    "FUTURES_STRUCTURE_BASELINE",
    "FUTURES_MEMBER_POSITION_DAILY",
    "FUTURES_OI_LEADERBOARD",
    "CN_MARGIN",
    "A_SHARE_BREADTH",
    "HSGT_FLOW",
    "CN_ZT_POOL",
    "FUTURE_GLOBAL_BAR",
    "USD_INDEX_VIX",
}


def _definition(dataset_id="CN_STOCK_BAR", **overrides):
    values = dict(
        dataset_id=dataset_id,
        dataset_name="A股个股K线",
        market="CN",
        asset_type="STOCK",
        frequency="1d",
        source="akshare",
        update_cycle="DAILY",
        primary_key=("instrument_id", "period", "bar_start"),
        fields=BAR_FIELDS,
        sync_policy="FILTERED",
        quality_rule="OHLC 有界、质量 PASS",
    )
    values.update(overrides)
    return DatasetDefinition(**values)


def test_default_datasets_cover_all_required_ids():
    assert {ds.dataset_id for ds in DEFAULT_DATASETS} == EXPECTED_IDS
    assert len(dataset_index()) == len(EXPECTED_IDS)
    for dataset_id in EXPECTED_IDS:
        assert dataset_index()[dataset_id].dataset_id == dataset_id


def test_every_default_dataset_passes_validation_and_round_trip():
    for ds in DEFAULT_DATASETS:
        validate_dataset_definition(ds)
        restored = DatasetDefinition.from_dict(ds.to_dict())
        assert restored == ds


def test_bar_datasets_use_open_close_instead_of_start_end():
    for ds in DEFAULT_DATASETS:
        if not ds.dataset_id.endswith("_BAR"):
            continue
        assert "open" in ds.fields
        assert "close" in ds.fields
        assert "bar_start" in ds.fields
        assert "bar_end" in ds.fields
        assert "start" not in [field.lower() for field in ds.fields]
        assert "end" not in [field.lower() for field in ds.fields]


def test_macro_series_records_source_available_time_quality_status():
    macro = dataset_index()["MACRO_SERIES"]
    for field in ("source", "available_time", "quality_status", "value"):
        assert field in macro.fields


def test_futures_breadth_and_leaderboard_are_registered():
    breadth = dataset_index()["FUTURES_BREADTH"]
    assert breadth.primary_key == ("trading_day", "series_kind")
    for field in ("advances", "declines", "unchanged", "calculation_method"):
        assert field in breadth.fields
    leaderboard = dataset_index()["FUTURES_OI_LEADERBOARD"]
    assert leaderboard.primary_key == ("instrument_id", "trading_day")
    for field in ("long_position", "short_position", "net_position", "net_position_change"):
        assert field in leaderboard.fields
    ranks = dataset_index()["FUTURES_MEMBER_POSITION_DAILY"]
    assert ranks.primary_key == ("trading_day", "exchange", "contract_code", "side", "rank", "source")
    for field in ("member_name", "position", "position_change", "collected_at"):
        assert field in ranks.fields


def test_futures_long_short_heat_registers_replayable_gold_without_user_total():
    heat = dataset_index()["FUTURES_LONG_SHORT_HEAT"]
    assert heat.primary_key == ("formula_version", "trade_date")
    for field in (
        "breadth_score_daily",
        "fund_score_daily",
        "breadth_score_10d",
        "fund_score_10d",
        "source_cutoff",
        "calculation_method",
        "calculated_at",
    ):
        assert field in heat.fields
    assert "total_score_10d" not in heat.fields


def test_futures_structure_datasets_keep_fixed_baseline_and_price_provenance():
    daily = dataset_index()["FUTURES_STRUCTURE_DAILY"]
    assert daily.primary_key == ("chart_id", "direction", "formula_version", "trade_date", "member_key")
    for field in ("value", "data_quality_status", "price_basis", "source", "calculated_at"):
        assert field in daily.fields
    baseline = dataset_index()["FUTURES_STRUCTURE_BASELINE"]
    assert baseline.primary_key == ("chart_id", "direction", "formula_version")
    for field in ("baseline_day", "threshold", "stack_order", "other_members", "price_basis"):
        assert field in baseline.fields


def test_from_dict_rejects_missing_primary_key_or_fields():
    doc = _definition().to_dict()
    doc["primary_key"] = []
    with pytest.raises(ValueError, match="primary_key"):
        DatasetDefinition.from_dict(doc)
    doc = _definition().to_dict()
    doc["fields"] = []
    with pytest.raises(ValueError, match="fields"):
        DatasetDefinition.from_dict(doc)


def test_validate_rejects_non_upper_snake_id():
    with pytest.raises(ValueError, match="UPPER_SNAKE"):
        validate_dataset_definition(_definition(dataset_id="cn_stock"))
    with pytest.raises(ValueError, match="UPPER_SNAKE"):
        validate_dataset_definition(_definition(dataset_id="CNSTOCK"))


def test_validate_rejects_empty_quality_rule_or_sync_policy():
    with pytest.raises(ValueError, match="quality_rule"):
        validate_dataset_definition(_definition(quality_rule=""))
    with pytest.raises(ValueError, match="sync_policy"):
        validate_dataset_definition(_definition(sync_policy=""))
