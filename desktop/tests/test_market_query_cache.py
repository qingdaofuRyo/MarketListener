"""Focused tests for low-latency K-line windows."""

from __future__ import annotations

from pathlib import Path
import json
from hashlib import sha256
import threading
import time

import duckdb
import pytest

from market_monitor.market_query_cache import (
    KLineCacheUnavailable,
    KLineQueryStore,
    QUERY_INDEX_SCHEMA_VERSION,
)
from market_monitor.market_data_version import advance_market_data_version, market_data_version
from market_monitor.storage import MarketStore, PartitionKey
from market_monitor.web_api.common import load_inventory, read_bars_before
from web_fixtures import silver_row, write_silver


INSTRUMENT = "CN.DCE.FUTURE.JM2609"
OTHER_INSTRUMENT = "CN.DCE.FUTURE.PS2609"


def test_legacy_pytdx_midnight_30m_is_excluded_with_correct_windows(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "data"
    instrument = "CN.SSE.STOCK.600519"
    bad = silver_row(instrument, "2026-08-21", period="30m", close=10.0)
    bad.update(source="pytdx", bar_open_time="2026-08-21T00:00:00+08:00")
    good = silver_row(instrument, "2026-08-21", period="30m", close=11.0)
    good.update(source="local-verified", bar_open_time="2026-08-21T10:00:00+08:00")
    writer = MarketStore(root)
    try:
        run_id = writer.begin_run("synthetic-quality")
        path = writer.write_silver_bars(PartitionKey("CN", "STOCK", "30m", 2026, "CN-STOCK-30m-2026"),
                                        [bad, good], good["bar_open_time"], run_id)
    finally:
        writer.close()
    store = KLineQueryStore(root)
    store.rebuild()
    assert [x["close"] for x in store.read_bars(instrument, "30m", 10)] == [11.0]
    assert [x["close"] for x in store.read_tails([instrument], "30m", 10)[instrument]] == [11.0]
    assert store.bounds(instrument, "30m") == (1, "2026-08-21T10:00:00+08:00", "2026-08-21T10:00:00+08:00")
    window, total = store.read_window(instrument, "30m", 0, 10)
    assert total == 1 and [x["close"] for x in window] == [11.0]
    cursor = store.read_before(instrument, "30m", limit=10)
    assert cursor.total == 1 and [x["close"] for x in cursor.bars] == [11.0] and not cursor.has_more
    assert store.read_bars(instrument, None, 10)[0]["close"] == 11.0
    from market_monitor.web_api import common
    class BrokenIndex:
        def read_bars(self, *_args): raise RuntimeError("synthetic")
        def read_window(self, *_args): raise RuntimeError("synthetic")
        def read_before(self, *_args, **_kwargs): raise RuntimeError("synthetic")
        def _should_build_inline(self): return True
    monkeypatch.setattr(common, "get_kline_query_store", lambda _root: BrokenIndex())
    monkeypatch.setattr(common, "silver_period_partitions", lambda *_args: [path])
    assert [x["close"] for x in common.read_bars(root, instrument, period="30m")] == [11.0]
    rows, count = common.read_bars_window(root, instrument, period="30m", start=0, limit=10)
    assert count == 1 and [x["close"] for x in rows] == [11.0]
    page = common.read_bars_before(root, instrument, period="30m", limit=10)
    assert page.total == 1 and [x["close"] for x in page.bars] == [11.0]


def test_invalid_newer_duplicate_does_not_hide_verified_other_source(tmp_path: Path) -> None:
    root = tmp_path / "data"
    instrument = "CN.SSE.STOCK.600519"
    good = silver_row(instrument, "2026-08-21", period="30m", close=11.0)
    good.update(source="local-verified", bar_open_time="2026-08-21T00:00:00+08:00", fetched_at="2026-08-21T01:00:00+08:00")
    bad = dict(good, source="pytdx", close=99.0, fetched_at="2026-08-22T01:00:00+08:00")
    writer = MarketStore(root)
    try:
        run_id = writer.begin_run("synthetic-quality")
        writer.write_silver_bars(PartitionKey("CN", "STOCK", "30m", 2026, "CN-STOCK-30m-2026"),
                                 [bad], bad["bar_open_time"], run_id)
        writer.write_silver_bars(PartitionKey("CN", "STOCK", "30m", 2026, "verified-source"),
                                 [good], good["bar_open_time"], run_id)
    finally:
        writer.close()
    store = KLineQueryStore(root)
    store.rebuild()
    page = store.read_before(instrument, "30m", limit=10)
    assert page.total == 1 and len(page.bars) == 1
    assert page.bars[0]["source"] == "local-verified" and page.bars[0]["close"] == 11.0


def test_midnight_quality_counts_do_not_depend_on_partition_name(tmp_path: Path, monkeypatch) -> None:
    from market_monitor import market_query_cache

    root = tmp_path / "data"
    instrument = "CN.SSE.STOCK.600519"
    bad = silver_row(instrument, "2026-08-21", period="30m", close=10.0)
    bad.update(source="pytdx", bar_open_time="2026-08-21T00:00:00+08:00")
    good = silver_row(instrument, "2026-08-21", period="30m", close=11.0)
    good.update(source="local-verified", bar_open_time="2026-08-21T10:00:00+08:00")
    writer = MarketStore(root)
    try:
        run_id = writer.begin_run("synthetic-quality")
        writer.write_silver_bars(
            PartitionKey("CN", "STOCK", "30m", 2026, "unusual-source-name"),
            [bad, good], good["bar_open_time"], run_id,
        )
    finally:
        writer.close()
    store = KLineQueryStore(root)
    store.rebuild()
    calls = []
    original = market_query_cache._filtered_intraday_bounds

    def counted(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(market_query_cache, "_filtered_intraday_bounds", counted)
    assert store.bounds(instrument, "30m") == (1, good["bar_open_time"], good["bar_open_time"])
    rows, total = store.read_window(instrument, "30m", 0, 10)
    page = store.read_before(instrument, "30m", limit=10)
    assert total == page.total == 1
    assert [row["close"] for row in rows] == [row["close"] for row in page.bars] == [11.0]
    assert len(calls) == 1

    newer = dict(good, bar_open_time="2026-08-21T10:30:00+08:00", close=12.0)
    writer = MarketStore(root)
    try:
        run_id = writer.begin_run("synthetic-next-revision")
        writer.write_silver_bars(
            PartitionKey("CN", "STOCK", "30m", 2026, "another-source-name"),
            [newer], newer["bar_open_time"], run_id,
        )
    finally:
        writer.close()
    store.rebuild()
    assert store.bounds(instrument, "30m") == (2, good["bar_open_time"], newer["bar_open_time"])
    assert len(calls) == 2


def test_tdx_metadata_inventory_uses_source_specific_manifest_without_rebuilding(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "data"
    instrument = "CN.SSE.STOCK.600519.TDX_LOCAL"
    row = silver_row(instrument, "2026-09-24")
    row.update(source="通达信金融终端（本地）", actual_source="通达信金融终端（本地）", source_symbol="600519")
    write_silver(root, [row])
    store = KLineQueryStore(root)
    store.rebuild()
    monkeypatch.setattr(store, "ensure_ready", lambda: pytest.fail("metadata must not trigger a rebuild"))
    report = store.tdx_import_inventory()
    assert report["available"] is True
    assert report["schema"] == "tdx-source-inventory-v2"
    assert report["items"] == [{"terminal": "financial", "instrumentId": instrument, "symbol": "600519", "period": "1d",
                                "earliestDate": "2026-09-24", "latestDate": "2026-09-24", "rows": 1}]
    assert report["stale"] is False
    from market_monitor.market_data_version import advance_market_data_version

    advance_market_data_version(root)
    old = store.tdx_import_inventory()
    assert old["stale"] is True
    assert old["revision"] != old["dataRevision"]
    assert old["items"] == report["items"]


def test_tdx_inventory_preserves_venue_ids_and_rejects_unknown_or_mismatched_sources(tmp_path: Path) -> None:
    root = tmp_path / "data"
    instruments = (
        ("CN.SSE.INDEX.000001.TDX_LOCAL", "通达信金融终端（本地）", "000001", "2026-09-24"),
        ("CN.SZSE.STOCK.000001.TDX_LOCAL", "通达信金融终端（本地）", "000001", "2026-09-30"),
        ("CN.DCE.FUTURE.JM.MAIN.TDX", "通达信期货通", "JML8", "2026-09-29"),
        ("CN.DCE.FUTURE.JM.SECONDARY.TDX", "unknown fixture", "JML7", "2026-09-28"),
        ("CN.DCE.FUTURE.JM.WEIGHTED.TDX", "通达信金融终端（本地）", "JML9", "2026-09-27"),
    )
    rows = []
    for identifier, source, symbol, day in instruments:
        row = silver_row(identifier, day)
        row.update(source=source, actual_source=source, source_symbol=symbol)
        rows.append(row)
    write_silver(root, rows)
    store = KLineQueryStore(root)
    store.rebuild()
    items = {item["instrumentId"]: item for item in store.tdx_import_inventory()["items"]}
    assert set(items) == {identifier for identifier, *_ in instruments[:3]}
    assert items[instruments[0][0]]["latestDate"] == "2026-09-24"
    assert items[instruments[1][0]]["latestDate"] == "2026-09-30"
    assert items[instruments[2][0]]["terminal"] == "futures"


def _store(tmp_path: Path) -> KLineQueryStore:
    data_root = tmp_path / "data"
    write_silver(
        data_root,
        [
            silver_row(
                INSTRUMENT,
                f"2026-08-{day:02d}",
                asset_type="FUTURE",
                close=float(day),
            )
            for day in range(1, 11)
        ]
        + [
            silver_row(OTHER_INSTRUMENT, "2026-08-09", asset_type="FUTURE", close=90.0),
            silver_row(OTHER_INSTRUMENT, "2026-08-10", asset_type="FUTURE", close=100.0),
        ],
    )
    store = KLineQueryStore(data_root)
    store.rebuild()
    return store


def test_cursor_windows_are_chronological_stable_and_do_not_overlap(tmp_path: Path) -> None:
    store = _store(tmp_path)

    latest = store.read_before(INSTRUMENT, "1d", limit=3)
    earlier = store.read_before(INSTRUMENT, "1d", before=latest.before, limit=3)

    assert [bar["close"] for bar in latest.bars] == [8.0, 9.0, 10.0]
    assert latest.before == "2026-08-08T09:30:00"
    assert latest.total == 10
    assert latest.has_more is True
    assert [bar["close"] for bar in earlier.bars] == [5.0, 6.0, 7.0]
    assert set(bar["bar_open_time"] for bar in latest.bars).isdisjoint(
        bar["bar_open_time"] for bar in earlier.bars
    )


def test_cursor_page_resolves_file_paths_and_total_with_one_index_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    original_connect = store._connect
    opens = []

    def counted_connect(*, read_only=False):
        opens.append(read_only)
        return original_connect(read_only=read_only)

    monkeypatch.setattr(store, "_connect", counted_connect)
    page = store.read_before(INSTRUMENT, "1d", limit=3)
    assert page.total == 10
    assert [bar["close"] for bar in page.bars] == [8.0, 9.0, 10.0]
    assert opens == [True]


def test_offset_compatibility_window_uses_tail_and_keeps_order(tmp_path: Path) -> None:
    store = _store(tmp_path)

    bars, total = store.read_window(INSTRUMENT, "1d", start=8, limit=2)

    assert total == 10
    assert [bar["close"] for bar in bars] == [9.0, 10.0]


def test_persistent_cache_is_a_file_manifest_not_a_full_json_copy(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with duckdb.connect(str(store.path), read_only=True) as connection:
        tables = {str(row[0]) for row in connection.execute("SHOW TABLES").fetchall()}
        indexed_rows = int(connection.execute("SELECT sum(row_count) FROM instrument_file").fetchone()[0])

    assert "bars" not in tables
    assert "instrument_file" in tables
    assert indexed_rows == 12


def test_refresh_replaces_legacy_relative_path_alias_without_duplicate_rows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with duckdb.connect(str(store.path)) as connection:
        rows = connection.execute("SELECT * FROM instrument_file").fetchall()
        assert {row[2] for row in rows} == {rows[0][2]}
        absolute_path = Path(str(rows[0][2]))
        relative_path = str(Path(store.data_root.name) / absolute_path.relative_to(store.data_root))
        for row in rows:
            duplicate = list(row)
            duplicate[2] = relative_path
            connection.execute("INSERT INTO instrument_file VALUES (?, ?, ?, ?, ?, ?, ?, ?)", duplicate)

    previous = market_data_version(store.data_root)
    revision = advance_market_data_version(
        store.data_root, changed_file=absolute_path,
        sha256=sha256(absolute_path.read_bytes()).hexdigest(),
    )[1]
    assert store.refresh_partition_files(
        [absolute_path], expected_base_revision=previous, target_revision=revision,
    ) is True

    with duckdb.connect(str(store.path), read_only=True) as connection:
        file_paths = connection.execute("SELECT DISTINCT file_path FROM instrument_file").fetchall()
        indexed_rows = connection.execute("SELECT sum(row_count) FROM instrument_file").fetchone()[0]
        period_rows = connection.execute("SELECT sum(row_count) FROM instrument_period").fetchone()[0]
    assert file_paths == [(str(absolute_path),)]
    assert indexed_rows == period_rows == 12


def test_silver_write_refreshes_latest_before_marking_index_current(tmp_path: Path) -> None:
    cache = _store(tmp_path)
    root = cache.data_root
    store = MarketStore(root)
    try:
        run_id = store.begin_run("synthetic-incremental")
        next_bar = silver_row(INSTRUMENT, "2026-08-11", asset_type="FUTURE", close=11.0)
        store.write_silver_bars(
            PartitionKey("CN", "FUTURE", "1d", 2026, "synthetic-new-month"),
            [next_bar], next_bar["bar_open_time"], run_id,
        )
    finally:
        store.close()

    assert cache.status().ready is True
    with duckdb.connect(str(cache.path), read_only=True) as connection:
        row = connection.execute(
            "SELECT bar_open_time,bar_json FROM instrument_latest WHERE instrument_id=?",
            [INSTRUMENT],
        ).fetchone()
        schema, catalog_digest = connection.execute(
            "SELECT schema_version,catalog_digest FROM cache_meta"
        ).fetchone()
    assert row is not None
    assert row[0].startswith("2026-08-11")
    assert json.loads(row[1])["close"] == 11.0
    assert schema == QUERY_INDEX_SCHEMA_VERSION
    assert isinstance(catalog_digest, str) and len(catalog_digest) == 64


def test_relative_data_root_records_new_partition_before_promotion(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    root = Path("data")
    write_silver(root, [silver_row(INSTRUMENT, "2026-08-10", asset_type="FUTURE")])
    cache = KLineQueryStore(root)
    cache.rebuild()
    store = MarketStore(root)
    try:
        run_id = store.begin_run("synthetic-relative-root")
        bar = silver_row(INSTRUMENT, "2026-08-11", asset_type="FUTURE", close=11.0)
        store.write_silver_bars(
            PartitionKey("CN", "FUTURE", "1d", 2026, "relative-new"),
            [bar], bar["bar_open_time"], run_id,
        )
    finally:
        store.close()
    assert cache.status().ready is True
    assert [row["close"] for row in cache.read_bars(INSTRUMENT, "1d", 2)] == [10.0, 11.0]


def test_failed_silver_promotion_invalidates_old_mutable_path_index(tmp_path: Path, monkeypatch) -> None:
    from market_monitor import storage as storage_module

    cache = _store(tmp_path)
    original_replace = storage_module.os.replace

    def fail_target(source, target):
        if Path(target).name == "synthetic-failed.parquet":
            raise PermissionError("synthetic promotion failure")
        return original_replace(source, target)

    monkeypatch.setattr(storage_module.os, "replace", fail_target)
    store = MarketStore(cache.data_root)
    try:
        run_id = store.begin_run("synthetic-failed-promotion")
        bar = silver_row(INSTRUMENT, "2026-08-11", asset_type="FUTURE", close=11.0)
        with pytest.raises(PermissionError, match="promotion failure"):
            store.write_silver_bars(
                PartitionKey("CN", "FUTURE", "1d", 2026, "synthetic-failed"),
                [bar], bar["bar_open_time"], run_id,
            )
    finally:
        store.close()
    assert cache.status().ready is False
    assert not list((cache.data_root / "silver").rglob("synthetic-failed.parquet"))


def test_full_rebuild_reads_mixed_period_and_bar_period_layouts(tmp_path: Path) -> None:
    cache = _store(tmp_path)
    root = cache.data_root
    store = MarketStore(root)
    try:
        run_id = store.begin_run("synthetic-mixed-layout")
        next_bar = silver_row(INSTRUMENT, "2026-08-12", asset_type="FUTURE", close=12.0)
        store.write_silver_bars(
            PartitionKey("CN", "FUTURE", "1d", 2026, "synthetic-modern-layout"),
            [next_bar], next_bar["bar_open_time"], run_id, update_query_cache=False,
        )
    finally:
        store.close()

    rebuilt = cache.rebuild()
    assert rebuilt.ready is True
    assert rebuilt.rows == 13
    with duckdb.connect(str(cache.path), read_only=True) as connection:
        row = connection.execute(
            "SELECT bar_open_time,bar_json FROM instrument_latest WHERE instrument_id=?",
            [INSTRUMENT],
        ).fetchone()
    assert row is not None
    assert row[0].startswith("2026-08-12")
    assert json.loads(row[1])["close"] == 12.0


def test_partial_refresh_cannot_cover_an_earlier_unindexed_silver_write(tmp_path: Path) -> None:
    cache = _store(tmp_path)
    root = cache.data_root
    store = MarketStore(root)
    try:
        run_id = store.begin_run("synthetic-two-sources")
        first = silver_row("CN.DCE.FUTURE.A2609", "2026-08-11", asset_type="FUTURE")
        store.write_silver_bars(
            PartitionKey("CN", "FUTURE", "1d", 2026, "source-a"),
            [first], first["bar_open_time"], run_id, update_query_cache=False,
        )
        revision_after_a = market_data_version(root)
        second = silver_row("CN.DCE.FUTURE.B2609", "2026-08-11", asset_type="FUTURE")
        second_path = store.write_silver_bars(
            PartitionKey("CN", "FUTURE", "1d", 2026, "source-b"),
            [second], second["bar_open_time"], run_id, update_query_cache=False,
        )
    finally:
        store.close()

    assert cache.refresh_partition_files(
        [second_path], expected_base_revision=revision_after_a,
        target_revision=market_data_version(root),
    ) is False
    assert cache.status().ready is False


def test_revision_journal_rejects_interleaved_source_missing_from_refresh(tmp_path: Path) -> None:
    cache = _store(tmp_path)
    root = cache.data_root
    base = market_data_version(root)
    store = MarketStore(root)
    try:
        run_id = store.begin_run("synthetic-interleaved-sources")
        first = silver_row("CN.DCE.FUTURE.A2609", "2026-08-11", asset_type="FUTURE")
        first_path = store.write_silver_bars(
            PartitionKey("CN", "FUTURE", "1d", 2026, "interleaved-a"),
            [first], first["bar_open_time"], run_id, update_query_cache=False,
        )
        second = silver_row("CN.DCE.FUTURE.B2609", "2026-08-11", asset_type="FUTURE")
        second_path = store.write_silver_bars(
            PartitionKey("CN", "FUTURE", "1d", 2026, "interleaved-b"),
            [second], second["bar_open_time"], run_id, update_query_cache=False,
        )
    finally:
        store.close()
    target = market_data_version(root)

    assert cache.refresh_partition_files(
        [second_path], expected_base_revision=base, target_revision=target,
    ) is False
    assert cache.status().ready is False
    assert cache.refresh_partition_files(
        [first_path, second_path], expected_base_revision=base, target_revision=target,
    ) is True
    assert cache.status().ready is True


def test_refresh_rejects_any_missing_declared_file_without_publishing_current(tmp_path: Path) -> None:
    cache = _store(tmp_path)
    from market_monitor.market_data_version import advance_market_data_version

    base, target = advance_market_data_version(cache.data_root)
    existing = next((cache.data_root / "silver").rglob("*.parquet"))
    missing = existing.with_name("missing-synthetic.parquet")
    assert cache.refresh_partition_files(
        [existing, missing], expected_base_revision=base, target_revision=target,
    ) is False
    assert cache.status().ready is False


def test_old_shadow_build_cannot_replace_a_newer_silver_revision(tmp_path: Path) -> None:
    cache = _store(tmp_path)
    old_revision = market_data_version(cache.data_root)
    building = cache._build_to_file(old_revision)
    try:
        advance_market_data_version(cache.data_root)
        with pytest.raises(RuntimeError, match="Silver revision changed"):
            cache._commit_build(old_revision, building)
        assert cache.status().revision == old_revision
        assert cache.status().ready is False
    finally:
        building.unlink(missing_ok=True)


def test_stale_index_never_reads_replaced_parquet_files(tmp_path: Path, monkeypatch) -> None:
    cache = _store(tmp_path)
    monkeypatch.setattr(cache, "_start_background_rebuild", lambda _revision: None)
    advance_market_data_version(cache.data_root)

    with pytest.raises(KLineCacheUnavailable, match="stale"):
        cache.read_before(INSTRUMENT, "1d", limit=2)
    assert cache.status().ready is False


def test_full_build_ignores_an_unpublished_silver_staging_file(tmp_path: Path, monkeypatch) -> None:
    from market_monitor import storage as storage_module

    cache = _store(tmp_path)
    root = cache.data_root
    entered, release = threading.Event(), threading.Event()
    original_sha = storage_module._sha256
    errors = []

    def pause_after_valid_parquet(path):
        digest = original_sha(path)
        entered.set()
        if not release.wait(5):
            raise TimeoutError("synthetic write was not released")
        return digest

    monkeypatch.setattr(storage_module, "_sha256", pause_after_valid_parquet)

    def write_next() -> None:
        try:
            store = MarketStore(root)
            try:
                run_id = store.begin_run("synthetic-staging-race")
                bar = silver_row(INSTRUMENT, "2026-08-11", asset_type="FUTURE", close=11.0)
                store.write_silver_bars(
                    PartitionKey("CN", "FUTURE", "1d", 2026, "synthetic-staging-race"),
                    [bar], bar["bar_open_time"], run_id,
                )
            finally:
                store.close()
        except Exception as error:
            errors.append(error)

    writer = threading.Thread(target=write_next)
    writer.start()
    try:
        assert entered.wait(5)
        staged = list((root / "silver").rglob("*.tmp"))
        assert len(staged) == 1
        assert cache.rebuild().rows == 12
    finally:
        release.set()
        writer.join(timeout=5)
    assert not writer.is_alive()
    assert errors == []
    assert cache.status().ready is True
    assert cache.status().rows == 13


def test_full_build_rejects_revision_between_invalidation_and_file_promotion(
    tmp_path: Path, monkeypatch,
) -> None:
    from market_monitor import storage as storage_module

    cache = _store(tmp_path)
    root = cache.data_root
    entered, release = threading.Event(), threading.Event()
    original_advance = storage_module.advance_market_data_version
    errors: list[Exception] = []

    def pause_after_invalidation(*args, **kwargs):
        result = original_advance(*args, **kwargs)
        entered.set()
        if not release.wait(5):
            raise TimeoutError("synthetic promotion was not released")
        return result

    monkeypatch.setattr(storage_module, "advance_market_data_version", pause_after_invalidation)

    def write_next() -> None:
        try:
            store = MarketStore(root)
            try:
                run_id = store.begin_run("synthetic-promotion-race")
                bar = silver_row(INSTRUMENT, "2026-08-11", asset_type="FUTURE", close=11.0)
                store.write_silver_bars(
                    PartitionKey("CN", "FUTURE", "1d", 2026, "synthetic-promotion-race"),
                    [bar], bar["bar_open_time"], run_id,
                )
            finally:
                store.close()
        except Exception as error:
            errors.append(error)

    writer = threading.Thread(target=write_next)
    writer.start()
    try:
        assert entered.wait(5)
        pending_revision = market_data_version(root)
        assert cache.status().ready is False
        with pytest.raises(KLineCacheUnavailable, match="pending"):
            cache._build_to_file(pending_revision)
        with pytest.raises(KLineCacheUnavailable, match="pending"):
            cache.ensure_ready()
    finally:
        release.set()
        writer.join(timeout=5)
    assert not writer.is_alive()
    assert errors == []
    assert cache.status().ready is True
    assert cache.status().rows == 13


def test_interrupted_post_promotion_settlement_keeps_query_index_stale(
    tmp_path: Path, monkeypatch,
) -> None:
    from market_monitor import storage as storage_module

    cache = _store(tmp_path)
    root = cache.data_root
    store = MarketStore(root)
    try:
        run_id = store.begin_run("synthetic-post-promotion-interruption")
        bar = silver_row(INSTRUMENT, "2026-08-11", asset_type="FUTURE", close=11.0)
        monkeypatch.setattr(
            storage_module, "settle_market_data_version",
            lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic interruption")),
        )
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            store.write_silver_bars(
                PartitionKey("CN", "FUTURE", "1d", 2026, "synthetic-interruption"),
                [bar], bar["bar_open_time"], run_id,
            )
    finally:
        store.close()

    assert cache.status().ready is False
    with pytest.raises(KLineCacheUnavailable, match="pending"):
        cache.ensure_ready()


def test_default_inventory_does_not_hide_instruments_after_the_legacy_cap(tmp_path: Path) -> None:
    """Discovery must include the complete local universe, not only the first 20k IDs."""

    data_root = tmp_path / "large-inventory"
    last_instrument = "GLOBAL.GLOBAL_INDEX.INDEX.ZZ999999"
    rows = [
        silver_row(f"CN.SSE.STOCK.{index:06d}", "2026-08-10")
        for index in range(20_000)
    ]
    rows.append(silver_row(last_instrument, "2026-08-10", market="GLOBAL", asset_type="INDEX"))
    write_silver(data_root, rows)
    store = KLineQueryStore(data_root)
    store.rebuild()

    inventory = load_inventory(data_root)

    assert len(inventory.instruments) == 20_001
    assert last_instrument in inventory.instruments
    assert len(store.inventory_snapshot(20_000)["items"]) == 20_000


def test_schema_one_full_json_cache_is_migrated_to_current_manifest(tmp_path: Path) -> None:
    data_root = tmp_path / "legacy"
    write_silver(data_root, [silver_row(INSTRUMENT, "2026-08-10", asset_type="FUTURE")])
    store = KLineQueryStore(data_root)
    store.path.parent.mkdir(parents=True)
    with duckdb.connect(str(store.path)) as connection:
        connection.execute(
            "CREATE TABLE cache_meta (schema_version INTEGER, revision VARCHAR, "
            "row_count BIGINT, built_at VARCHAR, build_seconds DOUBLE)"
        )
        connection.execute("INSERT INTO cache_meta VALUES (1, 'legacy', 999, NULL, NULL)")
        connection.execute("CREATE TABLE bars (bar_json VARCHAR)")

    store.ensure_ready()

    with duckdb.connect(str(store.path), read_only=True) as connection:
        tables = {str(row[0]) for row in connection.execute("SHOW TABLES").fetchall()}
        schema = int(connection.execute("SELECT schema_version FROM cache_meta").fetchone()[0])
    assert schema == QUERY_INDEX_SCHEMA_VERSION
    assert "instrument_file" in tables
    assert "bars" not in tables


def test_schema_two_manifest_requires_one_verified_rebuild(tmp_path: Path) -> None:
    prior = _store(tmp_path)
    with duckdb.connect(str(prior.path)) as connection:
        connection.execute("UPDATE cache_meta SET schema_version=2")

    store = KLineQueryStore(prior.data_root)  # A process restart loads the new cache policy.
    assert store.status().ready is False
    assert store.ensure_ready() == market_data_version(store.data_root)
    with duckdb.connect(str(store.path), read_only=True) as connection:
        assert connection.execute("SELECT schema_version FROM cache_meta").fetchone()[0] == QUERY_INDEX_SCHEMA_VERSION


def test_schema_three_manifest_without_catalog_digest_requires_rebuild(tmp_path: Path) -> None:
    prior = _store(tmp_path)
    with duckdb.connect(str(prior.path)) as connection:
        connection.execute("ALTER TABLE cache_meta DROP COLUMN catalog_digest")
        connection.execute("UPDATE cache_meta SET schema_version=3")

    store = KLineQueryStore(prior.data_root)
    assert store.status().ready is False
    assert store.ensure_ready() == market_data_version(store.data_root)
    with duckdb.connect(str(store.path), read_only=True) as connection:
        schema, digest = connection.execute(
            "SELECT schema_version,catalog_digest FROM cache_meta"
        ).fetchone()
    assert schema == QUERY_INDEX_SCHEMA_VERSION
    assert isinstance(digest, str) and len(digest) == 64


def test_catalog_manifest_delta_recovers_after_revision_journal_truncation(
    tmp_path: Path, monkeypatch,
) -> None:
    from market_monitor import market_data_version as version_module

    cache = _store(tmp_path)
    root = cache.data_root
    with duckdb.connect(str(cache.path), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM indexed_catalog_manifest").fetchone()[0] == 1
    base = market_data_version(root)
    monkeypatch.setattr(version_module, "_MAX_TRANSITIONS", 2)
    writer = MarketStore(root)
    try:
        run_id = writer.begin_run("synthetic-truncated-journal")
        for day in (11, 12, 13):
            row = silver_row(INSTRUMENT, f"2026-08-{day:02d}", asset_type="FUTURE", close=float(day))
            writer.write_silver_bars(
                PartitionKey("CN", "FUTURE", "1d", 2026, f"delta-{day}"),
                [row], row["bar_open_time"], run_id, update_query_cache=False,
            )
    finally:
        writer.close()
    target = market_data_version(root)
    assert version_module.changes_between(root, base, target) is None
    assert cache.status().ready is False
    assert cache.refresh_catalog_delta(target_revision=target) is True
    assert cache.status().ready is True
    assert [bar["close"] for bar in cache.read_bars(INSTRUMENT, "1d", 2)] == [12.0, 13.0]
    with duckdb.connect(str(cache.path), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM indexed_catalog_manifest").fetchone()[0] == 4


def test_corrupt_revision_state_cannot_reset_epoch_and_publish_catalog_delta(tmp_path: Path) -> None:
    """A lost write intent must not turn a mismatched old file into CURRENT."""

    from market_monitor.market_data_version import pending_market_writes

    cache = _store(tmp_path)
    root = cache.data_root
    writer = MarketStore(root)
    try:
        row = silver_row(INSTRUMENT, "2026-08-11", asset_type="FUTURE", close=11.0)
        run_id = writer.begin_run("synthetic-corrupt-revision")
        writer.write_silver_bars(
            PartitionKey("CN", "FUTURE", "1d", 2026, "new-after-index"),
            [row], row["bar_open_time"], run_id, update_query_cache=False,
        )
    finally:
        writer.close()

    old_file = root / "silver" / "fixture.parquet"
    old_sha = sha256(old_file.read_bytes()).hexdigest()
    replacement = root / "silver" / "replacement.tmp.parquet"
    source_sql = old_file.as_posix().replace("'", "''")
    replacement_sql = replacement.as_posix().replace("'", "''")
    with duckdb.connect(":memory:") as connection:
        connection.execute(
            f"COPY (SELECT * FROM read_parquet('{source_sql}')) "
            f"TO '{replacement_sql}' (FORMAT PARQUET, COMPRESSION UNCOMPRESSED)"
        )
    replacement.replace(old_file)
    assert sha256(old_file.read_bytes()).hexdigest() != old_sha

    revision_file = root / "state" / "market_data_version.json"
    revision_file.write_text("{damaged", encoding="utf-8")
    assert pending_market_writes(root)["status"] == "INVALID"
    revision = market_data_version(root)
    assert revision == "invalid-silver-revision-state"
    assert revision_file.read_text(encoding="utf-8") == "{damaged"
    assert cache.refresh_catalog_delta(target_revision=revision) is False
    assert cache.status().ready is False


def test_broken_revision_journal_with_valid_epoch_does_not_keep_index_ready(tmp_path: Path) -> None:
    cache = _store(tmp_path)
    root = cache.data_root
    state_file = root / "state" / "market_data_version.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert cache.status().ready is True
    state["journal"] = [{"previous": "missing", "current": "broken", "kind": "UNKNOWN"}]
    state_file.write_text(json.dumps(state), encoding="utf-8")

    assert market_data_version(root) == "invalid-silver-revision-state"
    assert cache.status().ready is False
    assert cache.refresh_catalog_delta() is False


def test_schema_five_index_requires_comparable_unknown_epoch(tmp_path: Path) -> None:
    cache = _store(tmp_path)
    root = cache.data_root
    state_file = root / "state" / "market_data_version.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert cache.status().ready is True
    state.pop("unknownEpoch")
    state_file.write_text(json.dumps(state), encoding="utf-8")

    assert cache.status().ready is False
    with pytest.raises(KLineCacheUnavailable):
        cache.ensure_ready(rebuild_allowed=False)


def test_explicit_rebuild_migrates_legacy_ledger_only_after_full_catalog_audit(tmp_path: Path) -> None:
    cache = _store(tmp_path)
    state_file = cache.data_root / "state" / "market_data_version.json"
    state_file.write_text(json.dumps({"schemaVersion": 1, "revision": "legacy"}), encoding="utf-8")
    assert cache.status().ready is False
    result = cache.rebuild()
    assert result.ready is True
    assert result.revision is not None and result.revision.startswith("migrated-")
    assert cache.read_bars(INSTRUMENT, "1d", 1)[0]["close"] == 10.0


def test_failed_background_index_build_reports_progress_and_backs_off(tmp_path: Path, monkeypatch) -> None:
    cache = _store(tmp_path)
    revision = advance_market_data_version(cache.data_root)[1]
    monkeypatch.setattr(cache, "upgrade_schema_four_manifest", lambda **_kwargs: False)
    monkeypatch.setattr(cache, "refresh_catalog_delta", lambda **_kwargs: False)

    def reject_build(_revision: str) -> Path:
        cache._update_progress("AUDIT_SHA", completed=2, total=5, unit="files")
        raise KLineCacheUnavailable("synthetic Catalog SHA mismatch")

    monkeypatch.setattr(cache, "_build_to_file", reject_build)
    cache._background_rebuild(revision)
    build = cache.status().build
    assert build is not None
    assert build["state"] == "FAILED"
    assert build["failureCategory"] == "AUDIT_FAILED"
    assert build["completed"] == 2 and build["total"] == 5
    assert 1 <= build["retryAfterSeconds"] <= 30
    assert cache._retry_due(revision) is False

    next_revision = advance_market_data_version(cache.data_root)[1]
    assert cache._retry_due(next_revision) is True
    assert cache._rebuild_failures == 0
    assert cache.status().build is None


def test_catalog_manifest_delta_removes_deleted_partition_and_recomputes_latest(tmp_path: Path) -> None:
    from market_monitor.market_data_version import settle_market_data_version

    cache = _store(tmp_path)
    root = cache.data_root
    writer = MarketStore(root)
    try:
        run_id = writer.begin_run("synthetic-delete")
        row = silver_row(INSTRUMENT, "2026-08-11", asset_type="FUTURE", close=11.0)
        path = writer.write_silver_bars(
            PartitionKey("CN", "FUTURE", "1d", 2026, "to-delete"),
            [row], row["bar_open_time"], run_id,
        )
        assert cache.status().ready is True
        old_sha = sha256(path.read_bytes()).hexdigest()
        _base, revision = advance_market_data_version(root, changed_file=path, sha256=old_sha, pending=True)
        path.unlink()
        writer.connection.execute("DELETE FROM partitions WHERE partition_id='to-delete'")
        settle_market_data_version(root, revision)
    finally:
        writer.close()
    assert cache.refresh_catalog_delta(target_revision=revision) is True
    assert cache.status().ready is True
    assert [bar["close"] for bar in cache.read_bars(INSTRUMENT, "1d", 1)] == [10.0]


def test_catalog_manifest_delta_rejects_unknown_write(tmp_path: Path) -> None:
    cache = _store(tmp_path)
    root = cache.data_root
    advance_market_data_version(root)
    assert cache.refresh_catalog_delta() is False
    assert cache.status().ready is False


def test_current_schema_four_index_adopts_catalog_manifest_without_bar_scan(
    tmp_path: Path, monkeypatch,
) -> None:
    prior = _store(tmp_path)
    with duckdb.connect(str(prior.path)) as connection:
        connection.execute("DROP TABLE indexed_catalog_manifest")
        connection.execute("ALTER TABLE cache_meta DROP COLUMN unknown_epoch")
        connection.execute("UPDATE cache_meta SET schema_version=4")
    store = KLineQueryStore(prior.data_root)
    monkeypatch.setattr(store, "_build_to_file", lambda *_args: pytest.fail("adoption must not scan bars"))
    assert store.upgrade_schema_four_manifest() is True
    assert store.status().ready is True
    with duckdb.connect(str(store.path), read_only=True) as connection:
        assert connection.execute("SELECT schema_version FROM cache_meta").fetchone()[0] == 5
        assert connection.execute("SELECT count(*) FROM indexed_catalog_manifest").fetchone()[0] == 1


def test_catalog_delta_rejects_unregistered_parquet_and_keeps_old_index(tmp_path: Path) -> None:
    cache = _store(tmp_path)
    root = cache.data_root
    writer = MarketStore(root)
    try:
        run_id = writer.begin_run("synthetic-orphan")
        row = silver_row(INSTRUMENT, "2026-08-11", asset_type="FUTURE", close=11.0)
        writer.write_silver_bars(
            PartitionKey("CN", "FUTURE", "1d", 2026, "new"),
            [row], row["bar_open_time"], run_id, update_query_cache=False,
        )
    finally:
        writer.close()
    (root / "silver" / "orphan.parquet").write_bytes((root / "silver" / "fixture.parquet").read_bytes())
    assert cache.refresh_catalog_delta() is False
    assert cache.status().ready is False


def test_rebuild_guard_defers_chart_recovery_during_source_update(
    tmp_path: Path, monkeypatch,
) -> None:
    cache = _store(tmp_path)
    advance_market_data_version(cache.data_root)
    monkeypatch.setattr(cache, "_start_background_rebuild", lambda *_args: pytest.fail("rebuild deferred"))
    cache.set_rebuild_allowed(lambda: False)
    with pytest.raises(KLineCacheUnavailable):
        cache.ensure_ready()
    with pytest.raises(KLineCacheUnavailable):
        cache.ensure_ready(rebuild_allowed=False)


@pytest.mark.parametrize("corruption", ("missing", "wrong_sha", "wrong_count", "orphan"))
def test_full_rebuild_fails_closed_without_complete_catalog_evidence(
    tmp_path: Path, corruption: str,
) -> None:
    store = _store(tmp_path)
    root = store.data_root
    fixture_path = root / "silver" / "fixture.parquet"
    if corruption == "orphan":
        (root / "silver" / "orphan.parquet").write_bytes(fixture_path.read_bytes())
    else:
        with duckdb.connect(str(root / "catalog.duckdb")) as connection:
            if corruption == "missing":
                connection.execute("DELETE FROM partitions")
            elif corruption == "wrong_sha":
                connection.execute("UPDATE partitions SET sha256=?", ["0" * 64])
            else:
                connection.execute("UPDATE partitions SET row_count=row_count+1")
    advance_market_data_version(root)
    with pytest.raises(KLineCacheUnavailable, match="Catalog audit did not pass"):
        store.rebuild()
    assert store.status().ready is False


def test_full_rebuild_refuses_missing_catalog(tmp_path: Path) -> None:
    root = tmp_path / "no-catalog"
    write_silver(root, [silver_row(INSTRUMENT, "2026-08-10", asset_type="FUTURE")])
    (root / "catalog.duckdb").unlink()
    store = KLineQueryStore(root)
    with pytest.raises(KLineCacheUnavailable, match="Catalog unavailable"):
        store.rebuild()
    assert not store.path.exists()


def test_full_rebuild_refuses_index_that_drops_a_catalog_row(tmp_path: Path) -> None:
    root = tmp_path / "invalid-physical-row"
    catalog = MarketStore(root)
    catalog.close()
    path = root / "silver" / "fixture.parquet"
    path.parent.mkdir(parents=True)
    with duckdb.connect(":memory:") as connection:
        connection.execute("CREATE TABLE source (instrument_id VARCHAR, period VARCHAR, "
                           "bar_open_time VARCHAR, bar_json VARCHAR)")
        connection.execute("INSERT INTO source VALUES (NULL, '1d', '2026-08-10T09:30:00', '{}')")
        connection.execute("COPY source TO ? (FORMAT PARQUET)", [str(path)])
    digest = sha256(path.read_bytes()).hexdigest()
    with duckdb.connect(str(root / "catalog.duckdb")) as connection:
        connection.execute(
            "INSERT INTO partitions VALUES ('synthetic-invalid', 'silver/fixture.parquet', 1, "
            "'2026-08-10T09:30:00', ?, 'synthetic', 'COMPLETE', '2026-08-10T15:00:00')",
            [digest],
        )
    store = KLineQueryStore(root)
    with pytest.raises(RuntimeError, match="Catalog or revision changed"):
        store.rebuild()
    assert store.status().ready is False


def test_full_build_rejects_catalog_change_after_audit_before_publication(tmp_path: Path) -> None:
    store = _store(tmp_path)
    revision = market_data_version(store.data_root)
    building = store._build_to_file(revision)
    try:
        with duckdb.connect(str(store.data_root / "catalog.duckdb")) as connection:
            connection.execute("UPDATE partitions SET status='BROKEN'")
        with pytest.raises(RuntimeError, match="Catalog or revision changed"):
            store._commit_build(revision, building)
    finally:
        building.unlink(missing_ok=True)


@pytest.mark.parametrize("corruption", ("missing", "wrong_sha", "wrong_count", "incomplete", "duplicate"))
def test_incremental_refresh_rejects_changed_partition_without_matching_catalog_evidence(
    tmp_path: Path, corruption: str,
) -> None:
    cache = _store(tmp_path)
    root = cache.data_root
    base = market_data_version(root)
    writer = MarketStore(root)
    try:
        run_id = writer.begin_run("synthetic-catalog-evidence")
        bar = silver_row(INSTRUMENT, "2026-08-11", asset_type="FUTURE", close=11.0)
        path = writer.write_silver_bars(
            PartitionKey("CN", "FUTURE", "1d", 2026, "catalog-evidence"),
            [bar], bar["bar_open_time"], run_id, update_query_cache=False,
        )
        target = market_data_version(root)
        if corruption == "missing":
            writer.connection.execute("DELETE FROM partitions WHERE partition_id='catalog-evidence'")
        elif corruption == "wrong_sha":
            writer.connection.execute(
                "UPDATE partitions SET sha256=? WHERE partition_id='catalog-evidence'", ["0" * 64],
            )
        elif corruption == "wrong_count":
            writer.connection.execute(
                "UPDATE partitions SET row_count=row_count+1 WHERE partition_id='catalog-evidence'"
            )
        elif corruption == "incomplete":
            writer.connection.execute(
                "UPDATE partitions SET status='PENDING' WHERE partition_id='catalog-evidence'"
            )
        else:
            writer.connection.execute(
                "INSERT INTO partitions SELECT 'catalog-evidence-copy',file_path,row_count,data_cutoff,"
                "sha256,source_run_id,status,updated_at FROM partitions "
                "WHERE partition_id='catalog-evidence'"
            )
    finally:
        writer.close()

    assert cache.refresh_partition_files(
        [path], expected_base_revision=base, target_revision=target,
    ) is False
    assert cache.status().ready is False


def test_incremental_refresh_rolls_back_when_catalog_digest_changes_before_commit(
    tmp_path: Path, monkeypatch,
) -> None:
    from market_monitor import market_query_cache as query_cache

    cache = _store(tmp_path)
    root = cache.data_root
    base = market_data_version(root)
    writer = MarketStore(root)
    try:
        run_id = writer.begin_run("synthetic-catalog-change")
        bar = silver_row(INSTRUMENT, "2026-08-11", asset_type="FUTURE", close=11.0)
        path = writer.write_silver_bars(
            PartitionKey("CN", "FUTURE", "1d", 2026, "catalog-change"),
            [bar], bar["bar_open_time"], run_id, update_query_cache=False,
        )
    finally:
        writer.close()
    target = market_data_version(root)
    original = query_cache._catalog_snapshot
    calls = 0

    def changed_catalog(data_root):
        nonlocal calls
        calls += 1
        digest, rows = original(data_root)
        return (("0" * 64) if calls == 2 else digest), rows

    monkeypatch.setattr(query_cache, "_catalog_snapshot", changed_catalog)
    assert cache.refresh_partition_files(
        [path], expected_base_revision=base, target_revision=target,
    ) is False
    assert calls == 2
    assert cache.status().ready is False
    with duckdb.connect(str(cache.path), read_only=True) as connection:
        assert connection.execute("SELECT revision FROM cache_meta").fetchone()[0] == base


def test_read_path_does_not_repeat_full_catalog_audit(tmp_path: Path, monkeypatch) -> None:
    from market_monitor import market_query_cache as query_cache

    store = _store(tmp_path)
    monkeypatch.setattr(
        query_cache, "audit_silver_catalog",
        lambda *_args, **_kwargs: pytest.fail("GET must not repeat full SHA audit"),
    )
    assert store.status().ready is True
    assert store.read_before(INSTRUMENT, "1d", limit=2).total == 10


def test_common_cursor_api_uses_same_window_contract(tmp_path: Path) -> None:
    store = _store(tmp_path)

    window = read_bars_before(store.data_root, INSTRUMENT, period="1d", limit=4)

    assert [bar["close"] for bar in window.bars] == [7.0, 8.0, 9.0, 10.0]
    assert window.before == "2026-08-07T09:30:00"
    assert window.has_more is True


def test_visible_card_tails_are_batched_and_chronological(tmp_path: Path) -> None:
    store = _store(tmp_path)

    tails = store.read_tails([INSTRUMENT, OTHER_INSTRUMENT], "1d", limit=2)

    assert [bar["close"] for bar in tails[INSTRUMENT]] == [9.0, 10.0]
    assert [bar["close"] for bar in tails[OTHER_INSTRUMENT]] == [90.0, 100.0]


@pytest.mark.parametrize("reader", ("bars", "tails", "window", "before"))
def test_parquet_read_rejects_revision_changed_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: str,
) -> None:
    from market_monitor import market_query_cache as query_cache

    store = _store(tmp_path)
    original = query_cache._read_parquet_rows
    captured = market_data_version(store.data_root)
    calls = 0

    def change_after_read(*args, **kwargs):
        nonlocal calls
        rows = original(*args, **kwargs)
        calls += 1
        # A batch must cover all requested instruments, not only the first.
        if calls == (2 if reader == "tails" else 1):
            advance_market_data_version(store.data_root)
        return rows

    monkeypatch.setattr(query_cache, "_read_parquet_rows", change_after_read)
    with pytest.raises(KLineCacheUnavailable, match="revision changed"):
        if reader == "bars":
            store.read_bars(INSTRUMENT, "1d", 2)
        elif reader == "tails":
            store.read_tails([INSTRUMENT, OTHER_INSTRUMENT], "1d", 2)
        elif reader == "window":
            store.read_window(INSTRUMENT, "1d", 8, 2)
        else:
            store.read_before(INSTRUMENT, "1d", limit=2)

    assert calls == (2 if reader == "tails" else 1)
    assert not any(key[1] == captured for key in store._hot)


def test_hot_hit_and_empty_read_recheck_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    assert store.read_bars(INSTRUMENT, "1d", 2)
    original_hot_get = store._hot_get

    def invalidate_during_hot_get(key):
        value = original_hot_get(key)
        advance_market_data_version(store.data_root)
        return value

    monkeypatch.setattr(store, "_hot_get", invalidate_during_hot_get)
    with pytest.raises(KLineCacheUnavailable, match="revision changed"):
        store.read_bars(INSTRUMENT, "1d", 2)

    fresh = _store(tmp_path / "empty")
    original_files = fresh._files

    def invalidate_empty_read(instrument_id, period):
        files = original_files(instrument_id, period)
        advance_market_data_version(fresh.data_root)
        return files

    monkeypatch.setattr(fresh, "_files", invalidate_empty_read)
    with pytest.raises(KLineCacheUnavailable, match="revision changed"):
        fresh.read_bars("CN.DCE.FUTURE.UNKNOWN", "1d", 2)


def test_revision_change_during_hot_put_discards_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    captured = market_data_version(store.data_root)
    original_hot_put = store._hot_put

    def invalidate_after_hot_put(key, value):
        original_hot_put(key, value)
        advance_market_data_version(store.data_root)

    monkeypatch.setattr(store, "_hot_put", invalidate_after_hot_put)
    with pytest.raises(KLineCacheUnavailable, match="revision changed"):
        store.read_bars(INSTRUMENT, "1d", 2)
    assert not any(key[1] == captured for key in store._hot)


def test_overlapping_import_partitions_return_one_latest_bar_per_timestamp(tmp_path: Path) -> None:
    data_root = tmp_path / "duplicates"
    older = silver_row(INSTRUMENT, "2026-08-09", asset_type="FUTURE", close=9.0)
    older["fetched_at"] = "2026-08-09T15:00:00+08:00"
    corrected = silver_row(INSTRUMENT, "2026-08-09", asset_type="FUTURE", close=9.5)
    corrected["fetched_at"] = "2026-08-10T15:00:00+08:00"
    latest = silver_row(INSTRUMENT, "2026-08-10", asset_type="FUTURE", close=10.0)
    write_silver(data_root, [older, corrected, latest])
    store = KLineQueryStore(data_root)
    store.rebuild()

    window = store.read_before(INSTRUMENT, "1d", limit=10)

    assert [bar["close"] for bar in window.bars] == [9.5, 10.0]
    assert len({bar["bar_open_time"] for bar in window.bars}) == 2


def test_large_first_run_starts_background_build_instead_of_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = KLineQueryStore(tmp_path / "large")
    silver = store.data_root / "silver"
    silver.mkdir(parents=True)
    for index in range(33):
        (silver / f"{index}.parquet").touch()
    started = threading.Event()

    def fake_rebuild(_revision: str) -> None:
        started.set()

    monkeypatch.setattr(store, "_background_rebuild", fake_rebuild)
    before = time.perf_counter()

    with pytest.raises(KLineCacheUnavailable):
        store.ensure_ready()

    assert time.perf_counter() - before < 0.5
    assert started.wait(1)


def test_build_lease_is_non_blocking_across_store_instances(tmp_path: Path) -> None:
    first = KLineQueryStore(tmp_path / "data")
    second = KLineQueryStore(tmp_path / "data")

    first_lease = first._claim_build_lease()
    assert first_lease is not None
    try:
        assert second._claim_build_lease() is None
    finally:
        first._release_build_lease(first_lease)

    second_lease = second._claim_build_lease()
    assert second_lease is not None
    second._release_build_lease(second_lease)


def test_dead_owned_build_file_does_not_block_future_rebuild(tmp_path: Path, monkeypatch) -> None:
    from market_monitor import market_query_cache as cache

    store = cache.KLineQueryStore(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    owned = store.path.with_name(store.path.name + ".building-1234-99")
    owned.write_bytes(b"synthetic abandoned building file")
    monkeypatch.setattr(cache, "_process_is_running", lambda pid: False)
    lease = store._claim_build_lease()
    assert lease is not None
    store._release_build_lease(lease)
    monkeypatch.setattr(cache, "_process_is_running", lambda pid: True)
    assert store._claim_build_lease() is None
