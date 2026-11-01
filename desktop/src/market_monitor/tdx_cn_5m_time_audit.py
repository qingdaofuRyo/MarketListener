"""Read-only evidence for the time labels of mainland stock five-minute bars.

The comparison is diagnostic only: a match is not permission to rewrite Silver
or evidence that an unobserved date or instrument uses the same convention.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from math import isfinite
from typing import Any, Mapping, Sequence


def _expected_labels(day: date, minutes: int) -> list[str]:
    result: list[str] = []
    for start, end in ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60)):
        for value in range(start + minutes, end + 1, minutes):
            result.append(f"{day:%Y%m%d}{value // 60:02d}{value % 60:02d}")
    return result


def _numbers(values: Sequence[Any]) -> tuple[float, float, float, float] | None:
    if len(values) != 4 or any(isinstance(value, bool) for value in values):
        return None
    try:
        result = tuple(float(value) for value in values)
    except (TypeError, ValueError):
        return None
    if not all(isfinite(value) and value > 0 for value in result):
        return None
    open_, close, high, low = result
    return result if low <= min(open_, close) <= max(open_, close) <= high else None


def _index(rows: Sequence[tuple[str, Sequence[Any]]], day: str) -> tuple[dict[str, tuple[float, ...]], int]:
    result: dict[str, tuple[float, ...]] = {}
    invalid = 0
    for label, prices in rows:
        if not label.startswith(day):
            continue
        parsed = _numbers(prices)
        if parsed is None or label in result:
            invalid += 1
            continue
        result[label] = parsed
    return result, invalid


def audit_day(
    day: str,
    local_bars: Sequence[Mapping[str, Any]],
    remote_5m: Sequence[Sequence[Any]],
    remote_30m: Sequence[Sequence[Any]],
    *,
    instrument_id: str,
    tolerance: float = 0.011,
) -> dict[str, Any]:
    """Compare source labels and OHLC within one day, without mixing sources.

    Remote rows use Tencent's [label, open, close, high, low, ...] layout.
    Local labels are the stored raw time, even though the existing Silver field
    currently calls them ``bar_open_time``. Inputs must already be restricted
    to one physical TDX stock instrument.
    """
    parsed_day = date.fromisoformat(day)
    compact_day = parsed_day.strftime("%Y%m%d")
    if not isfinite(tolerance) or tolerance <= 0:
        raise ValueError("tolerance must be a finite positive value")
    local_rows = []
    invalid_local_metadata = 0
    for bar in local_bars:
        label = str(bar.get("bar_open_time", ""))
        if not label.startswith(day):
            continue
        try:
            parsed = datetime.fromisoformat(label)
        except ValueError:
            invalid_local_metadata += 1
            continue
        if (parsed.utcoffset() != timedelta(hours=8) or parsed.second or parsed.microsecond
                or bar.get("instrument_id") != instrument_id
                or bar.get("source") != "通达信金融终端（本地）" or bar.get("period") != "5m"
                or bar.get("quality_status") != "PASS" or bar.get("normalization_version") != "tdx-cn-v2"):
            invalid_local_metadata += 1
            continue
        local_rows.append((parsed.strftime("%Y%m%d%H%M"), [bar.get(k) for k in ("open", "close", "high", "low")]))

    def remote_rows(rows: Sequence[Sequence[Any]]) -> list[tuple[str, Sequence[Any]]]:
        return [(str(row[0]), row[1:5]) for row in rows if row]

    local, invalid_local = _index(local_rows, compact_day)
    invalid_local += invalid_local_metadata
    source5, invalid5 = _index(remote_rows(remote_5m), compact_day)
    source30, invalid30 = _index(remote_rows(remote_30m), compact_day)
    expected5, expected30 = _expected_labels(parsed_day, 5), _expected_labels(parsed_day, 30)
    local_match = sum(all(abs(a - b) < tolerance for a, b in zip(local[key], source5[key], strict=True))
                      for key in expected5 if key in local and key in source5)
    matches = comparable = shifted_matches = shifted_comparable = local_bucket_matches = local_bucket_comparable = 0

    def combine(bars: list[tuple[float, ...]]) -> tuple[float, float, float, float]:
        return bars[0][0], bars[-1][1], max(x[2] for x in bars), min(x[3] for x in bars)

    def matches_30m(bars: list[tuple[float, ...]], endpoint: str) -> bool:
        return all(abs(a - b) < tolerance for a, b in zip(combine(bars), source30[endpoint], strict=True))

    for endpoint in expected30:
        end = datetime.strptime(endpoint, "%Y%m%d%H%M")
        keys = [(end - timedelta(minutes=offset)).strftime("%Y%m%d%H%M") for offset in (25, 20, 15, 10, 5, 0)]
        if endpoint not in source30:
            continue
        if all(key in source5 for key in keys):
            comparable += 1
            matches += matches_30m([source5[key] for key in keys], endpoint)
        if all(key in local for key in keys):
            local_bucket_comparable += 1
            local_bucket_matches += matches_30m([local[key] for key in keys], endpoint)
        shifted = [(end - timedelta(minutes=offset)).strftime("%Y%m%d%H%M")
                   for offset in (30, 25, 20, 15, 10, 5)]
        if all(key in source5 for key in shifted):
            shifted_comparable += 1
            shifted_matches += matches_30m([source5[key] for key in shifted], endpoint)
    complete = (set(local) == set(expected5) and set(source5) == set(expected5)
                and set(source30) == set(expected30) and not (invalid_local or invalid5 or invalid30))
    status = ("INCOMPLETE" if not complete else "CONSISTENT_SAMPLE"
              if local_match == 48 and comparable == matches == 8 else "MISMATCH")
    return {"day": day, "status": status, "expected5m": 48, "expected30m": 8,
            "local5m": len(local), "remote5m": len(source5), "remote30m": len(source30),
            "local5mOhlcMatches": local_match, "remoteEndLabelBucketsCompared": comparable,
            "remoteEndLabelBucketsMatched": matches,
            "remoteStartLabelBucketsCompared": shifted_comparable,
            "remoteStartLabelBucketsMatched": shifted_matches,
            "localEndLabelBucketsCompared": local_bucket_comparable,
            "localEndLabelBucketsMatched": local_bucket_matches,
            "invalidOrDuplicate": {"local5m": invalid_local, "remote5m": invalid5, "remote30m": invalid30},
            "ohlcToleranceYuan": tolerance, "automaticCorrectionAllowed": False}
