"""Schedule ordinary market fetches and changed local TDX files.

The checks are read-only.  Source jobs still run through the serial operation
manager, and a page request never starts a provider fetch.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator
from zoneinfo import ZoneInfo

from .operations import Operation, OperationKind, OperationStatus


BEIJING = ZoneInfo("Asia/Shanghai")
MARKET_WINDOW = time(20, 30)
MARKET_RETRY_AFTER = timedelta(hours=6)
TDX_RETRY_AFTER = timedelta(hours=6)
MAX_SOURCE_FILES = 100_000
_ACTIVE = {OperationStatus.QUEUED, OperationStatus.RUNNING}
_INTERRUPTED_WORKFLOW = "RUNNING"


def market_target(now: datetime) -> tuple[str, datetime]:
    """Most recent Beijing evening refresh window, including a new startup."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(BEIJING)
    day = local.date()
    opened = datetime.combine(day, MARKET_WINDOW, BEIJING)
    if local < opened:
        day -= timedelta(days=1)
        opened = datetime.combine(day, MARKET_WINDOW, BEIJING)
    return day.isoformat(), opened.astimezone(timezone.utc)


def due_market_update(operations: Iterable[Operation], now: datetime) -> bool:
    """Run once per window; failed runs retry after completion-time cooldown."""

    _day, opened = market_target(now)
    jobs = [item for item in operations if item.kind == OperationKind.MARKET_UPDATE]
    if any(item.status in _ACTIVE for item in jobs):
        return False
    relevant = []
    try:
        for item in jobs:
            created = datetime.fromisoformat(item.created_at)
            if created.tzinfo is None:
                return False
            created = created.astimezone(timezone.utc)
            if created >= opened:
                relevant.append((created, item))
    except (ValueError, TypeError, OverflowError):
        return False
    latest = max(relevant, key=lambda entry: entry[0])[1] if relevant else None
    if latest is None:
        return True
    if latest.status in {OperationStatus.PASS, OperationStatus.CANCELLED}:
        return False
    if latest.status == OperationStatus.FAILED and latest.detail == "process stopped before operation completed":
        return True
    try:
        completed = datetime.fromisoformat(latest.completed_at or latest.created_at)
        if completed.tzinfo is None:
            return False
        return now.astimezone(timezone.utc) - completed.astimezone(timezone.utc) >= MARKET_RETRY_AFTER
    except (ValueError, TypeError, OverflowError):
        return False


def _eligible_futures_files(root: Path) -> Iterator[Path]:
    """Use the importer's filename and exchange ownership rules for a stat scan."""

    from .futures_bulk import (
        _CFFEX_RANKED, _CFFEX_REFERENCE, _CONTRACT, _DCE_F_CONTRACT,
        _DCE_F_SPECIAL, _EXCHANGES, _INDEX, _OPTION_VOLATILITY, _SPECIAL,
    )

    patterns = (_OPTION_VOLATILITY, _CFFEX_REFERENCE, _CFFEX_RANKED,
                _DCE_F_CONTRACT, _DCE_F_SPECIAL, _SPECIAL, _CONTRACT, _INDEX)
    for folder in (root / "vipdoc" / "ds" / "lday", root / "vipdoc" / "ds" / "fzline"):
        if not folder.is_dir():
            continue
        for path in folder.glob("*"):
            match = None
            for pattern in patterns:
                match = pattern.fullmatch(path.name)
                if match is not None:
                    break
            if match is None:
                continue
            market_code = match.groupdict().get("market") or (
                "42" if path.name.upper().startswith("42#") else ""
            )
            if market_code in {*_EXCHANGES, "42", "68"}:
                yield path


def _has_changed_files(root: Path, checkpoint: dict, files: Iterable[Path],
                       *, require_version: str | None = None,
                       since: datetime | None = None) -> bool:
    previous_files = checkpoint.get("files")
    if not isinstance(previous_files, dict):
        previous_files = {}
    checked = 0
    for path in files:
        checked += 1
        if checked > MAX_SOURCE_FILES:
            return True  # Fail open: an oversized scan must not suppress an import.
        if require_version is not None and checkpoint.get("normalization_version") != require_version:
            return True
        relative = str(path.relative_to(root)).replace("\\", "/")
        previous = previous_files.get(relative)
        try:
            stat = path.stat()
        except OSError:
            return True
        changed = (not isinstance(previous, dict) or previous.get("size") != stat.st_size
                   or previous.get("mtime_ns") != stat.st_mtime_ns
                   or (require_version is not None
                       and previous.get("normalization_version") != require_version))
        if changed:
            if since is None:
                return True
            # A rejected file can stay outside a checkpoint forever. Recheck
            # it automatically only after its source changes since that run.
            observed_mtime = stat.st_mtime_ns
            if previous is None:
                try:
                    observed_mtime = max(observed_mtime, path.parent.stat().st_mtime_ns)
                except OSError:
                    return True
            if observed_mtime > int(since.timestamp() * 1_000_000_000):
                return True
    return False


