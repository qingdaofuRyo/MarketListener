"""Atomic, immutable market-list publications produced by the single writer.

Readers never build a missing publication. These files are derived runtime
outputs; Silver and the official source facts remain authoritative.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat as stat_module
import threading
from typing import Any, Mapping, Sequence
from uuid import uuid4
from zlib import error as ZlibError

from .atomic_files import replace_with_retry
from .profile_gate import profile_write_gate


SCHEMA_VERSION = 1
_MAX_PAYLOAD_BYTES = 128 * 1024 * 1024
_MAX_HOT_PAYLOAD_BYTES = 128 * 1024 * 1024
_LOCK = threading.RLock()
_HOT: OrderedDict[tuple[str, str, int, int], "MarketReadModel"] = OrderedDict()
_HOT_SIZES: dict[tuple[str, str, int, int], int] = {}
_PUBLICATION_FILE = "market_list_publication.json"


@dataclass(frozen=True)
class MarketReadModel:
    items: list[dict[str, Any]]
    snapshot_version: str
    publication_version: str
    status: str
    published_at: str
    dependencies: dict[str, Any]

    def metadata(self) -> dict[str, Any]:
        """The query version describes the rows actually read, even if stale."""
        return {"queryVersion": self.snapshot_version,
                "snapshotVersion": self.snapshot_version,
                "publicationVersion": self.publication_version,
                "snapshotStatus": self.status,
                "snapshotPublishedAt": self.published_at,
                "sourcePolicyVersion": self.dependencies.get("sourcePolicyVersion")}


@dataclass(frozen=True)
class MarketReadModelReference:
    """Advisory publication metadata, never evidence that rows are readable.

    Startup may use this to choose when to queue publication work. Readers and
    the background recovery watcher must still call ``load_market_readmodel``
    to verify the payload before accepting its data.
    """

    snapshot_version: str
    status: str


@dataclass(frozen=True)
class _PublicationLocation:
    manifest: dict[str, Any]
    entry: dict[str, Any]
    path: Path
    file_stat: os.stat_result
    status: str


def _manifest_path(root: Path) -> Path:
    return Path(root) / "state" / _PUBLICATION_FILE


def publication_manifest(root: Path) -> dict[str, Any] | None:
    """Read only the small commit pointer, without touching Catalog or Silver."""
    try:
        with _manifest_path(root).open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return None
        value = json.loads(raw)
        if (isinstance(value, dict) and value.get("schemaVersion") == SCHEMA_VERSION
                and isinstance(value.get("models"), dict)
                and isinstance(value.get("publicationVersion"), str)):
            return value
    except (OSError, ValueError, TypeError):
        pass
    return None


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def publish_market_readmodel(root: Path, *, version: str,
                             items: Sequence[Mapping[str, Any]], identity: str = "all",
                             source_policy_version: str = "",
                             dependencies: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Publish fully decorated rows after their source/version checks succeed.

    The caller is the existing writer/background publication boundary. No
    second database writer or source reads are introduced by this function.
    Existing model identities retain their own real versions until replaced.
    """
    if not isinstance(version, str) or not version or not isinstance(identity, str) or not identity:
        raise ValueError("Market publication requires a version and identity")
    rows = [dict(item) for item in items]
    ids = [str(item.get("instrumentId") or "") for item in rows]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("Market publication requires unique instrument identifiers")
    provenance = dict(dependencies or {})
    if source_policy_version:
        provenance["sourcePolicyVersion"] = source_policy_version
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = json.dumps({"schemaVersion": SCHEMA_VERSION, "version": version,
                          "identity": identity, "items": rows, "dependencies": provenance},
                         ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(payload) > _MAX_PAYLOAD_BYTES:
        raise ValueError("Market publication exceeds the bounded payload budget")
    digest = sha256(payload).hexdigest()
    name = f"{digest}.json.gz"
    # The profile backup includes the publication pointer. Keep the payload
    # and pointer switch behind one outer profile transaction boundary.
    with profile_write_gate(Path(root)), _LOCK:
        _atomic_write(Path(root) / "cache" / "market-readmodel" / name,
                      gzip.compress(payload, compresslevel=1, mtime=0))
        previous = publication_manifest(root)
        models = dict((previous or {}).get("models", {}))
        models[identity] = {"version": version, "file": name, "sha256": digest,
                            "recordCount": len(rows), "publishedAt": now,
                            "dependencies": provenance}
        manifest = {"schemaVersion": SCHEMA_VERSION, "publicationVersion": version,
                    "publishedAt": now, "models": models}
        pointer = json.dumps(manifest, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(pointer) > 1024 * 1024:
            raise ValueError("Market publication pointer exceeds the metadata budget")
        _atomic_write(_manifest_path(root), pointer)
        # Do not evict/delete an older file while an in-flight reader uses it.
        # Retention is a writer maintenance task, never a GET side effect.
    return manifest


def _publication_location(root: Path, *, current_version: str,
                          identity: str, allow_stale: bool) -> _PublicationLocation | None:
    manifest = publication_manifest(root)
    entry = (manifest or {}).get("models", {}).get(identity)
    if not isinstance(entry, dict):
        return None
    snapshot_version = entry.get("version")
    if not isinstance(snapshot_version, str) or not snapshot_version:
        return None
    status = "CURRENT" if snapshot_version == current_version else "STALE"
    if status == "STALE" and not allow_stale:
        return None
    name, digest = entry.get("file"), entry.get("sha256")
    if (not isinstance(name, str) or not re.fullmatch(r"[a-f0-9]{64}\.json\.gz", name)
            or not isinstance(digest, str) or name != f"{digest}.json.gz"):
        return None
    count = entry.get("recordCount")
    if (not isinstance(count, int) or isinstance(count, bool) or count < 0
            or not isinstance(entry.get("dependencies"), dict)):
        return None
    path = Path(root) / "cache" / "market-readmodel" / name
    try:
        stat = path.stat()
    except OSError:
        return None
    if not stat_module.S_ISREG(stat.st_mode) or not 0 < stat.st_size <= _MAX_PAYLOAD_BYTES:
        return None
    return _PublicationLocation(manifest, entry, path, stat, status)


def inspect_market_readmodel(root: Path, *, current_version: str,
                             identity: str = "all",
                             allow_stale: bool = False) -> MarketReadModelReference | None:
    """Inspect a bounded commit pointer/file stat without decoding the payload.

    A result only means a structurally valid reference exists. It deliberately
    does not populate the verified hot cache or attest to SHA/schema integrity.
    Corrupt payloads remain unreadable until the writer publishes a valid one.
    """
    location = _publication_location(root, current_version=current_version,
                                     identity=identity, allow_stale=allow_stale)
    if location is None:
        return None
    return MarketReadModelReference(location.entry["version"], location.status)


def load_market_readmodel(root: Path, *, current_version: str,
                         identity: str = "all", allow_stale: bool = False) -> MarketReadModel | None:
    """Return one verified publication, or None; never perform source work."""
    location = _publication_location(root, current_version=current_version,
                                     identity=identity, allow_stale=allow_stale)
    if location is None:
        return None
    manifest, entry = location.manifest, location.entry
    path, stat, status = location.path, location.file_stat, location.status
    snapshot_version, digest = entry["version"], entry["sha256"]
    cache_key = (str(Path(root).resolve()), digest, stat.st_mtime_ns, stat.st_size)
    with _LOCK:
        cached = _HOT.get(cache_key)
        if cached is not None:
            _HOT.move_to_end(cache_key)
            rows, dependencies = cached.items, cached.dependencies
            raw_size = _HOT_SIZES[cache_key]
            if (cached.snapshot_version != snapshot_version or len(rows) != entry.get("recordCount")
                    or dependencies != entry.get("dependencies")):
                return None
        else:
            try:
                with gzip.open(path, "rb") as stream:
                    raw = stream.read(_MAX_PAYLOAD_BYTES + 1)
                if len(raw) > _MAX_PAYLOAD_BYTES or sha256(raw).hexdigest() != digest:
                    return None
                payload = json.loads(raw)
                raw_size = len(raw)
                rows, dependencies = payload["items"], payload["dependencies"]
                if (payload.get("schemaVersion") != SCHEMA_VERSION
                        or payload.get("version") != snapshot_version or payload.get("identity") != identity
                        or not isinstance(rows, list) or len(rows) != entry.get("recordCount")
                        or any(not isinstance(row, dict) for row in rows)
                        or not isinstance(dependencies, dict) or dependencies != entry.get("dependencies")):
                    return None
            except (OSError, EOFError, ZlibError, ValueError, KeyError, TypeError):
                return None
        result = MarketReadModel(rows, snapshot_version, str(manifest["publicationVersion"]),
                                 status, str(entry.get("publishedAt") or ""), dependencies)
        _HOT[cache_key] = result
        _HOT_SIZES[cache_key] = raw_size
        while len(_HOT) > 32 or sum(_HOT_SIZES.values()) > _MAX_HOT_PAYLOAD_BYTES:
            oldest, _value = _HOT.popitem(last=False)
            _HOT_SIZES.pop(oldest, None)
    return result
