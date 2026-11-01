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
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Iterable, Mapping

import duckdb

from .market_data_version import (
    changes_between,
    market_data_version,
    migrate_legacy_market_data_version,
    market_unknown_epoch,
    revision_is_settled,
)
from .market_silver_catalog_audit import (
    _catalog_digest,
    _catalog_rows,
    _physical_silver_paths,
    _valid_silver_relative_path,
    audit_silver_catalog,
)


QUERY_INDEX_SCHEMA_VERSION = 5
_SCHEMA_VERSION = QUERY_INDEX_SCHEMA_VERSION
_MAX_CATALOG_PARTITIONS = 100_000
_MAX_CATALOG_DELTA_FILES = 4_096
_MAX_CATALOG_DELTA_BYTES = 8 * 1024**3
_MAX_HOT_WINDOWS = 4096
_INLINE_BUILD_FILE_LIMIT = 32
_REBUILD_RETRY_BASE_SECONDS = 30.0
_REBUILD_RETRY_MAX_SECONDS = 600.0
_STORES: dict[str, "KLineQueryStore"] = {}
_STORES_LOCK = threading.RLock()
_LOG = logging.getLogger(__name__)


def _open_catalog(data_root: Path) -> duckdb.DuckDBPyConnection:
    """Use a writable handle so this process can share MarketStore's Catalog."""

    if not (Path(data_root) / "catalog.duckdb").is_file():
        raise FileNotFoundError("Silver Catalog is missing")
    # MarketStore imports this module.  Resolve the connection helper only at
    # runtime to avoid a module initialization cycle.
    from .storage import connect_catalog

    return connect_catalog(data_root)


def _catalog_snapshot(data_root: Path) -> tuple[str, list[tuple[Any, ...]]]:
    """Read the bounded Catalog evidence set without hashing Silver again."""

    with _open_catalog(data_root) as connection:
        rows = _catalog_rows(connection, _MAX_CATALOG_PARTITIONS)
    if len(rows) > _MAX_CATALOG_PARTITIONS:
        raise ValueError("Silver Catalog partition limit exceeded")
    return _catalog_digest(rows), rows


def _validated_catalog_manifest(
    data_root: Path, rows: list[tuple[Any, ...]], *, check_physical_set: bool = False,
) -> dict[str, tuple[str, str, int]]:
    """Normalize the bounded COMPLETE Catalog set; reject aliases and orphans."""

    root = Path(data_root).resolve()
    silver_root = (root / "silver").resolve()
    manifest: dict[str, tuple[str, str, int]] = {}
    for _partition_id, raw_path, row_count, raw_sha, status in rows:
        relative = _valid_silver_relative_path(raw_path)
        if relative is None or status != "COMPLETE":
            raise ValueError("Catalog has an invalid Silver partition")
        path = root.joinpath(*relative.split("/")).resolve()
        try:
            path.relative_to(silver_root)
        except ValueError as error:
            raise ValueError("Catalog Silver path escapes data root") from error
        if (isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 1
                or not isinstance(raw_sha, str) or re.fullmatch(r"[0-9a-fA-F]{64}", raw_sha) is None):
            raise ValueError("Catalog has invalid Silver count or SHA")
        identity = relative.casefold()
        if identity in manifest:
            raise ValueError("Catalog contains duplicate Silver paths")
        manifest[identity] = (relative, raw_sha.lower(), row_count)
    if check_physical_set:
        issues: list[str] = []
        physical = _physical_silver_paths(
            root / "silver", _MAX_CATALOG_PARTITIONS, time.monotonic() + 30.0,
            lambda code, *_args: issues.append(str(code)),
        )
        if physical is None or issues or set(physical) != set(manifest):
            raise ValueError("Catalog and physical Silver path sets differ")
    return manifest


def _indexed_catalog_manifest(
    connection: duckdb.DuckDBPyConnection,
) -> dict[str, tuple[str, str, int]]:
    rows = connection.execute(
        "SELECT file_path,sha256,row_count FROM indexed_catalog_manifest"
    ).fetchall()
    if len(rows) > _MAX_CATALOG_PARTITIONS:
        raise ValueError("query-index Catalog manifest exceeds partition limit")
    manifest: dict[str, tuple[str, str, int]] = {}
    for raw_path, raw_sha, raw_count in rows:
        relative = _valid_silver_relative_path(raw_path)
        if (relative is None or not isinstance(raw_sha, str)
                or re.fullmatch(r"[0-9a-fA-F]{64}", raw_sha) is None
                or isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 1):
            raise ValueError("query-index Catalog manifest is invalid")
        identity = relative.casefold()
        if identity in manifest:
            raise ValueError("query-index Catalog manifest has duplicate paths")
        manifest[identity] = (relative, raw_sha.lower(), raw_count)
    return manifest


def _catalog_evidence_for_changes(data_root: Path, changed: Mapping[str, str]) -> str:
    """Require one matching COMPLETE Catalog row and Parquet count per changed file."""

    root = Path(data_root).resolve()
    digest, rows = _catalog_snapshot(root)
    expected: dict[str, tuple[str, str]] = {}
    for path, file_sha in changed.items():
        canonical = Path(path).resolve()
        try:
            relative = canonical.relative_to(root).as_posix()
        except ValueError as error:
            raise ValueError("changed Silver path escapes data root") from error
        if _valid_silver_relative_path(relative) != relative:
            raise ValueError("changed file is not a Silver Parquet path")
        identity = relative.casefold()
        if identity in expected:
            raise ValueError("duplicate changed Silver path")
        expected[identity] = (str(canonical), file_sha)
    matched: dict[str, tuple[str, int]] = {}
    for _partition_id, raw_path, row_count, catalog_sha, status in rows:
        relative = _valid_silver_relative_path(raw_path)
        if relative is None or relative.casefold() not in expected:
            continue
        identity = relative.casefold()
        if identity in matched:
            raise ValueError("duplicate Catalog path for changed Silver file")
        actual_path = root.joinpath(*relative.split("/")).resolve()
        path, file_sha = expected[identity]
        if (str(actual_path).casefold() != path.casefold() or status != "COMPLETE"
                or not isinstance(catalog_sha, str) or catalog_sha.lower() != file_sha
                or isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 1):
            raise ValueError("changed Silver file does not match COMPLETE Catalog evidence")
        matched[identity] = (path, row_count)
    if set(matched) != set(expected):
        raise ValueError("changed Silver file is missing from Catalog")

    paths = [item[0] for item in matched.values()]
    with duckdb.connect(":memory:") as connection:
        parquet_counts = {
            str(Path(name).resolve()).casefold(): int(count)
            for name, count in connection.execute(
                "SELECT file_name, sum(row_group_num_rows)::BIGINT "
                "FROM (SELECT DISTINCT file_name, row_group_id, row_group_num_rows "
                "FROM parquet_metadata(?)) GROUP BY file_name",
                [paths],
            ).fetchall()
        }
    if any(parquet_counts.get(path.casefold()) != count for path, count in matched.values()):
        raise ValueError("changed Silver Parquet row count differs from Catalog")
    return digest


