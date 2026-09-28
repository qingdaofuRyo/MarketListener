from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from market_monitor.collector import _collect_oi_leaderboard
from market_monitor.futures_member_positions import (
    MemberPositionRank,
    collect_exchange_member_position_ranks,
    normalise_exchange_member_position_ranks,
)
from market_monitor.storage import MarketStore
from market_monitor.futures_official_member_sources import DirectOfficialRankApi
from market_monitor.web_app import create_web_app


@dataclass
class _Frame:
    records: list[dict[str, object]]

    def to_dict(self, *, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return self.records


def _record(*, rank: int = 1, long: str = "多方甲", short: str = "空方乙") -> dict[str, object]:
    return {
        "rank": rank,
        "long_party_name": long,
        "long_open_interest": 120,
        "long_open_interest_chg": 7,
        "short_party_name": short,
        "short_open_interest": 90,
        "short_open_interest_chg": -3,
    }


def _rank(
    *, exchange: str = "SHFE", contract: str = "RB2610", side: str = "LONG", member: str = "甲",
    position: float = 10.0, rank: int = 1,
) -> dict[str, object]:
    return MemberPositionRank(
        trading_day="2026-08-27",
        exchange=exchange,
        contract_code=contract,
        product_code="".join(character for character in contract if character.isalpha()),
        side=side,
        rank=rank,
        member_key=member.casefold(),
        member_name=member,
        position=position,
        position_change=1.0,
        source=f"akshare-{exchange.lower()}-member-ranking",
        collected_at="2026-08-27T08:00:00+00:00",
    ).to_dict()


def _coverage(
    exchange: str,
    status: str,
    *,
    contract_count: int = 0,
    record_count: int = 0,
    error: str | None = None,
) -> dict[str, object]:
    return {
        "trading_day": "2026-08-27",
        "exchange": exchange,
        "status": status,
        "contract_count": contract_count,
        "record_count": record_count,
        "source": f"fixture-{exchange.lower()}-member-ranking",
        "error": error,
        "collected_at": "2026-08-27T08:00:00+00:00",
    }


def test_normalise_keeps_separate_direction_coverage_and_maps_ine() -> None:
    rows = normalise_exchange_member_position_ranks(
        {
            "rb2610": _Frame([_record(), _record(rank=21, long="合计", short="总计")]),
            "sc2610": _Frame([_record(long="能源多", short="能源空")]),
        },
        exchange="SHFE",
        trading_day="2026-08-27",
        source="fixture",
        collected_at="2026-08-27T08:00:00+00:00",
    )

    assert [(row.exchange, row.contract_code, row.side, row.member_name) for row in rows] == [
        ("INE", "SC2610", "LONG", "能源多"),
        ("INE", "SC2610", "SHORT", "能源空"),
        ("SHFE", "RB2610", "LONG", "多方甲"),
        ("SHFE", "RB2610", "SHORT", "空方乙"),
    ]
    assert all(row.rank == 1 for row in rows)


def test_collects_each_exchange_independently_when_dce_fails() -> None:
    class Api:
        def get_cffex_rank_table(self, *, date: str):
            assert date == "20260827"
            return {"IF2609": _Frame([_record()])}

        def futures_dce_position_rank(self, *, date: str):
            raise ValueError("not a zip")

        def get_rank_table_czce(self, *, date: str):
            return {"AP2610": _Frame([_record()])}

        def get_shfe_rank_table(self, *, date: str):
            return {"RB2610": _Frame([_record()])}

        def futures_gfex_position_rank(self, *, date: str):
            return {"SI2610": _Frame([_record()])}

    rows, coverage = collect_exchange_member_position_ranks(
        Api(), day_compact="20260827", trading_day="2026-08-27", collected_at="2026-08-27T08:00:00+00:00"
    )

    assert len(rows) == 8
    assert {item.exchange: item.status for item in coverage} == {
        "CFFEX": "PASS", "DCE": "FAILED", "CZCE": "PASS", "SHFE": "PASS", "INE": "FAILED", "GFEX": "PASS",
    }
    assert "ValueError" in next(item.error for item in coverage if item.exchange == "DCE" if item.error)


def test_oi_collection_persists_each_exchange_coverage_even_when_one_source_fails(monkeypatch) -> None:
    class Api:
        raw_artifacts: list[dict[str, object]] = []

        def evidence_for(self, exchange: str) -> dict[str, object]:
            return {
                "publisher": exchange, "source_url": f"https://example.invalid/{exchange}",
                "adapter": f"official-{exchange.lower()}-rank-v1",
                "retrieved_at": "2026-08-27T08:00:00+00:00",
                "payload_sha256": "synthetic-fixture", "quality_status": "PASS",
                "legacy_adapter": None,
            }

        def get_cffex_rank_table(self, *, date: str):
            assert date == "20260827"
            return {"IF2609": _Frame([_record()])}

        def futures_dce_position_rank(self, *, date: str):
            raise ValueError("not a zip")

        def get_rank_table_czce(self, *, date: str):
            return {"AP2610": _Frame([_record()])}

        def get_shfe_rank_table(self, *, date: str):
            return {"RB2610": _Frame([_record()]), "SC2610": _Frame([_record()])}

        def futures_gfex_position_rank(self, *, date: str):
            return {"SI2610": _Frame([_record()])}

    monkeypatch.setattr("market_monitor.collector._last_trading_day_compact", lambda: "20260827")
    monkeypatch.setattr("market_monitor.collector._now", lambda: "2026-08-27T08:00:00+00:00")

    result = _collect_oi_leaderboard(Api())

    coverage = {item["exchange"]: item for item in result.persist.member_position_coverage}
    assert result.status == "PARTIAL_FAILURE"
    assert coverage["DCE"]["status"] == "FAILED"
    assert coverage["DCE"]["error"] == "ValueError: not a zip"
    assert coverage["INE"]["status"] == "PASS"
    assert coverage["INE"]["contract_count"] == 1
    assert coverage["CFFEX"]["status"] == "PASS"
    assert coverage["SHFE"]["publisher"] == "SHFE"
    assert coverage["SHFE"]["quality_status"] == "PASS"
    assert coverage["SHFE"]["payload_sha256"] == "synthetic-fixture"
    assert all(item["source"].startswith("official-") for item in result.persist.member_position_ranks)
    assert {item["trading_day"] for item in coverage.values()} == {"2026-08-27"}


def test_direct_rank_http_failure_keeps_status_and_payload_hash_without_archiving_challenge() -> None:
    challenge = b"<html>temporary verification page</html>"

    class Response:
        content = challenge
        status_code = 412
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def raise_for_status(self) -> None:
            raise RuntimeError("412 Precondition Failed")

    class Session:
        def request(self, *args: object, **kwargs: object) -> Response:
            assert kwargs["timeout"] == 15
            return Response()

    api = DirectOfficialRankApi(session=Session())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="412"):
        api.futures_dce_position_rank(date="20260827")
    evidence = api.evidence_for("DCE")
    assert evidence["http_status"] == 412
    assert evidence["response_content_type"] == "text/html; charset=utf-8"
    assert evidence["payload_sha256"] == sha256(challenge).hexdigest()
    assert evidence["quality_status"] == "FAILED"
    assert api.raw_artifacts == []


