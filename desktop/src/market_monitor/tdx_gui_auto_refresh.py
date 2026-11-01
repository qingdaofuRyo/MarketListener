"""Schedule the logged-in financial terminal's guarded after-hours UI download."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
from typing import Iterable

from .operations import Operation, OperationKind, OperationStatus
from .tdx_afterhours import BEIJING, latest_slot
from .tdx_download_ui import FINANCIAL, FUTURES, TERMINALS, pending_keys, terminal_completion


RETRY_AFTER = timedelta(minutes=30)
MARKET_HOST_RETRY_AFTER = timedelta(minutes=10)
_ACTIVE = {OperationStatus.QUEUED, OperationStatus.RUNNING}
_LOCAL_STATUS_MAX_AGE = timedelta(hours=24)
_WEEKEND_RAW_STATUS_MAX_AGE = timedelta(hours=72)
_BACKFILL_WINDOW = timedelta(days=31)
_FINANCIAL_STOCK_GROUPS = frozenset({"sh.STOCK", "sz.STOCK", "bj.STOCK",
                                     "sh.B_SHARE", "sz.B_SHARE"})
_FUTURES_EXCHANGE_PREFIXES = frozenset({"28", "29", "30", "47", "66"})
_FUTURES_NIGHT_PREFIXES = frozenset({"28", "29", "30"})


def reconcile_due_unknown_downloads(
    data_root: Path, operations: Iterable[Operation], now: datetime,
) -> dict[str, str]:
    """Recover a stranded GUI run once its slot is old and its app has exited."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(BEIJING)
    if local < datetime.fromisoformat(latest_slot(local)) + RETRY_AFTER:
        return {}
    from .tdx_unknown_recovery import reconcile_unknown_download

    history = list(operations)
    results = {}
    for terminal in TERMINALS:
        try:
            entries = _terminal_checkpoints(data_root, terminal)
        except (OSError, ValueError):
            results[terminal] = "RECONCILIATION_UNAVAILABLE"
            continue
        if any(isinstance(item, dict) and item.get("status") == "RUNNING_UNKNOWN"
               for item in entries.values()):
            results[terminal] = str(reconcile_unknown_download(data_root, terminal, history)["status"])
    return results


def _recent_raw_group_coverage(
    data_root: Path, terminal: str, slot_day: date,
    expected: dict[str, date], *, captured_after: datetime,
) -> str | None:
    """Use a dated endpoint scan only as a GUI hint, never as Silver proof."""

    path = Path(data_root) / "state" / "tdx_local_status.json"
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        checked_at = datetime.fromisoformat(snapshot["checkedAt"])
        if (checked_at.tzinfo is None or checked_at < captured_after
                or datetime.combine(slot_day, time(23), BEIJING) - checked_at > _WEEKEND_RAW_STATUS_MAX_AGE):
            return None
        roots = snapshot["roots"]
        root = next(item for item in roots if item.get("terminal") == terminal and item.get("available") is True)
        groups = {item["key"]: item for item in root["groups"]}
    except (OSError, ValueError, KeyError, StopIteration, TypeError):
        return None
    for key, target in expected.items():
        group = groups.get(key)
        if not isinstance(group, dict):
            return None
        try:
            count = int(group["fileCount"])
            current = int(group["commonLatestCount"])
            day = date.fromisoformat(group["commonLatestDate"])
            invalid = int(group["invalidFileCount"])
        except (KeyError, TypeError, ValueError):
            return None
        threshold = (count * 9 + 9) // 10 if terminal == "financial" else (count + 1) // 2
        if (count < 10 or invalid or group.get("rawStatus") != "ENDPOINTS_ONLY"
                or current < threshold or day < target):
            return None
    # importedLatestDate is a group maximum, not per-symbol Silver coverage.
    # A majority endpoint date likewise cannot prove every selected symbol.
    return "RAW_MAJORITY_RECENT_SILVER_UNVERIFIED"


