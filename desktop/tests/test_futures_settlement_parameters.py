from hashlib import sha256
import json

import duckdb
import pytest

from market_monitor.futures_settlement_parameters import (
    PARSER_VERSION, fetch_settlement_parameters, import_partition,
    import_settlement_bronze, parse_cffex, parse_czce, parse_dce, parse_gfex,
    parse_shfe, read_settlement_parameters,
)


STAMP = "2026-10-07T00:00:00+00:00"
DAY = "2026-09-29"


def _shfe_raw(long=0.11, short=0.12, *, day="20260929", code="cu2610", hedge=0.05):
    return json.dumps({"report_date": day, "o_cursor": [{"INSTRUMENTID": code,
        "SPECLONGMARGINRATIO": long, "SPECSHORTMARGINRATIO": short,
        "HEDGLONGMARGINRATIO": hedge, "HEDGSHORTMARGINRATIO": hedge,
        "SETTLEMENTPRICE": 100000}]}).encode()


def _shfe(raw=None):
    return parse_shfe(raw or _shfe_raw(), trading_day=DAY,
                      source_url="https://www.shfe.com.cn/data/tradedata/future/dailydata/js20260929.dat",
                      retrieved_at=STAMP)


def test_speculation_decimal_and_unequal_sides_use_max_not_hedge_or_double():
    row = _shfe()[0]
    assert row["spec_long_rate"] == 0.11
    assert row["spec_short_rate"] == row["standard_margin_rate"] == 0.12
    assert row["margin_policy"] == "MAX_SPEC"
    assert row["source_margin_unit"] == "DECIMAL"
    assert row["payload_sha256"] == sha256(_shfe_raw()).hexdigest()


def test_missing_side_stays_missing_but_real_zero_is_preserved():
    missing = _shfe(_shfe_raw(short=""))[0]
    assert missing["standard_margin_rate"] is None and missing["quality_status"] == "PARTIAL"
    zero = _shfe(_shfe_raw(long=0, short=0))[0]
    assert zero["standard_margin_rate"] == 0 and zero["quality_status"] == "PASS"


@pytest.mark.parametrize("raw", [_shfe_raw(day="20260928"), b"<html>challenge</html>",
                                _shfe_raw(long=11), _shfe_raw(long=-0.01)])
def test_wrong_day_html_or_wrong_scale_rejected(raw):
    with pytest.raises(ValueError):
        _shfe(raw)


def test_ine_is_source_isolated_and_option_rows_not_accepted():
    raw = _shfe_raw(code="sc2610")
    rows = parse_shfe(raw, exchange="INE", trading_day=DAY,
                      source_url="https://www.ine.cn/data/tradedata/future/dailydata/js20260929.dat",
                      retrieved_at=STAMP)
    assert rows[0]["exchange"] == "INE"
    with pytest.raises(ValueError, match="no dated"):
        _shfe(raw)


def test_gfex_requested_date_and_spec_sell_are_used():
    raw = json.dumps({"code": "0", "param": {"trade_date": ["20260929"]},
                      "data": [{"contractId": "lc2610", "specBuyRate": 0.17,
                                "specSellRate": 0.18, "hedgeBuyRate": 0.16,
                                "hedgeSellRate": 0.16},
                               {"contractId": "lc2610-C-90000", "specBuyRate": 0.1}]}).encode()
    kwargs = dict(trading_day=DAY, source_url="http://www.gfex.com.cn/u/interfacesWebTiFutAndOptSettle/loadList", retrieved_at=STAMP)
    rows = parse_gfex(raw, **kwargs)
    assert len(rows) == 1 and rows[0]["standard_margin_rate"] == 0.18
    with pytest.raises(ValueError, match="date"):
        parse_gfex(raw.replace(b"20260929", b"20260928"), **kwargs)


@pytest.mark.parametrize("encoding", ["utf-8", "gb18030"])
def test_czce_percentage_and_legacy_contract_normalize(encoding):
    raw = ("郑州商品交易所期货结算参数表(2026-09-29)\n"
           "合约代码|当日结算价|交易保证金率(%)|涨跌停板(%)\n"
           "AP610|7,871.00|10|±8\nAP611|7,316.00|9|±8\n").encode(encoding)
    rows = parse_czce(raw, trading_day=DAY,
                      source_url="https://www.czce.com.cn/cn/DFSStaticFiles/Future/2026/20260929/FutureDataClearParams.txt",
                      retrieved_at=STAMP)
    assert rows[0]["contract_code"] == "AP2610"
    assert rows[0]["standard_margin_rate"] == 0.1 and rows[1]["standard_margin_rate"] == 0.09


