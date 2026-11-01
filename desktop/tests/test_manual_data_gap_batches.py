import json
from pathlib import Path

import duckdb

from market_monitor import manual_data_gaps
from market_monitor.manual_data_gaps import event_driven_items, exchange_stat_items, legacy_pytdx_intraday_item, macro_and_hk_items


def test_event_f10_reports_only_valid_aggregate_batch_and_actual_time(tmp_path: Path):
    target = tmp_path / "f10" / "cn" / "revenue_summary.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"completed_at": "2026-10-31T10:28:51+00:00", "new_revenue": 25,
        "remaining_details": 5464, "failed_codes": 0, "total_revenue": 5564,
        "errors": [{"code": "synthetic-private"}]}), encoding="utf-8")
    item = next(row for row in event_driven_items(tmp_path) if row["key"] == "f10.revenue")
    assert item["lastRecordedBatch"] == {"completedAt": "2026-10-31T10:28:51+00:00", "updated": 25,
                                         "remaining": 5464, "failed": 0, "stored": 5564}
    assert item["startDate"] is None and item["latestDate"] is None
    assert item["updateMode"] == "AUTO_START_AND_MANUAL"
    assert "synthetic-private" not in json.dumps(item)


def test_event_f10_invalid_summary_does_not_fabricate_counts(tmp_path: Path):
    target = tmp_path / "f10" / "cn" / "revenue_summary.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"completed_at": "2026-10-31T10:28:51", "new_revenue": True,
        "remaining_details": 3, "failed_codes": 0, "total_revenue": 9}), encoding="utf-8")
    item = next(row for row in event_driven_items(tmp_path) if row["key"] == "f10.revenue")
    assert item["lastRecordedBatch"] is None


def test_official_exchange_stats_are_source_isolated_and_unknown_is_not_zero(tmp_path: Path, monkeypatch):
    (tmp_path / "catalog.duckdb").write_bytes(b"synthetic")
    connection = duckdb.connect()
    connection.execute("CREATE TABLE gold_metrics(metric_id VARCHAR, instrument_id VARCHAR, period VARCHAR, "
                       "metric_name VARCHAR, trading_date VARCHAR, timestamp VARCHAR)")
    connection.execute("INSERT INTO gold_metrics VALUES "
        "('OFFICIAL_EXCHANGE_DAILY:1','CN.SSE.MARKET.STOCK_A','1d','市值','2026-07-27','2026-10-31T10:00:00Z'),"
        "('OFFICIAL_EXCHANGE_DAILY:2','CN.SSE.MARKET.FUND','1d','成交额','2026-10-30','2026-10-31T10:00:00Z'),"
        "('OTHER:3','CN.SSE.MARKET.STOCK_A','1d','其他来源','2020-01-01','2026-10-31T10:00:00Z'),"
        "('OFFICIAL_EXCHANGE_DAILY:4','CN.SSE.MARKET.FUND','1mo','月度','2019-01','2026-10-31T10:00:00Z')")
    monkeypatch.setattr(manual_data_gaps, "connect_catalog", lambda _: connection)
    items = {item["key"]: item for item in exchange_stat_items(tmp_path)}
    assert items["exchange.sse"]["startDate"] == "2026-07-27"
    assert items["exchange.sse"]["latestDate"] == "2026-10-30"
    assert items["exchange.sse"]["observedStatistics"] == {
        "records": 2, "categories": 2, "metrics": 2, "lastWrittenAt": "2026-10-31T10:00:00Z"}
    assert items["exchange.szse"]["observedStatistics"] is None


def test_official_exchange_stats_missing_catalog_is_unknown(tmp_path: Path):
    items = exchange_stat_items(tmp_path)
    assert len(items) == 3
    assert all(item["status"] == "UNKNOWN" and item["observedStatistics"] is None for item in items)


