"""Explicit per-account DCE request policy, independent of other providers."""
from __future__ import annotations

from concurrent.futures import CancelledError
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import threading
import time
from zoneinfo import ZoneInfo

from .atomic_files import replace_with_retry
from .futures_official_backfill import _process_file_lock


class DceAccountRateLimiter:
    def __init__(self, root: Path, key: str, *, window_seconds: float = 1, requests_per_window: int = 3,
                 now=None, sleep=time.sleep) -> None:
        if not math.isfinite(window_seconds) or window_seconds <= 0 or requests_per_window < 1:
            raise ValueError("Invalid account request policy")
        account_id = hashlib.sha256(key.encode()).hexdigest()[:20]
        directory = root / "state" / "dce_account_limits"
        self.path = directory / f"{account_id}.rate.json"
        self.cooldown_path = directory / f"{account_id}.json"
        self.lock_path = directory / f"{account_id}.lock"
        self.window_seconds, self.requests_per_window = window_seconds, requests_per_window
        self.now = now or (lambda: datetime.now(ZoneInfo("Asia/Shanghai")))
        self.sleep = sleep
        self.stop_event: threading.Event | None = None
        self.active = False
        self.adaptive_window: float | None = None
        if self.path.is_file():
            self._adopt(json.loads(self.path.read_text(encoding="utf-8")))

    def _adopt(self, state: dict) -> None:
        window = float(state.get("adaptiveWindowSeconds") or 0)
        limit = int(state.get("adaptiveRequestsPerWindow") or self.requests_per_window)
        if math.isfinite(window) and window > 0 and limit > 0:
            self.window_seconds = max(self.window_seconds, min(window, 60))
            self.requests_per_window = min(self.requests_per_window, limit)
            self.adaptive_window = self.window_seconds

    def backoff(self) -> None:
        """Persist a source-directed downgrade for this account only."""
        with _process_file_lock(self.lock_path):
            state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
            self._adopt(state)
            self.window_seconds = min(max(2.0, self.window_seconds * 2), 60)
            self.requests_per_window = 1
            self.adaptive_window = self.window_seconds
            state.update(adaptiveWindowSeconds=self.window_seconds, adaptiveRequestsPerWindow=1)
            self._save(self.path, state)

    def _save(self, path: Path, data: dict) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        replace_with_retry(temporary, path)

    def acquire(self, host: str) -> None:
        if host != "www.dce.com.cn" or self.active:
            raise RuntimeError("Invalid DCE account lease")
        while True:
            if self.stop_event is not None and self.stop_event.is_set():
                raise CancelledError()
            with _process_file_lock(self.lock_path):
                current = self.now()
                delay = 0.0
                if self.cooldown_path.exists():
                    cooldown = json.loads(self.cooldown_path.read_text(encoding="utf-8"))
                    delay = max(0, (datetime.fromisoformat(cooldown["pausedUntil"]) - current).total_seconds())
                if not delay:
                    state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
                    self._adopt(state)
                    starts = [datetime.fromisoformat(stamp) for stamp in state.get("requestStarts", [])]
                    starts = [stamp for stamp in starts if (current - stamp).total_seconds() < self.window_seconds]
                    if len(starts) >= self.requests_per_window:
                        delay = self.window_seconds - (current - min(starts)).total_seconds()
                    else:
                        day = current.date().isoformat()
                        count = int(state.get("dayCount", 0)) if state.get("day") == day else 0
                        self._save(self.path, {"schema": "dce-account-rate/v1", "day": day, "dayCount": count + 1,
                            "dailyLimit": None, "windowSeconds": self.window_seconds,
                            "requestsPerWindow": self.requests_per_window,
                            "adaptiveWindowSeconds": self.adaptive_window,
                            "adaptiveRequestsPerWindow": self.requests_per_window if self.adaptive_window else None,
                            "requestStarts": [stamp.isoformat() for stamp in [*starts, current]]})
                        self.active = True
                        return
            if self.stop_event is not None:
                if self.stop_event.wait(delay):
                    raise CancelledError()
            else:
                self.sleep(delay)

    def complete(self, host: str, *, retry_after_seconds: float = 0) -> None:
        if host != "www.dce.com.cn" or not self.active:
            raise RuntimeError("No DCE account lease")
        try:
            if retry_after_seconds > 0:
                with _process_file_lock(self.lock_path):
                    until = self.now() + timedelta(seconds=retry_after_seconds)
                    if self.cooldown_path.exists():
                        previous = json.loads(self.cooldown_path.read_text(encoding="utf-8"))
                        until = max(until, datetime.fromisoformat(previous["pausedUntil"]))
                    self._save(self.cooldown_path, {"pausedUntil": until.isoformat()})
        finally:
            self.active = False
