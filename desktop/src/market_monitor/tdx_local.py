"""Offline importer for TongdaXin desktop securities and verified DS files.

The ordinary TongdaXin terminal and TongdaXin Futures use different binary
layouts.  Domestic futures remain owned by ``futures_bulk`` so their open
interest and settlement can never be read as stock turnover fields.  The
ordinary terminal's verified ``ds`` prefixes are parsed separately because
their daily OHLC values are float32 rather than the security ``.day`` integer
format.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
from collections import Counter
from hashlib import sha256
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

import duckdb

from .market_data_version import advance_market_data_version
from .market_classification import market_classification_spec
from .market_query_cache import rebuild_kline_query_cache
from .storage import MarketStore, PartitionKey


_CN_DAY = struct.Struct("<IiiiifII")
_HK_DAY = struct.Struct("<IfffffII")
_MINUTE = struct.Struct("<HHfffffII")
_CN_FILE = re.compile(r"^(?P<prefix>sh|sz|bj)(?P<code>\d{6})\.(?P<kind>day|lc5)$", re.IGNORECASE)
_DS_FILE = re.compile(r"^(?P<prefix>\d+)#(?P<code>[A-Za-z0-9_]+)\.(?P<kind>day|lc5)$", re.IGNORECASE)
# Kept for the raw-unclassified scanner and older callers that specifically
# need the financial-terminal Hong Kong main-board filename shape.
_HK_FILE = re.compile(r"^31#(?P<code>\d{5})\.(?P<kind>day|lc5)$", re.IGNORECASE)
_SOURCE = "通达信金融终端（本地）"
_NORMALIZATION_VERSION = "tdx-cn-v2"
_VOLUME_MULTIPLIERS = (1.0, 10.0, 100.0)
_DAILY_VWAP_TOLERANCE = 0.005
_MINUTE_VWAP_TOLERANCE = 0.02

def resolve_tdx_root(value: Path | None = None) -> Path | None:
    """Resolve an ordinary TongdaXin terminal installation, never a futures root."""

    candidates = [value] if value else []
    if environment_root := os.environ.get("TDX_ROOT"):
        candidates.append(Path(environment_root))
    candidates.append(Path(r"C:\tongdaxin"))
    for candidate in candidates:
        if candidate and (candidate / "vipdoc" / "sh").is_dir() and (candidate / "vipdoc" / "ds").is_dir():
            return candidate
    return None


def financial_ds_metadata(filename: str) -> dict[str, str] | None:
    """Classify one verified ordinary-terminal ``vipdoc/ds`` filename.

    The mapping intentionally omits FX (``10#``), macro (``38#``), legacy HK
    funds (``49#``), and unknown prefixes.  Their Bar semantic/unit contract
    has not been verified, so they remain visible to the review table instead
    of being promoted to Silver by filename alone.
    """

    match = _DS_FILE.fullmatch(filename)
    if match is None:
        return None
    prefix = match.group("prefix")
    configured = market_classification_spec().get("tdxFinancialDsPrefixes", {})
    base = configured.get(prefix) if isinstance(configured, dict) else None
    if not isinstance(base, dict):
        return None
    code = match.group("code").upper()
    if prefix in {"27", "31", "48"} and not re.fullmatch(r"\d{5}" if prefix != "27" else r"[A-Z0-9_]+", code):
        return None
    if prefix in {"62", "69", "102"} and not re.fullmatch(r"\d{6}", code):
        return None
    if prefix in {"12", "16", "17", "18"} and not re.fullmatch(r"[A-Z0-9_]+", code):
        return None
    return {
        **{str(key): str(value) for key, value in base.items()},
        "symbol": code,
        "period": "1d" if match.group("kind").lower() == "day" else "5m",
    }


def decode_minute_day(value: int) -> str:
    """Decode the packed date used by TDX .lc5 records."""

    year = value // 2048 + 2004
    remainder = value % 2048
    month, day = divmod(remainder, 100)
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        raise ValueError(f"无效分钟线日期编码: {value}")
    return f"{year:04d}-{month:02d}-{day:02d}"


def read_tdx_local_file(
    path: Path,
    *,
    hong_kong: bool = False,
    start_offset: int = 0,
    price_scale: float = 100.0,
) -> list[dict[str, Any]]:
    """Read one stable local TDX file and retain its raw fields.

    ``hong_kong`` is retained for compatibility; it selects the float32 daily
    record layout used by verified financial-terminal DS prefixes as well as
    Hong Kong files.
    """

    size_before = path.stat().st_size
    if size_before % 32:
        raise ValueError(f"文件长度不是 32 字节倍数: {path.name}")
    if start_offset < 0 or start_offset > size_before or start_offset % 32:
        raise ValueError(f"无效的增量读取偏移: {start_offset}")
    with path.open("rb") as stream:
        stream.seek(start_offset)
        payload = stream.read()
    if path.stat().st_size != size_before:
        raise RuntimeError(f"通达信正在写入，稍后重试: {path.name}")
    is_daily = path.suffix.lower() == ".day"
    records: list[dict[str, Any]] = []
    for offset in range(0, len(payload), 32):
        if is_daily:
            if hong_kong:
                raw_day, open_, high, low, close, amount, volume, _reserved = _HK_DAY.unpack_from(payload, offset)
                open_raw, high_raw, low_raw, close_raw = open_, high, low, close
            else:
                raw_day, open_raw, high_raw, low_raw, close_raw, amount, volume, _reserved = _CN_DAY.unpack_from(payload, offset)
                if price_scale <= 0:
                    raise ValueError("价格除数必须为正数")
                open_, high, low, close = (
                    open_raw / price_scale,
                    high_raw / price_scale,
                    low_raw / price_scale,
                    close_raw / price_scale,
                )
            day = _day_text(raw_day)
            records.append({
                "day": day, "time": "00:00:00", "open": open_, "high": high, "low": low, "close": close,
                "amount": amount, "volume": volume,
                "raw_open": open_raw, "raw_high": high_raw, "raw_low": low_raw, "raw_close": close_raw,
                "raw_amount": amount, "raw_volume": volume, "price_scale": 1.0 if hong_kong else price_scale,
            })
        else:
            raw_day, minutes, open_, high, low, close, amount, volume, _reserved = _MINUTE.unpack_from(payload, offset)
            if not 0 <= minutes < 24 * 60:
                raise ValueError(f"无效分钟值: {minutes}")
            records.append({
                "day": decode_minute_day(raw_day), "time": f"{minutes // 60:02d}:{minutes % 60:02d}:00",
                "open": open_, "high": high, "low": low, "close": close, "amount": amount, "volume": volume,
                "raw_open": open_, "raw_high": high, "raw_low": low, "raw_close": close,
                "raw_amount": amount, "raw_volume": volume, "price_scale": 1.0,
            })
    return records


def run_tdx_local_import(
    data_root: Path,
    *,
    tdx_root: Path | None = None,
    full_rescan: bool = False,
    batch_rows: int = 250_000,
    rebuild_cache: bool = True,
    start_date: str | None = None,
    end_date: str | None = None,
    audit_only: bool = False,
    replace_source: bool = False,
    resume_staging: Path | None = None,
) -> dict[str, Any]:
    """Audit or import TDX bars under the ``tdx-cn-v2`` quality policy."""

    data_root = Path(data_root)
    if audit_only and (replace_source or resume_staging):
        raise ValueError("--audit-only 不能与来源替换或暂存续跑同时使用")
    if replace_source and resume_staging:
        raise ValueError("--replace-source 与 --resume-staging 不能同时使用")
    if resume_staging is not None:
        return _resume_tdx_source(
            data_root,
            staging_root=Path(resume_staging),
            tdx_root=tdx_root,
            batch_rows=batch_rows,
            rebuild_cache=rebuild_cache,
            start_date=start_date,
            end_date=end_date,
        )
    if replace_source:
        if not full_rescan:
            raise ValueError("--replace-source 必须同时使用 --full-rescan")
        return _replace_tdx_source(
            data_root,
            tdx_root=tdx_root,
            batch_rows=batch_rows,
            rebuild_cache=rebuild_cache,
            start_date=start_date,
            end_date=end_date,
        )
    return _run_tdx_local_import(
        data_root,
        tdx_root=tdx_root,
        full_rescan=full_rescan,
        batch_rows=batch_rows,
        rebuild_cache=rebuild_cache,
        start_date=start_date,
        end_date=end_date,
        audit_only=audit_only,
    )


def _run_tdx_local_import(
    data_root: Path,
    *,
    tdx_root: Path | None,
    full_rescan: bool,
    batch_rows: int,
    rebuild_cache: bool,
    start_date: str | None,
    end_date: str | None,
    audit_only: bool,
) -> dict[str, Any]:
    """Import local A-share, ETF/index, and HK stock daily/5-minute bars.

    Checkpoints are based on each file's length and mtime.  A changed file is
    re-imported in full, while Silver's immutable, source-specific partitions
    preserve existing provider data separately.
    """

    if batch_rows < 1_000:
        raise ValueError("--batch-rows 必须不少于 1000")
    root = resolve_tdx_root(tdx_root)
    if root is None:
        raise ValueError("未找到通达信金融终端本地目录；请通过 --tdx-root 指定安装目录")
    data_root = Path(data_root)
    start_date, end_date = _validate_date_range(start_date, end_date)
    state_path = _state_path(data_root, root, start_date=start_date, end_date=end_date)
    state = _load_state(state_path)
    names = _load_names(data_root)
    for market, code_names in _load_tdx_names(root).items():
        combined = dict(names.get(market, {}))
        combined.update(code_names)
        names[market] = combined
    store = None if audit_only else MarketStore(data_root)
    run_id = "audit-only" if store is None else store.begin_run("import-tdx-local:通达信金融终端:tdx-cn-v2")
    errors: list[str] = []
    audit_files: list[dict[str, Any]] = []
    asset_counts: Counter[str] = Counter()
    multiplier_counts: Counter[str] = Counter()
    next_files = dict(state.get("files") or {})
    buffered: list[dict[str, Any]] = []
    staged_files: dict[str, dict[str, Any]] = {}
    written = 0
    imported_files = 0
    skipped_files = 0
    batch_number = 0
    scanned_files = 0
    quarantined_files = 0
    quarantined_rows = 0
    rejected_files = 0

    def flush() -> None:
        nonlocal buffered, staged_files, written, imported_files, batch_number
        if not buffered:
            return
        batch_number += 1
        if store is None:
            raise RuntimeError("审计模式不能写入 Silver")
        written += _persist_batch(store, run_id, buffered, batch_number)
        next_files.update(staged_files)
        imported_files += len(staged_files)
        _save_json(state_path, {"files": next_files})
        buffered = []
        staged_files = {}

    try:
        for path, metadata in _files(root):
            scanned_files += 1
            relative = str(path.relative_to(root)).replace("\\", "/")
            signature = _signature(path)
            previous = next_files.get(relative)
            if not full_rescan and previous and _same_signature(previous, signature):
                skipped_files += 1
                continue
            try:
                policy_changed = not previous or previous.get("normalization_version") != _NORMALIZATION_VERSION
                start_offset = _incremental_offset(
                    previous,
                    signature,
                    full_rescan=full_rescan or policy_changed,
                )
                records = read_tdx_local_file(
                    path,
                    hong_kong=metadata["market"] == "HK" or metadata.get("daily_price_format") == "FLOAT32",
                    start_offset=start_offset,
                    price_scale=_price_scale(metadata),
                )
                if start_date or end_date:
                    records = [
                        record for record in records
                        if (start_date is None or str(record["day"]) >= start_date)
                        and (end_date is None or str(record["day"]) <= end_date)
                    ]
                rows, file_audit = _normalized_rows(records, metadata, names)
            except Exception as error:
                if len(errors) < 200:
                    errors.append(f"{relative}:{type(error).__name__}:{error}"[:500])
                continue
            file_audit["file"] = relative
            audit_files.append(file_audit)
            asset_counts[str(metadata["asset_type"])] += 1
            for multiplier, count in (file_audit.get("volumeMultipliers") or {}).items():
                multiplier_counts[f"{metadata['asset_type']}:{metadata['period']}:{multiplier}"] += int(count)
            quarantined_rows += int(file_audit.get("quarantinedRows") or 0)
            if file_audit.get("status") != "PASS":
                quarantined_files += 1
                _write_quarantine_evidence(data_root, relative, file_audit)
            if file_audit.get("status") == "FAILED":
                rejected_files += 1
                continue
            if not rows:
                if start_date or end_date:
                    # The source file is valid but has no bars inside this
                    # explicitly requested historical interval.
                    next_files[relative] = signature
                    continue
                if len(errors) < 200:
                    errors.append(f"{relative}:无有效K线")
                continue
            if audit_only:
                imported_files += 1
                written += len(rows)
            else:
                buffered.extend(rows)
                staged_files[relative] = {**signature, "normalization_version": _NORMALIZATION_VERSION}
                if len(buffered) >= batch_rows:
                    flush()
        flush()
        status = "COMPLETE" if not errors else "PARTIAL_FAILURE"
        if store is not None:
            store.finish_run(run_id, status, f"导入 {written} 条；文件 {imported_files}；隔离文件 {quarantined_files}")
    except Exception as error:
        if store is not None:
            store.finish_run(run_id, "FAILED", f"{type(error).__name__}: {error}"[:500])
        raise
    finally:
        if store is not None:
            store.close()

    cache: dict[str, Any] | None = None
    if written and rebuild_cache and not audit_only:
        cache = rebuild_kline_query_cache(data_root)
    if not audit_only:
        _save_json(state_path, {"normalization_version": _NORMALIZATION_VERSION, "files": next_files})
    summary = {
        "状态": (
            "完成" if written and not errors and not quarantined_files
            else "完成（含隔离）" if written and not errors
            else "部分完成" if written
            else "失败"
        ),
        "模式": "只读审计" if audit_only else "导入",
        "标准化版本": _NORMALIZATION_VERSION,
        "扫描文件": scanned_files,
        "写入K线": written,
        "导入文件": imported_files,
        "跳过未变化文件": skipped_files,
        "隔离文件": quarantined_files,
        "拒绝文件": rejected_files,
        "隔离K线": quarantined_rows,
        "错误数": len(errors),
        "错误": errors,
        "通达信目录": str(root),
        "开始日期": start_date,
        "结束日期": end_date,
        "检查点": str(state_path),
        "K线缓存": cache,
        "资产文件统计": dict(sorted(asset_counts.items())),
        "成交量倍率统计": dict(sorted(multiplier_counts.items())),
        "生成时间": _now(),
    }
    report = {
        **summary,
        "files": audit_files,
    }
    report_path = _audit_report_path(data_root)
    _save_json(report_path, report)
    summary["审计报告"] = str(report_path)
    return summary


def _audit_report_path(data_root: Path) -> Path:
    return data_root / "reports" / "tdx-local" / "latest-audit.json"


def _write_quarantine_evidence(data_root: Path, relative: str, audit: Mapping[str, Any]) -> None:
    token = sha256(relative.casefold().encode("utf-8")).hexdigest()[:16]
    path = data_root / "quarantine" / "tdx-cn-v2" / f"{token}.json"
    _save_json(path, {"sourceFile": relative, **audit})


def _replace_tdx_source(
    data_root: Path,
    *,
    tdx_root: Path | None,
    batch_rows: int,
    rebuild_cache: bool,
    start_date: str | None,
    end_date: str | None,
) -> dict[str, Any]:
    """Rebuild TDX in an isolated store and promote it with a recoverable backup."""

    if start_date or end_date:
        raise ValueError("--replace-source 只能执行完整日期范围迁移")
    migration_root = data_root / "tdx_local_migration"
    token = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
    staging_root = migration_root / "staging" / token
    summary = _run_tdx_local_import(
        staging_root,
        tdx_root=tdx_root,
        full_rescan=True,
        batch_rows=batch_rows,
        rebuild_cache=False,
        start_date=None,
        end_date=None,
        audit_only=False,
    )
    if int(summary.get("写入K线") or 0) <= 0:
        raise RuntimeError("暂存库没有可提升的 tdx-cn-v2 K 线")
    return _promote_tdx_staging(
        data_root,
        staging_root=staging_root,
        tdx_root=tdx_root,
        rebuild_cache=rebuild_cache,
        summary=summary,
        token=token,
    )


def _resume_tdx_source(
    data_root: Path,
    *,
    staging_root: Path,
    tdx_root: Path | None,
    batch_rows: int,
    rebuild_cache: bool,
    start_date: str | None,
    end_date: str | None,
) -> dict[str, Any]:
    if start_date or end_date:
        raise ValueError("--resume-staging 只能续跑完整日期范围迁移")
    staging_root = staging_root.resolve()
    expected_parent = (data_root / "tdx_local_migration" / "staging").resolve()
    if expected_parent not in staging_root.parents:
        raise ValueError("--resume-staging 必须指向 data_control/tdx_local_migration/staging 下的目录")
    if not (staging_root / "catalog.duckdb").is_file():
        raise ValueError("暂存目录缺少 catalog.duckdb，不能续跑")
    summary = _run_tdx_local_import(
        staging_root,
        tdx_root=tdx_root,
        full_rescan=False,
        batch_rows=batch_rows,
        rebuild_cache=False,
        start_date=None,
        end_date=None,
        audit_only=False,
    )
    if int(summary.get("写入K线") or 0) <= 0:
        raise RuntimeError("续跑后暂存库没有可提升的 tdx-cn-v2 K 线")
    return _promote_tdx_staging(
        data_root,
        staging_root=staging_root,
        tdx_root=tdx_root,
        rebuild_cache=rebuild_cache,
        summary=summary,
        token=staging_root.name,
    )


def _promote_tdx_staging(
    data_root: Path,
    *,
    staging_root: Path,
    tdx_root: Path | None,
    rebuild_cache: bool,
    summary: Mapping[str, Any],
    token: str,
) -> dict[str, Any]:
    backup_root = data_root / "tdx_local_migration" / "backups" / token
    staging_catalog = staging_root / "catalog.duckdb"
    staged_partitions = _tdx_partition_rows(staging_catalog)
    if not staged_partitions:
        raise RuntimeError("暂存目录没有登记 TDX Silver 分区")
    staged_files = [staging_root / str(row[1]) for row in staged_partitions]
    missing = [str(path) for path in staged_files if not path.is_file()]
    if missing:
        raise RuntimeError(f"暂存分区文件缺失: {missing[:3]}")

    active_catalog = data_root / "catalog.duckdb"
    backup_root.mkdir(parents=True, exist_ok=False)
    catalog_backup = backup_root / "catalog.duckdb"
    if active_catalog.is_file():
        shutil.copy2(active_catalog, catalog_backup)
    active_files = sorted((data_root / "silver").rglob("TDX-LOCAL-*.parquet")) if (data_root / "silver").is_dir() else []
    moved_old: list[tuple[Path, Path]] = []
    moved_new: list[tuple[Path, Path]] = []
    try:
        for source in active_files:
            target = backup_root / source.relative_to(data_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            moved_old.append((source, target))
        for source in staged_files:
            target = data_root / source.relative_to(staging_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            moved_new.append((source, target))
        _replace_catalog_partitions(active_catalog, staging_catalog, staged_partitions)
        advance_market_data_version(data_root)
        target_root = resolve_tdx_root(tdx_root)
        source_state = _state_path(staging_root, target_root, start_date=None, end_date=None) if target_root else None
        if target_root is not None and source_state is not None and source_state.is_file():
            target_state = _state_path(data_root, target_root, start_date=None, end_date=None)
            target_state.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_state, target_state)
        report_source = _audit_report_path(staging_root)
        if report_source.is_file():
            report_target = _audit_report_path(data_root)
            report_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(report_source, report_target)
    except Exception:
        for source, target in reversed(moved_new):
            if target.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(source))
        for source, target in reversed(moved_old):
            if target.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(source))
        if catalog_backup.is_file():
            shutil.copy2(catalog_backup, active_catalog)
        raise

    cache = rebuild_kline_query_cache(data_root) if rebuild_cache else None
    result = {
        **summary,
        "模式": "完整替换",
        "状态": "完成" if summary["状态"] == "完成" else "完成（含隔离）",
        "替换旧分区": len(active_files),
        "提升新分区": len(staged_partitions),
        "旧数据备份": str(backup_root),
        "暂存目录": str(staging_root),
        "K线缓存": cache,
    }
    _save_json(data_root / "reports" / "tdx-local" / f"migration-{token}.json", result)
    return result


def _tdx_partition_rows(catalog_path: Path) -> list[tuple[Any, ...]]:
    connection = duckdb.connect(str(catalog_path), read_only=True)
    try:
        return connection.execute(
            "SELECT partition_id, file_path, row_count, data_cutoff, sha256, source_run_id, status, updated_at "
            "FROM partitions WHERE partition_id LIKE 'TDX-LOCAL-%' ORDER BY partition_id"
        ).fetchall()
    finally:
        connection.close()


def _replace_catalog_partitions(
    active_catalog: Path,
    staging_catalog: Path,
    staged_partitions: Iterable[tuple[Any, ...]],
) -> None:
    store = MarketStore(active_catalog.parent)
    try:
        staging = duckdb.connect(str(staging_catalog), read_only=True)
        try:
            runs = staging.execute(
                "SELECT run_id, provider, status, started_at, completed_at, detail FROM runs"
            ).fetchall()
        finally:
            staging.close()
        store.connection.execute("BEGIN")
        store.connection.execute("DELETE FROM partitions WHERE partition_id LIKE 'TDX-LOCAL-%'")
        for run in runs:
            store.connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(run_id) DO NOTHING",
                list(run),
            )
        store.connection.executemany(
            "INSERT INTO partitions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [list(row) for row in staged_partitions],
        )
        store.connection.execute("COMMIT")
    except Exception:
        try:
            store.connection.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        store.close()


def _validate_date_range(start_date: str | None, end_date: str | None) -> tuple[str | None, str | None]:
    for value, label in ((start_date, "开始日期"), (end_date, "结束日期")):
        if value is None:
            continue
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as error:
            raise ValueError(f"{label}必须为 YYYY-MM-DD") from error
    if start_date and end_date and start_date > end_date:
        raise ValueError("开始日期不能晚于结束日期")
    return start_date, end_date


def _state_path(data_root: Path, root: Path, *, start_date: str | None, end_date: str | None) -> Path:
    """Keep checkpoints separate for installation roots and bounded imports."""

    default_root = Path(r"C:\tongdaxin")
    try:
        is_default_root = root.resolve() == default_root.resolve()
    except OSError:
        is_default_root = str(root) == str(default_root)
    if is_default_root and not start_date and not end_date:
        return data_root / "state" / "tdx_local_import.json"
    token = sha256(str(root.resolve()).casefold().encode("utf-8")).hexdigest()[:12]
    range_token = f"-{start_date or 'start'}-{end_date or 'end'}" if start_date or end_date else ""
    return data_root / "state" / f"tdx_local_import-{token}{range_token}.json"


def _files(root: Path) -> Iterable[tuple[Path, dict[str, str]]]:
    folders = (("sh", "CN"), ("sz", "CN"), ("bj", "CN"))
    for prefix, market in folders:
        for kind_folder in ("lday", "fzline"):
            directory = root / "vipdoc" / prefix / kind_folder
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*")):
                match = _CN_FILE.match(path.name)
                if not match or match.group("prefix").lower() != prefix:
                    continue
                asset_type, series_kind, exchange = _cn_classification(prefix, match.group("code"))
                yield path, {
                    "market": market, "asset_type": asset_type, "series_kind": series_kind, "exchange": exchange,
                    "symbol": match.group("code"), "period": "1d" if match.group("kind").lower() == "day" else "5m",
                }
    for kind_folder in ("lday", "fzline"):
        directory = root / "vipdoc" / "ds" / kind_folder
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*")):
            metadata = financial_ds_metadata(path.name)
            if metadata is not None:
                yield path, metadata


def _cn_classification(prefix: str, code: str) -> tuple[str, str, str]:
    if prefix == "sh":
        exchange = "SSE"
        if code.startswith("900"):
            return "B_SHARE", "B_SHARE", exchange
        if code.startswith("880"):
            return "INDEX", "TDX_BOARD_INDEX", exchange
        if code.startswith("881"):
            return "INDEX", "TDX_INDUSTRY_INDEX", exchange
        if code.startswith(("000", "889", "950", "999")):
            return "INDEX", "EQUITY_INDEX", exchange
        if code.startswith(("110", "111", "113", "118", "126")):
            return "CONVERTIBLE_BOND", "", exchange
        if code.startswith("132"):
            return "EXCHANGEABLE_BOND", "", exchange
        if code.startswith("204"):
            return "PLEDGED_REPO", "", exchange
        if code.startswith(("201", "202", "203", "204", "205", "206", "207")):
            return "REPO", "", exchange
        if code.startswith(("500", "501", "502", "505", "506")):
            return "LOF", "", exchange
        if code.startswith("508"):
            return "REIT", "", exchange
        if code.startswith(("510", "511", "512", "513", "514", "515", "516", "517", "518", "519", "520", "526", "530", "551", "560", "561", "562", "563", "581", "587", "588", "589")):
            return "ETF", "", exchange
    elif prefix == "sz":
        exchange = "SZSE"
        if code.startswith("200"):
            return "B_SHARE", "B_SHARE", exchange
        if code.startswith(("399", "980")):
            return "INDEX", "EQUITY_INDEX", exchange
        if code.startswith(("121", "123", "124", "127", "128")):
            return "CONVERTIBLE_BOND", "", exchange
        if code.startswith("120"):
            return "EXCHANGEABLE_BOND", "", exchange
        if code.startswith("1318"):
            return "PLEDGED_REPO", "", exchange
        if code.startswith("131"):
            return "REPO", "", exchange
        if code.startswith(("158", "159")):
            return "ETF", "", exchange
        if code.startswith("16"):
            return "LOF", "", exchange
        if code.startswith(("180", "181")):
            return "REIT", "", exchange
    else:
        exchange = "BSE"
        if code.startswith("899"):
            return "INDEX", "EQUITY_INDEX", exchange
    return "STOCK", "", exchange


def _price_scale(metadata: Mapping[str, str]) -> float:
    if metadata.get("daily_price_format") == "FLOAT32" or metadata["market"] == "HK" or metadata["period"] != "1d":
        return 1.0
    asset_type = metadata["asset_type"]
    if asset_type in {"B_SHARE", "ETF", "LOF", "REIT"}:
        return 1_000.0
    if asset_type in {"CONVERTIBLE_BOND", "EXCHANGEABLE_BOND", "PLEDGED_REPO", "REPO"}:
        return 10_000.0
    return 100.0


def _volume_unit(metadata: Mapping[str, str]) -> str:
    asset_type = metadata["asset_type"]
    if asset_type == "INDEX":
        return "TDX_INDEX_RAW"
    if asset_type == "FUTURE" and metadata.get("volume_semantics") == "RAW":
        return "TDX_FOREIGN_FUTURE_RAW"
    if asset_type in {"PLEDGED_REPO", "REPO"}:
        return "REPO_LOT_1000_CNY"
    if asset_type in {"CONVERTIBLE_BOND", "EXCHANGEABLE_BOND"}:
        return "BOND_UNIT"
    return "SHARE"


def _volume_profile(
    records: Iterable[Mapping[str, Any]], metadata: Mapping[str, str]
) -> tuple[list[float | None], str, Counter[float], list[str]]:
    records = list(records)
    asset_type = metadata["asset_type"]
    if asset_type == "INDEX":
        return [1.0] * len(records), "asset-rule:index-raw-volume", Counter({1.0: len(records)}), []
    if asset_type == "FUTURE" and metadata.get("volume_semantics") == "RAW":
        return [1.0] * len(records), "asset-rule:foreign-future-raw-volume", Counter({1.0: len(records)}), []
    is_repo = asset_type in {"PLEDGED_REPO", "REPO"}
    tolerance = _DAILY_VWAP_TOLERANCE if metadata["period"] == "1d" else _MINUTE_VWAP_TOLERANCE
    multipliers: list[float | None] = []
    inferred: Counter[float] = Counter()
    reasons: list[str] = []
    no_trade: list[int] = []
    for index, record in enumerate(records):
        amount = _number(record.get("raw_amount")) or 0.0
        raw_volume = _number(record.get("raw_volume")) or 0.0
        if amount == 0 and raw_volume == 0:
            multipliers.append(None)
            no_trade.append(index)
            continue
        if amount <= 0 or raw_volume <= 0:
            multipliers.append(None)
            reasons.append("金额与成交量只有一项为零或非正")
            continue
        if is_repo:
            candidates = [
                multiplier
                for multiplier in _VOLUME_MULTIPLIERS
                if abs(amount / (raw_volume * multiplier * 1_000.0) - 1.0) <= 0.02
            ]
        else:
            low = _number(record.get("low")) or 0.0
            high = _number(record.get("high")) or 0.0
            candidates = [
                multiplier
                for multiplier in _VOLUME_MULTIPLIERS
                if low * (1.0 - tolerance) <= amount / (raw_volume * multiplier) <= high * (1.0 + tolerance)
            ]
        if len(candidates) != 1:
            multipliers.append(None)
            reasons.append("非零量额无法唯一匹配候选倍率")
        else:
            multiplier = candidates[0]
            multipliers.append(multiplier)
            inferred[multiplier] += 1
    if not inferred:
        reasons.extend("无成交记录且没有相邻已验证倍率" for _ in no_trade)
        method = "row-repo-notional" if is_repo else "row-vwap-within-ohlc"
        return multipliers, method, inferred, reasons
    # A genuine zero-turnover bar has no independent unit evidence.  Use the
    # nearest verified row in the same source file, preferring the prior row
    # so a historical unit transition is not projected backwards.
    verified_indexes = [index for index, value in enumerate(multipliers) if value is not None]
    for index in no_trade:
        previous = next((candidate for candidate in reversed(verified_indexes) if candidate < index), None)
        following = next((candidate for candidate in verified_indexes if candidate > index), None)
        source_index = previous if previous is not None else following
        if source_index is not None:
            multipliers[index] = multipliers[source_index]
        else:
            reasons.append("无成交记录且没有相邻已验证倍率")
    method = "row-repo-notional" if is_repo else "row-vwap-within-ohlc"
    return multipliers, method, Counter(value for value in multipliers if value is not None), reasons


def _normalized_rows(
    records: Iterable[Mapping[str, Any]], metadata: Mapping[str, str], names: Mapping[str, Mapping[str, str]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records = list(records)
    market = metadata["market"]
    code = metadata["symbol"]
    exchange = metadata["exchange"]
    asset_type = metadata["asset_type"]
    canonical = f"{market}.{exchange}.{asset_type}.{code}"
    physical = canonical + ".TDX_LOCAL"
    name = names.get(market, {}).get(code) or _fallback_name(market, asset_type, code)
    volume_multipliers, method, multiplier_counts, volume_reasons = _volume_profile(records, metadata)
    unique_multipliers = sorted(multiplier_counts)
    invalid_volume_rows = sum(value is None for value in volume_multipliers)
    audit: dict[str, Any] = {
        "status": "PASS",
        "reason": None,
        "market": market,
        "exchange": exchange,
        "assetType": asset_type,
        "seriesKind": metadata["series_kind"],
        "symbol": code,
        "period": metadata["period"],
        "priceScale": _price_scale(metadata),
        "volumeMultiplier": unique_multipliers[0] if len(unique_multipliers) == 1 else None,
        "volumeMultipliers": {f"x{factor:g}": count for factor, count in sorted(multiplier_counts.items())},
        "volumeUnit": _volume_unit(metadata),
        "normalizationMethod": method,
        "sourceRows": len(records),
        "quarantinedRows": invalid_volume_rows,
        "normalizationVersion": _NORMALIZATION_VERSION,
    }
    output: list[dict[str, Any]] = []
    invalid_price_rows = 0
    for record, volume_multiplier in zip(records, volume_multipliers, strict=True):
        try:
            day = str(record["day"])
            time_text = str(record["time"])
            close = float(record["close"])
            open_ = float(record["open"])
            high = float(record["high"])
            low = float(record["low"])
        except (KeyError, TypeError, ValueError):
            invalid_price_rows += 1
            continue
        if not _valid_bar(day, open_, high, low, close):
            invalid_price_rows += 1
            continue
        if volume_multiplier is None:
            continue
        opened = f"{day}T{time_text}+08:00"
        duration = timedelta(hours=15) if metadata["period"] == "1d" else timedelta(minutes=5)
        closed = (datetime.fromisoformat(opened) + duration).isoformat(timespec="seconds")
        output.append({
            "instrument_id": physical,
            "canonical_instrument_id": canonical,
            "symbol": code,
            "name": name,
            "market": market,
            "asset_type": asset_type,
            "period": metadata["period"],
            "trading_date": day,
            "trading_day": day,
            "bar_start": opened,
            "bar_end": closed,
            "bar_open_time": opened,
            "bar_close_time": closed,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": (_number(record.get("raw_volume")) or 0.0) * volume_multiplier,
            "amount": None if metadata.get("amount_semantics") == "UNKNOWN" else _number(record.get("raw_amount")),
            "open_interest": None,
            "settlement": None,
            "source": _SOURCE,
            "actual_source": _SOURCE,
            "source_symbol": code,
            "series_kind": metadata["series_kind"],
            "product_code": code,
            "exchange": exchange,
            "currency": _currency(metadata),
            "source_period": metadata["period"],
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "data_version": "2",
            "quality_status": "PASS",
            "raw_open": _number(record.get("raw_open")),
            "raw_high": _number(record.get("raw_high")),
            "raw_low": _number(record.get("raw_low")),
            "raw_close": _number(record.get("raw_close")),
            "raw_volume": _number(record.get("raw_volume")),
            "raw_amount": _number(record.get("raw_amount")),
            "price_scale": _price_scale(metadata),
            "volume_multiplier": volume_multiplier,
            "volume_unit": _volume_unit(metadata),
            "normalization_method": method,
            "normalization_status": "PASS",
            "normalization_version": _NORMALIZATION_VERSION,
        })
    audit["normalizedRows"] = len(output)
    audit["quarantinedRows"] = int(audit["quarantinedRows"]) + invalid_price_rows
    if audit["quarantinedRows"]:
        audit["status"] = "PARTIAL" if output else "FAILED"
        reasons: list[str] = []
        if invalid_volume_rows:
            reasons.append(f"{invalid_volume_rows} 条成交量无法验证")
        if invalid_price_rows:
            reasons.append(f"{invalid_price_rows} 条非法 OHLC 或日期")
        if volume_reasons:
            reasons.append(volume_reasons[0])
        audit["reason"] = "；".join(reasons)
        audit["sample"] = [dict(record) for record in records[:3]]
    return output, audit


def _currency(metadata: Mapping[str, str]) -> str:
    if metadata.get("currency"):
        return str(metadata["currency"])
    if metadata["market"] == "HK":
        return "HKD"
    if metadata["asset_type"] == "B_SHARE":
        return "USD" if metadata["exchange"] == "SSE" else "HKD"
    return "CNY"


def _persist_batch(store: MarketStore, run_id: str, rows: Iterable[dict[str, Any]], batch_number: int) -> int:
    grouped: dict[tuple[str, str, str, int], list[dict[str, Any]]] = {}
    run_token = run_id.removeprefix("run-")[:12]
    for row in rows:
        year = int(str(row["trading_date"])[:4])
        key = (str(row["market"]), str(row["asset_type"]), str(row["period"]), year)
        grouped.setdefault(key, []).append(row)
    for (market, asset_type, period, year), values in grouped.items():
        values.sort(key=lambda item: (str(item["instrument_id"]), str(item["bar_open_time"])))
        store.write_silver_bars(
            PartitionKey(
                market,
                asset_type,
                period,
                year,
                f"TDX-LOCAL-{market}-{asset_type}-{period}-{year}-{run_token}-{batch_number:06d}",
            ),
            values,
            str(values[-1]["bar_open_time"]),
            run_id,
            update_query_cache=False,
        )
    return sum(len(values) for values in grouped.values())


def _load_names(data_root: Path) -> dict[str, dict[str, str]]:
    output: dict[str, dict[str, str]] = {"CN": {}, "HK": {}}
    universe = data_root / "f10" / "cn" / "universe.jsonl"
    if universe.is_file():
        for line in universe.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            code = str(item.get("code") or "").strip()
            name = str(item.get("name") or "").strip()
            if code and name:
                output["CN"][code] = name
    for filename in ("records.json", "details_20260811.jsonl", "details_20260809.jsonl"):
        path = data_root / "f10" / "hk" / filename
        if not path.is_file():
            continue
        try:
            items = json.loads(path.read_text(encoding="utf-8")) if path.suffix == ".json" else [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, ValueError):
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip().zfill(5)
            name = str(item.get("name") or "").strip()
            if code and name:
                output["HK"][code] = name
    return output


def _load_tdx_names(tdx_root: Path) -> dict[str, dict[str, str]]:
    """Read the TongdaXin terminal's market name tables for every local code."""
    output: dict[str, dict[str, str]] = {"CN": {}, "HK": {}}
    hq_cache = tdx_root / "T0002" / "hq_cache"
    if not hq_cache.is_dir():
        return output
    for filename in ("shs.tnf", "szs.tnf", "bjs.tnf"):
        path = hq_cache / filename
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        # 50-byte header followed by fixed 360-byte records: 6 ASCII code
        # bytes, then a GBK name beginning after the code field.
        for offset in range(50, len(raw) - 40, 360):
            code = raw[offset : offset + 6].decode("ascii", errors="ignore")
            if not re.fullmatch(r"\d{6}", code):
                continue
            name_start = offset + 30
            limit = min(len(raw), offset + 120)
            while name_start < limit and raw[name_start] == 0:
                name_start += 1
            name_end = name_start
            while name_end < limit and raw[name_end] != 0:
                name_end += 1
            if name_start >= name_end:
                continue
            name = raw[name_start:name_end].decode("gbk", errors="replace").strip()
            if name:
                output["CN"][code] = name
    return output


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"files": {}}
    return value if isinstance(value, dict) else {"files": {}}


def _save_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _signature(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _same_signature(previous: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    return (
        previous.get("size") == current.get("size")
        and previous.get("mtime_ns") == current.get("mtime_ns")
        and previous.get("normalization_version") == _NORMALIZATION_VERSION
    )


def _incremental_offset(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    *,
    full_rescan: bool,
) -> int:
    """Read only records appended since the last durable file checkpoint."""

    if full_rescan or not previous:
        return 0
    try:
        previous_size = int(previous.get("size") or 0)
        current_size = int(current.get("size") or 0)
    except (TypeError, ValueError):
        return 0
    if previous_size < 0 or previous_size % 32 or current_size < previous_size:
        return 0
    # A same-size mtime change can be an in-place correction of the last bar.
    # Re-read only that record; a growing file starts exactly at the old EOF.
    return max(0, previous_size - 32) if current_size == previous_size else previous_size


def _day_text(value: int) -> str:
    year, remainder = divmod(value, 10_000)
    month, day = divmod(remainder, 100)
    if not 1990 <= year <= 2100 or not 1 <= month <= 12 or not 1 <= day <= 31:
        raise ValueError(f"无效日线日期: {value}")
    return f"{year:04d}-{month:02d}-{day:02d}"


def _valid_bar(day: str, open_: float, high: float, low: float, close: float) -> bool:
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return False
    return (
        all(value == value and value > 0 for value in (open_, high, low, close))
        and high >= max(open_, low, close)
        and low <= min(open_, high, close)
    )


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _fallback_name(market: str, asset_type: str, code: str) -> str:
    if asset_type == "INDEX":
        return f"{market}指数 {code}"
    if asset_type == "ETF":
        return f"ETF {code}"
    if asset_type == "B_SHARE":
        return f"B股 {code}"
    return f"港股 {code}" if market == "HK" else f"A股 {code}"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


__all__ = ("decode_minute_day", "read_tdx_local_file", "resolve_tdx_root", "run_tdx_local_import")