def _futures_weekend_coverage(data_root: Path, slot_day: date,
                              now: datetime | None = None) -> str | None:
    """A completed run after the last domestic session makes a closed weekend redundant."""

    if slot_day.weekday() not in {5, 6}:
        return None
    from .futures_calendar import load_futures_trading_calendar

    calendar = load_futures_trading_calendar(data_root)
    if calendar is None:
        return None
    local_now = (now or datetime.combine(slot_day, time(23), BEIJING)).astimezone(BEIJING)
    if (local_now.date() > slot_day and local_now.time() >= time(16)
            and local_now.date().isoformat() in calendar.trading_days):
        # An 18:50 startup on Monday belongs to a new completed day session,
        # even though latest_slot() still names Sunday's 23:00 slot.
        return None
    previous = [date.fromisoformat(value) for value in calendar.trading_days
                if date.fromisoformat(value) < slot_day]
    following = [date.fromisoformat(value) for value in calendar.trading_days
                 if date.fromisoformat(value) > slot_day]
    if not previous or not following:
        return None
    last_day, next_day = previous[-1], following[0]
    checkpoints = _terminal_checkpoints(data_root, "futures")
    prior = [checkpoints.get(batch.key, {}) for batch in FUTURES]
    if any(item.get("status") != "UI_DONE_UNVERIFIED" for item in prior):
        return None
    try:
        completed_slot = datetime.fromisoformat(prior[0]["slot"])
        if (prior[1]["slot"] != prior[0]["slot"] or completed_slot.tzinfo is None
                or not last_day <= completed_slot.astimezone(BEIJING).date() < slot_day):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    expected = {
        f"ds.{prefix}.{period}":
        next_day if period == "5m" and prefix in _FUTURES_NIGHT_PREFIXES
        and last_day.weekday() == 4 else last_day
        for prefix in _FUTURES_EXCHANGE_PREFIXES for period in ("1d", "5m")
    }
    return _recent_raw_group_coverage(
        data_root, "futures", slot_day, expected,
        captured_after=datetime.combine(last_day + timedelta(days=1), time(3), BEIJING),
    )


def financial_cn_coverage(data_root: Path, slot_day: date,
                          now: datetime | None = None) -> str | None:
    """Check only mainland stock batches; extended markets need separate evidence."""

    from .a_share_calendar import is_verified_a_share_trading_day

    local_now = (now or datetime.now(BEIJING)).astimezone(BEIJING)
    target = local_now.date() if local_now.time() >= time(16) else local_now.date() - timedelta(days=1)
    for _ in range(12):
        if is_verified_a_share_trading_day(target):
            break
        target -= timedelta(days=1)
    else:
        return None
    checkpoints = _terminal_checkpoints(data_root, "financial")
    current_slot = datetime.combine(slot_day, time(23), BEIJING)
    for key in ("cn-day", "cn-5m"):
        item = checkpoints.get(key, {})
        if item.get("status") != "UI_DONE_UNVERIFIED":
            return None
        try:
            completed_slot = datetime.fromisoformat(item["slot"])
            finished_at = (datetime.fromisoformat(item["finishedAt"])
                           if item.get("finishedAt") else completed_slot)
        except (KeyError, TypeError, ValueError):
            return None
        if (completed_slot.tzinfo is None or finished_at.tzinfo is None or
                not target <= finished_at.astimezone(BEIJING).date() <= slot_day or
                finished_at.astimezone(BEIJING) >= current_slot):
            return None
    # The terminal checkbox downloads *all A/B shares*. Its skip decision must
    # cover the B-share files too; an A-only check would suppress a real gap.
    expected = {f"{market}.STOCK.{period}": target for market in ("sh", "sz", "bj")
                for period in ("1d", "5m")}
    expected.update({f"{market}.B_SHARE.{period}": target for market in ("sh", "sz")
                     for period in ("1d", "5m")})
    return _recent_raw_group_coverage(
        data_root, "financial", slot_day, expected,
        captured_after=datetime.combine(target, time(16), BEIJING),
    )


