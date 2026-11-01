"""Customer-safe industry and financial-engineering research interfaces."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from market_monitor.quant_research import (
    load_run, method, methods, readiness, reproduce_run, run_diagnostic,
)
from market_monitor.industry_classification import public_classification, public_csrc_classification
from market_monitor.research_publication import read_publication

router = APIRouter(prefix="/api", tags=["research"])


def _root(request: Request) -> Path:
    return request.app.state.data_root


class DiagnosticRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    methodId: str = Field(min_length=1, max_length=100)
    instrumentId: str = Field(min_length=1, max_length=160)
    start: date
    end: date
    window: int | None = Field(default=None, ge=2, le=252)


@router.get("/industry/catalog")
def industry_catalog(request: Request) -> dict[str, Any]:
    return read_publication(_root(request), "catalog")


@router.get("/industry/classification")
def industry_classification() -> dict[str, Any]:
    return public_classification()


@router.get("/industry/csrc-classification")
def industry_csrc_classification() -> dict[str, Any]:
    return public_csrc_classification()


@router.get("/industry/chains/{chain_id}")
def industry_chain(request: Request, chain_id: str) -> dict[str, Any]:
    try:
        value = read_publication(_root(request), "chains", chain_id)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(404, "产业链尚未发布") from error
    if not value:
        raise HTTPException(404, "产业链尚未发布")
    return value


@router.get("/industry/entities/{entity_id}")
def industry_entity(request: Request, entity_id: str, chainId: str = Query(min_length=16, max_length=16)) -> dict[str, Any]:
    chain = industry_chain(request, chainId)
    node = next((item for item in chain["nodes"] if item["id"] == entity_id), None)
    if node is None:
        raise HTTPException(404, "产业节点不存在")
    return {"node": node, "chainId": chainId, "relationships": [edge for edge in chain["edges"]
            if edge["fromNode"] == entity_id or edge["toNode"] == entity_id], "version": chain["version"]}


@router.get("/industry/companies/{instrument_key}/chains")
def company_chains(request: Request, instrument_key: str) -> dict[str, Any]:
    value = read_publication(_root(request), "companies")
    return {"instrumentKey": instrument_key, "chains": value.get("items", {}).get(instrument_key, []),
            "version": value.get("version")}


@router.get("/quant/methods")
def quant_methods(request: Request, q: str = "", category: str = "", kind: str = "") -> dict[str, Any]:
    value = methods(_root(request))
    items = value["methods"]
    if q.strip():
        needle = q.strip().casefold()
        items = [item for item in items if needle in (item["name"] + " " + item["summary"]).casefold()]
    if category:
        items = [item for item in items if item["category"] == category]
    if kind:
        items = [item for item in items if item["kind"] == kind]
    return {**value, "methods": items}


@router.get("/quant/methods/{method_id}")
def quant_method(request: Request, method_id: str) -> dict[str, Any]:
    result = method(_root(request), method_id)
    if result is None:
        raise HTTPException(404, "研究方法不存在")
    return result


@router.get("/quant/methods/{method_id}/readiness")
def quant_readiness(request: Request, method_id: str, instrumentId: str,
                    start: date, end: date, window: int | None = None) -> dict[str, Any]:
    try:
        return readiness(_root(request), method_id, instrumentId, start.isoformat(), end.isoformat(), window=window)
    except KeyError as error:
        raise HTTPException(404, "研究方法不存在") from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post("/quant/runs")
def quant_run(request: Request, body: DiagnosticRunRequest) -> dict[str, Any]:
    try:
        return run_diagnostic(_root(request), body.methodId, body.instrumentId,
                              body.start.isoformat(), body.end.isoformat(), window=body.window)
    except KeyError as error:
        raise HTTPException(404, "研究方法不存在") from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/quant/runs/{run_id}")
def quant_result(request: Request, run_id: str) -> dict[str, Any]:
    try:
        result = load_run(_root(request), run_id)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    if result is None:
        raise HTTPException(404, "本地实验不存在")
    return result


@router.post("/quant/runs/{run_id}/reproduce")
def quant_reproduce(request: Request, run_id: str) -> dict[str, Any]:
    try:
        return reproduce_run(_root(request), run_id)
    except FileNotFoundError as error:
        raise HTTPException(404, "本地实验不存在") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
