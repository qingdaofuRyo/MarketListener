"""Audit DCE/GFEX contract units and extend the official multiplier registry."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

import requests

from capture_dce_android_batch_ranks import BrowserPage, CaptureError, _discover_page, _remove_forward
from market_monitor.atomic_files import replace_with_retry
from market_monitor.futures_official_multipliers import REGISTRY_PATH, load_official_multiplier_registry


DCE_URL = "http://www.dce.com.cn/dcereport/publicweb/tradepara/contractInfo"
GFEX_URL = "http://www.gfex.com.cn/u/interfacesWebTtQueryContractInfo/loadList"
CODE = re.compile(r"[A-Z]{1,3}\Z")
START_DAY = "2025-09-24"
END_DAY = "2026-09-30"


def _dce_response(adb: Path) -> bytes:
    port: int | None = None
    page: BrowserPage | None = None
    try:
        port, target = _discover_page(adb, 5038, "127.0.0.1:5555")
        page = BrowserPage(target)
        page.preflight()
        expression = """(async()=>{const r=await fetch('/dcereport/publicweb/tradepara/contractInfo',
          {method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},
           body:JSON.stringify({lang:'zh',tradeType:'1',varietyId:'all'})});
          return {status:r.status,body:await r.text()};})()"""
        result = page.command("Runtime.evaluate", {
            "expression": expression, "awaitPromise": True, "returnByValue": True,
        })
        value = result.get("result", {}).get("value")
        if "exceptionDetails" in result or not isinstance(value, dict) or value.get("status") != 200:
            raise ValueError("DCE contract-info page request failed")
        body = value.get("body")
        if not isinstance(body, str) or len(body) > 1_000_000:
            raise ValueError("DCE contract-info body is missing or oversized")
        return body.encode("utf-8")
    finally:
        if page is not None:
            page.close()
        if port is not None:
            _remove_forward(adb, 5038, "127.0.0.1:5555", port)


def _gfex_response() -> bytes:
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.post(
            GFEX_URL, params={"variety": "", "trade_type": "0"},
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": "http://www.gfex.com.cn",
                "Referer": "http://www.gfex.com.cn/gfex/hyxx/ywcs.shtml",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
                "X-Requested-With": "XMLHttpRequest",
            }, timeout=20, allow_redirects=False,
        )
        response.raise_for_status()
        if "json" not in response.headers.get("Content-Type", "").lower():
            raise ValueError("GFEX contract-info response is not JSON")
        return bytes(response.content)
    finally:
        session.close()


def _units(raw: bytes, exchange: str) -> dict[str, int]:
    payload = json.loads(raw.decode("utf-8-sig"))
    expected = 200 if exchange == "DCE" else "0"
    if (not isinstance(payload, dict) or str(payload.get("code")) != str(expected)
            or not isinstance(payload.get("data"), list)):
        raise ValueError(f"{exchange} contract-info response has no successful data list")
    if exchange == "DCE" and payload.get("success") is not True:
        raise ValueError("DCE contract-info business success is false")
    if exchange == "GFEX" and payload.get("param") != {"variety": [""], "trade_type": ["0"]}:
        raise ValueError("GFEX contract-info response parameters differ")
    units: dict[str, int] = {}
    for row in payload["data"]:
        if not isinstance(row, dict):
            continue
        code = str(row.get("varietyOrder") or "").upper()
        if CODE.fullmatch(code) is None:
            continue
        value = row.get("unit")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{exchange}.{code} has an invalid unit")
        if code in units and units[code] != value:
            raise ValueError(f"{exchange}.{code} has conflicting contract units")
        units[code] = value
    if not units:
        raise ValueError(f"{exchange} has no futures contract units")
    return units


def _snapshot_history(root: Path, official: dict[str, int]) -> tuple[dict[str, str], int]:
    path = root / "state" / "futures_contract_rule_snapshots.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("provider") != "akshare.futures_rule":
        raise ValueError("unexpected historical unit snapshot provider")
    first_seen: dict[str, str] = {}
    checked_days = 0
    for day, snapshot in sorted(payload.get("days", {}).items()):
        if not START_DAY <= day <= END_DAY:
            continue
        products = snapshot.get("products", {})
        compared = False
        for key, value in products.items():
            if key not in official:
                continue
            observed = value.get("contractMultiplier") if isinstance(value, dict) else None
            if observed != official[key]:
                raise ValueError(f"historical unit snapshot conflicts with official {key} on {day}")
            first_seen.setdefault(key, day)
            compared = True
        checked_days += int(compared)
    if set(first_seen) != set(official):
        raise ValueError("historical unit snapshots do not cover all current official products")
    return first_seen, checked_days


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    replace_with_retry(temporary, path)


def sync(root: Path, *, adb: Path, write: bool) -> dict:
    root = Path(root).resolve()
    raw_by_exchange = {"DCE": _dce_response(adb), "GFEX": _gfex_response()}
    current = {f"{exchange}.{code}": value
               for exchange, raw in raw_by_exchange.items()
               for code, value in _units(raw, exchange).items()}
    first_seen, checked_days = _snapshot_history(root, current)
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    if any(key in registry["products"] for key in current):
        raise ValueError("DCE/GFEX units are already registered; inspect before replacing")
    retrieved = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for exchange, raw in raw_by_exchange.items():
        digest = sha256(raw).hexdigest()
        evidence_id = f"{exchange}_CONTRACT_INFO_20261003"
        url = DCE_URL if exchange == "DCE" else GFEX_URL + "?variety=&trade_type=0"
        registry["evidence"][evidence_id] = {
            "sourceUrl": url, "retrievedAt": retrieved,
            "payloadSha256": digest, "qualityStatus": "PASS",
            "historicalCrossCheck": f"{checked_days} local rule-snapshot days through {END_DAY}; no differing units",
        }
        if write:
            _write_json(root / "bronze" / "official_futures_contract_info" / f"{exchange}-{digest[:12]}.json", {
                "publisher": exchange, "sourceUrl": url, "retrievedAt": retrieved,
                "payloadSha256": digest, "rawBase64": base64.b64encode(raw).decode("ascii"),
            })
        registry["exchangeCoverage"][exchange] = "PASS"
    for key, value in sorted(current.items()):
        exchange, code = key.split(".")
        quote_unit = (
            "CNY/gram" if exchange == "GFEX" and code in {"PT", "PD"} else
            "CNY/ton" if exchange == "GFEX" else
            "CNY/exchange quoted unit"
        )
        registry["products"][key] = {
            "effectiveFrom": first_seen[key], "effectiveTo": None,
            "contractMultiplier": value, "quoteUnit": quote_unit,
            "evidenceId": f"{exchange}_CONTRACT_INFO_20261003",
        }
    report = {
        "status": "PASS" if write else "VALIDATED", "products": len(current),
        "DCE": sum(key.startswith("DCE.") for key in current),
        "GFEX": sum(key.startswith("GFEX.") for key in current),
        "historicalCrossCheckDays": checked_days,
        "earliestEffective": min(first_seen.values()),
    }
    if write:
        _write_json(REGISTRY_PATH, registry)
        load_official_multiplier_registry()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="核验大商所和广期所官网期货交易单位")
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--adb", type=Path, default=Path(r"C:\leidian\LDPlayer14\adb.exe"))
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(sync(args.data_root, adb=args.adb, write=args.write), ensure_ascii=False))
    except CaptureError as error:
        print(json.dumps({"status": "BLOCKED", "reason": error.code}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