def tdx_sources_changed(data_root: Path, *, since: datetime | None = None,
                        selected_terminals: tuple[str, ...] = ("futures", "financial")) -> bool:
    """Compare importable source files with both durable importer checkpoints.

    New and modified files trigger work even when an import already ran today.
    Deleted files are outside the importer's additive contract; the coverage
    audit, rather than another import, must assess retired source files.
    """

    if since is not None and since.tzinfo is None:
        raise ValueError("since must be timezone-aware")
    if any(terminal not in {"financial", "futures"} for terminal in selected_terminals):
        raise ValueError("unknown terminal import selection")
    from .futures_bulk import _load_state as load_futures_state
    from .futures_bulk import resolve_tdx_root as resolve_futures_root
    from .tdx_local import (
        _NORMALIZATION_VERSION, _files, _load_state, _state_path,
        resolve_tdx_root as resolve_financial_root,
    )

    root = Path(data_root)
    financial_root = resolve_financial_root() if "financial" in selected_terminals else None
    if financial_root is not None:
        checkpoint = _load_state(_state_path(root, financial_root, start_date=None, end_date=None))
        if _has_changed_files(financial_root, checkpoint,
                              (path for path, _metadata in _files(financial_root)),
                              require_version=_NORMALIZATION_VERSION, since=since):
            return True
    futures_root = resolve_futures_root() if "futures" in selected_terminals else None
    if futures_root is not None:
        checkpoint = load_futures_state(root / "state" / "futures_import.json")
        if _has_changed_files(futures_root, checkpoint, _eligible_futures_files(futures_root),
                              since=since):
            return True
    return False


def due_tdx_import(data_root: Path, operations: Iterable[Operation], now: datetime) -> bool:
    """Import only changed local files or resume an incomplete import workflow."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    history = list(operations)
    if any(item.kind in {OperationKind.TDX_FINANCIAL_AUTO_DOWNLOAD,
                         OperationKind.TDX_FUTURES_AUTO_DOWNLOAD,
                         OperationKind.TDX_AFTERHOURS_DOWNLOAD} and item.status in _ACTIVE
           for item in history):
        return False
    from .tdx_import_workflow import terminal_import_selection

    selected_terminals, _blocked = terminal_import_selection(data_root)
    if not selected_terminals:
        return False
    jobs = [item for item in history if item.kind == OperationKind.TDX_LOCAL_IMPORT]
    if any(item.status in _ACTIVE for item in jobs):
        return False
    from .tdx_import_workflow import STATE_FILE, read_state

    workflow = read_state(Path(data_root) / STATE_FILE)
    interrupted = workflow.get("status") == _INTERRUPTED_WORKFLOW
    latest = max(jobs, key=lambda item: item.created_at, default=None)
    blocked_only = (latest is not None and latest.status == OperationStatus.PARTIAL_FAILURE
                    and bool(workflow.get("blockedTerminals")) and workflow.get("selectedStatus") == "PASS")
    last_attempt = (
        datetime.fromisoformat(latest.started_at or latest.created_at)
        if latest and latest.status in {OperationStatus.FAILED, OperationStatus.PARTIAL_FAILURE}
        and not blocked_only
        else None
    )
    changed = tdx_sources_changed(data_root, since=last_attempt,
                                  selected_terminals=selected_terminals)
    if not changed and not interrupted:
        return False
    if latest is None:
        return True
    if latest.status == OperationStatus.FAILED and latest.detail == "process stopped before operation completed":
        return True
    if latest.status in {OperationStatus.FAILED, OperationStatus.PARTIAL_FAILURE, OperationStatus.CANCELLED} and not blocked_only:
        return now - datetime.fromisoformat(latest.completed_at or latest.created_at) >= TDX_RETRY_AFTER
    return True


def due_local_market_updates(data_root: Path, operations: Iterable[Operation],
                             now: datetime) -> list[OperationKind]:
    """Return stable startup/watcher queue order for local then remote sources."""

    history = list(operations)
    due = []
    if due_tdx_import(data_root, history, now):
        due.append(OperationKind.TDX_LOCAL_IMPORT)
    if due_market_update(history, now):
        due.append(OperationKind.MARKET_UPDATE)
    return due


__all__ = (
    "due_local_market_updates", "due_market_update", "due_tdx_import",
    "market_target", "tdx_sources_changed",
)
