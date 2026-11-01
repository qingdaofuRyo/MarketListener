from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


@pytest.fixture
def probe():
    path = Path(__file__).resolve().parents[2] / "scripts" / "probe_ths_etf_snapshot.py"
    spec = importlib.util.spec_from_file_location("ths_etf_official_probe", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_key_does_not_contact_provider(probe, monkeypatch, capsys):
    monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["probe"])
    monkeypatch.setattr(probe.requests, "Session", lambda: pytest.fail("unexpected network"))
    assert probe.main() == 2
    assert json.loads(capsys.readouterr().out)["status"] == "MISSING_API_KEY"


def test_response_requires_matching_symbol_and_never_logs_key(probe, monkeypatch, capsys):
    monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "synthetic-test-key")
    monkeypatch.setattr(sys, "argv", ["probe"])

    class Response:
        status_code = 200

        def iter_content(self, _size):
            yield json.dumps({"code": 0, "data": {"timestamp": 1000,
                "item": [{"thscode": "510300.SH", "last_price": 1.2}]}}).encode()

        def close(self):
            pass

    class Session:
        trust_env = True

        def get(self, url, **kwargs):
            assert url == probe.URL and kwargs["headers"]["X-api-key"] == "synthetic-test-key"
            assert self.trust_env is False and kwargs["allow_redirects"] is False
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(probe.requests, "Session", Session)
    assert probe.main() == 0
    output = capsys.readouterr().out
    assert "synthetic-test-key" not in output
    assert json.loads(output)["status"] == "UNVERIFIED"
