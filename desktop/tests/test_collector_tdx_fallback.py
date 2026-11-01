from market_monitor import collector
from market_monitor import full_market
import struct
import pytdx.hq
import pytest


@pytest.fixture(autouse=True)
def clear_configured_servers(monkeypatch):
    monkeypatch.delenv("TDX_SERVERS", raising=False)


def test_tdx_partial_server_response_tries_remaining_symbols_only(monkeypatch):
    calls = []
    class Api:
        def __init__(self, **kwargs): assert kwargs == {"raise_exception": True}
        def connect(self, host, port, time_out):
            self.host = host
            return True
        def get_security_bars(self, category, market, code, start, count):
            calls.append((self.host, code))
            return [] if self.host == "first" and code == "000002" else [{"code": code}]
        def disconnect(self): pass
    monkeypatch.setattr(pytdx.hq, "TdxHq_API", Api)
    monkeypatch.setattr(collector, "_TDX_SERVERS", (("first", 1), ("second", 2)))
    monkeypatch.setattr(collector, "_tdx_bar", lambda row, *_args: {"code": row["code"]})
    bars, errors = collector._tdx_bars("1d", [("000001", 0, "A"), ("000002", 0, "B")], 2)
    assert bars == [{"code": "000001"}, {"code": "000002"}]
    assert errors == [] and calls == [("first", "000001"), ("first", "000002"), ("second", "000002")]


def test_tdx_unresolved_symbols_remain_partial_and_are_not_fabricated(monkeypatch):
    class Api:
        def __init__(self, **kwargs): assert kwargs == {"raise_exception": True}
        def connect(self, host, port, time_out): return True
        def get_security_bars(self, category, market, code, start, count):
            return [{"code": code}] if code == "000001" else []
        def disconnect(self): pass
    monkeypatch.setattr(pytdx.hq, "TdxHq_API", Api)
    monkeypatch.setattr(collector, "_TDX_SERVERS", (("first", 1), ("second", 2)))
    monkeypatch.setattr(collector, "_tdx_bar", lambda row, *_args: {"code": row["code"]})
    bars, errors = collector._tdx_bars("1d", [("000001", 0, "A"), ("000002", 0, "B")], 2)
    assert bars == [{"code": "000001"}] and errors == ["000002:empty"]


def test_tdx_index_bad_batch_is_discarded_before_other_server_retry(monkeypatch):
    class Api:
        def __init__(self, **kwargs): assert kwargs == {"raise_exception": True}
        def connect(self, host, port, time_out): self.host = host; return True
        def get_index_bars(self, category, market, code, start, count):
            return [{"value": 1}, {"bad": True}] if self.host == "first" else [{"value": 2}]
        def disconnect(self): pass
    def convert(row, *_args):
        return {"value": row["value"]}
    monkeypatch.setattr(pytdx.hq, "TdxHq_API", Api)
    monkeypatch.setattr(collector, "_TDX_SERVERS", (("first", 1), ("second", 2)))
    monkeypatch.setattr(collector, "_tdx_bar", convert)
    bars, errors = collector._tdx_bars("1d", [("000001", 1, "指数")], 2, asset_type="INDEX")
    assert bars == [{"value": 2}] and errors == []


def test_tdx_uses_configured_servers_and_reports_protocol_exception(monkeypatch):
    calls = []
    class Api:
        def __init__(self, **kwargs): assert kwargs == {"raise_exception": True}
        def connect(self, host, port, time_out):
            calls.append((host, port))
            raise ConnectionError("synthetic")
        def disconnect(self): pass
    monkeypatch.setattr(pytdx.hq, "TdxHq_API", Api)
    monkeypatch.setenv("TDX_SERVERS", "configured.example:7710")
    bars, errors = collector._tdx_bars("1d", [("000001", 1, "指数")], 2, asset_type="INDEX")
    assert bars == [] and calls == [("configured.example", 7710)]
    assert errors == ["configured.example:7710:ConnectionError"]


