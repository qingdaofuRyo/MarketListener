"""Desktop browser download proof remains isolated from Android Bronze/Silver."""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from market_monitor.dce_desktop_batch_capture import (
    ADAPTER, BRONZE, SCHEMA, _capture_lock, audit_desktop_batch_download,
    verify_browser_download,
)
from market_monitor.dce_batch_rank_import import CAPTURE_ADAPTER, CAPTURE_SCHEMA
from market_monitor.dce_batch_zip import parse_dce_batch_zip


DAY = "2026-10-16"
COMPACT = "20261016"
PAGE = "http://www.dce.com.cn/dce/channel/list/176.html"
BLOB = "blob:http://www.dce.com.cn/a-synthetic-object-id"


def _archive(*, compression: int = ZIP_STORED, quantity: int = 100) -> bytes:
    lines = [
        "大连商品交易所 成交量_买持仓_卖持仓排名",
        "合约代码：a2611\tDate：2026-10-16", "期货公司会员",
    ]
    for label in ("成交量", "持买单量", "持卖单量"):
        lines.extend((f"名次\t会员简称\t{label}\t增减", f"1\t合成会员\t{quantity}\t-2",
                      f"合计\t{quantity}\t-2"))
    raw = BytesIO()
    with ZipFile(raw, "w", compression=compression) as zip_file:
        zip_file.writestr(f"{COMPACT}_a2611_成交量_买持仓_卖持仓排名.txt",
                          ("\r\n".join(lines) + "\r\n").encode())
    return raw.getvalue()


def _history(tmp_path: Path, archive: Path, *, page: str = PAGE, blob: str = BLOB,
             state: int = 1, byte_delta: int = 0) -> Path:
    history = tmp_path / "History"
    end_time = round((datetime.fromtimestamp(archive.stat().st_mtime, timezone.utc)
                      - datetime(1601, 1, 1, tzinfo=timezone.utc)).total_seconds() * 1_000_000)
    with sqlite3.connect(history) as db:
        db.execute("CREATE TABLE downloads (id INTEGER PRIMARY KEY, target_path TEXT, "
                   "current_path TEXT, state INTEGER, received_bytes INTEGER, "
                   "total_bytes INTEGER, end_time INTEGER, tab_url TEXT, site_url TEXT)")
        db.execute("CREATE TABLE downloads_url_chains (id INTEGER, chain_index INTEGER, url TEXT)")
        db.execute("INSERT INTO downloads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (7, str(archive), str(archive), state, archive.stat().st_size + byte_delta,
                    archive.stat().st_size + byte_delta, end_time, page, PAGE))
        db.execute("INSERT INTO downloads_url_chains VALUES (?, ?, ?)", (7, 0, blob))
        # A different download exists; the auditor must never include its details.
        db.execute("INSERT INTO downloads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (8, "private-path", "private-path", 1, 42, 42, end_time,
                    "https://unrelated.example/private", "https://unrelated.example/private"))
    return history


def test_audit_validates_only_matching_official_blob_and_dated_zip(tmp_path: Path) -> None:
    archive = tmp_path / "report.zip"
    archive.write_bytes(_archive())
    history = _history(tmp_path, archive)

    report = audit_desktop_batch_download(
        archive, history, browser="edge", trading_day=DAY,
    )

    assert report["status"] == "VALIDATED"
    assert report["summary"]["rankedContractCount"] == 1
    assert report["sourcePage"] == PAGE
    assert report["downloadOrigin"] == "blob:http://www.dce.com.cn"
    assert "private" not in json.dumps(report)
    assert not (tmp_path / BRONZE).exists()

@pytest.mark.parametrize("page,blob,state,delta", [
    ("http://other.example/dce/channel/list/176.html", BLOB, 1, 0),
    (PAGE, "blob:http://other.example/file", 1, 0),
    (PAGE, "http://www.dce.com.cn/dcereport/file.zip", 1, 0),
    (PAGE, BLOB, 0, 0),
    (PAGE, BLOB, 1, 1),
])
def test_audit_rejects_unproved_or_incomplete_download(
    tmp_path: Path, page: str, blob: str, state: int, delta: int,
) -> None:
    archive = tmp_path / "report.zip"
    archive.write_bytes(_archive())
    history = _history(tmp_path, archive, page=page, blob=blob, state=state, byte_delta=delta)

    with pytest.raises(ValueError, match="DCE_DESKTOP_HISTORY_PROVENANCE_FAILED"):
        verify_browser_download(history, archive, browser="chrome")


def test_audit_rejects_zip_for_different_day(tmp_path: Path) -> None:
    archive = tmp_path / "report.zip"
    archive.write_bytes(_archive())
    history = _history(tmp_path, archive)
    with pytest.raises(ValueError, match="unexpected entry"):
        audit_desktop_batch_download(archive, history, browser="chrome", trading_day="2026-10-15")


def test_capture_writes_separate_bronze_without_fabricated_http_status(tmp_path: Path) -> None:
    archive = tmp_path / "report.zip"
    archive.write_bytes(_archive())
    history = _history(tmp_path, archive)
    data_root = tmp_path / "data_control"

    report = audit_desktop_batch_download(
        archive, history, browser="chrome", trading_day=DAY,
        data_root=data_root, capture=True,
    )

    manifest = json.loads((data_root / BRONZE / f"{COMPACT}.json").read_text(encoding="utf-8"))
    assert report["status"] == "CAPTURED_RAW"
    assert (data_root / BRONZE / f"{COMPACT}.zip").read_bytes() == archive.read_bytes()
    assert manifest["schema"] == SCHEMA
    assert manifest["adapter"] == ADAPTER
    assert manifest["httpStatus"] is None
    assert manifest["authoritativeDataWritten"] is False
    with pytest.raises(FileExistsError, match="DCE_DESKTOP_CAPTURE_EXISTS"):
        audit_desktop_batch_download(
            archive, history, browser="chrome", trading_day=DAY,
            data_root=data_root, capture=True,
        )


