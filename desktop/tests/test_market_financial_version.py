from __future__ import annotations

import json
from pathlib import Path

import duckdb

from market_monitor import market_official_futures_metrics as metrics


def test_unpublished_version_is_read_only_and_never_scans_sources(tmp_path: Path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("reader must not open Catalog")

    monkeypatch.setattr(metrics.duckdb, "connect", forbidden)
    version = metrics.financial_metrics_version(tmp_path)
    assert version.startswith("unpublished-")
    assert metrics.financial_metrics_version(tmp_path) == version
    assert not list(tmp_path.iterdir())


def test_reader_reuses_published_evidence_without_catalog_queries(tmp_path: Path, monkeypatch):
    version = metrics.refresh_financial_metrics_version(tmp_path)
    monkeypatch.setattr(metrics.duckdb, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Catalog read")))
    for _ in range(4):
        assert metrics.financial_metrics_version(tmp_path) == version


def test_config_calendar_revision_and_quality_versions_invalidate(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    calendar = state / "cn_futures_trading_calendar.json"
    calendar.write_text('{"tradingDays": ["2026-09-30"]}', encoding="utf-8")
    first = metrics.refresh_financial_metrics_version(tmp_path,
        versions={"queryRevision": "r1", "sourcePolicyVersion": "policy-1", "verificationVersion": "checked-1"})
    assert metrics.financial_metrics_version(tmp_path) == first
    revision = state / "market_data_version.json"
    revision.write_text('{"revision": "r2"}', encoding="utf-8")
    assert metrics.financial_metrics_version(tmp_path).startswith("unpublished-")
    second = metrics.refresh_financial_metrics_version(tmp_path,
        versions={"queryRevision": "r2", "sourcePolicyVersion": "policy-1", "verificationVersion": "checked-1"})
    assert second != first
    third = metrics.refresh_financial_metrics_version(tmp_path,
        versions={"queryRevision": "r2", "sourcePolicyVersion": "policy-1", "verificationVersion": "checked-2"})
    assert third != second
    calendar.write_text('{"tradingDays": ["2026-09-30", "2026-10-08"]}', encoding="utf-8")
    assert metrics.financial_metrics_version(tmp_path).startswith("unpublished-")


def test_unrelated_catalog_ddl_and_run_updates_do_not_invalidate(tmp_path: Path):
    with duckdb.connect(str(tmp_path / "catalog.duckdb")) as connection:
        connection.execute("CREATE TABLE runs(id VARCHAR, status VARCHAR)")
        connection.execute("INSERT INTO runs VALUES ('fixture', 'RUNNING')")
    first = metrics.refresh_financial_metrics_version(tmp_path)
    with duckdb.connect(str(tmp_path / "catalog.duckdb")) as connection:
        connection.execute("UPDATE runs SET status='PASS'")
        connection.execute("CREATE TABLE unrelated_ddl(value INTEGER)")
    assert metrics.financial_metrics_version(tmp_path) == first
    assert metrics.refresh_financial_metrics_version(tmp_path) == first


def test_writer_evidence_sha_parser_count_and_effective_intervals_change_version(tmp_path: Path):
    with duckdb.connect(str(tmp_path / "catalog.duckdb")) as connection:
        connection.execute("CREATE TABLE futures_contract_daily_official_coverage("
            "trade_date VARCHAR, exchange VARCHAR, payload_sha256 VARCHAR, status VARCHAR, "
            "parser_version VARCHAR, usable_count INTEGER, record_count INTEGER)")
        connection.execute("INSERT INTO futures_contract_daily_official_coverage VALUES "
            "('2026-09-30', 'DCE', ?, 'PASS', 'fixture-v1', 250, 250)", ["a" * 64])
        connection.execute("CREATE TABLE futures_settlement_parameter_official("
            "contract_code VARCHAR,exchange VARCHAR,trade_date VARCHAR,effective_from VARCHAR, "
            "effective_to VARCHAR,interval_payload_sha256 VARCHAR)")
        connection.execute("INSERT INTO futures_settlement_parameter_official VALUES "
            "('IF2612', 'CFFEX', '2026-09-30','2026-10-08','2026-10-09',?)", ["b" * 64])
    first = metrics.refresh_financial_metrics_version(tmp_path)
    with duckdb.connect(str(tmp_path / "catalog.duckdb")) as connection:
        connection.execute("UPDATE futures_contract_daily_official_coverage SET parser_version='fixture-v2'")
        second = metrics.refresh_financial_metrics_version(tmp_path, connection)
        assert second != first
        connection.execute("UPDATE futures_settlement_parameter_official SET effective_to='2026-10-10'")
        third = metrics.refresh_financial_metrics_version(tmp_path, connection)
        assert third != second
    assert metrics.financial_metrics_version(tmp_path) == third


def test_corrupt_version_pointer_is_unpublished_not_cached_success(tmp_path: Path):
    metrics.refresh_financial_metrics_version(tmp_path)
    path = tmp_path / "state" / "market_financial_version.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schemaVersion"] = 999
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert metrics.financial_metrics_version(tmp_path).startswith("unpublished-")
