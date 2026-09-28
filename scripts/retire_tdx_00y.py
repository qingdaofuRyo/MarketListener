"""Retire exact financial-terminal 00Y series with recoverable local backups."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

import duckdb

from market_monitor.market_data_version import advance_market_data_version
from market_monitor.market_query_cache import refresh_kline_cache_partitions

PATTERN = r"GLOBAL\.(COMEX|NYMEX|CBOT)\.FUTURE\.[A-Z0-9_-]+00Y\.TDX_LOCAL"


def inside(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("Path outside explicit data directory")
    return path


def run(root: Path, terminal: Path, apply: bool):
    root = root.resolve()
    connection = duckdb.connect(str(root / "catalog.duckdb"), read_only=not apply)
    changes = []
    for record in connection.execute("SELECT partition_id,file_path FROM partitions WHERE status='COMPLETE'").fetchall():
        if "market=GLOBAL" not in record[1] or "asset_type=FUTURE" not in record[1]:
            continue
        path = inside(root / "silver", root / record[1])
        count = connection.execute("SELECT count(*) FROM read_parquet(?,hive_partitioning=false) WHERE regexp_full_match(instrument_id,?)", [str(path), PATTERN]).fetchone()[0]
        if count:
            changes.append((record[0], path, count))
    raw = [p for folder in ("lday", "fzline") for p in (terminal / "vipdoc/ds" / folder).glob("*")
           if re.fullmatch(r"(?:16|17|18)#[A-Z0-9_-]+00Y\.(?:day|lc5)", p.name, re.I)]
    report = {"partitions": len(changes), "rows": sum(r[2] for r in changes), "rawFiles": len(raw), "apply": apply}
    if not apply:
        connection.close()
        return report
    backup = root / "retired" / ("00Y-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    backup.mkdir(parents=True, exist_ok=False)
    # Closed/checkpointed catalog copy is a recovery record, not a live DB clone.
    connection.execute("CHECKPOINT")
    connection.close()
    shutil.copy2(root / "catalog.duckdb", backup / "catalog.duckdb")
    for _, path, _ in changes:
        target = backup / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    manifest = {**report, "backup": str(backup), "files": [str(p.relative_to(root)) for _, p, _ in changes],
                "raw": [str(p) for p in raw], "status": "BACKED_UP"}
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    connection = duckdb.connect(str(root / "catalog.duckdb"))
    try:
        connection.execute("BEGIN TRANSACTION")
        for partition, path, _ in changes:
            staged = backup / (partition + ".replacement.parquet")
            connection.execute("COPY (SELECT * FROM read_parquet($source,hive_partitioning=false) WHERE NOT regexp_full_match(instrument_id,$pattern)) TO $target (FORMAT PARQUET, COMPRESSION ZSTD)", {"source": str(path), "pattern": PATTERN, "target": str(staged)})
            count, cutoff = connection.execute("SELECT count(*),max(bar_open_time) FROM read_parquet(?,hive_partitioning=false)", [str(staged)]).fetchone()
            sha = hashlib.sha256(staged.read_bytes()).hexdigest()
            os.replace(staged, path)
            connection.execute("UPDATE partitions SET row_count=?,data_cutoff=coalesce(?,data_cutoff),sha256=?,updated_at=? WHERE partition_id=?", [count, cutoff, sha, datetime.now(timezone.utc).isoformat(), partition])
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        for _, path, _ in changes:
            shutil.copy2(backup / path.relative_to(root), path)
        raise
    finally:
        connection.close()
    for path in raw:
        source = inside(terminal / "vipdoc/ds", path)
        target = backup / "terminal" / source.relative_to(terminal.resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
    advance_market_data_version(root)
    # Existing cache paths use the caller's relative data-root spelling.
    cache_files = [Path(os.path.relpath(path, Path.cwd())) for _, path, _ in changes]
    refreshed = refresh_kline_cache_partitions(root, cache_files) if changes else True
    manifest.update(status="RETIRED", cacheRefreshed=refreshed)
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--tdx-root", type=Path, default=Path(r"C:\tongdaxin"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.data_root, args.tdx_root, args.apply), ensure_ascii=False, indent=2))
