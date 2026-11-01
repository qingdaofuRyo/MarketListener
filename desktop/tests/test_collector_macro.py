from __future__ import annotations

from typing import Any
from datetime import date

from market_monitor import collector


class _Frame:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def to_dict(self, *, orient: str) -> list[dict[str, Any]]:
        assert orient == "records"
        return self._rows


class _MacroApi:
    def macro_china_money_supply(self) -> _Frame:
        return _Frame([{
            "月份": "2026年7月份",
            "流通中的现金(M0)-同比增长": "11.8",
            "货币(M1)-同比增长": "5.6",
            "货币和准货币(M2)-同比增长": "8.8",
        }])

    def macro_china_cpi_yearly(self) -> _Frame:
        return _Frame([])

    def macro_china_imports_yoy(self) -> _Frame:
        return _Frame([{"日期": "2026-08-07", "今值": "4.1"}])

    def macro_china_exports_yoy(self) -> _Frame:
        return _Frame([{"日期": "2026-08-07", "今值": "7.2"}])

    def macro_china_trade_balance(self) -> _Frame:
        return _Frame([{"日期": "2026-08-07", "今值": "982.4"}])

    def macro_china_fx_reserves_yearly(self) -> _Frame:
        return _Frame([{"日期": "2026-08-07", "今值": "32920"}])

    def macro_china_consumer_goods_retail(self) -> _Frame:
        return _Frame([{
            "月份": "2026年7月份",
            "同比增长": "0.6",
            "环比增长": "-8.5",
        }])

    def macro_china_society_electricity(self) -> _Frame:
        return _Frame([{
            "统计时间": "2026.7",
            "全社会用电量": "613990000",
        }])

    def macro_china_ppi_yearly(self) -> _Frame:
        return _Frame([])

    def macro_china_pmi(self) -> _Frame:
        return _Frame([])

    def macro_china_cx_pmi_yearly(self) -> _Frame:
        return _Frame([])

    def macro_china_cx_services_pmi_yearly(self) -> _Frame:
        return _Frame([])

    def repo_rate_query(self, *, symbol: str) -> _Frame:
        assert symbol == "银银间回购定盘利率"
        return _Frame([])

    def bond_zh_us_rate(self) -> _Frame:
        return _Frame([])

    def macro_bank_usa_interest_rate(self) -> _Frame:
        return _Frame([])

    def macro_usa_non_farm(self) -> _Frame:
        return _Frame([{"日期": "2026-08-07", "今值": "7.3"}])


def test_macro_collection_persists_registered_m0_yoy(monkeypatch) -> None:
    monkeypatch.setattr(collector, "_ak", lambda: _MacroApi())
    monkeypatch.setattr(collector, "_fetch_bea_us_imports_rows", lambda: [("2026-07", 395_280.0)])
    monkeypatch.setattr(collector, "_fetch_bea_core_pce_final_rows", lambda: [("2026-Q1", 4.4)])

    result = collector._collect_macro()

    assert result.status == "PASS"
    assert result.rows == 13
    values = {row["instrument_id"]: row["value"] for row in result.persist.gold_metrics}
    assert values == {
        "M0_MONEY_SUPPLY": 11.8,
        "M1_MONEY_SUPPLY": 5.6,
        "M2_MONEY_SUPPLY": 8.8,
        "CN_IMPORT_USD_YOY": 4.1,
        "CN_EXPORT_USD_YOY": 7.2,
        "CN_TRADE_BALANCE_USD": 982.4,
        "CN_FOREX_RESERVES": 32920.0,
        "CN_RETAIL_SALES_YOY": 0.6,
        "CN_RETAIL_SALES_MOM": -8.5,
        "CN_ELECTRICITY_CONSUMPTION": 61399.0,
        "US_NONFARM_PAYROLLS_SA": 73.0,
        "US_IMPORTS_SA": 395280.0,
        "US_CORE_PCE_QOQ_SAAR_FINAL": 4.4,
    }