def test_member_position_api_preserves_unpublished_direction_as_null(tmp_path: Path) -> None:
    root = tmp_path / "data"
    store = MarketStore(root)
    try:
        store.upsert_futures_member_position_ranks(
            [
                _rank(member="甲", side="LONG", position=120),
                _rank(member="甲", side="SHORT", position=90),
                _rank(member="乙", side="LONG", position=50, rank=2),
                _rank(exchange="CFFEX", contract="IF2609", member="金融", side="LONG"),
            ]
        )
        store.upsert_futures_member_position_coverage(
            [
                _coverage("SHFE", "PASS", contract_count=1, record_count=3),
                _coverage("DCE", "FAILED", error="fixture DCE unavailable"),
                _coverage("CZCE", "FAILED", error="fixture CZCE unavailable"),
                _coverage("INE", "FAILED", error="fixture INE unavailable"),
                _coverage("GFEX", "FAILED", error="fixture GFEX unavailable"),
                _coverage("CFFEX", "PASS", contract_count=1, record_count=1),
            ]
        )
    finally:
        store.close()

    client = TestClient(create_web_app(root), client=("127.0.0.1", 50000))
    response = client.get("/api/futures/member-positions", params={"contract_code": "RB2610"})

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["coverage"]["exchanges"] == ["SHFE"]
    assert body["coverage"]["missingExchanges"] == ["CZCE", "DCE", "GFEX", "INE"]
    assert body["coverage"]["isComplete"] is False
    assert body["coverage"]["dataQualityStatus"] == "PARTIAL"
    exchange_coverage = {item["exchange"]: item for item in body["coverage"]["exchangeCoverage"]}
    assert exchange_coverage["SHFE"]["status"] == "PASS"
    assert exchange_coverage["SHFE"]["qualityStatus"] == "UNVERIFIED"
    assert exchange_coverage["DCE"]["error"] == "fixture DCE unavailable"
    by_member = {item["memberName"]: item for item in body["rows"]}
    assert by_member["甲"]["netPosition"] == 30
    assert by_member["甲"]["publishers"] == ["SHFE"]
    assert by_member["甲"]["qualityStatuses"] == ["LEGACY_WRAPPED"]
    assert by_member["甲"]["legacyAdapters"] == ["akshare-shfe-member-ranking"]
    assert by_member["乙"]["shortPosition"] is None
    assert by_member["乙"]["netPosition"] is None
    assert client.get("/api/futures/member-positions", params={"exchange": "INE"}).status_code == 200
    assert client.get("/api/futures/member-positions", params={"exchange": "UNKNOWN"}).status_code == 422


