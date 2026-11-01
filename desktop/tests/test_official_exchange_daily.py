"""Official exchange row contracts use synthetic dated responses, not live sites."""

from __future__ import annotations

import io
import json
from pathlib import Path

import openpyxl
import pytest

from market_monitor import official_exchange_daily as daily
from market_monitor.storage import MarketStore


DAY = "2026-10-21"


def _xlsx(rows: list[list[object]]) -> bytes:
    book = openpyxl.Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


def _sse(rows: list[dict[str, str]]) -> bytes:
    return json.dumps({"actionErrors": [], "result": rows}, ensure_ascii=False).encode()


def _market_row(name: str, cap: str, floating: str, amount: str, count: str) -> dict[str, str]:
    return {"PRODUCT_NAME": name, "TRADE_DATE": "20261021", "TOTAL_VALUE": cap,
            "NEGO_VALUE": floating, "TOTAL_TRADE_AMT": amount, "SECURITY_NUM": count}


def _stock_row(name: str, cap: str, floating: str, amount: str, count: str,
               companies: str, shares: str, float_shares: str) -> dict[str, str]:
    return {**_market_row(name, cap, floating, amount, count),
            "LIST_COM_NUM": companies, "TOTAL_ISSUE_VOL": shares,
            "NEGO_ISSUE_VOL": float_shares}


def _szse_table_row(name: str, count: str, amount_yuan: str,
                    cap_yuan: str, float_yuan: str) -> list[str]:
    return [name, count, amount_yuan, cap_yuan, float_yuan]


def _indicator_rows(items: list[tuple[str, str]]) -> list[list[str]]:
    return [["指标名称", "本日数值", "比上日增减", "幅度%"],
            *[[name, value, "0", "0"] for name, value in items]]


