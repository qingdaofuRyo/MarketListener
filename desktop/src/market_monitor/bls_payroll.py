"""BLS public CES seasonally adjusted payroll levels and monthly changes."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import math
from typing import Any

import requests

URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/CES0000000001"
SERIES = "CES0000000001"
MAX_BYTES = 1_000_000


def parse_payroll_changes(payload: Any) -> list[tuple[str, float]]:
    if not isinstance(payload, dict) or payload.get("status") != "REQUEST_SUCCEEDED":
        raise ValueError("BLS response did not succeed")
    result = payload.get("Results")
    series = result.get("series") if isinstance(result, dict) else None
    if (not isinstance(series, list) or len(series) != 1 or not isinstance(series[0], dict)
            or series[0].get("seriesID") != SERIES or not isinstance(series[0].get("data"), list)):
        raise ValueError("unexpected BLS series")
    values: dict[tuple[int, int], Decimal] = {}
    for row in series[0]["data"]:
        if not isinstance(row, dict):
            raise ValueError("invalid BLS row")
        year, period = str(row.get("year") or ""), str(row.get("period") or "")
        if len(year) != 4 or not year.isascii() or not year.isdigit():
            raise ValueError("invalid BLS year")
        if period == "M13":
            continue
        if len(period) != 3 or period[0] != "M" or not period[1:].isdigit() or not 1 <= int(period[1:]) <= 12:
            raise ValueError("invalid BLS month")
        try:
            value = Decimal(str(row.get("value")))
        except InvalidOperation:
            raise ValueError("invalid BLS value") from None
        key = (int(year), int(period[1:]))
        if key in values or not value.is_finite() or value < 0:
            raise ValueError("duplicate or invalid BLS observation")
        values[key] = value
    changes = []
    for year, month in sorted(values):
        previous = (year - 1, 12) if month == 1 else (year, month - 1)
        if previous in values:
            change = float(values[(year, month)] - values[previous])
            if not math.isfinite(change):
                raise ValueError("invalid BLS change")
            changes.append((f"{year:04d}-{month:02d}", change))
    if not changes:
        raise ValueError("BLS series lacks adjacent monthly observations")
    return changes


def fetch_payroll_changes(*, session: requests.Session | None = None) -> tuple[list[tuple[str, float]], str]:
    client = session or requests.Session()
    response = None
    try:
        response = client.get(URL, timeout=(4, 8), allow_redirects=False, stream=True)
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("unexpected BLS HTTP status")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            body.extend(chunk)
            if len(body) > MAX_BYTES:
                raise ValueError("BLS response exceeds size limit")
        rows = parse_payroll_changes(json.loads(bytes(body)))
        return rows, sha256(body).hexdigest()
    finally:
        if response is not None:
            response.close()
        if session is None:
            client.close()
