from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from market_monitor.exchange_delisted import DelistedReviewChanged, DelistedSourceError, load_exchange, parse_pages, refresh_exchange, review_page
from market_monitor.strategy_screener import _delisted_evidence
from market_monitor import exchange_delisted
from market_monitor.manual_data_gaps import delisted_snapshot_items


def _page(exchange: str, page: int, total: int, size: int, rows: list[dict]) -> bytes:
    page_count = (total + size - 1) // size
    if exchange == "SSE":
        payload = {"pageHelp": {"pageNo": page, "pageSize": size, "pageCount": page_count, "total": total},
                   "actionErrors": [], "fieldErrors": {}, "result": rows}
    else:
        payload = [{"metadata": {"tabkey": "tab2", "pageno": page, "pagesize": size,
                                 "pagecount": page_count, "recordcount": total}, "error": None, "data": rows}]
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def test_sse_distinguishes_a_and_b_share_rows():
    rows = [
        {"A_STOCK_CODE": "600190", "COMPANY_ABBR": "样本", "DELIST_DATE": "20250728", "STOCK_TYPE": "1"},
        {"A_STOCK_CODE": "600190", "COMPANY_ABBR": "样本B", "DELIST_DATE": "20250728", "STOCK_TYPE": "2"},
        {"A_STOCK_CODE": "688001", "COMPANY_ABBR": "科创样本", "DELIST_DATE": "2026-10-19", "STOCK_TYPE": "8"},
    ]
    result = parse_pages("SSE", [_page("SSE", 1, 3, 3, rows)])
    assert [item["instrumentId"] for item in result] == ["CN.SSE.STOCK.600190", "CN.SSE.STOCK.688001"]
    assert result[0]["delistedAt"] == "2025-07-28"


def test_szse_rejects_missing_duplicate_and_changed_pages():
    row1 = {"zqdm": "000004", "zqjc": "样本", "zzrq": "2026-07-14"}
    row2 = {"zqdm": "000851", "zqjc": "样本二", "zzrq": "2025-11-11"}
    first, second = _page("SZSE", 1, 2, 1, [row1]), _page("SZSE", 2, 2, 1, [row2])
    assert len(parse_pages("SZSE", [first, second])) == 2
    with pytest.raises(DelistedSourceError):
        parse_pages("SZSE", [first])
    with pytest.raises(DelistedSourceError):
        parse_pages("SZSE", [first, _page("SZSE", 2, 2, 1, [row1])])
    with pytest.raises(DelistedSourceError):
        parse_pages("SZSE", [first, _page("SZSE", 2, 3, 1, [row2])])


def test_snapshot_keeps_previous_version_on_failure_and_detects_tampering(tmp_path: Path):
    row = {"zqdm": "000004", "zqjc": "样本", "zzrq": "2026-07-14"}

    class Response:
        content = _page("SZSE", 1, 1, 1, [row])

        def raise_for_status(self):
            pass

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    status = refresh_exchange(tmp_path, "SZSE", session=Session())
    rows, loaded = load_exchange(tmp_path, "SZSE")
    assert status["sha256"] == loaded["sha256"] and rows[0]["instrumentId"] == "CN.SZSE.STOCK.000004"
    pointer = tmp_path / "bronze" / "exchange-delisted" / "SZSE-latest.json"
    old = pointer.read_bytes()
    Response.content = b"invalid-json"
    with pytest.raises(DelistedSourceError):
        refresh_exchange(tmp_path, "SZSE", session=Session())
    assert pointer.read_bytes() == old and load_exchange(tmp_path, "SZSE")[1]["status"] == "PASS"
    Response.content = _page("SZSE", 1, 1, 1, [{**row, "zzrq": "2026-07-15"}])
    with pytest.raises(DelistedSourceError, match="historical records changed"):
        refresh_exchange(tmp_path, "SZSE", session=Session())
    assert pointer.read_bytes() == old
    assert load_exchange(tmp_path, "SZSE")[1]["review"]["affectedIds"] == ["CN.SZSE.STOCK.000004"]
    Response.content = _page("SZSE", 1, 1, 1, [{**row, "zqjc": "改名后的样本"}])
    with pytest.raises(exchange_delisted.DelistedReviewRequired):
        refresh_exchange(tmp_path, "SZSE", session=Session())
    assert pointer.read_bytes() == old
    assert load_exchange(tmp_path, "SZSE")[1]["review"]["affectedCount"] == 1
    name = json.loads(old)["snapshot"]
    (pointer.parent / name).write_text("tampered", encoding="utf-8")
    with pytest.raises(DelistedSourceError):
        load_exchange(tmp_path, "SZSE")


