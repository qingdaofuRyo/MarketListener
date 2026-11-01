from __future__ import annotations

import gzip
import json
import threading
from pathlib import Path

import pytest

from market_monitor import market_readmodel as models
from market_monitor.profile_gate import ProfileGateBusy, profile_read_gate


def _rows(amount=0):
    return [{"instrumentId": "CN.DCE.FUTURE.A2611", "amount": amount,
             "notionalValue": None, "comparisonStatus": "MISMATCH",
             "sourcePolicyVersion": "tdx-test-policy"}]


def test_missing_readmodel_is_read_only_and_has_no_builder(tmp_path: Path):
    assert models.load_market_readmodel(tmp_path, current_version="v1") is None
    assert list(tmp_path.iterdir()) == []


def test_zero_and_nullable_metrics_keep_verified_status_and_real_version(tmp_path: Path):
    models.publish_market_readmodel(tmp_path, version="v1", items=_rows(),
                                   source_policy_version="tdx-test-policy",
                                   dependencies={"financialVersion": "financial-v1", "queryRevision": "r1"})
    snapshot = models.load_market_readmodel(tmp_path, current_version="v1")
    assert snapshot is not None and snapshot.status == "CURRENT"
    assert snapshot.items == _rows()
    assert snapshot.metadata() == {"queryVersion": "v1", "snapshotVersion": "v1",
        "publicationVersion": "v1", "snapshotStatus": "CURRENT",
        "snapshotPublishedAt": snapshot.published_at, "sourcePolicyVersion": "tdx-test-policy"}
    assert models.load_market_readmodel(tmp_path, current_version="v2") is None
    stale = models.load_market_readmodel(tmp_path, current_version="v2", allow_stale=True)
    assert stale is not None
    assert stale.metadata()["queryVersion"] == "v1"
    assert stale.metadata()["snapshotStatus"] == "STALE"


def test_each_category_keeps_own_version_and_atomic_publication_pointer(tmp_path: Path):
    models.publish_market_readmodel(tmp_path, version="v1", items=_rows(), identity="DCE")
    models.publish_market_readmodel(tmp_path, version="v2", items=_rows(123), identity="SHFE")
    old = models.load_market_readmodel(tmp_path, current_version="v2", identity="DCE", allow_stale=True)
    new = models.load_market_readmodel(tmp_path, current_version="v2", identity="SHFE")
    assert old is not None and new is not None
    assert old.snapshot_version == "v1" and old.publication_version == "v2" and old.status == "STALE"
    assert old.items[0]["amount"] == 0 and new.items[0]["amount"] == 123
    assert not list(tmp_path.rglob("*.tmp"))


def test_failed_manifest_replace_retains_previous_publication(tmp_path: Path, monkeypatch):
    models.publish_market_readmodel(tmp_path, version="v1", items=_rows())
    original = models.os.replace

    def failed_manifest(source, target):
        if Path(target).name == "market_list_publication.json":
            raise OSError("synthetic failure")
        original(source, target)

    monkeypatch.setattr(models.os, "replace", failed_manifest)
    with pytest.raises(OSError):
        models.publish_market_readmodel(tmp_path, version="v2", items=_rows(123))
    snapshot = models.load_market_readmodel(tmp_path, current_version="v1")
    assert snapshot is not None and snapshot.items == _rows()
    assert not list(tmp_path.rglob("*.tmp"))


def test_payload_sha_damage_rejected_even_after_warm_read(tmp_path: Path):
    manifest = models.publish_market_readmodel(tmp_path, version="v1", items=_rows())
    assert models.load_market_readmodel(tmp_path, current_version="v1") is not None
    path = tmp_path / "cache" / "market-readmodel" / manifest["models"]["all"]["file"]
    path.write_bytes(gzip.compress(b'{"items": []}', mtime=0))
    assert models.load_market_readmodel(tmp_path, current_version="v1") is None


def test_manifest_path_escape_and_wrong_count_rejected(tmp_path: Path):
    manifest = models.publish_market_readmodel(tmp_path, version="v1", items=_rows())
    pointer = tmp_path / "state" / "market_list_publication.json"
    manifest["models"]["all"]["file"] = "../../catalog.duckdb"
    pointer.write_text(json.dumps(manifest), encoding="utf-8")
    assert models.load_market_readmodel(tmp_path, current_version="v1") is None
    manifest = models.publish_market_readmodel(tmp_path, version="v1", items=_rows())
    models.load_market_readmodel(tmp_path, current_version="v1")
    manifest["models"]["all"]["recordCount"] = 200
    pointer.write_text(json.dumps(manifest), encoding="utf-8")
    assert models.load_market_readmodel(tmp_path, current_version="v1") is None


@pytest.mark.parametrize("items", [[{"instrumentId": ""}], [{"instrumentId": "same"}, {"instrumentId": "same"}],
                                  [{"instrumentId": "finite", "amount": float("nan")}]])
def test_invalid_publication_does_not_create_pointer(tmp_path: Path, items):
    with pytest.raises(ValueError):
        models.publish_market_readmodel(tmp_path, version="v1", items=items)
    assert models.publication_manifest(tmp_path) is None


