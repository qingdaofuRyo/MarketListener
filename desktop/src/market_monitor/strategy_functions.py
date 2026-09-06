"""Pure, deterministic calculations shared by Indicators and Strategies.

Every function receives all data explicitly and returns newly allocated values.
Unavailable rolling values are represented by ``None`` (or an explicit status
object for structured counts); no function downloads data or reads global state.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Sequence


NumberSeries = Sequence[int | float | None]


class StrategyFunctionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class UpDownCountResult:
    up: int | None
    down: int | None
    flat: int | None
    available: bool
    unavailable_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VolumeProfileBucket:
    bucket_low: float
    bucket_high: float
    volume: float
    share: float
    is_poc: bool
    is_value_area: bool

    def to_dict(self) -> dict[str, float | bool]:
        return {
            "bucketLow": self.bucket_low,
            "bucketHigh": self.bucket_high,
            "volume": self.volume,
            "share": self.share,
            "isPoc": self.is_poc,
            "isValueArea": self.is_value_area,
        }


@dataclass(frozen=True)
class VolumeProfileResult:
    algorithm_version: str
    range_start_index: int
    range_end_index: int
    input_bar_count: int
    requested_bin_count: int
    effective_bin_count: int
    price_representative: str
    allocation: str
    value_area_percent: float
    total_volume: float
    poc_bucket_index: int
    value_area_volume: float
    buckets: tuple[VolumeProfileBucket, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithmVersion": self.algorithm_version,
            "range": {
                "startIndex": self.range_start_index,
                "endIndex": self.range_end_index,
                "inputBarCount": self.input_bar_count,
                "rangeSemantics": "inclusive_bar_indices",
            },
            "requestedBinCount": self.requested_bin_count,
            "effectiveBinCount": self.effective_bin_count,
            "priceRepresentative": self.price_representative,
            "allocation": self.allocation,
            "valueAreaPercent": self.value_area_percent,
            "totalVolume": self.total_volume,
            "pocBucketIndex": self.poc_bucket_index,
            "valueAreaVolume": self.value_area_volume,
            "buckets": [item.to_dict() for item in self.buckets],
        }


def _window(value: Any, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value or value < minimum:
        raise StrategyFunctionError("INVALID_PARAMETER", f"lookback must be an integer >= {minimum}")
    return int(value)


def _finite(value: int | float | None) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _same_length(*series: NumberSeries) -> None:
    if len({len(values) for values in series}) > 1:
        raise StrategyFunctionError("SERIES_LENGTH", "input series must have equal lengths")


def _rolling(values: NumberSeries, lookback: Any) -> list[list[float] | None]:
    size = _window(lookback)
    result: list[list[float] | None] = [None] * len(values)
    for index in range(size - 1, len(values)):
        parsed = [_finite(value) for value in values[index - size + 1:index + 1]]
        if all(value is not None for value in parsed):
            result[index] = [value for value in parsed if value is not None]
    return result


def sma(values: NumberSeries, lookback: Any) -> list[float | None]:
    size = _window(lookback)
    return [sum(window) / size if window is not None else None for window in _rolling(values, size)]


def ema(values: NumberSeries, lookback: Any) -> list[float | None]:
    """Standard EMA seeded by an SMA after each missing-data gap."""
    size = _window(lookback)
    factor = 2.0 / (size + 1)
    result: list[float | None] = [None] * len(values)
    seed: list[float] = []
    previous: float | None = None
    for index, raw in enumerate(values):
        value = _finite(raw)
        if value is None:
            seed.clear()
            previous = None
            continue
        if previous is None:
            seed.append(value)
            if len(seed) > size:
                seed.pop(0)
            if len(seed) == size:
                previous = sum(seed) / size
                result[index] = previous
            continue
        previous = value * factor + previous * (1 - factor)
        result[index] = previous
    return result


def highest(values: NumberSeries, lookback: Any) -> list[float | None]:
    return [max(window) if window is not None else None for window in _rolling(values, lookback)]


def lowest(values: NumberSeries, lookback: Any) -> list[float | None]:
    return [min(window) if window is not None else None for window in _rolling(values, lookback)]


def stddev(values: NumberSeries, lookback: Any) -> list[float | None]:
    size = _window(lookback)
    result: list[float | None] = []
    for window in _rolling(values, size):
        if window is None:
            result.append(None)
            continue
        average = sum(window) / size
        result.append(math.sqrt(sum((value - average) ** 2 for value in window) / size))
    return result


def percentage_change(values: NumberSeries, lookback: Any = 1) -> list[float | None]:
    size = _window(lookback)
    result: list[float | None] = [None] * len(values)
    for index in range(size, len(values)):
        current, previous = _finite(values[index]), _finite(values[index - size])
        if current is not None and previous not in (None, 0):
            result[index] = current / previous - 1
    return result


def volume_change(values: NumberSeries, lookback: Any = 1) -> list[float | None]:
    return percentage_change(values, lookback)


def true_range(high: NumberSeries, low: NumberSeries, close: NumberSeries) -> list[float | None]:
    _same_length(high, low, close)
    result: list[float | None] = [None] * len(high)
    for index in range(len(high)):
        high_value, low_value = _finite(high[index]), _finite(low[index])
        if high_value is None or low_value is None:
            continue
        if index == 0:
            result[index] = high_value - low_value
            continue
        previous_close = _finite(close[index - 1])
        if previous_close is not None:
            result[index] = max(high_value - low_value, abs(high_value - previous_close), abs(low_value - previous_close))
    return result


def _wilder_average(values: NumberSeries, lookback: Any) -> list[float | None]:
    """Wilder smoothing, seeded by the first complete arithmetic window."""

    size = _window(lookback)
    result: list[float | None] = [None] * len(values)
    previous: float | None = None
    seed: list[float] = []
    for index, raw in enumerate(values):
        value = _finite(raw)
        if value is None:
            previous = None
            seed.clear()
            continue
        if previous is None:
            seed.append(value)
            if len(seed) > size:
                seed.pop(0)
            if len(seed) == size:
                previous = sum(seed) / size
                result[index] = previous
            continue
        previous = ((size - 1) * previous + value) / size
        result[index] = previous
    return result


def atr(high: NumberSeries, low: NumberSeries, close: NumberSeries, lookback: Any) -> list[float | None]:
    """Wilder ATR, seeded with the arithmetic mean of the first complete window."""
    return _wilder_average(true_range(high, low, close), lookback)


def rsi(close: NumberSeries, lookback: Any) -> list[float | None]:
    _window(lookback)
    gains: list[float | None] = [None]
    losses: list[float | None] = [None]
    for index in range(1, len(close)):
        current, previous = _finite(close[index]), _finite(close[index - 1])
        if current is None or previous is None:
            gains.append(None)
            losses.append(None)
        else:
            change = current - previous
            gains.append(max(change, 0.0))
            losses.append(max(-change, 0.0))
    average_gain = _wilder_average(gains, lookback)
    average_loss = _wilder_average(losses, lookback)
    result: list[float | None] = []
    for gain, loss in zip(average_gain, average_loss, strict=True):
        if gain is None or loss is None:
            result.append(None)
        elif loss == 0:
            result.append(100.0 if gain > 0 else 50.0)
        else:
            result.append(100 - 100 / (1 + gain / loss))
    return result


def chaikin_volatility(
    high: NumberSeries,
    low: NumberSeries,
    ema_lookback: Any = 10,
    change_lookback: Any = 10,
) -> list[float | None]:
    """Percent change of an EMA of the current high-low range.

    This is the published Chaikin Volatility variant: ``100 * (EMA(H-L)_t /
    EMA(H-L)_(t-m) - 1)``.  It deliberately uses neither volume nor future
    bars and leaves a zero historical range unavailable instead of inventing a
    percentage.
    """

    _same_length(high, low)
    change_size = _window(change_lookback)
    ranges = [
        high_value - low_value if high_value is not None and low_value is not None else None
        for high_value, low_value in zip(
            (_finite(value) for value in high),
            (_finite(value) for value in low),
            strict=True,
        )
    ]
    average = ema(ranges, ema_lookback)
    result: list[float | None] = [None] * len(average)
    for index in range(change_size, len(average)):
        current, previous = average[index], average[index - change_size]
        if current is not None and previous not in (None, 0):
            result[index] = (current / previous - 1) * 100.0
    return result


def atr_percent(
    high: NumberSeries,
    low: NumberSeries,
    close: NumberSeries,
    lookback: Any = 21,
) -> list[float | None]:
    """Publicly reproducible ATR percentage: ``100 * Wilder ATR / close``.

    Twiggs Volatility is proprietary.  This function intentionally identifies
    the published, reproducible ATR-percent variant rather than claiming that
    it reproduces the proprietary Twiggs® implementation.
    """

    ranges = atr(high, low, close, lookback)
    return [
        value / price * 100.0 if value is not None and price not in (None, 0) else None
        for value, price in zip(ranges, (_finite(value) for value in close), strict=True)
    ]


def relative_volatility_index(
    close: NumberSeries,
    stddev_lookback: Any = 10,
    smooth_lookback: Any = 14,
) -> list[float | None]:
    """Dorsey's 1993 close-based Relative Volatility Index (0--100)."""

    _window(stddev_lookback)
    _window(smooth_lookback)
    deviations = stddev(close, stddev_lookback)
    up: list[float | None] = [None] * len(close)
    down: list[float | None] = [None] * len(close)
    for index in range(1, len(close)):
        current, previous, deviation = _finite(close[index]), _finite(close[index - 1]), deviations[index]
        if current is None or previous is None or deviation is None:
            continue
        up[index] = deviation if current > previous else 0.0
        down[index] = deviation if current < previous else 0.0
    average_up = _wilder_average(up, smooth_lookback)
    average_down = _wilder_average(down, smooth_lookback)
    result: list[float | None] = [None] * len(close)
    for index, (up_value, down_value) in enumerate(zip(average_up, average_down, strict=True)):
        if up_value is None or down_value is None:
            continue
        total = up_value + down_value
        result[index] = up_value / total * 100.0 if total else 50.0
    return result


