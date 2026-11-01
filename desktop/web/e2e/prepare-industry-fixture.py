"""Build a deterministic atlas for browser tests from the existing synthetic fixture."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys

from market_monitor.industry_atlas import build_atlas
from market_monitor.research_corpus import add_candidate, process_pages, review_candidate, scan_reports
from market_monitor.research_publication import publish_research
import pymupdf
import sqlite3


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "desktop" / "tests"))
from test_industry_atlas import LEGACY_HTML, _chain_index  # noqa: E402


def main() -> None:
    data_root = ROOT / "artifacts" / "e2e-customer"
    research_root = data_root / "research"
    assert research_root.resolve().is_relative_to(data_root.resolve())
    if research_root.exists():
        shutil.rmtree(research_root)
    fixture = data_root / "industry" / "legacy-fixture.html"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text(LEGACY_HTML, encoding="utf-8")
    f10_dir = data_root / "industry" / "f10"
    f10_dir.mkdir(parents=True, exist_ok=True)
    companies = [
        {"code": "300661", "name": "圣邦股份", "full_name": "合成半导体公司甲", "market": "CN",
         "total_market_cap": 500, "quote_as_of": "2026-10-09T15:00:00+08:00",
         "quote_source": "synthetic_f10_fixture", "industry": "电子设备-半导体"},
        {"code": "01801", "name": "信达生物", "full_name": "合成生物公司乙", "market": "HK",
         "total_market_cap": 600, "quote_as_of": "2026-10-09T15:00:00+08:00",
         "quote_source": "synthetic_f10_fixture", "industry": "生物制品"},
    ]
    for market in ("cn", "hk"):
        rows = [company for company in companies if company["market"] == market.upper()]
        (f10_dir / f"{market}_f10.jsonl").write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows), encoding="utf-8")
    output = data_root / "industry" / "build"
    output.mkdir(parents=True, exist_ok=True)
    (output / "chain_index.json").write_text(
        json.dumps(_chain_index(), ensure_ascii=False), encoding="utf-8")
    build_atlas(output, data_root=data_root,
                legacy_html_path=fixture, f10_dir=f10_dir)

    research_source = data_root / "synthetic-research" / "行业产业链研报"
    research_source.mkdir(parents=True, exist_ok=True)
    research_pdf = research_source / "synthetic-industry.pdf"
    evidence = "Silicon wafers are processed into chips for electronics."
    if not research_pdf.is_file():
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((40, 60), evidence)
            document.save(research_pdf)
    scan_reports(data_root, [research_source])
    process_pages(data_root, use_ocr=False)
    with sqlite3.connect(data_root / "research" / "private" / "corpus.sqlite3") as connection:
        digest = connection.execute("SELECT sha256 FROM documents ORDER BY sha256 LIMIT 1").fetchone()[0]
    candidate = add_candidate(data_root, digest, 1, "INDUSTRY_RELATION", {
        "chain": "半导体", "sourceType": "RAW_MATERIAL", "sourceName": "硅片",
        "predicate": "INPUT_TO", "targetType": "PRODUCT", "targetName": "芯片",
        "summary": "硅片在芯片制造中充当上游材料。", "stage": "上游",
    }, evidence)
    try:
        review_candidate(data_root, candidate, "APPROVED", "synthetic-e2e", "synthetic fixture",
                         current_verified=True)
    except ValueError:
        pass
    publish_research(data_root)


if __name__ == "__main__":
    main()
