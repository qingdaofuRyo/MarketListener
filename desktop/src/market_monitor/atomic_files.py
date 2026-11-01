"""Bounded handling of transient Windows file replacement contention."""
from __future__ import annotations

import os
from pathlib import Path
import time
from uuid import uuid4


def publish_new_bytes(target: Path, content: bytes) -> bool:
    """Create a complete file once; never expose a partially written target."""

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # A hard link publishes the already durable bytes atomically and
            # fails if another launcher created the target first.
            os.link(temporary, target)
        except FileExistsError:
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


def replace_with_retry(source: Path, target: Path) -> None:
    """Keep atomic replacement and propagate persistent access failures."""
    for attempt in range(6):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * 2 ** attempt)