def _cffex_raw():
    return ("期货合约结算业务参数表（20260921）\n"
            "期货合约,合约多头保证金标准,合约空头保证金标准,交易手续费标准\n"
            "IC2610,12.00%,13.00%,万分之0.23\n"
            "IO2610-C-4000,0%,0%,2\n").encode("gb18030")


def test_cffex_parser_keeps_original_publication_date_and_explicit_interval_gate():
    kwargs = dict(source_url="http://www.cffex.com.cn/sj/jscs/202609/21/20260921_1.csv", retrieved_at=STAMP)
    rows = parse_cffex(_cffex_raw(), trading_day="2026-09-21", **kwargs)
    assert len(rows) == 1 and rows[0]["standard_margin_rate"] == 0.13
    with pytest.raises(ValueError, match="interval"):
        parse_cffex(_cffex_raw(), trading_day=DAY, **kwargs)
    declaration = "本通知明确自2026年9月21日起至2026年9月30日按本表执行".encode()
    evidence = {"raw": declaration, "explicit_declaration_verified": True,
                "effective_from": "2026-09-21", "effective_to": "2026-09-30",
                "source_url": "http://www.cffex.com.cn/jystz/20260920/notice.html",
                "payload_sha256": sha256(declaration).hexdigest()}
    row = parse_cffex(_cffex_raw(), trading_day=DAY, interval_evidence=evidence, **kwargs)[0]
    assert row["effective_to"] == "2026-09-30" and row["interval_payload_sha256"]


def test_cffex_read_uses_latest_verified_publication_without_older_contract_fallback(tmp_path):
    connection = duckdb.connect(str(tmp_path / "catalog.duckdb"))
    try:
        for compact, values in (
            ("20260914", "IF2610,10%,10%\nIF2611,11%,11%\n"),
            ("20260921", "IF2610,12%,12%\n"),
        ):
            day = f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
            raw = (f"期货合约结算业务参数表（{compact}）\n"
                   "期货合约,合约多头保证金标准,合约空头保证金标准\n"
                   + values).encode("gb18030")
            url = f"http://www.cffex.com.cn/sj/jscs/{compact[:6]}/{compact[6:]}/{compact}_1.csv"
            rows = parse_cffex(raw, trading_day=day, source_url=url, retrieved_at=STAMP)
            import_partition(connection, rows, exchange="CFFEX", trade_date=day,
                             source_url=url, payload_sha256=sha256(raw).hexdigest(), retrieved_at=STAMP)
    finally:
        connection.close()

    assert read_settlement_parameters(tmp_path, "2026-09-13", exchange="CFFEX")["rows"] == []
    before = read_settlement_parameters(tmp_path, "2026-09-20", exchange="CFFEX", contract_code="IF2610")
    assert before["rows"][0]["spec_long_rate"] == .10
    assert before["cffexPublication"] == {
        "selection": "LATEST_VERIFIED_PUBLICATION", "publishedOn": "2026-09-14", "carriedForward": True,
    }
    after = read_settlement_parameters(tmp_path, "2026-10-16", exchange="CFFEX", contract_code="IF2610")
    assert after["rows"][0]["spec_long_rate"] == .12
    assert after["cffexPublication"]["publishedOn"] == "2026-09-21"
    missing = read_settlement_parameters(tmp_path, "2026-10-16", exchange="CFFEX", contract_code="IF2611")
    assert missing["rows"] == []
    assert missing["cffexPublication"]["publishedOn"] == "2026-09-21"
    connection = duckdb.connect(str(tmp_path / "catalog.duckdb"))
    try:
        connection.execute(
            "UPDATE futures_settlement_parameter_coverage SET status='PARTIAL' "
            "WHERE exchange='CFFEX' AND trade_date='2026-09-21'",
        )
    finally:
        connection.close()
    rejected = read_settlement_parameters(tmp_path, "2026-10-16", exchange="CFFEX", contract_code="IF2610")
    assert rejected["cffexPublication"]["publishedOn"] is None
    assert rejected["rows"] == []


