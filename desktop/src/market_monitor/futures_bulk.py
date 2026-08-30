"""Dedicated domestic and global futures import job.

The job deliberately treats the running TongdaXin Futures client as a local
file provider.  It does not use the obsolete public TDX extended-quote
protocol.  Provider rows stay physically separate in Silver; the web API
selects a source for each canonical series at read time.
"""

from __future__ import annotations

import json
import os
import re
import struct
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .futures import resolve_futures_contract_spec
from .storage import MarketStore, PartitionKey

_DAY = struct.Struct("<IffffIIf")
_LC5 = struct.Struct("<HHffffIIf")
_SPECIAL = re.compile(r"^(?P<market>\d+)#(?P<code>[A-Z]+L[789])\.(?P<kind>day|lc5)$", re.I)
_CONTRACT = re.compile(
    r"^(?P<market>\d+)#(?P<code>(?P<product>[A-Z]+)(?P<delivery>\d{3,4}))\.(?P<kind>day|lc5)$",
    re.I,
)
_INDEX = re.compile(r"^42#(?P<code>(?:IMCI|T\d{3}))\.(?P<kind>day|lc5)$", re.I)
_EXCHANGES = {"28": "CZCE", "29": "DCE", "30": "SHFE", "47": "CFFEX", "66": "GFEX"}
_INE_PRODUCTS = {"SC", "NR", "LU", "BC", "EC"}
_LOCAL_TDX_MINUTE_START = "2018-01-02"
_GLOBAL_CONTINUOUS = (
    ("GC00Y", "COMEX黄金连续", "COMEX"), ("SI00Y", "COMEX白银连续", "COMEX"),
    ("HG00Y", "COMEX铜连续", "COMEX"), ("PL00Y", "NYMEX铂金连续", "NYMEX"),
    ("PA00Y", "NYMEX钯金连续", "NYMEX"), ("CL00Y", "WTI原油连续", "NYMEX"),
    ("HO00Y", "NYMEX燃油连续", "NYMEX"), ("NG00Y", "NYMEX天然气连续", "NYMEX"),
    ("YM00Y", "道琼斯期货连续", "CBOT"), ("NQ00Y", "纳斯达克100期货连续", "CME"),
    ("ES00Y", "标普500期货连续", "CME"), ("RT00Y", "罗素2000期货连续", "CME"),
    ("CN00Y", "富时中国A50期货连续", "SGX"), ("TU00Y", "美国2年期国债期货连续", "CBOT"),
    ("FV00Y", "美国5年期国债期货连续", "CBOT"), ("TY00Y", "美国10年期国债期货连续", "CBOT"),
    ("US00Y", "美国30年期国债期货连续", "CBOT"), ("ZS00Y", "CBOT大豆连续", "CBOT"),
    ("ZC00Y", "CBOT玉米连续", "CBOT"), ("ZM00Y", "CBOT豆粕连续", "CBOT"),
    ("ZL00Y", "CBOT豆油连续", "CBOT"), ("ZW00Y", "CBOT小麦连续", "CBOT"),
)
_GLOBAL_SINA = (("AHD", "LME铝", "LME"), ("OIL", "布伦特原油（现金）", "ICE"))
_GLOBAL_EQUITY = {"YM00Y", "NQ00Y", "ES00Y", "RT00Y", "CN00Y"}
_GLOBAL_RATES = {"TU00Y", "FV00Y", "TY00Y", "US00Y"}
_CONTINUOUS_LABELS = {"SECONDARY": "次连", "MAIN": "主连", "WEIGHTED": "加权"}


def resolve_tdx_root(value: Path | None = None) -> Path | None:
    """Resolve only an existing TDX Futures installation directory."""
    candidates = [value] if value else []
    env_value = os.environ.get("TDX_FUTURES_ROOT")
    if env_value:
        candidates.append(Path(env_value))
    candidates.append(Path(r"C:\new_tdxqh"))
    for candidate in candidates:
        if candidate and (candidate / "vipdoc" / "ds").is_dir():
            return candidate
    return None


def decode_lc5_day(value: int) -> str:
    year = value // 2048 + 2004
    remainder = value % 2048
    return f"{year:04d}-{remainder // 100:02d}-{remainder % 100:02d}"


