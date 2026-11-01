from hashlib import sha256
from types import SimpleNamespace

import duckdb
import pytest

from market_monitor.futures_settlement_backup import (
    QUALITY_STATUS, RATE_SCOPE, SOURCE_KIND, TABLE, current_backup_margins,
    import_backup, parse_minfutures, sync_current_backup,
)
from market_monitor.futures_settlement_parameters import ensure_schema as official_schema

DAY = "2026-09-30"
STAMP = "2026-10-07T05:00:00+00:00"
URL = "https://www.minfutures.com/main/a/20260929/85387.shtml"
RATES = {"IF": "12", "IH": "12", "IC": "12", "IM": "12", "TS": "0.5", "TF": "1.2", "T": "2", "TL": "3.5"}


def _raw(*, day="20260930", spec="12", exception=""):
    headers = ('<tr><th rowspan="2">交易所</th><th rowspan="2">品种</th>'
               '<th rowspan="2">品种代码</th><th colspan="2">交易所保证金（%）</th>'
               '<th colspan="2">公司保证金（%）</th><th rowspan="2">涨跌幅（%）</th>'
               '<th rowspan="2">特殊合约</th><th colspan="2">交易所保证金（%）</th>'
               '<th colspan="2">公司保证金（%）</th><th rowspan="2">涨跌幅（%）</th></tr>'
               '<tr>' + '<th>投机</th><th>套保</th>' * 4 + '</tr>')
    body = []
    for index, (product, rate) in enumerate(RATES.items()):
        venue = '<td rowspan="8">中金所</td>' if index == 0 else ""
        cells = [product, product, spec if product == "IF" else rate, "8", "19", "18", "10",
                 exception if product == "IF" else "", "", "", "", "", ""]
        body.append('<tr>' + venue + ''.join('<td>' + value + '</td>' for value in cells) + '</tr>')
    return ('<html><h1>' + day + '期货保证金率及涨跌停板</h1><table>'
            + headers + ''.join(body) + '</table></html>').encode()


def _parse(raw=None):
    return parse_minfutures(raw or _raw(), trading_day=DAY, source_url=URL, retrieved_at=STAMP)


def _contracts():
    return [{"trade_date": DAY, "exchange": "CFFEX", "contract_code": product + "2612",
             "product_code": product, "quality_status": "PASS"} for product in RATES]


def test_broker_exchange_speculative_percentage_not_hedge_or_company():
    rows = {row["product_code"]: row for row in _parse()}
    assert rows["IF"]["spec_long_rate"] == rows["IF"]["spec_short_rate"] == 0.12
    assert rows["TF"]["spec_long_rate"] == 0.012
    assert rows["TS"]["spec_long_rate"] == 0.005
    assert rows["TL"]["spec_long_rate"] == 0.035
    assert rows["IF"]["quality_status"] == QUALITY_STATUS
    assert rows["IF"]["source_kind"] == SOURCE_KIND
    assert rows["IF"]["margin_rate_scope"] == RATE_SCOPE
    assert rows["IF"]["payload_sha256"] == sha256(_raw()).hexdigest()


@pytest.mark.parametrize("raw", [_raw(day="20260929"), _raw(spec=""), _raw(spec="101"),
                                _raw(exception="IF2612"), _raw().replace("公司保证金（%）".encode(), "交易所保证金（%）".encode())])
def test_missing_date_rates_wrong_column_and_contract_exceptions_rejected(raw):
    with pytest.raises(ValueError):
        _parse(raw)


def test_zero_standard_is_not_missing():
    assert _parse(_raw(spec="0"))[1]["product_code"] == "IF"
    assert next(row for row in _parse(_raw(spec="0")) if row["product_code"] == "IF")["spec_long_rate"] == 0


