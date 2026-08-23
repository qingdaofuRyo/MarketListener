"""期货数据模块：合约/主力/加权/指数四类区分与沉淀资金。

规则（架构调整任务第五、六节）：
- FUTURE_CONTRACT：固定合约（如 rb2610）；
- FUTURE_MAIN：主力合约连续（按持仓量/成交量/近月选择）；
- FUTURE_WEIGHTED：加权连续（按各合约持仓量加权合成）；
- FUTURE_INDEX：商品指数（同花顺/文华等独立指数行情）。
四类数据不得混用。

沉淀资金：国内 Open Interest 按单边统计（1 手多 + 1 手空 = OI 1），
但沉淀资金 = 多头冻结保证金 + 空头冻结保证金，因此公式为：
    沉淀资金 = 持仓量 × 合约乘数 × 价格 × 保证金比例 × 2（side_count=2）。
结果必须保存 formula_version / contract_multiplier / margin_rate /
open_interest / calculation_time，便于日后口径核对。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from functools import lru_cache
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


class FuturesSeriesKind(str, Enum):
    CONTRACT = "CONTRACT"
    MAIN = "MAIN"
    WEIGHTED = "WEIGHTED"
    INDEX = "INDEX"


SERIES_KINDS = frozenset(kind.value for kind in FuturesSeriesKind)


@dataclass(frozen=True)
class FuturesContractSpec:
    symbol: str
    exchange: str
    contract_multiplier: float
    margin_rate: float
    effective_from: date | None = None
    source: str = ""

    def validation_errors(self) -> list[str]:
        errors: list[str] = []
        if self.contract_multiplier <= 0:
            errors.append("contract_multiplier must be > 0")
        if not 0 < self.margin_rate <= 1:
            errors.append("margin_rate must be in (0, 1]")
        return errors


@dataclass(frozen=True)
class FuturesContractSpecResolution:
    """A date-effective contract specification, including its audit source."""

    spec: FuturesContractSpec | None
    reason: str | None = None


_CONFIG_PATH = Path(__file__).with_name("config") / "futures_contract_specs.json"
_PRODUCT_NAMES_PATH = Path(__file__).with_name("config") / "futures_product_names.json"
_MONTH_CONTRACT = re.compile(r"^(?P<product>[A-Z]+)(?P<delivery>\d{3,4})$", re.I)


@lru_cache(maxsize=16)
def _load_contract_specs(path_text: str, mtime_ns: int, size: int) -> dict[str, Any]:
    del mtime_ns, size
    try:
        payload = json.loads(Path(path_text).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


@lru_cache(maxsize=1)
def _load_product_names() -> dict[str, str]:
    try:
        payload = json.loads(_PRODUCT_NAMES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key).upper(): str(value).strip() for key, value in payload.items() if str(value).strip()}


def resolve_futures_contract_spec(
    product_code: str,
    exchange: str,
    trading_day: date,
    *,
    config_path: Path | None = None,
) -> FuturesContractSpecResolution:
    """Resolve the latest spec effective on ``trading_day``.

    Rates are deliberately configuration-backed because exchanges can change
    them independently of application releases.  Missing or incomplete
    records return a user-facing reason rather than fabricating a value.
    """

    product = str(product_code or "").strip().upper()
    venue = str(exchange or "").strip().upper()
    if not product:
        return FuturesContractSpecResolution(None, "缺少期货品种代码")
    path = config_path or _CONFIG_PATH
    try:
        stat = path.stat()
        payload = _load_contract_specs(str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        payload = {}
    products = payload.get("products") if isinstance(payload.get("products"), dict) else {}
    configured = products.get(f"{venue}.{product}") or products.get(product)
    if not isinstance(configured, dict):
        return FuturesContractSpecResolution(None, f"未配置{venue or '未知交易所'}.{product}合约规格")
    records = configured.get("records")
    if not isinstance(records, list):
        return FuturesContractSpecResolution(None, f"{venue}.{product}合约规格缺少生效记录")
    candidates: list[tuple[date, Mapping[str, Any]]] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        try:
            effective = date.fromisoformat(str(record.get("effectiveFrom")))
        except (TypeError, ValueError):
            continue
        if effective <= trading_day:
            candidates.append((effective, record))
    if not candidates:
        return FuturesContractSpecResolution(None, f"{venue}.{product}在{trading_day.isoformat()}无生效合约规格")
    effective, record = max(candidates, key=lambda item: item[0])
    try:
        multiplier = float(record["contractMultiplier"])
    except (KeyError, TypeError, ValueError):
        return FuturesContractSpecResolution(None, f"{venue}.{product}缺少合约乘数")
    try:
        margin_rate = float(record["marginRate"])
    except (KeyError, TypeError, ValueError):
        return FuturesContractSpecResolution(None, f"{venue}.{product}缺少保证金率")
    spec = FuturesContractSpec(
        symbol=product,
        exchange=venue,
        contract_multiplier=multiplier,
        margin_rate=margin_rate,
        effective_from=effective,
        source=str(record.get("source") or configured.get("source") or ""),
    )
    errors = spec.validation_errors()
    if errors:
        return FuturesContractSpecResolution(None, f"{venue}.{product}合约规格无效: {'; '.join(errors)}")
    return FuturesContractSpecResolution(spec)


def futures_product_name(
    product_code: str,
    exchange: str,
    *,
    config_path: Path | None = None,
) -> str | None:
    """Return the configured Chinese product name without hard-coding it."""

    product = str(product_code or "").strip().upper()
    venue = str(exchange or "").strip().upper()
    if not product:
        return None
    path = config_path or _CONFIG_PATH
    try:
        stat = path.stat()
        payload = _load_contract_specs(str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None
    products = payload.get("products") if isinstance(payload.get("products"), dict) else {}
    configured = products.get(f"{venue}.{product}") or products.get(product)
    if not isinstance(configured, dict):
        return _load_product_names().get(f"{venue}.{product}")
    name = str(configured.get("name") or "").strip()
    return name or _load_product_names().get(f"{venue}.{product}")


def contract_delivery_month(
    symbol: str,
    exchange: str,
    *,
    reference_day: date,
    last_trading_day: date | None = None,
) -> date | None:
    """Return the first day of a concrete futures delivery month.

    CZCE still exposes some legacy three-digit YMM symbols.  Their decade is
    selected using the contract's latest trading date when available, which
    keeps archived 505 data in 2015/2025 distinct from an active 2035 symbol.
    """

    match = _MONTH_CONTRACT.fullmatch(str(symbol or "").strip().upper())
    if not match:
        return None
    digits = match.group("delivery")
    month = int(digits[-2:])
    if not 1 <= month <= 12:
        return None
    if len(digits) == 4:
        return date(2000 + int(digits[:2]), month, 1)
    if str(exchange or "").upper() != "CZCE":
        return None
    year_digit = int(digits[0])
    anchor = last_trading_day or reference_day
    candidates = [
        date(year, month, 1)
        for year in range(max(2000, anchor.year - 15), anchor.year + 16)
        if year % 10 == year_digit
    ]
    return min(candidates, key=lambda value: (abs((value.year - anchor.year) * 12 + value.month - anchor.month), value)) if candidates else None


def is_expired_futures_contract(
    symbol: str,
    exchange: str,
    *,
    reference_day: date,
    last_trading_day: date | None = None,
) -> bool:
    delivery = contract_delivery_month(
        symbol,
        exchange,
        reference_day=reference_day,
        last_trading_day=last_trading_day,
    )
    return delivery is not None and delivery < reference_day.replace(day=1)


@dataclass(frozen=True)
class FutureCapitalDeposit:
    """一笔沉淀资金计算记录。"""

    instrument_id: str
    trading_day: date
    price: float
    open_interest: float
    contract_multiplier: float
    margin_rate: float
    side_count: int = 2
    formula_version: str = "2026-01-v1"
    calculation_time: str = ""

    @property
    def value(self) -> float:
        return (
            self.open_interest
            * self.contract_multiplier
            * self.price
            * self.margin_rate
            * self.side_count
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "trading_day": self.trading_day.isoformat(),
            "price": self.price,
            "open_interest": self.open_interest,
            "contract_multiplier": self.contract_multiplier,
            "margin_rate": self.margin_rate,
            "side_count": self.side_count,
            "formula_version": self.formula_version,
            "calculation_time": self.calculation_time or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "value": self.value,
            "formula": "OI * contract_multiplier * price * margin_rate * side_count",
        }


def compute_future_capital_deposit(
    *,
    instrument_id: str,
    trading_day: date,
    price: float,
    open_interest: float,
    contract_multiplier: float,
    margin_rate: float,
    side_count: int = 2,
) -> FutureCapitalDeposit:
    """按统一公式计算单笔沉淀资金；拒绝非法参数。"""

    if price <= 0:
        raise ValueError("price must be > 0")
    if open_interest < 0:
        raise ValueError("open_interest must be >= 0")
    if side_count not in (1, 2):
        raise ValueError("side_count must be 1 or 2 (国内期货单边 OI 下按 2 计算多空双方保证金)")
    spec_errors = FuturesContractSpec("", "", contract_multiplier, margin_rate).validation_errors()
    if spec_errors:
        raise ValueError("; ".join(spec_errors))
    return FutureCapitalDeposit(
        instrument_id=instrument_id,
        trading_day=trading_day,
        price=price,
        open_interest=open_interest,
        contract_multiplier=contract_multiplier,
        margin_rate=margin_rate,
        side_count=side_count,
    )


@dataclass(frozen=True)
class WeightedBar:
    """加权连续 bar：以各合约持仓量为权重的 OI 加权合成结果。"""

    trading_day: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    open_interest: float
    contracts: tuple[str, ...]
    weights: tuple[float, ...]
    algorithm_version: int = 1

    def to_bar(self, instrument_id: str) -> dict[str, Any]:
        return {
            "instrument_key": {"country_or_market": "CN", "exchange": "DCE", "asset_type": "FUTURE", "code": instrument_id},
            "period": "1d",
            "trading_day": self.trading_day.isoformat(),
            "bar_open_time": f"{self.trading_day.isoformat()}T09:00:00+08:00",
            "bar_close_time": f"{self.trading_day.isoformat()}T15:00:00+08:00",
            "timezone": "Asia/Shanghai",
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "amount": self.amount,
            "open_interest": self.open_interest,
            "price_mode": "RAW",
            "source": {"provider": "local", "source_symbol": instrument_id, "retrieved_at": ""},
            "source_period": "1d",
            "quality_status": "PASS",
            "weighted_contracts": list(self.contracts),
            "weighted_weights": list(self.weights),
            "weighted_algorithm_version": self.algorithm_version,
        }


def build_weighted_series(
    bars_by_contract_day: Mapping[tuple[date, str], Mapping[str, Any]],
    contracts_by_day: Mapping[date, Sequence[str]],
    *,
    instrument_id: str,
) -> list[WeightedBar]:
    """按每日各合约持仓量加权合成加权连续 bar。

    OHLC 均按持仓量加权（开盘/最高/最低/收盘各取 OI 加权值），
    volume/amount/open_interest 直接求和。换月日不产生跳空修正，
    合成算法版本记录在结果中。
    """

    output: list[WeightedBar] = []
    for trading_day in sorted(contracts_by_day):
        parsed_day = date.fromisoformat(str(trading_day))
        contracts = contracts_by_day[trading_day]
        rows = [(symbol, bars_by_contract_day.get((trading_day, symbol))) for symbol in contracts]
        rows = [(symbol, bar) for symbol, bar in rows if bar is not None]
        if not rows:
            continue
        total_oi = sum(float(bar["open_interest"] or 0) for _, bar in rows)
        if total_oi <= 0:
            weights = tuple(1.0 / len(rows) for _ in rows)
        else:
            weights = tuple(float(bar["open_interest"] or 0) / total_oi for _, bar in rows)
        output.append(
            WeightedBar(
                trading_day=parsed_day,
                open=_weighted([float(bar["open"]) for _, bar in rows], weights),
                high=_weighted([float(bar["high"]) for _, bar in rows], weights),
                low=_weighted([float(bar["low"]) for _, bar in rows], weights),
                close=_weighted([float(bar["close"]) for _, bar in rows], weights),
                volume=sum(float(bar.get("volume", 0)) for _, bar in rows),
                amount=sum(float(bar.get("amount", 0)) for _, bar in rows),
                open_interest=total_oi,
                contracts=tuple(symbol for symbol, _ in rows),
                weights=weights,
            )
        )
    return output


def normalize_futures_bar(row: Mapping[str, Any], kind: FuturesSeriesKind) -> dict[str, Any]:
    """把来源行归一为统一字段，并强制标记系列类型，防止四类混用。"""

    if kind.value not in SERIES_KINDS:
        raise ValueError(f"unknown futures series kind: {kind}")
    bar = {
        "instrument_key": {
            "country_or_market": "CN",
            "exchange": str(row.get("exchange", "SHFE")),
            "asset_type": "FUTURE",
            "code": str(row["code"]),
        },
        "period": str(row.get("period", "1d")),
        "trading_day": str(row["trading_day"]),
        "bar_open_time": str(row["bar_open_time"]),
        "bar_close_time": str(row["bar_close_time"]),
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": float(row.get("volume", 0)),
        "amount": float(row.get("amount", 0)),
        "open_interest": float(row["open_interest"]) if row.get("open_interest") is not None else None,
        "price_mode": "RAW",
        "source": {
            "provider": str(row.get("provider", "unknown")),
            "source_symbol": str(row.get("source_symbol", row["code"])),
            "retrieved_at": str(row.get("retrieved_at", "")),
        },
        "source_period": str(row.get("source_period", str(row.get("period", "1d")))),
        "quality_status": str(row.get("quality_status", "PASS")),
        "futures_series_kind": kind.value,
    }
    if kind is FuturesSeriesKind.MAIN:
        bar["is_roll_day"] = bool(row.get("is_roll_day", False))
        bar["roll_gap"] = float(row.get("roll_gap", 0.0))
    return bar


def _weighted(values: Sequence[float], weights: Sequence[float]) -> float:
    return sum(value * weight for value, weight in zip(values, weights))