def _bodies() -> dict[str, bytes]:
    market = [
        _market_row("股票", "121", "95.5", "15.1", "4"),
        _market_row("主板A", "100", "80", "10", "2"),
        _market_row("主板B", "1", "0.5", "0.1", "1"),
        _market_row("科创板", "20", "15", "5", "1"),
        _market_row("基金", "57", "56", "1.12", "3"),
        _market_row("ETF", "50", "50", "1", "1"),
        _market_row("LOF", "5", "5", "0.1", "1"),
        _market_row("公募REITs", "2", "1", "0.02", "1"),
        _market_row("债券", "1000", "1000", "3", "10"),
        _market_row("政府债", "400", "400", "1", "4"),
        _market_row("信用债", "600", "600", "2", "6"),
        _market_row("回购", "-", "-", "10", "1"),
    ]
    stock = [
        _stock_row("股票", "121", "95.5", "15.1", "4", "4", "300", "240"),
        _stock_row("主板A", "100", "80", "10", "2", "2", "250", "190"),
        _stock_row("科创板", "20", "15", "5", "1", "1", "40", "40"),
    ]
    volumes = [{"PRODUCT_CODE": code, "TRADE_DATE": "20261021", "TRADE_VOL": volume,
                "TRADE_AMT": amount} for code, volume, amount in (
                    ("01", "5", "10"), ("02", "0.1", "0.1"), ("03", "2", "5"),
                    ("11", "0", "0"), ("17", "7.1", "15.1"))]
    categories = [
        ["证券类别", "数量(只)", "成交金额(元)", "总市值(元)", "流通市值(元)"],
        _szse_table_row("股票", "4", "1202000000", "11200000000", "8100000000"),
        _szse_table_row("主板A股", "2", "800000000", "8000000000", "6000000000"),
        _szse_table_row("主板B股", "1", "2000000", "200000000", "100000000"),
        _szse_table_row("创业板A股", "1", "400000000", "3000000000", "2000000000"),
        _szse_table_row("基金", "4", "130000000", "1300000000", "1200000000"),
        _szse_table_row("ETF", "1", "100000000", "1000000000", "1000000000"),
        _szse_table_row("LOF", "1", "10000000", "100000000", "100000000"),
        _szse_table_row("封闭式基金", "1", "0", "0", "0"),
        _szse_table_row("不动产基金", "1", "20000000", "200000000", "100000000"),
        _szse_table_row("债券", "3", "710000000", "-", "-"),
        _szse_table_row("债券现券", "1", "200000000", "10000000000", "10000000000"),
        _szse_table_row("债券回购", "1", "500000000", "-", "-"),
        _szse_table_row("ABS", "1", "10000000", "1000000000", "1000000000"),
    ]
    all_stock = _indicator_rows([
        ("上市公司数", "3"), ("股票总股本（股/份）", "30000000000"),
        ("股票流通股本（股/份）", "25000000000"),
        ("股票总市值（元）", "11200000000"),
        ("股票流通市值（元）", "8100000000"),
        ("股票成交金额（元）", "1202000000"),
    ])
    main = _indicator_rows([
        ("上市公司数", "2"), ("总股本(股/份)", "20000000000"),
        ("流通股本(股/份)", "17000000000"), ("成交量", "300000000"),
    ])
    chinext = _indicator_rows([
        ("上市公司数", "1"), ("总股本(股/份)", "10000000000"),
        ("流通股本(股/份)", "8000000000"), ("成交量", "200000000"),
    ])
    bse_stock = [{"xxzrlx": "2", "rq": "20261021", "gpgsjs": 1,
                  "xxzgb": 1000000000, "xxfxsgb": 800000000,
                  "zsz": 2000000000, "ltsz": 1500000000,
                  "hqcjje": 200000000, "hqcjsl": 100000000}]
    bse_bonds = [{"zqlb": code, "rq": DAY, "tgzs": count, "drcjje": 0,
                  "drcjbs": 0} for code, count in (("20", 2), ("30", 1), ("sum", 3))]
    return {
        "sse_market": _sse(market), "sse_stock": _sse(stock), "sse_volume": _sse(volumes),
        "szse_categories": _xlsx(categories), "szse_stock": _xlsx(all_stock),
        "szse_main": _xlsx(main), "szse_chinext": _xlsx(chinext),
        "bse_stock": f"{daily._CALLBACK}({json.dumps(bse_stock)})".encode(),
        "bse_bond": f"{daily._CALLBACK}({json.dumps(bse_bonds)})".encode(),
    }


def test_venue_and_comparable_totals_do_not_double_count_parent_rows() -> None:
    records = daily._parse_day(DAY, _bodies())
    assert records[("ALL", "STOCK_A")]["capYi"] == 250
    assert records[("ALL", "STOCK_A")]["floatCapYi"] == 190
    assert records[("ALL", "STOCK_A")]["turnoverYi"] == 29
    assert records[("ALL", "STOCK_LISTED")]["companyCount"] == 8
    assert records[("ALL", "STOCK_LISTED")]["sharesYi"] == 610
    assert records[("ALL", "STOCK_LISTED")]["volumeYi"] == daily.Decimal("13.1")
    assert records[("SSE_SZSE", "FUND")]["capYi"] == 70
    assert records[("SSE_SZSE", "FUND")]["turnoverYi"] == daily.Decimal("2.42")
    assert records[("ALL", "BOND_CASH")]["turnoverYi"] == daily.Decimal("5.1")
    assert records[("BSE", "BOND_CASH")]["turnoverYi"] == 0


