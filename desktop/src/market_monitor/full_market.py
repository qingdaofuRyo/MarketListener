"""Resumable, rate-limited initial backfill for all A/H-share daily bars."""

from __future__ import annotations

import concurrent.futures
import json
import math
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from .f10 import load_a_share_universe, load_hk_universe
from .storage import MarketStore, PartitionKey


FetchFrame = Callable[[str, str, str], Iterable[Mapping[str, Any]]]
_MAX_WORKERS = 4


def run_full_stock_backfill(
    data_root: Path,
    *,
    market: str = "BOTH",
    history_days: int = 450,
    workers: int = 2,
    batch_size: int = 20,
    pause_seconds: float = 0.3,
    fetcher: FetchFrame | None = None,
) -> dict[str, Any]:
    """Fetch every A/H stock with durable checkpoints after every batch."""
    market = market.upper()
    if market not in {"CN", "HK", "BOTH"}:
        raise ValueError("market must be CN, HK, or BOTH")
    if history_days < 30:
        raise ValueError("history_days must be at least 30")
    if not 1 <= workers <= _MAX_WORKERS:
        raise ValueError(f"workers must be between 1 and {_MAX_WORKERS}")
    if batch_size < 1 or pause_seconds < 0.1:
        raise ValueError("batch_size must be positive and pause_seconds must be at least 0.1")

    root = Path(data_root)
    selected = ("CN", "HK") if market == "BOTH" else (market,)
    summary: dict[str, Any] = {"状态": "完成", "市场": {}, "更新时间": _now()}
    for market_key in selected:
        summary["市场"][market_key] = _run_market(
            root, market_key, history_days, workers, batch_size, pause_seconds, fetcher or _fetch_market_rows
        )
    if any(item["状态"] != "完成" for item in summary["市场"].values()):
        summary["状态"] = "部分完成"
    summary["更新时间"] = _now()
    return summary


def run_full_etf_backfill(
    data_root: Path,
    *,
    workers: int = 4,
    batch_size: int = 20,
    pause_seconds: float = 0.2,
) -> dict[str, Any]:
    """Fetch all currently listed domestic exchange-traded funds, resumably."""
    if not 1 <= workers <= _MAX_WORKERS:
        raise ValueError(f"workers must be between 1 and {_MAX_WORKERS}")
    root = Path(data_root)
    universe = _etf_universe()
    state_path = root / "bulk_etf" / "cn_state.json"
    previous = _load_state(state_path)
    completed = {str(code) for code in previous.get("已完成代码", [])}
    pending = [item for item in universe if item["code"] not in completed]
    result: dict[str, Any] = {
        "状态": "运行中", "市场": "中国大陆（A股）", "类型": "交易型开放式指数基金（ETF）", "数据源": "通达信 TCP",
        "总标的": len(universe), "已完成": len(completed), "待处理": len(pending), "失败": list(previous.get("失败", []))[-200:], "更新时间": _now(),
    }
    _save_state(state_path, result, completed)
    print(f"【全量ETF日线】开始：共 {len(universe)} 个标的，待处理 {len(pending)} 个。", flush=True)
    if not pending:
        result["状态"] = "完成"
        _save_state(state_path, result, completed)
        return result
    store = MarketStore(root)
    run_id = store.begin_run("全量ETF日线")
    failures: list[dict[str, str]] = list(result["失败"])
    written_rows = 0
    try:
        for batch_number, batch in enumerate(_batches(pending, batch_size), start=1):
            bars: list[dict[str, Any]] = []
            successful: set[str] = set()
            for item, rows, error in _fetch_batch(batch, "ETF", "", workers, pause_seconds, _fetch_etf_from_tdx):
                if error:
                    failures.append({"代码": item["code"], "错误": error})
                    continue
                normalized = _normalize_etf_rows(rows, item["code"], item["name"], item["market"])
                if not normalized:
                    failures.append({"代码": item["code"], "错误": "数据源未返回有效日线"})
                    continue
                bars.extend(normalized)
                successful.add(item["code"])
            if bars:
                written_rows += _persist_batch(store, run_id, "CN", bars, batch_number, asset_type="ETF")
            completed.update(successful)
            failures = [item for item in failures if item["代码"] not in successful]
            result.update({"已完成": len(completed), "待处理": len(universe) - len(completed), "失败": failures[-200:], "本次写入行数": written_rows, "更新时间": _now()})
            _save_state(state_path, result, completed)
            print(f"【全量ETF日线】进度：已完成 {len(completed)}/{len(universe)}，待处理 {len(universe) - len(completed)}。", flush=True)
        result["状态"] = "完成" if not failures else "部分完成"
        store.finish_run(run_id, "PASS" if not failures else "PARTIAL_FAILURE", f"已完成 {len(completed)}/{len(universe)}")
    finally:
        result["更新时间"] = _now()
        _save_state(state_path, result, completed)
        store.close()
    print(f"【全量ETF日线】{result['状态']}：已完成 {len(completed)}/{len(universe)}。", flush=True)
    return result


