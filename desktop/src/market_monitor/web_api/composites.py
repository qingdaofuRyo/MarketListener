"""Versioned composite strategy editing and explicitly requested local scans."""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from market_monitor.composite_monitor import advance_composite, current_items, peer_groups
from market_monitor.composite_program import TEMPLATE, compile_program
from market_monitor.market_classification import UNCLASSIFIED_CATEGORY, classify_market, matches_market_category
from . import signals

router = APIRouter(prefix="/api/composites", tags=["composites"])


class DefinitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    displayName: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    source: str = Field(min_length=1, max_length=20000)
    period: str = "1d"
    enabled: bool = False
    version: int = Field(default=0, ge=0)


def directory(request: Request):
    return signals.directory(request).parent / "composites"


def all_definitions(request: Request):
    return [item for item in signals.read(directory(request) / "definitions.json", {}).values() if not item.get("deleted")]


@router.get("/definitions")
def definitions(request: Request):
    legacy = signals.read(signals.directory(request) / "definitions.json", {})
    return {"items": all_definitions(request), "periods": signals.PERIODS, "template": TEMPLATE,
            "legacyCount": sum(not item.get("deleted") for item in legacy.values())}


@router.post("/validate")
def validate(body: DefinitionBody):
    if body.period not in signals.PERIODS:
        raise HTTPException(422, detail="不支持该K线周期")
    try:
        program = compile_program(body.source)
    except ValueError as error:
        raise HTTPException(422, detail=str(error)) from error
    return {"valid": True, "dependencies": program.dependencies, "parts": ["attention", "position", "timing"]}


def save(request: Request, body: DefinitionBody, identifier: str | None = None):
    validation = validate(body)
    if not body.displayName.strip():
        raise HTTPException(422, detail="策略名称不能为空白")
    with signals._lock:
        path = directory(request) / "definitions.json"
        items = signals.read(path, {})
        if identifier and (identifier not in items or items[identifier].get("deleted")):
            raise HTTPException(404, detail="组合策略不存在")
        old = items.get(identifier, {})
        if identifier and old["version"] != body.version:
            raise HTTPException(409, detail="策略已更新，请重新打开编辑器")
        identifier = identifier or "composite_" + uuid4().hex
        document = {**body.model_dump(), "id": identifier, "version": old.get("version", 0) + 1,
                    "kind": "composite", "dependencies": validation["dependencies"]}
        # Old snapshots are retained so edited rules never rewrite earlier evidence.
        if old:
            signals.write(directory(request) / "versions" / f"{identifier}.{old['version']}.json", old)
        items[identifier] = document
        signals.write(path, items)
        return document


@router.post("/definitions", status_code=201)
def create(request: Request, body: DefinitionBody):
    return save(request, body)


@router.put("/definitions/{identifier}")
def update(identifier: str, request: Request, body: DefinitionBody):
    return save(request, body, identifier)


@router.delete("/definitions/{identifier}")
def delete(identifier: str, request: Request):
    with signals._lock:
        path = directory(request) / "definitions.json"
        items = signals.read(path, {})
        if identifier not in items:
            raise HTTPException(404, detail="组合策略不存在")
        items[identifier]["deleted"] = True
        signals.write(path, items)
    return {"deleted": True, "recoverable": True}


@router.post("/definitions/{identifier}/restore")
def restore(identifier: str, request: Request):
    with signals._lock:
        path = directory(request) / "definitions.json"
        items = signals.read(path, {})
        if identifier not in items:
            raise HTTPException(404, detail="组合策略不存在")
        items[identifier]["deleted"] = False
        # Restoration requires explicit enable; old cursors are never auto-resumed.
        items[identifier]["enabled"] = False
        signals.write(path, items)
    return items[identifier]


def payload(request: Request, state: dict[str, Any], logical=None):
    items = current_items(state, all_definitions(request))
    if logical is None and items:
        logical = signals._logical_instruments(request.app.state.data_root)
    logical = logical or {}
    return {"items": [{**signals._normalize_tdx_instrument(signals._normalize_future_name(logical.get(item["instrumentId"], {}))),
                       **item, "categoryId": classify_market(logical.get(item["instrumentId"], {}))}
                      for item in peer_groups(items)], "events": state.get("events", [])[-100:]}


@router.get("/monitor")
def monitor(request: Request):
    return payload(request, signals.read(directory(request) / "monitor.json", {}))


@router.post("/scan")
def scan(request: Request, body: signals.ScanBody):
    root = request.app.state.data_root
    with signals._lock:
        definitions = [item for item in all_definitions(request) if item["enabled"]]
        state = signals.read(directory(request) / "monitor.json", {})
        if not definitions:
            return {**payload(request, state, {}), "scanned": 0, "total": 0, "nextOffset": None,
                    "nextAfterId": None, "issues": [], "issueCount": 0}
        logical = signals._logical_instruments(root)
        active = {(item["strategyId"], item["instrumentId"]) for item in current_items(state, definitions)}
        active_ids = {identifier for _, identifier in active}
        def eligible(item):
            return not body.categoryKeys or any(matches_market_category(item, category) for category in body.categoryKeys)
        selected = [item for item in definitions if not body.strategyIds or item["id"] in body.strategyIds]
        universe = sorted(identifier for identifier, item in logical.items()
                          if classify_market(item) != UNCLASSIFIED_CATEGORY and
                          (identifier in active_ids or (selected and not body.monitoringOnly and eligible(item))))
        remaining = [identifier for identifier in universe if not body.afterId or identifier > body.afterId]
        batch = remaining[body.offset:body.offset + body.limit]
        issues = []
        for identifier in batch:
            windows = {}
            for definition in definitions:
                watched = (definition["id"], identifier) in active
                allow = not body.monitoringOnly and definition in selected and eligible(logical[identifier])
                if not watched and not allow:
                    continue
                try:
                    period = definition["period"]
                    if period not in windows:
                        windows[period] = signals._history_before(root, logical[identifier], period, None, 500)[0]
                    advance_composite(state, definition, logical[identifier], windows[period], allow_attention=allow)
                except (ValueError, TypeError, KeyError, OverflowError) as error:
                    issues.append({"instrumentId": identifier, "strategyId": definition["id"], "reason": str(error)})
        signals.write(directory(request) / "monitor.json", state)
        more = body.offset + len(batch) < len(remaining)
        return {**payload(request, state, logical), "scanned": len(batch), "total": len(universe),
                "nextOffset": body.offset + len(batch) if more else None, "nextAfterId": batch[-1] if more else None,
                "issues": issues[:100], "issueCount": len(issues)}
