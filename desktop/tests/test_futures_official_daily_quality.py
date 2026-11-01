from market_monitor.futures_official_daily_quality import validated_daily_inputs


def _fact(day="2026-09-30", version="official-futures-daily-v4"):
    return {"trade_date": day, "exchange": "DCE", "adapter": "official-dce-daily-v2", "quality_status": "PASS", "parser_version": version, "payload_sha256": "a" * 64}


def _coverage(day="2026-09-30", version="official-futures-daily-v4", count=1):
    return {"trade_date": day, "exchange": "DCE", "adapter": "official-dce-daily-v2", "status": "PASS", "parser_version": version, "usable_count": count, "record_count": count, "payload_sha256": "a" * 64}


def test_current_parser_and_exact_row_count_allow_derived_total():
    selected, coverage = validated_daily_inputs([_fact()], [_coverage()])
    assert len(selected) == 1
    assert coverage[0]["status"] == "PASS"


def test_old_or_incomplete_day_stays_gap_instead_of_zero():
    for facts, covered in [([], _coverage()), ([_fact()], _coverage(count=2)), ([_fact(version="official-futures-daily-v2")], _coverage(version="official-futures-daily-v2"))]:
        selected, coverage = validated_daily_inputs(facts, [covered])
        assert selected == []
        assert coverage[0]["status"] == "FAILED"


def test_fact_payload_must_match_coverage_digest():
    fact = {**_fact(), "payload_sha256": "b" * 64}
    selected, coverage = validated_daily_inputs([fact], [_coverage()])
    assert selected == []
    assert coverage[0]["status"] == "FAILED"