def test_reader_hashes_once_per_immutable_payload(tmp_path: Path, monkeypatch):
    models.publish_market_readmodel(tmp_path, version="v1", items=_rows())
    calls = []
    original = models.gzip.open

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(models.gzip, "open", counted)
    for _ in range(3):
        assert models.load_market_readmodel(tmp_path, current_version="v1") is not None
    assert len(calls) == 1


def test_startup_reference_does_not_read_or_trust_corrupt_payload(tmp_path: Path, monkeypatch):
    manifest = models.publish_market_readmodel(tmp_path, version="v1", items=_rows())
    path = tmp_path / "cache" / "market-readmodel" / manifest["models"]["all"]["file"]
    path.write_bytes(gzip.compress(b'{"items": []}', mtime=0))
    original = models.gzip.open

    def forbidden_payload_read(*_args, **_kwargs):
        raise AssertionError("startup reference inspection must not decompress rows")

    monkeypatch.setattr(models.gzip, "open", forbidden_payload_read)
    reference = models.inspect_market_readmodel(tmp_path, current_version="v1")
    assert reference is not None and reference.status == "CURRENT"
    assert not hasattr(reference, "items")
    monkeypatch.setattr(models.gzip, "open", original)
    # Metadata inspection must not prime a trusted cache. The first real read
    # still verifies SHA and fails closed, so the watcher can queue recovery.
    assert models.load_market_readmodel(tmp_path, current_version="v1") is None


@pytest.mark.parametrize("corruption", ["same_size", "truncated"])
def test_reference_never_skips_real_reader_validation(tmp_path: Path, corruption):
    manifest = models.publish_market_readmodel(tmp_path, version="v1", items=_rows())
    path = tmp_path / "cache" / "market-readmodel" / manifest["models"]["all"]["file"]
    original = path.read_bytes()
    damaged = bytearray(original)
    if corruption == "same_size":
        damaged[-8] ^= 1  # gzip CRC damage while preserving file size
    else:
        damaged = damaged[:-3]
    path.write_bytes(damaged)
    assert models.inspect_market_readmodel(tmp_path, current_version="v1") is not None
    assert models.load_market_readmodel(tmp_path, current_version="v1") is None


def test_startup_reference_rejects_manifest_path_escape(tmp_path: Path):
    manifest = models.publish_market_readmodel(tmp_path, version="v1", items=_rows())
    manifest["models"]["all"]["file"] = "../../catalog.duckdb"
    pointer = tmp_path / "state" / "market_list_publication.json"
    pointer.write_text(json.dumps(manifest), encoding="utf-8")
    assert models.inspect_market_readmodel(tmp_path, current_version="v1") is None


def test_startup_reference_preserves_stale_version_and_missing_file(tmp_path: Path):
    manifest = models.publish_market_readmodel(tmp_path, version="v1", items=_rows())
    assert models.inspect_market_readmodel(tmp_path, current_version="v2") is None
    reference = models.inspect_market_readmodel(tmp_path, current_version="v2", allow_stale=True)
    assert reference is not None
    assert reference.snapshot_version == "v1" and reference.status == "STALE"
    path = tmp_path / "cache" / "market-readmodel" / manifest["models"]["all"]["file"]
    path.unlink()
    assert models.inspect_market_readmodel(tmp_path, current_version="v2", allow_stale=True) is None


@pytest.mark.parametrize("count", [True, -1, "1"])
def test_malformed_reference_count_is_not_accepted_by_startup_or_reader(tmp_path: Path, count):
    manifest = models.publish_market_readmodel(tmp_path, version="v1", items=_rows())
    manifest["models"]["all"]["recordCount"] = count
    pointer = tmp_path / "state" / "market_list_publication.json"
    pointer.write_text(json.dumps(manifest), encoding="utf-8")
    assert models.inspect_market_readmodel(tmp_path, current_version="v1") is None
    assert models.load_market_readmodel(tmp_path, current_version="v1") is None


def test_backup_cannot_capture_between_publication_payload_and_pointer(tmp_path: Path, monkeypatch):
    payload_written = threading.Event()
    resume = threading.Event()
    errors: list[BaseException] = []
    original = models._atomic_write

    def paused_write(path: Path, payload: bytes) -> None:
        original(path, payload)
        if path.name.endswith(".json.gz"):
            payload_written.set()
            assert resume.wait(3)

    monkeypatch.setattr(models, "_atomic_write", paused_write)

    def publish() -> None:
        try:
            models.publish_market_readmodel(tmp_path, version="v1", items=_rows())
        except BaseException as error:
            errors.append(error)

    writer = threading.Thread(target=publish)
    writer.start()
    try:
        assert payload_written.wait(3)
        with pytest.raises(ProfileGateBusy):
            with profile_read_gate(tmp_path, timeout=0.05):
                pytest.fail("backup captured a half-published model")
    finally:
        resume.set()
        writer.join(timeout=3)
    assert not writer.is_alive() and not errors
    assert models.load_market_readmodel(tmp_path, current_version="v1") is not None
