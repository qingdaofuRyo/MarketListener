"""Explicit human attestation for a stranded financial-terminal CN batch.

The two records remain unverified with respect to raw files and Silver.
This command never starts a terminal, selects controls or initiates downloads.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime
import json
from pathlib import Path
import re
from uuid import uuid4

from .tdx_afterhours import BEIJING, latest_slot, process_lock, save_report
from .tdx_download_ui import (
    FINANCIAL, DialogDriver, StopRun, _checkpoint_write_lock, _read_checkpoint_state,
    completion_state, plain,
)
from .tdx_process_ownership import load_auto_owned


def confirm_financial_cn(root: Path, slot: str, *, user_confirmed: bool,
                         driver_factory=DialogDriver) -> dict[str, object]:
    """Record the human assertion only after strict same-process UI checks."""
    if not user_confirmed or slot != latest_slot(datetime.now(BEIJING)):
        raise StopRun("USER_CONFIRMATION_OR_SLOT_INVALID")
    root = Path(root).resolve()
    state_dir = root / "state" / "tdx_download_ui"
    path = state_dir / "checkpoints.json"
    exe = Path(r"C:\tongdaxin\tdxw.exe").resolve()
    with ExitStack() as locks:
        # Acquire both GUI worker locks; neither download may be active.
        for name in ("financial", "futures"):
            locks.enter_context(process_lock(state_dir / f"{name}.run.lock"))
        locks.enter_context(_checkpoint_write_lock(state_dir / "checkpoints.write.lock"))
        state = _read_checkpoint_state(path)
        entries = state.get("financial")
        if not isinstance(entries, dict):
            raise StopRun("FINANCIAL_CHECKPOINT_INVALID")
        keys = ("cn-day", "cn-5m")
        rows = [entries.get(key) for key in keys]
        if any(not isinstance(row, dict) or row.get("slot") != slot or
               row.get("status") != "RUNNING_UNKNOWN" for row in rows):
            raise StopRun("CN_BATCHES_NOT_UNKNOWN")
        if any(isinstance(item, dict) and item.get("status") == "RUNNING_UNKNOWN"
               for key, item in entries.items() if key not in keys):
            raise StopRun("OTHER_UNKNOWN_BATCH")
        for key in ("datePolicy", "startDate", "selectionHash"):
            if rows[0].get(key) != rows[1].get(key):
                raise StopRun("CN_BATCH_EVIDENCE_MISMATCH")
        if rows[0].get("datePolicy") != "LOCAL_COVERAGE_START_CLIENT_END" or not rows[0].get("startDate"):
            raise StopRun("CN_BATCH_EVIDENCE_MISSING")
        identity = load_auto_owned(root, "financial", exe)
        if identity is None:
            raise StopRun("OWNED_PROCESS_RECORD_MISMATCH")
        driver = driver_factory(exe)
        driver.connect()  # Does not start a process or log in.
        if getattr(driver.app, "process", None) != identity[0]:
            raise StopRun("OWNED_PROCESS_IDENTITY_CHANGED")
        driver.dialog = driver.find_dialog()
        dialog = driver.dialog
        if (dialog is None or dialog.process_id() != identity[0] or dialog.class_name() != "#32770"
                or dialog.window_text() != "盘后数据下载" or not dialog.is_visible() or not dialog.is_enabled()):
            raise StopRun("DOWNLOAD_DIALOG_UNVERIFIED")
        driver.ensure_connected_process()
        driver.ensure_idle()
        message, button = driver.status()
        if completion_state(message, button, True) != "UI_DONE_UNVERIFIED":
            raise StopRun("USER_CONFIRMED_WINDOW_NOT_COMPLETE")
        selected = [plain(name) for name in driver.tab_names() if "*" in name]
        if selected != [batch.tab for batch in FINANCIAL[:2]]:
            raise StopRun("USER_CONFIRMED_BATCH_SELECTION_CHANGED")
        if load_auto_owned(root, "financial", exe) != identity:
            raise StopRun("OWNED_PROCESS_IDENTITY_CHANGED")
        at = datetime.now(BEIJING).isoformat()
        audit_id = uuid4().hex
        evidence = {"method": "USER_CONFIRMED_AND_WINDOW_VALIDATED",
                    "previousStatus": "RUNNING_UNKNOWN", "slot": slot,
                    "confirmedAt": at, "auditId": audit_id}
        # This append-only assertion is written first. If the checkpoint write
        # later fails it remains an assertion, not a claim of commit or data
        # coverage. It cannot replace or overwrite any prior audit entry.
        audit_dir = state_dir / "user_confirmations"
        audit_dir.mkdir(parents=True, exist_ok=True)
        with (audit_dir / f"{audit_id}.json").open("x", encoding="utf-8") as stream:
            json.dump({"schema": "tdx-user-confirmation-v1", **evidence,
                       "batches": list(keys), "rawCoverage": "UNVERIFIED"}, stream)
        for row in rows:
            row["status"] = "UI_DONE_UNVERIFIED"
            row["completionEvidence"] = dict(evidence)
        save_report(path, state)
        return {"status": "USER_CONFIRMED_AND_WINDOW_VALIDATED", "slot": slot,
                "batches": list(keys), "rawCoverage": "UNVERIFIED"}


def main() -> int:
    parser = argparse.ArgumentParser(description="记录用户确认的金融终端沪深京完成状态")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--slot", required=True)
    parser.add_argument("--confirm-cn-finished", action="store_true", required=True)
    args = parser.parse_args()
    try:
        report = confirm_financial_cn(args.data_root, args.slot, user_confirmed=args.confirm_cn_finished)
    except (OSError, ValueError, StopRun) as exc:
        reason = str(exc) if isinstance(exc, StopRun) and re.fullmatch(r"[A-Z0-9_]+", str(exc)) else type(exc).__name__
        print(json.dumps({"status": "NOT_CONFIRMED", "reason": reason}))
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