def test_sse_fund_repo_is_stored_without_changing_fund_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _sse([
        {"PRODUCT_CODE": code, "TRADE_DATE": "20261021", "TRADE_VOL": volume,
         "TRADE_AMT": amount}
        for code, volume, amount in (
            ("05", "15", "1.12"), ("13", "13", "1"), ("14", "1.5", "0.1"),
            ("16", "0.5", "0.02"), ("12", "0.07", "0.03"),
        )
    ])
    values = daily.extensions.parse_sse_fund_daily(raw, DAY)
    monkeypatch.setattr(daily, "_EXTENSION_NAMES", ("sse_fund_daily",))
    monkeypatch.setattr(daily, "_read_extension", lambda *_args: (values, raw))
    records = daily._parse_day(DAY, _bodies())
    blobs, errors = daily._merge_extensions(tmp_path, DAY, object(), records)
    assert not errors
    assert records[("SSE", "FUND_REPO")]["turnoverYi"] == daily.Decimal("0.03")
    assert records[("SSE_SZSE", "FUND")]["turnoverYi"] == daily.Decimal("2.42")
    rows = daily._gold_rows(DAY, records, blobs)
    repo = next(row for row in rows if "SSE.MARKET.FUND_REPO" in row["metric_id"]
                and row["metric_id"].endswith(":TOTAL_AMOUNT_YI"))
    assert repo["value"] == 0.03


def test_cached_fund_repo_without_gold_is_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"verified fund response containing a repo row"
    repo_values = {("SSE", "FUND_REPO"): {
        "volumeYi": daily.Decimal("0.07"), "turnoverYi": daily.Decimal("0.03")}}
    cached = {"sse_fund_daily": (repo_values, raw), "szse_fund_daily": ({}, b"szse"),
              "sse_bond_custody_daily": ({}, b"bond")}
    monkeypatch.setattr(daily, "_read_extension", lambda _root, _day, source: cached[source])
    records = {
        ("SSE_SZSE", "FUND"): {"volumeYi": daily.Decimal("15")},
        ("SSE", "BOND_CUSTODY_ALL"): {"custodyValueYi": daily.Decimal("1"),
                                       "custodyFaceYi": daily.Decimal("1")},
    }
    store = MarketStore(tmp_path)
    try:
        store.upsert_gold_metrics(daily._gold_rows(DAY, records, {"sse_fund_daily": raw}))
        assert not daily._extensions_complete(store, tmp_path, DAY)
        records.update(repo_values)
        store.upsert_gold_metrics(daily._gold_rows(DAY, records, {"sse_fund_daily": raw}))
        assert daily._extensions_complete(store, tmp_path, DAY)
    finally:
        store.close()


def test_wrong_source_day_and_bse_callback_are_rejected() -> None:
    bodies = _bodies()
    bodies["sse_market"] = bodies["sse_market"].replace(b"20261021", b"20261022")
    with pytest.raises(ValueError, match="date"):
        daily._parse_day(DAY, bodies)
    bodies = _bodies()
    bodies["bse_stock"] = bodies["bse_stock"].replace(daily._CALLBACK.encode(), b"other")
    with pytest.raises(ValueError, match="BSE daily response"):
        daily._parse_day(DAY, bodies)


def test_sse_optional_code_11_can_be_absent_without_losing_stock_volume() -> None:
    bodies = _bodies()
    response = json.loads(bodies["sse_volume"])
    response["result"] = [item for item in response["result"] if item["PRODUCT_CODE"] != "11"]
    bodies["sse_volume"] = json.dumps(response).encode()
    records = daily._parse_day(DAY, bodies)
    assert records[("ALL", "STOCK_LISTED")]["volumeYi"] == daily.Decimal("13.1")


def test_missing_bse_bond_keeps_stock_fund_without_pretending_bond_total() -> None:
    bodies = _bodies()
    del bodies["bse_bond"]
    records = daily._parse_day(DAY, bodies)
    assert records[("ALL", "STOCK_A")]["capYi"] == 250
    assert records[("SSE_SZSE", "FUND")]["capYi"] == 70
    assert ("ALL", "BOND_CASH") not in records


