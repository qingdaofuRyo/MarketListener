"""Versioned catalog metadata for persisted Strategy definitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class StrategyRegistration:
    strategy_id: str
    version: int
    display_name: str
    description: str
    category: str
    origin: str
    status: str
    run_mode: str
    backtest_status: str
    script_kind: str
    base_timeframe: str
    supported_asset_types: tuple[str, ...]
    inputs: tuple[str, ...]
    parameters: Mapping[str, Any]
    created_at: str
    updated_at: str
    mark_color_id: str | None = None

    @property
    def key(self) -> tuple[str, int]:
        return self.strategy_id, self.version

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "resourceKind": "strategy",
            "strategyId": self.strategy_id,
            "id": self.strategy_id,
            "version": self.version,
            "versionedId": f"{self.strategy_id}@{self.version}",
            "strategyVersion": str(self.version),
            "displayName": self.display_name,
            "description": self.description,
            "category": self.category,
            "origin": self.origin,
            "status": self.status,
            "enabled": self.status == "active",
            "runMode": self.run_mode,
            "availableRunModes": [
                {"id": "backtest", "enabled": True},
                {"id": "paper", "enabled": True},
                {"id": "live", "enabled": False, "reason": "实盘交易接口尚未配置"},
            ],
            "backtestStatus": self.backtest_status,
            "scriptKind": self.script_kind,
            "baseTimeframe": self.base_timeframe,
            "supportedAssetTypes": list(self.supported_asset_types),
            "inputs": list(self.inputs),
            "parameters": dict(self.parameters),
            "markColorId": self.mark_color_id,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


class StrategyRegistry:
    def __init__(self, definitions: Iterable[StrategyRegistration] = ()) -> None:
        self._definitions: dict[tuple[str, int], StrategyRegistration] = {}
        for definition in definitions:
            if definition.key in self._definitions:
                raise ValueError(f"duplicate strategy {definition.strategy_id}@{definition.version}")
            self._definitions[definition.key] = definition

    def list(
        self,
        *,
        query: str | None = None,
        category: str | None = None,
        asset_type: str | None = None,
        origin: str | None = None,
        status: str | None = None,
    ) -> tuple[StrategyRegistration, ...]:
        values = list(self._definitions.values())
        if query:
            term = query.casefold().strip()
            values = [value for value in values if term in " ".join((value.strategy_id, value.display_name, value.description)).casefold()]
        if category:
            values = [value for value in values if value.category == category]
        if asset_type:
            term = asset_type.upper().strip()
            values = [value for value in values if term in value.supported_asset_types]
        if origin:
            values = [value for value in values if value.origin == origin]
        if status:
            values = [value for value in values if value.status == status]
        return tuple(sorted(values, key=lambda value: (value.updated_at, value.strategy_id), reverse=True))


def registration_from_document(
    document: Mapping[str, Any],
    metadata: Mapping[str, Any],
    *,
    created_at: str,
    updated_at: str,
) -> StrategyRegistration:
    strategy_id = str(document.get("strategy_id") or "")
    raw_version = document.get("strategy_version") or document.get("formula_version") or 1
    try:
        version = max(1, int(raw_version))
    except (TypeError, ValueError):
        version = 1
    universe = document.get("universe") if isinstance(document.get("universe"), Mapping) else {}
    asset_type = str(universe.get("asset_type") or "").upper()
    assets = (asset_type,) if asset_type else ("STOCK", "B_SHARE", "INDEX", "FUTURE", "ETF", "LOF", "REIT", "FUND")
    runtime_kind = str(document.get("script_kind") or "dsl_v1")
    script_kind = str(document.get("condition_kind") or runtime_kind)
    default_inputs = ("open", "high", "low", "close", "volume") if runtime_kind == "formula_v1" else ()
    return StrategyRegistration(
        strategy_id=strategy_id,
        version=version,
        display_name=str(metadata.get("displayName") or strategy_id),
        description=str(document.get("description") or ""),
        category=str(metadata.get("category") or "custom"),
        origin=str(metadata.get("origin") or "custom"),
        status=str(metadata.get("status") or "active"),
        run_mode=str(metadata.get("runMode") or "backtest"),
        backtest_status=str(metadata.get("backtestStatus") or "not_run"),
        script_kind=script_kind,
        base_timeframe=str(document.get("base_timeframe") or document.get("period") or "1d"),
        supported_asset_types=assets,
        inputs=tuple(str(value) for value in (document.get("inputs") or default_inputs)),
        parameters=dict(document.get("parameters") or {}),
        created_at=str(metadata.get("createdAt") or created_at),
        updated_at=updated_at,
        mark_color_id=str(metadata.get("markColorId") or "") or None,
    )


__all__ = ["StrategyRegistration", "StrategyRegistry", "registration_from_document"]