def _parquet_layout_expressions(
    connection: duckdb.DuckDBPyConnection, source: list[str] | str, alias: str,
) -> tuple[str, str, str]:
    """Resolve mixed Silver schemas without requiring identical Hive layouts."""

    columns = {
        str(row[0]) for row in connection.execute(
            "DESCRIBE SELECT * FROM read_parquet(?,filename=true,union_by_name=true,hive_partitioning=false)",
            [source],
        ).fetchall()
    }
    period_columns = [f"nullif({alias}.{name}, '')" for name in ("bar_period", "period") if name in columns]
    if not period_columns:
        raise ValueError("Silver Parquet has no supported period column")

    def from_path(field: str) -> str:
        return ("nullif(regexp_extract(replace("
                f"{alias}.filename, chr(92), '/'), '/{field}=([^/]+)', 1), '')")

    period = f"coalesce({', '.join((*period_columns, from_path('period')))})"
    market_columns = [f"nullif({alias}.market, '')"] if "market" in columns else []
    asset_columns = [f"nullif({alias}.asset_type, '')"] if "asset_type" in columns else []
    market = f"coalesce({', '.join((*market_columns, from_path('market')))})"
    asset_type = f"coalesce({', '.join((*asset_columns, from_path('asset_type')))})"
    return period, market, asset_type


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
    build: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "revision": self.revision,
            "rows": self.rows,
            "builtAt": self.built_at,
            "buildSeconds": self.build_seconds,
            "hotHits": self.hot_hits,
            "hotMisses": self.hot_misses,
            "build": self.build,
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
        self._ready_epoch: int | None = None
        self._hot_hits = 0
        self._hot_misses = 0
        self._rebuilding = False
        self._next_rebuild_attempt = 0.0
        self._rebuild_failure_revision: str | None = None
        self._rebuild_failures = 0
        self._rebuild_allowed: Callable[[], bool] | None = None
        # Kept apart from the index lock: a status GET must observe progress
        # while a manual rebuild holds that lock for the entire DuckDB scan.
        self._progress_lock = threading.Lock()
        self._build_progress: dict[str, Any] | None = None
        self._progress_owner: int | None = None
        self._progress_started: float | None = None

    def _start_progress(self, revision: str, stage: str) -> bool:
        with self._progress_lock:
            if self._build_progress is not None and self._build_progress["state"] == "RUNNING":
                return False
            now = _now()
            self._progress_owner = threading.get_ident()
            self._progress_started = time.monotonic()
            self._build_progress = {
                "state": "RUNNING", "stage": stage, "targetRevision": revision,
                "completed": None, "total": None, "unit": None,
                "startedAt": now, "updatedAt": now,
            }
            return True

    def _update_progress(
        self, stage: str, *, completed: int | None = None,
        total: int | None = None, unit: str | None = None,
    ) -> None:
        with self._progress_lock:
            progress = self._build_progress
            if (progress is None or progress["state"] != "RUNNING"
                    or self._progress_owner != threading.get_ident()):
                return
            progress.update({
                "stage": stage, "completed": completed, "total": total,
                "unit": unit, "updatedAt": _now(),
            })

    def _finish_progress(self, success: bool | None) -> None:
        with self._progress_lock:
            progress = self._build_progress
            if (progress is None or progress["state"] != "RUNNING"
                    or self._progress_owner != threading.get_ident()):
                return
            prior_stage = progress["stage"]
            progress["state"] = "SUCCEEDED" if success else "DEFERRED" if success is None else "FAILED"
            progress["stage"] = "DONE" if success else "WAITING" if success is None else "FAILED"
            if success is False:
                progress["failureCategory"] = (
                    "AUDIT_FAILED" if str(prior_stage).startswith("AUDIT_") else
                    "PUBLICATION_FAILED" if prior_stage == "PUBLISHING" else
                    "INDEX_FAILED" if str(prior_stage).startswith("INDEX_") else
                    "SOURCE_NOT_READY"
                )
            progress["updatedAt"] = _now()
            self._progress_owner = None
            self._progress_started = None

    def _progress_snapshot(self, current_revision: str) -> dict[str, Any] | None:
        with self._progress_lock:
            progress = self._build_progress
            if progress is None or progress["targetRevision"] != current_revision:
                return None
            result = dict(progress)
            if result["state"] == "RUNNING" and self._progress_started is not None:
                result["elapsedSeconds"] = max(0, int(time.monotonic() - self._progress_started))
            if result["state"] == "FAILED" and self._rebuild_failure_revision == current_revision:
                result["retryAfterSeconds"] = max(0, int(self._next_rebuild_attempt - time.monotonic() + 0.999))
            return result

    def _retry_due(self, revision: str) -> bool:
        """Reset automatic backoff when Silver publishes a different revision."""

        if self._rebuild_failure_revision != revision:
            self._rebuild_failure_revision = None
            self._rebuild_failures = 0
            self._next_rebuild_attempt = 0.0
        return time.monotonic() >= self._next_rebuild_attempt

    def _record_background_outcome(self, revision: str, success: bool | None) -> None:
        if success:
            self._rebuild_failure_revision = None
            self._rebuild_failures = 0
            self._next_rebuild_attempt = 0.0
        elif success is False:
            if self._rebuild_failure_revision != revision:
                self._rebuild_failures = 0
            self._rebuild_failure_revision = revision
            self._rebuild_failures += 1
            delay = min(
                _REBUILD_RETRY_BASE_SECONDS * (2 ** min(self._rebuild_failures - 1, 5)),
                _REBUILD_RETRY_MAX_SECONDS,
            )
            self._next_rebuild_attempt = time.monotonic() + delay
        else:
            self._next_rebuild_attempt = time.monotonic() + 5.0

    def set_rebuild_allowed(self, callback: Callable[[], bool] | None) -> None:
        """Defer background recovery while a source writer is in progress."""

        with self._lock:
            self._rebuild_allowed = callback

    def _may_rebuild(self, requested: bool) -> bool:
        if not requested:
            return False
        callback = self._rebuild_allowed
        if callback is None:
            return True
        try:
            return bool(callback())
        except Exception:
            return False

    def status(self) -> KLineCacheStatus:
        current = market_data_version(self.data_root)
        current_epoch = market_unknown_epoch(self.data_root)
        progress = self._progress_snapshot(current)
        # Status is part of page bootstrap and must never wait behind a
        # multi-gigabyte initial/rebuild transaction.  A busy store reports
        # "not ready" while normal market/instrument endpoints remain usable.
        if not self._lock.acquire(blocking=False):
            return KLineCacheStatus(
                False, self._ready_revision, 0, None, None, self._hot_hits, self._hot_misses, progress
            )
        try:
            metadata = self._metadata()
            if metadata is None:
                return KLineCacheStatus(False, None, 0, None, None, self._hot_hits, self._hot_misses, progress)
            revision, rows, built_at, seconds, indexed_epoch = metadata
            ready = (revision == current and current_epoch is not None
                     and indexed_epoch == current_epoch
                     and revision_is_settled(self.data_root, current))
            return KLineCacheStatus(
                ready, revision, rows, built_at, seconds, self._hot_hits, self._hot_misses, progress,
            )
        finally:
            self._lock.release()

    def rebuild(self) -> KLineCacheStatus:
        """Build the ordered query cache from immutable Parquet once."""

        migrate_legacy_market_data_version(self.data_root)
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

    def ensure_ready(self, *, rebuild_allowed: bool = True) -> str:
        revision = market_data_version(self.data_root)
        current_epoch = market_unknown_epoch(self.data_root)
        if not revision_is_settled(self.data_root, revision):
            raise KLineCacheUnavailable("Silver write is pending; query index cannot be published")
        with self._lock:
            if (current_epoch is not None and self._ready_revision == revision
                    and self._ready_epoch == current_epoch):
                self._set_hot_revision(revision)
                return revision
            metadata = self._metadata()
            if (metadata is not None and metadata[0] == revision
                    and current_epoch is not None and metadata[4] == current_epoch):
                self._ready_revision = revision
                self._ready_epoch = current_epoch
                self._set_hot_revision(revision)
                return revision
            if metadata is not None:
                # The manifest points to mutable Parquet paths. After a Silver
                # replacement, old bounds cannot safely read those new files.
                # Preserve its metadata for diagnostics, but reject chart reads
                # until the matching revision is rebuilt.
                stale_revision = str(metadata[0])
                self._ready_revision = stale_revision
                self._ready_epoch = metadata[4]
                self._set_hot_revision(stale_revision)
                if (self._may_rebuild(rebuild_allowed) and not self._rebuilding
                        and self._retry_due(revision)):
                    self._start_background_rebuild(revision)
                raise KLineCacheUnavailable("K-line query index is stale and rebuilding")
            # Tiny fixture/development stores are cheaper to build than to
            # open through the fallback.  A real store is always prepared in
            # the background so the first chart request is never the cache
            # build job.
            if self._may_rebuild(rebuild_allowed) and self._should_build_inline():
                lease = self._claim_build_lease()
                if lease is not None:
                    try:
                        self._build_locked(revision)
                    finally:
                        self._release_build_lease(lease)
                    self._ready_revision = revision
                    self._ready_epoch = market_unknown_epoch(self.data_root)
                    self._set_hot_revision(revision)
                    return revision
            if (self._may_rebuild(rebuild_allowed) and not self._rebuilding
                    and self._retry_due(revision)):
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
        lease: int | None = None
        outcome: bool | None = None
        self._start_progress(revision, "CHECKING")
        try:
            self._update_progress("ADOPTING")
            if self.upgrade_schema_four_manifest(target_revision=revision):
                outcome = True
                return
            self._update_progress("DELTA_CHECK")
            if self.refresh_catalog_delta(target_revision=revision):
                outcome = True
                return
            lease = self._claim_build_lease()
            if lease is None:
                return
            building = self._build_to_file(revision)
            with self._lock:
                self._commit_build(revision, building)
                building = None
            outcome = True
        except Exception:
            if building is not None:
                building.unlink(missing_ok=True)
            outcome = False
        finally:
            if lease is not None:
                self._release_build_lease(lease)
            with self._lock:
                self._record_background_outcome(revision, outcome)
                self._rebuilding = False
            self._finish_progress(outcome)

    def read_bars(self, instrument_id: str, period: str | None, limit: int) -> list[dict[str, Any]]:
        revision = self.ensure_ready()
        safe_limit = max(1, min(int(limit), 5_000))
        key = ("tail", revision, instrument_id, period or "", safe_limit)
        cached = self._hot_get(key)
        if cached is not None:
            self._assert_read_revision(revision)
            return cached
        files = self._files(instrument_id, period)
        if not files:
            self._assert_read_revision(revision)
            return []
        rows = _read_parquet_rows(
            files, instrument_id, period, descending=True, limit=safe_limit
        )
        bars = [_decode_bar(row[1], instrument_id) for row in reversed(rows)]
        result = [bar for bar in bars if bar is not None]
        self._hot_put_checked(key, result, revision)
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
            self._assert_read_revision(revision)
            return {}
        key = ("tails", revision, requested, period, safe_limit)
        cached = self._hot_get(key)
        if cached is not None:
            self._assert_read_revision(revision)
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
        self._hot_put_checked(key, result, revision)
        return deepcopy(result)

    def read_window(self, instrument_id: str, period: str, start: int, limit: int) -> tuple[list[dict[str, Any]], int]:
        revision = self.ensure_ready()
        safe_start = max(0, int(start))
        safe_limit = max(1, min(int(limit), 5_000))
        key = ("window", revision, instrument_id, period, safe_start, safe_limit)
        cached = self._hot_get(key)
        if cached is not None:
            self._assert_read_revision(revision)
            return cached
        files = self._files(instrument_id, period)
        with self._lock:
            with self._connect(read_only=True) as connection:
                metadata = connection.execute(
                    "SELECT row_count FROM instrument_period WHERE instrument_id = ? AND period = ?",
                    [instrument_id, period],
                ).fetchone()
                total = int(metadata[0]) if metadata else 0
        if files and _intraday_quality_clause(instrument_id, period):
            total, _, _ = self._quality_bounds(revision, files, instrument_id, period)
        if not files or not total:
            self._assert_read_revision(revision)
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
        self._hot_put_checked(key, result, revision)
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
            self._assert_read_revision(revision)
            return cached
        with self._lock:
            with self._connect(read_only=True) as connection:
                # The viewport and its count belong to the same indexed
                # revision.  Resolve both in one DuckDB connection instead of
                # reopening the index once for paths and once for the count.
                indexed = connection.execute(
                    "SELECT p.row_count, f.file_path FROM instrument_period p "
                    "LEFT JOIN instrument_file f USING (instrument_id, period) "
                    "WHERE p.instrument_id = ? AND p.period = ? "
                    "ORDER BY f.earliest_bar_at",
                    [instrument_id, period],
                ).fetchall()
                total = int(indexed[0][0]) if indexed else 0
                files = [str(row[1]) for row in indexed if row[1]]
        if files and _intraday_quality_clause(instrument_id, period):
            total, _, _ = self._quality_bounds(revision, files, instrument_id, period)
        if not files:
            self._assert_read_revision(revision)
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
        self._hot_put_checked(key, result, revision)
        return deepcopy(result)

    def bounds(self, instrument_id: str, period: str) -> tuple[int, str | None, str | None]:
        revision = self.ensure_ready()
        key = ("bounds", revision, instrument_id, period)
        cached = self._hot_get(key)
        if cached is not None:
            self._assert_read_revision(revision)
            return cached
        with self._lock:
            with self._connect(read_only=True) as connection:
                row = connection.execute(
                    "SELECT row_count, earliest_bar_at, latest_bar_at FROM instrument_period "
                    "WHERE instrument_id = ? AND period = ?",
                    [instrument_id, period],
                ).fetchone()
        result = (int(row[0]), _text(row[1]), _text(row[2])) if row else (0, None, None)
        if _intraday_quality_clause(instrument_id, period):
            files = self._files(instrument_id, period)
            if files:
                result = self._quality_bounds(revision, files, instrument_id, period)
        self._hot_put_checked(key, result, revision)
        return result

    def _quality_bounds(
        self, revision: str, files: list[str], instrument_id: str, period: str
    ) -> tuple[int, str | None, str | None]:
        # A row's quality does not depend on its partition filename.  Reuse
        # the same filtered count for all windows of this Silver revision.
        key = ("quality-bounds", revision, instrument_id, period)
        cached = self._hot_get(key)
        if cached is not None:
            self._assert_read_revision(revision)
            return cached
        result = _filtered_intraday_bounds(files, instrument_id, period)
        self._hot_put_checked(key, result, revision)
        return result

    def periods(self, instrument_id: str) -> list[str]:
        revision = self.ensure_ready()
        key = ("periods", revision, instrument_id)
        cached = self._hot_get(key)
        if cached is not None:
            self._assert_read_revision(revision)
            return cached
        with self._lock:
            with self._connect(read_only=True) as connection:
                rows = connection.execute(
                    "SELECT period FROM instrument_period WHERE instrument_id = ? ORDER BY period",
                    [instrument_id],
                ).fetchall()
        result = [str(row[0]) for row in rows]
        self._hot_put_checked(key, result, revision)
        return list(result)

    def periods_many(self, instrument_ids: Iterable[str]) -> dict[str, list[str]]:
        """Return stored periods for a page of physical instruments at once."""

        revision = self.ensure_ready()
        requested = tuple(dict.fromkeys(str(value) for value in instrument_ids if value))
        if not requested:
            self._assert_read_revision(revision)
            return {}
        key = ("periods-many", revision, requested)
        cached = self._hot_get(key)
        if cached is not None:
            self._assert_read_revision(revision)
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
        self._hot_put_checked(key, result, revision)
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
            self._assert_read_revision(revision)
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
        self._hot_put_checked(key, result, revision)
        return deepcopy(result)

    def tdx_import_inventory(self) -> dict[str, Any]:
        """Read persisted source ranges without building/scanning market bars.

        The console may poll while an import refreshes the manifest. Busy
        metadata returns unavailable immediately and retains its old UI data.
        """
        if not self.path.is_file() or not self._lock.acquire(blocking=False):
            return {"available": False, "items": []}
        try:
            with self._connect(read_only=True) as connection:
                rows = connection.execute(
                    "SELECT p.instrument_id, p.period, p.earliest_bar_at, p.latest_bar_at, "
                    "p.row_count, l.bar_json FROM instrument_period p JOIN instrument_latest l "
                    "ON l.instrument_id=p.instrument_id "
                    "WHERE p.instrument_id LIKE '%.TDX_LOCAL' OR p.instrument_id LIKE '%.TDX'"
                ).fetchall()
            items = []
            for instrument, period, earliest, latest, rows_count, raw in rows:
                bar = _decode_bar(raw, str(instrument)) or {}
                source = str(bar.get("actual_source") or bar.get("source") or "")
                terminal = (
                    "financial" if str(instrument).endswith(".TDX_LOCAL") and "金融终端" in source
                    else "futures" if str(instrument).endswith(".TDX") and "期货通" in source
                    else None
                )
                if terminal is None:
                    continue
                items.append({"terminal": terminal, "instrumentId": str(instrument),
                              "symbol": bar.get("source_symbol") or bar.get("symbol"),
                              "period": str(period), "earliestDate": str(earliest)[:10] if earliest else None,
                              "latestDate": str(latest)[:10] if latest else None, "rows": int(rows_count or 0)})
            metadata = self._metadata()
            current = market_data_version(self.data_root)
            return {"schema": "tdx-source-inventory-v2", "available": True, "items": items, "checkedAt": _now(),
                    "revision": metadata[0] if metadata else None, "dataRevision": current,
                    "stale": metadata is None or metadata[0] != current}
        except duckdb.Error:
            return {"available": False, "items": []}
        finally:
            self._lock.release()

    def get_derived(self, instrument_id: str, period: str) -> list[dict[str, Any]] | None:
        revision = self.ensure_ready()
        key = ("derived", revision, instrument_id, period)
        cached = self._hot_get(key)
        if cached is not None:
            self._assert_read_revision(revision)
            return cached
        with self._lock:
            with self._connect(read_only=True) as connection:
                row = connection.execute(
                    "SELECT payload FROM derived_bars WHERE instrument_id = ? AND period = ? AND revision = ?",
                    [instrument_id, period, revision],
                ).fetchone()
        if row is None:
            self._assert_read_revision(revision)
            return None
        try:
            value = json.loads(str(row[0]))
        except ValueError:
            self._assert_read_revision(revision)
            return None
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            self._assert_read_revision(revision)
            return None
        result = [dict(item) for item in value]
        self._hot_put_checked(key, result, revision)
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
        file: Path | str,
    ) -> bool:
        """Refresh the exact file written by Silver, including its latest bar."""

        return self.refresh_partition_files(
            [file], expected_base_revision=previous_revision,
            target_revision=current_revision,
        )

    def upgrade_schema_four_manifest(self, *, target_revision: str | None = None) -> bool:
        """Adopt a CURRENT, previously audited schema-4 index without rescanning bars."""

        target = target_revision or market_data_version(self.data_root)
        if not self.path.is_file() or not revision_is_settled(self.data_root, target):
            return False
        try:
            with self._connect(read_only=True) as connection:
                old_schema = connection.execute(
                    "SELECT schema_version FROM cache_meta LIMIT 1"
                ).fetchone()
            if old_schema is None or old_schema[0] != 4:
                return False
        except (duckdb.Error, OSError):
            return False
        lease = self._claim_build_lease()
        if lease is None:
            return False
        try:
            digest, catalog_rows = _catalog_snapshot(self.data_root)
            manifest = _validated_catalog_manifest(
                self.data_root, catalog_rows, check_physical_set=True,
            )
            total = sum(row[2] for row in manifest.values())
            with self._connect() as connection:
                connection.execute("BEGIN TRANSACTION")
                try:
                    row = connection.execute(
                        "SELECT revision,row_count,catalog_digest FROM cache_meta "
                        "WHERE schema_version=4 LIMIT 1"
                    ).fetchone()
                    if (row is None or row[0] != target or row[1] != total
                            or row[2] != digest or connection.execute(
                                "SELECT coalesce(sum(row_count),0) FROM instrument_period"
                            ).fetchone()[0] != total):
                        raise ValueError("schema-4 index is not a CURRENT audited baseline")
                    epoch = market_unknown_epoch(self.data_root, initialize=True)
                    if epoch is None:
                        raise ValueError("Silver unknown epoch is unavailable")
                    connection.execute(
                        "CREATE TABLE indexed_catalog_manifest "
                        "(file_path VARCHAR PRIMARY KEY, sha256 VARCHAR, row_count BIGINT)"
                    )
                    if manifest:
                        connection.executemany(
                            "INSERT INTO indexed_catalog_manifest VALUES (?, ?, ?)",
                            [item for item in manifest.values()],
                        )
                    connection.execute("ALTER TABLE cache_meta ADD COLUMN unknown_epoch BIGINT")
                    connection.execute(
                        "UPDATE cache_meta SET schema_version=?, unknown_epoch=?",
                        [_SCHEMA_VERSION, epoch],
                    )
                    final_digest, final_rows = _catalog_snapshot(self.data_root)
                    if (final_digest != digest or len(final_rows) != len(catalog_rows)
                            or market_unknown_epoch(self.data_root) != epoch
                            or not revision_is_settled(self.data_root, target)):
                        raise RuntimeError("Silver Catalog changed during schema-5 adoption")
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
            self._ready_revision = target
            self._ready_epoch = epoch
            self._set_hot_revision(target)
            return True
        except (duckdb.Error, OSError, ValueError, RuntimeError) as error:
            _LOG.info("schema-4 query-index adoption skipped: %s", type(error).__name__)
            return False
        finally:
            self._release_build_lease(lease)

    def refresh_catalog_delta(self, *, target_revision: str | None = None) -> bool:
        """Recover a stale schema-5 index from the audited Catalog path delta.

        This is only safe when every intervening revision was a declared FILE
        write.  The revision state's persistent unknown epoch makes that proof
        independent of the bounded 2048-transition journal.
        """

        target = target_revision or market_data_version(self.data_root)
        if not self.path.is_file() or not revision_is_settled(self.data_root, target):
            return False
        current_epoch = market_unknown_epoch(self.data_root)
        if current_epoch is None:
            return False
        try:
            with self._connect(read_only=True) as connection:
                row = connection.execute(
                    "SELECT revision,unknown_epoch FROM cache_meta WHERE schema_version=? LIMIT 1",
                    [_SCHEMA_VERSION],
                ).fetchone()
                indexed = _indexed_catalog_manifest(connection)
            if row is None or row[1] != current_epoch:
                return False
            base_revision = str(row[0])
            if base_revision == target:
                return True
            catalog_digest, catalog_rows = _catalog_snapshot(self.data_root)
            current = _validated_catalog_manifest(
                self.data_root, catalog_rows, check_physical_set=True,
            )
            changed = {key for key, value in current.items() if indexed.get(key) != value}
            deleted = set(indexed) - set(current)
            removed_paths = {
                indexed[key][0] for key in deleted
            } | {
                indexed[key][0] for key in changed & set(indexed)
                if indexed[key][0] != current[key][0]
            }
            if len(changed) + len(removed_paths) > _MAX_CATALOG_DELTA_FILES:
                return False
            source_files = [self.data_root.joinpath(*current[key][0].split("/")) for key in sorted(changed)]
            if sum(path.stat().st_size for path in source_files) > _MAX_CATALOG_DELTA_BYTES:
                return False
            return self.refresh_partition_files(
                source_files,
                expected_base_revision=base_revision,
                target_revision=target,
                _catalog_delta=True,
                _deleted_files=[self.data_root.joinpath(*name.split("/")) for name in sorted(removed_paths)],
                _expected_catalog_digest=catalog_digest,
            )
        except (duckdb.Error, OSError, ValueError, TypeError):
            return False

    def refresh_partition_files(
        self,
        files: Iterable[Path | str],
        *,
        expected_base_revision: str | None = None,
        target_revision: str | None = None,
        _catalog_delta: bool = False,
        _deleted_files: Iterable[Path | str] = (),
        _expected_catalog_digest: str | None = None,
    ) -> bool:
        """Reconcile a complete write batch only from its indexed base version.

        A caller without both revisions cannot prove that its file list covers
        every write since the current manifest.  It must request a full rebuild
        instead of publishing a partial manifest as CURRENT.
        """

        requested_files = [Path(value).resolve() for value in files]
        deleted_files = [Path(value).resolve() for value in _deleted_files]
        if (not requested_files and not _catalog_delta
                or any(not path.is_file() for path in requested_files)
                or any(path.is_file() for path in deleted_files)):
            return False
        source_files = sorted({str(path) for path in requested_files})
        if (not self.path.is_file() or not expected_base_revision
                or not target_revision or market_data_version(self.data_root) != target_revision):
            return False
        epoch = market_unknown_epoch(self.data_root)
        if epoch is None:
            return False
        if _catalog_delta:
            expected_files = None
        else:
            recorded_changes = changes_between(self.data_root, expected_base_revision, target_revision)
            expected_files = ({str((self.data_root / relative).resolve()): digest
                               for relative, digest in recorded_changes.items()}
                              if recorded_changes is not None else None)
            if expected_files is None or set(expected_files) != set(source_files):
                return False
        if _catalog_delta:
            try:
                _digest, _rows = _catalog_snapshot(self.data_root)
                if _digest != _expected_catalog_digest:
                    return False
                _manifest = _validated_catalog_manifest(
                    self.data_root, _rows, check_physical_set=True,
                )
                root = self.data_root.resolve()
                expected_files = {}
                for path in source_files:
                    identity = Path(path).relative_to(root).as_posix().casefold()
                    entry = _manifest.get(identity)
                    if entry is not None:
                        expected_files[path] = entry[1]
                if set(expected_files) != set(source_files):
                    return False
            except (duckdb.Error, OSError, ValueError):
                return False
        assert expected_files is not None
        for path, expected_digest in expected_files.items():
            try:
                with Path(path).open("rb") as stream:
                    actual_digest = sha256()
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        actual_digest.update(chunk)
                if actual_digest.hexdigest() != expected_digest:
                    return False
            except OSError:
                return False
        with self._lock:
            metadata = self._metadata()
            if metadata is None or metadata[0] != expected_base_revision:
                return False
            lease = self._claim_build_lease()
            if lease is None:
                return False
            try:
                catalog_digest, catalog_rows = _catalog_snapshot(self.data_root)
                catalog_manifest = _validated_catalog_manifest(
                    self.data_root, catalog_rows, check_physical_set=_catalog_delta,
                )
                if (_expected_catalog_digest is not None
                        and catalog_digest != _expected_catalog_digest):
                    return False
                if expected_files and _catalog_evidence_for_changes(
                    self.data_root, expected_files,
                ) != catalog_digest:
                    return False
                with self._connect() as connection:
                    connection.execute("BEGIN TRANSACTION")
                    try:
                        indexed_manifest = _indexed_catalog_manifest(connection)
                        index_meta = connection.execute(
                            "SELECT revision,unknown_epoch,row_count FROM cache_meta "
                            "WHERE schema_version=? LIMIT 1", [_SCHEMA_VERSION],
                        ).fetchone()
                        if (index_meta is None or index_meta[0] != expected_base_revision
                                or index_meta[1] != epoch
                                or sum(item[2] for item in indexed_manifest.values()) != index_meta[2]):
                            raise ValueError("query-index Catalog baseline is invalid")
                        root = self.data_root.resolve()
                        requested_keys = {
                            Path(path).relative_to(root).as_posix().casefold()
                            for path in source_files
                        }
                        deleted_keys = {
                            path.relative_to(root).as_posix().casefold()
                            for path in deleted_files
                        }
                        observed_changes = {
                            key for key, value in catalog_manifest.items()
                            if indexed_manifest.get(key) != value
                        }
                        observed_deletes = set(indexed_manifest) - set(catalog_manifest)
                        if (not observed_changes.issubset(requested_keys)
                                or observed_deletes != deleted_keys - set(catalog_manifest)
                                or not requested_keys.issubset(set(catalog_manifest))):
                            raise ValueError("Silver change set does not match Catalog manifest")
                        # Older manifests may store paths relative to the
                        # repository while callers now pass absolute paths.
                        # Both spellings refer to the same Windows file and
                        # must be replaced together to avoid duplicate bars.
                        target_paths = {
                            str(Path(value).resolve()).casefold()
                            for value in [*source_files, *deleted_files]
                        }
                        target_names = {
                            Path(value).name.casefold() for value in [*source_files, *deleted_files]
                        }
                        aliases = {*source_files, *(str(path) for path in deleted_files)}
                        for (stored_text,) in connection.execute(
                            "SELECT DISTINCT file_path FROM instrument_file"
                        ).fetchall():
                            stored = Path(str(stored_text))
                            if stored.name.casefold() not in target_names:
                                continue
                            candidates = [stored] if stored.is_absolute() else [
                                stored, root.parent / stored, root / stored,
                            ]
                            if any(str(candidate.resolve()).casefold() in target_paths for candidate in candidates):
                                aliases.add(str(stored_text))
                        connection.execute(
                            "CREATE TEMP TABLE changed_files (file_path VARCHAR PRIMARY KEY)"
                        )
                        connection.executemany(
                            "INSERT INTO changed_files VALUES (?)", [(value,) for value in sorted(aliases)]
                        )
                        connection.execute(
                            "CREATE TEMP TABLE affected (instrument_id VARCHAR PRIMARY KEY)"
                        )
                        connection.execute(
                            "INSERT OR IGNORE INTO affected SELECT DISTINCT instrument_id "
                            "FROM instrument_file WHERE file_path IN (SELECT file_path FROM changed_files)"
                        )
                        if source_files:
                            connection.execute(
                                "INSERT OR IGNORE INTO affected SELECT DISTINCT CAST(instrument_id AS VARCHAR) "
                                "FROM read_parquet(?, union_by_name=true, hive_partitioning=false) "
                                "WHERE instrument_id IS NOT NULL",
                                [source_files],
                            )
                        connection.execute(
                            "DELETE FROM instrument_file WHERE file_path IN (SELECT file_path FROM changed_files)"
                        )
                        if source_files:
                            period_column, market_column, asset_column = _parquet_layout_expressions(
                                connection, source_files, "raw",
                            )
                            connection.execute(
                                "INSERT INTO instrument_file "
                                f"SELECT CAST(raw.instrument_id AS VARCHAR), CAST({period_column} AS VARCHAR), "
                                "CAST(raw.filename AS VARCHAR), count(*)::BIGINT, min(raw.bar_open_time), "
                                f"max(raw.bar_open_time), any_value({market_column}), any_value({asset_column}) "
                                "FROM read_parquet(?, filename=true, union_by_name=true, hive_partitioning=false) AS raw "
                                f"WHERE raw.instrument_id IS NOT NULL AND {period_column} IS NOT NULL "
                                "AND raw.bar_open_time IS NOT NULL "
                                f"GROUP BY raw.instrument_id, {period_column}, raw.filename",
                                [source_files],
                            )
                        connection.execute(
                            "DELETE FROM instrument_period WHERE instrument_id IN "
                            "(SELECT instrument_id FROM affected)"
                        )
                        connection.execute(
                            "INSERT INTO instrument_period SELECT instrument_id, period, "
                            "sum(row_count)::BIGINT, min(earliest_bar_at), max(latest_bar_at) "
                            "FROM instrument_file WHERE instrument_id IN (SELECT instrument_id FROM affected) "
                            "GROUP BY instrument_id, period"
                        )
                        affected_files = [
                            str(row[0])
                            for row in connection.execute(
                                "SELECT DISTINCT file_path FROM instrument_file WHERE instrument_id IN "
                                "(SELECT instrument_id FROM affected)"
                            ).fetchall()
                        ]
                        connection.execute(
                            "DELETE FROM instrument_latest WHERE instrument_id IN "
                            "(SELECT instrument_id FROM affected)"
                        )
                        if affected_files:
                            latest_period, latest_market, latest_asset = _parquet_layout_expressions(
                                connection, affected_files, "raw",
                            )
                            connection.execute(
                                "INSERT INTO instrument_latest "
                                "WITH bounds AS (SELECT instrument_id, max(latest_bar_at) AS latest_bar_at "
                                "FROM instrument_period WHERE instrument_id IN (SELECT instrument_id FROM affected) "
                                "GROUP BY instrument_id) "
                                f"SELECT CAST(raw.instrument_id AS VARCHAR), any_value({latest_market}), "
                                f"any_value({latest_asset}), any_value(CAST({latest_period} AS VARCHAR)), "
                                "bounds.latest_bar_at, any_value(raw.bar_json) "
                                "FROM read_parquet(?, filename=true, union_by_name=true, hive_partitioning=false) AS raw "
                                "JOIN bounds ON CAST(raw.instrument_id AS VARCHAR) = bounds.instrument_id "
                                "AND raw.bar_open_time = bounds.latest_bar_at "
                                "GROUP BY raw.instrument_id, bounds.latest_bar_at",
                                [affected_files],
                            )
                        connection.execute(
                            "DELETE FROM derived_bars WHERE instrument_id IN (SELECT instrument_id FROM affected)"
                        )
                        row_count = int(
                            connection.execute(
                                "SELECT coalesce(sum(row_count), 0) FROM instrument_period"
                            ).fetchone()[0]
                        )
                        current_catalog_digest, current_rows = _catalog_snapshot(self.data_root)
                        current_manifest = _validated_catalog_manifest(
                            self.data_root, current_rows, check_physical_set=_catalog_delta,
                        )
                        catalog_total = sum(item[2] for item in current_manifest.values())
                        if (not revision_is_settled(self.data_root, target_revision)
                                or market_unknown_epoch(self.data_root) != epoch
                                or current_catalog_digest != catalog_digest
                                or current_manifest != catalog_manifest
                                or row_count != catalog_total):
                            raise RuntimeError("Silver revision changed during query-index refresh")
                        manifest_changes = {
                            key for key in set(indexed_manifest) | set(catalog_manifest)
                            if indexed_manifest.get(key) != catalog_manifest.get(key)
                        }
                        old_manifest_paths = [
                            (indexed_manifest[key][0],) for key in sorted(manifest_changes)
                            if key in indexed_manifest
                        ]
                        new_manifest_rows = [
                            catalog_manifest[key] for key in sorted(manifest_changes)
                            if key in catalog_manifest
                        ]
                        if old_manifest_paths:
                            connection.executemany(
                                "DELETE FROM indexed_catalog_manifest WHERE file_path=?",
                                old_manifest_paths,
                            )
                        if new_manifest_rows:
                            connection.executemany(
                                "INSERT INTO indexed_catalog_manifest VALUES (?, ?, ?)",
                                new_manifest_rows,
                            )
                        connection.execute("DELETE FROM cache_meta")
                        connection.execute(
                            "INSERT INTO cache_meta VALUES (?, ?, ?, ?, ?, ?, ?)",
                            [_SCHEMA_VERSION, target_revision, row_count, _now(), 0.0, catalog_digest, epoch],
                        )
                        connection.execute("COMMIT")
                    except Exception:
                        connection.execute("ROLLBACK")
                        raise
            except Exception as error:
                _LOG.warning("incremental query-index refresh failed: %s", type(error).__name__)
                return False
            finally:
                self._release_build_lease(lease)
            self._hot.clear()
            self._hot_revision = target_revision
            self._ready_revision = target_revision
            self._ready_epoch = epoch
            return True

    def _build_locked(self, revision: str) -> None:
        """Build and commit atomically.  Called within self._lock."""
        owned_progress = self._start_progress(revision, "AUDIT_SHA")
        success = False
        try:
            building = self._build_to_file(revision)
            try:
                self._commit_build(revision, building)
                success = True
            finally:
                building.unlink(missing_ok=True)
        finally:
            if owned_progress:
                self._finish_progress(success)

    def _build_to_file(self, revision: str) -> Path:
        """Build the .building DuckDB file without touching shared state."""
        if not revision_is_settled(self.data_root, revision):
            raise KLineCacheUnavailable("Silver write is pending; query index build deferred")
        started = time.perf_counter()
        self._update_progress("AUDIT_SHA", completed=0, unit="files")
        # The SHA and row-group audit runs once per full rebuild.  Its exact
        # PASS file set is the only input to DuckDB; a wildcard could admit a
        # file published after the audit or an unregistered orphan.
        try:
            with _open_catalog(self.data_root) as catalog_connection:
                audit = audit_silver_catalog(
                    self.data_root, revision, catalog_connection=catalog_connection,
                    max_files=_MAX_CATALOG_PARTITIONS,
                    progress=self._audit_progress,
                )
        except (OSError, duckdb.Error, ValueError) as error:
            raise KLineCacheUnavailable("Silver Catalog unavailable for query-index build") from error
        if not audit.ok or not audit.catalog_digest:
            issue = audit.issues[0].code if audit.issues else audit.status
            raise KLineCacheUnavailable(f"Silver Catalog audit did not pass: {issue}")
        self._update_progress(
            "AUDIT_METADATA", completed=audit.verified_files,
            total=audit.catalog_partitions, unit="files",
        )
        catalog_digest, catalog_rows = _catalog_snapshot(self.data_root)
        if catalog_digest != audit.catalog_digest:
            raise KLineCacheUnavailable("Silver Catalog changed after query-index audit")
        catalog_manifest = _validated_catalog_manifest(self.data_root, catalog_rows)
        epoch = market_unknown_epoch(self.data_root, initialize=True)
        if epoch is None or not revision_is_settled(self.data_root, revision):
            raise KLineCacheUnavailable("Silver revision changed after query-index audit")
        source_paths = [str(path) for path in audit.files]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Use a process/thread-specific staging file.  A desktop server and a
        # maintenance command may legitimately rebuild at the same time; a
        # shared ``.building`` path lets one process delete the other's work.
        building = self.path.with_name(
            f"{self.path.name}.building-{os.getpid()}-{threading.get_ident()}"
        )
        building.unlink(missing_ok=True)
        building.with_name(building.name + ".wal").unlink(missing_ok=True)
        try:
            with duckdb.connect(str(building)) as connection:
                connection.execute("PRAGMA threads=2")
                connection.execute("SET preserve_insertion_order=false")
                if source_paths:
                    self._update_progress("INDEX_FILES", completed=0, total=2, unit="passes")
                    period_column, market_column, asset_column = _parquet_layout_expressions(
                        connection, source_paths, "raw",
                    )
                    connection.execute(
                        "CREATE TABLE instrument_file AS "
                        "SELECT CAST(instrument_id AS VARCHAR) AS instrument_id, "
                        f"CAST({period_column} AS VARCHAR) AS period, CAST(filename AS VARCHAR) AS file_path, "
                        "count(*)::BIGINT AS row_count, min(bar_open_time) AS earliest_bar_at, "
                        f"max(bar_open_time) AS latest_bar_at, any_value({market_column}) AS market, "
                        f"any_value({asset_column}) AS asset_type "
                        "FROM read_parquet(?, filename=true, union_by_name=true, hive_partitioning=false) AS raw "
                        f"WHERE instrument_id IS NOT NULL AND {period_column} IS NOT NULL "
                        f"AND bar_open_time IS NOT NULL GROUP BY instrument_id, {period_column}, filename",
                        [source_paths],
                    )
                    self._update_progress("INDEX_FILES", completed=1, total=2, unit="passes")
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
                        f"any_value({market_column}) AS market, any_value({asset_column}) AS asset_type, "
                        f"any_value(CAST({period_column} AS VARCHAR)) AS period, "
                        "bounds.latest_bar_at AS bar_open_time, any_value(raw.bar_json) AS bar_json "
                        "FROM read_parquet(?, filename=true, union_by_name=true, hive_partitioning=false) AS raw "
                        "JOIN bounds ON CAST(raw.instrument_id AS VARCHAR) = bounds.instrument_id "
                        "AND raw.bar_open_time = bounds.latest_bar_at "
                        "GROUP BY raw.instrument_id, bounds.latest_bar_at",
                        [source_paths],
                    )
                    self._update_progress("INDEX_FILES", completed=2, total=2, unit="passes")
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
                self._update_progress("INDEX_METADATA")
                connection.execute(
                    "CREATE TABLE cache_meta (schema_version INTEGER, revision VARCHAR, row_count BIGINT, "
                    "built_at VARCHAR, build_seconds DOUBLE, catalog_digest VARCHAR, unknown_epoch BIGINT)"
                )
                connection.execute(
                    "CREATE TABLE indexed_catalog_manifest "
                    "(file_path VARCHAR PRIMARY KEY, sha256 VARCHAR, row_count BIGINT)"
                )
                if catalog_manifest:
                    connection.executemany(
                        "INSERT INTO indexed_catalog_manifest VALUES (?, ?, ?)",
                        [item for item in catalog_manifest.values()],
                    )
                connection.execute(
                    "CREATE INDEX file_lookup ON instrument_file (instrument_id, period, earliest_bar_at, latest_bar_at)"
                )
                connection.execute("CREATE INDEX period_lookup ON instrument_period (instrument_id, period)")
                rows = int(
                    connection.execute("SELECT coalesce(sum(row_count), 0) FROM instrument_period").fetchone()[0]
                )
                connection.execute(
                    "INSERT INTO cache_meta VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [_SCHEMA_VERSION, revision, rows, _now(), time.perf_counter() - started,
                     audit.catalog_digest, epoch],
                )
        except Exception:
            building.unlink(missing_ok=True)
            building.with_name(building.name + ".wal").unlink(missing_ok=True)
            raise
        return building

    def _audit_progress(self, state: dict[str, Any]) -> None:
        stage = "AUDIT_METADATA" if state.get("stage") == "PARQUET_METADATA" else "AUDIT_SHA"
        completed_key = "verifiedFiles" if stage == "AUDIT_METADATA" else "hashedFiles"
        completed = state.get(completed_key)
        total = state.get("catalogPartitions")
        self._update_progress(
            stage, completed=completed if isinstance(completed, int) else None,
            total=total if isinstance(total, int) else None, unit="files",
        )

    def _commit_build(self, revision: str, building: Path) -> None:
        """Atomically swap the built file and update shared state.  Called within self._lock."""
        self._update_progress("PUBLISHING")
        if not revision_is_settled(self.data_root, revision):
            raise RuntimeError("Silver revision changed during query-index build")
        with duckdb.connect(str(building), read_only=True) as connection:
            row = connection.execute(
                "SELECT revision,catalog_digest,row_count,unknown_epoch "
                "FROM cache_meta WHERE schema_version=? LIMIT 1",
                [_SCHEMA_VERSION],
            ).fetchone()
        catalog_digest, catalog_rows = _catalog_snapshot(self.data_root)
        catalog_total = sum(int(item[2]) for item in catalog_rows)
        if (row is None or row[0] != revision or not row[1]
                or catalog_digest != row[1] or int(row[2]) != catalog_total
                or row[3] != market_unknown_epoch(self.data_root)
                or not revision_is_settled(self.data_root, revision)):
            raise RuntimeError("Silver Catalog or revision changed during query-index build")
        from .atomic_files import replace_with_retry

        replace_with_retry(building, self.path)
        self._hot.clear()
        self._hot_revision = revision
        self._ready_revision = revision
        self._ready_epoch = int(row[3])

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
            owned = re.fullmatch(re.escape(self.path.name) + r"\.building-(\d+)-\d+(?:\.wal)?", candidate.name)
            if owned and not _process_is_running(int(owned[1])):
                continue
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

    def _metadata(self) -> tuple[str, int, str | None, float | None, int | None] | None:
        if not self.path.is_file():
            return None
        try:
            with self._connect(read_only=True) as connection:
                row = connection.execute(
                    "SELECT revision, row_count, built_at, build_seconds, unknown_epoch FROM cache_meta "
                    "WHERE schema_version = ? AND catalog_digest IS NOT NULL LIMIT 1",
                    [_SCHEMA_VERSION],
                ).fetchone()
            if row is None:
                return None
            return (
                str(row[0]), int(row[1]), _text(row[2]),
                float(row[3]) if row[3] is not None else None,
                int(row[4]) if isinstance(row[4], int) and not isinstance(row[4], bool) else None,
            )
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

    def _assert_read_revision(self, revision: str) -> None:
        if not revision_is_settled(self.data_root, revision):
            raise KLineCacheUnavailable("Silver revision changed during K-line query")

    def _hot_put_checked(self, key: tuple[Any, ...], value: Any, revision: str) -> None:
        """Cache a completed read only while the indexed Silver revision holds."""

        self._assert_read_revision(revision)
        self._hot_put(key, value)
        try:
            self._assert_read_revision(revision)
        except KLineCacheUnavailable:
            with self._lock:
                self._hot.pop(key, None)
            raise


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
    file: Path | str,
) -> bool:
    return get_kline_query_store(data_root).apply_silver_update(previous_revision, current_revision, file)


