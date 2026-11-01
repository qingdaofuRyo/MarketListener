"""Resumable, lower-priority page extraction over the private PDF manifest."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "desktop" / "src"))

from market_monitor.research_corpus import corpus_status, process_pages  # noqa: E402


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=PROJECT / "data_control")
    parser.add_argument("--batch-pages", type=int, default=500)
    parser.add_argument("--retry-errors", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.batch_pages <= 5000:
        parser.error("--batch-pages must be between 1 and 5000")
    state = args.data_root / "research" / "private" / "full-extraction.json"
    pause = args.data_root / "research" / "private" / "pause.flag"
    total_processed = 0
    started = time.monotonic()
    retry_errors = args.retry_errors
    while not pause.exists():
        result = process_pages(args.data_root, max_pages=args.batch_pages,
                               retry_problem_pages=retry_errors, should_stop=pause.exists)
        retry_errors = False
        total_processed += result["processed"]
        status = corpus_status(args.data_root)
        phase = ("PAUSED" if pause.exists() else "COMPLETE" if status["pendingPages"] == 0
                 else "REVIEW_REQUIRED" if result["processed"] == 0 else "RUNNING")
        report = {"phase": phase, "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "pid": os.getpid(), "sessionProcessed": total_processed,
                  "elapsedSeconds": round(time.monotonic() - started, 1),
                  "lastBatch": result, "corpus": status}
        _write(state, report)
        if result["processed"] == 0 or phase != "RUNNING":
            print(json.dumps(report, ensure_ascii=False), flush=True)
            break
    if pause.exists():
        status = corpus_status(args.data_root)
        _write(state, {"phase": "PAUSED", "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "pid": os.getpid(), "sessionProcessed": total_processed, "corpus": status})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
