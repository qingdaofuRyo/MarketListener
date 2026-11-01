"""Rate-limited APP observations, independent of the published market data."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import threading
from zoneinfo import ZoneInfo

from .a_share_calendar import is_verified_a_share_trading_day
from .kpl_auto_refresh import _lock, _save
from .ths_app_etf_live import capture, credential_path, load_session

_BEIJING = ZoneInfo("Asia/Shanghai")
_INTERVAL = timedelta(minutes=15)
_ERROR_DELAY = timedelta(minutes=30)
_RATE_DELAY = timedelta(hours=1)
_STOPPED = {"AUTH_EXPIRED", "PROVIDER_REJECTED", "SESSION_INVALID"}
_RESULTS = _STOPPED | {"FAILED", "HTTP_ERROR", "RATE_LIMITED", "UNVERIFIED", "RUNNING", "NEVER"}


def enabled(root: Path) -> bool:
    path = Path(root) / "state" / "ths_app_etf_settings.json"
    try:
        if path.stat().st_size > 4096:
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(data, dict) and data.get("schema") == 1 and data.get("enabled") is True
    except (OSError, ValueError, TypeError):
        return False


def _read(root: Path) -> dict:
    path = Path(root) / "state" / "ths_app_etf_auto.json"
    try:
        if path.stat().st_size > 8192:
            raise ValueError("状态过大")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != 1 or data.get("status") not in _RESULTS:
            raise ValueError("状态无效")
        return data
    except FileNotFoundError:
        return {"schema": 1, "status": "NEVER"}


def read_status(root: Path, *, auto_enabled: bool) -> dict:
    try:
        data = _read(root)
    except (OSError, ValueError, TypeError):
        return {"status": "FAILED", "autoEnabled": auto_enabled and enabled(root), "intervalMinutes": 15}
    return {"status": data["status"], "autoEnabled": auto_enabled and enabled(root), "intervalMinutes": 15,
            "attemptAt": data.get("attemptAt"), "lastSuccessAt": data.get("lastSuccessAt"),
            "count": data.get("count")}


def run_due(root: Path, now: datetime, *, force: bool = False, fetch=capture) -> dict:
    """Record attempts before I/O; a rejected session stays blocked until reimported."""
    if now.tzinfo is None:
        raise ValueError("timezone required")
    root = Path(root)
    if not force and not enabled(root):
        return {"status": "DISABLED"}
    local = now.astimezone(_BEIJING)
    clock = local.time().replace(tzinfo=None)
    trading = is_verified_a_share_trading_day(local.date())
    in_session = trading and (time(9, 30) <= clock <= time(11, 30) or time(13) <= clock < time(15))
    after_close = trading and clock >= time(15, 30)
    if not force and not (in_session or after_close):
        return {"status": "NOT_DUE"}
    path = root / "state" / "ths_app_etf_auto.json"
    try:
        with _lock(path.with_suffix(".lock")):
            state = _read(root)
            try:
                protected = credential_path().read_bytes()
                if not protected or len(protected) > 200_000:
                    raise ValueError("会话无效")
                revision = sha256(protected).hexdigest()
            except (OSError, ValueError, RuntimeError):
                state.update(status="SESSION_INVALID", attemptAt=now.astimezone(timezone.utc).isoformat())
                _save(path, state)
                return {"status": "SESSION_INVALID"}
            if state.get("status") in _STOPPED and state.get("sessionRevision") == revision:
                return {"status": state["status"]}
            if not force and after_close and state.get("closedDay") == local.date().isoformat():
                return {"status": "NOT_DUE"}
            if not force and state.get("attemptAt"):
                previous = datetime.fromisoformat(state["attemptAt"])
                if previous.tzinfo is None:
                    raise ValueError("时区缺失")
                delay = (_RATE_DELAY if state["status"] == "RATE_LIMITED" else
                         _ERROR_DELAY if state["status"] in {"FAILED", "HTTP_ERROR", "RUNNING"} else _INTERVAL)
                if now.astimezone(timezone.utc) - previous.astimezone(timezone.utc) < delay:
                    return {"status": "NOT_DUE"}
            state.update(status="RUNNING", attemptAt=now.astimezone(timezone.utc).isoformat(),
                         sessionRevision=revision)
            _save(path, state)
            try:
                config = load_session()
            except (OSError, ValueError, RuntimeError, ImportError):
                result = {"status": "SESSION_INVALID"}
            else:
                try:
                    result = fetch(root, config)
                except Exception:
                    result = {"status": "FAILED"}
            status = result.get("status") if isinstance(result, dict) else None
            if status not in _RESULTS - {"NEVER", "RUNNING"}:
                status = "FAILED"
            state["status"] = status
            if status == "UNVERIFIED":
                state.update(lastSuccessAt=result["captureEnd"], count=result["count"])
                if after_close:
                    state["closedDay"] = local.date().isoformat()
            _save(path, state)
            return {"status": status, "count": result.get("count")}
    except (OSError, ValueError, TypeError, KeyError):
        return {"status": "FAILED"}


def watch(root: Path, stop: threading.Event) -> None:
    while not stop.is_set():
        run_due(root, datetime.now(timezone.utc))
        stop.wait(60)
