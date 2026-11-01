"""GFEX Bronze integrity and source-isolated import using synthetic responses."""

from __future__ import annotations

import base64
from hashlib import sha256
import json
from pathlib import Path

import pytest

from market_monitor.futures_calendar import CALENDAR_PROVIDER, CALENDAR_SCHEMA
from market_monitor.gfex_rank_backfill import BASE, CONTRACTS, RANKS, VARIETIES
from market_monitor.gfex_rank_import import (
    SOURCE, import_gfex_rank_bronze, prepare_gfex_rank_day,
)
from market_monitor.storage import MarketStore


EARLY = "2023-01-03"
RANKED = "2023-11-10"


def _proof(path: Path, endpoint: str, body: dict[str, str], rows: list,
           validation: dict | None = None) -> str:
    payload = {"code": "0", "data": rows, "param": {key: [value] for key, value in body.items()}}
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    digest = sha256(raw).hexdigest()
    proof = {
        "publisher": "GFEX", "adapter": "official-gfex-rank-raw-v1",
        "sourceUrl": BASE + endpoint, "requestBody": body,
        "retrievedAt": "2026-10-03T00:00:00+08:00",
        "payloadSha256": digest, "contentType": "application/json;charset=UTF-8",
        "httpStatus": 200, "apiCode": "0",
        "rawBase64": base64.b64encode(raw).decode("ascii"),
    }
    if validation is not None:
        proof.update(validation=validation, qualityStatus=validation["status"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proof), encoding="utf-8")
    return digest


def _fixture(root: Path) -> None:
    calendar = root / "state" / "cn_futures_trading_calendar.json"
    calendar.parent.mkdir(parents=True)
    calendar.write_text(json.dumps({
        "schema": CALENDAR_SCHEMA, "provider": CALENDAR_PROVIDER,
        "market": "CN_FUTURE", "retrievedAt": "2026-10-03T00:00:00+08:00",
        "tradingDays": [EARLY, RANKED],
    }), encoding="utf-8")
    bronze = root / "bronze" / "gfex_rank_capture"
    _proof(bronze / "varieties-latest.json", VARIETIES, {}, [{"varietyId": "si"}])
    for day, contracts in ((EARLY, []), (RANKED, ["si2401"])):
        compact = day.replace("-", "")
        list_digest = _proof(
            bronze / day / "si-contracts.json", CONTRACTS,
            {"variety": "si", "trade_date": compact}, contracts,
        )
        directions: dict[str, dict] = {}
        for direction in (1, 2, 3) if contracts else ():
            key = f"si2401.{direction}"
            rows = [
                {"contractId": "1", "abbr": "合成期货", "todayQty": 12, "qtySub": -2},
                {"contractId": "总计", "abbr": "", "todayQty": 12, "qtySub": -2},
            ]
            validation = {"publishedRows": 1, "summaryRows": 1, "status": "PASS"}
            digest = _proof(
                bronze / day / f"{key}.json", RANKS,
                {"trade_date": compact, "trade_type": "0", "variety": "si",
                 "contract_id": "si2401", "data_type": str(direction)},
                rows, validation,
            )
            directions[key] = {**validation, "payloadSha256": digest}
        state = {
            "schema": "gfex-rank-capture/v1", "tradingDay": day,
            "status": "PASS_RAW", "products": ["si"],
            "contractLists": {"si": {
                "contracts": contracts, "payloadSha256": list_digest,
                "status": "PASS" if contracts else "NO_PUBLISHED_CONTRACTS",
            }},
            "directions": directions,
        }
        target = root / "state" / "gfex_rank_capture" / f"{day}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(state), encoding="utf-8")


def test_gfex_bronze_import_keeps_empty_official_days_distinct_from_ranked_days(tmp_path: Path) -> None:
    _fixture(tmp_path)
    unavailable = prepare_gfex_rank_day(tmp_path, EARLY)
    ranked = prepare_gfex_rank_day(tmp_path, RANKED)
    assert unavailable.status == "SOURCE_UNAVAILABLE"
    assert unavailable.coverage["status"] == "NO_PUBLICATION" and not unavailable.positions
    assert ranked.status == "PASS"
    assert (ranked.contract_count, ranked.direction_count) == (1, 3)
    assert len(ranked.positions) == 2 and len(ranked.trades) == 1
    assert {row["side"] for row in ranked.positions} == {"LONG", "SHORT"}

    dry = import_gfex_rank_bronze(tmp_path, start_day=EARLY, end_day=RANKED)
    assert dry["status"] == "VALIDATED_WITH_UNAVAILABLE"
    assert dry["authoritativeDataWritten"] is False
    assert dry["verifiedDays"] == 2 and dry["unavailableDays"] == 1
    assert not (tmp_path / "catalog.duckdb").exists()

    legacy_source = "official-gfex-member-ranking"
    store = MarketStore(tmp_path)
    try:
        store.replace_futures_member_rank_day_source(
            trading_day=RANKED, exchange="GFEX", source=legacy_source,
            positions=[{**row, "source": legacy_source} for row in ranked.positions],
            trades=[{**row, "source": legacy_source} for row in ranked.trades],
            coverage={**ranked.coverage, "source": legacy_source},
        )
    finally:
        store.close()
    result = import_gfex_rank_bronze(tmp_path, start_day=EARLY, end_day=RANKED, write=True)
    assert result["status"] == "PASS_WITH_UNAVAILABLE"
    assert result["importedDays"] == 1
    evidence = json.loads((tmp_path / "state" / "gfex_rank_import" / f"{EARLY}.json").read_text(encoding="utf-8"))
    assert evidence["qualityStatus"] == "NO_PUBLICATION"
    assert evidence["authoritativeDataWritten"] is False
    store = MarketStore(tmp_path)
    try:
        assert len(store.list_futures_member_position_ranks(trading_day=RANKED, exchange="GFEX")) == 4
        assert len(store.list_futures_member_trade_ranks(trading_day=RANKED, exchange="GFEX")) == 2
        assert [row["status"] for row in store.list_futures_member_position_coverage(
            trading_day=EARLY, exchange="GFEX",
        )] == ["NO_PUBLICATION"]
        assert {row["source"] for row in store.list_futures_member_position_coverage(
            trading_day=RANKED, exchange="GFEX",
        )} == {SOURCE, legacy_source}
    finally:
        store.close()


def test_gfex_bronze_import_rejects_tampered_direction_before_catalog_write(tmp_path: Path) -> None:
    _fixture(tmp_path)
    target = tmp_path / "bronze" / "gfex_rank_capture" / RANKED / "si2401.2.json"
    proof = json.loads(target.read_text(encoding="utf-8"))
    proof["payloadSha256"] = "0" * 64
    target.write_text(json.dumps(proof), encoding="utf-8")
    with pytest.raises(ValueError, match="SHA mismatch"):
        prepare_gfex_rank_day(tmp_path, RANKED)
    assert not (tmp_path / "catalog.duckdb").exists()
