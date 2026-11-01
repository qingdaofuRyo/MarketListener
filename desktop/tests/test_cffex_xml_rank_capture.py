"""CFFEX Bronze capture recovery and adaptive concurrency contracts."""

from __future__ import annotations

import base64
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
from threading import Lock
import time
from xml.etree import ElementTree


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "capture_cffex_xml_ranks.py"
SPEC = importlib.util.spec_from_file_location("capture_cffex_xml_ranks", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
capture = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = capture
SPEC.loader.exec_module(capture)


def _xml(day: str, product: str) -> bytes:
    root = ElementTree.Element("positionRank")
    for direction in ("0", "1", "2"):
        item = ElementTree.SubElement(root, "data")
        for field, value in (
            ("instrumentid", f"{product}2303"), ("tradingday", day),
            ("datatypeid", direction), ("rank", "1"),
            ("shortname", "示例会员"), ("volume", "100"), ("varvolume", "1"),
        ):
            ElementTree.SubElement(item, field).text = value
    return ElementTree.tostring(root, encoding="utf-8")


def test_transient_reduces_parallelism_and_keeps_successful_products(tmp_path: Path) -> None:
    attempts: dict[str, int] = {}
    lock = Lock()
    active = 0
    peak = 0
    delays: list[float] = []

    def fetch(day: str, product: str, *, timeout: float) -> dict:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            attempts[product] = attempts.get(product, 0) + 1
            attempt = attempts[product]
        time.sleep(0.01)
        with lock:
            active -= 1
        if product == "IF" and attempt == 1:
            return {"product": product, "status": "TRANSIENT", "httpStatus": 503}
        raw = _xml(day.replace("-", ""), product)
        return {"product": product, "status": "PASS_RAW", "sourceUrl": capture._source_url(day, product),
                "raw": raw, "contractCount": 1, "rankRows": 1}

    results, width, status = capture._capture_day(
        tmp_path, "2023-01-06", {}, workers=8, max_attempts=3,
        timeout=1, sleep=delays.append, fetch=fetch,
    )

    assert status == "PASS_RAW"
    assert len(results) == 7
    assert width == 4
    assert peak > 1
    assert attempts["IF"] == 2
    assert all(count == 1 for product, count in attempts.items() if product != "IF")
    assert delays == [1]
    assert (tmp_path / "bronze/cffex_xml_rank_capture/2023-01-06/IF.xml").is_file()


def test_http_412_stops_without_retry(tmp_path: Path) -> None:
    seen: list[str] = []

    def fetch(day: str, product: str, *, timeout: float) -> dict:
        seen.append(product)
        return {"product": product, "status": "BLOCKED", "httpStatus": 412}

    results, _, status = capture._capture_day(
        tmp_path, "2023-01-06", {}, workers=8, max_attempts=4,
        timeout=1, sleep=lambda _: None, fetch=fetch,
    )

    assert status == "BLOCKED"
    assert len(seen) == len(results) == 7
    assert all(result["httpStatus"] == 412 for result in results.values())
    assert not (tmp_path / "bronze").exists()


def test_dry_run_reuses_verified_legacy_xml_and_rejects_bad_checksum(tmp_path: Path) -> None:
    day = "2023-01-06"
    calendar_path = tmp_path / "state" / "cn_futures_trading_calendar.json"
    calendar_path.parent.mkdir(parents=True)
    calendar_path.write_text(json.dumps({
        "schema": "market-listener/cn-futures-trading-calendar/v1",
        "provider": "akshare.tool_trade_date_hist_sina", "market": "CN_FUTURE",
        "retrievedAt": "2026-10-02T00:00:00+00:00", "tradingDays": [day],
    }), encoding="utf-8")
    legacy = tmp_path / "bronze" / "official_futures_member_cffex_legacy"
    legacy.mkdir(parents=True)
    for product in ("IF", "IH", "IC", "IM", "TS", "TF", "T"):
        raw = _xml("20230106", product)
        digest = sha256(raw).hexdigest()
        (legacy / f"{product}.json").write_text(json.dumps({
            "publisher": "CFFEX",
            "sourceUrl": capture._source_url(day, product),
            "payloadSha256": digest,
            "contentBase64": base64.b64encode(raw).decode("ascii"),
        }), encoding="utf-8")

    summary = capture.capture(tmp_path, start_day=day, end_day=day, dry_run=True)
    assert summary["completedDays"] == 1
    assert summary["pendingDays"] == 0

    bad_path = legacy / "IF.json"
    payload = json.loads(bad_path.read_text(encoding="utf-8"))
    payload["payloadSha256"] = "0" * 64
    bad_path.write_text(json.dumps(payload), encoding="utf-8")
    summary = capture.capture(tmp_path, start_day=day, end_day=day, dry_run=True)
    assert summary["completedDays"] == 0
    assert summary["firstPendingDay"] == day


def test_all_404_products_keep_trading_day_uncovered(tmp_path: Path, monkeypatch) -> None:
    day = "2023-01-06"
    calendar_path = tmp_path / "state" / "cn_futures_trading_calendar.json"
    calendar_path.parent.mkdir(parents=True)
    calendar_path.write_text(json.dumps({
        "schema": "market-listener/cn-futures-trading-calendar/v1",
        "provider": "akshare.tool_trade_date_hist_sina", "market": "CN_FUTURE",
        "retrievedAt": "2026-10-02T00:00:00+00:00", "tradingDays": [day],
    }), encoding="utf-8")
    calls: list[str] = []

    def no_publication(trading_day: str, product: str, *, timeout: float) -> dict:
        calls.append(product)
        return {"product": product, "status": "NO_PUBLICATION", "httpStatus": 404,
                "sourceUrl": capture._source_url(trading_day, product)}

    monkeypatch.setattr(capture, "_fetch_once", no_publication)
    first = capture.capture(tmp_path, start_day=day, end_day=day, max_days=1)
    assert first["status"] == "NO_PUBLICATION"
    assert first["completedDays"] == 0
    assert len(calls) == 7
    assert not list((tmp_path / "bronze").rglob("*.json"))
    report = json.loads((tmp_path / "state/cffex_xml_rank_capture/20230106.json")
                        .read_text(encoding="utf-8"))
    assert report["status"] == "NO_PUBLICATION"
    assert {item["status"] for item in report["products"].values()} == {"NO_PUBLICATION"}

    second = capture.capture(tmp_path, start_day=day, end_day=day, max_days=1)
    assert second["completedDays"] == 0
    assert len(calls) == 14
