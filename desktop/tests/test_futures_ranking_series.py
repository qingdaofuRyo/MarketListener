from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_monitor.futures_calendar import CALENDAR_PROVIDER, CALENDAR_RELATIVE_PATH, CALENDAR_SCHEMA
from market_monitor.futures_member_rank_source import (
    CFFEX_PUBLISHED_SOURCE,
    CZCE_WORKBOOK_SOURCE,
    DCE_BATCH_SOURCE,
    DCE_DESKTOP_BATCH_SOURCE,
)
from market_monitor.futures_ranking_series import canonical_member, ranking_directory, ranking_series
from market_monitor.storage import MarketStore
from market_monitor.web_api.futures import router


VENUES = ("CFFEX", "SHFE", "INE", "DCE", "CZCE", "GFEX")


def _rank(
    day: str, *, exchange: str = "DCE", contract: str = "A2611", product: str = "A",
    member: str = "甲期货", side: str = "LONG", value: float = 10,
    rank: int = 1, source: str = DCE_BATCH_SOURCE, quality: str = "PASS",
) -> dict[str, object]:
    return {
        "trading_day": day, "exchange": exchange, "contract_code": contract,
        "product_code": product, "side": side, "rank": rank,
        "member_key": member.casefold(), "member_name": member,
        "position": value, "position_change": None, "source": source,
        "collected_at": day + "T08:00:00+00:00", "quality_status": quality,
    }


def _coverage(day: str, venue: str = "DCE", status: str = "PASS", source: str = DCE_BATCH_SOURCE) -> dict[str, object]:
    return {
        "trading_day": day, "exchange": venue, "status": status, "source": source,
        "contract_count": int(status == "PASS"), "record_count": int(status == "PASS"),
        "error": None, "collected_at": day + "T08:00:00+00:00",
        "quality_status": "PASS", "payload_sha256": "synthetic-" + day + "-" + status,
    }


def _complete(day: str) -> list[dict[str, object]]:
    return [
        _coverage(day, venue, "PASS" if venue == "DCE" else "NO_PUBLICATION",
                  DCE_BATCH_SOURCE if venue == "DCE" else "official-synthetic-" + venue)
        for venue in VENUES
    ]


def _save(root: Path, ranks: list[dict[str, object]], coverage: list[dict[str, object]]) -> None:
    store = MarketStore(root)
    try:
        store.upsert_futures_member_position_ranks(ranks)
        store.upsert_futures_member_position_coverage(coverage)
    finally:
        store.close()


def _by_key(payload: dict, metric: str = "gross") -> dict[str, list[float | None]]:
    return {item["key"]: item["values"] for item in payload["metrics"][metric]}


def test_company_aliases_pair_before_contract_net_and_merge_across_venues(tmp_path: Path) -> None:
    day = "2026-09-30"
    _save(tmp_path, [
        _rank(day, member="甲期货（代客）", value=100),
        _rank(day, member="甲期货", value=10, rank=2),
        _rank(day, member="甲期货 (代客)", side="SHORT", value=80),
        _rank(day, member="甲期货", contract="A2701", value=20),
        _rank(day, member="乙期货", value=50, rank=3),
        _rank(day, exchange="CFFEX", contract="IF2610", product="IF",
              source=CFFEX_PUBLISHED_SOURCE, value=30),
        _rank(day, exchange="CFFEX", contract="IF2610", product="IF", side="SHORT",
              source=CFFEX_PUBLISHED_SOURCE, value=10),
    ], [
        _coverage(day), _coverage(day, "CFFEX", source=CFFEX_PUBLISHED_SOURCE),
    ])
    global_payload = ranking_series(tmp_path, view="members")
    assert _by_key(global_payload) == {"甲期货": [250], "乙期货": [50]}
    assert len(global_payload["members"]) == 2
    directory = ranking_directory(tmp_path)
    assert directory["members"] == global_payload["members"]
    assert directory["products"] == global_payload["products"]
    assert "metrics" not in directory and "dates" not in directory
    company = ranking_series(tmp_path, view="member-products", member_key="甲期货（代客）")
    assert _by_key(company) == {"DCE.A": [210], "CFFEX.IF": [40]}
    assert _by_key(company, "longPosition") == {"DCE.A": [130], "CFFEX.IF": [30]}
    assert _by_key(company, "shortPosition") == {"DCE.A": [80], "CFFEX.IF": [10]}
    product = ranking_series(tmp_path, view="product-members", product_key="DCE.A")
    assert _by_key(product, "longPosition") == {"甲期货": [130], "乙期货": [50]}
    assert _by_key(product, "shortPosition") == {"甲期货": [80], "乙期货": [None]}
    assert _by_key(product, "netLongPosition") == {"甲期货": [30], "乙期货": [None]}
    assert _by_key(product, "netShortPosition") == {"甲期货": [0], "乙期货": [None]}
    assert canonical_member(" 甲 期货 ( 代客 ) ") == "甲期货"
    assert canonical_member("甲期货（代客）部门") == "甲期货（代客）部门"
    store = MarketStore(tmp_path)
    try:
        assert store.connection.execute("SELECT count(*) FROM futures_member_position_ranks").fetchone()[0] == 7
        assert "甲期货（代客）" in {row["member_name"] for row in store.list_futures_member_position_ranks()}
    finally:
        store.close()


