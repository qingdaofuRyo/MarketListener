"""Transparent, replayable long-short heat calculations for Chinese commodity futures."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from functools import lru_cache
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from .futures import compute_future_capital_deposit


_CONFIG_PATH = Path(__file__).with_name("config") / "futures_long_short_heat.json"
_PRODUCT_FROM_CONTRACT = re.compile(r"^(?P<product>[A-Z]+)\d{3,4}$")
_CONFIG_KEYS = frozenset(
    {
        "schemaVersion",
        "formulaVersion",
        "lookbackTradingDays",
        "timeWeight",
        "defaultUserWeight",
        "userWeight",
        "score",
        "stateBands",
        "fundUnit",
        "neutralThreshold",
        "divergenceThreshold",
        "minFundCoverage",
        "excludeExchanges",
    }
)


@dataclass(frozen=True)
class LongShortHeatConfig:
    formula_version: str
    lookback_trading_days: int
    half_life_trading_days: float
    breadth_weight: float
    fund_weight: float
    user_weight_min: float
    user_weight_max: float
    user_weight_step: float
    score_min: float
    score_max: float
    state_bands: tuple[tuple[float, float, str], ...]
    fund_unit: str
    neutral_threshold: float
    divergence_threshold: float
    min_fund_coverage: float
    exclude_exchanges: frozenset[str]


@dataclass(frozen=True)
class ProductDailySignal:
    trade_date: str
    exchange: str
    product_code: str
    product_return: float | None
    direction: str | None
    deposited_fund: float | None
    return_method: str | None
    return_contract_count: int
    fund_contract_count: int
    unavailable_reason: str | None = None

    @property
    def product_key(self) -> str:
        return f"{self.exchange}.{self.product_code}"


@dataclass(frozen=True)
class FuturesHeatDaily:
    trade_date: str
    total_variety_count: int
    valid_variety_count: int
    missing_variety_count: int
    up_variety_count: int
    down_variety_count: int
    flat_variety_count: int
    fund_valid_variety_count: int
    fund_missing_variety_count: int
    up_fund: float
    down_fund: float
    flat_fund: float
    return_coverage: float
    fund_coverage: float
    breadth_score_daily: float | None
    fund_score_daily: float | None
    breadth_score_10d: float | None
    fund_score_10d: float | None
    divergence: float | None
    is_warmup: bool
    data_quality_status: str
    formula_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "total_variety_count": self.total_variety_count,
            "valid_variety_count": self.valid_variety_count,
            "missing_variety_count": self.missing_variety_count,
            "up_variety_count": self.up_variety_count,
            "down_variety_count": self.down_variety_count,
            "flat_variety_count": self.flat_variety_count,
            "fund_valid_variety_count": self.fund_valid_variety_count,
            "fund_missing_variety_count": self.fund_missing_variety_count,
            "up_fund": self.up_fund,
            "down_fund": self.down_fund,
            "flat_fund": self.flat_fund,
            "return_coverage": self.return_coverage,
            "fund_coverage": self.fund_coverage,
            "breadth_score_daily": self.breadth_score_daily,
            "fund_score_daily": self.fund_score_daily,
            "breadth_score_10d": self.breadth_score_10d,
            "fund_score_10d": self.fund_score_10d,
            "divergence": self.divergence,
            "is_warmup": self.is_warmup,
            "data_quality_status": self.data_quality_status,
            "formula_version": self.formula_version,
        }


@lru_cache(maxsize=8)
def _load_config(path_text: str, mtime_ns: int, size: int) -> LongShortHeatConfig:
    del mtime_ns, size
    payload = json.loads(Path(path_text).read_text(encoding="utf-8"))
    return parse_long_short_heat_config(payload)


def load_long_short_heat_config(path: Path | None = None) -> LongShortHeatConfig:
    """Load and strictly validate the centralized heat configuration."""

    target = path or _CONFIG_PATH
    stat = target.stat()
    return _load_config(str(target), stat.st_mtime_ns, stat.st_size)


def parse_long_short_heat_config(payload: Mapping[str, Any]) -> LongShortHeatConfig:
    unknown = set(payload) - _CONFIG_KEYS
    missing = _CONFIG_KEYS - set(payload)
    if unknown or missing:
        raise ValueError(f"invalid long-short heat config keys: missing={sorted(missing)}, unknown={sorted(unknown)}")
    if payload.get("schemaVersion") != 1:
        raise ValueError("schemaVersion must be 1")
    formula_version = _non_empty_string(payload.get("formulaVersion"), "formulaVersion")
    lookback = _strict_int(payload.get("lookbackTradingDays"), "lookbackTradingDays")
    if lookback <= 0:
        raise ValueError("lookbackTradingDays must be positive")
    time_weight = _strict_mapping(payload.get("timeWeight"), "timeWeight")
    if set(time_weight) != {"method", "halfLifeTradingDays"}:
        raise ValueError("timeWeight must contain only method and halfLifeTradingDays")
    if time_weight.get("method") != "exponential_decay":
        raise ValueError("timeWeight.method must be exponential_decay")
    half_life = _strict_number(time_weight.get("halfLifeTradingDays"), "halfLifeTradingDays")
    if half_life <= 0:
        raise ValueError("halfLifeTradingDays must be positive")
    default_weight = _strict_mapping(payload.get("defaultUserWeight"), "defaultUserWeight")
    if set(default_weight) != {"breadthWeight", "fundWeight"}:
        raise ValueError("defaultUserWeight must contain only breadthWeight and fundWeight")
    breadth_weight = _unit_number(default_weight.get("breadthWeight"), "breadthWeight")
    fund_weight = _unit_number(default_weight.get("fundWeight"), "fundWeight")
    if not math.isclose(breadth_weight + fund_weight, 1.0, abs_tol=1e-12):
        raise ValueError("default user weights must sum to 1")
    user_weight = _strict_mapping(payload.get("userWeight"), "userWeight")
    if set(user_weight) != {"min", "max", "step"}:
        raise ValueError("userWeight must contain only min, max and step")
    user_weight_min = _unit_number(user_weight.get("min"), "userWeight.min")
    user_weight_max = _unit_number(user_weight.get("max"), "userWeight.max")
    user_weight_step = _strict_number(user_weight.get("step"), "userWeight.step")
    if user_weight_min != 0 or user_weight_max != 1:
        raise ValueError("userWeight range must be [0, 1]")
    if not 0 < user_weight_step <= user_weight_max - user_weight_min:
        raise ValueError("userWeight.step must fit inside its range")
    score = _strict_mapping(payload.get("score"), "score")
    if set(score) != {"min", "max"}:
        raise ValueError("score must contain only min and max")
    score_min = _strict_number(score.get("min"), "score.min")
    score_max = _strict_number(score.get("max"), "score.max")
    if score_min != -100 or score_max != 100:
        raise ValueError("score range must be [-100, 100]")
    raw_bands = payload.get("stateBands")
    if not isinstance(raw_bands, list) or not raw_bands:
        raise ValueError("stateBands must be a non-empty list")
    state_bands: list[tuple[float, float, str]] = []
    expected_min = score_min
    for index, raw_band in enumerate(raw_bands):
        band = _strict_mapping(raw_band, f"stateBands[{index}]")
        if set(band) != {"min", "max", "label"}:
            raise ValueError("each state band must contain only min, max and label")
        band_min = _strict_number(band.get("min"), f"stateBands[{index}].min")
        band_max = _strict_number(band.get("max"), f"stateBands[{index}].max")
        label = _non_empty_string(band.get("label"), f"stateBands[{index}].label")
        if not math.isclose(band_min, expected_min, abs_tol=1e-12) or band_max <= band_min:
            raise ValueError("stateBands must be ordered, contiguous and non-empty")
        state_bands.append((band_min, band_max, label))
        expected_min = band_max
    if not math.isclose(expected_min, score_max, abs_tol=1e-12):
        raise ValueError("stateBands must cover the full score range")
    fund_unit = _non_empty_string(payload.get("fundUnit"), "fundUnit")
    neutral_threshold = _strict_number(payload.get("neutralThreshold"), "neutralThreshold")
    if neutral_threshold < 0:
        raise ValueError("neutralThreshold must be non-negative")
    divergence_threshold = _strict_number(payload.get("divergenceThreshold"), "divergenceThreshold")
    if divergence_threshold < 0:
        raise ValueError("divergenceThreshold must be non-negative")
    min_fund_coverage = _unit_number(payload.get("minFundCoverage"), "minFundCoverage")
    exchanges = payload.get("excludeExchanges")
    if not isinstance(exchanges, list) or not all(isinstance(item, str) and item.strip() for item in exchanges):
        raise ValueError("excludeExchanges must be a list of non-empty strings")
    normalized_exchanges = [item.strip().upper() for item in exchanges]
    if len(set(normalized_exchanges)) != len(normalized_exchanges):
        raise ValueError("excludeExchanges must not contain duplicates")
    return LongShortHeatConfig(
        formula_version=formula_version,
        lookback_trading_days=lookback,
        half_life_trading_days=half_life,
        breadth_weight=breadth_weight,
        fund_weight=fund_weight,
        user_weight_min=user_weight_min,
        user_weight_max=user_weight_max,
        user_weight_step=user_weight_step,
        score_min=score_min,
        score_max=score_max,
        state_bands=tuple(state_bands),
        fund_unit=fund_unit,
        neutral_threshold=neutral_threshold,
        divergence_threshold=divergence_threshold,
        min_fund_coverage=min_fund_coverage,
        exclude_exchanges=frozenset(normalized_exchanges),
    )


def exponential_decay_weights(observations: int, half_life: float) -> tuple[float, ...]:
    """Return normalized weights ordered from the latest observation backwards."""

    if observations <= 0:
        raise ValueError("observations must be positive")
    if half_life <= 0:
        raise ValueError("half_life must be positive")
    raw = [2 ** (-lag / half_life) for lag in range(observations)]
    total = sum(raw)
    return tuple(value / total for value in raw)


def compute_product_daily_signals(
    bars: Sequence[Mapping[str, Any]],
    *,
    config: LongShortHeatConfig | None = None,
    trading_days: Sequence[str] | None = None,
) -> list[ProductDailySignal]:
    """Build one product direction and capital-deposit observation per trading day."""

    settings = config or load_long_short_heat_config()
    prepared = [_prepare_bar(bar) for bar in bars]
    prepared = [bar for bar in prepared if bar is not None and bar["exchange"] not in settings.exclude_exchanges]
    observed_days = {str(bar["trade_date"]) for bar in prepared}
    ordered_days = _ordered_days(trading_days or sorted(observed_days))
    previous_day = {day: ordered_days[index - 1] if index else None for index, day in enumerate(ordered_days)}
    by_product_day: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for bar in prepared:
        by_product_day.setdefault((bar["exchange"], bar["product_code"], bar["trade_date"]), []).append(bar)
    signals: list[ProductDailySignal] = []
    for exchange, product_code, trade_day in sorted(by_product_day, key=lambda item: (item[2], item[0], item[1])):
        current = by_product_day[(exchange, product_code, trade_day)]
        prior = by_product_day.get((exchange, product_code, previous_day.get(trade_day)), [])
        product_return, method, return_count, return_reason = _product_return(current, prior)
        deposited_fund, fund_count, fund_reason = _product_deposited_fund(current, trade_day)
        direction = _direction(product_return, settings.neutral_threshold)
        reasons = [reason for reason in (return_reason, fund_reason) if reason]
        signals.append(
            ProductDailySignal(
                trade_date=trade_day,
                exchange=exchange,
                product_code=product_code,
                product_return=product_return,
                direction=direction,
                deposited_fund=deposited_fund,
                return_method=method,
                return_contract_count=return_count,
                fund_contract_count=fund_count,
                unavailable_reason="; ".join(reasons) or None,
            )
        )
    return signals


def compute_futures_long_short_heat(
    bars: Sequence[Mapping[str, Any]],
    *,
    config: LongShortHeatConfig | None = None,
    trading_days: Sequence[str] | None = None,
    expected_products_by_day: Mapping[str, Sequence[str]] | None = None,
) -> list[FuturesHeatDaily]:
    """Compute daily and exponentially smoothed heat without future observations."""

    settings = config or load_long_short_heat_config()
    signals = compute_product_daily_signals(bars, config=settings, trading_days=trading_days)
    by_day: dict[str, list[ProductDailySignal]] = {}
    for signal in signals:
        by_day.setdefault(signal.trade_date, []).append(signal)
    days = _ordered_days(trading_days or sorted(by_day))
    expected = expected_products_by_day or {}
    daily = [_daily_snapshot(day, by_day.get(day, []), expected.get(day), settings) for day in days]
    output: list[FuturesHeatDaily] = []
    for index, snapshot in enumerate(daily):
        start = max(0, index - settings.lookback_trading_days + 1)
        window = daily[start:index + 1]
        breadth_10d = _smoothed_value(window, "breadth_score_daily", settings)
        fund_10d = _smoothed_value(window, "fund_score_daily", settings)
        divergence = breadth_10d - fund_10d if breadth_10d is not None and fund_10d is not None else None
        breadth_observations = sum(item.breadth_score_daily is not None for item in window)
        fund_observations = sum(item.fund_score_daily is not None for item in window)
        is_warmup = (
            breadth_observations < settings.lookback_trading_days
            or fund_observations < settings.lookback_trading_days
        )
        if snapshot.breadth_score_daily is None:
            status = "UNAVAILABLE"
        elif (
            snapshot.fund_score_daily is None
            or snapshot.missing_variety_count
            or snapshot.fund_missing_variety_count
            or is_warmup
        ):
            status = "PARTIAL"
        else:
            status = "PASS"
        output.append(
            replace(
                snapshot,
                breadth_score_10d=breadth_10d,
                fund_score_10d=fund_10d,
                divergence=divergence,
                is_warmup=is_warmup,
                data_quality_status=status,
            )
        )
    return output


def _daily_snapshot(
    trade_day: str,
    signals: Sequence[ProductDailySignal],
    expected_products: Sequence[str] | None,
    config: LongShortHeatConfig,
) -> FuturesHeatDaily:
    expected = {
        str(item).strip().upper()
        for item in (expected_products or ())
        if str(item).strip().upper().split(".", 1)[0] not in config.exclude_exchanges
    }
    observed = {signal.product_key for signal in signals}
    total = len(expected or observed)
    valid = [signal for signal in signals if signal.direction is not None]
    valid_count = len(valid)
    missing = max(0, total - valid_count)
    up = [signal for signal in valid if signal.direction == "UP"]
    down = [signal for signal in valid if signal.direction == "DOWN"]
    flat = [signal for signal in valid if signal.direction == "FLAT"]
    fund_valid = [signal for signal in valid if signal.deposited_fund is not None]
    fund_valid_count = len(fund_valid)
    fund_missing = valid_count - fund_valid_count
    return_coverage = valid_count / total if total else 0.0
    fund_coverage = fund_valid_count / valid_count if valid_count else 0.0
    up_fund = sum(signal.deposited_fund or 0.0 for signal in up if signal.deposited_fund is not None)
    down_fund = sum(signal.deposited_fund or 0.0 for signal in down if signal.deposited_fund is not None)
    flat_fund = sum(signal.deposited_fund or 0.0 for signal in flat if signal.deposited_fund is not None)
    directional_count = len(up) + len(down)
    breadth_score = (len(up) - len(down)) / directional_count * 100 if directional_count else None
    directional_fund = up_fund + down_fund
    fund_score = None
    if fund_coverage >= config.min_fund_coverage and directional_fund > 0:
        fund_score = (up_fund - down_fund) / directional_fund * 100
    return FuturesHeatDaily(
        trade_date=trade_day,
        total_variety_count=total,
        valid_variety_count=valid_count,
        missing_variety_count=missing,
        up_variety_count=len(up),
        down_variety_count=len(down),
        flat_variety_count=len(flat),
        fund_valid_variety_count=fund_valid_count,
        fund_missing_variety_count=fund_missing,
        up_fund=up_fund,
        down_fund=down_fund,
        flat_fund=flat_fund,
        return_coverage=return_coverage,
        fund_coverage=fund_coverage,
        breadth_score_daily=breadth_score,
        fund_score_daily=fund_score,
        breadth_score_10d=None,
        fund_score_10d=None,
        divergence=None,
        is_warmup=True,
        data_quality_status="UNAVAILABLE",
        formula_version=config.formula_version,
    )


def _product_return(
    current: Sequence[dict[str, Any]], prior: Sequence[dict[str, Any]]
) -> tuple[float | None, str | None, int, str | None]:
    current_weighted = [bar for bar in current if bar["series_kind"] == "WEIGHTED"]
    prior_weighted = [bar for bar in prior if bar["series_kind"] == "WEIGHTED"]
    if len(current_weighted) > 1 or len(prior_weighted) > 1:
        raise ValueError("standardized product input must contain at most one WEIGHTED row per day")
    if current_weighted and prior_weighted:
        current_price = _positive_number(current_weighted[0].get("settlement"))
        prior_price = _positive_number(prior_weighted[0].get("settlement"))
        if current_price is not None and prior_price is not None:
            return math.log(current_price / prior_price), "WEIGHTED_SETTLEMENT", 0, None
    prior_contracts = {bar["contract_code"]: bar for bar in prior if bar["series_kind"] == "CONTRACT"}
    components: list[tuple[float, float]] = []
    for bar in current:
        if bar["series_kind"] != "CONTRACT" or not bar["is_active"]:
            continue
        previous = prior_contracts.get(bar["contract_code"])
        settlement = _positive_number(bar.get("settlement"))
        previous_settlement = _positive_number(previous.get("settlement")) if previous else None
        open_interest = _positive_number(bar.get("open_interest"))
        if settlement is None or previous_settlement is None or open_interest is None:
            continue
        multiplier = _positive_number(bar.get("contract_multiplier"))
        if multiplier is None:
            continue
        components.append(
            (math.log(settlement / previous_settlement), settlement * multiplier * open_interest)
        )
    total_weight = sum(weight for _, weight in components)
    if not components or total_weight <= 0:
        return None, None, 0, "缺少可计算的WEIGHTED或具备结算价、乘数、持仓量的有效月份合约收益"
    value = sum(contract_return * weight for contract_return, weight in components) / total_weight
    return value, "CONTRACT_MARKET_VALUE_WEIGHTED", len(components), None


def _product_deposited_fund(
    current: Sequence[dict[str, Any]], trade_day: str
) -> tuple[float | None, int, str | None]:
    contracts = [bar for bar in current if bar["series_kind"] == "CONTRACT" and bar["is_active"]]
    if not contracts:
        return None, 0, "缺少有效月份合约，无法计算沉淀资金"
    values: list[float] = []
    for bar in contracts:
        settlement = _positive_number(bar.get("settlement"))
        open_interest = _non_negative_number(bar.get("open_interest"))
        multiplier = _positive_number(bar.get("contract_multiplier"))
        margin_rate = _positive_number(bar.get("margin_rate"))
        if settlement is None or open_interest is None or multiplier is None or margin_rate is None or margin_rate > 1:
            return None, 0, "有效月份合约缺少结算价、持仓量、合约乘数或保证金率"
        values.append(
            compute_future_capital_deposit(
                instrument_id=bar["contract_code"],
                trading_day=date.fromisoformat(trade_day),
                price=settlement,
                open_interest=open_interest,
                contract_multiplier=multiplier,
                margin_rate=margin_rate,
                side_count=2,
            ).value
        )
    return sum(values), len(values), None


def _smoothed_value(
    window: Sequence[FuturesHeatDaily], field: str, config: LongShortHeatConfig
) -> float | None:
    current = getattr(window[-1], field) if window else None
    if current is None:
        return None
    raw = [2 ** (-lag / config.half_life_trading_days) for lag in range(len(window))]
    available = [
        (float(value), raw[lag])
        for lag, snapshot in enumerate(reversed(window))
        if (value := getattr(snapshot, field)) is not None
    ]
    weight_total = sum(weight for _, weight in available)
    if not weight_total:
        return None
    score = sum(value * weight for value, weight in available) / weight_total
    # Convex combinations can exceed an exact boundary by a few ulps.
    return max(config.score_min, min(config.score_max, score))


def _prepare_bar(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    trade_day = str(raw.get("trade_date") or raw.get("trading_day") or raw.get("trading_date") or "")[:10]
    try:
        date.fromisoformat(trade_day)
    except ValueError:
        return None
    exchange = str(raw.get("exchange") or "").strip().upper()
    series_kind = str(raw.get("series_kind") or raw.get("seriesKind") or raw.get("futures_series_kind") or "").upper()
    if not exchange or series_kind not in {"WEIGHTED", "CONTRACT"}:
        return None
    contract_code = str(raw.get("contract_code") or raw.get("symbol") or raw.get("instrument_id") or "").strip().upper()
    product_code = str(raw.get("product_code") or raw.get("productCode") or "").strip().upper()
    if not product_code and series_kind == "CONTRACT":
        match = _PRODUCT_FROM_CONTRACT.fullmatch(contract_code.split(".")[-1])
        product_code = match.group("product") if match else ""
    if not product_code:
        return None
    return {
        "trade_date": trade_day,
        "exchange": exchange,
        "product_code": product_code,
        "series_kind": series_kind,
        "contract_code": contract_code or f"{exchange}.{product_code}.{series_kind}",
        "settlement": raw.get("settlement"),
        "open_interest": raw.get("open_interest"),
        "contract_multiplier": raw.get("contract_multiplier") or raw.get("contractMultiplier"),
        "margin_rate": raw.get("margin_rate") or raw.get("marginRate"),
        "is_active": raw.get("is_active", True) is not False and raw.get("is_expired", False) is not True,
    }


def _direction(value: float | None, threshold: float) -> str | None:
    if value is None:
        return None
    if value > threshold:
        return "UP"
    if value < -threshold:
        return "DOWN"
    return "FLAT"


def _ordered_days(values: Iterable[str]) -> list[str]:
    days = sorted({str(value)[:10] for value in values})
    for value in days:
        date.fromisoformat(value)
    return days


def _strict_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _strict_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def _strict_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{field} must be a finite number")
    return float(value)


def _unit_number(value: Any, field: str) -> float:
    number = _strict_number(value, field)
    if not 0 <= number <= 1:
        raise ValueError(f"{field} must be in [0, 1]")
    return number


def _non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _positive_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _non_negative_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None
