from market_monitor.market_package import build_market_package
from market_monitor.quality import QualityReport
from market_monitor.security_audit import (
    backup_store,
    rotate_signing_key,
    scan_for_credentials,
    verify_old_package_with_rotated_keys,
)
from market_monitor.signing import generate_development_key, sign_market_package


def test_credential_scan_finds_synthetic_secrets_and_skips_excluded(tmp_path) -> None:
    (tmp_path / "config.env").write_text("JQDATA_PASSWORD=review-only-value\n", encoding="utf-8")
    (tmp_path / "keys.txt").write_text("-----BEGIN PRIVATE KEY-----\nABCD\n-----END PRIVATE KEY-----\n", encoding="utf-8")  # Synthetic test credential or marker.
    (tmp_path / "token.txt").write_text("Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9synthetic", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "secret.txt").write_text("JQDATA_PASSWORD=inside-git\n", encoding="utf-8")

    findings = scan_for_credentials(tmp_path)

    patterns = {finding.pattern for finding in findings}
    assert {"ENV_SECRET_VALUE", "PRIVATE_KEY", "BEARER_TOKEN"} <= patterns
    assert all(".git" not in finding.path for finding in findings)
    assert all("review-only-value" not in finding.snippet for finding in findings)


def test_signing_key_rotation_keeps_old_verification_and_new_key_rejects_old_package(tmp_path) -> None:
    private = tmp_path / "keys" / "private.pem"
    public = tmp_path / "keys" / "public.pem"
    backup_dir = tmp_path / "backup"
    generate_development_key(private, public)
    package = build_market_package(
        tmp_path,
        "market-001",
        [{"instrument_key": {"country_or_market": "CN", "exchange": "SSE", "asset_type": "STOCK", "code": "600519"}, "period": "1d", "bar_open_time": "2026-08-03T09:30:00+08:00"}],
        QualityReport("p", []),
        "2026-08-03T15:00:00+08:00",
        [],
    )
    sign_market_package(package, private)

    result = rotate_signing_key(private, public, backup_dir)
    matrix = verify_old_package_with_rotated_keys(package, result.backup_public_path, result.new_public_path)

    assert matrix == {"old_key_verifies": True, "new_key_rejects": True}
    assert result.backup_private_path.is_file() and result.backup_public_path.is_file()


def test_backup_drill_copies_and_verifies_hashes(tmp_path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "catalog.duckdb").write_bytes(b"binary-catalog")
    (data / "bronze").mkdir()
    (data / "bronze" / "run.json").write_text('{"raw": 1}', encoding="utf-8")

    result = backup_store(data, tmp_path / "backups")

    assert result.verified
    assert {relative for relative, _, _ in result.files} == {"bronze/run.json", "catalog.duckdb"}
    assert (result.backup_root / "bronze" / "run.json").read_text(encoding="utf-8") == '{"raw": 1}'
