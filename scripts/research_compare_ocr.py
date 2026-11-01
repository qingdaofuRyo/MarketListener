"""Compare local Umi-OCR and RapidOCR on sampled private report pages.

This is a disagreement audit, not ground-truth accuracy. It prints no source
text, PDF title, filename or image, and writes only aggregate metrics.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "desktop" / "src"))

from market_monitor.report_ocr import _get_engine  # noqa: E402
from market_monitor.research_corpus import private_page_png  # noqa: E402
from market_monitor.research_umi_ocr import umi_ocr_png  # noqa: E402

_NUMBERS = re.compile(r"(?<!\w)\d+(?:\.\d+)?")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=PROJECT / "data_control")
    parser.add_argument("--output", type=Path, default=PROJECT / "reports" / "research" / "umi-ocr-comparison.json")
    parser.add_argument("--sample-pages", type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.sample_pages <= 100:
        parser.error("sample size must be 1–100")
    with sqlite3.connect(args.data_root / "research" / "private" / "corpus.sqlite3") as connection:
        rows = connection.execute("SELECT sha256,page FROM pages WHERE status='OCR_OK'").fetchall()
    chosen = random.Random(20261020).sample(rows, min(args.sample_pages, len(rows)))
    comparisons = []
    for sha256, page in chosen:
        png = private_page_png(args.data_root, sha256, page, dpi=180)
        umi = umi_ocr_png(png, dpi=180)
        rapid_rows, _elapsed = _get_engine()(png)
        rapid = "\n".join(str(item[1]) for item in rapid_rows or [])
        umi_numbers = set(_NUMBERS.findall(umi.text))
        rapid_numbers = set(_NUMBERS.findall(rapid))
        comparisons.append({"umiChars": len(umi.text), "rapidChars": len(rapid),
                            "umiConfidence": umi.mean_confidence,
                            "numberAgreement": len(umi_numbers & rapid_numbers) /
                            max(1, len(umi_numbers | rapid_numbers))})
    output = {"generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "pages": len(comparisons),
              "umiMedianChars": statistics.median(item["umiChars"] for item in comparisons) if comparisons else 0,
              "rapidMedianChars": statistics.median(item["rapidChars"] for item in comparisons) if comparisons else 0,
              "meanUmiConfidence": statistics.mean(item["umiConfidence"] for item in comparisons
                                                   if item["umiConfidence"] is not None) if any(
                  item["umiConfidence"] is not None for item in comparisons) else None,
              "meanNumericTokenAgreement": statistics.mean(item["numberAgreement"] for item in comparisons)
              if comparisons else None,
              "interpretation": "Agreement is not accuracy; manual page-level gold review is required"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