def _run_market(
    root: Path, market: str, history_days: int, workers: int, batch_size: int, pause_seconds: float, fetcher: FetchFrame
) -> dict[str, Any]:
    universe = _universe(root, market)
    state_path = root / "bulk_stock" / f"{market.lower()}_state.json"
    previous = _load_state(state_path)
    completed = {str(code) for code in previous.get("已完成代码", [])}
    pending = [item for item in universe if item["code"] not in completed]
    start_date = (date.today() - timedelta(days=history_days)).strftime("%Y%m%d")
    result: dict[str, Any] = {
        "状态": "运行中", "市场": "A股" if market == "CN" else "港股",
        "数据源": ("通达信 TCP（A股）" if market == "CN" else "新浪财经（经 AkShare，港股）")
                  if fetcher is _fetch_market_rows else "调用方提供（来源待核验）",
        "总标的": len(universe), "已完成": len(completed), "待处理": len(pending),
        "失败": list(previous.get("失败", []))[-200:], "起始日期": start_date, "更新时间": _now(),
    }
    _save_state(state_path, result, completed)
    print(f"【全量{result['市场']}日线】开始：共 {len(universe)} 个标的，待处理 {len(pending)} 个。", flush=True)
    if not pending:
        result["状态"] = "完成"
        _save_state(state_path, result, completed)
        return result

    store = MarketStore(root)
    run_id = store.begin_run(f"全量{result['市场']}日线")
    failures: list[dict[str, str]] = list(result["失败"])
    written_rows = 0
    try:
        for batch_number, batch in enumerate(_batches(pending, batch_size), start=1):
            bars: list[dict[str, Any]] = []
            successful: set[str] = set()
            for item, rows, error in _fetch_batch(batch, market, start_date, workers, pause_seconds, fetcher):
                if error:
                    failures.append({"代码": item["code"], "错误": error})
                    continue
                source = (("pytdx-full-market-stock" if market == "CN" else "akshare-sina-hk-daily")
                          if fetcher is _fetch_market_rows else "unverified-custom-fetcher")
                normalized = _normalize_rows(rows, market, item["code"], item["name"], source=source)
                if not normalized:
                    failures.append({"代码": item["code"], "错误": "数据源未返回有效日线"})
                    continue
                bars.extend(normalized)
                successful.add(item["code"])
            if bars:
                written_rows += _persist_batch(store, run_id, market, bars, batch_number)
            completed.update(successful)
            failures = [item for item in failures if item["代码"] not in successful]
            result.update({"已完成": len(completed), "待处理": len(universe) - len(completed), "失败": failures[-200:], "本次写入行数": written_rows, "更新时间": _now()})
            _save_state(state_path, result, completed)
            print(f"【全量{result['市场']}日线】进度：已完成 {len(completed)}/{len(universe)}，本次写入 {written_rows} 行，待处理 {len(universe) - len(completed)}。", flush=True)
        result["状态"] = "完成" if not failures else "部分完成"
        store.finish_run(run_id, "PASS" if not failures else "PARTIAL_FAILURE", f"已完成 {len(completed)}/{len(universe)}")
    except Exception as error:
        result["状态"] = "失败"
        result["错误"] = f"{type(error).__name__}: {error}"[:500]
        store.finish_run(run_id, "FAILED", result["错误"])
        raise
    finally:
        result["更新时间"] = _now()
        _save_state(state_path, result, completed)
        store.close()
    print(f"【全量{result['市场']}日线】{result['状态']}：已完成 {len(completed)}/{len(universe)}。", flush=True)
    return result


def _universe(root: Path, market: str) -> list[dict[str, str]]:
    rows = load_a_share_universe(root / "f10" / "cn") if market == "CN" else load_hk_universe(cache_dir=root / "f10" / "hk")
    return sorted(({"code": str(row["code"]), "name": str(row.get("name") or "")} for row in rows), key=lambda item: item["code"])