def refresh_kline_cache_partitions(
    data_root: Path,
    files: Iterable[Path | str],
    *,
    expected_base_revision: str | None = None,
    target_revision: str | None = None,
) -> bool:
    """Refresh a complete write batch from the exact indexed base revision."""

    return get_kline_query_store(data_root).refresh_partition_files(
        files, expected_base_revision=expected_base_revision, target_revision=target_revision,
    )


def _where(instrument_id: str, period: str | None) -> tuple[str, list[str]]:
    if period:
        return "instrument_id = ? AND period = ?", [instrument_id, period]
    return "instrument_id = ?", [instrument_id]


def _intraday_quality_clause(instrument_id: str, period: str | None) -> str:
    if not instrument_id.startswith("CN.") or ".STOCK." not in instrument_id or period not in {None, "30m"}:
        return ""
    return (" AND NOT (coalesce(json_extract_string(bar_json, '$.source'), '') = 'pytdx' "
            "AND coalesce(json_extract_string(bar_json, '$.period'), '') = '30m' "
            "AND substr(CAST(bar_open_time AS VARCHAR), 12, 8) = '00:00:00')")


def _filtered_intraday_bounds(files: Iterable[str], instrument_id: str, period: str) -> tuple[int, str | None, str | None]:
    groups: dict[bool, list[str]] = {False: [], True: []}
    for value in files:
        groups["period=" in Path(value).as_posix()].append(value)
    branches, parameters = [], []
    for hive_partitioning, source_files in groups.items():
        if not source_files:
            continue
        where, values = _where(instrument_id, period)
        where += _intraday_quality_clause(instrument_id, period)
        branches.append("SELECT bar_open_time FROM read_parquet(?, union_by_name=true, "
                        f"hive_partitioning={str(hive_partitioning).lower()}) WHERE {where}")
        parameters.extend([source_files, *values])
    if not branches:
        return 0, None, None
    with duckdb.connect(database=":memory:") as connection:
        row = connection.execute("SELECT count(DISTINCT bar_open_time), min(bar_open_time), max(bar_open_time) FROM ("
                                 + " UNION ALL ".join(branches) + ")", parameters).fetchone()
    return int(row[0]), _text(row[1]), _text(row[2])


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
        where += _intraday_quality_clause(instrument_id, period)
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
    if os.name == "nt":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        kernel.GetExitCodeProcess.restype = ctypes.c_int
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel.CloseHandle.restype = ctypes.c_int
        handle = kernel.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # Only ERROR_INVALID_PARAMETER proves the PID no longer exists.
            return ctypes.get_last_error() != 87
        try:
            status = ctypes.c_ulong()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(status)) or status.value == 259
        finally:
            kernel.CloseHandle(handle)
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
