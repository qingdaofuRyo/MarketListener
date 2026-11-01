"""Select one published ranking source per exchange and trading day.

Raw facts remain source-isolated in storage. Consumers that aggregate member
positions must select a complete source before adding positions; summing two
official observations of the same leaderboard would double count them.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence


DCE_BATCH_SOURCE = "official-dce-batch-member-ranking"
DCE_DESKTOP_BATCH_SOURCE = "official-dce-desktop-browser-batch-member-ranking"
CZCE_WORKBOOK_SOURCE = "official-czce-workbook-member-ranking"
CFFEX_PUBLISHED_SOURCE = "official-cffex-published-member-ranking"
SHFE_INE_DAT_SOURCE = "official-shfe-ine-dat-member-ranking"
GFEX_BROWSER_SOURCE = "official-gfex-browser-member-ranking"


def select_member_position_source_rows(
    ranks: Sequence[Mapping[str, Any]],
    coverage: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Keep every row of the best single source for each day and exchange.

    A source's own coverage record, rather than a different adapter's PASS,
    determines its completeness. Verified daily DCE archives and CZCE
    workbooks take precedence over older official adapters when complete.
    """

    coverage_by_source = {
        (str(item.get("trading_day")), str(item.get("exchange")), str(item.get("source"))): item
        for item in coverage
    }
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in ranks:
        key = (str(row["trading_day"]), str(row["exchange"]), str(row["source"]))
        groups[key].append(row)

    selected: dict[tuple[str, str], str] = {}
    selected_scores: dict[tuple[str, str], tuple[int, int, int, str, str]] = {}
    for day, exchange, source in set(groups) | set(coverage_by_source):
        source_rows = groups.get((day, exchange, source), [])
        # A non-official observation with no matching published rows cannot
        # suppress another source's actual rankings merely by claiming PASS.
        if not source_rows and not source.startswith("official-"):
            continue
        source_coverage = coverage_by_source.get((day, exchange, source), {})
        status = str(source_coverage.get("status") or "").upper()
        status_score = {"PASS": 4, "PARTIAL": 3, "NO_PUBLICATION": 2}.get(status, 1 if not status else 0)
        source_score = (
            4 if source == DCE_BATCH_SOURCE else
            3 if source in {
                DCE_DESKTOP_BATCH_SOURCE, CZCE_WORKBOOK_SOURCE,
                CFFEX_PUBLISHED_SOURCE, SHFE_INE_DAT_SOURCE,
                GFEX_BROWSER_SOURCE,
            } else
            2 if source == "official-dce-member-ranking" else
            1 if source.startswith("official-") else 0
        )
        quality_score = int(all(str(row.get("quality_status") or "") == "PASS" for row in source_rows))
        newest = max(
            [str(source_coverage.get("retrieved_at") or source_coverage.get("collected_at") or "")]
            + [str(row.get("retrieved_at") or row.get("collected_at") or "") for row in source_rows]
        )
        score = status_score, source_score, quality_score, newest, source
        key = (day, exchange)
        if score > selected_scores.get(key, (-1, -1, -1, "", "")):
            selected_scores[key] = score
            selected[key] = source

    return [
        row for row in ranks
        if str(row["source"]) == selected[(str(row["trading_day"]), str(row["exchange"]))]
    ]


__all__ = (
    "CFFEX_PUBLISHED_SOURCE", "CZCE_WORKBOOK_SOURCE", "DCE_BATCH_SOURCE",
    "DCE_DESKTOP_BATCH_SOURCE",
    "GFEX_BROWSER_SOURCE", "SHFE_INE_DAT_SOURCE", "select_member_position_source_rows",
)