def volume_profile(
    high: NumberSeries,
    low: NumberSeries,
    close: NumberSeries,
    volume: NumberSeries,
    bins: Any = 24,
    value_area_percent: Any = 70.0,
    range_start: Any = 0,
    range_end: Any | None = None,
) -> VolumeProfileResult:
    """Allocate each selected bar's full volume to its HLC3 price bucket.

    The deterministic single-price allocation is deliberately stated in the
    returned metadata.  It guarantees that bucket volume equals the sum of the
    selected bar ``volume`` values, without synthesising volume from amount.
    """

    _same_length(high, low, close, volume)
    if isinstance(bins, bool) or not isinstance(bins, (int, float)) or int(bins) != bins:
        raise StrategyFunctionError("INVALID_PARAMETER", "bins must be an integer")
    requested_bins = int(bins)
    if not 1 <= requested_bins <= 2_000:
        raise StrategyFunctionError("INVALID_PARAMETER", "bins must be between 1 and 2000")
    if isinstance(value_area_percent, bool) or not isinstance(value_area_percent, (int, float)):
        raise StrategyFunctionError("INVALID_PARAMETER", "value_area_percent must be a number")
    value_area = float(value_area_percent)
    if not math.isfinite(value_area) or not 0 < value_area <= 100:
        raise StrategyFunctionError("INVALID_PARAMETER", "value_area_percent must be between 0 and 100")
    if isinstance(range_start, bool) or not isinstance(range_start, (int, float)) or int(range_start) != range_start:
        raise StrategyFunctionError("INVALID_PARAMETER", "range_start must be an integer")
    start = int(range_start)
    end_value = len(high) - 1 if range_end is None else range_end
    if isinstance(end_value, bool) or not isinstance(end_value, (int, float)) or int(end_value) != end_value:
        raise StrategyFunctionError("INVALID_PARAMETER", "range_end must be an integer")
    end = int(end_value)
    if not high or start < 0 or end < start or end >= len(high):
        raise StrategyFunctionError("INVALID_PARAMETER", "range must select at least one input bar")

    selected: list[tuple[float, float, float, float]] = []
    for index in range(start, end + 1):
        high_value = _finite(high[index])
        low_value = _finite(low[index])
        close_value = _finite(close[index])
        volume_value = _finite(volume[index])
        if None in (high_value, low_value, close_value, volume_value):
            raise StrategyFunctionError("MISSING_FIELD", "volume profile requires high, low, close and volume for every selected bar")
        if high_value < low_value or volume_value < 0:
            raise StrategyFunctionError("INVALID_PARAMETER", "selected bar has invalid high, low or volume")
        selected.append((high_value, low_value, close_value, volume_value))
    total_volume = sum(item[3] for item in selected)
    if total_volume <= 0:
        raise StrategyFunctionError("NO_VOLUME", "selected bars contain no positive volume")

    minimum = min(item[1] for item in selected)
    maximum = max(item[0] for item in selected)
    effective_bins = 1 if minimum == maximum else requested_bins
    width = (maximum - minimum) / effective_bins if effective_bins > 1 else 0.0
    bucket_volumes = [0.0] * effective_bins
    for high_value, low_value, close_value, volume_value in selected:
        representative = (high_value + low_value + close_value) / 3.0
        index = 0 if width == 0 else min(effective_bins - 1, int((representative - minimum) / width))
        bucket_volumes[index] += volume_value
    poc_index = max(range(effective_bins), key=lambda index: (bucket_volumes[index], -index))
    value_area_indexes = {poc_index}
    value_area_volume = bucket_volumes[poc_index]
    lower, upper = poc_index - 1, poc_index + 1
    target_volume = total_volume * value_area / 100.0
    while value_area_volume < target_volume and (lower >= 0 or upper < effective_bins):
        lower_volume = bucket_volumes[lower] if lower >= 0 else -1.0
        upper_volume = bucket_volumes[upper] if upper < effective_bins else -1.0
        if lower_volume >= upper_volume:
            selected_index = lower
            lower -= 1
        else:
            selected_index = upper
            upper += 1
        value_area_indexes.add(selected_index)
        value_area_volume += bucket_volumes[selected_index]
    buckets = tuple(
        VolumeProfileBucket(
            bucket_low=minimum + width * index,
            bucket_high=maximum if index == effective_bins - 1 else minimum + width * (index + 1),
            volume=bucket_volumes[index],
            share=bucket_volumes[index] / total_volume * 100.0,
            is_poc=index == poc_index,
            is_value_area=index in value_area_indexes,
        )
        for index in range(effective_bins)
    )
    return VolumeProfileResult(
        algorithm_version="volume_profile_typical_price_v1",
        range_start_index=start,
        range_end_index=end,
        input_bar_count=len(selected),
        requested_bin_count=requested_bins,
        effective_bin_count=effective_bins,
        price_representative="HLC3",
        allocation="full_bar_volume_to_hlc3_bucket",
        value_area_percent=value_area,
        total_volume=total_volume,
        poc_bucket_index=poc_index,
        value_area_volume=value_area_volume,
        buckets=buckets,
    )


