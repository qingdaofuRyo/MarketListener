"""Local data-source inventory and routing-preference API.

The router deliberately distinguishes three facts: providers implemented in
this repository, rows currently present in local Silver storage, and routing
preferences chosen by the local administrator.  A preference never claims an
unconfigured commercial provider is usable.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from market_monitor.dataset_catalog import BAR_FIELDS
from market_monitor.market_query_cache import get_kline_query_store

from .common import clean, load_json, now_iso, save_json

router = APIRouter(prefix="/api/data-sources", tags=["data-sources"])


# These are repository facts, not a catalogue of vendors the product might
# support in the future. Endpoint text comes from the adapters/collector.
_PROVIDERS: tuple[dict[str, Any], ...] = (
    {
        "providerId": "tdx_local",
        "name": "通达信本地行情缓存",
        "type": "local_file_adapter",
        "access": "vipdoc/sh|sz|ds/lday *.day; vipdoc/sh|sz|ds/fzline *.lc5",
        "endpoint": "TDX_ROOT 或本机通达信安装目录",
        "authentication": "none; requires the desktop client to download data",
        "implemented": True,
        "configured": True,
        "priority": 5,
        "enabled": True,
        "markets": ["CN", "HK"],
        "assetTypes": ["STOCK", "B_SHARE", "ETF", "LOF", "REIT", "INDEX", "CONVERTIBLE_BOND", "EXCHANGEABLE_BOND", "PLEDGED_REPO", "REPO"],
        "periods": ["5m", "15m", "30m", "1h", "2h", "1d", "1w", "1mo", "1q", "1y"],
        "fields": ["open", "high", "low", "close", "volume", "amount"],
        "fieldNotes": "tdx-cn-v2 保留原始值、价格除数、成交量倍率与单位；未通过质量门的数据进入隔离区。",
        "status": "IMPLEMENTED_LOCAL_VALIDATED",
    },
    {
        "providerId": "tdx_futures_local",
        "name": "通达信期货通本地缓存",
        "type": "local_file_adapter",
        "access": "vipdoc/ds/lday *.day; vipdoc/ds/fzline *.lc5",
        "endpoint": "TDX_FUTURES_ROOT 或 C:\\new_tdxqh",
        "authentication": "none; requires the desktop client to download data",
        "implemented": True,
        "configured": True,
        "priority": 5,
        "enabled": True,
        "markets": ["CN"],
        "assetTypes": ["FUTURE", "INDEX"],
        "periods": ["5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w", "1mo", "1q", "1y"],
        "fields": ["open", "high", "low", "close", "volume", "open_interest", "settlement"],
        "fieldNotes": "持仓量仅用于期货与商品指数；日线含结算价，5 分钟原始文件不含成交额和结算价。",
        "status": "IMPLEMENTED_LOCAL",
    },
    {
        "providerId": "pytdx",
        "name": "通达信 pytdx",
        "type": "protocol_adapter",
        "access": "TDX TCP quote protocol",
        "endpoint": "TDX_SERVERS (default public hosts, TCP/7709)",
        "authentication": "none",
        "implemented": True,
        "configured": True,
        "priority": 10,
        "enabled": True,
        "markets": ["CN"],
        "assetTypes": ["STOCK", "ETF", "INDEX"],
        "periods": ["1m", "5m", "15m", "30m", "1h", "1d", "1w", "1mo"],
        "fields": ["open", "high", "low", "close", "volume", "amount"],
        "fieldNotes": "当前接入范围为股票、ETF、指数；不将 position 作为这些类别的持仓量字段。",
        "status": "IMPLEMENTED_UNVERIFIED",
    },
    {
        "providerId": "akshare",
        "name": "AKShare",
        "type": "sdk_adapter",
        "access": "Python AKShare SDK; upstream varies by function",
        "endpoint": "AKShare functions (stock_hk_hist, futures_main_sina, futures_index_ccidx and others)",
        "authentication": "none for current collector calls",
        "implemented": True,
        "configured": True,
        "priority": 20,
        "enabled": True,
        "markets": ["CN", "HK", "GLOBAL"],
        "assetTypes": ["STOCK", "INDEX", "FUTURE", "MACRO"],
        "periods": ["1d"],
        "fields": ["open", "high", "low", "close", "volume", "amount", "open_interest"],
        "fieldNotes": "持仓量只在国内期货与商品指数接口写入；股票、ETF 和普通指数为空。",
        "status": "IMPLEMENTED_UNVERIFIED",
    },
    {
        "providerId": "baostock",
        "name": "Baostock",
        "type": "sdk_adapter",
        "access": "Python baostock SDK",
        "endpoint": "baostock.login / query_history_k_data_plus",
        "authentication": "account-free SDK login",
        "implemented": True,
        "configured": True,
        "priority": 30,
        "enabled": True,
        "markets": ["CN"],
        "assetTypes": ["STOCK"],
        "periods": ["1d", "30m"],
        "fields": ["open", "high", "low", "close", "volume", "amount"],
        "fieldNotes": "标准历史行情接口不返回期货持仓量。",
        "status": "IMPLEMENTED_UNVERIFIED",
    },
    {
        "providerId": "joinquant",
        "name": "JQData / 聚宽",
        "type": "sdk_adapter",
        "access": "jqdatasdk",
        "endpoint": "jqdatasdk.auth and price APIs",
        "authentication": "JQDATA_USERNAME + JQDATA_PASSWORD required",
        "implemented": True,
        "configured": False,
        "priority": 90,
        "enabled": False,
        "markets": ["CN"],
        "assetTypes": ["STOCK", "ETF", "INDEX", "FUTURE"],
        "periods": ["1m", "30m", "1d"],
        "fields": ["open", "high", "low", "close", "volume", "amount"],
        "fieldNotes": "JQData 原始字段 money 统一标准化为成交额 amount。",
        "status": "BLOCKED_CONFIGURATION",
    },
    {
        "providerId": "tushare",
        "name": "Tushare Pro",
        "type": "sdk_adapter",
        "access": "tushare.pro_api",
        "endpoint": "TUSHARE_TOKEN + Pro endpoints daily/stk_mins/stock_basic",
        "authentication": "TUSHARE_TOKEN and endpoint entitlement required",
        "implemented": True,
        "configured": False,
        "priority": 90,
        "enabled": False,
        "markets": ["CN"],
        "assetTypes": ["STOCK", "ETF", "INDEX"],
        "periods": ["1m", "1d"],
        "fields": ["open", "high", "low", "close", "volume", "amount"],
        "fieldNotes": "Tushare 原始字段 vol 统一标准化为成交量 volume。",
        "status": "BLOCKED_CONFIGURATION",
    },
    {
        "providerId": "eastmoney_cboe",
        "name": "东方财富 / CBOE",
        "type": "http_adapter",
        "access": "HTTPS JSON and CSV with Tencent fallback",
        "endpoint": "https://push2his.eastmoney.com/api/qt/stock/kline/get; https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
        "authentication": "none for current public endpoints",
        "implemented": True,
        "configured": True,
        "priority": 20,
        "enabled": True,
        "markets": ["GLOBAL"],
        "assetTypes": ["MACRO", "INDEX"],
        "periods": ["1d"],
        "fields": ["date", "close"],
        "fieldNotes": "当前实现的全球指数/宏观接口仅保证日期和收盘价。",
        "status": "IMPLEMENTED_UNVERIFIED",
    },
)

_PROVIDERS_BY_ID = {str(provider["providerId"]): provider for provider in _PROVIDERS}


class RoutingPreference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary: str | None = Field(default=None, max_length=120)
    fallback1: str | None = Field(default=None, max_length=120)
    fallback2: str | None = Field(default=None, max_length=120)


class RoutingPreferencesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preferences: dict[str, RoutingPreference]


def _data_root(request: Request) -> Path:
    return Path(request.app.state.data_root)


def _preference_path(data_root: Path) -> Path:
    return data_root / "data_source_preferences.json"


def _category_key(market: str, asset_type: str, period: str) -> str:
    return f"{market}:{asset_type}:{period}"


def _provider_for_source(source: str) -> str:
    value = source.casefold()
    if value.startswith("tdx_futures"):
        return "tdx_futures_local"
    if (
        value.startswith("tdx_local")
        or value.startswith("tdx-local")
        or (source.startswith("通达信") and "本地" in source)
    ):
        return "tdx_local"
    if "期货通" in source:
        return "tdx_futures_local"
    if value.startswith("pytdx"):
        return "pytdx"
    if value.startswith("akshare") or value.startswith("sina-"):
        return "akshare"
    if "eastmoney" in value or "cboe" in value or value.startswith("tencent"):
        return "eastmoney_cboe"
    if value.startswith("baostock"):
        return "baostock"
    if value.startswith("joinquant") or value.startswith("jqdata"):
        return "joinquant"
    if value.startswith("tushare"):
        return "tushare"
    return source or "unknown"


def _source_details(source_ids: set[str]) -> list[dict[str, Any]]:
    """Expose the exact registered access path behind each stored source id."""
    details: list[dict[str, Any]] = []
    for provider_id in sorted(source_ids):
        provider = _PROVIDERS_BY_ID.get(provider_id)
        if provider is None:
            details.append(
                {
                    "providerId": provider_id,
                    "name": provider_id,
                    "endpoint": None,
                    "status": "UNREGISTERED_SOURCE",
                    "periods": [],
                    "fields": [],
                    "fieldNotes": None,
                }
            )
            continue
        details.append(
            {
                "providerId": provider_id,
                "name": provider["name"],
                "endpoint": provider["endpoint"],
                "status": provider["status"],
                "periods": provider["periods"],
                "fields": provider["fields"],
                "fieldNotes": provider.get("fieldNotes"),
            }
        )
    return details


_COVERAGE_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "open_interest",
    "settlement",
    "pct_change",
    "amplitude",
)
_FIELD_TYPES = {
    "schema_version": "INTEGER",
    "instrument_id": "VARCHAR",
    "instrument_key": "OBJECT",
    "symbol": "VARCHAR",
    "name": "VARCHAR",
    "market": "VARCHAR",
    "asset_type": "VARCHAR",
    "trading_date": "DATE",
    "trading_day": "DATE",
    "bar_start": "TIMESTAMP_TZ",
    "bar_end": "TIMESTAMP_TZ",
    "bar_open_time": "TIMESTAMP_TZ",
    "bar_close_time": "TIMESTAMP_TZ",
    "period": "VARCHAR",
    "open": "DOUBLE",
    "high": "DOUBLE",
    "low": "DOUBLE",
    "close": "DOUBLE",
    "settlement": "DOUBLE?",
    "pct_change": "DOUBLE?",
    "amplitude": "DOUBLE?",
    "volume": "DOUBLE",
    "amount": "DOUBLE?",
    "open_interest": "DOUBLE?",
    "currency": "VARCHAR?",
    "adjustment": "VARCHAR?",
    "price_mode": "VARCHAR",
    "source": "VARCHAR/OBJECT",
    "source_period": "VARCHAR",
    "fetched_at": "TIMESTAMP_TZ",
    "data_version": "VARCHAR",
    "quality_status": "VARCHAR",
}


def _inferred_type(field: str) -> str:
    if field in _FIELD_TYPES:
        return _FIELD_TYPES[field]
    if field.endswith(("_at", "_time", "timestamp")):
        return "TIMESTAMP/VARCHAR"
    if field.endswith(("_date", "_day")):
        return "DATE/VARCHAR"
    if field.endswith(("_id", "_name", "_code", "_status")):
        return "VARCHAR"
    return "未声明"


def _field_specs(fields: list[str] | tuple[str, ...], *, json_storage: bool = False) -> list[dict[str, Any]]:
    return [
        {
            "name": field,
            "type": _inferred_type(field),
            "nullable": _inferred_type(field).endswith("?"),
            "storage": f"bar_json.$.{field}" if json_storage else field,
        }
        for field in fields
    ]


def _provider_documents() -> list[dict[str, Any]]:
    return [
        {**provider, "fieldSchema": _field_specs(tuple(str(field) for field in provider["fields"]))}
        for provider in _PROVIDERS
    ]


def _manifest_connection(data_root: Path) -> duckdb.DuckDBPyConnection | None:
    """Open the compact K-line manifest, never the full Silver row store."""

    store = get_kline_query_store(data_root)
    try:
        store.ensure_ready()
    except Exception:
        # A large first-time build intentionally runs in the background.  The
        # data-source page must return an empty/not-ready inventory immediately.
        return None
    if not store.path.is_file():
        return None
    try:
        connection = duckdb.connect(str(store.path), read_only=True)
        version = connection.execute("SELECT schema_version FROM cache_meta LIMIT 1").fetchone()
        if not version or int(version[0]) < 2:
            connection.close()
            return None
        return connection
    except Exception:
        return None


def _latest_group_facts(connection: duckdb.DuckDBPyConnection) -> dict[tuple[str, str], dict[str, Any]]:
    field_sql = ", ".join(
        "sum(CASE WHEN json_extract(bar_json, '$.%s') IS NOT NULL "
        "AND json_extract(bar_json, '$.%s') != 'null' THEN 1 ELSE 0 END)" % (field, field)
        for field in _COVERAGE_FIELDS
    )
    rows = connection.execute(
        "SELECT market, asset_type, count(*)::BIGINT, "
        "max(json_extract_string(bar_json, '$.fetched_at')), "
        "list(DISTINCT coalesce(nullif(json_extract_string(bar_json, '$.actual_source'), ''), "
        "nullif(json_extract_string(bar_json, '$.source'), ''), 'unknown')), "
        "histogram(coalesce(nullif(json_extract_string(bar_json, '$.quality_status'), ''), 'UNKNOWN')), "
        f"{field_sql} FROM instrument_latest GROUP BY market, asset_type"
    ).fetchall()
    facts: dict[tuple[str, str], dict[str, Any]] = {}
    for market, asset_type, samples, updated_at, sources, quality, *counts in rows:
        sample_count = max(1, int(samples or 0))
        facts[(str(market), str(asset_type))] = {
            "samples": sample_count,
            "updatedAt": str(updated_at) if updated_at else None,
            "sources": [str(value) for value in (sources or []) if value],
            "quality": {str(key): int(value) for key, value in dict(quality or {}).items()},
            "fieldCompleteness": {
                field: round(int(value or 0) / sample_count, 4)
                for field, value in zip(_COVERAGE_FIELDS, counts)
            },
        }
    return facts


def _local_inventory(data_root: Path) -> tuple[list[dict[str, Any]], int]:
    """Return exact category counts from the compact K-line file manifest.

    Row, instrument and partition counts are summed from producer-maintained
    metadata.  Only one latest row per instrument is inspected for source and
    optional-field hints; the hundreds of millions of Silver rows are never
    opened by this endpoint.
    """

    connection = _manifest_connection(data_root)
    if connection is None:
        return [], 0
    try:
        rows = connection.execute(
            "SELECT market, asset_type, period, sum(row_count)::BIGINT, "
            "count(DISTINCT instrument_id)::BIGINT, count(DISTINCT file_path)::BIGINT, "
            "min(earliest_bar_at), max(latest_bar_at) "
            "FROM instrument_file GROUP BY market, asset_type, period ORDER BY 1, 2, 3"
        ).fetchall()
        unique_instruments = int(
            connection.execute("SELECT count(DISTINCT instrument_id) FROM instrument_period").fetchone()[0] or 0
        )
        latest = _latest_group_facts(connection)
    finally:
        connection.close()
    result: list[dict[str, Any]] = []
    for market, asset_type, period, total, instruments, partitions, earliest, latest_at in rows:
        key = (str(market), str(asset_type))
        facts = latest.get(
            key,
            {
                "samples": 0,
                "updatedAt": None,
                "sources": [],
                "quality": {},
                "fieldCompleteness": {field: 0.0 for field in _COVERAGE_FIELDS},
            },
        )
        source_ids = {_provider_for_source(value) for value in facts["sources"]}
        result.append(
            {
                "categoryKey": _category_key(*key, str(period)),
                "market": key[0],
                "assetType": key[1],
                "period": str(period),
                "instruments": int(instruments),
                "rows": int(total),
                "partitions": int(partitions),
                "earliestBarAt": str(earliest) if earliest else None,
                "latestBarAt": str(latest_at) if latest_at else None,
                "lastUpdatedAt": facts["updatedAt"],
                "sources": sorted(source_ids),
                "sourceDetails": _source_details(source_ids),
                "quality": dict(sorted(facts["quality"].items())),
                "fieldCompleteness": facts["fieldCompleteness"],
                "fieldCoverageSamples": facts["samples"],
                "rowCountMode": "MANIFEST_EXACT",
                "fieldCoverageMode": "LATEST_INSTRUMENT_SAMPLE",
            }
        )
    return result, unique_instruments


def _catalog_tables(data_root: Path) -> list[dict[str, Any]]:
    """Describe local DuckDB tables using catalog statistics only."""

    path = data_root / "catalog.duckdb"
    if not path.is_file():
        return []
    try:
        connection = duckdb.connect(str(path), read_only=True)
    except Exception:
        return []
    try:
        table_rows = connection.execute(
            "SELECT table_name, estimated_size, column_count FROM duckdb_tables() "
            "WHERE database_name = current_database() AND schema_name = 'main' ORDER BY table_name"
        ).fetchall()
        columns = connection.execute(
            "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position"
        ).fetchall()
    finally:
        connection.close()
    by_table: dict[str, list[dict[str, Any]]] = {}
    for table, name, data_type, nullable in columns:
        by_table.setdefault(str(table), []).append(
            {"name": str(name), "type": str(data_type), "nullable": nullable == "YES", "storage": str(name)}
        )
    updated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
    return [
        {
            "tableId": f"catalog.{name}",
            "name": str(name),
            "kind": "DUCKDB_TABLE",
            "storage": "catalog.duckdb / main",
            "dataSources": ["本地 DuckDB"],
            "rows": int(estimated or 0),
            "rowCountMode": "CATALOG_ESTIMATE",
            "partitions": 1,
            "updatedAt": updated_at,
            "fields": by_table.get(str(name), []),
            "columnCount": int(column_count or 0),
        }
        for name, estimated, column_count in table_rows
    ]


def _silver_tables(inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    physical = ("instrument_id", "market", "asset_type", "period", "bar_open_time")
    logical = tuple(field for field in dict.fromkeys((*BAR_FIELDS, "settlement")) if field not in physical)
    fields = _field_specs(physical) + _field_specs(logical, json_storage=True)
    return [
        {
            "tableId": f"silver.{item['categoryKey']}",
            "name": f"{item['market']}_{item['assetType']}_{item['period']}",
            "kind": "PARQUET_DATASET",
            "storage": (
                f"silver/market={item['market']}/asset_type={item['assetType']}/"
                f"period={item['period']}/year=*"
            ),
            "dataSources": item["sources"],
            "rows": item["rows"],
            "rowCountMode": item["rowCountMode"],
            "partitions": item["partitions"],
            "updatedAt": item["lastUpdatedAt"] or item["latestBarAt"],
            "fields": fields,
            "columnCount": len(fields),
        }
        for item in inventory
    ]


def _registered_datasets(data_root: Path, inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    path = data_root / "catalog.duckdb"
    if not path.is_file():
        return []
    try:
        connection = duckdb.connect(str(path), read_only=True)
    except Exception:
        return []
    try:
        tables = {str(row[0]) for row in connection.execute("SHOW TABLES").fetchall()}
        if "datasets" not in tables:
            return []
        rows = connection.execute(
            "SELECT dataset_id, dataset_json, registered_at FROM datasets ORDER BY dataset_id"
        ).fetchall()
    finally:
        connection.close()
    output: list[dict[str, Any]] = []
    for dataset_id, raw, registered_at in rows:
        try:
            document = json.loads(str(raw))
        except (TypeError, ValueError):
            continue
        market_values = {value for value in str(document.get("market") or "").split("/") if value}
        asset_type = str(document.get("asset_type") or "")
        matching = [
            item for item in inventory
            if item["market"] in market_values and item["assetType"] == asset_type
        ]
        is_bar = str(dataset_id).endswith("_BAR")
        output.append(
            {
                "datasetId": str(dataset_id),
                "name": str(document.get("dataset_name") or dataset_id),
                "market": str(document.get("market") or ""),
                "assetType": asset_type,
                "frequency": str(document.get("frequency") or ""),
                "source": str(document.get("source") or ""),
                "rows": sum(int(item["rows"]) for item in matching) if is_bar else None,
                "rowCountMode": "MANIFEST_EXACT" if is_bar else "NOT_MAPPED_TO_PHYSICAL_TABLE",
                "partitions": sum(int(item["partitions"]) for item in matching) if is_bar else None,
                "registeredAt": str(registered_at) if registered_at else None,
                "primaryKey": [str(value) for value in document.get("primary_key", [])],
                "fields": _field_specs(tuple(str(value) for value in document.get("fields", []))),
                "description": str(document.get("description") or ""),
            }
        )
    return output


def local_inventory(data_root: Path) -> list[dict[str, Any]]:
    """Return only category-level facts for callers that do not need totals."""
    inventory, _ = _local_inventory(data_root)
    return inventory


def _inventory_payload(request: Request) -> dict[str, Any]:
    root = _data_root(request)
    preferences = load_json(_preference_path(root), {"preferences": {}})
    stored = preferences.get("preferences", {}) if isinstance(preferences, dict) else {}
    inventory, unique_instruments = _local_inventory(root)
    datasets = _registered_datasets(root, inventory)
    tables = [*_silver_tables(inventory), *_catalog_tables(root)]
    return clean(
        {
            "generatedAt": now_iso(),
            "inventory": inventory,
            "tables": tables,
            "datasets": datasets,
            "preferences": stored,
            "metadata": {
                "mode": "LIGHTWEIGHT_MANIFEST",
                "rowCounts": "K 线为清单精确值；DuckDB 表为目录估算值",
                "fieldCoverage": "可选字段完整度仅抽样每个标的最新记录",
                "scansSilverRows": False,
            },
            "summary": {
                "categories": len(inventory),
                "rows": sum(int(item["rows"]) for item in inventory),
                "instruments": unique_instruments,
                "tables": len(tables),
                "datasets": len(datasets),
            },
        }
    )


@router.get("/providers")
def providers() -> dict[str, Any]:
    """Return code-registered providers without touching local storage."""

    return clean({"generatedAt": now_iso(), "items": _provider_documents()})


@router.get("/inventory")
def inventory(request: Request) -> dict[str, Any]:
    return _inventory_payload(request)


@router.get("/tdx-local-normalization")
def tdx_local_normalization(request: Request) -> dict[str, Any]:
    """Expose the latest local normalization audit without scanning Silver."""

    path = _data_root(request) / "reports" / "tdx-local" / "latest-audit.json"
    document = load_json(path, {})
    if not isinstance(document, dict) or not document:
        return clean({"available": False, "normalizationVersion": "tdx-cn-v2", "reason": "尚未运行通达信标准化审计"})
    return clean({
        "available": True,
        "normalizationVersion": document.get("标准化版本"),
        "status": document.get("状态"),
        "generatedAt": document.get("生成时间"),
        "scannedFiles": document.get("扫描文件", 0),
        "importedFiles": document.get("导入文件", 0),
        "writtenBars": document.get("写入K线", 0),
        "quarantinedFiles": document.get("隔离文件", 0),
        "quarantinedBars": document.get("隔离K线", 0),
        "assetFiles": document.get("资产文件统计", {}),
        "volumeMultipliers": document.get("成交量倍率统计", {}),
        "reportPath": str(path),
    })


@router.get("")
def data_sources(request: Request) -> dict[str, Any]:
    """Compatibility aggregate; both halves are independently available."""

    payload = _inventory_payload(request)
    payload["providers"] = _provider_documents()
    return payload


@router.put("")
def save_routing_preferences(request: Request, body: RoutingPreferencesRequest) -> dict[str, Any]:
    root = _data_root(request)
    known = {item["providerId"] for item in _PROVIDERS}
    preferences: dict[str, dict[str, str | None]] = {}
    for category, value in body.preferences.items():
        if not category or len(category) > 200:
            raise HTTPException(status_code=400, detail="invalid category key")
        current = value.model_dump()
        # Custom text is accepted and explicitly labelled by the UI/API; known
        # identifiers are never promoted to configured/usable here.
        for provider_id in current.values():
            if provider_id and provider_id in known:
                continue
        preferences[category] = current
    payload = {"updatedAt": now_iso(), "preferences": preferences}
    save_json(_preference_path(root), payload)
    return clean(payload)


__all__ = ("local_inventory", "router")
