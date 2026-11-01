"""Offline DCE batch import gates without touching the production Catalog."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
from zipfile import ZipFile

import pytest

from market_monitor.dce_batch_rank_import import (
    CAPTURE_ADAPTER, CAPTURE_SCHEMA, PAGE, _exclusive_authorized_writer_window,
    PreparedDceBatchDay, _reconcile_authorized_overlap,
    prepare_dce_batch_day,
)
from market_monitor.dce_batch_zip import parse_dce_batch_zip
from market_monitor.futures_member_rank_source import (
    DCE_BATCH_SOURCE, select_member_position_source_rows,
)
from market_monitor.web_api.futures import _member_position_coverage_payload
from market_monitor.storage import MarketStore


DAY = "2026-09-30"
COMPACT = "20260930"
PATH = "/dcereport/publicweb/dailystat/memberDealPosi/batchDownload"


def test_authorized_reconciliation_accepts_only_equal_quantity_tie_swap() -> None:
    source = "official-dce-member-ranking"
    common = {"trading_day": DAY, "exchange": "DCE", "contract_code": "PP2609",
              "side": "SHORT"}
    prior_positions = [
        {**common, "source": source, "rank": 1, "member_key": "member-a",
         "position": 5661, "position_change": 1626},
        {**common, "source": source, "rank": 2, "member_key": "member-b",
         "position": 5661, "position_change": 64},
    ]
    batch_positions = [
        {**prior_positions[1], "rank": 1, "source": DCE_BATCH_SOURCE},
        {**prior_positions[0], "rank": 2, "source": DCE_BATCH_SOURCE},
    ]
    prior_trades = [{"trading_day": DAY, "exchange": "DCE", "source": source,
                     "contract_code": "PP2609", "rank": 1, "member_key": "member-a",
                     "volume": 10, "volume_change": 2}]
    batch_trades = [{**prior_trades[0], "source": DCE_BATCH_SOURCE}]

    class FakeStore:
        def list_futures_member_position_ranks(self, **_kwargs):
            return prior_positions

        def list_futures_member_trade_ranks(self, **_kwargs):
            return prior_trades

        def list_futures_member_position_coverage(self, **_kwargs):
            return [{"source": source, "status": "PASS"}]

    prepared = PreparedDceBatchDay(
        trading_day=DAY, archive_sha256="synthetic", entry_count=1,
        ranked_contract_count=1, empty_contract_count=0,
        excluded_contracts=(), positions=batch_positions, trades=batch_trades,
        coverage={},
    )
    _reconcile_authorized_overlap(FakeStore(), prepared)
    changed = replace(prepared, positions=[
        {**batch_positions[0], "position_change": 999}, batch_positions[1],
    ])
    with pytest.raises(ValueError, match="differs from authorized"):
        _reconcile_authorized_overlap(FakeStore(), changed)


def _zip(*, include_excluded: bool = False) -> bytes:
    def report(contract: str) -> bytes:
        lines = [
            "大连商品交易所 成交量_买持仓_卖持仓排名",
            f"合约代码：{contract}\tDate：2026-09-30", "期货公司会员",
        ]
        for label in ("成交量", "持买单量", "持卖单量"):
            lines.extend((f"名次\t会员简称\t{label}\t增减", "1\t合成会员\t100\t-2", "合计\t100\t-2"))
        return ("\r\n".join(lines) + "\r\n").encode()

    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr(f"{COMPACT}_a2611_成交量_买持仓_卖持仓排名.txt", report("a2611"))
        if include_excluded:
            archive.writestr(f"{COMPACT}_v2609F_成交量_买持仓_卖持仓排名.txt", report("v2609F"))
    return stream.getvalue()


@pytest.mark.parametrize("manifest_version", ["current", "legacy-flat", "legacy-nested"])
def test_prepare_dce_batch_day_accepts_both_verified_bronze_manifest_versions(tmp_path, manifest_version: str) -> None:
    raw = _zip()
    parsed = parse_dce_batch_zip(raw, trading_day=COMPACT)
    directory = tmp_path / "bronze" / "dce_dom_capture"
    directory.mkdir(parents=True)
    (directory / f"{COMPACT}.zip").write_bytes(raw)
    common = {
        "source": PAGE, "batchPath": PATH, "tradeDate": DAY,
        "httpStatus": 200, "bytes": len(raw), "status": "PASS_RAW",
        "authoritativeDataWritten": False,
        "capturedAtUtc": "2026-10-02T10:00:00+00:00",
    }
    if manifest_version != "current":
        manifest = {**common, "method": "LDPlayer page fetch + CDP Fetch",
                    "sha256": sha256(raw).hexdigest(), "entries": 1,
                    "auditVersion": "dce_ldplayer_zip_v1", "uniqueContracts": 1,
                    "allDateContractAndThreeSections": True, "crcOk": True}
        if manifest_version == "legacy-nested":
            manifest["audit"] = {
                "auditVersion": manifest.pop("auditVersion"),
                "uniqueContracts": manifest.pop("uniqueContracts"),
                "allDateContractAndThreeSections": manifest.pop("allDateContractAndThreeSections"),
                "crcOk": manifest.pop("crcOk"),
                "issueCount": 0,
            }
    else:
        manifest = {**common, "schema": CAPTURE_SCHEMA, "publisher": "DCE",
                    "adapter": CAPTURE_ADAPTER, "contentType": "application/zip",
                    "qualityStatus": "PASS_RAW", "payloadSha256": sha256(raw).hexdigest(),
                    "summary": parsed.summary}
    (directory / f"{COMPACT}.json").write_text(json.dumps(manifest), encoding="utf-8")

    prepared = prepare_dce_batch_day(tmp_path, DAY)

    assert prepared.archive_sha256 == sha256(raw).hexdigest()
    assert prepared.ranked_contract_count == 1
    assert len(prepared.positions) == 2
    assert len(prepared.trades) == 1
    assert prepared.coverage["source"] == DCE_BATCH_SOURCE
    assert prepared.coverage["record_count"] == 2


def test_prepare_dce_batch_day_rejects_manifest_that_claims_authoritative_write(tmp_path) -> None:
    raw = _zip()
    parsed = parse_dce_batch_zip(raw, trading_day=COMPACT)
    directory = tmp_path / "bronze" / "dce_dom_capture"
    directory.mkdir(parents=True)
    (directory / f"{COMPACT}.zip").write_bytes(raw)
    (directory / f"{COMPACT}.json").write_text(json.dumps({
        "schema": CAPTURE_SCHEMA, "publisher": "DCE", "adapter": CAPTURE_ADAPTER,
        "source": PAGE, "batchPath": PATH, "tradeDate": DAY, "httpStatus": 200,
        "contentType": "application/zip", "bytes": len(raw),
        "payloadSha256": sha256(raw).hexdigest(), "summary": parsed.summary,
        "capturedAtUtc": "2026-10-02T10:00:00+00:00", "status": "PASS_RAW",
        "qualityStatus": "PASS_RAW", "authoritativeDataWritten": True,
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="provenance"):
        prepare_dce_batch_day(tmp_path, DAY)


def test_prepare_dce_batch_day_includes_validated_month_average_with_original_bronze_evidence(tmp_path) -> None:
    raw = _zip(include_excluded=True)
    parsed = parse_dce_batch_zip(raw, trading_day=COMPACT)
    directory = tmp_path / "bronze" / "dce_dom_capture"
    directory.mkdir(parents=True)
    (directory / f"{COMPACT}.zip").write_bytes(raw)
    (directory / f"{COMPACT}.json").write_text(json.dumps({
        "schema": CAPTURE_SCHEMA, "publisher": "DCE", "adapter": CAPTURE_ADAPTER,
        "source": PAGE, "batchPath": PATH, "tradeDate": DAY, "httpStatus": 200,
        "contentType": "application/zip", "bytes": len(raw),
        "payloadSha256": sha256(raw).hexdigest(), "summary": parsed.summary,
        "capturedAtUtc": "2026-10-02T10:00:00+00:00", "status": "PASS_RAW",
        "qualityStatus": "PASS_RAW", "authoritativeDataWritten": False,
    }), encoding="utf-8")

    prepared = prepare_dce_batch_day(tmp_path, DAY)

    assert prepared.entry_count == 2
    assert prepared.ranked_contract_count == 2
    assert prepared.excluded_contracts == ()
    assert prepared.coverage["contract_count"] == 2
    assert {row["contract_code"] for row in prepared.positions} == {"A2611", "V-F2609"}
    assert prepared.summary["excludedContractCount"] == 0


def test_batch_source_selection_uses_complete_day_source_before_filtered_member_rows() -> None:
    authorized = {"trading_day": DAY, "exchange": "DCE", "source": "official-dce-member-ranking",
                  "member_key": "only-authorized", "quality_status": "PASS"}
    batch = {**authorized, "source": DCE_BATCH_SOURCE, "member_key": "batch-member"}
    coverage = [
        {"trading_day": DAY, "exchange": "DCE", "source": authorized["source"], "status": "PARTIAL"},
        {"trading_day": DAY, "exchange": "DCE", "source": DCE_BATCH_SOURCE, "status": "PASS"},
    ]

    assert select_member_position_source_rows([authorized, batch], coverage) == [batch]
    assert select_member_position_source_rows([authorized], coverage) == []


def test_batch_import_writer_lock_fails_fast_when_authorized_window_is_held(tmp_path) -> None:
    lock = tmp_path / "state" / "dce_authorized_rank_writer.lock"
    with _exclusive_authorized_writer_window(lock):
        with pytest.raises(RuntimeError, match="DCE_AUTHORIZED_WRITER_ACTIVE"):
            with _exclusive_authorized_writer_window(lock):
                pytest.fail("the same Catalog writer lock was acquired twice")


def test_alternate_authorized_failure_does_not_downgrade_complete_batch_coverage() -> None:
    common = {"trading_day": DAY, "exchange": "DCE", "contract_count": 1,
              "record_count": 2, "collected_at": "2026-10-02T10:00:00+00:00",
              "quality_status": "PASS", "adapter": "official-dce-rank-v1"}
    records = [
        {**common, "source": "official-dce-member-ranking", "status": "PARTIAL",
         "error": "some contracts pending"},
        {**common, "source": DCE_BATCH_SOURCE, "status": "PASS", "error": None,
         "adapter": "official-dce-android-browser-batch-v1"},
    ]

    result = _member_position_coverage_payload([], records, commodity_only=True)
    dce = next(row for row in result["exchangeCoverage"] if row["exchange"] == "DCE")
    assert dce["status"] == "PASS"
    assert set(dce["sources"]) == {records[0]["source"], DCE_BATCH_SOURCE}


def test_atomic_day_replace_keeps_other_source_and_rolls_back_a_failed_replacement(tmp_path) -> None:
    raw = _zip()
    parsed = parse_dce_batch_zip(raw, trading_day=COMPACT)
    directory = tmp_path / "bronze" / "dce_dom_capture"
    directory.mkdir(parents=True)
    (directory / f"{COMPACT}.zip").write_bytes(raw)
    (directory / f"{COMPACT}.json").write_text(json.dumps({
        "schema": CAPTURE_SCHEMA, "publisher": "DCE", "adapter": CAPTURE_ADAPTER,
        "source": PAGE, "batchPath": PATH, "tradeDate": DAY, "httpStatus": 200,
        "contentType": "application/zip", "bytes": len(raw),
        "payloadSha256": sha256(raw).hexdigest(), "summary": parsed.summary,
        "capturedAtUtc": "2026-10-02T10:00:00+00:00", "status": "PASS_RAW",
        "qualityStatus": "PASS_RAW", "authoritativeDataWritten": False,
    }), encoding="utf-8")
    prepared = prepare_dce_batch_day(tmp_path, DAY)
    authorized_source = "official-dce-member-ranking"
    store = MarketStore(tmp_path)
    try:
        store.replace_futures_member_rank_day_source(
            trading_day=DAY, exchange="DCE", source=authorized_source,
            positions=[{**row, "source": authorized_source} for row in prepared.positions],
            trades=[{**row, "source": authorized_source} for row in prepared.trades],
            coverage={**prepared.coverage, "source": authorized_source},
        )
        store.replace_futures_member_rank_day_source(
            trading_day=DAY, exchange="DCE", source=DCE_BATCH_SOURCE,
            positions=prepared.positions, trades=prepared.trades, coverage=prepared.coverage,
        )
        ranks = store.list_futures_member_position_ranks(trading_day=DAY, exchange="DCE")
        assert len(ranks) == 4
        assert {row["source"] for row in ranks} == {authorized_source, DCE_BATCH_SOURCE}
        _reconcile_authorized_overlap(store, prepared)
        conflicting = replace(prepared, positions=[
            {**prepared.positions[0], "position": prepared.positions[0]["position"] + 1},
            *prepared.positions[1:],
        ])
        with pytest.raises(ValueError, match="differs from authorized"):
            _reconcile_authorized_overlap(store, conflicting)
        with pytest.raises(Exception):
            store.replace_futures_member_rank_day_source(
                trading_day=DAY, exchange="DCE", source=DCE_BATCH_SOURCE,
                positions=prepared.positions, trades=[*prepared.trades, *prepared.trades],
                coverage=prepared.coverage,
            )
        assert len(store.list_futures_member_position_ranks(trading_day=DAY, exchange="DCE")) == 4
        assert len(store.list_futures_member_trade_ranks(trading_day=DAY, exchange="DCE")) == 2
        assert len(store.list_futures_member_position_coverage(trading_day=DAY, exchange="DCE")) == 2
    finally:
        store.close()
