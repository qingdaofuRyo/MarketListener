"""Audit a DCE ranking ZIP downloaded by a visible desktop browser.

This path never makes an exchange request or writes Silver. It reads only the
specified download row from a Chrome/Edge History database and keeps its
evidence separate from Android-browser captures.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile
from typing import Any, Iterator
from urllib.parse import urlsplit
from zipfile import ZipFile

from .dce_batch_rank_import import prepare_dce_batch_day
from .dce_batch_zip import DceBatchZipResult, parse_dce_batch_zip


SCHEMA = "dce-desktop-browser-download/v1"
ADAPTER = "official-dce-desktop-browser-download-raw-v1"
BRONZE = Path("bronze") / "dce_desktop_download"
_ANDROID_BRONZE = Path("bronze") / "dce_dom_capture"
_OFFICIAL_HOST = "www.dce.com.cn"
_RANKING_PATHS = {
    "/dce/channel/list/176.html",
    "/dalianshangpin/xqsj/tjsj26/rtj/rcjccpm/index.html",
    "/frontend/dcereport/",
}
_MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
_WINDOWS_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class DesktopDownloadEvidence:
    browser: str
    page: str
    blob_origin: str
    downloaded_at_utc: str
    byte_count: int


def _official_url(value: str, *, page: bool) -> str | None:
    """Return a query-free official page or Blob origin, never a tokenized URL."""

    if not isinstance(value, str):
        return None
    if page:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or parsed.hostname != _OFFICIAL_HOST
                or parsed.username or parsed.password or parsed.path not in _RANKING_PATHS):
            return None
        try:
            if parsed.port not in (None, 80 if parsed.scheme == "http" else 443):
                return None
        except ValueError:
            return None
        return f"{parsed.scheme}://{_OFFICIAL_HOST}{parsed.path}"

    if not value.startswith("blob:"):
        return None
    inner = urlsplit(value[5:])
    if (inner.scheme not in {"http", "https"} or inner.hostname != _OFFICIAL_HOST
            or inner.username or inner.password or not inner.path.startswith("/")):
        return None
    try:
        if inner.port not in (None, 80 if inner.scheme == "http" else 443):
            return None
    except ValueError:
        return None
    return f"blob:{inner.scheme}://{_OFFICIAL_HOST}"


def _download_time(value: int) -> datetime:
    try:
        timestamp = _WINDOWS_EPOCH + timedelta(microseconds=int(value))
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError("DCE_DESKTOP_DOWNLOAD_TIME_INVALID") from error
    if not 2020 <= timestamp.year <= datetime.now(timezone.utc).year + 1:
        raise ValueError("DCE_DESKTOP_DOWNLOAD_TIME_INVALID")
    return timestamp


def verify_browser_download(history: Path, archive: Path, *, browser: str) -> DesktopDownloadEvidence:
    """Inspect only the exact requested file's download record and URL chain."""

    if browser not in {"chrome", "edge"}:
        raise ValueError("DCE_DESKTOP_BROWSER_UNSUPPORTED")
    history, archive = Path(history).resolve(), Path(archive).resolve(strict=True)
    if not history.is_file():
        raise FileNotFoundError("DCE_DESKTOP_HISTORY_MISSING")
    archive_size = archive.stat().st_size
    if not 0 < archive_size <= _MAX_ARCHIVE_BYTES:
        raise ValueError("DCE_DESKTOP_ARCHIVE_SIZE_INVALID")

    try:
        with sqlite3.connect(f"{history.as_uri()}?mode=ro", uri=True, timeout=1) as connection:
            connection.row_factory = sqlite3.Row
            # Do not read visits, cookies or any unrelated download rows.
            matches = connection.execute(
                "SELECT id, target_path, current_path, state, received_bytes, "
                "total_bytes, end_time, tab_url "
                "FROM downloads WHERE target_path = ? COLLATE NOCASE "
                "OR current_path = ? COLLATE NOCASE ORDER BY end_time DESC LIMIT 5",
                (str(archive), str(archive)),
            ).fetchall()
            if not matches:
                raise ValueError("DCE_DESKTOP_HISTORY_NO_MATCH")
            for row in matches:
                if (row["state"] != 1 or row["received_bytes"] != archive_size
                        or row["total_bytes"] != archive_size):
                    continue
                page = _official_url(row["tab_url"], page=True)
                if page is None:
                    continue
                downloaded = _download_time(row["end_time"])
                if abs(archive.stat().st_mtime - downloaded.timestamp()) > 15 * 60:
                    continue
                urls = connection.execute(
                    "SELECT url FROM downloads_url_chains WHERE id = ? ORDER BY chain_index",
                    (row["id"],),
                )
                origin = next(
                    (result for chain in urls if (result := _official_url(chain["url"], page=False))),
                    None,
                )
                if origin is None:
                    continue
                return DesktopDownloadEvidence(
                    browser=browser, page=page, blob_origin=origin,
                    downloaded_at_utc=downloaded.isoformat(timespec="seconds"),
                    byte_count=archive_size,
                )
    except sqlite3.OperationalError as error:
        if "locked" in str(error).lower():
            raise RuntimeError("DCE_DESKTOP_HISTORY_LOCKED") from error
        raise RuntimeError("DCE_DESKTOP_HISTORY_UNREADABLE") from error
    except sqlite3.DatabaseError as error:
        raise RuntimeError("DCE_DESKTOP_HISTORY_UNREADABLE") from error
    raise ValueError("DCE_DESKTOP_HISTORY_PROVENANCE_FAILED")