def test_official_exchange_stats_respect_requested_dates(tmp_path: Path, monkeypatch):
    (tmp_path / "catalog.duckdb").write_bytes(b"synthetic")
    connection = duckdb.connect()
    connection.execute("CREATE TABLE gold_metrics(metric_id VARCHAR, instrument_id VARCHAR, period VARCHAR, "
                       "metric_name VARCHAR, trading_date VARCHAR, timestamp VARCHAR)")
    connection.execute("INSERT INTO gold_metrics VALUES "
        "('OFFICIAL_EXCHANGE_DAILY:1','CN.SSE.MARKET.STOCK_A','1d','市值','2026-07-27','2026-10-31T10:00:00Z'),"
        "('OFFICIAL_EXCHANGE_DAILY:2','CN.SSE.MARKET.STOCK_A','1d','市值','2026-10-30','2026-10-31T10:00:00Z')")
    monkeypatch.setattr(manual_data_gaps, "connect_catalog", lambda _: connection)
    item = exchange_stat_items(tmp_path, start="2026-10-01", end="2026-10-31")[0]
    assert item["startDate"] == item["latestDate"] == "2026-10-30"
    assert item["observedStatistics"]["records"] == 1


def test_macro_and_hk_ranges_keep_frequency_and_provider_boundaries(tmp_path: Path, monkeypatch):
    (tmp_path / "catalog.duckdb").write_bytes(b"synthetic")
    connection = duckdb.connect()
    connection.execute("CREATE TABLE gold_metrics(metric_id VARCHAR, instrument_id VARCHAR, period VARCHAR, "
                       "metric_name VARCHAR, trading_date VARCHAR, timestamp VARCHAR)")
    connection.execute("INSERT INTO gold_metrics VALUES "
        "('PMI_MANUFACTURING:1','PMI_MANUFACTURING','MONTHLY','PMI','2025-01','2026-10-31T10:00:00Z'),"
        "('PMI_MANUFACTURING:2','PMI_MANUFACTURING','MONTHLY','PMI','2026-10','2026-10-31T10:00:00Z'),"
        "('OTHER:3','PMI_MANUFACTURING','MONTHLY','其他','2020-01','2026-10-31T10:00:00Z'),"
        "('HKEX_MARKET_CAP:4','HK.HKEX.EQUITY.ALL','1d','市值','2026-09-30','2026-10-31T10:00:00Z')")
    monkeypatch.setattr(manual_data_gaps, "connect_catalog", lambda _: connection)
    items = {item["key"]: item for item in macro_and_hk_items(tmp_path, start="2026-10-01")}
    macro = items["macro.pmi-manufacturing"]
    assert macro["observedSeries"]["records"] == 2
    assert macro["observedSeries"]["first"] == "2025-01" and macro["observedSeries"]["latest"] == "2026-10"
    assert macro["observedSeries"]["frequency"] == "MONTHLY" and macro["startDate"] is None
    assert "不随所选日期筛选" in macro["notes"]
    assert items["hkex.market-cap"]["observedSeries"]["latest"] == "2026-09-30"
    assert items["hkex.market-cap"]["actionKind"] is None


def test_macro_missing_catalog_is_unknown_not_zero(tmp_path: Path):
    items = macro_and_hk_items(tmp_path)
    assert len(items) > 20 and all(item["observedSeries"] is None for item in items)


def test_legacy_pytdx_partition_count_does_not_imply_all_rows_bad(tmp_path: Path, monkeypatch):
    (tmp_path / "catalog.duckdb").write_bytes(b"synthetic")
    connection = duckdb.connect()
    connection.execute("CREATE TABLE partitions(partition_id VARCHAR, row_count BIGINT, data_cutoff VARCHAR, status VARCHAR)")
    connection.execute("INSERT INTO partitions VALUES "
        "('CN-STOCK-30m-2026', 25, '2026-08-21T00:00:00+08:00', 'COMPLETE'),"
        "('TDX-LOCAL-CN-STOCK-5m-2026', 100, '2026-10-30', 'COMPLETE')")
    monkeypatch.setattr(manual_data_gaps, "connect_catalog", lambda _: connection)
    item = legacy_pytdx_intraday_item(tmp_path)
    assert item["qualityIssue"] == {"partitions": 1, "rows": 25, "lastCutoff": "2026-08-21T00:00:00+08:00"}
    assert item["status"] == "UNKNOWN" and "逐条核验" in item["notes"]