def _fetch_batch(batch: list[dict[str, str]], market: str, start_date: str, workers: int, pause_seconds: float, fetcher: FetchFrame) -> list[tuple[dict[str, str], list[Mapping[str, Any]], str | None]]:
    def fetch_one(item: dict[str, str]) -> tuple[dict[str, str], list[Mapping[str, Any]], str | None]:
        try:
            return item, list(fetcher(market, item["code"], start_date)), None
        except Exception as error:
            return item, [], f"{type(error).__name__}: {error}"[:300]
        finally:
            time.sleep(pause_seconds)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="全量行情") as pool:
        return list(pool.map(fetch_one, batch))


def _fetch_market_rows(market: str, code: str, start_date: str) -> Iterable[Mapping[str, Any]]:
    if market == "CN":
        return _fetch_cn_from_tdx(code)
    import akshare as ak
    # 新浪源不支持起止日期；只保留最近约 300 个交易日以匹配首轮回填口径。
    frame = ak.stock_hk_daily(symbol=code, adjust="")
    return frame.tail(320).to_dict(orient="records")


def _fetch_cn_from_tdx(code: str) -> Iterable[Mapping[str, Any]]:
    from pytdx.hq import TdxHq_API
    from .collector import _configured_tdx_servers, _tdx_error_type

    market_id = 1 if code.startswith(("60", "68", "90")) else 2 if code.startswith(("43", "83", "87", "88", "92")) else 0
    errors: list[str] = []
    for host, port in _configured_tdx_servers():
        api = TdxHq_API(raise_exception=True)
        try:
            if not api.connect(host, port, time_out=8):
                errors.append(f"{host}:{port} 无法连接")
                continue
            rows = api.get_security_bars(4, market_id, code, 0, 320) or []
            if rows:
                return [
                    {
                        "date": f"{int(row['year']):04d}-{int(row['month']):02d}-{int(row['day']):02d}",
                        "open": row.get("open"), "high": row.get("high"), "low": row.get("low"), "close": row.get("close"),
                        "volume": row.get("vol"), "amount": row.get("amount"),
                    }
                    for row in rows
                ]
            errors.append(f"{host}:{port} 未返回数据")
        except Exception as error:
            errors.append(f"{host}:{port} {_tdx_error_type(error)}")
        finally:
            try:
                api.disconnect()
            except Exception:
                pass
    raise RuntimeError("；".join(errors)[:300])


def _normalize_rows(rows: Iterable[Mapping[str, Any]], market: str, code: str, name: str, *,
                    source: str = "unverified-custom-fetcher") -> list[dict[str, Any]]:
    currency, exchange = ("CNY", _exchange(code)) if market == "CN" else ("HKD", "HKEX")
    normalized: list[dict[str, Any]] = []
    for row in rows:
        raw_date = _first(row, "日期", "date", "Date")
        if raw_date is None:
            continue
        trading_date = str(raw_date)[:10]
        try:
            date.fromisoformat(trading_date)
        except ValueError:
            continue
        close = _number(_first(row, "收盘", "close", "Close"))
        open_price = _number(_first(row, "开盘", "open", "Open"))
        high = _number(_first(row, "最高", "high", "High"))
        low = _number(_first(row, "最低", "low", "Low"))
        volume = _number(_first(row, "成交量", "volume", "Volume"))
        if (any(value is None or not math.isfinite(value) or value <= 0
                for value in (open_price, high, low, close))
                or volume is None or not math.isfinite(volume) or volume < 0):
            continue
        if high < max(open_price, close, low) or low > min(open_price, close, high):
            continue
        bar_time = f"{trading_date}T00:00:00+08:00"
        normalized.append({
            "instrument_id": f"{market}.{exchange}.STOCK.{code}", "symbol": code, "name": name, "trading_date": trading_date,
            "bar_start": bar_time, "bar_end": f"{trading_date}T23:59:59+08:00", "bar_open_time": bar_time, "period": "1d",
            "open": open_price, "high": high, "low": low, "close": close, "pct_change": _number(_first(row, "涨跌幅", "pct_change", "change")),
            "amplitude": (high - low) / low * 100, "volume": volume,
            "amount": _number(_first(row, "成交额", "amount", "Amount")), "open_interest": None, "currency": currency, "adjustment": "",
            "source": source, "source_period": "1d", "fetched_at": _now(), "data_version": "1", "quality_status": "PASS",
        })
    return normalized