def test_dce_average_month_contract_and_declared_unit():
    raw = json.dumps({"code": 200, "success": True, "tradeDate": "20260929", "data": [{"contractId": "l2610F",
                      "specBuyRate": 8, "specSellRate": 9}]}).encode()
    kwargs = dict(trading_day=DAY, source_url="http://www.dce.com.cn/dce/channel/list/181.html", retrieved_at=STAMP)
    with pytest.raises(ValueError, match="unit"):
        parse_dce(raw, **kwargs)
    row = parse_dce(raw, margin_unit="PERCENT", **kwargs)[0]
    assert row["contract_code"] == "L-F2610" and row["standard_margin_rate"] == 0.09


def test_dce_undated_response_requires_actual_network_body_evidence_and_preserves_raw(tmp_path):
    url = "http://www.dce.com.cn/dcereport/publicweb/tradepara/futAndOptSettle"
    raw = json.dumps({"code": 200, "success": True, "data": [{"contractId": "a2611",
                     "specBuyRate": "0.1", "specSellRate": "0.12", "clearPrice": "5236"}]}).encode()
    post = json.dumps({"varietyId": "all", "tradeDate": "20260929", "tradeType": "1", "lang": "zh"})
    evidence = {"method": "POST", "source_url": url, "post_data": post,
                "post_data_sha256": sha256(post.encode()).hexdigest(), "trade_date_verified": True}
    kwargs = dict(trading_day=DAY, source_url=url, margin_unit="DECIMAL", retrieved_at=STAMP)
    with pytest.raises(ValueError, match="Network"):
        parse_dce(raw, **kwargs)
    row = parse_dce(raw, request_evidence=evidence, **kwargs)[0]
    assert row["source_date_basis"] == "VERIFIED_REQUEST_BODY"
    assert row["payload_sha256"] == sha256(raw).hexdigest()
    with pytest.raises(ValueError, match="SHA"):
        parse_dce(raw, request_evidence={**evidence, "post_data": post.replace("20260929", "20260928")}, **kwargs)
    result = fetch_settlement_parameters(tmp_path, exchange="DCE", trading_day=DAY,
        dce_fetcher=lambda day: {"raw": raw, "source_url": url, "margin_unit": "DECIMAL", "request_evidence": evidence})
    assert result["status"] == "PASS"
    assert fetch_settlement_parameters(tmp_path, exchange="DCE", trading_day=DAY)["cached"] is True
    connection = duckdb.connect(":memory:")
    try:
        imported = import_settlement_bronze(tmp_path, connection)
        assert imported["recordCount"] == 1
        assert connection.execute("SELECT request_payload_sha256 FROM futures_settlement_parameter_official").fetchone()[0] == evidence["post_data_sha256"]
    finally:
        connection.close()


def test_atomic_replace_idempotent_and_failure_preserves_earlier_verified_rows(tmp_path):
    connection = duckdb.connect(str(tmp_path / "catalog.duckdb"))
    rows = _shfe()
    kwargs = dict(exchange="SHFE", trade_date=DAY, source_url=rows[0]["source_url"],
                  payload_sha256=rows[0]["payload_sha256"], retrieved_at=STAMP)
    try:
        assert import_partition(connection, rows, **kwargs) == 1
        assert import_partition(connection, rows, **kwargs) == 0
        assert connection.execute("SELECT count(*) FROM futures_settlement_parameter_official").fetchone()[0] == 1
        with pytest.raises(duckdb.ConstraintException):
            import_partition(connection, [rows[0], rows[0]], **kwargs)
        assert connection.execute("SELECT count(*) FROM futures_settlement_parameter_official").fetchone()[0] == 1
        with pytest.raises(ValueError, match="interval"):
            import_partition(connection, [{**rows[0], "effective_to": "2026-09-30"}], **kwargs)
        import_partition(connection, [], **kwargs, status="DEFERRED", error="HTTP_412")
    finally:
        connection.close()
    result = read_settlement_parameters(tmp_path, DAY, exchange="SHFE", contract_code="CU2610")
    assert len(result["rows"]) == 1
    assert result["coverage"][0]["status"] == "DEFERRED"


