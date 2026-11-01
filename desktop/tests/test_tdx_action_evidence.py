from pathlib import Path

from market_monitor import tdx_action_evidence as evidence


def test_missing_or_malformed_file_is_unavailable_and_never_proves_no_action(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(evidence, "resolve_tdx_root", lambda: tmp_path)
    missing = evidence.load_action_snapshot()
    assert missing.status == "UNAVAILABLE"
    assert evidence.action_snapshot_is_current(missing)
    path = tmp_path / "T0002" / "hq_cache" / "gbbq"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\x01\x00\x00\x00")
    assert not evidence.action_snapshot_is_current(missing)
    malformed = evidence.load_action_snapshot()
    assert malformed.status == "UNAVAILABLE"
    assert malformed.sha256 is None
    assert evidence.action_snapshot_is_current(malformed)
    path.write_bytes(b"\x02\x00\x00\x00\x00")
    assert not evidence.action_snapshot_is_current(malformed)