def read_tdx_file(path: Path, *, start_offset: int = 0) -> list[dict[str, Any]]:
    """Read a stable snapshot of one standard 32-byte TDX Futures file."""
    size_before = path.stat().st_size
    if size_before % 32:
        raise ValueError(f"文件长度不是32字节倍数: {path.name}")
    if start_offset < 0 or start_offset > size_before or start_offset % 32:
        raise ValueError(f"无效的增量读取偏移: {start_offset}")
    with path.open("rb") as stream:
        stream.seek(start_offset)
        payload = stream.read()
    if path.stat().st_size != size_before:
        raise RuntimeError(f"期货通正在写入，稍后重试: {path.name}")
    suffix = path.suffix.lower()
    rows: list[dict[str, Any]] = []
    for offset in range(0, len(payload), 32):
        values = (_DAY if suffix == ".day" else _LC5).unpack_from(payload, offset)
        if suffix == ".day":
            raw_day, open_, high, low, close, open_interest, volume, settlement = values
            day = f"{raw_day // 10000:04d}-{raw_day % 10000 // 100:02d}-{raw_day % 100:02d}"
            rows.append({"day": day, "time": "00:00:00", "open": open_, "high": high, "low": low,
                         "close": close, "open_interest": open_interest, "volume": volume, "settlement": settlement})
        else:
            raw_day, minutes, open_, high, low, close, open_interest, volume, _reserved = values
            rows.append({"day": decode_lc5_day(raw_day), "time": f"{minutes // 60:02d}:{minutes % 60:02d}:00",
                         "open": open_, "high": high, "low": low, "close": close,
                         "open_interest": open_interest, "volume": volume})
    return rows


def _metadata(root: Path) -> dict[str, str]:
    path = root / "T0002" / "hq_cache" / "code2qhidx.ini"
    try:
        text = path.read_bytes().decode("gbk", errors="replace")
    except OSError:
        return {}
    names: dict[str, str] = {}
    index_names: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^(\d+)_([^=]+)=([^|\r\n]+)", line.strip())
        if match:
            names[f"{match.group(1)}#{match.group(2).upper()}"] = match.group(3).strip()
            continue
        index_match = re.match(r"^Index\d+=([^|]+)\|([^|\r\n]+)", line.strip())
        if index_match:
            index_names[index_match.group(1).upper()] = index_match.group(2).strip()
    for code, name in index_names.items():
        names.setdefault(f"42#{code}", name)
    return names


def _exchange(market_code: str, product: str) -> str:
    if market_code == "30" and product.upper() in _INE_PRODUCTS:
        return "INE"
    return _EXCHANGES.get(market_code, "TDX")


def _continuous_name(raw_name: str | None, product: str, series_kind: str) -> str:
    """Give each TDX L7/L8/L9 series an unambiguous public name."""

    label = _CONTINUOUS_LABELS[series_kind]
    base = re.sub(r"(?:指数|主力连续|主力|主连|次连|加权)$", "", str(raw_name or "").strip())
    return f"{base or product}{label}"