def futures_download_decision(
    data_root: Path, operations: Iterable[Operation], now: datetime,
) -> dict[str, object]:
    """Tell the scheduler why a future-terminal GUI run is or is not due."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    slot = latest_slot(now)
    completion = terminal_completion(_terminal_checkpoints(data_root, "futures"), slot, FUTURES)
    if completion in {"UI_DONE_UNVERIFIED", "COMPLETE_WITH_SKIPS", "NO_GUI_RAW_RECENT"}:
        return {"due": False, "reason": "CURRENT_SLOT_UI_DONE", "slot": slot}
    if completion == "PREVIOUS_RUN_NEEDS_RECONCILIATION":
        return {"due": False, "reason": "RECONCILIATION_REQUIRED", "slot": slot}
    history = [item for item in operations if item.kind == OperationKind.TDX_FUTURES_AUTO_DOWNLOAD]
    if any(item.status in _ACTIVE for item in history):
        return {"due": False, "reason": "ACTIVE", "slot": slot}
    local_day = datetime.fromisoformat(slot).astimezone(BEIJING).date()
    weekend = _futures_weekend_coverage(data_root, local_day, now)
    if weekend is not None:
        return {"due": False, "reason": weekend, "slot": slot}
    latest = max((item for item in history
                  if datetime.fromisoformat(item.created_at) >= datetime.fromisoformat(slot)),
                 key=lambda item: item.created_at, default=None)
    if latest is None:
        return {"due": True, "reason": "NEW_SLOT", "slot": slot}
    if latest.status in {OperationStatus.PASS, OperationStatus.CANCELLED}:
        return {"due": False, "reason": latest.status.value, "slot": slot}
    retry_from = latest.completed_at or latest.created_at
    retry_after = (MARKET_HOST_RETRY_AFTER if latest.detail and
                   "FUTURES_MARKET_HOST_DISCONNECTED" in latest.detail else RETRY_AFTER)
    due = now - datetime.fromisoformat(retry_from) >= retry_after
    return {"due": due, "reason": "RETRY_DUE" if due else "RETRY_BACKOFF", "slot": slot}


def _terminal_checkpoints(data_root: Path, terminal: str) -> dict:
    path = Path(data_root) / "state" / "tdx_download_ui" / "checkpoints.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get(terminal, {}), dict):
        raise ValueError("terminal download checkpoint is malformed")
    return payload.get(terminal, {})


def _financial_checkpoints(data_root: Path) -> dict:
    return _terminal_checkpoints(data_root, "financial")


def due_financial_download(
    data_root: Path, operations: Iterable[Operation], now: datetime,
) -> bool:
    """Retry an incomplete due slot after login; never replay an uncertain UI run."""

    return bool(financial_download_decision(data_root, operations, now)["due"])


def financial_download_decision(
    data_root: Path, operations: Iterable[Operation], now: datetime,
) -> dict[str, object]:
    """Skip current mainland data, retaining independent extended-market work."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    slot = latest_slot(now)
    checkpoints = _financial_checkpoints(data_root)
    completion = terminal_completion(checkpoints, slot, FINANCIAL)
    if completion in {"UI_DONE_UNVERIFIED", "COMPLETE_WITH_SKIPS", "NO_GUI_RAW_RECENT"}:
        return {"due": False, "reason": completion, "slot": slot, "skipBatches": {}}
    if completion == "PREVIOUS_RUN_NEEDS_RECONCILIATION":
        return {"due": False, "reason": completion, "slot": slot, "skipBatches": {}}
    history = [item for item in operations if item.kind == OperationKind.TDX_FINANCIAL_AUTO_DOWNLOAD]
    if any(item.status in _ACTIVE for item in history):
        return {"due": False, "reason": "ACTIVE", "slot": slot, "skipBatches": {}}
    reason = financial_cn_coverage(data_root, datetime.fromisoformat(slot).astimezone(BEIJING).date(), now)
    skip = {batch.key: reason for batch in FINANCIAL if batch.key.startswith("cn-")} if reason else {}
    outstanding = pending_keys(FINANCIAL, checkpoints, slot)
    if outstanding and all(batch.key in skip for batch in outstanding):
        return {"due": False, "reason": reason, "slot": slot, "skipBatches": skip}
    latest = max(
        (item for item in history if datetime.fromisoformat(item.created_at) >= datetime.fromisoformat(slot)),
        key=lambda item: item.created_at,
        default=None,
    )
    if latest is None:
        return {"due": True, "reason": "NEW_SLOT", "slot": slot, "skipBatches": skip}
    if latest.status in {OperationStatus.PASS, OperationStatus.CANCELLED}:
        return {"due": False, "reason": latest.status.value, "slot": slot, "skipBatches": skip}
    retry_from = latest.completed_at or latest.created_at
    retry_after = (MARKET_HOST_RETRY_AFTER if latest.detail and
                   "MARKET_HOST_INITIALIZING" in latest.detail else RETRY_AFTER)
    due = now - datetime.fromisoformat(retry_from) >= retry_after
    return {"due": due, "reason": "RETRY_DUE" if due else "RETRY_BACKOFF",
            "slot": slot, "skipBatches": skip}


