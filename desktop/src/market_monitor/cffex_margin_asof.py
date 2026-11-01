"""Resolve CFFEX margin rates from the latest complete official publication.

CFFEX publishes a dated contract parameter sheet only when it changes.  A
trading day uses the latest verified sheet on or before that day, but only for
contracts actually listed in that sheet.  Original publication rows keep their
own dates; this module never copies them into missing daily partitions.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date
import re
from typing import Any
from urllib.parse import urlsplit


def _official_publication_url(day: str, url: str) -> bool:
    parsed = urlsplit(url)
    compact = day.replace("-", "")
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname == "www.cffex.com.cn"
        and parsed.username is None and parsed.password is None
        and parsed.query == "" and parsed.fragment == ""
        and re.fullmatch(
            rf"/sj/jscs/{compact[:6]}/{compact[6:]}/{compact}_1\.csv", parsed.path,
        )
    )


def verified_publications(connection: Any, parser_version: str) -> list[tuple[str, str, str]]:
    """Return (day, URL, SHA) for complete CFFEX publication partitions."""

    available = {str(row[0]) for row in connection.execute("SHOW TABLES").fetchall()}
    if not {"futures_settlement_parameter_official", "futures_settlement_parameter_coverage"} <= available:
        return []
    rows = connection.execute(
        """WITH originals AS (
             SELECT trade_date, exchange, source_url,
                    count(*) AS rows, count(DISTINCT payload_sha256) AS sha_count,
                    min(payload_sha256) AS payload_sha256,
                    count(*) FILTER (WHERE quality_status='PASS' AND parser_version=?
                      AND spec_long_rate BETWEEN 0 AND 1
                      AND spec_short_rate BETWEEN 0 AND 1) AS valid_rows
             FROM futures_settlement_parameter_official
             WHERE exchange='CFFEX' GROUP BY 1,2,3
           )
           SELECT c.trade_date,c.source_url,c.payload_sha256
           FROM futures_settlement_parameter_coverage c
           JOIN originals o USING(trade_date,exchange,source_url)
           WHERE c.exchange='CFFEX' AND c.status='PASS' AND c.parser_version=?
             AND c.record_count=o.rows AND c.usable_count=o.rows
             AND o.valid_rows=o.rows AND o.sha_count=1
             AND c.payload_sha256=o.payload_sha256
             AND length(c.payload_sha256)=64
           ORDER BY c.trade_date,c.source_url""",
        [parser_version, parser_version],
    ).fetchall()
    return [
        (str(day), str(url), str(digest)) for day, url, digest in rows
        if _official_publication_url(str(day), str(url))
    ]


def verified_publication_days(connection: Any, parser_version: str) -> list[str]:
    """Return sorted dates of complete official CFFEX parameter sheets."""

    return sorted({day for day, _, _ in verified_publications(connection, parser_version)})


def known_publication_days(connection: Any, parser_version: str) -> list[str]:
    """Include partially parsed official sheets that must stop older carry-forward."""

    available = {str(row[0]) for row in connection.execute("SHOW TABLES").fetchall()}
    if "futures_settlement_parameter_coverage" not in available:
        return []
    rows = connection.execute(
        "SELECT trade_date,source_url FROM futures_settlement_parameter_coverage "
        "WHERE exchange='CFFEX' AND parser_version=? AND status IN ('PASS','PARTIAL') "
        "AND length(payload_sha256)=64",
        [parser_version],
    ).fetchall()
    return sorted({str(day) for day, url in rows
                   if _official_publication_url(str(day), str(url))})


def publication_on_or_before(publication_days: list[str], trading_day: str) -> str | None:
    """Select the latest prior publication, never a future or older fallback."""

    date.fromisoformat(trading_day)
    index = bisect_right(publication_days, trading_day) - 1
    return publication_days[index] if index >= 0 else None


def applicable_publication_on_or_before(
    verified_days: list[str], known_days: list[str], trading_day: str,
) -> str | None:
    """Use the latest known sheet only if that very sheet passed full validation."""

    latest = publication_on_or_before(known_days, trading_day)
    return latest if latest in verified_days else None


__all__ = (
    "applicable_publication_on_or_before", "known_publication_days",
    "publication_on_or_before", "verified_publication_days", "verified_publications",
)