def macd(close: NumberSeries, fast: Any = 12, slow: Any = 26, signal: Any = 9) -> dict[str, list[float | None]]:
    fast_size, slow_size, signal_size = _window(fast), _window(slow), _window(signal)
    if fast_size >= slow_size:
        raise StrategyFunctionError("INVALID_PARAMETER", "fast period must be smaller than slow period")
    fast_line, slow_line = ema(close, fast_size), ema(close, slow_size)
    line = [left - right if left is not None and right is not None else None for left, right in zip(fast_line, slow_line, strict=True)]
    signal_line = ema(line, signal_size)
    histogram = [left - right if left is not None and right is not None else None for left, right in zip(line, signal_line, strict=True)]
    return {"macd": line, "signal": signal_line, "histogram": histogram}


def bollinger(close: NumberSeries, lookback: Any = 20, multiplier: Any = 2.0) -> dict[str, list[float | None]]:
    factor = float(multiplier)
    if not math.isfinite(factor) or factor <= 0:
        raise StrategyFunctionError("INVALID_PARAMETER", "multiplier must be a positive finite number")
    middle, deviation = sma(close, lookback), stddev(close, lookback)
    upper = [mean + factor * sd if mean is not None and sd is not None else None for mean, sd in zip(middle, deviation, strict=True)]
    lower = [mean - factor * sd if mean is not None and sd is not None else None for mean, sd in zip(middle, deviation, strict=True)]
    return {"middle": middle, "upper": upper, "lower": lower}