def _beijing_now() -> str:
    return datetime.now().astimezone().astimezone(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _bar(
    *, physical_id: str, canonical_id: str, symbol: str, name: str, market: str, asset_type: str,
    period: str, day: str, time_text: str, row: Mapping[str, Any], source: str, series_kind: str,
    exchange: str, source_symbol: str, product_code: str | None = None,
    availability: str = "可用", unsupported_reason: str | None = None,
) -> dict[str, Any]:
    opened = f"{day}T{time_text}+08:00"
    minutes = {"5m": 5, "15m": 15, "30m": 30, "1h": 60}.get(period, 0)
    close_time = opened if not minutes else datetime.fromisoformat(opened).replace().isoformat()
    product = product_code or canonical_id.split(".")[-2]
    result = {
        "instrument_id": physical_id, "canonical_instrument_id": canonical_id, "symbol": symbol, "name": name,
        "market": market, "asset_type": asset_type, "period": period, "trading_date": day,
        "trading_day": day, "bar_start": opened, "bar_end": close_time, "bar_open_time": opened,
        "bar_close_time": close_time, "open": _number(row.get("open")), "high": _number(row.get("high")),
        "low": _number(row.get("low")), "close": _number(row.get("close")), "volume": _number(row.get("volume")),
        "amount": _number(row.get("amount")), "open_interest": _number(row.get("open_interest")),
        "settlement": _number(row.get("settlement")), "source": source, "actual_source": source,
        "source_symbol": source_symbol, "series_kind": series_kind,
        "product_code": product,
        "exchange": exchange, "availability": availability, "unsupported_reason": unsupported_reason,
        "source_period": period, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_version": "2", "quality_status": "PASS",
    }
    if asset_type == "FUTURE" and market == "CN":
        try:
            resolution = resolve_futures_contract_spec(product, exchange, date.fromisoformat(day))
        except ValueError:
            resolution = None
        if resolution and resolution.spec:
            result.update(
                {
                    "contract_multiplier": resolution.spec.contract_multiplier,
                    "margin_rate": resolution.spec.margin_rate,
                    "contract_spec_effective_from": (
                        resolution.spec.effective_from.isoformat() if resolution.spec.effective_from else None
                    ),
                    "contract_spec_source": resolution.spec.source,
                }
            )
        elif resolution:
            result["contract_spec_reason"] = resolution.reason
    return result


def _partition_writes(store: MarketStore, run_id: str, rows: Iterable[dict[str, Any]], *, prefix: str) -> int:
    grouped: dict[tuple[str, str, str, int], list[dict[str, Any]]] = {}
    for row in rows:
        try:
            year = int(str(row["trading_date"])[:4])
        except (KeyError, ValueError):
            continue
        key = (str(row["market"]), str(row["asset_type"]), str(row["period"]), year)
        grouped.setdefault(key, []).append(row)
    written = 0
    for (market, asset_type, period, year), values in grouped.items():
        values.sort(key=lambda item: str(item["bar_open_time"]))
        store.write_silver_bars(
            PartitionKey(market, asset_type, period, year, f"{prefix}-{market}-{asset_type}-{period}-{year}"),
            values, str(values[-1]["bar_open_time"]), run_id,
        )
        written += len(values)
    return written


def _load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"files": {}}


def _save_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _file_signature(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "last": rows[-1]["day"] + " " + rows[-1]["time"] if rows else ""}


def _incremental_offset(old: Mapping[str, Any] | None, size: int, *, full_rescan: bool) -> int:
    if full_rescan or not old:
        return 0
    try:
        previous_size = int(old.get("size") or 0)
    except (TypeError, ValueError):
        return 0
    if previous_size < 0 or previous_size % 32 or size < previous_size:
        return 0
    return max(0, previous_size - 32) if size == previous_size else previous_size


