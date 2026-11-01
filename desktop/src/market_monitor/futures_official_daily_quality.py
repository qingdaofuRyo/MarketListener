"""Select complete current-parser official source days for derived Gold."""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

from .futures_official_daily_sources import PARSER_VERSION


def validated_daily_inputs(
    facts: Sequence[Mapping[str, Any]], coverage: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep an incomplete source day visible as a gap, never as a zero total."""

    counts = Counter(
        (str(row["trade_date"]), str(row["exchange"]), str(row["adapter"]))
        for row in facts
        if row.get("parser_version") == PARSER_VERSION
        and row.get("quality_status") == "PASS"
        and str(row.get("adapter") or "").endswith("-daily-v2")
    )
    digests: dict[tuple[str, str, str], set[str]] = {}
    for row in facts:
        if row.get("parser_version") != PARSER_VERSION or row.get("quality_status") != "PASS":
            continue
        key = str(row["trade_date"]), str(row["exchange"]), str(row["adapter"])
        digests.setdefault(key, set()).add(str(row.get("payload_sha256") or ""))
    valid: set[tuple[str, str, str]] = set()
    selected_coverage: list[dict[str, Any]] = []
    for row in coverage:
        adapter = str(row.get("adapter") or "")
        if not adapter.endswith("-daily-v2"):
            continue
        key = str(row["trade_date"]), str(row["exchange"]), adapter
        correct = (
            row.get("status") == "PASS"
            and row.get("parser_version") == PARSER_VERSION
            and counts[key] > 0
            and counts[key] == int(row.get("usable_count") or 0)
            and counts[key] == int(row.get("record_count") or 0)
            and len(digests.get(key, set())) == 1
            and digests[key] == {str(row.get("payload_sha256") or "")}
            and len(str(row.get("payload_sha256") or "")) == 64
        )
        if correct:
            valid.add(key)
            selected_coverage.append(dict(row))
        else:
            selected_coverage.append({**row, "status": "FAILED", "error": "current parser facts or source count incomplete"})
    selected_facts = [dict(row) for row in facts
                      if (str(row["trade_date"]), str(row["exchange"]), str(row["adapter"])) in valid
                      and row.get("parser_version") == PARSER_VERSION
                      and row.get("quality_status") == "PASS"]
    return selected_facts, selected_coverage


__all__ = ("validated_daily_inputs",)