def test_replay_requires_matching_bronze_sha_and_gold(tmp_path: Path) -> None:
    bodies = _bodies()
    artifacts = {}
    for source, blob in bodies.items():
        method, url, parameters, _ = daily._request(source, DAY)
        query = parameters if method == "GET" else {"callback": parameters["callback"]}
        request_url = daily.requests.Request(method, url, params=query).prepare().url
        assert request_url is not None
        artifacts[source] = daily._Artifact(source, request_url, request_url,
                                             "2026-10-21T16:00:00+00:00", blob)
    daily._save_bronze(tmp_path, DAY, artifacts)
    assert daily._read_bronze(tmp_path, DAY) == bodies
    store = MarketStore(tmp_path)
    try:
        store.upsert_gold_metrics(daily._gold_rows(DAY, daily._parse_day(DAY, bodies), bodies))
        assert daily._published_complete(store, DAY, bodies)
    finally:
        store.close()
    target = tmp_path / "bronze" / "official_exchange_daily" / f"day={DAY}" / "bse_stock.raw"
    target.write_bytes(target.read_bytes() + b"tampered")
    assert daily._read_bronze(tmp_path, DAY) is None


def test_missing_bond_source_retries_without_losing_published_stocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies = _bodies()
    availability = {"bond": False}
    calls: list[str] = []

    def fetch(_session: object, source: str, day: str) -> daily._Artifact:
        calls.append(source)
        if source == "bse_bond" and not availability["bond"]:
            raise ValueError("bond report unavailable")
        method, url, parameters, _ = daily._request(source, day)
        query = parameters if method == "GET" else {"callback": parameters["callback"]}
        request_url = daily.requests.Request(method, url, params=query).prepare().url
        assert request_url is not None
        return daily._Artifact(source, request_url, request_url,
                               "2026-10-21T16:00:00+00:00", bodies[source])

    monkeypatch.setattr(daily, "_fetch", fetch)
    monkeypatch.setattr(daily, "_merge_extensions", lambda *_args: ({}, []))
    first = daily.sync_official_exchange_daily(
        tmp_path, start_day=DAY, end_day=DAY, session=object(),
    )
    assert first["writtenDays"] == [DAY]
    assert first["failedDays"][0]["day"] == DAY
    store = MarketStore(tmp_path)
    try:
        assert store.connection.execute("""SELECT value FROM gold_metrics WHERE metric_id=?""", [
            f"OFFICIAL_EXCHANGE_DAILY:CN.ALL.MARKET.STOCK_A:{DAY}:1d:TOTAL_MARKET_CAP_YI"
        ]).fetchone()[0] == 250
        assert store.connection.execute("""SELECT 1 FROM gold_metrics WHERE metric_id=?""", [
            f"OFFICIAL_EXCHANGE_DAILY:CN.ALL.MARKET.BOND_CASH:{DAY}:1d:TOTAL_AMOUNT_YI"
        ]).fetchone() is None
    finally:
        store.close()
    availability["bond"] = True
    second = daily.sync_official_exchange_daily(tmp_path, start_day=DAY, end_day=DAY,
                                                session=object())
    assert second["failedDays"] == []
    assert calls.count("bse_bond") == 2
    assert calls.count("sse_market") == 1
    assert daily._read_bronze(tmp_path, DAY) == bodies


def test_due_refresh_retries_failures_older_than_rolling_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    daily._update_backlog(tmp_path, "daily", completed=[],
                          failed=[{"day": "2026-08-07", "error": "SOURCE_UNAVAILABLE"}], key="day")
    calls: list[tuple[str, str]] = []

    def refresh(root: Path, *, start_day: str, end_day: str, session: object) -> dict:
        calls.append((start_day, end_day))
        daily._update_backlog(root, "daily", completed=[start_day], failed=[], key="day")
        return {"writtenDays": [start_day], "skippedDays": [], "closedDays": [],
                "failedDays": [], "metricCount": 1}

    monkeypatch.setattr(daily, "sync_official_exchange_daily", refresh)
    result = daily.sync_official_exchange_due(
        tmp_path, start_day="2026-10-20", end_day="2026-10-23", session=object(),
    )
    assert calls == [("2026-10-20", "2026-10-23"), ("2026-08-07", "2026-08-07")]
    assert result["retriedBacklogDays"] == ["2026-08-07"]
    assert result["backlogRemaining"] == 0
