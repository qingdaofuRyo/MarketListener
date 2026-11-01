"""Synthetic desktop-Bronze import gates; no exchange or live Catalog use."""

from __future__ import annotations

from datetime import date
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from market_monitor.dce_batch_rank_import import _exclusive_authorized_writer_window
from market_monitor.dce_batch_zip import parse_dce_batch_zip
from market_monitor.dce_desktop_batch_capture import ADAPTER, BRONZE, SCHEMA
from market_monitor.dce_desktop_batch_import import (
    IMPORT_ADAPTER, import_dce_desktop_batch_ranks, prepare_dce_desktop_batch_day,
)
from market_monitor.futures_calendar import sync_futures_trading_calendar
from market_monitor.futures_member_rank_source import (
    DCE_BATCH_SOURCE, DCE_DESKTOP_BATCH_SOURCE, select_member_position_source_rows,
)
from market_monitor.storage import MarketStore


DAY = "2026-10-16"
COMPACT = "20261016"
PAGE = "http://www.dce.com.cn/dce/channel/list/176.html"


def _archive(*, omit_short: bool = False) -> bytes:
    lines = [
        "大连商品交易所 成交量_买持仓_卖持仓排名",
        f"合约代码：a2611\tDate：{DAY}", "期货公司会员",
    ]
    for label in ("成交量", "持买单量", "持卖单量"):
        lines.append(f"名次\t会员简称\t{label}\t增减")
        if not (omit_short and label == "持卖单量"):
            lines.append("1\t合成会员\t100\t-2")
            lines.append("合计\t100\t-2")
        else:
            lines.append("合计\t0\t0")
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr(f"{COMPACT}_a2611_成交量_买持仓_卖持仓排名.txt",
                         ("\r\n".join(lines) + "\r\n").encode())
    return stream.getvalue()


def _bronze(root: Path, *, raw: bytes | None = None, changes: dict | None = None) -> None:
    body = raw if raw is not None else _archive()
    parsed = parse_dce_batch_zip(body, trading_day=COMPACT)
    manifest = {
        "schema": SCHEMA, "publisher": "DCE", "adapter": ADAPTER,
        "source": PAGE, "tradeDate": DAY,
        "method": "manual visible Chrome/Edge button download; History Blob record",
        "browser": "edge", "downloadOrigin": "blob:http://www.dce.com.cn",
        "downloadedAtUtc": "2026-10-18T02:30:00+00:00",
        "bytes": len(body), "payloadSha256": parsed.archive_sha256,
        "summary": parsed.summary, "status": "PASS_RAW",
        "qualityStatus": "PASS_RAW", "authoritativeDataWritten": False,
        "httpStatus": None,
    }
    manifest.update(changes or {})
    directory = root / BRONZE
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{COMPACT}.zip").write_bytes(body)
    (directory / f"{COMPACT}.json").write_text(json.dumps(manifest), encoding="utf-8")


class _Calendar:
    def itertuples(self, *, index: bool, name: None):
        assert index is False and name is None
        return iter([(date.fromisoformat(DAY),)])


def _calendar(root: Path) -> None:
    sync_futures_trading_calendar(root, fetcher=_Calendar)


def test_prepare_rechecks_bronze_and_uses_observed_page_as_source_url(tmp_path: Path) -> None:
    _bronze(tmp_path)
    prepared = prepare_dce_desktop_batch_day(tmp_path, DAY)

    assert prepared.browser == "edge"
    assert prepared.ranked_contract_count == 1
    assert len(prepared.positions) == 2
    assert len(prepared.trades) == 1
    assert prepared.coverage["source"] == DCE_DESKTOP_BATCH_SOURCE
    assert prepared.coverage["source_url"] == PAGE
    assert prepared.coverage["adapter"] == IMPORT_ADAPTER
    assert prepared.coverage["payload_sha256"] == prepared.archive_sha256
    assert all(row["source_url"] == PAGE for row in [*prepared.positions, *prepared.trades])


@pytest.mark.parametrize("changes", [
    {"tradeDate": "2026-10-15"},
    {"payloadSha256": "0" * 64},
    {"bytes": 1},
    {"summary": {"rankRows": {"volume": 99}}},
    {"browser": "firefox"},
    {"source": "http://other.example/dce/channel/list/176.html"},
    {"downloadOrigin": "blob:http://other.example"},
    {"downloadedAtUtc": "2026-10-18T02:30:00"},
    {"historyReadMode": "unverified"},
    {"authoritativeDataWritten": True},
    {"httpStatus": 200},
])
def test_prepare_rejects_changed_manifest_proof(tmp_path: Path, changes: dict) -> None:
    _bronze(tmp_path, changes=changes)
    with pytest.raises(ValueError):
        prepare_dce_desktop_batch_day(tmp_path, DAY)