def keltner(
    high: NumberSeries,
    low: NumberSeries,
    close: NumberSeries,
    ema_lookback: Any = 20,
    atr_lookback: Any = 10,
    multiplier: Any = 2.0,
) -> dict[str, list[float | None]]:
    factor = float(multiplier)
    if not math.isfinite(factor) or factor <= 0:
        raise StrategyFunctionError("INVALID_PARAMETER", "multiplier must be a positive finite number")
    middle, ranges = ema(close, ema_lookback), atr(high, low, close, atr_lookback)
    upper = [mean + factor * width if mean is not None and width is not None else None for mean, width in zip(middle, ranges, strict=True)]
    lower = [mean - factor * width if mean is not None and width is not None else None for mean, width in zip(middle, ranges, strict=True)]
    return {"middle": middle, "upper": upper, "lower": lower}


def donchian(high: NumberSeries, low: NumberSeries, lookback: Any = 20) -> dict[str, list[float | None]]:
    _same_length(high, low)
    upper, lower = highest(high, lookback), lowest(low, lookback)
    middle = [(left + right) / 2 if left is not None and right is not None else None for left, right in zip(upper, lower, strict=True)]
    return {"upper": upper, "middle": middle, "lower": lower}


def stochastic(
    high: NumberSeries, low: NumberSeries, close: NumberSeries, lookback: Any = 14, smooth: Any = 3,
) -> dict[str, list[float | None]]:
    _same_length(high, low, close)
    upper, lower = highest(high, lookback), lowest(low, lookback)
    percent_k = [
        (value - bottom) / (top - bottom) * 100 if value is not None and top is not None and bottom is not None and top != bottom else None
        for value, top, bottom in zip((_finite(value) for value in close), upper, lower, strict=True)
    ]
    return {"k": percent_k, "d": sma(percent_k, smooth)}


