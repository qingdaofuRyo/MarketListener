"""Calculate versioned Indicator Instances from explicit market bars.

The service is deliberately below the web adapter and above the pure Strategy
Function library.  It does not read storage, account state, drawings, or order
capabilities.  Each instance is isolated so one invalid or unavailable
indicator cannot suppress other chart overlays/panes.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping, Sequence

from market_monitor.external_market import ExternalMarketSeries, align_external_series
from market_monitor.formula_engine import hsar_resistance, hsar_support
from market_monitor.indicator_registry import IndicatorDefinition, IndicatorRegistry
from market_monitor.strategy_functions import (
    StrategyFunctionError,
    atr,
    atr_percent,
    bollinger,
    chaikin_volatility,
    donchian,
    ema,
    keltner,
    macd,
    relative_volatility_index,
    rsi,
    sma,
    stddev,
    stochastic,
    volume_profile,
)


NumberSeries = list[float | None]


@dataclass(frozen=True)
class IndicatorCalculationError(ValueError):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _field(bars: Sequence[Mapping[str, Any]], name: str) -> NumberSeries:
    camel = name.split("_")[0] + "".join(part.title() for part in name.split("_")[1:])
    return [_number(bar.get(name, bar.get(camel))) for bar in bars]


def _bar_time(bar: Mapping[str, Any]) -> str | None:
    value = bar.get("barOpenTime", bar.get("bar_open_time", bar.get("tradingDate", bar.get("trading_date"))))
    return str(value) if value is not None else None


def _validated_parameters(
    definition: IndicatorDefinition,
    supplied: Mapping[str, Any],
) -> dict[str, int | float]:
    definitions = {str(item["name"]): item for item in definition.parameters}
    unknown = sorted(set(supplied) - set(definitions))
    if unknown:
        raise IndicatorCalculationError(
            "INVALID_PARAMETER",
            f"unknown parameters for {definition.indicator_id}: {', '.join(unknown)}",
        )
    result: dict[str, int | float] = {}
    for name, item in definitions.items():
        value = supplied.get(name, item.get("default"))
        expected = item.get("type")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise IndicatorCalculationError("INVALID_PARAMETER", f"{name} must be a finite {expected}")
        if expected == "integer" and int(value) != value:
            raise IndicatorCalculationError("INVALID_PARAMETER", f"{name} must be an integer")
        minimum, maximum = item.get("minimum"), item.get("maximum")
        if minimum is not None and value < minimum or maximum is not None and value > maximum:
            raise IndicatorCalculationError(
                "INVALID_PARAMETER",
                f"{name} must be between {minimum} and {maximum}",
            )
        result[name] = int(value) if expected == "integer" else float(value)
    return result


def indicator_warmup(definition: IndicatorDefinition, parameters: Mapping[str, Any]) -> int:
    """Return a conservative pre-roll for the registered definition."""

    values = _validated_parameters(definition, parameters)
    periods = [
        int(value)
        for name, value in values.items()
        if any(token in name.casefold() for token in ("lookback", "fast", "slow", "signal", "smooth"))
    ]
    calculation_id = definition.calculation_id or definition.indicator_id
    if calculation_id in {"indicator.macd"}:
        return int(values["slow"]) + int(values["signal"]) + 2
    if calculation_id in {"indicator.keltner"}:
        return max(int(values["emaLookback"]), int(values["atrLookback"])) + 2
    if calculation_id == "indicator.chaikin_volatility":
        return int(values["emaLookback"]) + int(values["changeLookback"])
    if calculation_id == "indicator.twiggs_volatility":
        return int(values["lookback"])
    if calculation_id == "indicator.rvi":
        return int(values["stddevLookback"]) + int(values["smoothLookback"]) - 1
    if calculation_id in {"indicator.volume_profile", "indicator.vix"}:
        return 0
    return max(periods or [1]) + 2


def calculate_indicator(
    definition: IndicatorDefinition,
    bars: Sequence[Mapping[str, Any]],
    parameters: Mapping[str, Any],
    *,
    range_start: Any = 0,
    range_end: Any | None = None,
) -> dict[str, Any]:
    """Calculate one definition using only shared, deterministic functions."""

    values = _validated_parameters(definition, parameters)
    close, high, low = _field(bars, "close"), _field(bars, "high"), _field(bars, "low")
    indicator_id = definition.calculation_id or definition.indicator_id
    if indicator_id in {"indicator.ma", "ma"}:
        return {"ma": sma(close, values["lookback"])}
    if indicator_id == "indicator.ema":
        return {"ema": ema(close, values["lookback"])}
    if indicator_id in {"indicator.bollinger", "bollinger"}:
        return bollinger(close, values["lookback"], values["multiplier"])
    if indicator_id == "indicator.keltner":
        return keltner(
            high,
            low,
            close,
            values["emaLookback"],
            values["atrLookback"],
            values["multiplier"],
        )
    if indicator_id == "indicator.donchian":
        return donchian(high, low, values["lookback"])
    if indicator_id == "indicator.rsi":
        return {"rsi": rsi(close, values["lookback"])}
    if indicator_id == "indicator.macd":
        return macd(close, values["fast"], values["slow"], values["signal"])
    if indicator_id == "indicator.stochastic":
        return stochastic(high, low, close, values["lookback"], values["smooth"])
    if indicator_id in {"indicator.atr", "atr"}:
        return {"atr": atr(high, low, close, values["lookback"])}
    if indicator_id == "indicator.chaikin_volatility":
        return {
            "value": chaikin_volatility(
                high,
                low,
                values["emaLookback"],
                values["changeLookback"],
            )
        }
    if indicator_id == "indicator.twiggs_volatility":
        return {"value": atr_percent(high, low, close, values["lookback"])}
    if indicator_id == "indicator.rvi":
        return {
            "rvi": relative_volatility_index(
                close,
                values["stddevLookback"],
                values["smoothLookback"],
            )
        }
    if indicator_id == "indicator.volume_profile":
        profile = volume_profile(
            high,
            low,
            close,
            _field(bars, "volume"),
            values["bins"],
            values["valueAreaPercent"],
            range_start,
            range_end,
        )
        return {"__profile__": profile.to_dict()}
    if indicator_id == "sd":
        return {"sd": stddev(close, values["lookback"])}
    if indicator_id == "hsar":
        return {
            "resistance": hsar_resistance(high, values["lookback"], values["topPercent"]),
            "support": hsar_support(low, values["lookback"], values["topPercent"]),
        }
    if indicator_id == "volume":
        return {"volume": _field(bars, "volume")}
    raise IndicatorCalculationError("DISABLED", f"{indicator_id} has no published calculation version")


def calculate_indicator_instance(
    registry: IndicatorRegistry,
    bars: Sequence[Mapping[str, Any]],
    request: Mapping[str, Any],
    *,
    asset_type: str,
    external_series: ExternalMarketSeries | None = None,
) -> dict[str, Any]:
    """Resolve and calculate one isolated instance into a chart-safe payload."""

    instance_id = str(request.get("instanceId") or request.get("instance_id") or "")
    definition_id = str(request.get("definitionId") or request.get("definition_id") or "")
    version = int(request.get("version") or 1)
    parameters = request.get("parameters") if isinstance(request.get("parameters"), Mapping) else {}
    range_start = request.get("rangeStart", request.get("range_start", 0))
    range_end = request.get("rangeEnd", request.get("range_end"))
    visible = bool(request.get("visible", True))
    style = dict(request.get("style") or {}) if isinstance(request.get("style"), Mapping) else {}
    base = {
        "instanceId": instance_id,
        "definitionId": definition_id,
        "version": version,
        "parameters": dict(parameters),
        "style": style,
        "visible": visible,
        "placement": request.get("placement"),
        "series": {},
    }
    try:
        definition = registry.resolve(definition_id, version)
    except KeyError:
        return {
            **base,
            "status": "unavailable",
            "unavailableCode": "DEFINITION_NOT_FOUND",
            "unavailableReason": f"unknown indicator definition: {definition_id}@{version}",
        }
    resolved = {
        **base,
        "displayName": definition.name,
        "englishName": definition.english_name,
        "placement": definition.placement,
        "plots": [dict(plot) for plot in definition.plots],
    }
    if definition.resource_kind != "indicator":
        return {
            **resolved,
            "status": "unavailable",
            "unavailableCode": "DRAWING_TOOL",
            "unavailableReason": f"{definition_id} is a drawing tool, not an indicator instance",
        }
    requested_placement = request.get("placement")
    if requested_placement and requested_placement != definition.placement:
        return {
            **resolved,
            "status": "unavailable",
            "unavailableCode": "INVALID_PARAMETER",
            "unavailableReason": f"{definition_id} must use {definition.placement} placement",
        }
    if definition.status == "disabled":
        return {
            **resolved,
            "status": "unavailable",
            "unavailableCode": definition.unavailable_code or "DISABLED",
            "unavailableReason": definition.unavailable_reason or "indicator is disabled",
        }
    if asset_type.upper() not in definition.supported_asset_types:
        return {
            **resolved,
            "status": "unavailable",
            "unavailableCode": "UNSUPPORTED_ASSET",
            "unavailableReason": f"{definition_id} does not support {asset_type or 'UNKNOWN'}",
        }
    if (definition.calculation_id or definition.indicator_id) == "indicator.vix":
        alignment = align_external_series(bars, external_series)
        external = alignment.to_public_dict()
        if alignment.status != "ready":
            unavailable_code = (
                "MISSING_DATASOURCE"
                if alignment.series.status != "ready"
                else "NO_ALIGNED_POINTS"
            )
            return {
                **resolved,
                "parameters": {},
                "status": "unavailable",
                "unavailableCode": unavailable_code,
                "unavailableReason": alignment.reason,
                "external": external,
            }
        return {
            **resolved,
            "parameters": {},
            "status": "ready",
            "unavailableCode": None,
            "unavailableReason": None,
            "series": {"vix": list(alignment.values)},
            "external": external,
        }
    missing_fields = [
        field
        for field in definition.required_fields
        if bars and not any(value is not None for value in _field(bars, field))
    ]
    if missing_fields:
        return {
            **resolved,
            "status": "unavailable",
            "unavailableCode": "MISSING_FIELD",
            "unavailableReason": f"{definition_id} requires populated fields: {', '.join(missing_fields)}",
        }
    try:
        normalized_parameters = _validated_parameters(definition, parameters)
        calculated = calculate_indicator(
            definition,
            bars,
            normalized_parameters,
            range_start=range_start,
            range_end=range_end,
        )
    except (IndicatorCalculationError, StrategyFunctionError) as error:
        return {
            **resolved,
            "status": "unavailable",
            "unavailableCode": error.code,
            "unavailableReason": str(error),
        }
    profile = calculated.pop("__profile__", None)
    result = {
        **resolved,
        "parameters": normalized_parameters,
        "status": "ready",
        "unavailableCode": None,
        "unavailableReason": None,
        "series": calculated,
    }
    if profile is not None:
        profile_range = profile.get("range")
        if isinstance(profile_range, dict):
            start = int(profile_range["startIndex"])
            end = int(profile_range["endIndex"])
            profile_range.update(
                {
                    "rangeSemantics": "inclusive_visible_indices_in_requested_window",
                    "firstBarAt": _bar_time(bars[start]),
                    "lastBarAt": _bar_time(bars[end]),
                }
            )
        result["profile"] = profile
    return result


def calculate_legacy_indicator_series(
    registry: IndicatorRegistry,
    bars: Sequence[Mapping[str, Any]],
    requested: Sequence[Mapping[str, Any]],
) -> dict[str, NumberSeries]:
    """Adapt the pre-R4 flat chart keys without keeping duplicate algorithms."""

    output: dict[str, NumberSeries] = {}
    for config in requested:
        indicator_id = str(config.get("id") or "").casefold()
        try:
            definition = registry.resolve(indicator_id, 1)
        except KeyError as error:
            raise IndicatorCalculationError(
                "DEFINITION_NOT_FOUND", f"unknown chart indicator: {indicator_id}"
            ) from error
        supplied = {key: value for key, value in config.items() if key != "id"}
        if indicator_id == "atr":
            close, high, low = _field(bars, "close"), _field(bars, "high"), _field(bars, "low")
            atr_lookback = supplied.get("atrLookback", 14)
            center_lookback = supplied.get("centerLookback", 20)
            for name, value in (("atrLookback", atr_lookback), ("centerLookback", center_lookback)):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value or not 1 <= value <= 2_000:
                    raise IndicatorCalculationError(
                        "INVALID_PARAMETER", f"{name} must be an integer between 1 and 2000"
                    )
            multiplier = supplied.get("multiplier", 2.0)
            channel = keltner(high, low, close, center_lookback, atr_lookback, multiplier)
            output.update(
                {
                    "atrUpper": channel["upper"],
                    "atrMiddle": channel["middle"],
                    "atrLower": channel["lower"],
                }
            )
            continue
        series = calculate_indicator(definition, bars, supplied)
        if indicator_id == "bollinger":
            output.update(
                {
                    "bollingerUpper": series["upper"],
                    "bollingerMiddle": series["middle"],
                    "bollingerLower": series["lower"],
                }
            )
        elif indicator_id == "hsar":
            output.update(
                {
                    "hsarResistance": series["resistance"],
                    "hsarSupport": series["support"],
                }
            )
        else:
            output.update(series)
    return output


__all__ = [
    "IndicatorCalculationError",
    "calculate_indicator",
    "calculate_indicator_instance",
    "calculate_legacy_indicator_series",
    "indicator_warmup",
]
