import json

import pytest

from market_monitor.futures_calendar import CALENDAR_PROVIDER, CALENDAR_SCHEMA
from market_monitor.gfex_rank_backfill import (
    CONTRACTS, RANKS, VARIETIES, GfexSourceError, capture,
    parse_contracts, parse_varieties, validate_rank_rows,
)


def test_lists_do_not_guess_contracts_or_turn_failed_lists_into_empty():
    assert parse_varieties([{"varietyId": "si"}, {"varietyId": "lc"}]) == ["lc", "si"]
    assert parse_contracts([], "pt") == []
    assert parse_contracts(["si2501", {"contractId": "si2502"}], "si") == ["si2501", "si2502"]
    with pytest.raises(ValueError):
        parse_varieties([])
    with pytest.raises(ValueError):
        parse_contracts(["lc2501"], "si")
    with pytest.raises(ValueError):
        parse_contracts(["si2501", "si2501"], "si")


def test_rank_contract_id_is_rank_and_zero_quantities_are_valid():
    rows = [{"contractId": "1", "abbr": "合成会员", "todayQty": 0, "qtySub": -2},
            {"contractId": "合计", "abbr": "合计", "todayQty": 0}]
    assert validate_rank_rows(rows) == {"publishedRows": 1, "summaryRows": 1, "status": "PASS"}
    assert validate_rank_rows([])["status"] == "NO_PUBLISHED_ROWS"
    with pytest.raises(ValueError):
        validate_rank_rows([rows[0], rows[0]])
    with pytest.raises(ValueError):
        validate_rank_rows([{"contractId": "1", "abbr": "合成会员", "todayQty": float("nan")}])


def test_direction_checkpoints_resume_and_capture_never_creates_catalog(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "cn_futures_trading_calendar.json").write_text(json.dumps({
        "schema": CALENDAR_SCHEMA, "provider": CALENDAR_PROVIDER, "market": "CN_FUTURE",
        "retrievedAt": "2026-10-02T00:00:00+08:00", "tradingDays": ["2025-01-02"],
    }), encoding="utf-8")

    class Client:
        fail = True

        def __init__(self):
            self.calls = []

        def fetch(self, endpoint, body):
            self.calls.append((endpoint, body.get("data_type")))
            if endpoint == VARIETIES:
                rows = [{"varietyId": "si"}, {"varietyId": "pt"}]
            elif endpoint == CONTRACTS:
                rows = ["si2505"] if body["variety"] == "si" else []
            else:
                if body["data_type"] == "2" and self.fail:
                    raise GfexSourceError(200, None, "non-JSON response; body not archived")
                rows = [{"contractId": "1", "abbr": "合成会员", "todayQty": 10, "qtySub": 2}]
            return rows, {"payloadSha256": "a" * 64, "sourceUrl": "http://www.gfex.com.cn/test"}

    client = Client()
    first = capture(tmp_path, client, "2025-01-01", "2025-01-02")
    assert first["status"] == "FAILED"
    assert first["directionRequestsThisRun"] == 1
    assert not (tmp_path / "catalog.duckdb").exists()
    client.calls.clear()
    client.fail = False
    second = capture(tmp_path, client, "2025-01-01", "2025-01-02")
    assert second["status"] == "PASS_RAW"
    assert client.calls == [(VARIETIES, None), (RANKS, "2"), (RANKS, "3")]
    checkpoint = json.loads((state / "gfex_rank_capture" / "2025-01-02.json").read_text(encoding="utf-8"))
    assert checkpoint["contractLists"]["pt"]["status"] == "NO_PUBLISHED_CONTRACTS"
    assert len(checkpoint["directions"]) == 3
    client.calls.clear()
    assert capture(tmp_path, client, "2025-01-01", "2025-01-02")["completedDays"] == 1
    assert client.calls == [(VARIETIES, None)]
