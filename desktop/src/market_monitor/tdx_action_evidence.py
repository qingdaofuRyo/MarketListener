"""Read-only, bounded evidence from the financial terminal's local gbbq file.

This is positive evidence only: missing records do not prove that a company had
no corporate action. It never updates TDX files or authoritative Silver.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import re
import struct
import tempfile
import threading
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from market_monitor.tdx_local import resolve_tdx_root

_MAX_BYTES = 64 * 1024 * 1024
_PRICE_ACTION_CATEGORIES = {1, 11, 12, 15}
_LOCK = threading.Lock()
_CACHE_KEY: tuple[Any, ...] | None = None
_CACHE: "ActionSnapshot | None" = None


@dataclass(frozen=True)
class ActionSnapshot:
    status: str
    sha256: str | None
    modified_at: str | None
    events: dict[str, tuple[str, ...]]
    source_path: Path | None = None
    identity: tuple[int, int, int] | None = None

    def public(self) -> dict[str, Any]:
        return {"status": self.status, "sha256": self.sha256, "localModifiedAt": self.modified_at}

    def window_events(self, instrument_id: str, first_day: str, last_day: str) -> list[str]:
        return [day for day in self.events.get(instrument_id, ()) if first_day < day <= last_day]


def _index_events(rows: Iterable[tuple[Any, Any, Any, Any]]) -> dict[str, tuple[str, ...]]:
    markets = {0: "SZSE", 1: "SSE", 2: "BSE"}
    indexed: dict[str, set[str]] = {}
    for market, code, raw_day, category in rows:
        try:
            market, category = int(market), int(category)
        except (TypeError, ValueError, OverflowError):
            continue
        if category not in _PRICE_ACTION_CATEGORIES or market not in markets or not re.fullmatch(r"\d{6}", str(code)):
            continue
        try:
            day = datetime.strptime(str(raw_day), "%Y%m%d").date().isoformat()
        except ValueError:
            continue
        key = f"CN.{markets[market]}.STOCK.{code}"
        indexed.setdefault(key, set()).add(day)
    return {key: tuple(sorted(days)) for key, days in indexed.items()}


def _identity(path: Path) -> tuple[int, int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def action_snapshot_is_current(snapshot: ActionSnapshot) -> bool:
    if snapshot.source_path is None:
        root = resolve_tdx_root()
        return root is None or not (root / "T0002" / "hq_cache" / "gbbq").is_file()
    try:
        return _identity(snapshot.source_path) == snapshot.identity
    except OSError:
        return False


def load_action_snapshot() -> ActionSnapshot:
    """Decode one immutable byte snapshot and cache it until the source changes."""
    global _CACHE_KEY, _CACHE
    root = resolve_tdx_root()
    path = root / "T0002" / "hq_cache" / "gbbq" if root else None
    if path is None or not path.is_file():
        return ActionSnapshot("UNAVAILABLE", None, None, {})
    try:
        identity = _identity(path)
    except OSError:
        return ActionSnapshot("UNAVAILABLE", None, None, {})
    key = (str(path.resolve()), *identity)
    with _LOCK:
        if key == _CACHE_KEY and _CACHE is not None:
            return _CACHE
        status, digest, modified, events = "UNAVAILABLE", None, None, {}
        try:
            if identity[0] < 4 or identity[0] > _MAX_BYTES:
                raise ValueError("invalid action file size")
            raw = path.read_bytes()
            if _identity(path) != identity or len(raw) != identity[0]:
                raise ValueError("action file changed while reading")
            count = struct.unpack_from("<I", raw)[0]
            if count <= 0 or len(raw) != 4 + count * 29:
                raise ValueError("invalid action file structure")
            from pytdx.reader import GbbqReader

            with tempfile.TemporaryDirectory(prefix="marketlistener-actions-") as temp:
                copy = Path(temp) / "gbbq"
                copy.write_bytes(raw)
                frame = GbbqReader().get_df(str(copy))
            if len(frame) != count:
                raise ValueError("action record count mismatch")
            columns = ["market", "code", "datetime", "category"]
            events = _index_events(frame[columns].itertuples(index=False, name=None))
            if _identity(path) != identity:
                raise ValueError("action file changed while decoding")
            digest = sha256(raw).hexdigest()
            modified = datetime.fromtimestamp(identity[1] / 1_000_000_000, ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
            status = "LOCAL_RECORDS_UNVERIFIED"
        except (OSError, ValueError, KeyError, TypeError, IndexError, OverflowError, EOFError, struct.error, ImportError, UnicodeError):
            pass
        snapshot = ActionSnapshot(status, digest, modified, events, path, identity)
        _CACHE_KEY, _CACHE = key, snapshot
        return snapshot
