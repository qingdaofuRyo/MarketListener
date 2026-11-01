"""Build the CSRC 2024 manufacturing subcategory to GB/T 4754 division map."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import pymupdf

PROJECT = Path(__file__).resolve().parents[1]
SOURCE_URL = ("https://www.csrc.gov.cn/csrc/c101954/c7520291/7520291/files/"
              "%E9%99%84%E4%BB%B61%EF%BC%9A%E3%80%8A%E4%B8%8A%E5%B8%82%E5%85%AC%E5%8F%B8"
              "%E8%A1%8C%E4%B8%9A%E7%BB%9F%E8%AE%A1%E5%88%86%E7%B1%BB%E4%B8%8E%E4%BB%A3"
              "%E7%A0%81%E3%80%8B.pdf")


def build(source: Path, nbs_file: Path) -> dict:
    nbs = json.loads(nbs_file.read_text(encoding="utf-8"))
    division_names = {item["code"]: item["name"] for item in nbs["items"]
                      if item["level"] == 2 and item["sector"] == "C"}
    groups: dict[str, dict] = {}
    current: str | None = None
    previous = 12
    with pymupdf.open(source) as document:
        for page in document.pages(7, 47):
            lines = [line.strip() for line in page.get_text().splitlines() if line.strip()]
            for index, code in enumerate(lines):
                if re.fullmatch(r"C[A-J]", code):
                    current = code
                    groups.setdefault(code, {"code": code, "name": lines[index + 1], "nbsDivisions": []})
                elif re.fullmatch(r"[D-T]", code):
                    current = None
                elif current and code in division_names and int(code) > previous:
                    groups[current]["nbsDivisions"].append({"code": code, "name": division_names[code]})
                    previous = int(code)
    if sorted(groups) != ["CA", "CB", "CC", "CD", "CE", "CF", "CG", "CH", "CI", "CJ"]:
        raise ValueError("CSRC manufacturing subcategory count mismatch")
    codes = [item["code"] for group in groups.values() for item in group["nbsDivisions"]]
    if codes != list(division_names):
        raise ValueError("CSRC to NBS manufacturing division range is incomplete")
    return {"schema": "marketlistener/csrc-industry-2024/v1", "standard": "JR/T 0020-2024",
            "sourceUrl": SOURCE_URL, "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "nbsStandard": "GB/T 4754-2017", "manufacturingSubcategories": list(groups.values()),
            "companyAssignmentPolicy": "仅采用正式行业分类结果或已披露的可核验归属；F10名称不自动推断"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=PROJECT / "data_control" / "research" / "sources" /
                        "csrc-listed-company-industry-2024.pdf")
    parser.add_argument("--nbs", type=Path, default=PROJECT / "desktop" / "src" / "market_monitor" /
                        "config" / "nbs_gbt4754_2017.json")
    parser.add_argument("--output", type=Path, default=PROJECT / "desktop" / "src" / "market_monitor" /
                        "config" / "csrc_industry_2024.json")
    args = parser.parse_args()
    value = build(args.source, args.nbs)
    args.output.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print({"groups": len(value["manufacturingSubcategories"]),
           "divisions": sum(len(item["nbsDivisions"]) for item in value["manufacturingSubcategories"]),
           "sourceSha256": value["sourceSha256"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
