"""Monotonic local-market data revisions.

The desktop terminal uses the revision as a cache key.  Producers advance it
only after a Silver partition has been atomically replaced, so a chart never
mixes bars from two local collection batches.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


_LOCK = threading.RLock()
_VERSION_FILE = "market_data_version.json"


def market_data_version(data_root: Path) -> str:
    """Return the current local-market revision without scanning Silver files."""

    path = _version_path(data_root)
    with _LOCK:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            revision = payload.get("revision") if isinstance(payload, dict) else None
            if isinstance(revision, str) and revision:
                return revision
        except (OSError, ValueError, TypeError):
            pass
        # Existing installations predate the revision file.  Establishing one
        # is a one-time local operation; later reads are O(1).
        revision = f"bootstrap-{_silver_fingerprint(data_root)}"
        _write(path, revision)
        return revision


def advance_market_data_version(data_root: Path) -> tuple[str, str]:
    """Advance and persist the revision after a successful Silver write."""

    with _LOCK:
        previous = market_data_version(data_root)
        current = f"r-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex[:10]}"
        _write(_version_path(data_root), current)
        return previous, current


def _version_path(data_root: Path) -> Path:
    return Path(data_root) / "state" / _VERSION_FILE


def _write(path: Path, revision: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    payload = {
        "schemaVersion": 1,
        "revision": revision,
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _silver_fingerprint(data_root: Path) -> str:
    count = 0
    newest = 0
    size = 0
    for path in (Path(data_root) / "silver").rglob("*.parquet"):
        try:
            stat = path.stat()
        except OSError:
            continue
        count += 1
        newest = max(newest, stat.st_mtime_ns)
        size += stat.st_size
    return f"{count}-{newest}-{size}"


__all__ = ("advance_market_data_version", "market_data_version")