def _tdx_rows(
    root: Path, state: dict[str, Any], *, full_rescan: bool, emit: Callable[[list[dict[str, Any]]], int],
    checkpoint: Callable[[Mapping[str, Any]], None] | None = None,
) -> tuple[int, dict[str, Any], list[str]]:
    metadata = _metadata(root)
    written = 0
    next_files = dict(state.get("files") or {})
    errors: list[str] = []
    for folder in (root / "vipdoc" / "ds" / "lday", root / "vipdoc" / "ds" / "fzline"):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*")):
            match = _SPECIAL.match(path.name) or _CONTRACT.match(path.name) or _INDEX.match(path.name)
            if not match:
                continue
            relative = str(path.relative_to(root)).replace("\\", "/")
            stat = path.stat()
            old = next_files.get(relative)
            if not full_rescan and old and old.get("size") == stat.st_size and old.get("mtime_ns") == stat.st_mtime_ns:
                continue
            try:
                records = read_tdx_file(
                    path,
                    start_offset=_incremental_offset(old, stat.st_size, full_rescan=full_rescan),
                )
            except Exception as error:
                errors.append(f"{path.name}:{type(error).__name__}")
                continue
            next_files[relative] = _file_signature(path, records)
            period = "1d" if path.suffix.lower() == ".day" else "5m"
            # 通达信期货通的本地连续合约缓存可能带有零散的更早分钟
            # 历史；桌面项目仅保留完整覆盖开始日之后的 5 分钟数据。
            # 原始 .lc5 文件保持不动，避免影响用户的通达信终端缓存。
            if period == "5m":
                records = [record for record in records if str(record["day"]) >= _LOCAL_TDX_MINUTE_START]
            file_rows: list[dict[str, Any]] = []
            if "code" in match.groupdict() and path.name.upper().startswith("42#"):
                code = match.group("code").upper()
                canonical = f"CN.TDX.INDEX.{code}.COMMODITY_INDEX"
                physical = canonical + ".TDX"
                name = metadata.get(f"42#{code}", f"通达信商品指数 {code}")
                for record in records:
                    file_rows.append(_bar(physical_id=physical, canonical_id=canonical, symbol=code, name=name, market="CN",
                                       asset_type="INDEX", period=period, day=record["day"], time_text=record["time"], row=record,
                                       source="通达信期货通", series_kind="COMMODITY_INDEX", exchange="TDX", source_symbol=code))
            elif match.groupdict().get("delivery"):
                market_code, code = match.group("market"), match.group("code").upper()
                product = match.group("product").upper()
                exchange = _exchange(market_code, product)
                canonical = f"CN.{exchange}.FUTURE.{code}.CONTRACT"
                physical = canonical + ".TDX"
                name = metadata.get(f"{market_code}#{code}", f"{product}{match.group('delivery')}")
                for record in records:
                    file_rows.append(_bar(
                        physical_id=physical, canonical_id=canonical, symbol=code, name=name, market="CN",
                        asset_type="FUTURE", period=period, day=record["day"], time_text=record["time"], row=record,
                        source="通达信期货通", series_kind="CONTRACT", exchange=exchange,
                        source_symbol=code, product_code=product,
                    ))
            else:
                market_code, code = match.group("market"), match.group("code").upper()
                product, suffix = code[:-2], code[-2:]
                kind = {"L7": "SECONDARY", "L8": "MAIN", "L9": "WEIGHTED"}[suffix]
                exchange = _exchange(market_code, product)
                canonical = f"CN.{exchange}.FUTURE.{product}.{kind}"
                physical = canonical + ".TDX"
                raw_name = metadata.get(f"{market_code}#{code}") or metadata.get(f"{market_code}#{code[:-1]}9")
                name = _continuous_name(raw_name, product, kind)
                for record in records:
                    file_rows.append(_bar(physical_id=physical, canonical_id=canonical, symbol=code, name=name, market="CN",
                                          asset_type="FUTURE", period=period, day=record["day"], time_text=record["time"], row=record,
                                          source="通达信期货通", series_kind=kind, exchange=exchange, source_symbol=code))
            written += emit(file_rows)
            if checkpoint:
                checkpoint({"files": next_files})
    return written, {"files": next_files}, errors


def _frame_rows(frame: Any) -> list[dict[str, Any]]:
    try:
        return [dict(item) for item in frame.to_dict(orient="records")]
    except Exception:
        return []