def due_futures_download(data_root: Path, operations: Iterable[Operation], now: datetime) -> bool:
    """Schedule the futures terminal's two batches for the current due slot."""

    return bool(futures_download_decision(data_root, operations, now)["due"])


def _is_x86_python(executable: Path) -> bool:
    try:
        with executable.open("rb") as source:
            if source.read(2) != b"MZ":
                return False
            source.seek(0x3C)
            pe_offset = struct.unpack("<I", source.read(4))[0]
            source.seek(pe_offset)
            return source.read(4) == b"PE\x00\x00" and struct.unpack("<H", source.read(2))[0] == 0x14C
    except (OSError, struct.error):
        return False


def _local_status_groups(data_root: Path, terminal: str, now: datetime) -> list[dict]:
    """Use recent raw endpoints as hints; independently gate Silver ranges."""

    path = Path(data_root) / "state" / "tdx_local_status.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        checked_at = datetime.fromisoformat(payload["checkedAt"])
        age = now - checked_at
    except (OSError, ValueError, KeyError, TypeError):
        return []
    if checked_at.tzinfo is None or not timedelta(0) <= age < _LOCAL_STATUS_MAX_AGE:
        return []
    roots = payload.get("roots")
    if not isinstance(roots, list):
        return []
    for root in roots:
        if isinstance(root, dict) and root.get("terminal") == terminal and root.get("available") is True:
            groups = root.get("groups")
            if not isinstance(groups, list):
                return []
            inventory_valid = payload.get("inventoryAvailable") is True and payload.get("inventoryStale") is False
            return [{**item, "importedRangeStale": True} if not inventory_valid else item
                    for item in groups if isinstance(item, dict)]
    return []


