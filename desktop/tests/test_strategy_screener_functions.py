from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import requests

from market_monitor.formula_engine import FormulaError, period_return, sum_daily_returns, up_days
from market_monitor import strategy_screener as screening
from market_monitor.composite_program import TEMPLATE, compile_program, evaluate_part
from market_monitor.tdx_action_evidence import ActionSnapshot, _index_events


def test_up_days_uses_previous_close_and_requires_complete_window():
    assert up_days([10, 11, 10, 10, 12], 4) == [None, None, None, None, 2]
    assert up_days([10, 11, None, 12, 13], 4)[-1] is None
    assert up_days([10, 11, 0, 12, 13], 4)[-1] is None


def test_sum_daily_returns_differs_from_cumulative_return():
    close = [100.0, 110.0, 99.0]
    assert sum_daily_returns(close, 2)[-1] == pytest.approx(0.0)
    assert period_return(close, 2)[-1] == pytest.approx(-0.01)


def test_up_days_registered_function_can_be_used_by_python_composite():
    source = TEMPLATE.replace('fn("price.percentage_change", 1, close, 10)', 'fn("up_days", 1, close, 20)')
    program = compile_program(source)
    bars = [{"close": float(index + 10)} for index in range(21)]
    result = evaluate_part(program, "attention", bars, {"direction": 0, "observed_win_rate": None, "margin_rate": None})
    assert result["long"] is True
    assert any(item["id"] == "up_days" and item["version"] == 1 for item in program.dependencies)


def test_pool_requires_matching_saved_count_and_unique_codes():
    payload = {"rc": 0, "data": {"qdate": 20261030, "tc": 1, "pool": [{"c": "600519"}]}}
    # qdate is the latest provider date, not necessarily the requested date.
    assert screening.parse_limit_pool(payload, "2026-10-26", 1) == {"600519"}
    with pytest.raises(FormulaError):
        screening.parse_limit_pool(payload, "2026-10-26", 2)
    with pytest.raises(FormulaError):
        screening.parse_limit_pool({"rc": 0, "data": {"tc": 2, "pool": [{"c": "600519"}] * 2}}, "2026-10-26", 2)


def test_transient_bad_pool_is_retried_but_never_accepted(monkeypatch):
    monkeypatch.setattr(screening.clock, "sleep", lambda _delay: None)

    class Response:
        content = b"fixture"

        def __init__(self, code):
            self.code = code

        def raise_for_status(self):
            pass

        def json(self):
            return {"rc": 0, "data": {"tc": 1, "pool": [{"c": self.code}]}}

    class Session:
        calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            return Response("bad" if self.calls == 1 else "600519")

    client = Session()
    expected = {("20261027", "getTopicZTPool"): 1, ("20261027", "getTopicDTPool"): 1}
    pools, _ = screening.fetch_limit_pools(client, ["2026-10-27"], expected)
    assert client.calls == 3
    assert pools["2026-10-27"] == ({"600519"}, {"600519"})