def test_prepare_rejects_changed_zip_and_missing_rank_direction(tmp_path: Path) -> None:
    _bronze(tmp_path)
    (tmp_path / BRONZE / f"{COMPACT}.zip").write_bytes(b"not a ZIP")
    with pytest.raises(ValueError):
        prepare_dce_desktop_batch_day(tmp_path, DAY)

    _bronze(tmp_path, raw=_archive(omit_short=True))
    with pytest.raises(ValueError, match="ranking direction"):
        prepare_dce_desktop_batch_day(tmp_path, DAY)


def test_dry_run_does_not_open_catalog_and_write_is_idempotent(tmp_path: Path) -> None:
    _bronze(tmp_path)
    _calendar(tmp_path)
    report = import_dce_desktop_batch_ranks(
        tmp_path, start_day=DAY, end_day=DAY,
    )
    assert report["status"] == "VALIDATED"
    assert report["verifiedDays"] == 1
    assert report["authoritativeDataWritten"] is False
    assert not (tmp_path / "catalog.duckdb").exists()

    written = import_dce_desktop_batch_ranks(
        tmp_path, start_day=DAY, end_day=DAY, write=True,
    )
    assert written["status"] == "PASS"
    assert written["importedDays"] == 1
    store = MarketStore(tmp_path)
    try:
        rows = store.list_futures_member_position_ranks(trading_day=DAY, exchange="DCE")
        assert len(rows) == 2
        assert {row["source"] for row in rows} == {DCE_DESKTOP_BATCH_SOURCE}
    finally:
        store.close()

    resumed = import_dce_desktop_batch_ranks(
        tmp_path, start_day=DAY, end_day=DAY, write=True,
    )
    assert resumed["coveredDays"] == 1
    assert resumed["importedDays"] == 0
    assert resumed["authoritativeDataWritten"] is False


def test_existing_android_batch_is_not_double_counted(tmp_path: Path) -> None:
    _bronze(tmp_path)
    _calendar(tmp_path)
    prepared = prepare_dce_desktop_batch_day(tmp_path, DAY)
    store = MarketStore(tmp_path)
    try:
        store.replace_futures_member_rank_day_source(
            trading_day=DAY, exchange="DCE", source=DCE_BATCH_SOURCE,
            positions=[{**row, "source": DCE_BATCH_SOURCE} for row in prepared.positions],
            trades=[{**row, "source": DCE_BATCH_SOURCE} for row in prepared.trades],
            coverage={**prepared.coverage, "source": DCE_BATCH_SOURCE},
        )
    finally:
        store.close()

    result = import_dce_desktop_batch_ranks(
        tmp_path, start_day=DAY, end_day=DAY, write=True,
    )
    assert result["coveredDays"] == 1
    assert result["importedDays"] == 0
    store = MarketStore(tmp_path)
    try:
        rows = store.list_futures_member_position_ranks(trading_day=DAY, exchange="DCE")
        assert len(rows) == 2
        assert {row["source"] for row in rows} == {DCE_BATCH_SOURCE}
    finally:
        store.close()


def test_existing_android_conflict_fails_closed(tmp_path: Path) -> None:
    _bronze(tmp_path)
    _calendar(tmp_path)
    prepared = prepare_dce_desktop_batch_day(tmp_path, DAY)
    store = MarketStore(tmp_path)
    try:
        existing = [{**row, "source": DCE_BATCH_SOURCE} for row in prepared.positions]
        existing[0]["position"] += 1
        store.replace_futures_member_rank_day_source(
            trading_day=DAY, exchange="DCE", source=DCE_BATCH_SOURCE,
            positions=existing,
            trades=[{**row, "source": DCE_BATCH_SOURCE} for row in prepared.trades],
            coverage={**prepared.coverage, "source": DCE_BATCH_SOURCE},
        )
    finally:
        store.close()
    with pytest.raises(ValueError, match="differs from complete"):
        import_dce_desktop_batch_ranks(
            tmp_path, start_day=DAY, end_day=DAY, write=True,
        )


def test_desktop_source_selected_once_below_android_and_above_old_api() -> None:
    common = {"trading_day": DAY, "exchange": "DCE", "quality_status": "PASS"}
    old = {**common, "source": "official-dce-member-ranking", "position": 10}
    desktop = {**common, "source": DCE_DESKTOP_BATCH_SOURCE, "position": 10}
    android = {**common, "source": DCE_BATCH_SOURCE, "position": 10}
    coverage = [{**common, "source": row["source"], "status": "PASS"}
                for row in (old, desktop, android)]

    assert select_member_position_source_rows([old, desktop], coverage[:2]) == [desktop]
    assert select_member_position_source_rows([old, desktop, android], coverage) == [android]


def test_desktop_import_obeys_shared_single_writer_lock(tmp_path: Path) -> None:
    _bronze(tmp_path)
    _calendar(tmp_path)
    lock = tmp_path / "state" / "dce_authorized_rank_writer.lock"
    with _exclusive_authorized_writer_window(lock):
        with pytest.raises(RuntimeError, match="DCE_AUTHORIZED_WRITER_ACTIVE"):
            import_dce_desktop_batch_ranks(
                tmp_path, start_day=DAY, end_day=DAY, write=True,
            )
    assert not (tmp_path / "catalog.duckdb").exists()