def test_member_position_api_lists_products_by_exchange_scoped_member(tmp_path: Path) -> None:
    root = tmp_path / "data"
    store = MarketStore(root)
    try:
        store.upsert_futures_member_position_ranks([
            _rank(exchange="SHFE", contract="RB2610", side="LONG", member="甲", position=120),
            _rank(exchange="SHFE", contract="RB2610", side="SHORT", member="甲", position=90),
            _rank(exchange="SHFE", contract="HC2610", side="LONG", member="甲", position=50),
            _rank(exchange="DCE", contract="JM2610", side="LONG", member="甲", position=70),
        ])
    finally:
        store.close()
    client = TestClient(create_web_app(root), client=("127.0.0.1", 50000))
    overview = client.get("/api/futures/member-positions").json()
    assert overview["rows"] == []
    assert {(item["exchange"], item["memberKey"]) for item in overview["members"]} == {
        ("SHFE", "甲"), ("DCE", "甲"),
    }
    assert client.get("/api/futures/member-positions", params={"member_key": "甲"}).status_code == 422
    shfe = client.get(
        "/api/futures/member-positions", params={"exchange": "SHFE", "member_key": "甲"}
    ).json()
    assert shfe["filters"]["memberKey"] == "甲"
    assert {(row["productCode"], row["contractCode"]) for row in shfe["rows"]} == {
        ("RB", "RB2610"), ("HC", "HC2610"),
    }
    assert next(row for row in shfe["rows"] if row["productCode"] == "HC")["shortPosition"] is None
    dce = client.get(
        "/api/futures/member-positions", params={"exchange": "DCE", "member_key": "甲"}
    ).json()
    assert [row["productCode"] for row in dce["rows"]] == ["JM"]


def test_member_position_api_exposes_a_failed_coverage_run_without_fake_rank_rows(tmp_path: Path) -> None:
    root = tmp_path / "data"
    store = MarketStore(root)
    try:
        store.upsert_futures_member_position_coverage(
            [
                _coverage(exchange, "FAILED", error=f"fixture {exchange} unavailable")
                for exchange in ("DCE", "CZCE", "SHFE", "INE", "GFEX")
            ]
        )
    finally:
        store.close()

    client = TestClient(create_web_app(root), client=("127.0.0.1", 50000))
    body = client.get("/api/futures/member-positions").json()

    assert body["available"] is False
    assert body["tradingDay"] == "2026-08-27"
    assert body["coverage"]["publishedDirectionRankCount"] == 0
    assert body["coverage"]["missingExchanges"] == ["CZCE", "DCE", "GFEX", "INE", "SHFE"]
    assert body["coverage"]["dataQualityStatus"] == "PARTIAL"
    assert body["coverage"]["exchangeCoverage"][0]["error"].startswith("fixture")


def test_member_position_api_exposes_last_published_day_after_newer_failure(tmp_path: Path) -> None:
    root = tmp_path / "data"
    store = MarketStore(root)
    try:
        store.upsert_futures_member_position_ranks([_rank(member="甲", contract="RB2610")])
        store.upsert_futures_member_position_coverage([
            {**_coverage(exchange, "FAILED", error="fixture unavailable"), "trading_day": "2026-08-28"}
            for exchange in ("DCE", "CZCE", "SHFE", "INE", "GFEX")
        ])
    finally:
        store.close()
    client = TestClient(create_web_app(root), client=("127.0.0.1", 50000))
    latest = client.get("/api/futures/member-positions").json()
    assert latest["available"] is False
    assert latest["tradingDay"] == "2026-08-28"
    assert latest["latestPublishedDay"] == "2026-08-27"
    historical = client.get(
        "/api/futures/member-positions", params={"trading_day": "2026-08-27", "product_code": "RB"}
    ).json()
    assert historical["rows"][0]["memberName"] == "甲"