def _value(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


def _ak_domestic(api: Any, errors: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        displayed = _frame_rows(api.futures_display_main_sina())
    except Exception as error:
        errors.append(f"AKShare国内主连目录:{type(error).__name__}")
        return rows
    today = datetime.now().strftime("%Y%m%d")
    for item in displayed:
        symbol = str(_value(item, "symbol", "代码") or "").upper()
        if not symbol.endswith("0"):
            continue
        product = symbol[:-1]
        exchange = str(_value(item, "exchange", "交易所") or "TDX").upper()
        name = str(_value(item, "name", "名称") or f"{product}主连")
        canonical = f"CN.{exchange}.FUTURE.{product}.MAIN"
        physical = canonical + ".AKSHARE"
        for period, api_period in (("5m", "5"), ("15m", "15"), ("30m", "30"), ("1h", "60")):
            try:
                frame = api.futures_zh_minute_sina(symbol=symbol, period=api_period)
            except Exception as error:
                errors.append(f"{symbol}:{period}:{type(error).__name__}")
                continue
            for record in _frame_rows(frame):
                stamp = str(_value(record, "datetime", "时间", "日期") or "")
                if len(stamp) < 10:
                    continue
                day, time_text = stamp[:10], (stamp[11:19] if len(stamp) >= 19 else "00:00:00")
                rows.append(_bar(physical_id=physical, canonical_id=canonical, symbol=symbol, name=name, market="CN", asset_type="FUTURE",
                                 period=period, day=day, time_text=time_text, row={"open": _value(record, "open", "开盘"), "high": _value(record, "high", "最高"), "low": _value(record, "low", "最低"), "close": _value(record, "close", "收盘"), "volume": _value(record, "volume", "成交量"), "open_interest": _value(record, "hold", "持仓量")},
                                 source="AKShare", series_kind="MAIN", exchange=exchange, source_symbol=symbol))
        try:
            daily = api.futures_main_sina(symbol=symbol, start_date="20000101", end_date=today)
        except Exception as error:
            errors.append(f"{symbol}:1d:{type(error).__name__}")
            continue
        for record in _frame_rows(daily):
            day = str(_value(record, "日期", "date") or "")[:10]
            if not day:
                continue
            rows.append(_bar(physical_id=physical, canonical_id=canonical, symbol=symbol, name=name, market="CN", asset_type="FUTURE",
                             period="1d", day=day, time_text="00:00:00", row={"open": _value(record, "开盘价", "open"), "high": _value(record, "最高价", "high"), "low": _value(record, "最低价", "low"), "close": _value(record, "收盘价", "close"), "volume": _value(record, "成交量", "volume"), "open_interest": _value(record, "持仓量", "hold")},
                             source="AKShare", series_kind="MAIN", exchange=exchange, source_symbol=symbol))
    return rows


def _ak_indexes(api: Any, errors: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code, name in (("100001.CCI", "中证商品期货指数"), ("000001.CCI", "中证商品期货价格指数")):
        try:
            frame = api.futures_index_ccidx(symbol=code)
        except Exception as error:
            errors.append(f"{code}:{type(error).__name__}")
            continue
        canonical = f"CN.CCIDX.INDEX.{code}.COMMODITY_INDEX"
        for record in _frame_rows(frame):
            day = str(_value(record, "日期", "date") or "")[:10]
            if not day:
                continue
            rows.append(_bar(physical_id=canonical + ".AKSHARE", canonical_id=canonical, symbol=code, name=name, market="CN", asset_type="INDEX",
                             period="1d", day=day, time_text="00:00:00", row={"open": _value(record, "openingPrice", "开盘"), "high": _value(record, "highPrice", "最高"), "low": _value(record, "lowPrice", "最低"), "close": _value(record, "收盘点位", "结算点位", "close")},
                             source="AKShare", series_kind="COMMODITY_INDEX", exchange="CCIDX", source_symbol=code))
    return rows


def _ak_global(api: Any, errors: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for symbol, name, exchange in _GLOBAL_CONTINUOUS:
        try:
            frame = api.futures_global_hist_em(symbol=symbol)
        except Exception as error:
            errors.append(f"{symbol}:{type(error).__name__}")
            continue
        canonical = f"GLOBAL.{exchange}.FUTURE.{symbol[:-3]}.CONTINUOUS"
        series_kind = "GLOBAL_EQUITY_INDEX_FUTURE" if symbol in _GLOBAL_EQUITY else "GLOBAL_RATE_FUTURE" if symbol in _GLOBAL_RATES else "GLOBAL_COMMODITY_FUTURE"
        for record in _frame_rows(frame):
            day = str(_value(record, "日期", "date") or "")[:10]
            if not day:
                continue
            rows.append(_bar(physical_id=canonical + ".AKSHARE", canonical_id=canonical, symbol=symbol, name=name, market="GLOBAL", asset_type="FUTURE",
                             period="1d", day=day, time_text="00:00:00", row={"open": _value(record, "开盘", "open"), "high": _value(record, "最高", "high"), "low": _value(record, "最低", "low"), "close": _value(record, "收盘", "close"), "volume": _value(record, "成交量", "volume"), "open_interest": _value(record, "持仓量", "open_interest")},
                             source="AKShare", series_kind=series_kind, exchange=exchange, source_symbol=symbol))
    for symbol, name, exchange in _GLOBAL_SINA:
        try:
            frame = api.futures_foreign_hist(symbol=symbol)
        except Exception as error:
            errors.append(f"{symbol}:{type(error).__name__}")
            continue
        canonical = f"GLOBAL.{exchange}.FUTURE.{symbol}.CONTINUOUS"
        for record in _frame_rows(frame):
            day = str(_value(record, "date", "日期") or "")[:10]
            if day:
                rows.append(_bar(physical_id=canonical + ".AKSHARE", canonical_id=canonical, symbol=symbol, name=name, market="GLOBAL", asset_type="FUTURE",
                                 period="1d", day=day, time_text="00:00:00", row=record, source="AKShare", series_kind="GLOBAL_COMMODITY_FUTURE", exchange=exchange, source_symbol=symbol))
    try:
        frame = api.bond_gb_us_sina(symbol="美国10年期国债")
        canonical = "GLOBAL.US.INDEX.US10Y.REFERENCE_YIELD"
        for record in _frame_rows(frame):
            day = str(_value(record, "日期", "date") or "")[:10]
            if day:
                close = _value(record, "收盘", "close", "最新价")
                rows.append(_bar(physical_id=canonical + ".AKSHARE", canonical_id=canonical, symbol="US10Y", name="美国10年期国债收益率", market="GLOBAL", asset_type="INDEX",
                                 period="1d", day=day, time_text="00:00:00", row={"open": close, "high": close, "low": close, "close": close}, source="AKShare", series_kind="REFERENCE_YIELD", exchange="US", source_symbol="US10YT"))
    except Exception as error:
        errors.append(f"美国10年期国债收益率:{type(error).__name__}")
    return rows


def run_bulk_futures(
    data_root: Path, *, tdx_futures_root: Path | None = None, include_domestic: bool = True,
    include_global: bool = True, include_akshare: bool = True, full_rescan: bool = False, api: Any | None = None,
) -> dict[str, Any]:
    """Import all available local domestic series and the curated AKShare set."""
    data_root = Path(data_root)
    state_path = data_root / "state" / "futures_import.json"
    catalog_path = data_root / "state" / "futures_catalog.json"
    state = _load_state(state_path)
    errors: list[str] = []
    output: list[dict[str, Any]] = []
    written = 0
    tdx_root = resolve_tdx_root(tdx_futures_root)
    store = MarketStore(data_root)
    if include_domestic and tdx_root:
        store.register_default_datasets()
        tdx_run_id = store.begin_run("bulk-futures:通达信期货通")
        try:
            local_written, next_state, local_errors = _tdx_rows(
                tdx_root, state, full_rescan=full_rescan,
                emit=lambda rows: _partition_writes(
                    store,
                    tdx_run_id,
                    rows,
                    prefix=(
                        "FUTURES-TDX-"
                        + str(rows[0]["instrument_id"]).replace(".", "-")
                        + "-"
                        + tdx_run_id.removeprefix("run-")[:12]
                        if rows
                        else "FUTURES-TDX"
                    ),
                ),
                checkpoint=lambda value: _save_json(state_path, value),
            )
            written += local_written
            state = next_state
            errors.extend(local_errors)
            store.finish_run(tdx_run_id, "COMPLETE" if not local_errors else "PARTIAL_FAILURE", f"通达信期货通导入 {local_written} 条")
        except Exception as error:
            store.finish_run(tdx_run_id, "FAILED", str(error)[:500])
            raise
    elif include_domestic:
        errors.append("未找到通达信期货通本地目录")
    if include_akshare and api is None and (include_domestic or include_global):
        try:
            import akshare as api  # type: ignore[no-redef]
        except Exception as error:
            errors.append(f"AKShare不可用:{type(error).__name__}")
            api = None
    if include_akshare and api is not None and include_domestic:
        output.extend(_ak_domestic(api, errors))
        output.extend(_ak_indexes(api, errors))
    if include_akshare and api is not None and include_global:
        output.extend(_ak_global(api, errors))
    unsupported = [{"canonicalInstrumentId": "GLOBAL.CBOE.INDEX.VIX.REFERENCE_INDEX", "symbol": "VIX", "name": "VIX波动率指数", "market": "GLOBAL", "assetType": "INDEX", "seriesKind": "REFERENCE_INDEX", "availability": "暂不支持", "unsupportedReason": "当前 AKShare 版本没有已验证的 VIX 历史接口"}]
    try:
        store.register_default_datasets()
        run_id = store.begin_run("bulk-futures")
        try:
            written += _partition_writes(store, run_id, output, prefix="FUTURES-BULK") if output else 0
            status = "COMPLETE" if not errors else "PARTIAL_FAILURE"
            store.finish_run(run_id, status, f"期货导入 {written} 条；错误 {len(errors)} 条")
        except Exception as error:
            store.finish_run(run_id, "FAILED", str(error)[:500])
            raise
    finally:
        store.close()
    _save_json(state_path, state)
    _save_json(catalog_path, {"updatedAt": _beijing_now(), "unsupported": unsupported})
    return {"状态": "完成" if written and not errors else "部分完成" if written else "失败", "写入K线": written,
            "错误": errors, "通达信目录": str(tdx_root) if tdx_root else None, "检查点": str(state_path), "生成时间": _beijing_now()}


__all__ = ("decode_lc5_day", "read_tdx_file", "resolve_tdx_root", "run_bulk_futures")
