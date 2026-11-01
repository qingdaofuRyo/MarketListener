"""Independent, bounded sampling of unverified KPL evidence, never Gold."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import threading
from typing import Iterator
from uuid import uuid4
from zoneinfo import ZoneInfo

from .a_share_calendar import (has_verified_a_share_calendar, is_known_a_share_closed_day,
                               is_verified_a_share_trading_day)
from .kpl_market_probe import probe_kpl_history, probe_kpl_market

_BEIJING = ZoneInfo("Asia/Shanghai")
_INTERVAL = timedelta(minutes=5)
_ERROR_DELAY = timedelta(minutes=30)
_BLOCKED_DELAY = timedelta(hours=1)


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(state), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load(path: Path) -> dict:
    if not path.exists():
        return {"schema": 1, "live": {}, "history": {}}
    if path.stat().st_size > 4096:
        raise ValueError("oversized schedule state")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ValueError("invalid schedule state")
    if not all(isinstance(data.get(key), dict) for key in ("live", "history")):
        raise ValueError("invalid schedule entries")
    return data


def _previous_verified_day(day: date) -> date | None:
    if not has_verified_a_share_calendar(day.year):
        return None
    candidate = day - timedelta(days=1)
    for _ in range(31):
        if candidate.year != day.year and not has_verified_a_share_calendar(candidate.year):
            return None
        if is_verified_a_share_trading_day(candidate):
            return candidate
        if not is_known_a_share_closed_day(candidate):
            return None
        candidate -= timedelta(days=1)
    return None


def run_due_kpl_probes(root: Path, now: datetime, *, stop: threading.Event | None = None) -> list[str]:
    """Attempt due samples; a durable attempt timestamp is written before I/O."""
    if now.tzinfo is None:
        raise ValueError("timezone required")
    local = now.astimezone(_BEIJING)
    day, clock = local.date(), local.time().replace(tzinfo=None)
    trading = is_verified_a_share_trading_day(day)
    in_session = trading and (time(9, 30) <= clock <= time(11, 30) or time(13) <= clock < time(15))
    # Allow the source some time to settle after the closing auction. This is
    # still an unverified observation, not a claim that final data is ready.
    after_close = trading and clock >= time(15, 30)
    target = day
    if not in_session and not after_close:
        if trading and clock >= time(9, 30):  # lunch and the close settlement interval
            return []
        if not trading and not is_known_a_share_closed_day(day):
            return []
        target = _previous_verified_day(day)
        if target is None:
            return []
    state_path = Path(root) / "state" / "kpl_auto_schedule.json"
    done: list[str] = []
    with _lock(state_path.with_suffix(".lock")):
        state = _load(state_path)
        for kind in (("live",) if in_session else ("live", "history") if after_close else ("history",)):
            if stop is not None and stop.is_set():
                break
            entry = state[kind]
            if not in_session and entry.get("closedDay") == target.isoformat():
                continue
            last = entry.get("attemptAt")
            if last:
                stamp = datetime.fromisoformat(last)
                if stamp.tzinfo is None:
                    raise ValueError("naive attempt timestamp")
                delay = (_BLOCKED_DELAY if entry.get("result") == "BLOCKED" else
                         _ERROR_DELAY if entry.get("result") in {"FAILED", "EMPTY", "STALE", "RUNNING"} else
                         _INTERVAL)
                if now.astimezone(timezone.utc) - stamp.astimezone(timezone.utc) < delay:
                    continue
            entry.update({"attemptAt": now.astimezone(timezone.utc).isoformat(), "result": "RUNNING"})
            _save(state_path, state)
            try:
                result = (probe_kpl_market if kind == "live" else probe_kpl_history)(root, expected_date=target)
                status = result.get("status")
            except Exception:  # noqa: BLE001 - independent source failure must not stop the watcher
                status = "FAILED"
            entry["result"] = status if status in {"UNVERIFIED", "BLOCKED", "EMPTY", "STALE", "FAILED"} else "FAILED"
            if not in_session and entry["result"] == "UNVERIFIED":
                entry["closedDay"] = target.isoformat()
            _save(state_path, state)
            done.append(kind)
    return done


def watch_kpl(root: Path, stop: threading.Event) -> None:
    """Check immediately on service startup, then at most once a minute."""
    while not stop.is_set():
        try:
            run_due_kpl_probes(root, datetime.now(timezone.utc), stop=stop)
        except (OSError, ValueError, TypeError, KeyError):
            pass  # Fail closed; next check cannot erase invalid persisted state.
        if stop.wait(60):
            break
