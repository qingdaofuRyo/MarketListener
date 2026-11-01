from io import BytesIO

from openpyxl import Workbook
import pytest

from market_monitor.czce_rank_workbook import parse_czce_rank_workbook_checked


_HEADER = ["名次", "会员简称", "交易量（手）", "增减量", "会员简称", "持买仓量", "增减量",
           "会员简称", "持卖仓量", "增减量"]


def _workbook(*, title_day: str = "2026-09-29", section_day: str = "2026-09-29", rank: int = 1) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append([f"郑州商品交易所期货持仓排名表({title_day})"])
    sheet.append([f"品种：苹果AP     日期：{section_day}"])
    sheet.append(_HEADER)
    sheet.append([1, "品种汇总", "100", "1", "品种多", "20", "2", "品种空", "30", "3"])
    sheet.append(["合计", None, "100", "1", None, "20", "2", None, "30", "3"])
    sheet.append([f"合约：AP610     日期：{section_day}"])
    sheet.append(_HEADER)
    sheet.append([rank, "甲期货", "1,234", "-5", "乙期货", "100", "+2", "丙期货", "70", "-3"])
    sheet.append([2, "丁期货", "10", "0", "-", "-", "-", "-", "-", "-"])
    sheet.append(["合计", None, "1,244", "-5", None, "100", "2", None, "70", "-3"])
    sink = BytesIO()
    workbook.save(sink)
    return sink.getvalue()


def test_workbook_preserves_published_directions_and_excludes_variety():
    frames = parse_czce_rank_workbook_checked(_workbook(), trading_day="20260929")
    assert list(frames) == ["AP610"]
    assert frames["AP610"].iloc[0]["vol"] == 1234
    assert frames["AP610"].iloc[0]["long_open_interest_chg"] == 2
    assert frames["AP610"].iloc[1]["long_open_interest"] is None


@pytest.mark.parametrize("raw,day,reason", [
    (_workbook(title_day="2026-09-28"), "20260929", "title date"),
    (_workbook(section_day="2026-09-28"), "20260929", "report date"),
    (_workbook(rank=2), "20260929", "duplicate rank"),
    (b"<html>challenge</html>", "20260929", "not a supported"),
])
def test_rejects_wrong_dates_duplicate_ranks_and_challenge(raw, day, reason):
    with pytest.raises(ValueError, match=reason):
        parse_czce_rank_workbook_checked(raw, trading_day=day)
