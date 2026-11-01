from datetime import date

from market_monitor.tdx_cn_5m_time_audit import _expected_labels, audit_day

INSTRUMENT = "CN.SSE.STOCK.600519.TDX_LOCAL"


def sample():
    labels = _expected_labels(date(2026, 10, 30), 5)
    local, five, thirty = [], [], []
    for index, label in enumerate(labels):
        value = 10 + index / 10
        local.append({"instrument_id": INSTRUMENT, "bar_open_time": f"2026-10-30T{label[-4:-2]}:{label[-2:]}:00+08:00",
                      "source": "通达信金融终端（本地）", "period": "5m", "quality_status": "PASS",
                      "normalization_version": "tdx-cn-v2", "open": value, "close": value + .05,
                      "high": value + .1, "low": value - .1})
        five.append([label, value, value + .05, value + .1, value - .1])
    for offset in range(0, 48, 6):
        group = five[offset:offset + 6]
        thirty.append([group[-1][0], group[0][1], group[-1][2], max(row[3] for row in group),
                       min(row[4] for row in group)])
    return local, five, thirty


def test_complete_sample_supports_end_labels_but_does_not_allow_automatic_correction():
    local, five, thirty = sample()
    result = audit_day("2026-10-30", local, five, thirty, instrument_id=INSTRUMENT)
    assert result["status"] == "CONSISTENT_SAMPLE"
    assert result["local5mOhlcMatches"] == 48
    assert result["remoteEndLabelBucketsMatched"] == 8
    assert result["remoteStartLabelBucketsCompared"] == 6
    assert result["remoteStartLabelBucketsMatched"] == 0
    assert result["localEndLabelBucketsMatched"] == 8
    assert result["automaticCorrectionAllowed"] is False


def test_missing_duplicate_or_wrong_source_remains_incomplete():
    local, five, thirty = sample()
    assert audit_day("2026-10-30", local[:-1], five, thirty, instrument_id=INSTRUMENT)["status"] == "INCOMPLETE"
    assert audit_day("2026-10-30", local, five + five[:1], thirty, instrument_id=INSTRUMENT)["status"] == "INCOMPLETE"
    assert audit_day("2026-10-30", local, five + [[five[0][0]]], thirty, instrument_id=INSTRUMENT)["status"] == "INCOMPLETE"
    local[0]["source"] = "other"
    assert audit_day("2026-10-30", local, five, thirty, instrument_id=INSTRUMENT)["status"] == "INCOMPLETE"


def test_bad_time_zone_or_untrusted_quality_cannot_pass():
    local, five, thirty = sample()
    local[0]["bar_open_time"] = local[0]["bar_open_time"].replace("+08:00", "+00:00")
    assert audit_day("2026-10-30", local, five, thirty, instrument_id=INSTRUMENT)["status"] == "INCOMPLETE"
    local, five, thirty = sample()
    local[0]["quality_status"] = "QUARANTINED"
    assert audit_day("2026-10-30", local, five, thirty, instrument_id=INSTRUMENT)["status"] == "INCOMPLETE"


def test_price_disagreement_and_wrong_30m_bucket_are_not_approved():
    local, five, thirty = sample()
    local[0]["close"] += .02
    assert audit_day("2026-10-30", local, five, thirty, instrument_id=INSTRUMENT)["status"] == "MISMATCH"
    local, five, thirty = sample()
    thirty[-1][2] -= .02
    result = audit_day("2026-10-30", local, five, thirty, instrument_id=INSTRUMENT)
    assert result["status"] == "MISMATCH"
    assert result["remoteEndLabelBucketsMatched"] == 7
