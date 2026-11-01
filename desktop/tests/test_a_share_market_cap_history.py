from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo

import openpyxl
import pytest
import requests

from market_monitor import a_share_market_cap_history as history
from market_monitor.collector import _metric
from market_monitor.storage import MarketStore


_CAPS_RAW = {
    "000001": 5_103_849_900,
    "000300": 10_461_611_865.987,
    "000688": 1_404_973_300,
    "399001": 2_325_969_100,
    "399006": 1_626_819_400,
    "500001": 3_952_788_665.987,
    "899050": 81_441_718.766496,
}


def _eastmoney_blob(day: str) -> bytes:
    rows = [
        {
            "TRADE_MARKET_CODE": code,
            "TRADE_DATE": f"{day} 00:00:00",
            "LISTING_ORG_NUM": 351 if code == "899050" else 1500,
            "TOTAL_MARKET_CAP": value,
        }
        for code, value in _CAPS_RAW.items()
    ]
    return json.dumps({
        "success": True,
        "result": {"count": len(rows), "pages": 1, "data": rows},
    }).encode()


def _szse_blob(*, b_cap: str = "36,254,509,011.12", wrong_dimension: bool = False) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["证券类别", "数量(只)", "成交金额(元)", "总市值(元)", "流通市值(元)"])
    sheet.append(["股票", 2943, "757,364,626,822.79",
                  "39,527,886,659,870.04", "33,600,275,028,614.73"])
    sheet.append(["      主板A股", 1495, "380,293,555,209.16",
                  "23,223,437,316,648.40", "20,538,031,754,292.25"])
    sheet.append(["      主板B股", 37, "28,336,272.35", b_cap, "36,239,271,876.33"])
    sheet.append(["      创业板A股", 1411, "377,042,735,341.28",
                  "16,268,194,834,210.52", "13,026,004,002,446.15"])
    out = io.BytesIO()
    workbook.save(out)
    workbook.close()
    blob = out.getvalue()
    if not wrong_dimension:
        return blob
    adjusted = io.BytesIO()
    with ZipFile(io.BytesIO(blob)) as original, ZipFile(adjusted, "w", ZIP_DEFLATED) as output:
        for member in original.namelist():
            content = original.read(member)
            if member == "xl/worksheets/sheet1.xml":
                content = content.replace(b'<dimension ref="A1:E5"/>', b'<dimension ref="A1"/>')
            output.writestr(member, content)
    return adjusted.getvalue()


class _Response:
    def __init__(self, url: str, content: bytes) -> None:
        self.url = url
        self.content = content
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None


class _Session:
    def __init__(self, *, bad_em_day: str | None = None, b_cap: str = "36,254,509,011.12") -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.bad_em_day = bad_em_day
        self.b_cap = b_cap

    def get(self, url: str, *, params: dict[str, str], headers: dict[str, str],
            timeout: int) -> _Response:
        self.calls.append((url, params))
        response_url = requests.Request("GET", url, params=params).prepare().url
        assert response_url is not None
        if url == history.EASTMONEY_URL:
            day = params["filter"].split("'")[1]
            payload_day = "2026-10-19" if day == self.bad_em_day else day
            return _Response(response_url, _eastmoney_blob(payload_day))
        if url == history.SZSE_URL:
            return _Response(response_url, _szse_blob(b_cap=self.b_cap, wrong_dimension=True))
        raise AssertionError(url)


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(history, "_beijing_today", lambda: history.date(2026, 10, 24))


