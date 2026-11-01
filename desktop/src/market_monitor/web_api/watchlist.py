"""/api/personal/watchlist router: local watchlist persistence.

The watchlist is stored as ``data_control/personal/watchlist.json``.  This
router never writes to ``catalog.duckdb`` and relies on the web_app loopback
middleware to block remote POST/DELETE requests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from market_monitor.profile_gate import ProfileGateBusy, profile_write_gate

from .common import clean, load_inventory, load_json, now_iso, save_json

router = APIRouter(prefix="/api/personal", tags=["personal"])

# 生产环境默认数据根目录；测试或其他宿主可通过 ``app.state.data_root`` 覆盖。
_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_DATA_ROOT = _REPO_ROOT / "data_control"


def _data_root(request: Request) -> Path:
    configured = getattr(request.app.state, "data_root", None)
    if configured:
        return Path(configured)
    return _DEFAULT_DATA_ROOT


def _watchlist_path(data_root: Path) -> Path:
    return data_root / "personal" / "watchlist.json"


def _dashboard_path(data_root: Path) -> Path:
    return data_root / "personal" / "dashboard.json"


def _load_entries(data_root: Path) -> list[dict[str, Any]]:
    payload = load_json(_watchlist_path(data_root), default=None)
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        entries.append(
            {
                "instrumentId": str(item.get("instrumentId") or ""),
                "addedAt": str(item.get("addedAt") or ""),
                "note": str(item.get("note") or ""),
            }
        )
    return entries


def _save_entries(data_root: Path, entries: list[dict[str, Any]]) -> None:
    save_json(_watchlist_path(data_root), {"items": entries})


class WatchlistItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrumentId: str
    note: str = ""


class DashboardPanelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=80)
    metricId: str = Field(min_length=1, max_length=80)
    chartType: str = Field(default="line", pattern="^(line|bar)$")
    color: str = Field(default="#d64b4b", pattern=r"^#[0-9a-fA-F]{6}$")
    opacity: float = Field(default=0.16, ge=0.0, le=1.0)
    rangeDays: int = Field(default=0, ge=0, le=3650)
    width: str = Field(default="half", pattern="^(half|full)$")
    hidden: bool = False


class DashboardLayoutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    panels: list[DashboardPanelIn] = Field(default_factory=list, max_length=30)


@router.get("/watchlist")
def get_watchlist(request: Request) -> dict[str, Any]:
    return clean({"items": _load_entries(_data_root(request))})


@router.post("/watchlist")
def add_watchlist(request: Request, body: WatchlistItemIn) -> dict[str, Any]:
    """Add an instrument to the watchlist; duplicates return the existing entry."""
    data_root = _data_root(request)
    inventory = load_inventory(data_root)
    instrument_id = body.instrumentId.strip()
    if instrument_id not in inventory.instruments:
        raise HTTPException(status_code=400, detail="instrument not found in silver inventory")
    try:
        with profile_write_gate(data_root):
            entries = _load_entries(data_root)
            existing = next((entry for entry in entries if entry.get("instrumentId") == instrument_id), None)
            if existing is not None:
                return clean({"item": existing})
            entry = {"instrumentId": instrument_id, "addedAt": now_iso(), "note": body.note}
            entries.append(entry)
            _save_entries(data_root, entries)
            return clean({"item": entry})
    except ProfileGateBusy as error:
        raise HTTPException(status_code=503, detail=str(error), headers={"Retry-After": "2"}) from error


@router.delete("/watchlist/{instrument_id}")
def delete_watchlist(instrument_id: str, request: Request) -> dict[str, Any]:
    """Remove one watchlist entry; missing entries return 404."""
    data_root = _data_root(request)
    try:
        with profile_write_gate(data_root):
            entries = _load_entries(data_root)
            removed = next((entry for entry in entries if entry.get("instrumentId") == instrument_id), None)
            if removed is None:
                raise HTTPException(status_code=404, detail="watchlist item not found")
            remaining = [entry for entry in entries if entry.get("instrumentId") != instrument_id]
            _save_entries(data_root, remaining)
            return clean({"item": removed})
    except ProfileGateBusy as error:
        raise HTTPException(status_code=503, detail=str(error), headers={"Retry-After": "2"}) from error


@router.get("/dashboard")
def get_dashboard(request: Request) -> dict[str, Any]:
    payload = load_json(_dashboard_path(_data_root(request)), {"panels": []})
    return clean(payload if isinstance(payload, dict) else {"panels": []})


@router.put("/dashboard")
def save_dashboard(request: Request, body: DashboardLayoutIn) -> dict[str, Any]:
    payload = {"updatedAt": now_iso(), "panels": [panel.model_dump() for panel in body.panels]}
    try:
        with profile_write_gate(_data_root(request)):
            save_json(_dashboard_path(_data_root(request)), payload)
            return clean(payload)
    except ProfileGateBusy as error:
        raise HTTPException(status_code=503, detail=str(error), headers={"Retry-After": "2"}) from error


__all__ = ("router",)