def test_member_position_history_keeps_failed_days_and_unpublished_net_null(tmp_path: Path) -> None:
    root = tmp_path / "data"
    store = MarketStore(root)
    try:
        store.upsert_futures_member_position_ranks([
            _rank(contract="RB2610", member="甲", side="LONG", position=120),
            _rank(contract="RB2610", member="甲", side="SHORT", position=90),
            _rank(contract="HC2610", member="甲", side="LONG", position=50),
            {**_rank(contract="RB2610", member="甲", side="LONG", position=130), "trading_day": "2026-08-28"},
        ])
        store.upsert_futures_member_position_coverage([
            _coverage("SHFE", "PASS"),
            {**_coverage("SHFE", "PASS"), "trading_day": "2026-08-28"},
            {**_coverage("SHFE", "FAILED", error="fixture unavailable"), "trading_day": "2026-08-31"},
        ])
    finally:
        store.close()
    client = TestClient(create_web_app(root), client=("127.0.0.1", 50000))
    member = client.get(
        "/api/futures/member-positions/history", params={"exchange": "SHFE", "member_key": "甲"}
    )
    assert member.status_code == 200
    body = member.json()
    assert body["dates"] == ["2026-08-27", "2026-08-28", "2026-08-31"]
    assert body["sourceStatusByDay"]["2026-08-31"] == "FAILED"
    first = next(row for row in body["rows"] if row["tradingDay"] == "2026-08-27" and row["productCode"] == "RB")
    assert first["productName"] == "螺纹钢"
    assert first["netLongPosition"] == 30
    next_day = next(row for row in body["rows"] if row["tradingDay"] == "2026-08-28")
    assert next_day["shortPosition"] is None
    assert next_day["netLongPosition"] is None
    product = client.get(
        "/api/futures/member-positions/history", params={"exchange": "SHFE", "product_code": "RB"}
    ).json()
    assert {row["productCode"] for row in product["rows"]} == {"RB"}
    assert client.get("/api/futures/member-positions/history", params={"exchange": "SHFE"}).status_code == 422
    assert client.get(
        "/api/futures/member-positions/history",
        params={"exchange": "SHFE", "product_code": "RB", "member_key": "甲"},
    ).status_code == 422


def test_cross_exchange_overview_selects_largest_product_and_exact_member(tmp_path: Path) -> None:
    root = tmp_path / "data"
    store = MarketStore(root)
    try:
        store.upsert_futures_member_position_ranks([
            _rank(exchange="SHFE", contract="RB2610", side="LONG", member="甲", position=100),
            _rank(exchange="SHFE", contract="RB2610", side="SHORT", member="甲", position=80),
            _rank(exchange="DCE", contract="JM2610", side="LONG", member="甲", position=90),
            _rank(exchange="DCE", contract="JM2610", side="SHORT", member="甲", position=70),
            _rank(exchange="SHFE", contract="HC2610", side="LONG", member="乙", position=300),
        ])
        store.upsert_futures_member_position_coverage([
            _coverage("SHFE", "PASS"), _coverage("DCE", "PASS"),
        ])
    finally:
        store.close()
    client = TestClient(create_web_app(root), client=("127.0.0.1", 50000))
    overview = client.get("/api/futures/member-positions/overview").json()
    assert overview["tradingDay"] == "2026-08-27"
    assert overview["defaultProductKey"] == "SHFE.HC"
    assert overview["products"][0]["displayName"] == "热卷（HC）"
    assert overview["defaultMemberKey"] == "甲"
    assert overview["members"][0]["grossPublishedPosition"] == 340
    assert overview["members"][0]["exchanges"] == ["DCE", "SHFE"]

    member_history = client.get(
        "/api/futures/member-positions/history", params={"member_key": "甲"}
    ).json()
    assert {(row["exchange"], row["productKey"]) for row in member_history["rows"]} == {
        ("SHFE", "SHFE.RB"), ("DCE", "DCE.JM"),
    }
    assert {row["productDisplayName"] for row in member_history["rows"]} == {
        "螺纹钢（RB）", "焦煤（JM）",
    }
    product_history = client.get(
        "/api/futures/member-positions/history", params={"product_key": "DCE.JM"}
    ).json()
    assert product_history["productKey"] == "DCE.JM"
    assert {row["exchange"] for row in product_history["rows"]} == {"DCE"}
    product_day = client.get(
        "/api/futures/member-positions",
        params={"trading_day": "2026-08-27", "product_key": "DCE.JM"},
    )
    assert product_day.status_code == 200
    assert product_day.json()["filters"]["exchange"] == "DCE"
    assert product_day.json()["filters"]["productCode"] == "JM"
    assert {row["exchange"] for row in product_day.json()["rows"]} == {"DCE"}
    assert client.get(
        "/api/futures/member-positions",
        params={"product_key": "DCE.JM", "exchange": "SHFE"},
    ).status_code == 422
    assert client.get(
        "/api/futures/member-positions/history", params={"product_key": "JM"}
    ).status_code == 422