def test_tdx_all_protocol_errors_are_not_reported_as_empty(monkeypatch):
    class Api:
        def __init__(self, **kwargs): pass
        def connect(self, host, port, time_out): return True
        def get_index_bars(self, *_args): raise RuntimeError("synthetic protocol failure")
        def disconnect(self): pass
    monkeypatch.setattr(pytdx.hq, "TdxHq_API", Api)
    monkeypatch.setattr(collector, "_TDX_SERVERS", (("first", 1),))
    bars, errors = collector._tdx_bars("1d", [("000001", 1, "指数")], 2, asset_type="INDEX")
    assert bars == [] and errors == ["first:1:RuntimeError"]


def test_intraday_bars_are_not_fabricated_or_requested_before_time_basis_verified(monkeypatch):
    calls = []
    def fetch(period, specs, count, **_kwargs):
        calls.append(period)
        return [], ["synthetic:unavailable"]
    monkeypatch.setattr(collector, "_tdx_bars", fetch)
    result = collector._collect_cn_stock_bars(1)
    assert calls == ["1d"] and result.status == "PARTIAL_FAILURE"
    assert "INTRADAY_TIME_UNVERIFIED" in result.detail
    assert not result.persist.bar_writes
    with pytest.raises(ValueError, match="intraday timestamp semantics"):
        collector._tdx_bar({"year": 2026, "month": 10, "day": 30, "hour": 10, "minute": 0},
                           "CN.SSE.STOCK.600519", "样本", "30m")


def test_daily_bar_helper_rejects_intraday_period():
    with pytest.raises(ValueError, match="timestamp semantics"):
        collector._daily_bar("CN.SSE.STOCK.600519", "样本", "2026-10-30", open_=1, high=1,
                             low=1, close=1, period="30m")


def test_tdx_invalid_binary_response_is_classified_without_raw_payload(monkeypatch):
    class Api:
        def __init__(self, **kwargs): pass
        def connect(self, host, port, time_out): return True
        def get_index_bars(self, *_args):
            error = RuntimeError("synthetic raw data")
            error.original_exception = struct.error("synthetic private payload")
            raise error
        def disconnect(self): pass
    monkeypatch.setattr(pytdx.hq, "TdxHq_API", Api)
    monkeypatch.setattr(collector, "_TDX_SERVERS", (("first", 1),))
    bars, errors = collector._tdx_bars("1d", [("000001", 1, "指数")], 2, asset_type="INDEX")
    assert bars == [] and errors == ["first:1:TDX_INVALID_BINARY_RESPONSE"]


def test_full_market_stock_and_etf_fallback_use_config_and_retry_failure(monkeypatch):
    calls = []
    class Api:
        def __init__(self, **kwargs): assert kwargs == {"raise_exception": True}
        def connect(self, host, port, time_out): self.host = host; calls.append((host, port)); return True
        def get_security_bars(self, *_args):
            if self.host == "bad.example": raise RuntimeError("synthetic")
            return [{"year": 2026, "month": 10, "day": 30, "close": 10}]
        def disconnect(self): pass
    monkeypatch.setattr(pytdx.hq, "TdxHq_API", Api)
    monkeypatch.setenv("TDX_SERVERS", "bad.example:7710,good.example:7711")
    stock = full_market._fetch_cn_from_tdx("600519")
    etf = full_market._fetch_etf_from_tdx("ETF", "510300", "2026-01-01")
    assert list(stock)[0]["date"] == list(etf)[0]["date"] == "2026-10-30"
    assert calls == [("bad.example", 7710), ("good.example", 7711)] * 2


def test_full_market_etf_failure_reports_safe_protocol_type(monkeypatch):
    class Api:
        def __init__(self, **kwargs): pass
        def connect(self, host, port, time_out): return True
        def get_security_bars(self, *_args):
            error = RuntimeError("private synthetic payload")
            error.original_exception = struct.error("private synthetic bytes")
            raise error
        def disconnect(self): pass
    monkeypatch.setattr(pytdx.hq, "TdxHq_API", Api)
    monkeypatch.setenv("TDX_SERVERS", "configured.example:7710")
    with pytest.raises(RuntimeError, match="TDX_INVALID_BINARY_RESPONSE") as error:
        full_market._fetch_etf_from_tdx("ETF", "510300", "2026-01-01")
    assert "private synthetic" not in str(error.value)