def test_sync_writes_independent_cap_metric_with_evidence_and_skips_rerun(tmp_path: Path) -> None:
    store = MarketStore(tmp_path)
    try:
        store.upsert_gold_metrics([_metric(
            "A_SHARE_BREADTH", "CN.A_SHARE.BREADTH", "2026-10-20", "1d",
            "旧腾讯总市值", 1_199_999.0, "旧来源", "source=stock_zh_a_spot_tx",
            metric_key="TOTAL_MARKET_CAP_YI",
        )])
    finally:
        store.close()
    session = _Session()
    first = history.sync_a_share_market_cap_history(
        tmp_path, start_day="2026-10-20", end_day="2026-10-20", session=session,
    )
    second = history.sync_a_share_market_cap_history(
        tmp_path, start_day="2026-10-20", end_day="2026-10-20", session=session,
    )
    assert first["writtenDays"] == ["2026-10-20"]
    assert first["failedDays"] == []
    assert second["skippedDays"] == ["2026-10-20"]
    assert len(session.calls) == 2
    evidence = tmp_path / "bronze" / "a_share_market_cap_history" / "day=2026-10-20"
    manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["componentsYi"]["szseMainBExcluded"] == "362.5450901112"
    assert len(manifest["emSha256"]) == len(manifest["szseSha256"]) == 64
    assert "TRADE_DATE%3D%272026-10-20%27" in manifest["requests"]["eastmoney"]["requestedUrl"]
    assert "txtQueryDate=2026-10-20" in manifest["requests"]["szse"]["requestedUrl"]
    assert manifest["requests"]["eastmoney"]["responseUrl"] == manifest["requests"]["eastmoney"]["requestedUrl"]
    assert manifest["requests"]["szse"]["responseUrl"] == manifest["requests"]["szse"]["requestedUrl"]
    assert all(manifest["requests"][source]["httpStatus"] == 200 for source in ("eastmoney", "szse"))
    assert all(manifest["requests"][source]["fetchedAt"] for source in ("eastmoney", "szse"))
    store = MarketStore(tmp_path)
    try:
        rows = store.connection.execute(
            "SELECT metric_id, value, calculation_method FROM gold_metrics ORDER BY metric_id"
        ).fetchall()
    finally:
        store.close()
    assert len(rows) == 2
    assert rows[0][0].startswith("A_SHARE_BREADTH:")
    assert rows[0][1] == 1_199_999.0
    assert rows[1][0] == (
        "A_SHARE_MARKET_CAP_HISTORY:CN.A_SHARE.MARKET_CAP:2026-10-20:1d:TOTAL_MARKET_CAP_YI"
    )
    assert rows[1][1] == pytest.approx(1_053_942.813)
    assert rows[1][2].startswith(
        "source=eastmoney-RPT_VALUEMARKET+szse-official-daily; formula="
    )
    assert "szse_sha256=" in rows[1][2]


def test_market_day_mismatch_fails_without_publishing_and_later_day_continues(
    tmp_path: Path,
) -> None:
    bad = _Session(bad_em_day="2026-10-20")
    result = history.sync_a_share_market_cap_history(
        tmp_path, start_day="2026-10-20", end_day="2026-10-21", session=bad,
    )
    assert result["writtenDays"] == ["2026-10-21"]
    assert result["failedDays"][0]["day"] == "2026-10-20"
    assert "date disagrees" in result["failedDays"][0]["error"]
    good = _Session()
    retried = history.sync_a_share_market_cap_history(
        tmp_path, start_day="2026-10-20", end_day="2026-10-21", session=good,
    )
    assert retried["writtenDays"] == ["2026-10-20"]
    assert retried["skippedDays"] == ["2026-10-21"]
    assert len(good.calls) == 2


def test_szse_crosscheck_rejects_changed_b_share_component(tmp_path: Path) -> None:
    session = _Session(b_cap="99,999,999,999.99")
    result = history.sync_a_share_market_cap_history(
        tmp_path, start_day="2026-10-20", end_day="2026-10-20", session=session,
    )
    assert result["metricCount"] == 0
    assert result["failedDays"][0]["day"] == "2026-10-20"
    store = MarketStore(tmp_path)
    try:
        count = store.connection.execute(
            "SELECT count(*) FROM gold_metrics WHERE metric_id LIKE 'A_SHARE_MARKET_CAP_HISTORY:%'"
        ).fetchone()[0]
    finally:
        store.close()
    assert count == 0


def test_szse_workbook_with_wrong_dimension_still_reads_cells() -> None:
    parsed = history._parse_szse(_szse_blob(wrong_dimension=True))
    assert parsed["主板A股"] == history.Decimal("232234.373166484")
    assert parsed["主板B股"] == history.Decimal("362.5450901112")


