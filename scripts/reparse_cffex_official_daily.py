"""Reparse archived CFFEX daily ZIPs after correcting the published CSV headings."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import math
from pathlib import Path
import requests
from zipfile import ZipFile, is_zipfile

from market_monitor.futures_calendar import load_futures_trading_calendar
from market_monitor.futures_official_backfill import _store_cffex_archive
from market_monitor.futures_official_daily_sources import official_daily_request, parse_official_daily
from market_monitor.storage import MarketStore


ADAPTER = "official-cffex-daily-v2"
HEADERS = {
    "Referer": "http://www.cffex.com.cn/cn/rtj.html",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
}


def _archive(
    root: Path, month: str, digest: str, url: str,
    session: requests.Session,
) -> tuple[bytes, bool]:
    path = root / "bronze" / "official_futures_cffex_archives" / f"{month}-{digest[:12]}.zip"
    if path.is_file():
        raw, downloaded = path.read_bytes(), False
    else:
        response = session.get(url, headers=HEADERS, timeout=30, allow_redirects=False)
        response.raise_for_status()
        raw, downloaded = bytes(response.content), True
    if sha256(raw).hexdigest() != digest or not is_zipfile(BytesIO(raw)):
        raise ValueError(f"CFFEX archived ZIP hash or format differs for {month}")
    return raw, downloaded


def _same_existing_values(old: list[dict], new: list[dict], day: str) -> None:
    current = {str(row["contract_code"]): row for row in old}
    prepared = {str(row["contract_code"]): row for row in new}
    if not current or current.keys() != prepared.keys():
        raise ValueError(f"CFFEX contract set changed while reparsing {day}")
    for code, existing in current.items():
        replacement = prepared[code]
        for field in ("volume", "open_interest", "close", "settlement", "turnover_raw"):
            previous = existing.get(field)
            if previous is not None and not math.isclose(
                float(previous), float(replacement[field]), rel_tol=1e-10, abs_tol=1e-8,
            ):
                raise ValueError(f"CFFEX existing {field} differs on {day}/{code}")


def reparse(root: Path, *, start_day: str, end_day: str, write: bool = False) -> dict:
    root = Path(root)
    calendar = load_futures_trading_calendar(root)
    if calendar is None:
        raise ValueError("verified futures trading calendar is missing")
    days = calendar.between(start_day, end_day)
    if not days:
        raise ValueError("requested range has no verified trading days")
    store = MarketStore(root)
    session = requests.Session()
    session.trust_env = False
    try:
        coverages = {
            str(row["trade_date"]): row
            for row in store.list_futures_official_daily_coverage(
                start_day=start_day, end_day=end_day,
            )
            if row["exchange"] == "CFFEX" and row["adapter"] == ADAPTER
            and row["status"] == "PASS"
        }
        monthly_reference = {
            str(row["trade_date"]).replace("-", "")[:6]: row
            for row in coverages.values()
        }
        archives: dict[tuple[str, str], tuple[bytes, bool]] = {}
        prepared: list[tuple[str, list[dict], dict]] = []
        new_days = 0
        for day in days:
            coverage = coverages.get(day)
            month = day.replace("-", "")[:6]
            reference = coverage or monthly_reference.get(month)
            if reference is None:
                raise ValueError(f"CFFEX month has no verified source ZIP: {month}")
            digest = str(reference["payload_sha256"] or "")
            source_url = str(official_daily_request("CFFEX", day)["url"])
            archive_key = (month, digest)
            if archive_key not in archives:
                archives[archive_key] = _archive(
                    root, month, digest, source_url, session,
                )
            raw, _downloaded = archives[archive_key]
            with ZipFile(BytesIO(raw)) as archive:
                if f"{day.replace('-', '')}_1.csv" not in archive.namelist():
                    raise ValueError(f"CFFEX ZIP omits {day}")
            rows = parse_official_daily(
                raw, exchange="CFFEX", trading_day=day,
                source_url=source_url,
                retrieved_at=str(coverage["retrieved_at"] if coverage else datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            existing = [
                row for row in store.list_futures_official_daily(
                    start_day=day, end_day=day,
                )
                if row["exchange"] == "CFFEX" and row["adapter"] == ADAPTER
            ]
            if existing:
                _same_existing_values(existing, rows, day)
            elif coverage is not None:
                raise ValueError(f"CFFEX PASS coverage has no source rows on {day}")
            else:
                new_days += 1
                coverage = {
                    "trade_date": day, "exchange": "CFFEX", "status": "PASS",
                    "record_count": len(rows), "usable_count": len(rows),
                    "adapter": ADAPTER, "source_url": source_url,
                    "payload_sha256": digest, "error": None,
                    "retrieved_at": rows[0]["retrieved_at"],
                }
            prepared.append((day, rows, coverage))
        written = 0
        if write:
            timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for (month, _digest), (raw, downloaded) in archives.items():
                if downloaded:
                    first = next(day for day in days if day.replace("-", "").startswith(month))
                    _store_cffex_archive(
                        root, month, raw, str(official_daily_request("CFFEX", first)["url"]),
                        timestamp, sha256(raw).hexdigest(),
                    )
            for _day, rows, coverage in prepared:
                replacement = {**coverage, "record_count": len(rows), "usable_count": len(rows)}
                written += store.record_futures_official_daily(rows, replacement)
        return {
            "status": "PASS" if write else "VALIDATED",
            "selectedDays": len(days), "rows": sum(len(rows) for _, rows, _ in prepared),
            "replacedRows": written, "downloadedArchives": sum(item[1] for item in archives.values()),
            "months": len({month for month, _digest in archives}),
            "archives": len(archives), "newDays": new_days,
        }
    finally:
        session.close()
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="核对并重解析中金所已保存的官方日行情 ZIP")
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--start-day", required=True)
    parser.add_argument("--end-day", required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    report = reparse(
        args.data_root, start_day=args.start_day, end_day=args.end_day,
        write=args.write,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
