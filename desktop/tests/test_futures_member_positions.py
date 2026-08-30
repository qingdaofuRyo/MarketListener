from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

from market_monitor.futures_member_positions import (
    MemberPositionRank,
    collect_exchange_member_position_ranks,
    normalise_exchange_member_position_ranks,
)
from market_monitor.storage import MarketStore
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
        "CFFEX": "PASS", "DCE": "FAILED", "CZCE": "PASS", "SHFE": "PASS", "GFEX": "PASS",
    }
    assert "ValueError" in next(item.error for item in coverage if item.exchange == "DCE" if item.error)


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
    by_member = {item["memberName"]: item for item in body["rows"]}
    assert by_member["甲"]["netPosition"] == 30
    assert by_member["乙"]["shortPosition"] is None
    assert by_member["乙"]["netPosition"] is None
    assert client.get("/api/futures/member-positions", params={"exchange": "UNKNOWN"}).status_code == 422
