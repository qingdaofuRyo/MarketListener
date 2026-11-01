from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
import struct

from market_monitor.tdx_afterhours import BEIJING, latest_slot
from market_monitor.tdx_import_workflow import STATE_FILE, latest_import_runs, run_local_terminal_imports
from market_monitor.tdx_local_status import _file_bounds, _futures_instrument_id, scan_local_status


def test_active_dce_ranking_accounts_are_displayed_without_credentials(tmp_path: Path) -> None:
    from market_monitor.tdx_local_status import TdxLocalStatus

    state = tmp_path / "state"
    ranks = state / "dce_authorized_ranks"
    ranks.mkdir(parents=True)
    (state / "dce_official_api_status.json").write_text(json.dumps({
        "status": "PASS", "accounts": {"A": {"status": "PASS"}},
        "outcomes": [{"rows": [{"large": "fact"}]}],
    }), encoding="utf-8")
    accounts = {"A": {"status": "COOLDOWN_WAIT", "windowSeconds": 2, "requestsPerWindow": 1},
                "B": {"status": "RUNNING", "windowSeconds": 1, "requestsPerWindow": 3}}
    (ranks / "progress.json").write_text(json.dumps({"status": "RUNNING", "accountStatus": accounts}),
                                        encoding="utf-8")
    status = TdxLocalStatus(tmp_path)
    status._next_scan = float("inf")
    assert status.snapshot()["dceApi"] == {"status": "RUNNING", "accounts": accounts}
    (ranks / "progress.json").write_text(json.dumps({"status": "PASS", "accountStatus": accounts}),
                                        encoding="utf-8")
    assert status.snapshot()["dceApi"] == {"status": "PASS", "accounts": {"A": {"status": "PASS"}}}


def test_completed_download_slot_explains_why_startup_skips_terminal(tmp_path: Path) -> None:
    from market_monitor.tdx_local_status import TdxLocalStatus

    slot = latest_slot(datetime.now(BEIJING))
    next_slot = (datetime.fromisoformat(slot) + timedelta(days=1)).isoformat()
    state = tmp_path / "state" / "tdx_download_ui"
    state.mkdir(parents=True)
    state.joinpath("checkpoints.json").write_text(json.dumps({"financial": {
        key: {"slot": slot, "status": "UI_DONE_UNVERIFIED",
              "finishedAt": "2026-10-24T02:11:13+08:00"}
        for key in ("cn-day", "cn-5m", "extended-day", "extended-5m")
    }}), encoding="utf-8")
    status = TdxLocalStatus(tmp_path)
    status._next_scan = float("inf")
    financial = next(item for item in status.snapshot()["roots"] if item["terminal"] == "financial")
    assert financial["lastDownload"]["finishedAt"] == "2026-10-24T02:11:13+08:00"
    assert financial["downloadSchedule"] == {
        "currentSlot": slot, "nextCheckSlot": next_slot,
        "currentSlotComplete": True, "needsReconciliation": False,
        "skipReason": None, "skippedBatches": [],
    }