def crossover(left: NumberSeries, right: NumberSeries) -> list[bool | None]:
    _same_length(left, right)
    result: list[bool | None] = [None] * len(left)
    for index in range(1, len(left)):
        values = tuple(_finite(value) for value in (left[index - 1], right[index - 1], left[index], right[index]))
        if all(value is not None for value in values):
            previous_left, previous_right, current_left, current_right = values
            result[index] = bool(previous_left <= previous_right and current_left > current_right)  # type: ignore[operator]
    return result


def crossunder(left: NumberSeries, right: NumberSeries) -> list[bool | None]:
    _same_length(left, right)
    result: list[bool | None] = [None] * len(left)
    for index in range(1, len(left)):
        values = tuple(_finite(value) for value in (left[index - 1], right[index - 1], left[index], right[index]))
        if all(value is not None for value in values):
            previous_left, previous_right, current_left, current_right = values
            result[index] = bool(previous_left >= previous_right and current_left < current_right)  # type: ignore[operator]
    return result


def up_down_count(close: NumberSeries, lookback: Any) -> list[UpDownCountResult]:
    size = _window(lookback)
    result: list[UpDownCountResult] = []
    for index in range(len(close)):
        if index < size:
            result.append(UpDownCountResult(None, None, None, False, "INSUFFICIENT_DATA"))
            continue
        values = [_finite(value) for value in close[index - size:index + 1]]
        if any(value is None for value in values):
            result.append(UpDownCountResult(None, None, None, False, "MISSING_OR_NAN_BAR"))
            continue
        pairs = list(zip(values[:-1], values[1:], strict=True))
        up = sum(current > previous for previous, current in pairs)  # type: ignore[operator]
        down = sum(current < previous for previous, current in pairs)  # type: ignore[operator]
        result.append(UpDownCountResult(up, down, size - up - down, True))
    return result


