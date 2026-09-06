"""Shared resource and permission model for the three-layer strategy system."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Iterable, Mapping

from market_monitor.contracts import ContractValidationError, validate_contract


STRATEGY_RESOURCE_SCHEMA = "strategy-resource.schema.json"


class ResourceKind(StrEnum):
    STRATEGY_FUNCTION = "strategy_function"
    INDICATOR = "indicator"
    STRATEGY = "strategy"


class ResourceOrigin(StrEnum):
    BUILTIN = "builtin"
    CUSTOM = "custom"
    COMMUNITY = "community"
    PLUGIN = "plugin"


class ResourceStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"


class RuntimeCapability(StrEnum):
    MARKET_DATA_INPUT = "market_data_input"
    PLOT_CREATE = "plot_create"
    ACCOUNT_READ = "account_read"
    POSITION_READ = "position_read"
    ORDER_INTENT_CREATE = "order_intent_create"


class StrategyRunMode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class StrategyUnavailableCode(StrEnum):
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    UNSUPPORTED_ASSET = "UNSUPPORTED_ASSET"
    MISSING_FIELD = "MISSING_FIELD"
    MISSING_DATASOURCE = "MISSING_DATASOURCE"
    INVALID_PARAMETER = "INVALID_PARAMETER"
    DISABLED = "DISABLED"


_CAPABILITY_MATRIX: dict[ResourceKind, tuple[RuntimeCapability, ...]] = {
    ResourceKind.STRATEGY_FUNCTION: (RuntimeCapability.MARKET_DATA_INPUT,),
    ResourceKind.INDICATOR: (RuntimeCapability.MARKET_DATA_INPUT, RuntimeCapability.PLOT_CREATE),
    ResourceKind.STRATEGY: (
        RuntimeCapability.MARKET_DATA_INPUT,
        RuntimeCapability.ACCOUNT_READ,
        RuntimeCapability.POSITION_READ,
        RuntimeCapability.ORDER_INTENT_CREATE,
    ),
}


class StrategyResourceError(ValueError):
    """A stable strategy-resource validation or permission failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, order=True)
class ResourceReference:
    resource_kind: ResourceKind
    resource_id: str
    version: int

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ResourceReference:
        return cls(ResourceKind(str(value["resource_kind"])), str(value["id"]), int(value["version"]))

    def to_dict(self) -> dict[str, Any]:
        return {"resource_kind": self.resource_kind.value, "id": self.resource_id, "version": self.version}


@dataclass(frozen=True)
class StrategyResource:
    resource_kind: ResourceKind
    resource_id: str
    version: int
    display_name: str
    origin: ResourceOrigin
    supported_asset_types: tuple[str, ...]
    status: ResourceStatus
    created_at: str
    updated_at: str
    dependencies: tuple[ResourceReference, ...]
    capabilities: tuple[RuntimeCapability, ...]
    definition: Mapping[str, Any]

    @property
    def key(self) -> tuple[str, str, int]:
        return self.resource_kind.value, self.resource_id, self.version


def allowed_capabilities(resource_kind: ResourceKind | str) -> tuple[RuntimeCapability, ...]:
    """Return immutable capabilities for a resource layer."""

    return _CAPABILITY_MATRIX[ResourceKind(resource_kind)]