def test_legacy_manifest_is_upgraded_from_local_evidence_without_network(tmp_path: Path) -> None:
    first = history.sync_a_share_market_cap_history(
        tmp_path, start_day="2026-10-20", end_day="2026-10-20", session=_Session(),
    )
    assert first["writtenDays"] == ["2026-10-20"]
    path = tmp_path / "bronze/a_share_market_cap_history/day=2026-10-20/manifest.json"
    old = json.loads(path.read_text(encoding="utf-8"))
    del old["requests"]
    old.pop("schemaVersion", None)
    path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    session = _Session()
    resumed = history.sync_a_share_market_cap_history(
        tmp_path, start_day="2026-10-20", end_day="2026-10-20", session=session,
    )
    assert resumed["skippedDays"] == ["2026-10-20"]
    assert resumed["writtenDays"] == []
    assert session.calls == []
    upgraded = json.loads(path.read_text(encoding="utf-8"))
    assert upgraded["schemaVersion"] == 2
    for source in ("eastmoney", "szse"):
        metadata = upgraded["requests"][source]
        assert metadata["metadataOrigin"] == "LEGACY_RECONSTRUCTED"
        assert metadata["requestedDay"] == "2026-10-20"
        assert metadata["requestedUrl"].startswith("https://")
        assert metadata["responseUrl"] is None
        assert metadata["httpStatus"] is None
        assert metadata["fetchedAt"] is None
        assert metadata["reconstructedAt"]


def test_tampered_request_date_cannot_reuse_cached_evidence(tmp_path: Path) -> None:
    history.sync_a_share_market_cap_history(
        tmp_path, start_day="2026-10-20", end_day="2026-10-20", session=_Session(),
    )
    path = tmp_path / "bronze/a_share_market_cap_history/day=2026-10-20/manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["requests"]["szse"]["requestedDay"] = "2026-10-21"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    assert history._read_evidence(tmp_path, "2026-10-20") is None
    session = _Session()
    resumed = history.sync_a_share_market_cap_history(
        tmp_path, start_day="2026-10-20", end_day="2026-10-20", session=session,
    )
    assert resumed["writtenDays"] == ["2026-10-20"]
    assert len(session.calls) == 2


def test_versioned_manifest_cannot_downgrade_to_legacy_by_dropping_requests(tmp_path: Path) -> None:
    history.sync_a_share_market_cap_history(
        tmp_path, start_day="2026-10-20", end_day="2026-10-20", session=_Session(),
    )
    path = tmp_path / "bronze/a_share_market_cap_history/day=2026-10-20/manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["schemaVersion"] == 2
    del manifest["requests"]
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    assert history._read_evidence(tmp_path, "2026-10-20") is None


def test_tampered_response_url_date_blocks_cached_evidence(tmp_path: Path) -> None:
    history.sync_a_share_market_cap_history(
        tmp_path, start_day="2026-10-20", end_day="2026-10-20", session=_Session(),
    )
    path = tmp_path / "bronze/a_share_market_cap_history/day=2026-10-20/manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["requests"]["szse"]["responseUrl"] = manifest["requests"]["szse"]["responseUrl"].replace(
        "2026-10-20", "2026-10-21",
    )
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    assert history._read_evidence(tmp_path, "2026-10-20") is None


def test_unknown_year_is_reported_as_calendar_unverified_not_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(history, "_beijing_today", lambda: history.date(2027, 1, 5))
    session = _Session()
    result = history.sync_a_share_market_cap_history(
        tmp_path, start_day="2027-01-02", end_day="2027-01-04", session=session,
    )
    assert result["nonTradingDays"] == ["2027-01-02", "2027-01-03"]
    assert result["calendarUnverifiedDays"] == ["2027-01-04"]
    assert result["failedDays"] == [{"day": "2027-01-04", "error": "CALENDAR_UNVERIFIED"}]
    assert session.calls == []


def test_current_trading_day_is_fetched_only_after_the_session_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    day = history.date(2026, 10, 23)
    monkeypatch.setattr(history, "_beijing_today", lambda: day)
    monkeypatch.setattr(history, "_beijing_now", lambda: datetime(
        2026, 10, 23, 15, 29, tzinfo=ZoneInfo("Asia/Shanghai"),
    ))
    with pytest.raises(ValueError, match="completed A-share session"):
        history.sync_a_share_market_cap_history(
            tmp_path, start_day=day.isoformat(), end_day=day.isoformat(), session=_Session(),
        )
    monkeypatch.setattr(history, "_beijing_now", lambda: datetime(
        2026, 10, 23, 19, 0, tzinfo=ZoneInfo("Asia/Shanghai"),
    ))
    result = history.sync_a_share_market_cap_history(
        tmp_path, start_day=day.isoformat(), end_day=day.isoformat(), session=_Session(),
    )
    assert result["writtenDays"] == [day.isoformat()]
