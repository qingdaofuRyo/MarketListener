"""Offline importer for TongdaXin desktop A-share and Hong Kong market files.

The ordinary TongdaXin terminal and TongdaXin Futures use different binary
layouts.  This module deliberately handles only the ordinary terminal's
``sh``/``sz``/``bj`` and Hong Kong ``31#`` files; futures remain owned by
``futures_bulk`` so open interest and settlement can never be misread as
stock turnover fields.
"""

from __future__ import annotations

import json
import os
import re
import struct
from hashlib import sha256
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .market_query_cache import rebuild_kline_query_cache
from .storage import MarketStore, PartitionKey


_CN_DAY = struct.Struct("<IiiiifII")
_HK_DAY = struct.Struct("<IfffffII")
_MINUTE = struct.Struct("<HHfffffII")
_CN_FILE = re.compile(r"^(?P<prefix>sh|sz|bj)(?P<code>\d{6})\.(?P<kind>day|lc5)$", re.IGNORECASE)
_HK_FILE = re.compile(r"^31#(?P<code>\d{5})\.(?P<kind>day|lc5)$", re.IGNORECASE)
_SOURCE = "通达信金融终端（本地）"


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


def decode_minute_day(value: int) -> str:
    """Decode the packed date used by TDX .lc5 records."""

    year = value // 2048 + 2004
    remainder = value % 2048
    month, day = divmod(remainder, 100)
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        raise ValueError(f"无效分钟线日期编码: {value}")
    return f"{year:04d}-{month:02d}-{day:02d}"