def _group_start_candidates(groups: list[dict], batch_key: str, period: str,
                            target: date) -> list[date]:
    financial_prefixes: set[str] = set()
    if batch_key.startswith("extended-"):
        from .market_classification import market_classification_spec

        financial_prefixes = set(market_classification_spec().get("tdxFinancialDsPrefixes", {}))
    candidates: list[date] = []
    for group in groups:
        key = group.get("key")
        if not isinstance(key, str) or not key.endswith("." + period):
            continue
        scope = key[: -(len(period) + 1)]
        if batch_key.startswith("cn-"):
            permitted = scope in _FINANCIAL_STOCK_GROUPS
        elif batch_key.startswith("extended-"):
            permitted = scope.startswith("ds.") and scope[3:] in financial_prefixes
        else:
            permitted = scope.startswith("ds.") and scope[3:] in _FUTURES_EXCHANGE_PREFIXES
        if not permitted:
            continue
        try:
            count = int(group["fileCount"])
            common_count = int(group["commonLatestCount"])
            common_day = date.fromisoformat(group["commonLatestDate"])
        except (KeyError, TypeError, ValueError):
            continue
        # Small or split groups can be dominated by an illiquid or expired code.
        if count >= 10 and common_count >= (count + 1) // 2:
            candidates.append(min(common_day, target))
            # The local status index also reports the latest committed Silver
            # date. A lagging import can only move the download start earlier;
            # it must never be used to skip raw-file dates. Ignore carry-over
            # values when the import range itself is marked stale.
            if group.get("importedRangeStale") is not True:
                try:
                    imported_day = date.fromisoformat(str(group["importedLatestDate"]))
                except (KeyError, TypeError, ValueError):
                    pass
                else:
                    if imported_day <= target:
                        candidates.append(imported_day)
    return candidates


def batch_start_dates(data_root: Path, terminal: str, now: datetime,
                      *, selected_keys: set[str] | None = None) -> dict[str, date]:
    """Choose per-batch starts, overlapping the last observed day.

    UI completion is not file completeness. A fresh local inventory may pull the
    start earlier; missing evidence falls back to the last 31 calendar days.
    Older evidence needs an explicit historical backfill instead of truncation.
    """

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if terminal not in {"financial", "futures"}:
        raise ValueError("unknown terminal")
    target = datetime.fromisoformat(latest_slot(now)).astimezone(BEIJING).date()
    floor = target - _BACKFILL_WINDOW
    checkpoints = _terminal_checkpoints(data_root, terminal)
    groups = _local_status_groups(data_root, terminal, now)
    batches = FINANCIAL if terminal == "financial" else FUTURES
    starts: dict[str, date] = {}
    for batch in batches:
        if selected_keys is not None and batch.key not in selected_keys:
            continue
        period = "5m" if batch.key.endswith("5m") else "1d"
        candidates = _group_start_candidates(groups, batch.key, period, target)
        prior = checkpoints.get(batch.key)
        if isinstance(prior, dict) and prior.get("status") == "UI_DONE_UNVERIFIED":
            try:
                slot_time = datetime.fromisoformat(str(prior["slot"]))
                if slot_time.tzinfo is not None:
                    candidates.append(min(slot_time.astimezone(BEIJING).date(), target))
            except (KeyError, ValueError):
                pass
        if isinstance(prior, dict) and prior.get("status") == "INTERRUPTED_UNVERIFIED":
            try:
                candidates.append(date.fromisoformat(str(prior["startDate"])))
            except (KeyError, ValueError):
                pass
        start = min(candidates) if candidates else floor
        if start < floor:
            raise ValueError(f"{terminal}:{batch.key} requires explicit backfill before {floor.isoformat()}")
        starts[batch.key] = start
    return starts


def financial_minute_start(data_root: Path, now: datetime) -> date:
    """Compatibility wrapper for the existing extended-minute worker option."""

    return batch_start_dates(data_root, "financial", now)["extended-5m"]