def test_capture_skips_day_already_covered_by_android(tmp_path: Path) -> None:
    archive = tmp_path / "report.zip"
    archive.write_bytes(_archive())
    history = _history(tmp_path, archive)
    data_root = tmp_path / "data_control"
    android = data_root / "bronze" / "dce_dom_capture"
    android.mkdir(parents=True)
    old_raw = _archive(compression=ZIP_DEFLATED)
    assert old_raw != archive.read_bytes()
    _android_capture(android, old_raw)

    report = audit_desktop_batch_download(
        archive, history, browser="edge", trading_day=DAY,
        data_root=data_root, capture=True,
    )

    assert report["status"] == "SKIPPED_EXISTING_PASS"
    assert report["comparison"] == "IDENTICAL_MEMBER_CONTENT"
    assert not (data_root / BRONZE).exists()


def _android_capture(directory: Path, raw: bytes) -> None:
    parsed = parse_dce_batch_zip(raw, trading_day=COMPACT)
    (directory / f"{COMPACT}.zip").write_bytes(raw)
    (directory / f"{COMPACT}.json").write_text(json.dumps({
        "schema": CAPTURE_SCHEMA, "publisher": "DCE", "adapter": CAPTURE_ADAPTER,
        "source": PAGE, "batchPath": "/dcereport/publicweb/dailystat/memberDealPosi/batchDownload",
        "tradeDate": DAY, "httpStatus": 200, "contentType": "application/zip",
        "bytes": len(raw), "payloadSha256": parsed.archive_sha256,
        "summary": parsed.summary, "capturedAtUtc": "2026-10-17T00:00:00+00:00",
        "status": "PASS_RAW", "qualityStatus": "PASS_RAW", "authoritativeDataWritten": False,
    }), encoding="utf-8")


def test_capture_rejects_existing_android_status_without_valid_evidence(tmp_path: Path) -> None:
    archive = tmp_path / "report.zip"
    archive.write_bytes(_archive())
    history = _history(tmp_path, archive)
    root = tmp_path / "data_control"
    android = root / "bronze" / "dce_dom_capture"
    android.mkdir(parents=True)
    (android / f"{COMPACT}.json").write_text(json.dumps({"status": "PASS_RAW"}), encoding="utf-8")

    with pytest.raises(ValueError, match="DCE_DESKTOP_EXISTING_SOURCE_INVALID"):
        audit_desktop_batch_download(
            archive, history, browser="edge", trading_day=DAY, data_root=root, capture=True,
        )
    assert not (root / BRONZE).exists()


def test_capture_rejects_valid_but_conflicting_android_day(tmp_path: Path) -> None:
    archive = tmp_path / "report.zip"
    archive.write_bytes(_archive())
    history = _history(tmp_path, archive)
    root = tmp_path / "data_control"
    android = root / "bronze" / "dce_dom_capture"
    android.mkdir(parents=True)
    _android_capture(android, _archive(quantity=101))

    with pytest.raises(ValueError, match="DCE_DESKTOP_SOURCE_CONFLICT"):
        audit_desktop_batch_download(
            archive, history, browser="edge", trading_day=DAY, data_root=root, capture=True,
        )
    assert not (root / BRONZE).exists()


def test_capture_rejects_existing_desktop_before_android_skip(tmp_path: Path) -> None:
    archive = tmp_path / "report.zip"
    archive.write_bytes(_archive())
    history = _history(tmp_path, archive)
    root = tmp_path / "data_control"
    android = root / "bronze" / "dce_dom_capture"
    android.mkdir(parents=True)
    _android_capture(android, _archive(compression=ZIP_DEFLATED))
    desktop = root / BRONZE
    desktop.mkdir(parents=True)
    (desktop / f"{COMPACT}.json").write_text("existing", encoding="utf-8")

    with pytest.raises(FileExistsError, match="DCE_DESKTOP_CAPTURE_EXISTS"):
        audit_desktop_batch_download(
            archive, history, browser="edge", trading_day=DAY, data_root=root, capture=True,
        )
    assert (desktop / f"{COMPACT}.json").read_text(encoding="utf-8") == "existing"


def test_capture_lock_rejects_second_writer(tmp_path: Path) -> None:
    lock = tmp_path / "capture.lock"
    with _capture_lock(lock):
        with pytest.raises(RuntimeError, match="DCE_DESKTOP_CAPTURE_BUSY"):
            with _capture_lock(lock):
                pytest.fail("a second writer entered the capture window")


def test_audit_rejects_file_changed_after_history_check(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "report.zip"
    archive.write_bytes(_archive())
    history = _history(tmp_path, archive)
    from market_monitor import dce_desktop_batch_capture as module

    real_verify = module.verify_browser_download

    def changed(*args, **kwargs):
        evidence = real_verify(*args, **kwargs)
        current = archive.stat()
        os.utime(archive, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000_000))
        return evidence

    monkeypatch.setattr(module, "verify_browser_download", changed)
    with pytest.raises(ValueError, match="DCE_DESKTOP_ARCHIVE_CHANGED"):
        audit_desktop_batch_download(archive, history, browser="edge", trading_day=DAY)
