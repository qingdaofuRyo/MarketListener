"""Run a reproducible 60-report local PDF extraction pilot.

The resulting performance evidence is private runtime output. Accuracy cannot
be inferred from successful parsing; it requires separate page-level review.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "desktop" / "src"))

from market_monitor.research_corpus import corpus_status, process_pages  # noqa: E402


def select_sample(data_root: Path, per_kind: int = 20) -> list[dict]:
    database = data_root / "research" / "private" / "corpus.sqlite3"
    if not database.is_file():
        raise FileNotFoundError("请先在内部管理台扫描两份研报目录")
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = [dict(item) for item in connection.execute(
            "SELECT sha256,document_kind,page_count FROM documents ORDER BY document_kind,page_count,sha256"
        )]
    result: list[dict] = []
    for kind in ("INDUSTRY", "MIXED", "QUANT"):
        group = [item for item in rows if item["document_kind"] == kind]
        if len(group) < per_kind:
            raise ValueError(f"{kind} 文档不足 {per_kind} 篇")
        # Sorted page counts provide short, medium and long documents without
        # hand-picking easy pages or excluding difficult scans.
        for position in range(per_kind):
            index = round((position + .5) * len(group) / per_kind - .5)
            result.append(group[index])
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=PROJECT / "data_control")
    parser.add_argument("--output", type=Path, default=PROJECT / "reports" / "research" / "pilot.json")
    parser.add_argument("--no-ocr", action="store_true")
    args = parser.parse_args()
    sample = select_sample(args.data_root)
    selected = {item["sha256"] for item in sample}
    started = time.monotonic()
    progress = process_pages(args.data_root, max_pages=sum(item["page_count"] for item in sample),
                             use_ocr=not args.no_ocr, only_documents=selected)
    elapsed = time.monotonic() - started
    result = {"generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "sampleDocuments": len(sample), "samplePages": sum(item["page_count"] for item in sample),
              "selection": "20 documents per kind (industry, mixed, quant), spread across page counts",
              "elapsedSeconds": round(elapsed, 2), "secondsPerNewPage": round(elapsed / max(progress["processed"], 1), 3),
              "progress": progress, "corpus": corpus_status(args.data_root),
              "qualityAssessment": "NOT_REVIEWED; numerical and figure accuracy must be manually audited"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
