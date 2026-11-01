"""Prepare and score a private human audit of the 60-report extraction pilot.

The generated workbook contains only local document hashes and review entries.
It is never included in a customer publication or API response.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "desktop" / "src"))

from scripts.research_pilot import select_sample  # noqa: E402
from market_monitor.research_corpus import _visual_reasons  # noqa: E402


def _database(data_root: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(data_root / "research" / "private" / "corpus.sqlite3")
    connection.row_factory = sqlite3.Row
    return connection


def prepare(data_root: Path) -> dict:
    """Choose an auditable set of real pages across text, OCR and visuals."""

    sample = select_sample(data_root)
    documents = []
    with _database(data_root) as connection:
        for document in sample:
            rows = connection.execute("""
                SELECT p.page,p.status,p.text,p.tables_json,p.figures_json,o.engine
                FROM pages p LEFT JOIN page_ocr o ON o.sha256=p.sha256 AND o.page=p.page
                WHERE p.sha256=? ORDER BY p.page
            """, (document["sha256"],)).fetchall()
            if len(rows) != document["page_count"]:
                raise ValueError("Pilot document has unprocessed physical pages")
            classes: dict[str, list[int]] = {"OCR": [], "TABLE": [], "FIGURE": []}
            for row in rows:
                if row["engine"]:
                    classes["OCR"].append(row["page"])
                tables = json.loads(row["tables_json"])
                figures = json.loads(row["figures_json"])
                reasons = _visual_reasons(row["text"], tables, figures)
                if "TABLE" in reasons:
                    classes["TABLE"].append(row["page"])
                if any(reason in reasons for reason in ("LARGE_IMAGE", "VECTOR_DIAGRAM", "CAPTION")):
                    classes["FIGURE"].append(row["page"])
            numerical_pages = [row["page"] for row in rows if len(row["text"]) >= 100 and
                               len(re.findall(r"\d+(?:\.\d+)?%?", row["text"])) >= 3]
            selected = {numerical_pages[len(numerical_pages) // 2] if numerical_pages else 1}
            for group in classes.values():
                if group:
                    selected.add(group[len(group) // 2])
            pages = [{"page": number, "status": rows[number - 1]["status"],
                      "classes": [name for name, group in classes.items() if number in group],
                      "reviewed": False, "reviewer": "", "numericChecks": [], "visualChecks": []}
                     for number in sorted(selected)]
            documents.append({"sha256": document["sha256"], "kind": document["document_kind"],
                              "pageCount": document["page_count"], "longReport": document["page_count"] >= 50,
                              "pages": pages})
    return {"schema": "research-quality-audit-v1", "documents": documents,
            "instructions": {
                "numericChecks": "逐项手工填写 value、unit、period、correct；correct 仅在三者均与原页一致时为 true",
                "visualChecks": "逐项手工填写 kind(TABLE/CHART/FLOW)、field、captured；字段须与原图核对",
                "reviewed": "逐页人工对照原图和抽取结果后设为 true，并填写 reviewer",
            }}


def evaluate(workbook: dict, expected_sample: list[dict]) -> dict:
    """Fail closed until every sample document and enough gold fields are reviewed."""

    if workbook.get("schema") != "research-quality-audit-v1":
        raise ValueError("Unknown quality workbook schema")
    documents = workbook.get("documents")
    if not isinstance(documents, list) or len(documents) != 60:
        raise ValueError("Quality audit requires exactly 60 sampled reports")
    if len({item.get("sha256") for item in documents}) != 60:
        raise ValueError("Quality audit contains duplicate reports")
    expected = {(item["sha256"], item["document_kind"], item["page_count"])
                for item in expected_sample}
    actual = {(item.get("sha256"), item.get("kind"), item.get("pageCount"))
              for item in documents}
    if len(expected) != 60 or actual != expected:
        raise ValueError("Quality workbook differs from the pinned 60-report sample")
    missing = []
    category = Counter()
    numeric_total = numeric_correct = visual_total = visual_captured = 0
    for document in documents:
        if document.get("longReport"):
            category["LONG"] += 1
        pages = document.get("pages")
        if not isinstance(pages, list) or not pages:
            missing.append(document.get("sha256"))
            continue
        for page in pages:
            for label in page.get("classes", []):
                category[label] += 1
            if page.get("status") not in {"TEXT_OK", "OCR_OK", "CONFIRMED_BLANK"}:
                missing.append(f"{document['sha256']}:{page.get('page')}:source unresolved")
            if page.get("reviewed") is not True or not str(page.get("reviewer") or "").strip():
                missing.append(f"{document['sha256']}:{page.get('page')}:human review missing")
            for item in page.get("numericChecks", []):
                if not all(str(item.get(field) or "").strip() for field in ("value", "unit", "period")):
                    missing.append(f"{document['sha256']}:{page.get('page')}:numeric tuple incomplete")
                if type(item.get("correct")) is not bool:
                    missing.append(f"{document['sha256']}:{page.get('page')}:numeric decision missing")
                else:
                    numeric_total += 1
                    numeric_correct += int(item["correct"])
            for item in page.get("visualChecks", []):
                if item.get("kind") not in {"TABLE", "CHART", "FLOW"} or not str(item.get("field") or "").strip():
                    missing.append(f"{document['sha256']}:{page.get('page')}:visual field incomplete")
                if type(item.get("captured")) is not bool:
                    missing.append(f"{document['sha256']}:{page.get('page')}:visual decision missing")
                else:
                    visual_total += 1
                    visual_captured += int(item["captured"])
    for label in ("OCR", "TABLE", "FIGURE", "LONG"):
        if not category[label]:
            missing.append(f"No {label} sample")
    if numeric_total < 100:
        missing.append("At least 100 human-checked numeric value/unit/period tuples required")
    if visual_total < 20:
        missing.append("At least 20 human-checked table/chart/flow fields required")
    numeric_rate = numeric_correct / numeric_total if numeric_total else None
    visual_rate = visual_captured / visual_total if visual_total else None
    return {"status": ("PASS" if not missing and numeric_rate is not None and numeric_rate >= .99
                       and visual_rate is not None and visual_rate >= .90 else "NOT_PASSED"),
            "numeric": {"correct": numeric_correct, "total": numeric_total, "rate": numeric_rate,
                        "requiredRate": .99},
            "visual": {"captured": visual_captured, "total": visual_total, "rate": visual_rate,
                       "requiredRate": .90},
            "categories": dict(category), "missing": missing[:200], "missingCount": len(missing)}


def verify_page_selection(workbook: dict, data_root: Path) -> None:
    """Check that annotated page identities and source states still match the corpus."""

    current = prepare(data_root)
    expected = {item["sha256"]: item for item in current["documents"]}
    for item in workbook.get("documents", []):
        original = expected.get(item.get("sha256"))
        if original is None:
            raise ValueError("Quality workbook contains a non-sample document")
        audited = [(page.get("page"), page.get("status"), page.get("classes"))
                   for page in item.get("pages", [])]
        actual = [(page["page"], page["status"], page["classes"])
                  for page in original["pages"]]
        if audited != actual:
            raise ValueError("Quality workbook page selection or source state changed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "evaluate"))
    parser.add_argument("--data-root", type=Path, default=PROJECT / "data_control")
    args = parser.parse_args()
    workbook_path = args.data_root / "research" / "private" / "quality-audit.json"
    if args.action == "prepare":
        if workbook_path.exists():
            raise FileExistsError("Existing human annotations would be overwritten")
        workbook = prepare(args.data_root)
        workbook_path.write_text(json.dumps(workbook, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"path": str(workbook_path), "documents": len(workbook["documents"]),
                          "pages": sum(len(item["pages"]) for item in workbook["documents"])}, ensure_ascii=False))
    else:
        workbook = json.loads(workbook_path.read_text(encoding="utf-8"))
        verify_page_selection(workbook, args.data_root)
        print(json.dumps(evaluate(workbook, select_sample(args.data_root)),
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
