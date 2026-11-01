from hashlib import sha256
import json

import duckdb
import pytest

from market_monitor.futures_dce_settlement_browser import DceSettlementBrowser, SOURCE
from market_monitor.futures_settlement_parameters import (
    SettlementSourceError, import_partition, parse_dce,
)


@pytest.mark.parametrize("mime", ["application/json", "application/octet-stream;charset=UTF-8"])
def test_browser_capture_requires_actual_dated_network_body_and_original_bytes(mime: str):
    browser = object.__new__(DceSettlementBrowser)
    browser.events = []
    raw = b'{"success":true,"code":200,"data":[]}'

    def command(method, params):
        if method == "Runtime.evaluate":
            body = json.dumps({"varietyId": "all", "tradeDate": "20260929", "tradeType": "1", "lang": "zh"}, separators=(",", ":"))
            browser.events.append({"method": "Network.requestWillBeSent", "params": {
                "requestId": "synthetic-request", "request": {"url": SOURCE, "method": "POST", "postData": body}}})
            return {"result": {"value": {"status": 200, "mime": mime}}}
        assert params == {"requestId": "synthetic-request"}
        return {"body": raw.decode(), "base64Encoded": False}

    browser.command = command
    result = browser.capture("2026-09-29")
    assert result["raw"] == raw
    evidence = result["request_evidence"]
    assert evidence["post_data_sha256"] == sha256(evidence["post_data"].encode()).hexdigest()
    with pytest.raises(SettlementSourceError, match="DATE_REQUEST_NOT_OBSERVED"):
        browser.capture("2026-09-28")


def test_dated_request_cannot_import_prices_from_another_official_day():
    connection = duckdb.connect(":memory:")
    connection.execute("CREATE TABLE futures_contract_daily_official "
                       "(trade_date VARCHAR,exchange VARCHAR,contract_code VARCHAR,settlement DOUBLE,quality_status VARCHAR)")
    connection.execute("INSERT INTO futures_contract_daily_official VALUES ('2026-09-29','DCE','M2611',3000,'PASS')")
    body = json.dumps({"varietyId": "all", "tradeDate": "20260929", "tradeType": "1", "lang": "zh"})
    raw = json.dumps({"success": True, "code": 200, "data": [{
        "contractId": "m2611", "specBuyRate": .1, "specSellRate": .12, "clearPrice": 2900}]}).encode()
    stamp = "2026-10-07T00:00:00+00:00"
    rows = parse_dce(raw, trading_day="2026-09-29", source_url=SOURCE, retrieved_at=stamp,
                     margin_unit="DECIMAL", request_evidence={"trade_date_verified": True, "method": "POST",
                        "source_url": SOURCE, "post_data": body, "post_data_sha256": sha256(body.encode()).hexdigest()})
    with pytest.raises(ValueError, match="date price differs"):
        import_partition(connection, rows, exchange="DCE", trade_date="2026-09-29",
                         source_url=SOURCE, payload_sha256=sha256(raw).hexdigest(), retrieved_at=stamp)
    assert connection.execute("SELECT count(*) FROM futures_settlement_parameter_official").fetchone()[0] == 0
    connection.close()