def test_disappeared_historical_record_is_preserved_and_flagged_until_source_recovers(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo
    from market_monitor import strategy_screener as screening
    from market_monitor.tdx_action_evidence import ActionSnapshot

    row1 = {"zqdm": "000005", "zqjc": "样本一", "zzrq": "2025-01-10"}
    row2 = {"zqdm": "000006", "zqjc": "样本二", "zzrq": "2025-02-10"}

    class Response:
        content = _page("SZSE", 1, 2, 2, [row1, row2])

        def raise_for_status(self):
            pass

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    client = Session()
    refresh_exchange(tmp_path, "SZSE", session=client)
    _before, version_before = _delisted_evidence("2026-10-30", tmp_path)
    pointer = tmp_path / "bronze" / "exchange-delisted" / "SZSE-latest.json"
    prior = pointer.read_bytes()
    Response.content = _page("SZSE", 1, 1, 1, [row1])
    with pytest.raises(exchange_delisted.DelistedReviewRequired):
        refresh_exchange(tmp_path, "SZSE", session=client)
    assert pointer.read_bytes() == prior
    saved, status = load_exchange(tmp_path, "SZSE")
    assert len(saved) == 2 and status["review"]["affectedIds"] == ["CN.SZSE.STOCK.000006"]
    facts, version_after = _delisted_evidence("2026-10-30", tmp_path)
    assert version_after != version_before
    assert facts["CN.SZSE.STOCK.000006"]["reviewStatus"] == "REVIEW_REQUIRED"
    assert "reviewStatus" not in facts["CN.SZSE.STOCK.000005"]
    review_pointer = pointer.parent / "SZSE-review.json"
    review_before = review_pointer.read_bytes()
    Response.content = b"invalid-json"
    with pytest.raises(DelistedSourceError):
        refresh_exchange(tmp_path, "SZSE", session=client)
    assert review_pointer.read_bytes() == review_before

    ids = ["CN.SZSE.STOCK.000005", "CN.SZSE.STOCK.000006"]
    monkeypatch.setattr(screening, "_logical_instruments", lambda _root: {key: {
        "instrumentId": key, "storageInstrumentId": key + ".TDX_LOCAL", "market": "CN", "assetType": "STOCK"} for key in ids})
    monkeypatch.setattr(screening, "market_data_version", lambda _root: "revision")
    monkeypatch.setattr(screening, "revision_is_settled", lambda _root, _revision: True)
    monkeypatch.setattr(screening, "load_action_snapshot", lambda: ActionSnapshot("UNAVAILABLE", None, None, {}))
    monkeypatch.setattr(screening, "action_snapshot_is_current", lambda _snapshot: True)
    monkeypatch.setattr(screening, "get_kline_query_store", lambda _root: SimpleNamespace(
        bounds=lambda _id, _period: (10, "2024-01-01", "2024-12-31")))
    monkeypatch.setattr(screening, "_recorded_limit_counts", lambda *_args: {})
    monkeypatch.setattr(screening, "fetch_limit_pools", lambda *_args: ({}, []))
    result = screening.screen_batch(tmp_path, "value = 1\nsignal = True", 200, 0, 2,
                                    now=datetime(2026, 11, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai")), session=client)
    assert [item["instrumentId"] for item in result["excludedDelisted"]] == [ids[0]]
    assert [item["instrumentId"] for item in result["unavailable"]] == [ids[1]]
    assert "历史摘牌记录发生变动" in result["unavailable"][0]["reason"]

    Response.content = _page("SZSE", 1, 2, 2, [row1, row2])
    refresh_exchange(tmp_path, "SZSE", session=client)
    assert "review" not in load_exchange(tmp_path, "SZSE")[1]


def test_new_records_can_be_published_without_removing_existing_history(tmp_path: Path):
    first = {"zqdm": "000005", "zqjc": "样本一", "zzrq": "2025-01-10"}
    second = {"zqdm": "000006", "zqjc": "样本二", "zzrq": "2025-02-10"}

    class Response:
        content = _page("SZSE", 1, 1, 1, [first])

        def raise_for_status(self):
            pass

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    refresh_exchange(tmp_path, "SZSE", session=Session())
    Response.content = _page("SZSE", 1, 2, 2, [first, second])
    refresh_exchange(tmp_path, "SZSE", session=Session())
    rows, status = load_exchange(tmp_path, "SZSE")
    assert len(rows) == 2 and "review" not in status


def test_admin_review_details_are_bounded_and_filter_both_dates(tmp_path: Path):
    baseline = [{"zqdm": "000005", "zqjc": "样本一", "zzrq": "2025-01-10"},
                {"zqdm": "000006", "zqjc": "样本二", "zzrq": "2025-02-10"},
                {"zqdm": "000007", "zqjc": "样本三", "zzrq": "2025-03-10"},
                {"zqdm": "000008", "zqjc": "样本四", "zzrq": "2025-04-10"}]

    class Response:
        content = _page("SZSE", 1, 4, 4, baseline)

        def raise_for_status(self):
            pass

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    refresh_exchange(tmp_path, "SZSE", session=Session())
    changed = [baseline[0], {**baseline[2], "zzrq": "2026-02-10", "zqjc": "新名称"}, {**baseline[3], "zqjc": "改名"}]
    Response.content = _page("SZSE", 1, 3, 3, changed)
    with pytest.raises(exchange_delisted.DelistedReviewRequired):
        refresh_exchange(tmp_path, "SZSE", session=Session())
    public_status = load_exchange(tmp_path, "SZSE")[1]
    assert "changes" not in public_status["review"]
    items = {item["key"]: item for item in delisted_snapshot_items(tmp_path, limit=2)}
    review = items["exchange.szse-delisted"]["listingSnapshot"]["review"]
    assert review["affectedCount"] == review["matchedCount"] == 3
    assert len(review["changes"]) == 2 and review["truncated"] is True
    assert [item["kind"] for item in review["changes"]] == ["MISSING", "DATE_CHANGED"]
    filtered = {item["key"]: item for item in delisted_snapshot_items(tmp_path, start="2026-01-01")}
    review = filtered["exchange.szse-delisted"]["listingSnapshot"]["review"]
    assert review["affectedCount"] == 3 and review["matchedCount"] == 1
    assert review["changes"][0]["currentDate"] == "2026-02-10"
    assert review["changes"][0]["previousName"] == "样本三"
    assert review["changes"][0]["currentName"] == "新名称"
    version = public_status["review"]["sha256"]
    first_page = review_page(tmp_path, "SZSE", version=version, offset=0, limit=2)
    assert first_page["matchedCount"] == 3 and first_page["hasMore"]
    second_page = review_page(tmp_path, "SZSE", version=version, offset=2, limit=2)
    assert [item["kind"] for item in second_page["changes"]] == ["NAME_CHANGED"]
    assert not second_page["hasMore"]
    assert review_page(tmp_path, "SZSE", version=version, start="2026-01-01")["matchedCount"] == 1
    with pytest.raises(DelistedReviewChanged):
        review_page(tmp_path, "SZSE", version="0" * 64)
    with pytest.raises(ValueError):
        review_page(tmp_path, "SZSE", version=version, offset=-1)
    Response.content = _page("SZSE", 1, 4, 4, baseline)
    refresh_exchange(tmp_path, "SZSE", session=Session())
    with pytest.raises(DelistedReviewChanged):
        review_page(tmp_path, "SZSE", version=version)


def test_identical_review_retry_preserves_version_and_changed_raw_page_invalidates_it(tmp_path: Path, monkeypatch):
    rows = [{"zqdm": "000005", "zqjc": "样本一", "zzrq": "2025-01-10"},
            {"zqdm": "000006", "zqjc": "样本二", "zzrq": "2025-02-10"}]

    class Response:
        content = _page("SZSE", 1, 2, 2, rows)

        def raise_for_status(self):
            pass

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    refresh_exchange(tmp_path, "SZSE", session=Session())
    Response.content = _page("SZSE", 1, 1, 1, rows[:1])
    with pytest.raises(exchange_delisted.DelistedReviewRequired):
        refresh_exchange(tmp_path, "SZSE", session=Session())
    original = load_exchange(tmp_path, "SZSE")[1]["review"]
    version = original["sha256"]
    pointer = tmp_path / "bronze" / "exchange-delisted" / "SZSE-review.json"
    pointer_bytes = pointer.read_bytes()
    _, evidence_version = _delisted_evidence("2026-10-30", tmp_path)
    future = datetime.now(timezone.utc) + timedelta(days=1)

    class FutureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return future.astimezone(tz) if tz else future.replace(tzinfo=None)

    monkeypatch.setattr(exchange_delisted, "datetime", FutureClock)
    with pytest.raises(exchange_delisted.DelistedReviewRequired):
        refresh_exchange(tmp_path, "SZSE", session=Session())
    review = load_exchange(tmp_path, "SZSE")[1]["review"]
    assert pointer.read_bytes() == pointer_bytes
    assert review["sha256"] == version and review["observedAt"] == original["observedAt"]
    assert review["lastCheckedAt"] == future.isoformat(timespec="seconds")
    assert "pageSha256" not in review
    assert review_page(tmp_path, "SZSE", version=version)["matchedCount"] == 1
    assert _delisted_evidence("2026-10-30", tmp_path)[1] == evidence_version
    admin = {item["key"]: item for item in delisted_snapshot_items(tmp_path)}
    assert admin["exchange.szse-delisted"]["listingSnapshot"]["review"]["lastCheckedAt"] == review["lastCheckedAt"]

    marker = pointer.parent / "SZSE-review-last-check.json"
    marker.write_text("invalid", encoding="utf-8")
    assert "lastCheckedAt" not in load_exchange(tmp_path, "SZSE")[1]["review"]
    marker.write_text(json.dumps({"version": "0" * 64, "checkedAt": future.isoformat()}), encoding="utf-8")
    assert "lastCheckedAt" not in load_exchange(tmp_path, "SZSE")[1]["review"]

    # Equivalent parsed records with changed source bytes still require a new version.
    Response.content = json.dumps(json.loads(Response.content), ensure_ascii=False, indent=2).encode("utf-8")
    with pytest.raises(exchange_delisted.DelistedReviewRequired):
        refresh_exchange(tmp_path, "SZSE", session=Session())
    changed = load_exchange(tmp_path, "SZSE")[1]["review"]
    assert changed["sha256"] != version and "lastCheckedAt" not in changed
    with pytest.raises(DelistedReviewChanged):
        review_page(tmp_path, "SZSE", version=version)


def test_corrupt_review_evidence_fails_closed(tmp_path: Path):
    row1 = {"zqdm": "000005", "zqjc": "样本一", "zzrq": "2025-01-10"}
    row2 = {"zqdm": "000006", "zqjc": "样本二", "zzrq": "2025-02-10"}

    class Response:
        content = _page("SZSE", 1, 2, 2, [row1, row2])

        def raise_for_status(self):
            pass

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    refresh_exchange(tmp_path, "SZSE", session=Session())
    Response.content = _page("SZSE", 1, 1, 1, [row1])
    with pytest.raises(exchange_delisted.DelistedReviewRequired):
        refresh_exchange(tmp_path, "SZSE", session=Session())
    pointer = tmp_path / "bronze" / "exchange-delisted" / "SZSE-review.json"
    pointer.write_text("invalid", encoding="utf-8")
    with pytest.raises(DelistedSourceError):
        load_exchange(tmp_path, "SZSE")


def test_exchange_list_extends_evidence_and_changes_screener_fingerprint(tmp_path: Path):
    row = {"zqdm": "301139", "zqjc": "样本", "zzrq": "2026-10-28"}

    class Response:
        content = _page("SZSE", 1, 1, 1, [row])

        def raise_for_status(self):
            pass

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    before, old_version = _delisted_evidence("2026-10-30", tmp_path)
    refresh_exchange(tmp_path, "SZSE", session=Session())
    after, new_version = _delisted_evidence("2026-10-30", tmp_path)
    assert "CN.SZSE.STOCK.301139" not in before
    assert after["CN.SZSE.STOCK.301139"]["delistedAt"] == "2026-10-28"
    assert after["CN.SZSE.STOCK.301139"]["sourceType"] == "EXCHANGE_LIST"
    assert old_version != new_version
    prior, _ = _delisted_evidence("2026-10-27", tmp_path)
    assert "CN.SZSE.STOCK.301139" not in prior


def test_daily_refresh_skips_successful_source_and_retries_failed_source(tmp_path: Path, monkeypatch):
    calls = []

    def load(_root, exchange):
        return [], {"exchange": exchange, "status": "PASS", "count": 1,
                    "fetchedAt": "2026-10-20T11:30:00+00:00" if exchange == "SSE" else "2026-10-19T12:00:00+00:00"}

    def refresh(_root, exchange):
        calls.append(exchange)
        raise DelistedSourceError("synthetic")

    monkeypatch.setattr(exchange_delisted, "load_exchange", load)
    monkeypatch.setattr(exchange_delisted, "refresh_exchange", refresh)
    result = exchange_delisted.refresh_due(tmp_path, datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc))
    assert result["status"] == "PARTIAL_FAILURE" and calls == ["SZSE"]
    assert [x["status"] for x in result["items"]] == ["SKIPPED_FRESH", "FAILED"]


def test_fresh_snapshot_with_review_is_not_skipped(tmp_path: Path, monkeypatch):
    calls = []

    def load(_root, exchange):
        status = {"exchange": exchange, "status": "PASS", "count": 1, "fetchedAt": "2026-10-20T11:30:00+00:00"}
        if exchange == "SSE":
            status["review"] = {"status": "REVIEW_REQUIRED"}
        return [], status

    def refresh(_root, exchange):
        calls.append(exchange)
        raise DelistedSourceError("synthetic")

    monkeypatch.setattr(exchange_delisted, "load_exchange", load)
    monkeypatch.setattr(exchange_delisted, "refresh_exchange", refresh)
    result = exchange_delisted.refresh_due(tmp_path, datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc))
    assert calls == ["SSE"] and result["status"] == "PARTIAL_FAILURE"


def test_admin_inventory_distinguishes_date_range_empty_missing_and_corrupt(tmp_path: Path):
    rows = [{"zqdm": "000004", "zqjc": "样本", "zzrq": "2026-07-14"},
            {"zqdm": "301139", "zqjc": "样本二", "zzrq": "2026-10-28"}]

    class Response:
        content = _page("SZSE", 1, 2, 2, rows)

        def raise_for_status(self):
            pass

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    refresh_exchange(tmp_path, "SZSE", session=Session())
    (tmp_path / "operations.json").write_text(json.dumps([{"kind": "A_SHARE_DELISTED_UPDATE", "status": "PARTIAL_FAILURE",
        "created_at": "2026-10-30T11:00:00+00:00", "completed_at": "2026-10-30T11:01:00+00:00",
        "detail": "PARTIAL_FAILURE: 沪深交易所摘牌名单检查；SSE=SKIPPED_FRESH、SZSE=FAILED；"}]), encoding="utf-8")
    items = {item["key"]: item for item in delisted_snapshot_items(tmp_path, start="2026-10-01")}
    sz = items["exchange.szse-delisted"]
    assert sz["listingSnapshot"]["records"] == 1 and sz["listingSnapshot"]["totalRecords"] == 2
    assert sz["startDate"] == sz["latestDate"] == "2026-10-28"
    assert sz["listingSnapshot"]["lastJob"]["sourceOutcome"] == "FAILED"
    assert items["exchange.sse-delisted"]["listingSnapshot"]["lastJob"]["sourceOutcome"] == "SKIPPED_FRESH"
    assert items["exchange.sse-delisted"]["listingSnapshot"]["records"] is None
    (tmp_path / "bronze" / "exchange-delisted" / "SZSE-latest.json").write_text("invalid", encoding="utf-8")
    corrupt = {item["key"]: item for item in delisted_snapshot_items(tmp_path)}["exchange.szse-delisted"]
    assert corrupt["listingSnapshot"]["status"] == "INVALID" and corrupt["listingSnapshot"]["records"] is None
