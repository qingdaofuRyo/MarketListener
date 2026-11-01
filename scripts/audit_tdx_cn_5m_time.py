"""Capture a small read-only TDX/Tencent minute-label comparison locally."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import uuid4

import requests

from market_monitor.market_data_version import revision_is_settled
from market_monitor.market_query_cache import get_kline_query_store
from market_monitor.tdx_cn_5m_time_audit import audit_day


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--symbol", action="append", required=True, help="sh600519 or sz000001; repeat for up to 5")
    parser.add_argument("--day", action="append", required=True, help="YYYY-MM-DD; repeat for up to 7")
    args = parser.parse_args()
    symbols, days = list(dict.fromkeys(args.symbol)), list(dict.fromkeys(args.day))
    if len(symbols) > 5 or any(not re.fullmatch(r"(?:sh|sz)\d{6}", symbol) for symbol in symbols):
        parser.error("supply at most five sh/sz stock symbols")
    if len(days) > 7:
        parser.error("supply at most seven dates")
    for day in days:
        try:
            date.fromisoformat(day)
        except ValueError:
            parser.error("invalid date")

    root = args.data_root.resolve()
    store = get_kline_query_store(root)
    revision = store.ensure_ready()
    session = requests.Session()
    session.trust_env = False
    evidence: dict = {"schema": "tdx-cn-5m-time-audit-v1", "createdAt": datetime.now(timezone.utc).isoformat(),
                      "silverRevision": revision, "source": "https://ifzq.gtimg.cn/appstock/app/kline/mkline",
                      "results": [], "inputs": []}
    for symbol in symbols:
        source_rows = {}
        payloads = {}
        for period in ("m5", "m30"):
            response = session.get(evidence["source"], params={"param": f"{symbol},{period},,320"}, timeout=15)
            response.raise_for_status()
            payloads[period] = {"sha256": sha256(response.content).hexdigest(),
                                "bodyHex": response.content.hex(), "status": response.status_code,
                                "requestParam": f"{symbol},{period},,320",
                                "fetchedAt": datetime.now(timezone.utc).isoformat()}
            source_rows[period] = response.json()["data"][symbol][period]
        exchange = "SSE" if symbol.startswith("sh") else "SZSE"
        physical_id = f"CN.{exchange}.STOCK.{symbol[2:]}.TDX_LOCAL"
        bars = store.read_bars(physical_id, "5m", 500)
        selected = [bar for bar in bars if str(bar.get("bar_open_time", ""))[:10] in days]
        for day in days:
            evidence["results"].append({"instrumentId": physical_id,
                                        **audit_day(day, selected, source_rows["m5"], source_rows["m30"],
                                                    instrument_id=physical_id)})
        evidence["inputs"].append({"instrumentId": physical_id, "localBars": selected, "responses": payloads})
    if not revision_is_settled(root, revision):
        raise RuntimeError("Silver revision changed while collecting audit evidence; retry")
    folder = root / "reports" / "tdx-cn-5m-time-audit"
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}.json"
    with output.open("x", encoding="utf-8") as target:
        json.dump(evidence, target, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"report": str(output), "silverRevision": revision, "results": evidence["results"]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
