"""Serial, incremental imports of stable files from the two local terminals."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Callable


BEIJING = timezone(timedelta(hours=8))
STATE_FILE = Path("state/tdx_import_workflow.json")
TERMINAL_ORDER = ("futures", "financial")


def terminal_import_selection(data_root: Path) -> tuple[tuple[str, ...], dict[str, str]]:
    """Exclude only terminals whose raw-file download outcome remains unknown."""

    path = Path(data_root) / "state" / "tdx_download_ui" / "checkpoints.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    except (OSError, ValueError):
        return (), dict.fromkeys(TERMINAL_ORDER, "CHECKPOINT_UNREADABLE")
    if not isinstance(state, dict):
        return (), dict.fromkeys(TERMINAL_ORDER, "CHECKPOINT_MALFORMED")
    blocked = {}
    for terminal in TERMINAL_ORDER:
        entries = state.get(terminal, {})
        if not isinstance(entries, dict) or any(not isinstance(item, dict) for item in entries.values()):
            blocked[terminal] = "CHECKPOINT_MALFORMED"
        elif any(item.get("status") == "RUNNING_UNKNOWN" for item in entries.values()):
            blocked[terminal] = "RUNNING_UNKNOWN"
    return tuple(terminal for terminal in TERMINAL_ORDER if terminal not in blocked), blocked


def read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def now() -> str:
    return datetime.now(BEIJING).isoformat(timespec="seconds")


def latest_import_runs(data_root: Path) -> dict[str, Any]:
    """Customer-process metadata only, including CLI imports before this UI.

    Existing catalog connections share the default config in the owning
    runtime. No MarketStore construction/migration and no Silver row scan.
    """
    import duckdb

    path = Path(data_root) / "catalog.duckdb"
    if not path.is_file():
        return {}
    try:
        with duckdb.connect(str(path)) as connection:
            rows = connection.execute(
                "SELECT run_id, provider, status, started_at, completed_at, detail "
                "FROM runs WHERE provider LIKE 'import-tdx-local:%' "
                "OR provider='bulk-futures:通达信期货通' ORDER BY started_at DESC LIMIT 100"
            ).fetchall()
            result = {}
            for run_id, provider, status, started, completed, detail in rows:
                terminal = "financial" if str(provider).startswith("import-tdx-local:") else "futures"
                if terminal in result:
                    continue
                counts = connection.execute(
                    "SELECT coalesce(sum(row_count),0), count(*) FROM partitions "
                    "WHERE source_run_id=? AND status='COMPLETE'", [run_id],
                ).fetchone()
                detail_text = str(detail or "")
                isolated = re.search(r"隔离文件\s*(\d+)", detail_text)
                normalized_status = "PASS" if status == "COMPLETE" else str(status)
                if isolated and int(isolated.group(1)):
                    normalized_status = "PARTIAL_FAILURE"
                def local_time(value: Any) -> str | None:
                    if not value:
                        return None
                    try:
                        return datetime.fromisoformat(str(value)).astimezone(BEIJING).isoformat(timespec="seconds")
                    except ValueError:
                        return str(value)
                files = re.search(r"；文件\s*(\d+)", detail_text)
                result[terminal] = {"status": normalized_status, "startedAt": local_time(started),
                                    "completedAt": local_time(completed), "rows": int(counts[0]),
                                    "importedFiles": int(files.group(1)) if files else None,
                                    "pendingFiles": None, "detail": detail_text,
                                    "source": "已登记CLI/后台运行与正式分区清单", "runId": str(run_id)}
            return result
    except duckdb.Error:
        return {}


def recover_interrupted_workflow(data_root: Path) -> list[dict[str, Any]]:
    """Retain submitted partitions when the previous import process exited."""
    root = Path(data_root)
    path = root / STATE_FILE
    state = read_state(path)
    if state.get("status") != "RUNNING":
        return []
    from .market_data_version import market_data_version
    from .market_query_cache import (
        _process_is_running, get_kline_query_store, refresh_kline_cache_partitions,
    )
    from .process_runtime import process_start_token

    owner = state.get("ownerPid")
    if isinstance(owner, int) and _process_is_running(owner):
        expected = state.get("ownerProcessStartedAt")
        actual = process_start_token(owner)
        # Legacy states and inaccessible process identities remain guarded.
        # A different known creation token proves that the PID was reused.
        if not isinstance(expected, str) or not expected or not actual or expected == actual:
            raise RuntimeError("已有通达信本地导入进程正在运行")
    recovered = []
    files = []
    running_base_revisions = {
        item.get("marketRevisionAtStart")
        for item in state.get("terminals", {}).values()
        if item.get("status") == "RUNNING" and item.get("marketRevisionAtStart")
    }
    from .storage import MarketStore

    store = MarketStore(root) if (root / "catalog.duckdb").is_file() else None
    legacy_identity_unknown = False
    try:
        for terminal, item in state.get("terminals", {}).items():
            if item.get("status") != "RUNNING":
                continue
            run_id = item.get("runId")
            run = None
            if store is not None and isinstance(run_id, str) and run_id:
                row = store.connection.execute(
                    "SELECT provider,status FROM runs WHERE run_id=?", [run_id],
                ).fetchone()
                expected = (row is not None and
                            (str(row[0]).startswith("import-tdx-local:") if terminal == "financial"
                             else str(row[0]) == "bulk-futures:通达信期货通"))
                if not expected:
                    raise RuntimeError("上次导入的运行身份与终端不匹配")
                run = {"runId": run_id, "status": str(row[1])}
            elif store is not None:
                legacy_identity_unknown = True
            # File checkpoints and the catalog run can already be committed
            # when a process exits before refreshing the chart manifest. A
            # finalized run therefore still needs its submitted partitions
            # reconciled while the enclosing workflow remains unfinished.
            if store is not None and run is not None:
                facts = store.connection.execute(
                    "SELECT file_path,row_count FROM partitions WHERE source_run_id=? AND status='COMPLETE'",
                    [run["runId"]],
                ).fetchall()
                paths = [root / str(value[0]) for value in facts]
                if not all(value.is_file() for value in paths):
                    raise RuntimeError("上次导入的已登记分区文件缺失")
                rows = sum(int(value[1]) for value in facts)
                if run.get("status") == "RUNNING":
                    store.finish_run(run["runId"], "PARTIAL_FAILURE",
                        f"上次导入进程退出；已提交{len(facts)}个分区、{rows}根K线，保留检查点续跑")
                files.extend(paths)
                recovered.append({"terminal": terminal, "runId": run["runId"], "rows": rows, "partitions": len(facts)})
            item.update(status="PARTIAL_FAILURE", completedAt=now(),
                        detail=("上次导入进程退出；已提交分区及检查点保留，本次继续增量导入"
                                if run is not None else
                                "上次导入进程退出；旧检查点未记录运行身份，按全量索引恢复且来源待核验"))
    finally:
        if store is not None:
            store.close()
    # Leave the persisted RUNNING workflow intact on failure. The next
    # startup can retry even if the catalog run was finalized above.
    if files or legacy_identity_unknown:
        base_revision = (next(iter(running_base_revisions))
                         if len(running_base_revisions) == 1 and not legacy_identity_unknown else None)
        current_revision = market_data_version(root)
        refreshed = bool(base_revision) and refresh_kline_cache_partitions(
            root, files, expected_base_revision=base_revision,
            target_revision=current_revision,
        )
        if not refreshed:
            # Older checkpoints have no proof that these are every changed
            # file. A full rebuild is the only safe way to recover CURRENT.
            rebuilt = get_kline_query_store(root).rebuild()
            refreshed = rebuilt.ready and rebuilt.revision == market_data_version(root)
        if not refreshed:
            raise RuntimeError("上次已提交分区的查询缓存恢复失败")
    state.update(status="PARTIAL_FAILURE", completedAt=now(), recoveredRuns=recovered)
    save_state(path, state)
    return recovered


def run_local_terminal_imports(
    data_root: Path, *, selected_terminals: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Keep normalization gates and terminal writers inside one queue window.

    Each binary reader checks size/mtime before and after reading a file.
    A manual download need not have a matching UI-automation confirmation.
    """
    from .futures_bulk import run_bulk_futures
    from .process_runtime import process_start_token
    from .tdx_local import run_tdx_local_import

    root = Path(data_root)
    from .market_data_version import market_data_version
    requested = TERMINAL_ORDER if selected_terminals is None else selected_terminals
    if len(set(requested)) != len(requested) or any(item not in TERMINAL_ORDER for item in requested):
        raise ValueError("unknown or duplicate terminal import selection")
    eligible, all_blocked = terminal_import_selection(root)
    selected = tuple(item for item in TERMINAL_ORDER if item in requested and item in eligible)
    blocked = {key: reason for key, reason in all_blocked.items() if key in requested}
    path = root / STATE_FILE
    recovered = recover_interrupted_workflow(root)
    state = read_state(path)
    state.update(schema="tdx-local-import-workflow-v1", status="RUNNING", startedAt=now(), completedAt=None,
                 ownerPid=os.getpid(), ownerProcessStartedAt=process_start_token(os.getpid()), recoveredRuns=recovered,
                 selectedTerminals=list(selected), blockedTerminals=blocked)
    terminals = state.setdefault("terminals", {})

    def futures_run_started(run_id: str) -> None:
        item = terminals.get("futures")
        if item is not None:
            item["runId"] = run_id
            save_state(path, state)

    def financial_progress(progress: dict[str, Any]) -> None:
        item = terminals.get("financial")
        if item is not None:
            item.update(progress, progressAt=now())
            save_state(path, state)

    candidates: tuple[tuple[str, Callable[[], dict[str, Any]]], ...] = (
        ("futures", lambda: run_bulk_futures(
            root, include_global=False, include_akshare=False,
            run_started=futures_run_started,
        )),
        ("financial", lambda: run_tdx_local_import(root, progress_callback=financial_progress)),
    )
    jobs = tuple(job for job in candidates if job[0] in selected)
    save_state(path, state)
    for terminal, invoke in jobs:
        item: dict[str, Any] = {"status": "RUNNING", "startedAt": now(), "completedAt": None,
                                "rows": 0, "importedFiles": 0, "pendingFiles": None,
                                "marketRevisionAtStart": market_data_version(root)}
        terminals[terminal] = item
        save_state(path, state)
        try:
            report = invoke()
            rows = int(report.get("写入K线") or 0)
            errors = int(report.get("错误数") or len(report.get("错误") or []))
            quarantined = int(report.get("隔离文件") or 0)
            rejected = int(report.get("拒绝文件") or 0)
            # A valid checkpoint-only run has no new rows. It is not a failure,
            # nor does it prove full historical/calendar coverage.
            clean = not (errors or quarantined or rejected)
            status = "NO_CHANGE" if clean and not rows else "PASS" if clean else "PARTIAL_FAILURE"
            if report.get("扫描文件") == 0:
                status = "NO_FILES"
            detail = (f"本次写入 {rows} 根；错误 {errors}；隔离 {quarantined} 个文件；拒绝 {rejected} 个文件。"
                      "状态仅说明本次本地增量导入，不证明所有交易日完整覆盖。")
            item.update(status=status, rows=rows, importedFiles=report.get("导入文件"), detail=detail,
                        checkpoint=report.get("检查点"), sourceRoot=report.get("通达信目录"),
                        report=report)
        except Exception as error:
            item.update(status="FAILED", detail=f"{type(error).__name__}: {error}"[:500])
        item["completedAt"] = now()
        save_state(path, state)
    state["completedAt"] = now()
    selected_clean = bool(jobs) and all(
        terminals[key]["status"] in {"PASS", "NO_CHANGE"} for key, _ in jobs
    )
    state["selectedStatus"] = "PASS" if selected_clean else "PARTIAL_FAILURE"
    state["status"] = "PASS" if selected_clean and not blocked else "PARTIAL_FAILURE"
    save_state(path, state)
    return state
