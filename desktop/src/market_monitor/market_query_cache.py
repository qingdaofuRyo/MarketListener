"""Low-latency local K-line query cache.

Silver Parquet files remain the immutable source of truth.  This module builds
a lightweight DuckDB file manifest so a chart opens only the handful of files
that contain its instrument and period.  K-line JSON is never duplicated into
the manifest.  A bounded in-memory window cache handles repeated chart and
thumbnail requests.
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterable, Mapping

import duckdb

from .market_data_version import market_data_version


_SCHEMA_VERSION = 2
_MAX_HOT_WINDOWS = 4096
_INLINE_BUILD_FILE_LIMIT = 32
_STORES: dict[str, "KLineQueryStore"] = {}
_STORES_LOCK = threading.RLock()


class KLineCacheUnavailable(RuntimeError):
    """The optional on-disk cache is not ready yet.

    Request handlers can catch this and use the immutable Parquet store while
    the cache is prepared in the background.  In particular, opening the first
    chart must not synchronously build a multi-gigabyte JSON database.
    """


@dataclass(frozen=True)
class KLineWindow:
    """A chronological page and its stable time cursor."""

    bars: list[dict[str, Any]]
    total: int
    before: str | None
    has_more: bool


@dataclass(frozen=True)
class KLineCacheStatus:
    ready: bool
    revision: str | None
    rows: int
    built_at: str | None
    build_seconds: float | None
    hot_hits: int
    hot_misses: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "revision": self.revision,
            "rows": self.rows,
            "builtAt": self.built_at,
            "buildSeconds": self.build_seconds,
            "hotHits": self.hot_hits,
            "hotMisses": self.hot_misses,
        }


class KLineQueryStore:
    """A thread-safe Parquet file index backed by the local Silver store."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root)
        self.path = self.data_root / "state" / "kline_query.duckdb"
        self.build_lock_path = self.path.with_name(f"{self.path.name}.build.lock")
        self._lock = threading.RLock()
        self._hot: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
        self._hot_revision: str | None = None
        self._ready_revision: str | None = None
        self._hot_hits = 0
        self._hot_misses = 0
        self._rebuilding = False
        self._next_rebuild_attempt = 0.0

    def status(self) -> KLineCacheStatus:
        current = market_data_version(self.data_root)
        # Status is part of page bootstrap and must never wait behind a
        # multi-gigabyte initial/rebuild transaction.  A busy store reports
        # "not ready" while normal market/instrument endpoints remain usable.
        if not self._lock.acquire(blocking=False):
            return KLineCacheStatus(
                False, self._ready_revision, 0, None, None, self._hot_hits, self._hot_misses
            )
        try:
            metadata = self._metadata()
            if metadata is None:
                return KLineCacheStatus(False, None, 0, None, None, self._hot_hits, self._hot_misses)
            revision, rows, built_at, seconds = metadata
            return KLineCacheStatus(revision == current, revision, rows, built_at, seconds, self._hot_hits, self._hot_misses)
        finally:
            self._lock.release()

    def rebuild(self) -> KLineCacheStatus:
        """Build the ordered query cache from immutable Parquet once."""

        revision = market_data_version(self.data_root)
        with self._lock:
            if self._rebuilding:
                return self.status()
            lease = self._claim_build_lease()
            if lease is None:
                return self.status()
            try:
                self._build_locked(revision)
            finally:
                self._release_build_lease(lease)
            return self.status()

    def ensure_ready(self) -> str:
        revision = market_data_version(self.data_root)
        with self._lock:
            if self._ready_revision == revision:
                self._set_hot_revision(revision)
                return revision
            metadata = self._metadata()
            if metadata is not None and metadata[0] == revision:
                self._ready_revision = revision
                self._set_hot_revision(revision)
                return revision
            if metadata is not None:
                # Serve the stale cache immediately while rebuilding in the
                # background so chart requests do not block on a full rebuild.
                stale_revision = str(metadata[0])
                self._ready_revision = stale_revision
                self._set_hot_revision(stale_revision)
                if not self._rebuilding and time.monotonic() >= self._next_rebuild_attempt:
                    self._start_background_rebuild(revision)
                return stale_revision
            # Tiny fixture/development stores are cheaper to build than to
            # open through the fallback.  A real store is always prepared in
            # the background so the first chart request is never the cache
            # build job.
            if self._should_build_inline():
                lease = self._claim_build_lease()
                if lease is not None:
                    try:
                        self._build_locked(revision)
                    finally:
                        self._release_build_lease(lease)
                    self._ready_revision = revision
                    self._set_hot_revision(revision)
                    return revision
            if not self._rebuilding and time.monotonic() >= self._next_rebuild_attempt:
                self._start_background_rebuild(revision)
            raise KLineCacheUnavailable("K-line query cache is being prepared")

    def _should_build_inline(self) -> bool:
        files = self.data_root.joinpath("silver").rglob("*.parquet")
        return sum(1 for _, _path in zip(range(_INLINE_BUILD_FILE_LIMIT + 1), files)) <= _INLINE_BUILD_FILE_LIMIT

    def _start_background_rebuild(self, revision: str) -> None:
        self._rebuilding = True
        self._next_rebuild_attempt = time.monotonic() + 5.0
        thread = threading.Thread(
            target=self._background_rebuild,
            args=(revision,),
            daemon=True,
            name=f"kline-cache-{os.getpid()}",
        )
        thread.start()

    def _background_rebuild(self, revision: str) -> None:
        building: Path | None = None
        lease = self._claim_build_lease()
        if lease is None:
            with self._lock:
                self._rebuilding = False
            return
        try:
            building = self._build_to_file(revision)
            with self._lock:
                self._commit_build(revision, building)
                building = None
        except Exception:
            if building is not None:
                building.unlink(missing_ok=True)
        finally:
            self._release_build_lease(lease)
            with self._lock:
                self._rebuilding = False

    def read_bars(self, instrument_id: str, period: str | None, limit: int) -> list[dict[str, Any]]:
        revision = self.ensure_ready()
        safe_limit = max(1, min(int(limit), 5_000))
        key = ("tail", revision, instrument_id, period or "", safe_limit)
        cached = self._hot_get(key)
        if cached is not None:
            return cached
        files = self._files(instrument_id, period)
        if not files:
            return []
        rows = _read_parquet_rows(
            files, instrument_id, period, descending=True, limit=safe_limit
        )
        bars = [_decode_bar(row[1], instrument_id) for row in reversed(rows)]
        result = [bar for bar in bars if bar is not None]
        self._hot_put(key, result)
        return deepcopy(result)

    def read_tails(
        self, instrument_ids: Iterable[str], period: str, limit: int
    ) -> dict[str, list[dict[str, Any]]]:
        """Read recent bars for visible cards with indexed point queries.

        A window function over a large period scans and ranks every matching
        row before discarding almost all of them.  Visible pages contain only
        a small number of instruments, so reusing one connection for indexed
        top-N lookups is substantially cheaper and starts returning at once.
        """

        revision = self.ensure_ready()
        requested = tuple(dict.fromkeys(str(value) for value in instrument_ids if value))
        safe_limit = max(1, min(int(limit), 120))
        if not requested:
            return {}
        key = ("tails", revision, requested, period, safe_limit)
        cached = self._hot_get(key)
        if cached is not None:
            return cached
        result: dict[str, list[dict[str, Any]]] = {instrument_id: [] for instrument_id in requested}
        for instrument_id in requested:
            files = self._files(instrument_id, period)
            if files:
                rows = _read_parquet_rows(
                    files, instrument_id, period, descending=True, limit=safe_limit
                )
                decoded = [_decode_bar(row[1], instrument_id) for row in reversed(rows)]
                result[instrument_id] = [bar for bar in decoded if bar is not None]
        self._hot_put(key, result)
        return deepcopy(result)

    def read_window(self, instrument_id: str, period: str, start: int, limit: int) -> tuple[list[dict[str, Any]], int]:
        revision = self.ensure_ready()
        safe_start = max(0, int(start))
        safe_limit = max(1, min(int(limit), 5_000))
        key = ("window", revision, instrument_id, period, safe_start, safe_limit)
        cached = self._hot_get(key)
        if cached is not None:
            return cached
        files = self._files(instrument_id, period)
        with self._lock:
            with self._connect(read_only=True) as connection:
                metadata = connection.execute(
                    "SELECT row_count FROM instrument_period WHERE instrument_id = ? AND period = ?",
                    [instrument_id, period],
                ).fetchone()
                total = int(metadata[0]) if metadata else 0
        if not files or not total:
            return [], total
        end = min(total, safe_start + safe_limit)
        rows_after = max(0, total - end)
        if rows_after < safe_start:
            rows = _read_parquet_rows(
                files,
                instrument_id,
                period,
                descending=True,
                limit=end - safe_start,
                offset=rows_after,
            )
            rows.reverse()
        else:
            rows = _read_parquet_rows(
                files,
                instrument_id,
                period,
                descending=False,
                limit=safe_limit,
                offset=safe_start,
            )
        result = ([bar for row in rows if (bar := _decode_bar(row[1], instrument_id)) is not None], total)
        self._hot_put(key, result)
        return deepcopy(result)

    def read_before(
        self,
        instrument_id: str,
        period: str,
        *,
        before: str | None = None,
        limit: int = 120,
    ) -> KLineWindow:
        """Read a descending-keyset page and return it chronologically.

        ``before`` is the first bar time of the already loaded client window.
        It remains stable when new bars arrive and avoids increasingly costly
        OFFSET scans while a user drags toward older history.
        """

        revision = self.ensure_ready()
        safe_limit = max(1, min(int(limit), 5_000))
        cursor = _text(before)
        key = ("before", revision, instrument_id, period, cursor or "", safe_limit)
        cached = self._hot_get(key)
        if cached is not None:
            return cached
        files = self._files(instrument_id, period)
        with self._lock:
            with self._connect(read_only=True) as connection:
                metadata = connection.execute(
                    "SELECT row_count FROM instrument_period WHERE instrument_id = ? AND period = ?",
                    [instrument_id, period],
                ).fetchone()
                total = int(metadata[0]) if metadata else 0
        if not files:
            return KLineWindow([], total, cursor, False)
        rows = _read_parquet_rows(
            files,
            instrument_id,
            period,
            before=cursor,
            descending=True,
            limit=safe_limit + 1,
        )
        has_more = len(rows) > safe_limit
        selected = rows[:safe_limit]
        selected.reverse()
        bars = [
            bar
            for _opened, payload in selected
            if (bar := _decode_bar(payload, instrument_id)) is not None
        ]
        result = KLineWindow(
            bars=bars,
            total=total,
            before=_text(selected[0][0]) if selected else cursor,
            has_more=has_more,
        )
        self._hot_put(key, result)
        return deepcopy(result)

    def bounds(self, instrument_id: str, period: str) -> tuple[int, str | None, str | None]:
        revision = self.ensure_ready()
        key = ("bounds", revision, instrument_id, period)
        cached = self._hot_get(key)
        if cached is not None:
            return cached
        with self._lock:
            with self._connect(read_only=True) as connection:
                row = connection.execute(
                    "SELECT row_count, earliest_bar_at, latest_bar_at FROM instrument_period "
                    "WHERE instrument_id = ? AND period = ?",
                    [instrument_id, period],
                ).fetchone()
        result = (int(row[0]), _text(row[1]), _text(row[2])) if row else (0, None, None)
        self._hot_put(key, result)
        return result

    def periods(self, instrument_id: str) -> list[str]:
        revision = self.ensure_ready()
        key = ("periods", revision, instrument_id)
        cached = self._hot_get(key)
        if cached is not None:
            return cached
        with self._lock:
            with self._connect(read_only=True) as connection:
                rows = connection.execute(
                    "SELECT period FROM instrument_period WHERE instrument_id = ? ORDER BY period",
                    [instrument_id],
                ).fetchall()
        result = [str(row[0]) for row in rows]
        self._hot_put(key, result)
        return list(result)

    def periods_many(self, instrument_ids: Iterable[str]) -> dict[str, list[str]]:
        """Return stored periods for a page of physical instruments at once."""

        revision = self.ensure_ready()
        requested = tuple(dict.fromkeys(str(value) for value in instrument_ids if value))
        if not requested:
            return {}
        key = ("periods-many", revision, requested)
        cached = self._hot_get(key)
        if cached is not None:
            return cached
        placeholders = ",".join("?" for _ in requested)
        with self._lock:
            with self._connect(read_only=True) as connection:
                rows = connection.execute(
                    f"SELECT instrument_id, period FROM instrument_period "
                    f"WHERE instrument_id IN ({placeholders}) ORDER BY instrument_id, period",
                    list(requested),
                ).fetchall()
        result: dict[str, list[str]] = {instrument_id: [] for instrument_id in requested}
        for instrument_id, period in rows:
            result.setdefault(str(instrument_id), []).append(str(period))
        self._hot_put(key, result)
        return deepcopy(result)

    def _files(self, instrument_id: str, period: str | None) -> list[str]:
        """Resolve an instrument to a small set of immutable Parquet files."""

        if period:
            query = (
                "SELECT file_path FROM instrument_file WHERE instrument_id = ? AND period = ? "
                "ORDER BY earliest_bar_at"
            )
            parameters: list[str] = [instrument_id, period]
        else:
            query = (
                "SELECT file_path FROM instrument_file WHERE instrument_id = ? "
                "ORDER BY earliest_bar_at"
            )
            parameters = [instrument_id]
        with self._lock:
            with self._connect(read_only=True) as connection:
                rows = connection.execute(query, parameters).fetchall()
        return [str(row[0]) for row in rows if row and row[0]]

    def inventory_snapshot(self, max_instruments: int | None = None) -> dict[str, Any]:
        """Return the latest-bar index used by local market discovery.

        ``None`` intentionally means every local instrument.  The inventory is
        also the canonical lookup used by chart routes and the pending-review
        table, so silently taking the first N sorted identifiers can hide an
        otherwise valid source merely because a large A-share universe sorts
        before it.
        """

        revision = self.ensure_ready()
        safe_limit = max(1, int(max_instruments)) if max_instruments is not None else None
        key = ("inventory", revision, safe_limit or 0)
        cached = self._hot_get(key)
        if cached is not None:
            return cached
        with self._lock:
            with self._connect(read_only=True) as connection:
                summary = connection.execute(
                    "SELECT coalesce(sum(row_count), 0), max(latest_bar_at), list(DISTINCT period) "
                    "FROM instrument_period"
                ).fetchone()
                inventory_query = (
                    "SELECT instrument_id, market, asset_type, period, bar_open_time, bar_json "
                    "FROM instrument_latest ORDER BY instrument_id"
                )
                rows = (
                    connection.execute(f"{inventory_query} LIMIT ?", [safe_limit]).fetchall()
                    if safe_limit is not None
                    else connection.execute(inventory_query).fetchall()
                )
        result = {
            "rows": int(summary[0] or 0) if summary else 0,
            "latestBarAt": _text(summary[1]) if summary else None,
            "periods": [str(item) for item in (summary[2] or []) if item] if summary else [],
            "items": [tuple(row) for row in rows],
        }
        self._hot_put(key, result)
        return deepcopy(result)

    def get_derived(self, instrument_id: str, period: str) -> list[dict[str, Any]] | None:
        revision = self.ensure_ready()
        key = ("derived", revision, instrument_id, period)
        cached = self._hot_get(key)
        if cached is not None:
            return cached
        with self._lock:
            with self._connect(read_only=True) as connection:
                row = connection.execute(
                    "SELECT payload FROM derived_bars WHERE instrument_id = ? AND period = ? AND revision = ?",
                    [instrument_id, period, revision],
                ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(str(row[0]))
        except ValueError:
            return None
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            return None
        result = [dict(item) for item in value]
        self._hot_put(key, result)
        return deepcopy(result)

    def put_derived(self, instrument_id: str, period: str, bars: Iterable[Mapping[str, Any]]) -> None:
        revision = self.ensure_ready()
        rows = [dict(bar) for bar in bars]
        payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM derived_bars WHERE instrument_id = ? AND period = ?",
                    [instrument_id, period],
                )
                connection.execute(
                    "INSERT INTO derived_bars VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        instrument_id,
                        period,
                        revision,
                        payload,
                        len(rows),
                        _text(rows[0].get("bar_open_time")) if rows else None,
                        _text(rows[-1].get("bar_open_time")) if rows else None,
                    ],
                )
        self._hot_put(("derived", revision, instrument_id, period), rows)

    def apply_silver_update(
        self,
        previous_revision: str,
        current_revision: str,
        bars: Iterable[Mapping[str, Any]],
    ) -> bool:
        """Refresh only the manifest entries touched by one Silver write."""

        normalized = [_cache_row(bar) for bar in bars]
        normalized = [row for row in normalized if row is not None]
        if not normalized:
            return False
        with self._lock:
            metadata = self._metadata()
            if metadata is None or metadata[0] != previous_revision:
                return False
            changed_files = self._locate_updated_files(normalized)
            if not changed_files:
                return False
            affected = sorted({(row[0], row[1]) for row in normalized})
            instrument_ids = sorted({row[0] for row in normalized})
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN TRANSACTION")
                    try:
                        for path in changed_files:
                            connection.execute(
                                "DELETE FROM instrument_file WHERE file_path = ?", [path]
                            )
                            connection.execute(
                                "INSERT INTO instrument_file "
                                "SELECT CAST(instrument_id AS VARCHAR), CAST(period AS VARCHAR), "
                                "CAST(filename AS VARCHAR), count(*)::BIGINT, min(bar_open_time), "
                                "max(bar_open_time), any_value(market), any_value(asset_type) "
                                "FROM read_parquet(?, filename=true, union_by_name=true, hive_partitioning=true) "
                                "GROUP BY instrument_id, period, filename",
                                [path],
                            )
                        for instrument_id, period in affected:
                            connection.execute(
                                "DELETE FROM instrument_period WHERE instrument_id = ? AND period = ?",
                                [instrument_id, period],
                            )
                            connection.execute(
                                "INSERT INTO instrument_period SELECT instrument_id, period, "
                                "sum(row_count)::BIGINT, min(earliest_bar_at), max(latest_bar_at) "
                                "FROM instrument_file WHERE instrument_id = ? AND period = ? "
                                "GROUP BY instrument_id, period",
                                [instrument_id, period],
                            )
                        for instrument_id in instrument_ids:
                            # The overview's one-row snapshot may stay stale
                            # until the asynchronous manifest refresh.  Chart
                            # bodies use ``instrument_file`` immediately and
                            # must not be delayed by rebuilding overview JSON.
                            connection.execute(
                                "DELETE FROM derived_bars WHERE instrument_id = ?", [instrument_id]
                            )
                        row_count = int(
                            connection.execute(
                                "SELECT coalesce(sum(row_count), 0) FROM instrument_period"
                            ).fetchone()[0]
                        )
                        connection.execute("DELETE FROM cache_meta")
                        connection.execute(
                            "INSERT INTO cache_meta VALUES (?, ?, ?, ?, ?)",
                            [_SCHEMA_VERSION, current_revision, row_count, _now(), 0.0],
                        )
                        connection.execute("COMMIT")
                    except Exception:
                        connection.execute("ROLLBACK")
                        raise
            except Exception:
                return False
            self._hot.clear()
            self._hot_revision = current_revision
            self._ready_revision = current_revision
            return True

    def _locate_updated_files(
        self, rows: list[tuple[str, str, str, str, str, str]]
    ) -> list[str]:
        directories: set[Path] = set()
        for _instrument_id, period, opened, market, asset_type, _payload in rows:
            year = opened[:4]
            directories.add(
                self.data_root
                / "silver"
                / f"market={market}"
                / f"asset_type={asset_type}"
                / f"period={period}"
                / f"year={year}"
            )
        candidates: list[Path] = []
        for directory in directories:
            ranked = sorted(
                directory.glob("*.parquet"),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
            candidates.extend(ranked[:16])
        if not candidates:
            return []
        instruments = sorted({row[0] for row in rows})
        opened_values = sorted({row[2] for row in rows})
        placeholders_ids = ",".join("?" for _ in instruments)
        placeholders_times = ",".join("?" for _ in opened_values)
        query = (
            "SELECT DISTINCT filename FROM read_parquet(?, filename=true, union_by_name=true, "
            "hive_partitioning=true) "
            f"WHERE instrument_id IN ({placeholders_ids}) "
            f"AND bar_open_time IN ({placeholders_times})"
        )
        with duckdb.connect(database=":memory:") as connection:
            found = connection.execute(
                query,
                [[str(path) for path in candidates], *instruments, *opened_values],
            ).fetchall()
        return [str(row[0]) for row in found]

    def _build_locked(self, revision: str) -> None:
        """Build and commit atomically.  Called within self._lock."""
        building = self._build_to_file(revision)
        self._commit_build(revision, building)

    def _build_to_file(self, revision: str) -> Path:
        """Build the .building DuckDB file without touching shared state."""
        started = time.perf_counter()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Use a process/thread-specific staging file.  A desktop server and a
        # maintenance command may legitimately rebuild at the same time; a
        # shared ``.building`` path lets one process delete the other's work.
        building = self.path.with_name(
            f"{self.path.name}.building-{os.getpid()}-{threading.get_ident()}"
        )
        building.unlink(missing_ok=True)
        building.with_name(building.name + ".wal").unlink(missing_ok=True)
        files = sorted((self.data_root / "silver").rglob("*.parquet"))
        try:
            with duckdb.connect(str(building)) as connection:
                connection.execute("PRAGMA threads=2")
                connection.execute("SET preserve_insertion_order=false")
                if files:
                    glob = (self.data_root / "silver" / "**" / "*.parquet").as_posix()
                    connection.execute(
                        "CREATE TABLE instrument_file AS "
                        "SELECT CAST(instrument_id AS VARCHAR) AS instrument_id, "
                        "CAST(period AS VARCHAR) AS period, CAST(filename AS VARCHAR) AS file_path, "
                        "count(*)::BIGINT AS row_count, min(bar_open_time) AS earliest_bar_at, "
                        "max(bar_open_time) AS latest_bar_at, any_value(market) AS market, "
                        "any_value(asset_type) AS asset_type "
                        "FROM read_parquet(?, filename=true, union_by_name=true, hive_partitioning=true) "
                        "WHERE instrument_id IS NOT NULL AND period IS NOT NULL "
                        "AND bar_open_time IS NOT NULL GROUP BY instrument_id, period, filename",
                        [glob],
                    )
                    connection.execute(
                        "CREATE TABLE instrument_period AS SELECT instrument_id, period, "
                        "sum(row_count)::BIGINT AS row_count, min(earliest_bar_at) AS earliest_bar_at, "
                        "max(latest_bar_at) AS latest_bar_at FROM instrument_file GROUP BY instrument_id, period"
                    )
                    connection.execute(
                        "CREATE TABLE instrument_latest AS "
                        "WITH bounds AS (SELECT instrument_id, max(latest_bar_at) AS latest_bar_at "
                        "FROM instrument_period GROUP BY instrument_id) "
                        "SELECT CAST(raw.instrument_id AS VARCHAR) AS instrument_id, "
                        "any_value(raw.market) AS market, any_value(raw.asset_type) AS asset_type, "
                        "any_value(CAST(raw.period AS VARCHAR)) AS period, "
                        "bounds.latest_bar_at AS bar_open_time, any_value(raw.bar_json) AS bar_json "
                        "FROM read_parquet(?, union_by_name=true, hive_partitioning=true) AS raw "
                        "JOIN bounds ON CAST(raw.instrument_id AS VARCHAR) = bounds.instrument_id "
                        "AND raw.bar_open_time = bounds.latest_bar_at "
                        "GROUP BY raw.instrument_id, bounds.latest_bar_at",
                        [glob],
                    )
                else:
                    connection.execute(
                        "CREATE TABLE instrument_file (instrument_id VARCHAR, period VARCHAR, "
                        "file_path VARCHAR, row_count BIGINT, earliest_bar_at VARCHAR, "
                        "latest_bar_at VARCHAR, market VARCHAR, asset_type VARCHAR)"
                    )
                    connection.execute(
                        "CREATE TABLE instrument_latest (instrument_id VARCHAR, market VARCHAR, "
                        "asset_type VARCHAR, period VARCHAR, bar_open_time VARCHAR, bar_json VARCHAR)"
                    )
                    connection.execute(
                        "CREATE TABLE instrument_period (instrument_id VARCHAR, period VARCHAR, "
                        "row_count BIGINT, earliest_bar_at VARCHAR, latest_bar_at VARCHAR)"
                    )
                connection.execute(
                    "CREATE TABLE derived_bars (instrument_id VARCHAR, period VARCHAR, revision VARCHAR, payload VARCHAR, "
                    "row_count BIGINT, earliest_bar_at VARCHAR, latest_bar_at VARCHAR)"
                )
                connection.execute(
                    "CREATE TABLE cache_meta (schema_version INTEGER, revision VARCHAR, row_count BIGINT, built_at VARCHAR, build_seconds DOUBLE)"
                )
                connection.execute(
                    "CREATE INDEX file_lookup ON instrument_file (instrument_id, period, earliest_bar_at, latest_bar_at)"
                )
                connection.execute("CREATE INDEX period_lookup ON instrument_period (instrument_id, period)")
                rows = int(
                    connection.execute("SELECT coalesce(sum(row_count), 0) FROM instrument_period").fetchone()[0]
                )
                connection.execute(
                    "INSERT INTO cache_meta VALUES (?, ?, ?, ?, ?)",
                    [_SCHEMA_VERSION, revision, rows, _now(), time.perf_counter() - started],
                )
        except Exception:
            building.unlink(missing_ok=True)
            building.with_name(building.name + ".wal").unlink(missing_ok=True)
            raise
        return building

    def _commit_build(self, revision: str, building: Path) -> None:
        """Atomically swap the built file and update shared state.  Called within self._lock."""
        os.replace(building, self.path)
        self._hot.clear()
        self._hot_revision = revision
        self._ready_revision = revision

    def _claim_build_lease(self) -> int | None:
        """Claim the single cross-process rebuild lease without waiting."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            descriptor = os.open(self.build_lock_path, flags)
        except FileExistsError:
            if not self._remove_stale_build_lease():
                return None
            try:
                descriptor = os.open(self.build_lock_path, flags)
            except FileExistsError:
                return None
        # Older releases used a shared ``.building`` file without a lease.
        # Respect a recently active legacy builder instead of starting another
        # multi-gigabyte copy beside it.
        if self._legacy_build_is_active():
            os.close(descriptor)
            self.build_lock_path.unlink(missing_ok=True)
            return None
        payload = json.dumps({"pid": os.getpid(), "createdAt": _now()}).encode("utf-8")
        try:
            os.write(descriptor, payload)
        except Exception:
            os.close(descriptor)
            self.build_lock_path.unlink(missing_ok=True)
            raise
        return descriptor

    def _legacy_build_is_active(self) -> bool:
        cutoff = time.time() - (12 * 60 * 60)
        for candidate in self.path.parent.glob(f"{self.path.name}.building*"):
            try:
                if candidate.stat().st_mtime >= cutoff:
                    return True
            except OSError:
                continue
        return False

    def _remove_stale_build_lease(self) -> bool:
        """Remove a lease only when its owning process is known to be gone."""

        try:
            payload = json.loads(self.build_lock_path.read_text(encoding="utf-8"))
            owner = int(payload["pid"])
        except (OSError, ValueError, TypeError, KeyError):
            return False
        if owner == os.getpid() or _process_is_running(owner):
            return False
        try:
            self.build_lock_path.unlink()
        except OSError:
            return False
        return True

    def _release_build_lease(self, descriptor: int) -> None:
        try:
            os.close(descriptor)
        finally:
            # A different process can never claim this path before our file is
            # removed, so unlinking the exact lease is safe.
            self.build_lock_path.unlink(missing_ok=True)

    def _metadata(self) -> tuple[str, int, str | None, float | None] | None:
        if not self.path.is_file():
            return None
        try:
            with self._connect(read_only=True) as connection:
                row = connection.execute(
                    "SELECT revision, row_count, built_at, build_seconds FROM cache_meta "
                    "WHERE schema_version = ? LIMIT 1",
                    [_SCHEMA_VERSION],
                ).fetchone()
            if row is None:
                return None
            return str(row[0]), int(row[1]), _text(row[2]), float(row[3]) if row[3] is not None else None
        except Exception:
            return None

    def _connect(self, *, read_only: bool = False):
        return duckdb.connect(str(self.path), read_only=read_only)

    def _set_hot_revision(self, revision: str) -> None:
        if self._hot_revision != revision:
            self._hot.clear()
            self._hot_revision = revision

    def _hot_get(self, key: tuple[Any, ...]) -> Any | None:
        with self._lock:
            value = self._hot.get(key)
            if value is None:
                self._hot_misses += 1
                return None
            self._hot.move_to_end(key)
            self._hot_hits += 1
            return deepcopy(value)

    def _hot_put(self, key: tuple[Any, ...], value: Any) -> None:
        with self._lock:
            self._hot[key] = deepcopy(value)
            self._hot.move_to_end(key)
            while len(self._hot) > _MAX_HOT_WINDOWS:
                self._hot.popitem(last=False)


def get_kline_query_store(data_root: Path) -> KLineQueryStore:
    root = str(Path(data_root).resolve())
    with _STORES_LOCK:
        store = _STORES.get(root)
        if store is None:
            store = KLineQueryStore(Path(data_root))
            _STORES[root] = store
        return store


def rebuild_kline_query_cache(data_root: Path) -> dict[str, Any]:
    return get_kline_query_store(data_root).rebuild().to_dict()


def apply_kline_cache_update(
    data_root: Path,
    previous_revision: str,
    current_revision: str,
    bars: Iterable[Mapping[str, Any]],
) -> bool:
    return get_kline_query_store(data_root).apply_silver_update(previous_revision, current_revision, bars)


def _where(instrument_id: str, period: str | None) -> tuple[str, list[str]]:
    if period:
        return "instrument_id = ? AND period = ?", [instrument_id, period]
    return "instrument_id = ?", [instrument_id]


def _read_parquet_rows(
    files: Iterable[str],
    instrument_id: str,
    period: str | None,
    *,
    before: str | None = None,
    descending: bool,
    limit: int,
    offset: int = 0,
) -> list[tuple[Any, Any]]:
    """Read a globally ordered slice across legacy and Hive-style files."""

    groups: dict[bool, list[str]] = {False: [], True: []}
    for value in files:
        groups["period=" in Path(value).as_posix()].append(value)
    rows: list[tuple[Any, Any]] = []
    direction = "DESC" if descending else "ASC"
    per_group_limit = max(1, limit + offset)
    for hive_partitioning, source_files in groups.items():
        if not source_files:
            continue
        where, parameters = _where(instrument_id, period)
        if before:
            where += " AND bar_open_time < ?"
            parameters.append(before)
        query = (
            "SELECT bar_open_time, arg_max(bar_json, "
            "coalesce(json_extract_string(bar_json, '$.fetched_at'), '')) AS bar_json "
            "FROM read_parquet(?, union_by_name=true, "
            f"hive_partitioning={str(hive_partitioning).lower()}) WHERE {where} "
            "GROUP BY bar_open_time "
            f"ORDER BY bar_open_time {direction} LIMIT ?"
        )
        with duckdb.connect(database=":memory:") as connection:
            rows.extend(
                connection.execute(query, [source_files, *parameters, per_group_limit]).fetchall()
            )
    # Legacy and Hive-style partitions can overlap after importer migrations.
    # Resolve the same timestamp once more across both groups, preferring the
    # payload from the latest fetch when that provenance field is available.
    unique: dict[str, tuple[Any, Any]] = {}
    for row in rows:
        opened = str(row[0])
        current = unique.get(opened)
        if current is None or _fetched_at(row[1]) >= _fetched_at(current[1]):
            unique[opened] = row
    deduplicated = sorted(unique.values(), key=lambda row: str(row[0]), reverse=descending)
    return deduplicated[offset : offset + limit]


def _fetched_at(value: Any) -> str:
    try:
        payload = json.loads(str(value))
    except (TypeError, ValueError):
        return ""
    return str(payload.get("fetched_at") or "") if isinstance(payload, dict) else ""


def _decode_bar(value: Any, instrument_id: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(str(value))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    payload.setdefault("instrument_id", instrument_id)
    return payload


def _cache_row(bar: Mapping[str, Any]) -> tuple[str, str, str, str, str, str] | None:
    instrument_id = _instrument_id(bar)
    period = _text(bar.get("period"))
    opened = _text(bar.get("bar_open_time"))
    if not instrument_id or not period or not opened:
        return None
    return (
        instrument_id,
        period,
        opened,
        _text(bar.get("market")) or "",
        _text(bar.get("asset_type")) or "",
        json.dumps(dict(bar), ensure_ascii=False, separators=(",", ":")),
    )


def _instrument_id(bar: Mapping[str, Any]) -> str | None:
    direct = _text(bar.get("instrument_id"))
    if direct:
        return direct
    key = bar.get("instrument_key")
    if isinstance(key, Mapping):
        parts = [str(key.get(name) or "") for name in ("country_or_market", "exchange", "asset_type", "code")]
        value = ".".join(parts).strip(".")
        return value or None
    return _text(key)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value)
    return result if result else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Windows can report access errors for a live foreign process.
        return True
    return True


__all__ = (
    "KLineCacheUnavailable",
    "KLineCacheStatus",
    "KLineQueryStore",
    "KLineWindow",
    "apply_kline_cache_update",
    "get_kline_query_store",
    "rebuild_kline_query_cache",
)
