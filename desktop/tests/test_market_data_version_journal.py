"""Synthetic Silver revision-chain checks; never touch the real data root."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

import market_monitor.market_data_version as versions


def _silver_file(root: Path, name: str, content: bytes) -> tuple[Path, str]:
    file = root / "silver" / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(content)
    return file, hashlib.sha256(content).hexdigest()


def test_journal_proves_complete_chain_and_latest_sha(tmp_path: Path) -> None:
    first, first_sha = _silver_file(tmp_path, "exchange=DCE/a.parquet", b"first")
    second, second_sha = _silver_file(tmp_path, "exchange=CZCE/b.parquet", b"second")
    base = versions.market_data_version(tmp_path)
    assert versions.changes_between(tmp_path, base, base) == {}

    previous, middle = versions.advance_market_data_version(
        tmp_path, changed_file=first, sha256=first_sha.upper(),
    )
    assert previous == base
    previous, next_revision = versions.advance_market_data_version(
        tmp_path, changed_file=second, sha256=second_sha,
    )
    assert previous == middle
    first.write_bytes(b"replacement")
    replacement_sha = hashlib.sha256(b"replacement").hexdigest()
    previous, current = versions.advance_market_data_version(
        tmp_path, changed_file=first, sha256=replacement_sha,
    )
    assert previous == next_revision
    assert versions.changes_between(tmp_path, base, current) == {
        "silver/exchange=DCE/a.parquet": replacement_sha,
        "silver/exchange=CZCE/b.parquet": second_sha,
    }
    assert versions.changes_between(tmp_path, middle, current) == {
        "silver/exchange=DCE/a.parquet": replacement_sha,
        "silver/exchange=CZCE/b.parquet": second_sha,
    }
    assert versions.changes_between(tmp_path, base, next_revision) is None


def test_legacy_unknown_and_invalid_identity_force_full_rebuild(tmp_path: Path) -> None:
    state = tmp_path / "state" / "market_data_version.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"schemaVersion": 1, "revision": "legacy"}), encoding="utf-8")
    assert versions.changes_between(tmp_path, "legacy", "legacy") is None
    file, sha = _silver_file(tmp_path, "a.parquet", b"a")
    previous, upgraded = versions.advance_market_data_version(tmp_path, changed_file=file, sha256=sha)
    assert previous == "legacy"
    assert versions.changes_between(tmp_path, "legacy", upgraded) is None

    previous, current = versions.advance_market_data_version(tmp_path, changed_file=file, sha256=sha)
    assert previous == upgraded
    assert versions.changes_between(tmp_path, upgraded, current) == {"silver/a.parquet": sha}

    _, unknown = versions.advance_market_data_version(tmp_path)
    assert versions.changes_between(tmp_path, current, unknown) is None
    assert versions.changes_between(tmp_path, upgraded, unknown) is None
    _, invalid = versions.advance_market_data_version(
        tmp_path, changed_file=tmp_path / "silver" / ".." / "outside.parquet", sha256=sha,
    )
    assert versions.changes_between(tmp_path, unknown, invalid) is None


def test_legacy_revision_migrates_only_at_controlled_boundary(tmp_path: Path) -> None:
    state = tmp_path / "state" / "market_data_version.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"schemaVersion": 1, "revision": "legacy"}), encoding="utf-8")
    assert versions.market_data_version(tmp_path) == "legacy"
    assert json.loads(state.read_text(encoding="utf-8"))["schemaVersion"] == 1
    current = versions.migrate_legacy_market_data_version(tmp_path)
    assert current is not None and current.startswith("migrated-")
    assert versions.market_data_version(tmp_path) == current
    assert versions.revision_is_settled(tmp_path, current)
    assert versions.market_unknown_epoch(tmp_path) is not None
    assert versions.changes_between(tmp_path, "legacy", current) is None
    assert versions.migrate_legacy_market_data_version(tmp_path) == current

    state.write_text('{"schemaVersion":1,"revision":"legacy","pending":["lost"]}', encoding="utf-8")
    assert versions.migrate_legacy_market_data_version(tmp_path) is None
    assert versions.market_data_version(tmp_path) == "invalid-silver-revision-state"
    assert state.read_text(encoding="utf-8").startswith('{"schemaVersion":1')


def test_revision_is_invalidated_before_new_parquet_target_is_promoted(tmp_path: Path) -> None:
    base = versions.market_data_version(tmp_path)
    target = tmp_path / "silver" / "market=CN" / "new-partition.parquet"
    digest = hashlib.sha256(b"validated staging bytes").hexdigest()
    assert not target.exists()
    previous, current = versions.advance_market_data_version(
        tmp_path, changed_file=target, sha256=digest,
    )
    assert previous == base
    assert versions.changes_between(tmp_path, base, current) == {
        "silver/market=CN/new-partition.parquet": digest,
    }


def test_pending_write_blocks_publication_until_settled(tmp_path: Path) -> None:
    base = versions.market_data_version(tmp_path)
    target = tmp_path / "silver" / "new.parquet"
    digest = hashlib.sha256(b"new bytes").hexdigest()
    _, pending_revision = versions.advance_market_data_version(
        tmp_path, changed_file=target, sha256=digest, pending=True,
    )
    assert versions.market_data_version(tmp_path) == pending_revision
    assert versions.revision_is_settled(tmp_path, pending_revision) is False
    assert versions.changes_between(tmp_path, base, pending_revision) is None
    with_pending = json.loads((tmp_path / "state" / "market_data_version.json").read_text())
    assert with_pending["pending"] == [pending_revision]

    target.parent.mkdir(parents=True)
    target.write_bytes(b"new bytes")
    versions.settle_market_data_version(tmp_path, pending_revision)
    assert versions.revision_is_settled(tmp_path, pending_revision) is True
    assert versions.changes_between(tmp_path, base, pending_revision) == {
        "silver/new.parquet": digest,
    }


def test_interleaved_pending_writes_must_both_settle(tmp_path: Path) -> None:
    first = tmp_path / "silver" / "first.parquet"
    second = tmp_path / "silver" / "second.parquet"
    digest = hashlib.sha256(b"bytes").hexdigest()
    _, first_revision = versions.advance_market_data_version(
        tmp_path, changed_file=first, sha256=digest, pending=True,
    )
    _, second_revision = versions.advance_market_data_version(
        tmp_path, changed_file=second, sha256=digest, pending=True,
    )
    versions.settle_market_data_version(tmp_path, second_revision)
    assert versions.revision_is_settled(tmp_path, second_revision) is False
    versions.settle_market_data_version(tmp_path, first_revision)
    assert versions.revision_is_settled(tmp_path, second_revision) is True


def _catalog_file(root: Path, relative: str, digest: str) -> None:
    with duckdb.connect(str(root / "catalog.duckdb")) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS partitions "
                           "(file_path VARCHAR, sha256 VARCHAR, status VARCHAR)")
        connection.execute("DELETE FROM partitions WHERE file_path=?", [relative])
        connection.execute("INSERT INTO partitions VALUES (?, ?, 'COMPLETE')", [relative, digest])


def test_dead_pending_writer_recovers_only_with_catalog_and_file_agreement(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "complete"
    target, digest = _silver_file(root, "part.parquet", b"promoted")
    _catalog_file(root, "silver/part.parquet", digest)
    versions.market_data_version(root)
    _, pending = versions.advance_market_data_version(
        root, changed_file=target, sha256=digest, pending=True,
    )
    assert versions.pending_market_writes(root) == {"status": "PENDING", "count": 1}
    assert versions.recover_abandoned_pending_write(root)["status"] == "OWNER_RUNNING"
    monkeypatch.setattr(versions, "_process_is_running", lambda _pid: False)
    assert versions.recover_abandoned_pending_write(root) == {
        "status": "RECOVERED", "mode": "COMMITTED_OR_SUPERSEDED", "pendingCount": 0,
    }
    assert versions.revision_is_settled(root, pending)

    aborted = tmp_path / "aborted"
    old_file, old_digest = _silver_file(aborted, "part.parquet", b"old")
    _catalog_file(aborted, "silver/part.parquet", old_digest)
    versions.market_data_version(aborted)
    _, aborted_revision = versions.advance_market_data_version(
        aborted, changed_file=old_file, sha256=hashlib.sha256(b"new").hexdigest(), pending=True,
    )
    assert versions.recover_abandoned_pending_write(aborted)["mode"] == "ABORTED"
    assert versions.revision_is_settled(aborted, aborted_revision)


def test_crash_between_file_and_catalog_remains_pending(tmp_path: Path, monkeypatch) -> None:
    target, old_digest = _silver_file(tmp_path, "part.parquet", b"old")
    _catalog_file(tmp_path, "silver/part.parquet", old_digest)
    versions.market_data_version(tmp_path)
    _, pending = versions.advance_market_data_version(
        tmp_path, changed_file=target, sha256=hashlib.sha256(b"new").hexdigest(), pending=True,
    )
    target.write_bytes(b"new")
    monkeypatch.setattr(versions, "_process_is_running", lambda _pid: False)
    assert versions.recover_abandoned_pending_write(tmp_path) == {
        "status": "REVIEW_REQUIRED", "reason": "CATALOG_FILE_MISMATCH",
    }
    assert not versions.revision_is_settled(tmp_path, pending)


def test_later_verified_write_can_supersede_one_abandoned_pending_file(
    tmp_path: Path, monkeypatch,
) -> None:
    target, old_digest = _silver_file(tmp_path, "part.parquet", b"old")
    _catalog_file(tmp_path, "silver/part.parquet", old_digest)
    versions.market_data_version(tmp_path)
    _, abandoned = versions.advance_market_data_version(
        tmp_path, changed_file=target, sha256=hashlib.sha256(b"interrupted").hexdigest(), pending=True,
    )
    target.write_bytes(b"recovered by a later source run")
    current_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    _catalog_file(tmp_path, "silver/part.parquet", current_sha)
    _, current = versions.advance_market_data_version(
        tmp_path, changed_file=target, sha256=current_sha,
    )
    monkeypatch.setattr(versions, "_process_is_running", lambda _pid: False)
    assert versions.recover_abandoned_pending_write(tmp_path)["status"] == "RECOVERED"
    assert versions.revision_is_settled(tmp_path, current)
    assert abandoned != current


def test_corrupt_revision_state_is_reported_not_treated_as_clear(tmp_path: Path) -> None:
    state = tmp_path / "state" / "market_data_version.json"
    state.parent.mkdir(parents=True)
    state.write_text("{invalid", encoding="utf-8")
    assert versions.pending_market_writes(tmp_path) == {"status": "INVALID", "count": None}
    assert versions.recover_abandoned_pending_write(tmp_path) == {
        "status": "REVIEW_REQUIRED", "reason": "INVALID_REVISION_STATE",
    }
    assert versions.market_data_version(tmp_path) == "invalid-silver-revision-state"
    assert versions.market_unknown_epoch(tmp_path) is None
    assert not versions.revision_is_settled(tmp_path, "invalid-silver-revision-state")
    with pytest.raises(RuntimeError, match="damaged"):
        versions.advance_market_data_version(tmp_path)
    assert state.read_text(encoding="utf-8") == "{invalid"


def test_unknown_epoch_is_monotonic_and_legacy_adoption_requires_full_build(tmp_path: Path) -> None:
    file, digest = _silver_file(tmp_path, "part.parquet", b"source")
    base = versions.market_data_version(tmp_path)
    initial_epoch = versions.market_unknown_epoch(tmp_path)
    assert isinstance(initial_epoch, int) and initial_epoch >= 1 << 61
    versions.advance_market_data_version(tmp_path, changed_file=file, sha256=digest)
    assert versions.market_unknown_epoch(tmp_path) == initial_epoch
    versions.advance_market_data_version(tmp_path)
    assert versions.market_unknown_epoch(tmp_path) == initial_epoch + 1
    versions.advance_market_data_version(tmp_path, changed_file=file, sha256=digest)
    assert versions.market_unknown_epoch(tmp_path) == initial_epoch + 1
    assert versions.changes_between(tmp_path, base, versions.market_data_version(tmp_path)) is None

    path = tmp_path / "state" / "market_data_version.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state.pop("unknownEpoch")
    path.write_text(json.dumps(state), encoding="utf-8")
    assert versions.market_unknown_epoch(tmp_path) is None
    adopted_epoch = versions.market_unknown_epoch(tmp_path, initialize=True)
    assert isinstance(adopted_epoch, int) and adopted_epoch >= 1 << 61
    assert adopted_epoch != initial_epoch
    assert versions.market_unknown_epoch(tmp_path) == adopted_epoch


def test_invalid_unknown_epoch_fails_closed(tmp_path: Path) -> None:
    versions.market_data_version(tmp_path)
    path = tmp_path / "state" / "market_data_version.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["unknownEpoch"] = -1
    path.write_text(json.dumps(state), encoding="utf-8")
    assert versions.market_unknown_epoch(tmp_path) is None
    assert versions.market_unknown_epoch(tmp_path, initialize=True) is None
    assert versions.revision_is_settled(tmp_path, state["revision"]) is False
    assert versions.pending_market_writes(tmp_path)["status"] == "INVALID"


def test_relative_data_root_accepts_both_relative_file_spellings(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    root = Path("synthetic")
    file, sha = _silver_file(root, "exchange=DCE/a.parquet", b"a")
    base = versions.market_data_version(root)
    _, middle = versions.advance_market_data_version(root, changed_file=file, sha256=sha)
    assert versions.changes_between(root, base, middle) == {
        "silver/exchange=DCE/a.parquet": sha,
    }

    file.write_bytes(b"new")
    next_sha = hashlib.sha256(b"new").hexdigest()
    _, current = versions.advance_market_data_version(
        root, changed_file=file.relative_to(root), sha256=next_sha,
    )
    assert versions.changes_between(root, middle, current) == {
        "silver/exchange=DCE/a.parquet": next_sha,
    }


def test_truncation_and_tampering_fail_closed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(versions, "_MAX_TRANSITIONS", 2)
    file, sha = _silver_file(tmp_path, "x.parquet", b"x")
    base = versions.market_data_version(tmp_path)
    _, first = versions.advance_market_data_version(tmp_path, changed_file=file, sha256=sha)
    _, second = versions.advance_market_data_version(tmp_path, changed_file=file, sha256=sha)
    _, third = versions.advance_market_data_version(tmp_path, changed_file=file, sha256=sha)
    assert versions.changes_between(tmp_path, base, third) is None
    assert versions.changes_between(tmp_path, first, third) == {"silver/x.parquet": sha}

    state = tmp_path / "state" / "market_data_version.json"
    payload = json.loads(state.read_text(encoding="utf-8"))
    payload["journal"][0]["path"] = "silver/../escape.parquet"
    state.write_text(json.dumps(payload), encoding="utf-8")
    assert versions.changes_between(tmp_path, first, third) is None
    assert versions.market_data_version(tmp_path) == "invalid-silver-revision-state"
    with pytest.raises(RuntimeError, match="damaged"):
        versions.advance_market_data_version(tmp_path, changed_file=file, sha256=sha)


def test_parallel_processes_do_not_lose_revision_entries(tmp_path: Path) -> None:
    base = versions.market_data_version(tmp_path)
    files = [_silver_file(tmp_path, f"part-{index}.parquet", bytes([index])) for index in range(4)]
    src = Path(__file__).resolve().parents[1] / "src"
    code = (
        "import sys; from pathlib import Path; "
        "sys.path.insert(0, sys.argv[1]); "
        "from market_monitor.market_data_version import advance_market_data_version; "
        "root=Path(sys.argv[2]); file=Path(sys.argv[3]); sha=sys.argv[4]; "
        "[advance_market_data_version(root, changed_file=file, sha256=sha) for _ in range(4)]"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(src), str(tmp_path), str(file), sha],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for file, sha in files
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, f"{stdout}\n{stderr}"
    current = versions.market_data_version(tmp_path)
    assert versions.changes_between(tmp_path, base, current) == {
        f"silver/part-{index}.parquet": sha for index, (_, sha) in enumerate(files)
    }
    payload = json.loads((tmp_path / "state" / "market_data_version.json").read_text(encoding="utf-8"))
    assert len(payload["journal"]) == 16
    assert len({entry["current"] for entry in payload["journal"]}) == 16