def _parse_datetime(value: str, field: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise StrategyResourceError("INVALID_TIMESTAMP", f"{field} must be an ISO-8601 timestamp") from error


def validate_resource_definition(document: Mapping[str, Any]) -> StrategyResource:
    """Validate schema plus dependency, trust and timestamp semantics."""

    payload = dict(document)
    try:
        validate_contract(STRATEGY_RESOURCE_SCHEMA, payload)
    except ContractValidationError as error:
        raise StrategyResourceError("INVALID_RESOURCE", str(error)) from error

    kind = ResourceKind(str(payload["resource_kind"]))
    dependencies = tuple(ResourceReference.from_dict(value) for value in payload["dependencies"])
    resource = StrategyResource(
        resource_kind=kind,
        resource_id=str(payload["id"]),
        version=int(payload["version"]),
        display_name=str(payload["display_name"]),
        origin=ResourceOrigin(str(payload["origin"])),
        supported_asset_types=tuple(str(value) for value in payload["supported_asset_types"]),
        status=ResourceStatus(str(payload["status"])),
        created_at=str(payload["created_at"]),
        updated_at=str(payload["updated_at"]),
        dependencies=dependencies,
        capabilities=tuple(RuntimeCapability(str(value)) for value in payload["capabilities"]),
        definition=dict(payload["definition"]),
    )
    if _parse_datetime(resource.created_at, "created_at") > _parse_datetime(resource.updated_at, "updated_at"):
        raise StrategyResourceError("INVALID_TIMESTAMP", "updated_at must not precede created_at")
    if resource.origin in {ResourceOrigin.COMMUNITY, ResourceOrigin.PLUGIN} and resource.status != ResourceStatus.DISABLED:
        raise StrategyResourceError("UNTRUSTED_RESOURCE", "community and plugin resources must default to disabled")
    self_reference = ResourceReference(resource.resource_kind, resource.resource_id, resource.version)
    if self_reference in dependencies:
        raise StrategyResourceError("DEPENDENCY_CYCLE", "a resource cannot depend on itself")
    return resource


def validate_dependency_graph(documents: Iterable[Mapping[str, Any]]) -> tuple[StrategyResource, ...]:
    """Validate exact version references and reject dependency cycles."""

    resources = tuple(validate_resource_definition(document) for document in documents)
    by_key = {resource.key: resource for resource in resources}
    if len(by_key) != len(resources):
        raise StrategyResourceError("DUPLICATE_RESOURCE", "resource id and version must be unique per kind")
    edges: dict[tuple[str, str, int], tuple[tuple[str, str, int], ...]] = {}
    for resource in resources:
        referenced = tuple((item.resource_kind.value, item.resource_id, item.version) for item in resource.dependencies)
        missing = next((key for key in referenced if key not in by_key), None)
        if missing:
            raise StrategyResourceError("MISSING_DEPENDENCY", f"missing dependency {missing[0]}:{missing[1]}@{missing[2]}")
        edges[resource.key] = referenced

    visiting: set[tuple[str, str, int]] = set()
    visited: set[tuple[str, str, int]] = set()

    def visit(key: tuple[str, str, int]) -> None:
        if key in visiting:
            raise StrategyResourceError("DEPENDENCY_CYCLE", f"dependency cycle through {key[1]}@{key[2]}")
        if key in visited:
            return
        visiting.add(key)
        for dependency in edges[key]:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in edges:
        visit(key)
    return resources


def require_capability(resource: StrategyResource, capability: RuntimeCapability | str) -> None:
    """Enforce the server-side layer permission matrix."""

    requested = RuntimeCapability(capability)
    if requested not in allowed_capabilities(resource.resource_kind):
        raise StrategyResourceError(
            "PERMISSION_DENIED",
            f"{resource.resource_kind.value} cannot use capability {requested.value}",
        )


def adapt_legacy_strategy_definition(
    document: Mapping[str, Any], *, display_name: str, created_at: str, updated_at: str
) -> dict[str, Any]:
    """Wrap an existing dsl/formula/builder document without changing its execution meaning."""

    strategy_id = str(document.get("strategy_id") or "").strip()
    if not strategy_id:
        raise StrategyResourceError("INVALID_RESOURCE", "legacy strategy_id is required")
    raw_version = document.get("strategy_version") or document.get("formula_version") or 1
    try:
        version = int(raw_version)
    except (TypeError, ValueError) as error:
        raise StrategyResourceError("INVALID_RESOURCE", "legacy strategy version must be an integer") from error
    asset_type = str((document.get("universe") or {}).get("asset_type") or "").upper()
    supported = [asset_type] if asset_type in {"STOCK", "ETF", "INDEX", "FUTURE"} else ["STOCK", "ETF", "INDEX", "FUTURE"]
    return {
        "schema_version": 1,
        "resource_kind": ResourceKind.STRATEGY.value,
        "id": strategy_id.lower(),
        "version": version,
        "display_name": display_name,
        "origin": ResourceOrigin.CUSTOM.value,
        "supported_asset_types": supported,
        "status": ResourceStatus.ACTIVE.value,
        "created_at": created_at,
        "updated_at": updated_at,
        "dependencies": [],
        "capabilities": [capability.value for capability in allowed_capabilities(ResourceKind.STRATEGY)],
        "definition": {"legacy_runtime": str(document.get("script_kind") or "dsl_v1"), "document": dict(document)},
    }