def _persist_batch(store: MarketStore, run_id: str, market: str, bars: list[dict[str, Any]], batch_number: int, *, asset_type: str = "STOCK") -> int:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for bar in bars:
        grouped.setdefault(int(bar["trading_date"][:4]), []).append(bar)
    token = uuid4().hex[:8]
    for year, rows in grouped.items():
        key = PartitionKey(market, asset_type, "1d", year, f"{market}-{asset_type}-1d-{token}-{batch_number:05d}-{year}")
        store.write_silver_bars(key, rows, max(row["bar_open_time"] for row in rows), run_id)
    return len(bars)


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    return next((row[key] for key in keys if row.get(key) is not None), None)


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _exchange(code: str) -> str:
    if code.startswith(("60", "68", "90")):
        return "SSE"
    if code.startswith(("43", "83", "87", "88", "92")):
        return "BSE"
    return "SZSE"


def _etf_universe() -> list[dict[str, str | int]]:
    """Read all current domestic exchange-traded funds from the public spot list."""
    try:
        import akshare as ak

        frame = ak.fund_etf_spot_em()
        rows: list[dict[str, str | int]] = []
        for record in frame.to_dict(orient="records"):
            code = str(record.get("代码") or "").zfill(6)
            name = str(record.get("名称") or "").strip()
            if not code.isdigit() or not name:
                continue
            market = 1 if code.startswith(("50", "51", "52", "56", "58")) else 0
            rows.append({"code": code, "name": name, "market": market})
        if len(rows) >= 1000:
            return sorted({str(item["code"]): item for item in rows}.values(), key=lambda item: str(item["code"]))
    except Exception:
        pass

    # Fallback when the public ETF spot list is unavailable.  Prefix filtering
    # prevents ETF-named indices such as 399306 from being treated as funds.
    from pytdx.hq import TdxHq_API
    from .collector import _configured_tdx_servers, _tdx_error_type

    selected: dict[str, dict[str, str | int]] = {}
    error_types: set[str] = set()
    for host, port in _configured_tdx_servers():
        api = TdxHq_API(raise_exception=True)
        try:
            if not api.connect(host, port, time_out=8):
                continue
            for market in (0, 1):
                total = int(api.get_security_count(market) or 0)
                for offset in range(0, total, 1000):
                    for row in api.get_security_list(market, offset) or []:
                        code = str(row.get("code") or "")
                        name = str(row.get("name") or "").strip()
                        if "ETF" not in name.upper() or not code.isdigit() or not code.startswith(("15", "16", "50", "51", "52", "56", "58")):
                            continue
                        selected[code] = {"code": code, "name": name, "market": market}
            if selected:
                return sorted(selected.values(), key=lambda item: str(item["code"]))
        except Exception as error:
            error_types.add(_tdx_error_type(error))
            selected.clear()
        finally:
            try:
                api.disconnect()
            except Exception:
                pass
    raise RuntimeError("通达信未返回 ETF 清单" + ("；错误=" + ",".join(sorted(error_types)) if error_types else ""))


def _fetch_etf_from_tdx(_kind: str, code: str, _start_date: str) -> Iterable[Mapping[str, Any]]:
    from pytdx.hq import TdxHq_API
    from .collector import _configured_tdx_servers, _tdx_error_type

    market = 1 if code.startswith(("50", "51", "52", "56", "58")) else 0
    error_types: set[str] = set()
    for host, port in _configured_tdx_servers():
        api = TdxHq_API(raise_exception=True)
        try:
            if api.connect(host, port, time_out=8):
                rows = api.get_security_bars(4, market, code, 0, 320) or []
                if rows:
                    return [{"date": f"{int(row['year']):04d}-{int(row['month']):02d}-{int(row['day']):02d}", "open": row.get("open"), "high": row.get("high"), "low": row.get("low"), "close": row.get("close"), "volume": row.get("vol"), "amount": row.get("amount")} for row in rows]
        except Exception as error:
            error_types.add(_tdx_error_type(error))
            continue
        finally:
            try:
                api.disconnect()
            except Exception:
                pass
    raise RuntimeError("通达信未返回ETF日线" + ("；错误=" + ",".join(sorted(error_types)) if error_types else ""))


def _normalize_etf_rows(rows: Iterable[Mapping[str, Any]], code: str, name: str, market: int) -> list[dict[str, Any]]:
    normalized = _normalize_rows(rows, "CN", code, name, source="pytdx-full-market-etf")
    exchange = "SSE" if market == 1 else "SZSE"
    for row in normalized:
        row["instrument_id"] = f"CN.{exchange}.ETF.{code}"
    return normalized


def _batches(values: list[dict[str, str]], size: int) -> Iterable[list[dict[str, str]]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, status: Mapping[str, Any], completed: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(status)
    payload["已完成代码"] = sorted(completed)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = ("run_full_etf_backfill", "run_full_stock_backfill",)
