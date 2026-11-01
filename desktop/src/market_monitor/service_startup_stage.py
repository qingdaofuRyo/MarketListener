"""Bounded, best-effort startup checkpoints for the local service supervisor."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


_STARTUP_ID = re.compile(r"[0-9a-f]{32}\Z")


class StartupStageReporter:
    """Record only phase transitions for the supervisor's current launch."""

    def __init__(self, data_root: Path) -> None:
        launch_id = os.environ.get("MARKETLISTENER_STARTUP_ID", "")
        self._launch_id = launch_id if _STARTUP_ID.fullmatch(launch_id) else None
        self._path = data_root / "state" / "service_startup.json"
        self._started_wall = datetime.now(timezone.utc)
        self._started_clock = time.monotonic()
        self._phase_history: list[dict[str, str | float]] = []
        self.phase: str | None = None

    def mark(self, phase: str) -> None:
        if self._launch_id is None or phase == self.phase:
            return
        self.phase = phase
        now = datetime.now(timezone.utc)
        elapsed = round(max(0.0, time.monotonic() - self._started_clock), 3)
        self._phase_history.append({"phase": phase, "elapsedSeconds": elapsed})
        self._phase_history = self._phase_history[-16:]
        payload = {
            "schema": 1,
            "launchId": self._launch_id,
            "phase": phase,
            "status": "READY" if phase == "READY" else "FAILED" if phase == "FAILED" else "STARTING",
            "startedAt": self._started_wall.isoformat(),
            "updatedAt": now.isoformat(),
            "elapsedSeconds": elapsed,
            "phaseHistory": self._phase_history,
        }
        temporary: Path | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self._path.parent,
                prefix=".service_startup-", suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                # This is diagnostic telemetry, not authoritative data. The
                # atomic rename preserves a whole record without a disk flush
                # on every pre-READY phase transition.
            os.replace(temporary, self._path)
        except OSError:
            # Telemetry must never prevent the local service from starting.
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
