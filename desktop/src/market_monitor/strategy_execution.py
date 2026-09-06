"""Risk-gated execution boundary for versioned Strategy Definitions.

This module deliberately stops before any broker API.  A trusted desktop
Strategy Definition may create an :class:`OrderIntent`; the intent can reach
an isolated backtest or paper adapter only after the risk port accepts it.
Indicator, Strategy Function, Android and Live contexts never receive that
authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol

from market_monitor.contracts import ContractValidationError, validate_contract
from market_monitor.strategy_definition import StrategyDefinitionError, validate_strategy_definition
from market_monitor.strategy_resources import (
    ResourceKind,
    ResourceStatus,
    RuntimeCapability,
    StrategyResource,
    StrategyRunMode,
    allowed_capabilities,
)


ORDER_INTENT_SCHEMA = "order-intent.schema.json"
EXECUTION_RESULT_SCHEMA = "strategy-execution-result.schema.json"


class ExecutionPlatform(StrEnum):
    DESKTOP = "desktop"
    ANDROID = "android"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class PositionEffect(StrEnum):
    OPEN = "open"
    CLOSE = "close"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class RiskDecision(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NOT_EVALUATED = "not_evaluated"


class ExecutionStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DISABLED = "disabled"


class StrategyExecutionError(ValueError):
    """Stable boundary failure with an API-friendly status code."""

    def __init__(self, code: str, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _digest(value: Mapping[str, Any]) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CapabilityContext:
    """Server-issued authority; never constructed from client resource labels."""

    resource_kind: ResourceKind
    resource_id: str
    resource_version: int
    platform: ExecutionPlatform
    capabilities: tuple[RuntimeCapability, ...]
    definition_digest: str | None = None


def capability_context_for_resource(
    resource: StrategyResource,
    *,
    platform: ExecutionPlatform = ExecutionPlatform.DESKTOP,
) -> CapabilityContext:
    """Issue a context from an already validated server-side registry resource."""

    return CapabilityContext(
        resource_kind=resource.resource_kind,
        resource_id=resource.resource_id,
        resource_version=resource.version,
        platform=platform,
        capabilities=allowed_capabilities(resource.resource_kind),
    )


def capability_context_for_strategy(
    definition: Mapping[str, Any],
    *,
    platform: ExecutionPlatform = ExecutionPlatform.DESKTOP,
) -> CapabilityContext:
    """Validate an authoritative Strategy Definition and issue exact-version authority."""

    if str((definition.get("execution") or {}).get("run_mode") or "") == StrategyRunMode.LIVE:
        raise StrategyExecutionError("LIVE_DISABLED", "实盘执行适配器尚未配置并验收", status_code=409)
    try:
        validate_strategy_definition(definition)
    except StrategyDefinitionError as error:
        raise StrategyExecutionError(error.code, error.message) from error
    return CapabilityContext(
        resource_kind=ResourceKind.STRATEGY,
        resource_id=str(definition["id"]),
        resource_version=int(definition["version"]),
        platform=platform,
        capabilities=allowed_capabilities(ResourceKind.STRATEGY),
        definition_digest=_digest(definition),
    )


@dataclass(frozen=True)
class OrderIntent:
    schema_version: int
    intent_id: str
    strategy_id: str
    strategy_version: int
    run_mode: str
    instrument_id: str
    side: str
    position_effect: str
    order_type: str
    quantity: float
    signal_time: str
    created_at: str
    limit_price: float | None = None
    stop_price: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def create_order_intent(
    context: CapabilityContext,
    definition: Mapping[str, Any],
    *,
    instrument_id: str,
    side: OrderSide | str,
    position_effect: PositionEffect | str,
    order_type: OrderType | str,
    quantity: float,
    signal_time: str,
    run_mode: StrategyRunMode | str | None = None,
    intent_id: str | None = None,
    limit_price: float | None = None,
    stop_price: float | None = None,
    created_at: str | None = None,
) -> OrderIntent:
    """Create, but do not execute, an intent under a trusted capability context."""

    if context.platform != ExecutionPlatform.DESKTOP:
        raise StrategyExecutionError("PLATFORM_DENIED", "Android 只产生观察信号，不能生成订单意图", status_code=403)
    if context.resource_kind != ResourceKind.STRATEGY:
        raise StrategyExecutionError(
            "PERMISSION_DENIED",
            f"{context.resource_kind.value} 不能生成订单意图",
            status_code=403,
        )
    if RuntimeCapability.ORDER_INTENT_CREATE not in context.capabilities:
        raise StrategyExecutionError("PERMISSION_DENIED", "运行上下文不具备订单意图能力", status_code=403)
    if str(definition.get("id") or "") != context.resource_id or int(definition.get("version") or 0) != context.resource_version:
        raise StrategyExecutionError("CONTEXT_MISMATCH", "策略身份或版本与能力上下文不一致", status_code=403)
    if context.definition_digest != _digest(definition):
        raise StrategyExecutionError("CONTEXT_MISMATCH", "策略定义已在能力签发后改变", status_code=403)
    if str(definition.get("status") or "") != ResourceStatus.ACTIVE:
        raise StrategyExecutionError("STRATEGY_DISABLED", "只有 active 策略可以生成订单意图", status_code=409)

    definition_mode = StrategyRunMode(str(definition["execution"]["run_mode"]))
    requested_mode = StrategyRunMode(run_mode) if run_mode is not None else definition_mode
    if requested_mode == StrategyRunMode.LIVE:
        raise StrategyExecutionError("LIVE_DISABLED", "实盘执行适配器尚未配置并验收", status_code=409)
    if requested_mode != definition_mode:
        raise StrategyExecutionError("RUN_MODE_MISMATCH", "请求运行模式与策略定义不一致")

    intent = OrderIntent(
        schema_version=1,
        intent_id=intent_id or str(uuid.uuid4()),
        strategy_id=context.resource_id,
        strategy_version=context.resource_version,
        run_mode=requested_mode.value,
        instrument_id=instrument_id,
        side=OrderSide(side).value,
        position_effect=PositionEffect(position_effect).value,
        order_type=OrderType(order_type).value,
        quantity=float(quantity),
        signal_time=signal_time,
        created_at=created_at or _now_iso(),
        limit_price=float(limit_price) if limit_price is not None else None,
        stop_price=float(stop_price) if stop_price is not None else None,
    )
    if not math.isfinite(intent.quantity):
        raise StrategyExecutionError("INVALID_INTENT", "quantity 必须为有限数值")
    for name, value in (("limit_price", intent.limit_price), ("stop_price", intent.stop_price)):
        if value is not None and not math.isfinite(value):
            raise StrategyExecutionError("INVALID_INTENT", f"{name} 必须为有限数值")
    try:
        validate_contract(ORDER_INTENT_SCHEMA, intent.to_dict())
    except (ContractValidationError, ValueError) as error:
        raise StrategyExecutionError("INVALID_INTENT", str(error)) from error
    return intent


@dataclass(frozen=True)
class RiskContext:
    account_equity: float
    reference_price: float
    current_position_quantity: float = 0.0
    contract_multiplier: float = 1.0
    drawdown_percent: float = 0.0


@dataclass(frozen=True)
class RiskAssessment:
    decision: RiskDecision
    reason: str


class RiskEngine(Protocol):
    def evaluate(
        self,
        intent: OrderIntent,
        definition: Mapping[str, Any],
        context: RiskContext,
    ) -> RiskAssessment: ...


class DefinitionRiskEngine:
    """Small default gate enforcing position and drawdown limits from the definition."""

    def evaluate(
        self,
        intent: OrderIntent,
        definition: Mapping[str, Any],
        context: RiskContext,
    ) -> RiskAssessment:
        values = (
            context.account_equity,
            context.reference_price,
            context.current_position_quantity,
            context.contract_multiplier,
            context.drawdown_percent,
        )
        if not all(math.isfinite(value) for value in values):
            return RiskAssessment(RiskDecision.REJECTED, "风控输入包含非有限数值")
        if context.account_equity <= 0 or context.reference_price <= 0 or context.contract_multiplier <= 0:
            return RiskAssessment(RiskDecision.REJECTED, "账户权益、参考价格和合约乘数必须大于零")
        risk = definition["risk"]
        signed_quantity = intent.quantity if intent.side == OrderSide.BUY else -intent.quantity
        if intent.position_effect == PositionEffect.CLOSE:
            if context.current_position_quantity == 0:
                return RiskAssessment(RiskDecision.REJECTED, "无可平仓持仓")
            if context.current_position_quantity > 0 and intent.side != OrderSide.SELL:
                return RiskAssessment(RiskDecision.REJECTED, "多头平仓必须使用 sell")
            if context.current_position_quantity < 0 and intent.side != OrderSide.BUY:
                return RiskAssessment(RiskDecision.REJECTED, "空头平仓必须使用 buy")
            if intent.quantity > abs(context.current_position_quantity):
                return RiskAssessment(RiskDecision.REJECTED, "平仓数量超过当前持仓")
        elif context.drawdown_percent >= float(risk["max_drawdown_percent"]):
            return RiskAssessment(RiskDecision.REJECTED, "已达到策略最大回撤限制")
        projected_quantity = context.current_position_quantity + signed_quantity
        projected_notional = abs(projected_quantity) * context.reference_price * context.contract_multiplier
        maximum_notional = context.account_equity * float(risk["max_position_percent"]) / 100.0
        if projected_notional > maximum_notional + 1e-9:
            return RiskAssessment(RiskDecision.REJECTED, "预计持仓超过策略最大仓位限制")
        return RiskAssessment(RiskDecision.ACCEPTED, "通过策略定义风控限制")


@dataclass(frozen=True)
class _RiskPermit:
    intent_id: str
    decision: RiskDecision
    seal: object


@dataclass(frozen=True)
class AdapterReceipt:
    status: ExecutionStatus
    adapter_id: str
    reason: str


class ExecutionAdapter(Protocol):
    run_mode: StrategyRunMode
    adapter_id: str

    def submit(self, intent: OrderIntent, permit: _RiskPermit) -> AdapterReceipt: ...


class _IsolatedSimulationAdapter:
    """Accept an authorized intent into one isolated simulation environment."""

    def __init__(self, run_mode: StrategyRunMode, *, seal: object) -> None:
        if run_mode == StrategyRunMode.LIVE:
            raise ValueError("simulation adapter cannot represent live execution")
        self.run_mode = run_mode
        self.adapter_id = f"{run_mode.value}-simulation-v1"
        self._seal = seal

    def submit(self, intent: OrderIntent, permit: _RiskPermit) -> AdapterReceipt:
        if (
            not isinstance(permit, _RiskPermit)
            or permit.seal is not self._seal
            or permit.intent_id != intent.intent_id
            or permit.decision != RiskDecision.ACCEPTED
        ):
            raise StrategyExecutionError("RISK_BYPASS_DENIED", "执行适配器拒绝未经过本次风险端口的意图", status_code=403)
        if intent.run_mode != self.run_mode:
            raise StrategyExecutionError("RUN_MODE_MISMATCH", "订单意图与执行适配器模式不一致")
        return AdapterReceipt(
            ExecutionStatus.ACCEPTED,
            self.adapter_id,
            f"已进入隔离的{self.run_mode.value}模拟适配器，未触达真实 Order API",
        )


@dataclass(frozen=True)
class ExecutionOutcome:
    schema_version: int
    capability: str
    run_mode: str
    risk_decision: str
    execution_status: str
    reason: str
    intent: Mapping[str, Any]
    adapter_id: str | None
    idempotent: bool
    audited_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ExecutionStore(Protocol):
    def get(self, intent_id: str) -> Mapping[str, Any] | None: ...

    def put(self, intent_id: str, record: Mapping[str, Any]) -> Mapping[str, Any]: ...


class InMemoryExecutionStore:
    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}

    def get(self, intent_id: str) -> Mapping[str, Any] | None:
        record = self._records.get(intent_id)
        return dict(record) if record else None

    def put(self, intent_id: str, record: Mapping[str, Any]) -> Mapping[str, Any]:
        self._records.setdefault(intent_id, dict(record))
        return dict(self._records[intent_id])


class FileExecutionStore:
    """One immutable JSON record per intent gives restart-safe idempotency."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def _path(self, intent_id: str) -> Path:
        digest = hashlib.sha256(intent_id.encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.json"

    def get(self, intent_id: str) -> Mapping[str, Any] | None:
        path = self._path(intent_id)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) and value.get("intentId") == intent_id else None

    def put(self, intent_id: str, record: Mapping[str, Any]) -> Mapping[str, Any]:
        existing = self.get(intent_id)
        if existing is not None:
            return existing
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(intent_id)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(dict(record), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            try:
                os.link(temporary_name, path)
            except FileExistsError:
                pass
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        stored = self.get(intent_id)
        if stored is None:
            raise StrategyExecutionError("AUDIT_WRITE_FAILED", "无法保存订单意图幂等记录", status_code=500)
        return stored


class ExecutionAuditSink(Protocol):
    def append(self, event: Mapping[str, Any]) -> None: ...


class InMemoryExecutionAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def append(self, event: Mapping[str, Any]) -> None:
        self.events.append(dict(event))


_AUDIT_LOCK = threading.Lock()


class FileExecutionAudit:
    """Append only an allow-listed event; request bodies and credentials are never accepted."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def append(self, event: Mapping[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        date = str(event.get("auditedAt") or _now_iso())[:10]
        path = self.directory / f"execution-{date}.jsonl"
        line = json.dumps(dict(event), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with _AUDIT_LOCK, path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")


class StrategyExecutionGateway:
    """The sole public composition point from intent through risk to adapter."""

    def __init__(
        self,
        *,
        risk_engine: RiskEngine | None = None,
        store: ExecutionStore | None = None,
        audit: ExecutionAuditSink | None = None,
        enable_backtest: bool = True,
        enable_paper: bool = True,
    ) -> None:
        self.risk_engine = risk_engine or DefinitionRiskEngine()
        self.store = store or InMemoryExecutionStore()
        self.audit = audit or InMemoryExecutionAudit()
        self._seal = object()
        self._adapters: dict[StrategyRunMode, ExecutionAdapter] = {}
        if enable_backtest:
            self._adapters[StrategyRunMode.BACKTEST] = _IsolatedSimulationAdapter(
                StrategyRunMode.BACKTEST, seal=self._seal
            )
        if enable_paper:
            self._adapters[StrategyRunMode.PAPER] = _IsolatedSimulationAdapter(
                StrategyRunMode.PAPER, seal=self._seal
            )

    def capabilities(self) -> dict[str, Any]:
        modes = []
        for mode in StrategyRunMode:
            adapter = self._adapters.get(mode)
            enabled = adapter is not None and mode != StrategyRunMode.LIVE
            modes.append(
                {
                    "runMode": mode.value,
                    "enabled": enabled,
                    "executionStatus": "available" if enabled else ExecutionStatus.DISABLED.value,
                    "adapterId": adapter.adapter_id if enabled else None,
                    "reason": (
                        "隔离模拟适配器可用，不触达真实 Order API"
                        if enabled
                        else "实盘执行适配器尚未配置并验收"
                    ),
                }
            )
        return {
            "capability": RuntimeCapability.ORDER_INTENT_CREATE.value,
            "platform": ExecutionPlatform.DESKTOP.value,
            "resourceKinds": {
                ResourceKind.STRATEGY_FUNCTION.value: False,
                ResourceKind.INDICATOR.value: False,
                ResourceKind.STRATEGY.value: True,
            },
            "androidOrderIntentEnabled": False,
            "modes": modes,
        }

    def dispatch(
        self,
        definition: Mapping[str, Any],
        risk_context: RiskContext,
        **intent_values: Any,
    ) -> ExecutionOutcome:
        context = capability_context_for_strategy(definition)
        intent = create_order_intent(context, definition, **intent_values)
        fingerprint = _intent_fingerprint(intent)
        existing = self.store.get(intent.intent_id)
        if existing is not None:
            if existing.get("fingerprint") != fingerprint:
                raise StrategyExecutionError(
                    "IDEMPOTENCY_CONFLICT",
                    "同一 intentId 已用于不同订单意图",
                    status_code=409,
                )
            outcome = _outcome_from_record(existing)
            return replace(outcome, idempotent=True)

        assessment = self.risk_engine.evaluate(intent, definition, risk_context)
        if assessment.decision != RiskDecision.ACCEPTED:
            outcome = ExecutionOutcome(
                schema_version=1,
                capability=RuntimeCapability.ORDER_INTENT_CREATE.value,
                run_mode=intent.run_mode,
                risk_decision=assessment.decision.value,
                execution_status=ExecutionStatus.REJECTED.value,
                reason=assessment.reason,
                intent=intent.to_dict(),
                adapter_id=None,
                idempotent=False,
                audited_at=_now_iso(),
            )
        else:
            mode = StrategyRunMode(intent.run_mode)
            adapter = self._adapters.get(mode)
            if adapter is None or mode == StrategyRunMode.LIVE:
                raise StrategyExecutionError("EXECUTION_DISABLED", f"{mode.value} 执行适配器不可用", status_code=409)
            permit = _RiskPermit(intent.intent_id, assessment.decision, self._seal)
            receipt = adapter.submit(intent, permit)
            outcome = ExecutionOutcome(
                schema_version=1,
                capability=RuntimeCapability.ORDER_INTENT_CREATE.value,
                run_mode=intent.run_mode,
                risk_decision=assessment.decision.value,
                execution_status=receipt.status.value,
                reason=receipt.reason,
                intent=intent.to_dict(),
                adapter_id=receipt.adapter_id,
                idempotent=False,
                audited_at=_now_iso(),
            )
        try:
            validate_contract(EXECUTION_RESULT_SCHEMA, outcome.to_dict())
        except ContractValidationError as error:
            raise StrategyExecutionError("INVALID_EXECUTION_RESULT", str(error), status_code=500) from error
        record = {"intentId": intent.intent_id, "fingerprint": fingerprint, "outcome": outcome.to_dict()}
        stored = self.store.put(intent.intent_id, record)
        stored_outcome = _outcome_from_record(stored)
        self.audit.append(_audit_event(stored_outcome))
        return stored_outcome


def _intent_fingerprint(intent: OrderIntent) -> str:
    value = intent.to_dict()
    value.pop("created_at", None)
    return _digest(value)


def _outcome_from_record(record: Mapping[str, Any]) -> ExecutionOutcome:
    value = record.get("outcome")
    if not isinstance(value, Mapping):
        raise StrategyExecutionError("INVALID_EXECUTION_RECORD", "订单意图幂等记录损坏", status_code=500)
    return ExecutionOutcome(**dict(value))


def _audit_event(outcome: ExecutionOutcome) -> dict[str, Any]:
    intent = outcome.intent
    return {
        "schemaVersion": 1,
        "event": "strategy_order_intent",
        "auditedAt": outcome.audited_at,
        "intentId": intent["intent_id"],
        "strategyId": intent["strategy_id"],
        "strategyVersion": intent["strategy_version"],
        "instrumentId": intent["instrument_id"],
        "capability": outcome.capability,
        "runMode": outcome.run_mode,
        "riskDecision": outcome.risk_decision,
        "executionStatus": outcome.execution_status,
        "adapterId": outcome.adapter_id,
        "reason": outcome.reason,
    }


__all__ = [
    "CapabilityContext",
    "DefinitionRiskEngine",
    "ExecutionOutcome",
    "ExecutionPlatform",
    "ExecutionStatus",
    "FileExecutionAudit",
    "FileExecutionStore",
    "InMemoryExecutionAudit",
    "InMemoryExecutionStore",
    "OrderIntent",
    "OrderSide",
    "OrderType",
    "PositionEffect",
    "RiskAssessment",
    "RiskContext",
    "RiskDecision",
    "StrategyExecutionError",
    "StrategyExecutionGateway",
    "capability_context_for_resource",
    "capability_context_for_strategy",
    "create_order_intent",
]
