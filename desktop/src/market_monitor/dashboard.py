"""Local health dashboard: runs, partitions, storage and quarantine status."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .storage import MarketStore


@dataclass(frozen=True)
class HealthReport:
    generated_at: str
    stale_after_seconds: int
    sources: tuple[dict[str, Any], ...]
    partitions: tuple[dict[str, Any], ...]
    quarantine: tuple[dict[str, Any], ...]
    storage: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "stale_after_seconds": self.stale_after_seconds,
            "sources": list(self.sources),
            "partitions": list(self.partitions),
            "quarantine": list(self.quarantine),
            "storage": self.storage,
        }


def build_health_report(
    data_root: Path,
    *,
    stale_after_seconds: int = 24 * 3600,
    now: datetime | None = None,
) -> HealthReport:
    """Read the local store and surface failures, staleness and storage use."""

    current = now or datetime.now(timezone.utc)
    runs: list[dict[str, Any]] = []
    partitions: list[dict[str, Any]] = []
    try:
        store = MarketStore(data_root)
        try:
            runs = [
                {
                    "run_id": row[0], "provider": row[1], "status": row[2], "started_at": row[3],
                    "completed_at": row[4], "detail": row[5],
                }
                for row in store.connection.execute(
                    "SELECT run_id, provider, status, started_at, completed_at, detail FROM runs ORDER BY started_at DESC"
                ).fetchall()
            ]
            partitions = [
                {
                    "partition_id": row[0], "file_path": row[1], "row_count": row[2], "data_cutoff": row[3],
                    "status": row[4], "updated_at": row[5], "stale": _is_stale(row[5], current, stale_after_seconds),
                }
                for row in store.connection.execute(
                    """SELECT partition_id, file_path, row_count, data_cutoff, status, updated_at
                    FROM partitions ORDER BY updated_at DESC"""
                ).fetchall()
            ]
        finally:
            store.close()
    except Exception as error:
        # DuckDB takes an exclusive Windows file lock while a bulk writer is
        # active.  The web terminal stays usable from Silver in that window;
        # health simply reports that catalog-only detail will refresh later.
        runs = [{
            "run_id": "catalog-busy", "provider": "本地数据目录", "status": "RUNNING",
            "started_at": current.isoformat(timespec="seconds"), "completed_at": None,
            "detail": f"数据正在写入，运行台账将在任务完成后刷新：{type(error).__name__}",
        }]
    quarantine = _list_quarantine(data_root)
    return HealthReport(
        generated_at=current.isoformat(timespec="seconds"),
        stale_after_seconds=stale_after_seconds,
        sources=tuple(runs),
        partitions=tuple(partitions),
        quarantine=tuple(quarantine),
        storage=_storage_usage(data_root),
    )


def render_markdown(report: HealthReport) -> str:
    lines = [
        "# 本地健康看板",
        "",
        f"- 生成时间：{report.generated_at}",
        f"- 分区陈旧阈值：{report.stale_after_seconds} 秒",
        "",
        "## 数据源运行",
        "",
        "| 来源 | 状态 | 最近开始 | 完成 | 详情 |",
        "|---|---|---|---|---|",
    ]
    for source in report.sources:
        lines.append(
            f"| {source['provider']} | {source['status']} | {source['started_at']} | "
            f"{source['completed_at'] or ''} | {_escape(source.get('detail') or '')} |"
        )
    lines += ["", "## 分区", "", "| 分区 | 行数 | 截止时间 | 状态 | 陈旧 |", "|---|---|---|---|---|"]
    for partition in report.partitions:
        lines.append(
            f"| {partition['partition_id']} | {partition['row_count']} | {partition['data_cutoff']} | "
            f"{partition['status']} | {'是' if partition['stale'] else '否'} |"
        )
    lines += ["", "## 隔离区", ""]
    for entry in report.quarantine:
        lines.append(f"- {entry['partition_id']}：{entry['issue_count']} 个问题（阻断={entry['blocking']}）")
    if not report.quarantine:
        lines.append("- 无")
    lines += ["", "## 存储", ""]
    for key, value in report.storage.items():
        lines.append(f"- {key}：{value} 字节")
    return "\n".join(lines) + "\n"


def _list_quarantine(data_root: Path) -> list[dict[str, Any]]:
    root = data_root / "quarantine"
    entries: list[dict[str, Any]] = []
    if not root.is_dir():
        return entries
    for directory in sorted(root.iterdir()):
        report_path = directory / "quality-report.json"
        if not report_path.is_file():
            continue
        try:
            document = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            entries.append({"partition_id": directory.name, "issue_count": -1, "blocking": True})
            continue
        entries.append(
            {
                "partition_id": str(document.get("partition_id", directory.name)),
                "issue_count": len(document.get("issues", [])),
                "blocking": bool(document.get("blocking", True)),
            }
        )
    return entries


def _storage_usage(data_root: Path) -> dict[str, int]:
    usage: dict[str, int] = {}
    for name in ("bronze", "silver", "quarantine"):
        directory = data_root / name
        total = 0
        if directory.is_dir():
            for path in directory.rglob("*"):
                if path.is_file():
                    total += path.stat().st_size
        usage[name] = total
    usage["total"] = sum(usage.values())
    return usage


def _is_stale(value: str, now: datetime, stale_after_seconds: int) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed) > timedelta(seconds=stale_after_seconds)


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")
