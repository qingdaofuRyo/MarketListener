"""Generate the versioned GB/T 4754-2017 navigation from NBS's official XLSX."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from openpyxl import load_workbook

PROJECT = Path(__file__).resolve().parents[1]
SOURCE_URL = "https://www.stats.gov.cn/sj/tjbz/gmjjhyfl/202302/P020230213403084213497.xlsx"
EXPECTED = {1: 20, 2: 97, 3: 473, 4: 1382}


def build(source: Path) -> dict:
    raw = source.read_bytes()
    workbook = load_workbook(source, read_only=True, data_only=True)
    rows: list[dict[str, str | int | None]] = []
    seen: set[str] = set()
    sector: str | None = None
    try:
        for row in workbook.active.values:
            for cell in row[:3]:
                code = str(cell or "").strip()
                if not re.fullmatch(r"[A-T]|\d{2,4}", code) or code in seen:
                    continue
                name = str(row[3] or "").strip()
                if not name or "\ufffd" in name:
                    raise ValueError(f"Unusable official classification label at {code}")
                seen.add(code)
                if len(code) == 1:
                    sector = code
                    parent = None
                elif len(code) == 2:
                    if sector is None:
                        raise ValueError(f"Division {code} has no sector")
                    parent = sector
                else:
                    parent = code[:-1]
                rows.append({"code": code, "name": name, "parentCode": parent,
                             "level": len(code), "sector": sector})
    finally:
        workbook.close()
    counts = {level: sum(item["level"] == level for item in rows) for level in EXPECTED}
    if counts != EXPECTED:
        raise ValueError(f"Official classification count mismatch: {counts}")
    codes = {str(item["code"]) for item in rows}
    if any(item["parentCode"] is not None and item["parentCode"] not in codes for item in rows):
        raise ValueError("Industry category parent is missing")
    return {"schema": "marketlistener/gbt4754-classification/v1", "standard": "GB/T 4754-2017",
            "revision": "第1号修改单", "sourceUrl": SOURCE_URL,
            "sourceSha256": hashlib.sha256(raw).hexdigest(), "counts": counts, "items": rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=PROJECT / "data_control" / "research" / "sources" /
                        "nbs-gbt4754-2017-revised.xlsx")
    parser.add_argument("--output", type=Path, default=PROJECT / "desktop" / "src" / "market_monitor" /
                        "config" / "nbs_gbt4754_2017.json")
    args = parser.parse_args()
    document = build(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print({"items": len(document["items"]), "counts": document["counts"],
           "sourceSha256": document["sourceSha256"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
