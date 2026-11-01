"""Bounded, credential-free per-session collection status evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping
from uuid import uuid4

_STATUSES = {"PASS", "SKIPPED", "PARTIAL_FAILURE", "FAILED", "BLOCKED"}
_ID = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_FRESHNESS = re.compile(r"\b(US_NONFARM_PAYROLLS_SA|PMI_CAIXIN_MANUFACTURING|PMI_CAIXIN_SERVICES):(STALE_OVER_180_DAYS|NO_OBSERVATIONS)\b")


def _freshness(value: object) -> list[dict[str, str]]:
    if not isinstance(value, str):
        return []
    return [{"seriesId": series, "reason": reason} for series, reason in dict.fromkeys(_FRESHNESS.findall(value))]


def _validated_freshness(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    pairs = []
    for item in value:
        if isinstance(item, dict):
            pairs.extend(_freshness(f"{item.get('seriesId', '')}:{item.get('reason', '')}"))
    return pairs[:3]


def _time(value: object) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def save_collector_history(root: Path, summary: Mapping[str, Any]) -> None:
    completed = _time(summary.get("completed_at"))
    if completed is None:
        raise ValueError("invalid session completion time")
    tasks = []
    for item in summary.get("tasks", []):
        if not isinstance(item, dict):
            continue
        dataset, status, rows = item.get("dataset_id"), item.get("status"), item.get("rows")
        if not isinstance(dataset, str) or not _ID.fullmatch(dataset) or not isinstance(status, str) or status not in _STATUSES:
            continue
        if type(rows) is not int or rows < 0:
            continue
        tasks.append({"datasetId": dataset, "status": status, "rowsProcessed": rows,
                      "freshnessWarnings": _freshness(item.get("detail")) if dataset == "MACRO_SERIES" else []})
    directory = Path(root) / "state" / "collector_history"
    directory.mkdir(parents=True, exist_ok=True)
    name = completed.strftime("%Y%m%dT%H%M%S") + "-" + datetime.now(timezone.utc).strftime("%f") + "-" + uuid4().hex + ".json"
    target = directory / name
    temporary = directory / ("." + name + ".tmp")
    try:
        temporary.write_text(json.dumps({"version": 1, "completedAt": completed.isoformat(), "tasks": tasks}), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def latest_collector_statuses(root: Path, *, limit: int = 256) -> dict[str, dict[str, Any]]:
    """Inspect newest bounded saved sessions; never surface raw provider errors."""
    directory = Path(root) / "state" / "collector_history"
    latest: dict[str, dict[str, Any]] = {}
    try:
        paths = sorted(directory.glob("*.json"), reverse=True)[:limit]
    except OSError:
        return latest
    for path in paths:
        try:
            if path.stat().st_size > 100_000:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1 or _time(data.get("completedAt")) is None:
                continue
            for item in data.get("tasks", []):
                if not isinstance(item, dict):
                    continue
                dataset, status, rows = item.get("datasetId"), item.get("status"), item.get("rowsProcessed")
                if (isinstance(dataset, str) and _ID.fullmatch(dataset) and isinstance(status, str) and status in _STATUSES
                        and type(rows) is int and rows >= 0 and dataset not in latest):
                    latest[dataset] = {"status": status, "rowsProcessed": rows, "sessionCompletedAt": data["completedAt"],
                                       "freshnessWarnings": _validated_freshness(item.get("freshnessWarnings"))}
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    try:
        previous = Path(root) / "control_summary.json"
        if previous.stat().st_size <= 100_000:
            data = json.loads(previous.read_text(encoding="utf-8"))
            completed = _time(data.get("completed_at")) if isinstance(data, dict) else None
            if completed:
                for item in data.get("tasks", []):
                    if not isinstance(item, dict):
                        continue
                    dataset, status, rows = item.get("dataset_id"), item.get("status"), item.get("rows")
                    if (isinstance(dataset, str) and _ID.fullmatch(dataset) and isinstance(status, str) and status in _STATUSES
                            and type(rows) is int and rows >= 0
                            and (dataset not in latest or completed > _time(latest[dataset]["sessionCompletedAt"]))):
                        latest[dataset] = {"status": status, "rowsProcessed": rows,
                                           "sessionCompletedAt": completed.isoformat(),
                                           "freshnessWarnings": _freshness(item.get("detail")) if dataset == "MACRO_SERIES" else []}
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return latest