def read_tdx_local_file(
    path: Path, *, hong_kong: bool = False, start_offset: int = 0
) -> list[dict[str, Any]]:
    """Read one stable local TDX stock/HK file into raw OHLCVA records."""

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
            else:
                raw_day, open_raw, high_raw, low_raw, close_raw, amount, volume, _reserved = _CN_DAY.unpack_from(payload, offset)
                open_, high, low, close = (open_raw / 100.0, high_raw / 100.0, low_raw / 100.0, close_raw / 100.0)
            day = _day_text(raw_day)
            records.append({
                "day": day, "time": "00:00:00", "open": open_, "high": high, "low": low, "close": close,
                "amount": amount, "volume": volume,
            })
        else:
            raw_day, minutes, open_, high, low, close, amount, volume, _reserved = _MINUTE.unpack_from(payload, offset)
            if not 0 <= minutes < 24 * 60:
                raise ValueError(f"无效分钟值: {minutes}")
            records.append({
                "day": decode_minute_day(raw_day), "time": f"{minutes // 60:02d}:{minutes % 60:02d}:00",
                "open": open_, "high": high, "low": low, "close": close, "amount": amount, "volume": volume,
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
    store = MarketStore(data_root)
    run_id = store.begin_run("import-tdx-local:通达信金融终端")
    errors: list[str] = []
    next_files = dict(state.get("files") or {})
    buffered: list[dict[str, Any]] = []
    staged_files: dict[str, dict[str, Any]] = {}
    written = 0
    imported_files = 0
    skipped_files = 0
    batch_number = 0

    def flush() -> None:
        nonlocal buffered, staged_files, written, imported_files, batch_number
        if not buffered:
            return
        batch_number += 1
        written += _persist_batch(store, run_id, buffered, batch_number)
        next_files.update(staged_files)
        imported_files += len(staged_files)
        _save_json(state_path, {"files": next_files})
        buffered = []
        staged_files = {}

    try:
        for path, metadata in _files(root):
            relative = str(path.relative_to(root)).replace("\\", "/")
            signature = _signature(path)
            previous = next_files.get(relative)
            if not full_rescan and previous and _same_signature(previous, signature):
                skipped_files += 1
                continue
            try:
                start_offset = _incremental_offset(previous, signature, full_rescan=full_rescan)
                records = read_tdx_local_file(
                    path,
                    hong_kong=metadata["market"] == "HK",
                    start_offset=start_offset,
                )
                if start_date or end_date:
                    records = [
                        record for record in records
                        if (start_date is None or str(record["day"]) >= start_date)
                        and (end_date is None or str(record["day"]) <= end_date)
                    ]
                rows = _normalized_rows(records, metadata, names)
            except Exception as error:
                if len(errors) < 200:
                    errors.append(f"{relative}:{type(error).__name__}:{error}"[:500])
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
            buffered.extend(rows)
            staged_files[relative] = signature
            if len(buffered) >= batch_rows:
                flush()
        flush()
        status = "COMPLETE" if not errors else "PARTIAL_FAILURE"
        store.finish_run(run_id, status, f"导入 {written} 条；文件 {imported_files}；错误 {len(errors)}")
    except Exception as error:
        store.finish_run(run_id, "FAILED", f"{type(error).__name__}: {error}"[:500])
        raise
    finally:
        store.close()

    cache: dict[str, Any] | None = None
    if written and rebuild_cache:
        cache = rebuild_kline_query_cache(data_root)
    _save_json(state_path, {"files": next_files})
    return {
        "状态": "完成" if written and not errors else "部分完成" if written else "失败",
        "写入K线": written,
        "导入文件": imported_files,
        "跳过未变化文件": skipped_files,
        "错误数": len(errors),
        "错误": errors,
        "通达信目录": str(root),
        "开始日期": start_date,
        "结束日期": end_date,
        "检查点": str(state_path),
        "K线缓存": cache,
        "生成时间": _now(),
    }


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
        for path in sorted(directory.glob("31#*")):
            match = _HK_FILE.match(path.name)
            if match:
                yield path, {
                    "market": "HK", "asset_type": "STOCK", "series_kind": "", "exchange": "HKEX",
                    "symbol": match.group("code"), "period": "1d" if match.group("kind").lower() == "day" else "5m",
                }


def _cn_classification(prefix: str, code: str) -> tuple[str, str, str]:
    if prefix == "sh":
        exchange = "SSE"
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


def _normalized_rows(
    records: Iterable[Mapping[str, Any]], metadata: Mapping[str, str], names: Mapping[str, Mapping[str, str]],
) -> list[dict[str, Any]]:
    market = metadata["market"]
    code = metadata["symbol"]
    exchange = metadata["exchange"]
    asset_type = metadata["asset_type"]
    canonical = f"{market}.{exchange}.{asset_type}.{code}"
    physical = canonical + ".TDX_LOCAL"
    name = names.get(market, {}).get(code) or _fallback_name(market, asset_type, code)
    output: list[dict[str, Any]] = []
    for record in records:
        try:
            day = str(record["day"])
            time_text = str(record["time"])
            close = float(record["close"])
            open_ = float(record["open"])
            high = float(record["high"])
            low = float(record["low"])
        except (KeyError, TypeError, ValueError):
            continue
        if not _valid_bar(day, open_, high, low, close):
            continue
        opened = f"{day}T{time_text}+08:00"
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
            "bar_end": opened,
            "bar_open_time": opened,
            "bar_close_time": opened,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": _number(record.get("volume")),
            "amount": _number(record.get("amount")),
            "open_interest": None,
            "settlement": None,
            "source": _SOURCE,
            "actual_source": _SOURCE,
            "source_symbol": code,
            "series_kind": metadata["series_kind"],
            "product_code": code,
            "exchange": exchange,
            "currency": "HKD" if market == "HK" else "CNY",
            "source_period": metadata["period"],
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "data_version": "1",
            "quality_status": "PASS",
        })
    return output


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
    return previous.get("size") == current.get("size") and previous.get("mtime_ns") == current.get("mtime_ns")


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
    return all(value == value and value >= 0 for value in (open_, high, low, close)) and high >= low


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
    return f"港股 {code}" if market == "HK" else f"A股 {code}"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


__all__ = ("decode_minute_day", "read_tdx_local_file", "resolve_tdx_root", "run_tdx_local_import")