def test_a_share_scope_and_closing_quote_time_are_explicit():
    now = datetime(2026, 11, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert screening._a_share_symbol("CN.SSE.STOCK.600519") == "sh600519"
    assert screening._a_share_symbol("CN.SSE.STOCK.900901") is None
    assert screening._a_share_symbol("CN.SSE.ETF.510300") is None
    assert screening._same_day_closing_quote("20261030161500", "2026-10-30", now)
    assert not screening._same_day_closing_quote("20261030145900", "2026-10-30", now)
    assert not screening._same_day_closing_quote("20261029161500", "2026-10-30", now)


def test_only_evidenced_and_effective_delistings_are_excluded():
    before, _ = screening._delisted_evidence("2026-10-18")
    after, fingerprint = screening._delisted_evidence("2026-10-19")
    assert "CN.SSE.STOCK.601059" not in before
    assert after["CN.SSE.STOCK.601059"]["delistedAt"] == "2026-10-19"
    assert "CN.SSE.STOCK.601198" in after
    assert "CN.SZSE.STOCK.000004" in after and len(fingerprint) == 64
    assert after["CN.BSE.STOCK.920305"]["delistedAt"] == "2026-07-30"
    assert after["CN.BSE.STOCK.920680"]["delistedAt"] == "2026-01-05"
    assert "CN.SZSE.STOCK.000001" not in after


def test_missing_or_malformed_delisting_evidence_stops_screening(tmp_path, monkeypatch):
    path = tmp_path / "events.json"
    monkeypatch.setattr(screening, "_DELISTED_PATH", path)
    with pytest.raises(FormulaError, match="摘牌资料不可读取"):
        screening._delisted_evidence("2026-10-30")
    path.write_text('{"schemaVersion": 1, "items": [{"instrumentId": "wrong"}]}', encoding="utf-8")
    with pytest.raises(FormulaError, match="摘牌资料不可读取"):
        screening._delisted_evidence("2026-10-30")


def test_known_delisted_stock_is_not_requested_as_current_quote(tmp_path, monkeypatch):
    instrument = "CN.SSE.STOCK.601059"
    monkeypatch.setattr(screening, "_logical_instruments", lambda _root: {instrument: {
        "instrumentId": instrument, "storageInstrumentId": instrument + ".TDX_LOCAL", "market": "CN", "assetType": "STOCK"}})
    monkeypatch.setattr(screening, "market_data_version", lambda _root: "revision")
    monkeypatch.setattr(screening, "revision_is_settled", lambda _root, _revision: True)
    monkeypatch.setattr(screening, "load_action_snapshot", lambda: ActionSnapshot("UNAVAILABLE", None, None, {}))
    monkeypatch.setattr(screening, "action_snapshot_is_current", lambda _snapshot: True)
    monkeypatch.setattr(screening, "get_kline_query_store", lambda _root: SimpleNamespace(
        bounds=lambda _id, _period: (10, "2026-01-01T00:00:00+08:00", "2026-09-14T00:00:00+08:00")))
    monkeypatch.setattr(screening, "_recorded_limit_counts", lambda _root, _days: {})
    monkeypatch.setattr(screening, "fetch_limit_pools", lambda *_args: ({}, []))

    class Session:
        def get(self, *_args, **_kwargs):
            pytest.fail("摘牌标的不应请求当前报价")

    result = screening.screen_batch(tmp_path, "value = 1\nsignal = True", 200, 0, 1,
                                    now=datetime(2026, 11, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai")), session=Session())
    assert result["processed"] == 1 and result["excludedDelisted"][0]["instrumentId"] == instrument
    assert result["belowMarketCap"] == result["evaluated"] == 0 and result["unavailable"] == []
    with monkeypatch.context() as context:
        context.setattr(screening, "_SCREEN_POLICY_VERSION", "synthetic-next-policy")
        changed = screening.screen_batch(tmp_path, "value = 1\nsignal = True", 200, 0, 1,
                                        now=datetime(2026, 11, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai")), session=Session())
        assert changed["universeHash"] != result["universeHash"]

    with monkeypatch.context() as context:
        primary = instrument + ".TDX_LOCAL"
        secondary = instrument + ".OTHER_SOURCE"
        context.setattr(screening, "_logical_instruments", lambda _root: {instrument: {
            "instrumentId": instrument, "storageInstrumentId": primary, "sourceCandidates": [primary, secondary],
            "market": "CN", "assetType": "STOCK"}})
        context.setattr(screening, "get_kline_query_store", lambda _root: SimpleNamespace(
            bounds=lambda source, _period: (10, "2026-01-01T00:00:00+08:00",
                "2026-10-20T00:00:00+08:00" if source == secondary else "2026-09-14T00:00:00+08:00")))
        multi = screening.screen_batch(tmp_path, "value = 1\nsignal = True", 200, 0, 1,
                                       now=datetime(2026, 11, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai")), session=Session())
        assert multi["universeHash"] != result["universeHash"]
        assert multi["excludedDelisted"] == []
        assert multi["unavailable"][0]["lastLocalDay"] == "2026-10-20"

        def failed_secondary(source, _period):
            if source == secondary:
                raise screening.KLineCacheUnavailable("synthetic")
            return 10, "2026-01-01T00:00:00+08:00", "2026-09-14T00:00:00+08:00"

        context.setattr(screening, "get_kline_query_store", lambda _root: SimpleNamespace(bounds=failed_secondary))
        unknown = screening.screen_batch(tmp_path, "value = 1\nsignal = True", 200, 0, 1,
                                         now=datetime(2026, 11, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai")), session=Session())
        assert unknown["excludedDelisted"] == []
        assert "无法核对" in unknown["unavailable"][0]["reason"]

    monkeypatch.setattr(screening, "get_kline_query_store", lambda _root: SimpleNamespace(
        bounds=lambda _id, _period: (10, "2026-01-01T00:00:00+08:00", "2026-10-20T00:00:00+08:00")))
    conflict = screening.screen_batch(tmp_path, "value = 1\nsignal = True", 200, 0, 1,
                                      now=datetime(2026, 11, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai")), session=Session())
    assert conflict["excludedDelisted"] == []
    assert "证券身份或数据时序待核" in conflict["unavailable"][0]["reason"]
    assert conflict["unavailable"][0]["lastLocalDay"] == "2026-10-20"


def test_quote_diagnostics_distinguish_stale_missing_and_mismatched_data():
    now = datetime(2026, 11, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    quote = SimpleNamespace(symbol="sh600519", code="600519", quote_time="20261030090000", price=10, total_market_cap_yi=300)
    assert screening._quote_issue(None, "sh600519", "2026-10-30", now) == "腾讯未返回可解析的报价"
    assert screening._quote_issue(quote, "sh600519", "2026-10-30", now) == "腾讯报价时间早于收盘，无法核对收盘市值"
    quote.quote_time = "20261029150000"
    assert "不是目标交易日" in screening._quote_issue(quote, "sh600519", "2026-10-30", now)
    quote.quote_time = "20261030150000"
    assert screening._quote_issue(quote, "sh600519", "2026-10-30", now) is None
    quote.code = "600000"
    assert "代码与标的不一致" in screening._quote_issue(quote, "sh600519", "2026-10-30", now)


def test_local_action_index_is_market_scoped_and_only_positive_evidence():
    indexed = _index_events([(0, "000001", 20261023, 1), (1, "600000", 20261022, 1),
                             (2, "920000", 20261021, 11), (0, "000001", 20261029, 5)])
    snapshot = ActionSnapshot("LOCAL_RECORDS_UNVERIFIED", "hash", None, indexed)
    assert snapshot.window_events("CN.SZSE.STOCK.000001", "2026-10-01", "2026-10-30") == ["2026-10-23"]
    assert snapshot.window_events("CN.SSE.STOCK.000001", "2026-10-01", "2026-10-30") == []
    assert snapshot.window_events("CN.BSE.STOCK.920000", "2026-10-01", "2026-10-30") == ["2026-10-21"]


def test_adjusted_closes_require_matching_raw_source_and_observed_action():
    days = ["2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26"]

    class Response:
        content = b"fixture"

        def __init__(self, adjusted):
            self.adjusted = adjusted

        def raise_for_status(self):
            pass

        def json(self):
            closes = [9, 10, 10.5, 11] if self.adjusted else [10, 11, 10.5, 11]
            return {"data": {"sh600000": {"qfqday" if self.adjusted else "day":
                    [[day, "0", value] for day, value in zip(days, closes, strict=True)]}}}

    class Session:
        def get(self, _url, **kwargs):
            return Response(kwargs["params"]["param"].endswith(",qfq"))

    adjusted, proof = screening._adjusted_closes(Session(), "sh600000", days, [10, 11, 10.5, 11], ["2026-10-23"])
    assert adjusted == [9, 10, 10.5, 11] and proof["actionDays"] == ["2026-10-23"]
    assert proof["sourceHost"] == "ifzq.gtimg.cn"

    class FallbackSession(Session):
        def get(self, url, **kwargs):
            if "https://ifzq.gtimg.cn/" in url:
                raise requests.HTTPError("unavailable")
            return super().get(url, **kwargs)

    _, fallback = screening._adjusted_closes(FallbackSession(), "sh600000", days,
                                              [10, 11, 10.5, 11], ["2026-10-23"])
    assert fallback["sourceHost"] == "web.ifzq.gtimg.cn"
    with pytest.raises(ValueError, match="raw close disagreement"):
        screening._adjusted_closes(Session(), "sh600000", days, [10, 12, 10.5, 11], ["2026-10-23"])
    with pytest.raises(ValueError, match="action adjustment not observable"):
        screening._adjusted_closes(Session(), "sh600000", days, [10, 11, 10.5, 11], ["2026-10-22"])


def test_adjustment_source_challenge_is_backed_off(monkeypatch):
    monkeypatch.setattr(screening, "_FQ_BLOCKED_UNTIL", {})

    class Session:
        calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            raise requests.HTTPError("blocked", response=SimpleNamespace(status_code=501))

    session = Session()
    for _ in range(2):
        with pytest.raises(screening.AdjustmentSourceBlocked):
            screening._adjusted_closes(session, "sh600000", ["2026-10-22", "2026-10-23"], [10, 11], ["2026-10-23"])
    assert session.calls == 2


def test_python_screen_accepts_only_bound_functions_and_no_imports():
    program, lookback, _, limit_window = screening.validate_screen_source(
        "value = up_days(close, 20)\nsignal = value > 10 and no_limit_up(limit_up, 5)"
    )
    assert lookback == 20 and limit_window == 5 and program
    with pytest.raises(FormulaError):
        screening.validate_screen_source("import os\nvalue = 1\nsignal = True")
    with pytest.raises(FormulaError):
        screening.validate_screen_source("value = ma(close, 20)\nsignal = value > 1")


def test_screen_uses_same_day_quote_and_propagates_missing_bars(tmp_path, monkeypatch):
    now = datetime(2026, 11, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    days = screening.verified_days(now, 21)
    ids = ["CN.SZSE.STOCK.000001", "CN.SZSE.STOCK.000002"]
    items = {key: {"instrumentId": key, "storageInstrumentId": key + ".TDX_LOCAL",
                   "market": "CN", "assetType": "STOCK", "name": key[-6:]} for key in ids}
    rows = [{"trading_date": day, "close": 10 * 1.03 ** index, "source": "通达信金融终端（本地）",
             "quality_status": "PASS", "normalization_version": "tdx-cn-v2"}
            for index, day in enumerate(days)]
    monkeypatch.setattr(screening, "_logical_instruments", lambda _root: items)
    monkeypatch.setattr(screening, "market_data_version", lambda _root: "revision")
    monkeypatch.setattr(screening, "revision_is_settled", lambda _root, _revision: True)
    monkeypatch.setattr(screening, "load_action_snapshot", lambda: ActionSnapshot("UNAVAILABLE", None, None, {}))
    monkeypatch.setattr(screening, "action_snapshot_is_current", lambda _snapshot: True)
    monkeypatch.setattr(screening, "_recorded_limit_counts", lambda _root, _days: {})
    monkeypatch.setattr(screening, "fetch_limit_pools", lambda _client, selected, _counts:
                        ({day: (set(), set()) for day in selected}, []))
    monkeypatch.setattr(screening, "read_bars", lambda _root, instrument, **_kwargs:
                        rows if "000001" in instrument else rows[:-1])
    monkeypatch.setattr(screening, "parse_tencent_quotes", lambda _body: [
        SimpleNamespace(symbol="sz000001", code="000001", name="测试一", total_market_cap_yi=201, price=rows[-1]["close"], quote_time="20261030150000"),
        SimpleNamespace(symbol="sz000002", code="000002", name="测试二", total_market_cap_yi=201, price=rows[-1]["close"], quote_time="20261030150000"),
    ])

    class Response:
        text = "fixture"
        encoding = "gbk"

        def raise_for_status(self):
            pass

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    source = ("value = up_days(close, 20)\n"
              "signal = value > 10 and period_return(close, 5) > 0.1 "
              "and no_limit_up(limit_up, 5) and no_limit_down(limit_down, 5)")
    result = screening.screen_batch(tmp_path, source, 200, 0, 2, now=now, session=Session())
    assert [row["instrumentId"] for row in result["matches"]] == [ids[0]]
    assert result["matches"][0]["upDays20"] == 20
    assert result["matches"][0]["return5d"] == pytest.approx(1.03 ** 5 - 1)
    assert result["unavailable"][0]["instrumentId"] == ids[1]

    with monkeypatch.context() as context:
        context.setattr(screening, "parse_tencent_quotes", lambda _body: [
            SimpleNamespace(symbol="sz000001", code="000001", name="测试一", total_market_cap_yi=199,
                            price=rows[-1]["close"], quote_time="20261030150000"),
        ])
        # A valid latest close is sufficient to rule out the market-cap condition;
        # an incomplete earlier window need not prevent that conclusion.
        context.setattr(screening, "read_bars", lambda *_args, **_kwargs: rows[-1:])
        low_cap = screening.screen_batch(tmp_path, source, 200, 0, 1, now=now, session=Session())
        assert low_cap["belowMarketCap"] == 1 and not low_cap["unavailable"]
        context.setattr(screening, "read_bars", lambda *_args, **_kwargs: rows[:-1])
        missing = screening.screen_batch(tmp_path, source, 200, 0, 1, now=now, session=Session())
        assert missing["belowMarketCap"] == 0
        assert "目标交易日本地日线缺失" in missing["unavailable"][0]["reason"]
        context.setattr(screening, "read_bars", lambda *_args, **_kwargs: [{**rows[-1], "close": 1.0}])
        mismatch = screening.screen_batch(tmp_path, source, 200, 0, 1, now=now, session=Session())
        assert mismatch["belowMarketCap"] == 0
        assert "本地收盘价与腾讯同日报价不一致" in mismatch["unavailable"][0]["reason"]
        context.setattr(screening, "read_bars", lambda *_args, **_kwargs: [{**rows[-1], "quality_status": "FAIL"}])
        bad_quality = screening.screen_batch(tmp_path, source, 200, 0, 1, now=now, session=Session())
        assert bad_quality["belowMarketCap"] == 0
        assert "归一化质量门" in bad_quality["unavailable"][0]["reason"]
        assert low_cap["screenPolicyVersion"] == "a-share-screen-v5-market-cap-close-check"

    with monkeypatch.context() as context:
        context.setattr(screening, "parse_tencent_quotes", lambda _body: [
            SimpleNamespace(symbol="sz000001", code="000001", name="来源名称", total_market_cap_yi=201,
                            price=rows[-1]["close"], quote_time="20261030090000"),
        ])
        stale = screening.screen_batch(tmp_path, source, 200, 0, 1, now=now, session=Session())
        assert stale["unavailable"][0]["reason"] == "腾讯报价时间早于收盘，无法核对收盘市值"
        assert stale["unavailable"][0]["quoteTime"] == "20261030090000"
        assert stale["unavailable"][0]["quoteName"] == "来源名称"
        assert stale["unavailable"][0]["lastLocalDay"] == days[-1]

    retry = screening.screen_batch(tmp_path, source, 200, 0, 2, instrument_ids=list(reversed(ids)),
                                   now=now, session=Session())
    assert retry["requestedInstrumentIds"] == list(reversed(ids))
    assert retry["processed"] == 2 and retry["hasMore"] is False
    assert retry["matches"][0]["instrumentId"] == ids[0]
    with pytest.raises(FormulaError, match="不重复"):
        screening.screen_batch(tmp_path, source, 200, 0, 2, instrument_ids=[ids[0], ids[0]],
                               now=now, session=Session())
    with pytest.raises(FormulaError, match="不在当前本机目录"):
        screening.screen_batch(tmp_path, source, 200, 0, 2, instrument_ids=["CN.SZSE.STOCK.000003"],
                               now=now, session=Session())
    with pytest.raises(FormulaError, match="不重复"):
        screening.screen_batch(tmp_path, source, 200, 1, 2, instrument_ids=[ids[0]], now=now, session=Session())

    monkeypatch.setattr(screening, "load_action_snapshot", lambda: ActionSnapshot(
        "LOCAL_RECORDS_UNVERIFIED", "fixture", None, {ids[0]: (days[-3],)}))
    monkeypatch.setattr(screening, "_adjusted_closes", lambda _client, _symbol, _days, closes, events:
                        (closes, {"actionDays": events}))
    adjusted = screening.screen_batch(tmp_path, source, 200, 0, 2, now=now, session=Session())
    assert adjusted["matches"][0]["priceBasis"] == "TENCENT_QFQ_VERIFIED_AGAINST_TDX_RAW"
    assert adjusted["matches"][0]["adjustmentEvidence"]["actionDays"] == [days[-3]]

    def mismatch(*_args):
        raise ValueError("raw close disagreement")

    monkeypatch.setattr(screening, "_adjusted_closes", mismatch)
    unavailable = screening.screen_batch(tmp_path, source, 200, 0, 2, now=now, session=Session())
    assert not unavailable["matches"]
    assert "前复权价格缺失或未通过原价核对" in unavailable["unavailable"][0]["reason"]