def _atomic_bytes(path: Path, raw: bytes) -> None:
    with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        # Publishing a complete hard link fails if another writer already owns
        # the target. os.replace would silently overwrite that writer's proof.
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _capture_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError("DCE_DESKTOP_CAPTURE_BUSY") from error
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise RuntimeError("DCE_DESKTOP_CAPTURE_BUSY") from error
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_size, left.st_mtime_ns, left.st_ctime_ns, left.st_ino,
    ) == (
        right.st_size, right.st_mtime_ns, right.st_ctime_ns, right.st_ino,
    )


def _same_zip_members(left: bytes, right: bytes) -> bool:
    """Compare validated reports, not ZIP timestamps or compression bytes."""

    with ZipFile(BytesIO(left)) as left_zip, ZipFile(BytesIO(right)) as right_zip:
        names = set(left_zip.namelist())
        if names != set(right_zip.namelist()):
            return False
        return all(left_zip.read(name) == right_zip.read(name) for name in names)


def audit_desktop_batch_download(
    archive: Path, history: Path, *, browser: str, trading_day: str,
    data_root: Path | None = None, capture: bool = False,
) -> dict[str, Any]:
    """Validate the browser receipt and ZIP; optionally save isolated Bronze."""

    try:
        day = date.fromisoformat(trading_day).isoformat()
    except ValueError as error:
        raise ValueError("DCE_DESKTOP_TRADE_DATE_INVALID") from error
    if capture and data_root is None:
        raise ValueError("DCE_DESKTOP_CAPTURE_ROOT_REQUIRED")
    archive = Path(archive).resolve(strict=True)
    initial_state = archive.stat()
    evidence = verify_browser_download(history, archive, browser=browser)
    with archive.open("rb") as stream:
        opened_state = os.fstat(stream.fileno())
        raw = stream.read(_MAX_ARCHIVE_BYTES + 1)
        read_state = os.fstat(stream.fileno())
    if (not _same_file_state(initial_state, opened_state)
            or not _same_file_state(opened_state, read_state)
            or not _same_file_state(read_state, archive.stat())):
        raise ValueError("DCE_DESKTOP_ARCHIVE_CHANGED")
    parsed: DceBatchZipResult = parse_dce_batch_zip(raw, trading_day=day.replace("-", ""))
    if len(raw) != evidence.byte_count:
        raise ValueError("DCE_DESKTOP_ARCHIVE_CHANGED")
    result: dict[str, Any] = {
        "status": "VALIDATED", "tradeDate": day, "browser": browser,
        "sourcePage": evidence.page, "downloadOrigin": evidence.blob_origin,
        "downloadedAtUtc": evidence.downloaded_at_utc,
        "bytes": len(raw), "payloadSha256": parsed.archive_sha256,
        "summary": parsed.summary, "authoritativeDataWritten": False,
    }
    if not capture:
        return result

    root = Path(data_root)
    compact = day.replace("-", "")
    with _capture_lock(root / "state" / "dce_desktop_download" / f"{compact}.lock"):
        directory = root / BRONZE
        zip_target = directory / f"{compact}.zip"
        manifest_target = directory / f"{compact}.json"
        if zip_target.exists() or manifest_target.exists():
            raise FileExistsError("DCE_DESKTOP_CAPTURE_EXISTS")
        android_manifest = root / _ANDROID_BRONZE / f"{compact}.json"
        if android_manifest.is_file():
            try:
                existing = json.loads(android_manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ValueError("DCE_DESKTOP_EXISTING_SOURCE_UNREADABLE") from error
            if isinstance(existing, dict) and existing.get("status") == "PASS_RAW":
                try:
                    prepared = prepare_dce_batch_day(root, day)
                    android_raw = (root / _ANDROID_BRONZE / f"{compact}.zip").read_bytes()
                    if sha256(android_raw).hexdigest() != prepared.archive_sha256:
                        raise ValueError("DCE_DESKTOP_EXISTING_SOURCE_INVALID")
                    if not _same_zip_members(raw, android_raw):
                        raise ValueError("DCE_DESKTOP_SOURCE_CONFLICT")
                except (OSError, ValueError) as error:
                    if str(error) == "DCE_DESKTOP_SOURCE_CONFLICT":
                        raise
                    raise ValueError("DCE_DESKTOP_EXISTING_SOURCE_INVALID") from error
                return {**result, "status": "SKIPPED_EXISTING_PASS",
                        "comparison": "IDENTICAL_MEMBER_CONTENT"}

        directory.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema": SCHEMA, "publisher": "DCE", "adapter": ADAPTER,
            "source": evidence.page, "tradeDate": day,
            "method": "manual visible Chrome/Edge button download; History Blob record",
            "browser": browser, "downloadOrigin": evidence.blob_origin,
            "downloadedAtUtc": evidence.downloaded_at_utc,
            "bytes": len(raw), "payloadSha256": parsed.archive_sha256,
            "summary": parsed.summary, "status": "PASS_RAW",
            "qualityStatus": "PASS_RAW", "authoritativeDataWritten": False,
            "httpStatus": None,
        }
        _atomic_bytes(zip_target, raw)
        published_state = zip_target.stat()
        try:
            _atomic_bytes(manifest_target, json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        except BaseException:
            if zip_target.exists() and _same_file_state(published_state, zip_target.stat()):
                zip_target.unlink()
            raise
    return {**result, "status": "CAPTURED_RAW"}


__all__ = ("audit_desktop_batch_download", "verify_browser_download")