def test_explicit_cffex_interval_can_start_after_publication_and_resolve_a_later_day(tmp_path):
    raw = "期货合约结算业务参数表（20260921）\n期货合约,合约多头保证金标准,合约空头保证金标准\nIF2610,12%,12%\n".encode("gb18030")
    url = "http://www.cffex.com.cn/sj/jscs/202609/21/20260921_1.csv"
    rows = parse_cffex(raw, trading_day="2026-09-21", source_url=url, retrieved_at=STAMP)
    rows[0].update(effective_from="2026-09-22", effective_to="2026-09-30", published_on="2026-09-21",
                   interval_source_url="http://www.cffex.com.cn/jystz/synthetic-notice.html",
                   interval_payload_sha256="b" * 64)
    connection = duckdb.connect(str(tmp_path / "catalog.duckdb"))
    import_partition(connection, rows, exchange="CFFEX", trade_date="2026-09-21",
                     source_url=url, payload_sha256=sha256(raw).hexdigest(), retrieved_at=STAMP)
    connection.close()
    result = read_settlement_parameters(tmp_path, "2026-09-29", exchange="CFFEX", contract_code="IF2610")
    assert result["rows"][0]["trade_date"] == "2026-09-21"
    assert result["rows"][0]["published_on"] == "2026-09-21"
    assert result["rows"][0]["standard_margin_rate"] == .12
    assert read_settlement_parameters(tmp_path, "2026-09-21")["rows"] == []
    assert read_settlement_parameters(tmp_path, "2026-10-01")["rows"] == []


class _FakeResponse:
    status_code = 200
    content = _shfe_raw()


class _FakeSession:
    def __init__(self):
        self.calls = 0

    def request(self, *args, **kwargs):
        self.calls += 1
        return _FakeResponse()


def test_original_sha_cache_upgrade_and_tamper_gate(tmp_path):
    session = _FakeSession()
    kwargs = dict(exchange="SHFE", trading_day=DAY, session=session)
    first = fetch_settlement_parameters(tmp_path, **kwargs)
    second = fetch_settlement_parameters(tmp_path, **kwargs)
    assert first["status"] == "PASS" and second["cached"] and session.calls == 1
    directory = tmp_path / "bronze/official_futures_settlement/SHFE" / DAY
    metadata = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    metadata["parser_version"] = "old"
    (directory / "result.json").write_text(json.dumps(metadata), encoding="utf-8")
    assert fetch_settlement_parameters(tmp_path, **kwargs)["rows"][0]["parser_version"] == PARSER_VERSION
    connection = duckdb.connect(":memory:")
    try:
        report = import_settlement_bronze(tmp_path, connection)
        assert report["recordCount"] == 1
        (directory / first["raw_file"]).write_bytes(b"tampered")
        assert import_settlement_bronze(tmp_path, connection)["failed"] == 1
    finally:
        connection.close()


def test_dce_without_verified_browser_request_is_deferred_and_has_no_facts(tmp_path):
    result = fetch_settlement_parameters(tmp_path, exchange="DCE", trading_day=DAY)
    assert result["status"] == "DEFERRED" and result["rows"] == []
    assert result["payload_sha256"] is None


def test_incremental_sync_reuses_verified_original_and_imported_partition(tmp_path, monkeypatch):
    from datetime import date

    import market_monitor.futures_settlement_parameters as module
    from market_monitor.futures_calendar import sync_futures_trading_calendar

    class Session(_FakeSession):
        def close(self):
            pass

    session = Session()
    first = fetch_settlement_parameters(tmp_path, exchange="SHFE", trading_day=DAY, session=session)
    connection = duckdb.connect(str(tmp_path / "catalog.duckdb"))
    import_partition(connection, first["rows"], exchange="SHFE", trade_date=DAY,
                     source_url=first["source_url"], payload_sha256=first["payload_sha256"],
                     retrieved_at=first["retrieved_at"])
    connection.close()
    sync_futures_trading_calendar(tmp_path, fetcher=lambda: [(date(2026, 9, 29),)])
    monkeypatch.setattr(module, "fetch_settlement_parameters", lambda *args, **kwargs:
                        pytest.fail("verified historical partition must not be reparsed or downloaded"))
    progress = []
    module.sync_settlement_parameters(tmp_path, start_day=DAY, end_day=DAY, exchanges=["SHFE"],
                                      bronze_only=False, session_factory=Session, progress=progress.append)
    assert progress[0]["already_imported"] is True
    assert read_settlement_parameters(tmp_path, DAY)["coverage"][0]["status"] == "PASS"
