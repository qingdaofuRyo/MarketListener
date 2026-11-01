import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from market_monitor import bls_payroll, collector


def payload(rows):
    return {"status": "REQUEST_SUCCEEDED", "Results": {"series": [{"seriesID": bls_payroll.SERIES, "data": rows}]}}


def row(year, month, value):
    return {"year": str(year), "period": f"M{month:02}", "value": str(value)}


def test_bls_changes_use_adjacent_levels_and_preserve_reference_month():
    data = payload([row(2026, 2, 102.4), row(2025, 12, 100), row(2026, 1, 101.5), row(2026, 4, 120)])
    assert bls_payroll.parse_payroll_changes(data) == [("2026-01", 1.5), ("2026-02", 0.9)]


@pytest.mark.parametrize("bad", [
    {}, {"status": "REQUEST_SUCCEEDED", "Results": []},
    payload([row(2026, 1, 100), row(2026, 1, 101)]),
    payload([row(2026, 1, "NaN"), row(2026, 2, 10)]),
    payload([row(2026, 1, 100), row(2026, 3, 101)]),
])
def test_bls_rejects_invalid_or_nonadjacent_data(bad):
    with pytest.raises(ValueError):
        bls_payroll.parse_payroll_changes(bad)


def test_bls_fetch_exercises_http_success_and_failure(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/failed":
                self.send_response(503); self.end_headers(); return
            body = json.dumps(payload([row(2026, 1, 100), row(2026, 2, 102)])).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_args): pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        monkeypatch.setattr(bls_payroll, "URL", base + "/ok")
        changes, digest = bls_payroll.fetch_payroll_changes()
        assert changes == [("2026-02", 2.0)] and len(digest) == 64
        monkeypatch.setattr(bls_payroll, "URL", base + "/failed")
        with pytest.raises(requests.HTTPError):
            bls_payroll.fetch_payroll_changes()
        assert collector._collect_bls_payroll().status == "FAILED"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_bls_collector_keeps_new_series_separate(monkeypatch):
    monkeypatch.setattr(bls_payroll, "fetch_payroll_changes", lambda: ([("2026-09", 29.0)], "a" * 64))
    result = collector._collect_bls_payroll()
    assert result.status == "PASS" and result.rows == 1
    metric = result.persist.gold_metrics[0]
    assert metric["instrument_id"] == "US_NONFARM_BLS_LATEST" and metric["trading_date"] == "2026-09"
    assert "CES0000000001" in metric["calculation_method"] and "a" * 64 in metric["calculation_method"]
