from __future__ import annotations

import json
from pathlib import Path

from market_monitor.external_market import (
    ExternalMarketSeries,
    VIX_INSTRUMENT_ID,
    VIX_LOCAL_SERIES_PATH,
    align_external_series,
    load_vix_series,
)


def _write_vix(data_root: Path, payload: dict[str, object]) -> None:
    path = data_root / VIX_LOCAL_SERIES_PATH
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _verified_vix_payload() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "externalInstrumentId": VIX_INSTRUMENT_ID,
        "source": "fixture: verified VIX standard series",
        "sourceStatus": "PASS",
        "asOfDate": "2026-08-08",
        "points": [
            {"tradingDate": "2026-08-06", "value": 19.25},
            {"tradingDate": "2026-08-08", "value": 17.5},
        ],
    }


def test_vix_requires_a_local_pass_standard_series(tmp_path: Path) -> None:
    assert load_vix_series(tmp_path).reason == "缺少本地 VIX 标准序列"

    payload = _verified_vix_payload()
    payload["sourceStatus"] = "FAILED"
    _write_vix(tmp_path, payload)

    result = load_vix_series(tmp_path)

    assert result.status == "unavailable"
    assert result.points == ()
    assert result.reason == "本地 VIX 序列没有 PASS 来源证据"


def test_vix_aligns_exact_trading_days_without_forward_fill_or_synthesis(tmp_path: Path) -> None:
    _write_vix(tmp_path, _verified_vix_payload())
    series = load_vix_series(tmp_path)
    bars = [
        {"tradingDate": "2026-08-06", "close": 100.0},
        {"tradingDate": "2026-08-07", "close": 9000.0},
        {"barOpenTime": "2026-08-08T00:30:00-05:00", "close": 1.0},
    ]

    aligned = align_external_series(bars, series)

    assert aligned.status == "ready"
    assert aligned.values == (19.25, None, 17.5)
    assert aligned.to_public_dict()["points"] == [
        {"barIndex": 0, "tradingDate": "2026-08-06", "value": 19.25},
        {"barIndex": 2, "tradingDate": "2026-08-08", "value": 17.5},
    ]
    assert aligned.coverage == {
        "inputBarCount": 3,
        "matchedBarCount": 2,
        "sourcePointCount": 2,
        "firstInputDate": "2026-08-06",
        "lastInputDate": "2026-08-08",
    }


def test_vix_ready_source_with_no_current_market_dates_is_unavailable_without_fake_number() -> None:
    series = ExternalMarketSeries(
        VIX_INSTRUMENT_ID,
        "fixture: verified VIX standard series",
        "2026-08-08",
        (("2026-08-08", 17.5),),
        "ready",
    )

    aligned = align_external_series([{"tradingDate": "2026-08-09", "close": 99999.0}], series)

    assert aligned.status == "unavailable"
    assert aligned.values == (None,)
    assert aligned.reason == "当前图表区间没有可对齐的 VIX 交易日"