def test_full_width_space_alias_selects_same_company(tmp_path: Path) -> None:
    day = "2026-09-30"
    _save(tmp_path, [
        _rank(day, member="甲　期货（代客）", side="LONG", value=10),
        _rank(day, member="甲期货 (代客)", side="SHORT", value=8),
    ], [_coverage(day)])
    directory = ranking_series(tmp_path, view="members")
    assert directory["members"][0]["key"] == "甲期货"
    selected = ranking_series(tmp_path, view="member-products", member_key=directory["members"][0]["key"])
    assert _by_key(selected) == {"DCE.A": [18]}


def test_complete_source_beats_preferred_partial_and_preferred_complete_beats_other(tmp_path: Path) -> None:
    day1, day2 = "2026-09-29", "2026-09-30"
    old = "official-dce-member-ranking"
    _save(tmp_path, [
        _rank(day1, value=1000), _rank(day1, value=10, source=old),
        _rank(day2, value=20), _rank(day2, value=2000, source=old),
        _rank(day2, value=5000, source="akshare-dce-member-ranking"),
        _rank(day2, value=9000, member="未验证", rank=2, quality="FAILED"),
    ], [
        _coverage(day1, status="PARTIAL"), _coverage(day1, source=old),
        _coverage(day2), _coverage(day2, source=old),
        _coverage(day2, source="akshare-dce-member-ranking"),
    ])
    payload = ranking_series(tmp_path, view="members")
    assert _by_key(payload) == {"甲期货": [10, 20]}


def test_desktop_batch_source_is_selected_once_and_android_batch_takes_priority(tmp_path: Path) -> None:
    day = "2026-09-30"
    old = "official-dce-member-ranking"
    _save(tmp_path, [
        _rank(day, value=10, source=old),
        _rank(day, value=20, source=DCE_DESKTOP_BATCH_SOURCE),
    ], [
        _coverage(day, source=old),
        _coverage(day, source=DCE_DESKTOP_BATCH_SOURCE),
    ])
    assert _by_key(ranking_series(tmp_path, view="members")) == {"甲期货": [20]}

    _save(tmp_path, [_rank(day, value=30, source=DCE_BATCH_SOURCE)],
          [_coverage(day, source=DCE_BATCH_SOURCE)])
    assert _by_key(ranking_series(tmp_path, view="members")) == {"甲期货": [30]}


def test_latest_complete_baseline_and_metric_orders_survive_range_switch(tmp_path: Path) -> None:
    old, complete, latest = "2023-01-03", "2026-09-29", "2026-09-30"
    _save(tmp_path, [
        _rank(old, member="历史公司", value=50, contract="A2305"),
        _rank(complete, member="甲期货", value=50),
        _rank(complete, member="甲期货", side="SHORT", value=5),
        _rank(complete, member="乙期货", value=10, rank=2),
        _rank(complete, member="乙期货", side="SHORT", value=60, rank=2),
        _rank(latest, member="甲期货", value=1000),
        _rank(latest, member="乙期货", value=1, rank=2),
    ], [*_complete(old), *_complete(complete), _coverage(latest, status="PARTIAL")])
    all_payload = ranking_series(tmp_path, view="product-members", product_key="DCE.A", range_name="all")
    year_payload = ranking_series(tmp_path, view="product-members", product_key="DCE.A", range_name="1y")
    assert all_payload["baselineDay"] == year_payload["baselineDay"] == complete
    assert [row["key"] for row in year_payload["metrics"]["longPosition"]] == ["甲期货", "乙期货"]
    assert [row["key"] for row in year_payload["metrics"]["shortPosition"]] == ["乙期货", "甲期货"]
    assert [row["key"] for row in year_payload["metrics"]["netLongPosition"]] == ["甲期货", "乙期货"]
    assert [row["key"] for row in year_payload["metrics"]["netShortPosition"]] == ["乙期货", "甲期货"]
    assert all_payload["metrics"]["gross"][-1]["key"] == "历史公司"
    assert "历史公司" in {row["key"] for row in year_payload["members"]}
    assert next(row for row in year_payload["members"] if row["key"] == "历史公司")["position"] is None
    assert year_payload["coverage"][-1]["missingExchanges"] == sorted(VENUES)


