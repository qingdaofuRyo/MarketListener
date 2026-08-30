"""Focused tests for low-latency K-line windows."""

from __future__ import annotations

from pathlib import Path
import threading
import time

import duckdb
import pytest

from market_monitor.market_query_cache import (
    KLineCacheUnavailable,
    KLineQueryStore,
)
from market_monitor.web_api.common import load_inventory, read_bars_before
from web_fixtures import silver_row, write_silver


INSTRUMENT = "CN.DCE.FUTURE.JM2609"
OTHER_INSTRUMENT = "CN.DCE.FUTURE.PS2609"


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


def test_schema_one_full_json_cache_is_migrated_to_manifest_v2(tmp_path: Path) -> None:
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
    assert schema == 2
    assert "instrument_file" in tables
    assert "bars" not in tables


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
