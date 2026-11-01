from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from market_monitor.ths_market import _fetch_breadth


@pytest.mark.parametrize("nested", [True, False])
def test_indexflash_breadth_and_limits_are_distinct_and_real_zero_is_preserved(nested):
    data = {"zdfb_data": {"znum": 606, "dnum": 4922},
            "jrbx_data": {"last_zdf": 1.26, "time": ["09:30", "15:00"]},
            "zdt_data": {"last_zdt": {"ztzs": 0, "dtzs": 23}} if nested else {}}
    if not nested:
        data["last_zdt"] = {"ztzs": 0, "dtzs": 23}
    client = SimpleNamespace(get=lambda *args, **kwargs: SimpleNamespace(
        status_code=200, content=json.dumps(data).encode("utf-8")))
    values, issue = _fetch_breadth(client)
    assert not issue
    assert values["上涨个股数量"] == 606
    assert values["下跌个股数量"] == 4922
    assert values["涨停个股数量"] == 0
    assert values["跌停个股数量"] == 23
    assert values["昨日涨停今日收益率"] == 1.26
    assert values["数据时间"] is None
    assert values["日内时间序列"] == ["09:30", "15:00"]


def test_indexflash_does_not_fill_missing_limits_from_breadth():
    data = {"zdfb_data": {"znum": 606, "dnum": 4922}, "jrbx_data": {}}
    client = SimpleNamespace(get=lambda *args, **kwargs: SimpleNamespace(
        status_code=200, content=json.dumps(data).encode("utf-8")))
    values, _ = _fetch_breadth(client)
    assert values["涨停个股数量"] is None and values["跌停个股数量"] is None