def test_macro_missing_adapter_and_invalid_rows_do_not_discard_other_sources(monkeypatch) -> None:
    class PartialApi(_MacroApi):
        def __getattribute__(self, name):
            if name == "macro_china_society_electricity":
                raise AttributeError("synthetic missing adapter")
            return super().__getattribute__(name)

        def macro_usa_non_farm(self) -> _Frame:
            return _Frame([{"日期": "invalid", "今值": "7.3"}, {"日期": "2026-08-07", "今值": "6.1"}])

        def macro_china_cx_pmi_yearly(self) -> _Frame:
            return _Frame([{"今值": "51.0"}, {"日期": "2026-08-01", "今值": "52.0"}])

    monkeypatch.setattr(collector, "_ak", lambda: PartialApi())
    monkeypatch.setattr(collector.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(collector, "_fetch_bea_us_imports_rows", lambda: [("2026-07", 395_280.0)])
    monkeypatch.setattr(collector, "_fetch_bea_core_pce_final_rows", lambda: [("2026-Q1", 4.4)])
    result = collector._collect_macro()
    assert result.status == "PARTIAL_FAILURE"
    values = {row["instrument_id"]: row["value"] for row in result.persist.gold_metrics}
    assert values["US_NONFARM_PAYROLLS_SA"] == 61.0
    assert values["PMI_CAIXIN_MANUFACTURING"] == 52.0
    assert values["M0_MONEY_SUPPLY"] == 11.8
    assert "cn_society_electricity:AttributeError" in result.error
    assert "US_NONFARM_PAYROLLS_SA:INVALID_ROW" in result.error
    assert "PMI_CAIXIN_MANUFACTURING:INVALID_ROW" in result.error


def test_macro_invalid_first_rows_do_not_skip_following_valid_observations(monkeypatch) -> None:
    class PartialApi(_MacroApi):
        def macro_china_money_supply(self):
            return _Frame([{"月份": "invalid", "流通中的现金(M0)-同比增长": 1},
                           {"月份": "2026年7月份", "流通中的现金(M0)-同比增长": 2}])
        def macro_china_cpi_yearly(self):
            return _Frame([{"日期": "bad", "今值": 1}, {"日期": "2026-08-01", "今值": 2}])
        def macro_china_pmi(self):
            return _Frame([{"月份": "invalid", "制造业-指数": 49},
                           {"月份": "2026年7月份", "制造业-指数": 50}])
        def repo_rate_query(self, *, symbol):
            return _Frame([{"date": "invalid", "FDR007": 2},
                           {"date": "2026-08-01", "FDR007": 0, "FR007": 3}])
        def bond_zh_us_rate(self):
            return _Frame([{"日期": "bad", "中国国债收益率10年": 2},
                           {"日期": "2026-08-01", "中国国债收益率10年": 2.5}])
        def macro_bank_usa_interest_rate(self):
            return _Frame([{"日期": "bad", "今值": 2}, {"日期": "2026-08-01", "今值": 3}])
    monkeypatch.setattr(collector, "_ak", lambda: PartialApi())
    monkeypatch.setattr(collector, "_fetch_bea_us_imports_rows", lambda: [("2026-07", 1)])
    monkeypatch.setattr(collector, "_fetch_bea_core_pce_final_rows", lambda: [("2026-Q1", 1)])
    result = collector._collect_macro()
    assert result.status == "PARTIAL_FAILURE"
    values = {row["instrument_id"]: row["value"] for row in result.persist.gold_metrics}
    assert values["M0_MONEY_SUPPLY"] == 2 and values["CPI"] == 2
    assert values["PMI_MANUFACTURING"] == 50 and values["DR007"] == 0
    assert values["CN10Y_YIELD"] == 2.5 and values["FED_FUNDS_RATE"] == 3
    assert all(f"{key}:INVALID_ROW" in result.error for key in
               ("M0_MONEY_SUPPLY", "CPI", "PMI_MANUFACTURING", "DR007", "CN10Y_YIELD", "FED_FUNDS_RATE"))


def test_macro_freshness_flags_old_or_missing_without_inventing_missing_release():
    old = collector.normalise_macro_point("US_NONFARM_PAYROLLS_SA", available_time="2025-09-05", value=1)
    recent = collector.normalise_macro_point("PMI_CAIXIN_MANUFACTURING", available_time="2026-09-01T08:00:00+08:00", value=50)
    warnings = collector._macro_freshness_warnings([old, recent], date(2026, 10, 31))
    assert warnings == ["US_NONFARM_PAYROLLS_SA:STALE_OVER_180_DAYS", "PMI_CAIXIN_SERVICES:NO_OBSERVATIONS"]