def run_financial_download_worker(data_root: Path, *, owned_pid: int | None = None,
                                  owned_token: str | None = None) -> tuple[int | None, str | None]:
    """Run the native 32-bit GUI adapter; never use the 64-bit service process."""

    root = Path(data_root)
    python = root / "tools" / "python311-x86" / "python.exe"
    if not _is_x86_python(python):
        return None, "TDX_32BIT_RUNTIME_MISSING"
    state_dir = root / "state" / "tdx_download_ui"
    log_path = root / "logs" / "tdx-financial-gui.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    package_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (package_root, env.get("PYTHONPATH"))))
    env["PYTHONIOENCODING"] = "utf-8"
    now = datetime.now(BEIJING)
    skipped_reason = financial_cn_coverage(root, datetime.fromisoformat(latest_slot(now)).date())
    skipped_keys = {batch.key for batch in FINANCIAL if batch.key.startswith("cn-")} if skipped_reason else set()
    try:
        starts = batch_start_dates(root, "financial", now,
                                   selected_keys={batch.key for batch in FINANCIAL} - skipped_keys)
    except (OSError, ValueError) as error:
        return None, f"TDX_START_DATE_REVIEW_REQUIRED:{error}"
    command = [str(python), "-m", "market_monitor.tdx_download_ui", "--execute",
               "--terminal", "financial", "--auto-login-saved", "--timeout", "3600",
               "--state-dir", str(state_dir)]
    if owned_pid is not None or owned_token is not None:
        if owned_pid is None or owned_pid <= 0 or not owned_token:
            return None, "TDX_OWNED_PROCESS_INVALID"
        command.extend(("--owned-pid", str(owned_pid), "--owned-token", owned_token))
    if skipped_reason:
        for key in sorted(skipped_keys):
            command.extend(("--skip-batch", f"{key}:{skipped_reason}"))
    for batch in FINANCIAL:
        if batch.key in starts:
            command.extend(("--batch-start", f"{batch.key}:{starts[batch.key].isoformat()}"))
    try:
        with log_path.open("ab") as log:
            result = subprocess.run(
                command, cwd=Path(__file__).resolve().parents[3], env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                timeout=4 * 3600 + 180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
            )
    except subprocess.TimeoutExpired:
        return None, "TDX_DOWNLOAD_TIMEOUT_OUTCOME_UNKNOWN"
    except OSError:
        return None, "TDX_GUI_WORKER_START_FAILED"
    return result.returncode, None


def run_futures_download_worker(data_root: Path, *, owned_pid: int | None = None,
                                owned_token: str | None = None) -> tuple[int | None, str | None]:
    """Keep the 64-bit futures GUI worker outside the service process."""

    if sys.maxsize <= 2**32:
        return None, "TDX_64BIT_RUNTIME_REQUIRED"
    root = Path(data_root)
    state_dir = root / "state" / "tdx_download_ui"
    log_path = root / "logs" / "tdx-futures-gui.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    package_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (package_root, env.get("PYTHONPATH"))))
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        starts = batch_start_dates(root, "futures", datetime.now(BEIJING))
    except (OSError, ValueError) as error:
        return None, f"TDX_START_DATE_REVIEW_REQUIRED:{error}"
    command = [sys.executable, "-m", "market_monitor.tdx_download_ui", "--execute",
               "--terminal", "futures", "--timeout", "3600", "--state-dir", str(state_dir)]
    if owned_pid is not None or owned_token is not None:
        if owned_pid is None or owned_pid <= 0 or not owned_token:
            return None, "TDX_OWNED_PROCESS_INVALID"
        command.extend(("--owned-pid", str(owned_pid), "--owned-token", owned_token))
    for batch in FUTURES:
        command.extend(("--batch-start", f"{batch.key}:{starts[batch.key].isoformat()}"))
    try:
        with log_path.open("ab") as log:
            result = subprocess.run(
                command, cwd=Path(__file__).resolve().parents[3], env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                timeout=2 * 3600 + 180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
            )
    except subprocess.TimeoutExpired:
        return None, "TDX_FUTURES_TIMEOUT_OUTCOME_UNKNOWN"
    except OSError:
        return None, "TDX_FUTURES_GUI_WORKER_START_FAILED"
    return result.returncode, None


__all__ = ("due_financial_download", "financial_download_decision", "due_futures_download",
           "futures_download_decision", "financial_cn_coverage",
           "reconcile_due_unknown_downloads",
           "batch_start_dates", "financial_minute_start",
           "run_financial_download_worker", "run_futures_download_worker")