def test_import_expands_only_official_same_day_futures_and_retains_official_gap():
    conn = duckdb.connect(":memory:")
    official_schema(conn)
    contracts = _contracts() + [{**_contracts()[0], "contract_code": "IF2612-C-4000"},
                                {**_contracts()[0], "contract_code": "IF2703", "quality_status": "FAILED"}]
    assert import_backup(conn, _parse(), contracts) == 8
    assert import_backup(conn, _parse(), contracts) == 8
    assert conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] == 8
    assert conn.execute("SELECT count(*) FROM futures_settlement_parameter_official").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM futures_settlement_parameter_coverage").fetchone()[0] == 0
    assert current_backup_margins(conn, DAY, "2026-10-08") == {}
    assert current_backup_margins(conn, "2026-09-29", DAY) == {}
    assert current_backup_margins(conn, DAY, DAY)["CFFEX", "TF2612"]["spec_short_rate"] == 0.012
    with pytest.raises(ValueError, match="another trading"):
        import_backup(conn, _parse(), [{**_contracts()[0], "trade_date": "2026-09-29"}])
    assert conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] == 8
    conn.close()


def test_complete_official_zero_rates_take_precedence_over_backup():
    conn = duckdb.connect(":memory:")
    official_schema(conn)
    import_backup(conn, _parse(), _contracts())
    conn.execute("""INSERT INTO futures_settlement_parameter_official (
        trade_date,exchange,contract_code,product_code,source_contract_code,
        spec_long_rate,spec_short_rate,standard_margin_rate,margin_policy,source_margin_unit,
        source_url,payload_sha256,retrieved_at,parser_version,quality_status,effective_from,effective_to)
        VALUES (?, 'CFFEX','IF2612','IF','IF2612',0,0,0,'MAX_SPEC','PERCENT',
            'http://www.cffex.com.cn/real.csv',?,?,'official-futures-settlement-v1','PASS',?,?)""",
        [DAY, "a" * 64, STAMP, DAY, DAY])
    result = current_backup_margins(conn, DAY, DAY)
    assert ("CFFEX", "IF2612") not in result
    assert len(result) == 7
    conn.execute("UPDATE futures_settlement_parameter_official SET spec_short_rate=NULL")
    assert ("CFFEX", "IF2612") in current_backup_margins(conn, DAY, DAY)
    conn.close()


def test_latest_cffex_publication_suppresses_current_backup_rate():
    from hashlib import sha256

    from market_monitor.futures_settlement_parameters import import_partition, parse_cffex

    conn = duckdb.connect(":memory:")
    official_schema(conn)
    import_backup(conn, _parse(), _contracts())
    raw = ("期货合约结算业务参数表（20260921）\n"
           "期货合约,合约多头保证金标准,合约空头保证金标准\n"
           "TF2612,1.2%,1.2%\n").encode("gb18030")
    url = "http://www.cffex.com.cn/sj/jscs/202609/21/20260921_1.csv"
    rows = parse_cffex(raw, trading_day="2026-09-21", source_url=url, retrieved_at=STAMP)
    import_partition(conn, rows, exchange="CFFEX", trade_date="2026-09-21",
                     source_url=url, payload_sha256=sha256(raw).hexdigest(), retrieved_at=STAMP)
    assert ("CFFEX", "TF2612") not in current_backup_margins(conn, DAY, DAY)
    conn.close()


def test_sync_exact_title_cached_original_and_no_historical_requests(tmp_path):
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE futures_contract_daily_official (trade_date VARCHAR,exchange VARCHAR,"
                 "contract_code VARCHAR,product_code VARCHAR,quality_status VARCHAR)")
    conn.executemany("INSERT INTO futures_contract_daily_official VALUES (?,?,?,?,?)",
                     [[row[key] for key in ("trade_date", "exchange", "contract_code", "product_code", "quality_status")]
                      for row in _contracts()])
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if url == URL:
            return SimpleNamespace(status_code=200, content=_raw())
        return SimpleNamespace(status_code=200, content=(
            '<a href="/main/a/20260929/85387.shtml">20260930期货保证金率及涨跌停板</a>'
            '<a href="/main/a/20260930/85398.shtml">20261008期货保证金率及涨跌停板</a>').encode())

    client = SimpleNamespace(get=get)
    result = sync_current_backup(tmp_path, conn, DAY, session=client, latest_completed_day=DAY)
    assert result["status"] == QUALITY_STATUS and result["record_count"] == 8
    assert len(calls) == 2
    sync_current_backup(tmp_path, conn, DAY, session=client, latest_completed_day=DAY)
    assert len(calls) == 2
    result = sync_current_backup(tmp_path, conn, "2026-09-29", session=client, latest_completed_day=DAY)
    assert result["status"] == "SKIPPED_NON_CURRENT_DAY" and len(calls) == 2
    conn.close()