_UNITS = {"yuan": 1.0, "ten_thousand_yuan": 10_000.0, "hundred_million_yuan": 100_000_000.0}
_OPERATORS: Mapping[str, Callable[[float, float], bool]] = {
    "gte": lambda left, right: left >= right,
    "lte": lambda left, right: left <= right,
    "gt": lambda left, right: left > right,
    "eq": lambda left, right: left == right,
    "lt": lambda left, right: left < right,
    "ne": lambda left, right: left != right,
}


def market_cap(value_yuan: int | float | None, operator: str, threshold: int | float, unit: str = "yuan") -> bool | None:
    value, target = _finite(value_yuan), _finite(threshold)
    if operator not in _OPERATORS or unit not in _UNITS or target is None or target < 0:
        raise StrategyFunctionError("INVALID_PARAMETER", "market-cap operator, threshold or unit is invalid")
    return None if value is None else _OPERATORS[operator](value, target * _UNITS[unit])


STRATEGY_FUNCTION_IMPLEMENTATIONS: Mapping[str, Callable[..., Any]] = {
    "technical.sma": sma,
    "technical.ema": ema,
    "technical.highest": highest,
    "technical.lowest": lowest,
    "technical.stddev": stddev,
    "technical.rsi": rsi,
    "technical.atr": atr,
    "technical.macd": macd,
    "technical.bollinger": bollinger,
    "technical.keltner": keltner,
    "technical.donchian": donchian,
    "technical.stochastic": stochastic,
    "volatility.chaikin_volatility": chaikin_volatility,
    "volatility.atr_percent": atr_percent,
    "volatility.relative_volatility_index": relative_volatility_index,
    "market.volume_profile": volume_profile,
    "condition.crossover": crossover,
    "condition.crossunder": crossunder,
    "price.percentage_change": percentage_change,
    "volume.change": volume_change,
    "volatility.true_range": true_range,
    "market.up_down_count": up_down_count,
    "market.market_cap": market_cap,
}

# Version is part of a Strategy Definition call.  The legacy map remains for
# direct library callers, while AST/backtest execution must select this map by
# its exact published ``id@version``.
VERSIONED_STRATEGY_FUNCTION_IMPLEMENTATIONS: Mapping[tuple[str, int], Callable[..., Any]] = {
    **{(function_id, 1): implementation for function_id, implementation in STRATEGY_FUNCTION_IMPLEMENTATIONS.items()},
    ("technical.atr", 2): atr,
}


def execute_strategy_function(function_id: str, *args: Any, version: int | None = None) -> Any:
    """Execute a pure strategy function, optionally pinned to one version.

    Omitting ``version`` preserves the old library-level API.  Strategy ASTs
    always provide one and therefore cannot fall through to a newer release.
    """

    try:
        implementation = (
            STRATEGY_FUNCTION_IMPLEMENTATIONS[function_id]
            if version is None
            else VERSIONED_STRATEGY_FUNCTION_IMPLEMENTATIONS[(function_id, version)]
        )
    except KeyError as error:
        suffix = "" if version is None else f"@{version}"
        raise StrategyFunctionError("FUNCTION_NOT_FOUND", f"unknown strategy function: {function_id}{suffix}") from error
    return implementation(*args)


__all__ = [
    "STRATEGY_FUNCTION_IMPLEMENTATIONS",
    "VERSIONED_STRATEGY_FUNCTION_IMPLEMENTATIONS",
    "StrategyFunctionError",
    "UpDownCountResult",
    "VolumeProfileBucket",
    "VolumeProfileResult",
    "atr",
    "atr_percent",
    "bollinger",
    "chaikin_volatility",
    "crossover",
    "crossunder",
    "donchian",
    "ema",
    "execute_strategy_function",
    "highest",
    "keltner",
    "lowest",
    "macd",
    "market_cap",
    "percentage_change",
    "relative_volatility_index",
    "rsi",
    "sma",
    "stddev",
    "stochastic",
    "true_range",
    "up_down_count",
    "volume_change",
    "volume_profile",
]
