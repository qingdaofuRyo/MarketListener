"""Read-only, bounded reconciliation of published Silver files and Catalog.

This is an offline/publication gate, not a request-path check.  The caller
supplies the already observed Silver revision so the audit never initializes
or rewrites the revision file.  A matching revision alone does not prove that
Catalog and the mutable Parquet paths describe the same bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import duckdb

from .market_data_version import revision_is_settled


_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_HASH_CHUNK_BYTES = 1024 * 1024
_METADATA_BATCH_SIZE = 64


@dataclass(frozen=True)
class SilverCatalogIssue:
    code: str
    path: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class SilverCatalogAudit:
    status: str  # PASS, FAILED, or RETRY if the source changed during audit.
    revision: str
    catalog_digest: str | None
    catalog_partitions: int
    physical_files: int
    verified_files: int
    hashed_bytes: int
    issue_count: int
    issues: tuple[SilverCatalogIssue, ...]
    files: tuple[Path, ...]  # Exact audited files; exposed only for PASS.

    @property
    def ok(self) -> bool:
        return self.status == "PASS"


def audit_silver_catalog(
    data_root: Path,
    expected_revision: str,
    *,
    catalog_connection: duckdb.DuckDBPyConnection | None = None,
    max_files: int = 100_000,
    max_file_bytes: int = 32 * 1024**3,
    max_total_bytes: int = 2 * 1024**4,
    max_seconds: float = 1_800.0,
    max_reported_issues: int = 32,
    progress: Callable[[dict[str, int]], None] | None = None,
) -> SilverCatalogAudit:
    """Compare every COMPLETE Catalog partition with the exact Silver bytes.

    File SHA-256 is streamed in 1 MiB chunks; Parquet row counts come from
    bounded batches of row-group metadata instead of scanning all bars.  The
    Catalog is queried twice and the Silver revision must be settled at both
    ends.  The optional connection is borrowed and only receives SELECTs;
    without one, the Catalog is opened in read-only mode.  Busy or damaged
    sources fail closed.  No revision or runtime data is written.
    """

    root = Path(data_root).resolve()
    revision = str(expected_revision or "")
    issues: list[SilverCatalogIssue] = []
    issue_count = 0
    catalog_digest: str | None = None
    catalog_partitions = 0
    physical_files = 0
    verified_files = 0
    hashed_files = 0
    hashed_bytes = 0
    deadline = time.monotonic() + max_seconds
    next_progress = time.monotonic() + 2.0

    def report_issue(code: str, path: str | None = None, detail: str | None = None) -> None:
        nonlocal issue_count
        issue_count += 1
        if len(issues) < max_reported_issues:
            issues.append(SilverCatalogIssue(code, path, detail))

    def result(*, retry: bool = False, files: tuple[Path, ...] = ()) -> SilverCatalogAudit:
        return SilverCatalogAudit(
            "RETRY" if retry else "FAILED" if issue_count else "PASS", revision,
            catalog_digest, catalog_partitions, physical_files, verified_files,
            hashed_bytes, issue_count, tuple(issues), files if issue_count == 0 else (),
        )

    if (not revision or max_files < 1 or max_file_bytes < 1 or max_total_bytes < 1
            or max_seconds <= 0 or max_reported_issues < 1):
        report_issue("INVALID_AUDIT_ARGUMENT")
        return result()
    if not revision_is_settled(root, revision):
        report_issue("REVISION_UNSETTLED")
        return result(retry=True)

    catalog_path = root / "catalog.duckdb"
    if not catalog_path.is_file():
        report_issue("CATALOG_MISSING")
        return result()

    owned_connection: duckdb.DuckDBPyConnection | None = None
    try:
        connection = catalog_connection
        if connection is None:
            owned_connection = duckdb.connect(str(catalog_path), read_only=True)
            connection = owned_connection
        rows = _catalog_rows(connection, max_files)
        if len(rows) > max_files:
            report_issue("FILE_LIMIT_EXCEEDED")
            return result()
        catalog_partitions = len(rows)
        catalog_digest = _catalog_digest(rows)
        if not revision_is_settled(root, revision):
            report_issue("REVISION_CHANGED")
            return result(retry=True)

        silver_root = root / "silver"
        expected: dict[str, tuple[Path, int, str]] = {}
        for partition_id, raw_path, raw_count, raw_sha, status in rows:
            relative = _valid_silver_relative_path(raw_path)
            if relative is None:
                report_issue("INVALID_CATALOG_PATH", str(raw_path))
                continue
            path = root.joinpath(*relative.split("/"))
            if not _within_silver_root(path, silver_root):
                report_issue("INVALID_CATALOG_PATH", relative)
                continue
            identity = relative.casefold()
            if identity in expected:
                report_issue("DUPLICATE_CATALOG_PATH", relative)
                continue
            if status != "COMPLETE":
                report_issue("INVALID_CATALOG_STATUS", relative, str(status))
            if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 1:
                report_issue("INVALID_CATALOG_ROW_COUNT", relative, str(raw_count))
                count = -1
            else:
                count = raw_count
            if not isinstance(raw_sha, str) or _SHA256.fullmatch(raw_sha) is None:
                report_issue("INVALID_CATALOG_SHA", relative)
                digest = ""
            else:
                digest = raw_sha.lower()
            expected[identity] = (path, count, digest)

        physical = _physical_silver_paths(silver_root, max_files, deadline, report_issue)
        if physical is None:
            return result(retry=True)
        physical_files = len(physical)
        for identity in sorted(set(expected) - set(physical)):
            report_issue("MISSING_PARQUET", expected[identity][0].relative_to(root).as_posix())
        for identity in sorted(set(physical) - set(expected)):
            report_issue("ORPHAN_PARQUET", physical[identity].relative_to(root).as_posix())

        verified_candidates: list[tuple[Path, int, tuple[int, int, int, int]]] = []
        for identity in sorted(set(expected) & set(physical)):
            if time.monotonic() >= deadline:
                report_issue("AUDIT_TIME_LIMIT_EXCEEDED")
                return result(retry=True)
            path, expected_count, expected_sha = expected[identity]
            try:
                before = _stat_identity(path)
                if before[2] > max_file_bytes or hashed_bytes + before[2] > max_total_bytes:
                    report_issue("BYTE_LIMIT_EXCEEDED", path.relative_to(root).as_posix())
                    break
                actual_sha = _stream_sha256(path, expected_bytes=before[2], deadline=deadline)
                hashed_files += 1
                hashed_bytes += before[2]
                if _stat_identity(path) != before:
                    report_issue("FILE_CHANGED", path.relative_to(root).as_posix())
                    continue
            except TimeoutError:
                report_issue("AUDIT_TIME_LIMIT_EXCEEDED")
                return result(retry=True)
            except OSError as error:
                report_issue("FILE_READ_FAILED", path.relative_to(root).as_posix(), type(error).__name__)
                continue
            if actual_sha != expected_sha:
                report_issue("SHA_MISMATCH", path.relative_to(root).as_posix())
                continue
            if expected_count >= 1:
                verified_candidates.append((path, expected_count, before))
            if progress is not None and time.monotonic() >= next_progress:
                progress({"hashedFiles": hashed_files, "hashedBytes": hashed_bytes,
                          "catalogPartitions": catalog_partitions})
                next_progress = time.monotonic() + 2.0

        with duckdb.connect(":memory:") as metadata_connection:
            for offset in range(0, len(verified_candidates), _METADATA_BATCH_SIZE):
                if time.monotonic() >= deadline:
                    report_issue("AUDIT_TIME_LIMIT_EXCEEDED")
                    return result(retry=True)
                batch = verified_candidates[offset:offset + _METADATA_BATCH_SIZE]
                paths = [str(item[0]) for item in batch]
                try:
                    counts = {
                        str(Path(name).resolve()).casefold(): int(count)
                        for name, count in metadata_connection.execute(
                            "SELECT file_name, sum(row_group_num_rows)::BIGINT "
                            "FROM (SELECT DISTINCT file_name, row_group_id, row_group_num_rows "
                            "FROM parquet_metadata(?)) GROUP BY file_name",
                            [paths],
                        ).fetchall()
                    }
                except (duckdb.Error, OSError, ValueError) as error:
                    report_issue("PARQUET_METADATA_FAILED", detail=type(error).__name__)
                    continue
                for path, expected_count, before in batch:
                    relative = path.relative_to(root).as_posix()
                    actual_count = counts.get(str(path.resolve()).casefold())
                    if actual_count != expected_count:
                        report_issue("ROW_COUNT_MISMATCH", relative, str(actual_count))
                        continue
                    try:
                        if _stat_identity(path) != before:
                            report_issue("FILE_CHANGED", relative)
                            continue
                    except OSError as error:
                        report_issue("FILE_READ_FAILED", relative, type(error).__name__)
                        continue
                    verified_files += 1

        if not revision_is_settled(root, revision):
            report_issue("REVISION_CHANGED")
            return result(retry=True)
        if _catalog_digest(_catalog_rows(connection, max_files)) != catalog_digest:
            report_issue("CATALOG_CHANGED")
            return result(retry=True)
        final_physical = _physical_silver_paths(silver_root, max_files, deadline, report_issue)
        if final_physical is None:
            return result(retry=True)
        if set(final_physical) != set(physical):
            report_issue("SILVER_FILE_SET_CHANGED")
            return result(retry=True)
        if not revision_is_settled(root, revision):
            report_issue("REVISION_CHANGED")
            return result(retry=True)
        return result(files=tuple(expected[key][0] for key in sorted(expected)))
    except (duckdb.Error, OSError, ValueError, TypeError) as error:
        report_issue("AUDIT_SOURCE_ERROR", detail=type(error).__name__)
        return result()
    finally:
        if owned_connection is not None:
            owned_connection.close()


def _catalog_rows(connection: duckdb.DuckDBPyConnection, max_files: int) -> list[tuple[Any, ...]]:
    return connection.execute(
        "SELECT partition_id,file_path,row_count,sha256,status FROM partitions "
        "ORDER BY partition_id LIMIT ?", [max_files + 1],
    ).fetchall()


def _catalog_digest(rows: list[tuple[Any, ...]]) -> str:
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _valid_silver_relative_path(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw or len(raw) > 1024 or raw.startswith(("/", "\\")) or ":" in raw:
        return None
    parts = raw.replace("\\", "/").split("/")
    if len(parts) < 2 or parts[0].casefold() != "silver" or any(part in ("", ".", "..") for part in parts):
        return None
    if not parts[-1].lower().endswith(".parquet"):
        return None
    return "/".join(parts)


def _within_silver_root(path: Path, silver_root: Path) -> bool:
    try:
        path.resolve().relative_to(silver_root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _physical_silver_paths(
    silver_root: Path, max_files: int, deadline: float, report_issue: Any,
) -> dict[str, Path] | None:
    paths: dict[str, Path] = {}
    if not silver_root.is_dir():
        return paths
    root = silver_root.parent
    for path in silver_root.rglob("*"):
        if time.monotonic() >= deadline:
            report_issue("AUDIT_TIME_LIMIT_EXCEEDED")
            return None
        if path.suffix.casefold() != ".parquet":
            continue
        if len(paths) >= max_files:
            report_issue("FILE_LIMIT_EXCEEDED")
            return None
        relative = path.relative_to(root).as_posix()
        if not path.is_file() or not _within_silver_root(path, silver_root):
            report_issue("INVALID_PHYSICAL_PATH", relative)
            continue
        identity = relative.casefold()
        if identity in paths:
            report_issue("DUPLICATE_PHYSICAL_PATH", relative)
        paths[identity] = path
    return paths


def _stat_identity(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _stream_sha256(path: Path, *, expected_bytes: int, deadline: float) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        remaining = expected_bytes
        while remaining:
            if time.monotonic() >= deadline:
                raise TimeoutError("Silver SHA audit exceeded its time budget")
            chunk = stream.read(min(_HASH_CHUNK_BYTES, remaining))
            if not chunk:
                raise OSError("Silver file shrank during SHA audit")
            digest.update(chunk)
            remaining -= len(chunk)
        if stream.read(1):
            raise OSError("Silver file grew during SHA audit")
    return digest.hexdigest()


__all__ = ("SilverCatalogAudit", "SilverCatalogIssue", "audit_silver_catalog")