def _day(path: Path, first: int, last: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<I28x", first) + struct.pack("<I28x", last))


def test_raw_endpoint_dates_and_imported_dates_remain_distinct(tmp_path: Path) -> None:
    install = tmp_path / "tdx"
    _day(install / "vipdoc/sh/lday/sh600519.day", 19990104, 20260930)
    _day(install / "vipdoc/sh/lday/sh600000.day", 19991110, 20260924)
    broken = install / "vipdoc/sh/lday/sh600001.day"
    broken.write_bytes(b"short")
    report = scan_local_status(tmp_path / "data", roots=[{
        "terminal": "financial", "name": "金融终端", "path": str(install),
    }], imported={"available": True, "items": [{
        "terminal": "financial", "instrumentId": "CN.SSE.STOCK.600519.TDX_LOCAL", "symbol": "600519", "period": "1d",
        "earliestDate": "2023-01-03", "latestDate": "2026-09-24",
    }]})
    group = report["roots"][0]["groups"][0]
    assert group["earliestDate"] == "1999-01-04"
    assert group["latestDate"] == "2026-09-30"
    assert group["importedEarliestDate"] == "2023-01-03"
    assert group["importedLatestDate"] == "2026-09-24"
    assert group["fileCount"] == 3 and group["invalidFileCount"] == 1
    assert report["roots"][0]["pendingFiles"] == 3


def test_custom_terminal_checkpoint_uses_same_path_as_importer(tmp_path: Path) -> None:
    from market_monitor.tdx_local import _state_path
    from market_monitor.tdx_import_workflow import save_state

    install = tmp_path / "custom-install"
    raw = install / "vipdoc/sh/lday/sh600519.day"
    _day(raw, 20230103, 20260930)
    signature = raw.stat()
    data = tmp_path / "data"
    save_state(_state_path(data, install, start_date=None, end_date=None), {"files": {
        "vipdoc/sh/lday/sh600519.day": {"size": signature.st_size, "mtime_ns": signature.st_mtime_ns,
                                      "normalization_version": "tdx-cn-v2"}}})
    report = scan_local_status(data, roots=[{"terminal": "financial", "name": "金融终端", "path": str(install)}],
                               imported={"available": True, "stale": True, "items": []})
    assert report["roots"][0]["pendingFiles"] == 0
    assert report["inventoryStale"] is True


def test_minute_endpoint_dates_use_packed_date_not_calendar_range(tmp_path: Path) -> None:
    path = tmp_path / "31#00700.lc5"
    first = (2024 - 2004) * 2048 + 101
    last = (2026 - 2004) * 2048 + 925
    path.write_bytes(struct.pack("<HH28x", first, 9 * 60 + 35) + struct.pack("<HH28x", last, 16 * 60))
    bounds = _file_bounds(path)
    assert bounds["start"] == "2024-01-01" and bounds["end"] == "2026-09-25"
    assert bounds["status"] == "ENDPOINTS_READ"


def test_unavailable_manifest_retains_prior_import_evidence_with_stale_flag(tmp_path: Path) -> None:
    install = tmp_path / "tdx"
    _day(install / "vipdoc/sh/lday/sh600519.day", 20230103, 20260930)
    roots = [{"terminal": "financial", "name": "金融终端", "path": str(install)}]
    previous = scan_local_status(tmp_path / "data", roots=roots, imported={"available": True, "items": [{
        "terminal": "financial", "instrumentId": "CN.SSE.STOCK.600519.TDX_LOCAL", "symbol": "600519", "period": "1d",
        "earliestDate": "2023-01-03", "latestDate": "2026-09-24",
    }]})
    current = scan_local_status(tmp_path / "data", roots=roots, previous=previous)
    group = current["roots"][0]["groups"][0]
    assert group["importedLatestDate"] == "2026-09-24"
    assert group["importedRangeStale"] is True
    assert current["inventoryAvailable"] is False


def test_same_code_in_separate_venues_keeps_each_import_range(tmp_path: Path) -> None:
    install = tmp_path / "tdx"
    _day(install / "vipdoc/sh/lday/sh000001.day", 19900102, 20260930)
    _day(install / "vipdoc/sz/lday/sz000001.day", 19910403, 20260930)
    _day(install / "vipdoc/sh/lday/sh000300.day", 20050408, 20260930)
    _day(install / "vipdoc/ds/lday/62#000300.day", 20050408, 20260930)
    imports = [
        ("CN.SSE.INDEX.000001.TDX_LOCAL", "000001", "2023-01-03", "2026-09-24"),
        ("CN.SZSE.STOCK.000001.TDX_LOCAL", "000001", "2024-01-02", "2026-09-30"),
        ("CN.SSE.INDEX.000300.TDX_LOCAL", "000300", "2023-02-01", "2026-09-23"),
        ("CN.CSI.INDEX.000300.TDX_LOCAL", "000300", "2023-04-03", "2026-09-29"),
    ]
    report = scan_local_status(tmp_path / "data", roots=[{
        "terminal": "financial", "name": "金融终端", "path": str(install),
    }], imported={"available": True, "items": [{
        "terminal": "financial", "instrumentId": identifier, "symbol": symbol,
        "period": "1d", "earliestDate": start, "latestDate": end,
    } for identifier, symbol, start, end in imports]})
    groups = {item["key"]: item for item in report["roots"][0]["groups"]}
    assert groups["sh.INDEX.1d"]["importedLatestDate"] == "2026-09-24"
    assert groups["sz.STOCK.1d"]["importedEarliestDate"] == "2024-01-02"
    assert groups["sz.STOCK.1d"]["importedLatestDate"] == "2026-09-30"
    assert groups["ds.62.1d"]["importedEarliestDate"] == "2023-04-03"
    assert groups["ds.62.1d"]["importedLatestDate"] == "2026-09-29"


def test_futures_venue_and_series_identity_and_unknown_source_do_not_fallback(tmp_path: Path) -> None:
    install = tmp_path / "futures"
    _day(install / "vipdoc/ds/lday/29#JML8.day", 20230103, 20260930)
    _day(install / "vipdoc/ds/lday/30#JML8.day", 20230103, 20260930)
    _day(install / "vipdoc/ds/lday/29#L-F2611.day", 20230103, 20260930)
    _day(install / "vipdoc/ds/lday/30#SCL8.day", 20230103, 20260930)
    _day(install / "vipdoc/ds/lday/47#IFL0.day", 20230103, 20260930)
    _day(install / "vipdoc/ds/lday/29#A0.day", 20230103, 20260930)
    imported = {"available": True, "items": [
        {"terminal": "futures", "instrumentId": identifier, "symbol": symbol, "period": "1d",
         "earliestDate": "2023-01-03", "latestDate": end}
        for identifier, symbol, end in (
            ("CN.DCE.FUTURE.JM.MAIN.TDX", "JML8", "2026-09-24"),
            ("CN.DCE.FUTURE.L-F2611.CONTRACT.TDX", "L-F2611", "2026-09-23"),
            ("CN.INE.FUTURE.SC.MAIN.TDX", "SCL8", "2026-09-29"),
            ("CN.CFFEX.FUTURE.IF.RANKED_0.TDX", "IFL0", "2026-09-28"),
        )
    ]}
    # A legacy symbol-only record must not authorize the unknown A0 file.
    imported["items"].append({"terminal": "futures", "symbol": "A0", "period": "1d",
                              "earliestDate": "1990-01-01", "latestDate": "2099-01-01"})
    report = scan_local_status(tmp_path / "data", roots=[{
        "terminal": "futures", "name": "期货通", "path": str(install),
    }], imported=imported)
    groups = {item["key"]: item for item in report["roots"][0]["groups"]}
    assert groups["ds.29.1d"]["importedLatestDate"] == "2026-09-24"
    assert groups["ds.30.1d"]["importedLatestDate"] == "2026-09-29"
    assert groups["ds.47.1d"]["importedLatestDate"] == "2026-09-28"


def test_v1_cached_ranges_cannot_reenter_v2_source_matching(tmp_path: Path) -> None:
    install = tmp_path / "tdx"
    _day(install / "vipdoc/sh/lday/sh000001.day", 19900102, 20260930)
    previous = {"schema": "tdx-local-file-status-v1", "roots": [{
        "terminal": "financial", "groups": [{"key": "sh.INDEX.1d", "importedLatestDate": "2099-01-01"}],
    }]}
    report = scan_local_status(tmp_path / "data", roots=[{
        "terminal": "financial", "name": "金融终端", "path": str(install),
    }], previous=previous)
    assert report["schema"] == "tdx-local-file-status-v2"
    assert report["roots"][0]["groups"][0]["importedLatestDate"] is None


def test_futures_file_id_matches_the_actual_importer_for_all_supported_series(tmp_path: Path) -> None:
    from market_monitor.futures_bulk import _tdx_rows

    folder = tmp_path / "vipdoc/ds/lday"
    folder.mkdir(parents=True)
    names = (
        "29#JML8.day", "29#JM2701.day", "29#L-F2611.day", "29#L-FL9.day",
        "30#SCL8.day", "47#IFL0.day", "47#IF300.day", "42#IMCI.day", "68#AU_VIX.day",
    )
    for name in names:
        (folder / name).write_bytes(struct.pack("<IffffIIf", 20260930, 10, 11, 9, 10, 100, 80, 10))
    emitted = []

    def emit(rows):
        emitted.extend(rows)
        return len(rows)

    _, _, errors = _tdx_rows(tmp_path, {}, full_rescan=False, emit=emit)
    assert errors == []
    assert len(emitted) == len(names)
    actual = {row["source_symbol"]: row["instrument_id"] for row in emitted}
    for name in names:
        assert _futures_instrument_id(folder / name) == actual[name.partition("#")[2].removesuffix(".day")]
    assert _futures_instrument_id(folder / "29#A0.day") is None
    assert _futures_instrument_id(folder / "99#JML8.day") is None


def test_serial_local_import_keeps_no_change_and_quarantine_truthful(tmp_path: Path, monkeypatch) -> None:
    order = []

    def futures(_root, **options):
        assert options["include_global"] is False
        assert options["include_akshare"] is False
        assert callable(options["run_started"])
        order.append("futures")
        return {"写入K线": 0, "错误": []}

    def financial(_root, **_kwargs):
        order.append("financial")
        return {"写入K线": 20, "导入文件": 1, "隔离文件": 1, "错误数": 0}

    monkeypatch.setattr("market_monitor.futures_bulk.run_bulk_futures", futures)
    monkeypatch.setattr("market_monitor.tdx_local.run_tdx_local_import", financial)
    result = run_local_terminal_imports(tmp_path)
    assert order == ["futures", "financial"]
    assert result["status"] == "PARTIAL_FAILURE"
    assert result["terminals"]["futures"]["status"] == "NO_CHANGE"
    assert result["terminals"]["financial"]["status"] == "PARTIAL_FAILURE"
    persisted = json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8"))
    assert persisted["terminals"]["financial"]["rows"] == 20
    assert persisted["completedAt"]


def test_failed_terminal_does_not_stop_other_terminal(tmp_path: Path, monkeypatch) -> None:
    def failed(_root, **_options):
        raise RuntimeError("fixture terminal unavailable")

    monkeypatch.setattr("market_monitor.futures_bulk.run_bulk_futures", failed)
    monkeypatch.setattr("market_monitor.tdx_local.run_tdx_local_import", lambda _root, **_kwargs: {"写入K线": 0, "错误数": 0})
    report = run_local_terminal_imports(tmp_path)
    assert report["terminals"]["futures"]["status"] == "FAILED"
    assert report["terminals"]["financial"]["status"] == "NO_CHANGE"
    assert report["status"] == "PARTIAL_FAILURE"


def test_existing_cli_run_metadata_reports_partial_and_normalization_quarantine(tmp_path: Path) -> None:
    import duckdb

    with duckdb.connect(str(tmp_path / "catalog.duckdb")) as connection:
        connection.execute("CREATE TABLE runs(run_id VARCHAR, provider VARCHAR, status VARCHAR, started_at VARCHAR, completed_at VARCHAR, detail VARCHAR)")
        connection.execute("CREATE TABLE partitions(source_run_id VARCHAR, status VARCHAR, row_count BIGINT)")
        connection.execute("INSERT INTO runs VALUES ('financial-run', 'import-tdx-local:通达信金融终端:tdx-cn-v2', 'COMPLETE', '2026-10-07T01:00:00+00:00', '2026-10-07T02:00:00+00:00', '导入 10 条；文件 1；隔离文件 2')")
        connection.execute("INSERT INTO runs VALUES ('futures-run', 'bulk-futures:通达信期货通', 'PARTIAL_FAILURE', '2026-10-07T01:00:00+00:00', '2026-10-07T02:00:00+00:00', '导入失败部分保留')")
        connection.execute("INSERT INTO partitions VALUES ('financial-run','COMPLETE',10), ('futures-run','COMPLETE',3)")
    report = latest_import_runs(tmp_path)
    assert report["financial"]["status"] == "PARTIAL_FAILURE"
    assert report["financial"]["rows"] == 10
    assert report["financial"]["importedFiles"] == 1
    assert report["financial"]["completedAt"] == "2026-10-07T10:00:00+08:00"
    assert report["futures"]["status"] == "PARTIAL_FAILURE"
    assert report["futures"]["rows"] == 3
