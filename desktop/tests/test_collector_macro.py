from __future__ import annotations

from typing import Any

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

    result = collector._collect_macro()

    assert result.status == "PASS"
    assert result.rows == 11
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
    }
