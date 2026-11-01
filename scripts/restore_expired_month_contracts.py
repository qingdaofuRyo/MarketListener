"""Restore only the month-contract partitions archived by the 2026-09-26 cleanup."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

import duckdb

from market_monitor.market_data_version import advance_market_data_version, settle_market_data_version


_SCHEMA = "market-listener/retired-expired-month-contracts/v1"
_COLUMNS = (
    "partition_id", "file_path", "row_count", "data_cutoff", "sha256",
    "source_run_id", "status", "updated_at",
)


def _inside(root: Path, path: Path) -> Path:
    parent = root.resolve()
    resolved = path.resolve()
    if resolved == parent or not resolved.is_relative_to(parent):
        raise ValueError(f"路径超出预期目录: {path}")
    return resolved


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _active_operations(root: Path) -> int:
    path = root / "operations.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(rows, list):
        return 0
    return sum(
        isinstance(row, dict) and str(row.get("status", "")).upper() in {"QUEUED", "RUNNING"}
        for row in rows
    )


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _inventory(data_root: Path, manifest_path: Path) -> dict[str, Any]:
    root = data_root.resolve()
    backup_manifest = _inside(root / "retired", manifest_path)
    backup_root = backup_manifest.parent
    payload = json.loads(backup_manifest.read_text(encoding="utf-8"))
    if payload.get("schema") != _SCHEMA or payload.get("status") != "RETIRED":
        raise ValueError("备份清单不是已完成的过期月份合约清理记录")
    partitions = payload.get("partitionRows")
    if not isinstance(partitions, list) or len(partitions) != 371:
        raise ValueError("备份分区清单与预期的371个分区不一致")
    ids = [str(row["partitionId"]) for row in partitions]
    if len(set(ids)) != len(ids):
        raise ValueError("备份清单中存在重复分区ID")
    if len(set(payload.get("instrumentIds", []))) != 64:
        raise ValueError("备份清单中的合约标的数量与预期不一致")
    backup_catalog = backup_root / "catalog.duckdb"
    current_catalog = root / "catalog.duckdb"
    if not backup_catalog.is_file() or not current_catalog.is_file():
        raise FileNotFoundError("恢复所需的备份或现行 catalog.duckdb 不存在")

    with duckdb.connect(str(backup_catalog), read_only=True) as connection:
        backup_columns = tuple(row[0] for row in connection.execute("DESCRIBE partitions").fetchall())
        rows = connection.execute(
            "SELECT " + ", ".join(_COLUMNS) + " FROM partitions WHERE partition_id IN ("
            + ",".join("?" for _ in ids) + ")",
            ids,
        ).fetchall()
    with duckdb.connect(str(current_catalog), read_only=True) as connection:
        current_columns = tuple(row[0] for row in connection.execute("DESCRIBE partitions").fetchall())
        present = int(connection.execute(
            "SELECT count(*) FROM partitions WHERE partition_id IN ("
            + ",".join("?" for _ in ids) + ")",
            ids,
        ).fetchone()[0])
    if backup_columns != _COLUMNS or current_columns != _COLUMNS or len(rows) != len(ids):
        raise ValueError("备份与现行 catalog 的分区结构或目标记录不匹配")
    if present:
        raise ValueError(f"现行 catalog 已有{present}条目标分区，停止以防覆盖")
    from_catalog = {str(row[0]): row for row in rows}
    file_pairs: list[tuple[Path, Path]] = []
    bytes_total = 0
    for partition in partitions:
        row = from_catalog[str(partition["partitionId"])]
        if (
            str(row[1]) != str(partition["filePath"])
            or int(row[2]) != int(partition["rowCount"])
            or str(row[4]) != str(partition["sha256"])
        ):
            raise ValueError(f"备份 catalog 与清单内容不一致: {row[0]}")
        relative = Path(str(partition["filePath"]))
        destination = _inside(root / "silver", root / relative)
        source = _inside(backup_root / "silver", backup_root / relative)
        if destination.is_file():
            raise FileExistsError(f"目标文件已存在，停止以防覆盖: {destination}")
        if not source.is_file() or _hash(source) != str(partition["sha256"]):
            raise ValueError(f"备份文件缺失或校验失败: {source}")
        file_pairs.append((source, destination))
        bytes_total += source.stat().st_size
    if len({str(destination) for _, destination in file_pairs}) != len(file_pairs):
        raise ValueError("多个分区指向同一目标文件")
    row_total = sum(int(partition["rowCount"]) for partition in partitions)
    if row_total != int(payload.get("rows", -1)):
        raise ValueError("备份行数与清单汇总不一致")
    return {
        "root": root,
        "backupRoot": backup_root,
        "catalog": current_catalog,
        "rows": rows,
        "filePairs": file_pairs,
        "series": len(set(payload["instrumentIds"])),
        "bars": row_total,
        "bytes": bytes_total,
    }


def run(data_root: Path, manifest_path: Path, *, apply: bool = False) -> dict[str, Any]:
    inventory = _inventory(data_root, manifest_path)
    root = inventory["root"]
    result: dict[str, Any] = {
        "status": "PLAN_ONLY",
        "sourceBackup": str(inventory["backupRoot"]),
        "contracts": inventory["series"],
        "partitions": len(inventory["rows"]),
        "files": len(inventory["filePairs"]),
        "rows": inventory["bars"],
        "bytes": inventory["bytes"],
    }
    if not apply:
        return result
    active = _active_operations(root)
    if active:
        raise RuntimeError(f"存在{active}个排队或运行中的任务，停止恢复")
    restore_root = root / "retired" / (
        "restored-month-contracts-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    )
    restore_root.mkdir(parents=True, exist_ok=False)
    result["restoreAudit"] = str(restore_root)
    copied: list[Path] = []
    committed = False
    pending_revision: str | None = None
    try:
        with duckdb.connect(str(inventory["catalog"])) as connection:
            connection.execute("CHECKPOINT")
        shutil.copy2(inventory["catalog"], restore_root / "catalog.before.duckdb")
        current_version = root / "state" / "market_data_version.json"
        if current_version.is_file():
            shutil.copy2(current_version, restore_root / "market_data_version.before.json")
        old_revision, pending_revision = advance_market_data_version(root, pending=True)
        for source, destination in inventory["filePairs"]:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied.append(destination)
            if _hash(destination) != _hash(source):
                raise RuntimeError(f"恢复文件校验失败: {destination}")
        with duckdb.connect(str(inventory["catalog"])) as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                connection.executemany(
                    "INSERT INTO partitions (" + ", ".join(_COLUMNS) + ") VALUES ("
                    + ",".join("?" for _ in _COLUMNS) + ")",
                    inventory["rows"],
                )
                restored = int(connection.execute(
                    "SELECT count(*) FROM partitions WHERE partition_id IN ("
                    + ",".join("?" for _ in inventory["rows"]) + ")",
                    [row[0] for row in inventory["rows"]],
                ).fetchone()[0])
                if restored != len(inventory["rows"]):
                    raise RuntimeError(f"分区恢复数量不符: {restored}")
                connection.execute("COMMIT")
                committed = True
            except Exception:
                connection.execute("ROLLBACK")
                raise
        settle_market_data_version(root, pending_revision)
        result.update(
            status="RESTORED", previousDataRevision=old_revision,
            dataRevision=pending_revision, restoredFiles=len(copied),
        )
    except Exception as error:
        if not committed:
            for path in copied:
                path.unlink(missing_ok=True)
            if pending_revision is not None:
                settle_market_data_version(root, pending_revision)
        result.update(
            status="RESTORED_VERSION_PENDING" if committed else "FAILED_RECOVERABLE",
            error=f"{type(error).__name__}: {error}",
        )
        _write_json(restore_root / "manifest.json", result)
        raise
    _write_json(restore_root / "manifest.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.data_root, args.manifest, apply=args.apply), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
