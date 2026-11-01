"""Conservatively reconcile a stranded terminal download after its process exits."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import subprocess
from typing import Callable, Iterable

from .operations import Operation, OperationKind, OperationStatus
from .tdx_afterhours import BEIJING, process_lock, save_report
from .tdx_download_ui import TERMINALS, _checkpoint_write_lock, _read_checkpoint_state


_GUI_KINDS = {OperationKind.TDX_FINANCIAL_AUTO_DOWNLOAD,
              OperationKind.TDX_FUTURES_AUTO_DOWNLOAD}
_ACTIVE = {OperationStatus.QUEUED, OperationStatus.RUNNING}


def _matching_process_active(exe: Path) -> bool:
    """Check every TdxW process's full image path; uncertainty blocks recovery."""

    if not exe.is_absolute():
        raise ValueError("terminal executable path must be absolute")
    script = (
        "$ErrorActionPreference='Stop'; "
        "$rows=@(Get-CimInstance -ClassName Win32_Process -Filter \"Name='TdxW.exe'\" "
        "| ForEach-Object { @{ pid=$_.ProcessId; path=$_.ExecutablePath } }); "
        "ConvertTo-Json -InputObject $rows -Compress -Depth 2"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=10, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise RuntimeError("terminal process inventory unavailable")
    rows = json.loads(result.stdout)
    if not isinstance(rows, list):
        raise ValueError("terminal process inventory malformed")
    expected = exe.resolve().as_posix().casefold()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str) or not row["path"]:
            raise ValueError("terminal process path unavailable")
        if Path(row["path"]).resolve().as_posix().casefold() == expected:
            return True
    return False


def reconcile_unknown_download(
    data_root: Path, terminal: str, operations: Iterable[Operation], *,
    process_probe: Callable[[Path], bool] | None = None,
    observed_at: datetime | None = None,
) -> dict[str, object]:
    """Mark unknown UI batches interrupted only when no matching app can be active.

    This does not certify a completed download or Silver coverage. The caller
    decides when a subsequent GUI retry is due.
    """

    if terminal not in TERMINALS:
        raise ValueError("unknown terminal")
    timestamp = observed_at or datetime.now(BEIJING)
    if timestamp.tzinfo is None:
        raise ValueError("observed_at must have a timezone")
    if any(item.kind in _GUI_KINDS and item.status in _ACTIVE for item in operations):
        return {"status": "ACTIVE_GUI_OPERATION", "terminal": terminal, "batches": []}

    state_dir = Path(data_root) / "state" / "tdx_download_ui"
    path = state_dir / "checkpoints.json"
    try:
        with process_lock(state_dir / f"{terminal}.run.lock"):
            with _checkpoint_write_lock(state_dir / "checkpoints.write.lock"):
                state = _read_checkpoint_state(path)
                entries = state.get(terminal, {})
                if not isinstance(entries, dict):
                    raise ValueError("terminal checkpoint malformed")
                unknown = [key for key, item in entries.items()
                           if isinstance(item, dict) and item.get("status") == "RUNNING_UNKNOWN"]
            if not unknown:
                return {"status": "NO_UNKNOWN_BATCH", "terminal": terminal, "batches": []}

            exe = TERMINALS[terminal][0] / "tdxw.exe"
            if (process_probe or _matching_process_active)(exe):
                return {"status": "TERMINAL_STILL_RUNNING", "terminal": terminal, "batches": unknown}

            with _checkpoint_write_lock(state_dir / "checkpoints.write.lock"):
                state = _read_checkpoint_state(path)
                entries = state.get(terminal, {})
                if not isinstance(entries, dict):
                    raise ValueError("terminal checkpoint malformed")
                changed = []
                for key, item in entries.items():
                    if isinstance(item, dict) and item.get("status") == "RUNNING_UNKNOWN":
                        item["status"] = "INTERRUPTED_UNVERIFIED"
                        item["reconciliation"] = {
                            "previousStatus": "RUNNING_UNKNOWN",
                            "reason": "TERMINAL_PROCESS_ABSENT",
                            "observedAt": timestamp.astimezone(BEIJING).isoformat(),
                        }
                        changed.append(key)
                if changed:
                    save_report(path, state)
            return {"status": "INTERRUPTED_UNVERIFIED", "terminal": terminal,
                    "batches": changed}
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {"status": "RECONCILIATION_UNAVAILABLE", "terminal": terminal,
                "batches": []}


__all__ = ("reconcile_unknown_download",)