def test_calendar_missing_days_and_no_publication_stay_null(tmp_path: Path) -> None:
    first, missing, empty, latest = "2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30"
    _save(tmp_path, [_rank(first), _rank(latest, value=20)], [
        *_complete(first),
        *[_coverage(empty, venue, "NO_PUBLICATION", "official-synthetic-" + venue) for venue in VENUES],
        *_complete(latest),
    ])
    path = tmp_path / CALENDAR_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": CALENDAR_SCHEMA, "provider": CALENDAR_PROVIDER, "market": "CN_FUTURE",
        "retrievedAt": "2026-10-01T00:00:00+00:00", "tradingDays": [first, missing, empty, latest],
    }), encoding="utf-8")
    payload = ranking_series(tmp_path, view="members")
    assert payload["dates"] == [first, missing, empty, latest]
    assert _by_key(payload) == {"甲期货": [10, None, None, 20]}
    assert payload["coverage"][1]["missingExchanges"] == sorted(VENUES)
    assert payload["coverage"][2]["missingExchanges"] == []


def test_czce_legacy_delivery_and_dce_monthly_contract_are_kept(tmp_path: Path) -> None:
    day = "2026-09-30"
    _save(tmp_path, [
        _rank(day, exchange="CZCE", contract="TA701", product="TA", source=CZCE_WORKBOOK_SOURCE, value=30),
        _rank(day, exchange="CZCE", contract="TA609", product="TA", source=CZCE_WORKBOOK_SOURCE, value=5),
        _rank(day, exchange="CZCE", contract="TA608", product="TA", source=CZCE_WORKBOOK_SOURCE, value=9000),
        _rank(day, exchange="CZCE", contract="TA613", product="TA", source=CZCE_WORKBOOK_SOURCE, value=9000),
        _rank(day, contract="A2608", value=9000),
        _rank(day, contract="L-F2610", product="L-F", value=40),
    ], [_coverage(day), _coverage(day, "CZCE", source=CZCE_WORKBOOK_SOURCE)])
    assert _by_key(ranking_series(tmp_path, view="members")) == {"甲期货": [75]}
    czce = ranking_series(tmp_path, view="product-members", product_key="CZCE.TA", contract_code="TA701")
    assert _by_key(czce) == {"甲期货": [30]}
    monthly = ranking_series(tmp_path, view="product-members", product_key="DCE.L-F", contract_code="L-F2610")
    assert _by_key(monthly) == {"甲期货": [40]}


def test_cache_changes_when_official_daily_payload_changes(tmp_path: Path) -> None:
    day = "2026-09-30"
    _save(tmp_path, [_rank(day)], [_coverage(day)])
    initial = ranking_series(tmp_path, view="members")
    changed_coverage = {**_coverage(day), "payload_sha256": "synthetic-new-payload"}
    _save(tmp_path, [_rank(day, value=20)], [changed_coverage])
    updated = ranking_series(tmp_path, view="members")
    assert initial["dataVersion"] != updated["dataVersion"]
    assert _by_key(updated) == {"甲期货": [20]}


@pytest.mark.parametrize("params", [
    {"view": "unknown"}, {"range": "2y"}, {"view": "member-products"},
    {"view": "product-members"}, {"view": "product-members", "product_key": "SHFE.BAD?"},
    {"view": "product-members", "product_key": "DCE.A", "contract_code": "A701"},
    {"start_day": "bad"}, {"start_day": "2026-10-01", "end_day": "2026-09-30"},
])
def test_ranking_router_rejects_invalid_filters_without_starting_services(tmp_path: Path, params: dict) -> None:
    _save(tmp_path, [_rank("2026-09-30")], [_coverage("2026-09-30")])
    app = FastAPI()
    app.state.data_root = tmp_path
    app.include_router(router)
    response = TestClient(app).get("/api/futures/ranking-series", params=params)
    assert response.status_code == 422


def test_ranking_router_returns_compact_matrices(tmp_path: Path) -> None:
    _save(tmp_path, [_rank("2026-09-30")], [_coverage("2026-09-30")])
    app = FastAPI()
    app.state.data_root = tmp_path
    app.include_router(router)
    response = TestClient(app).get("/api/futures/ranking-series", params={"view": "members"})
    assert response.status_code == 200
    assert _by_key(response.json()) == {"甲期货": [10]}
    assert "rows" not in response.json()
