"""Source isolation and resume gates for official CZCE workbook imports."""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import json

from openpyxl import Workbook
import pytest

from market_monitor.czce_workbook_rank_import import (
    import_czce_workbook_ranks, prepare_czce_workbook_day,
)
from market_monitor.czce_workbook_rank_backfill import ADAPTER, SCHEMA, _source_url
from market_monitor.futures_calendar import CALENDAR_PROVIDER, CALENDAR_SCHEMA
from market_monitor.futures_member_rank_source import (
    CZCE_WORKBOOK_SOURCE, select_member_position_source_rows,
)
from market_monitor.storage import MarketStore


DAYS = ("2026-09-29", "2026-09-30")


def _workbook(day: str) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.append([f"郑州商品交易所期货持仓排名表({day})"])
    sheet.append([f"品种：苹果AP     日期：{day}"])
    sheet.append(["名次", "会员简称", "交易量（手）", "增减量", "会员简称", "持买仓量",
                  "增减量", "会员简称", "持卖仓量", "增减量"])
    sheet.append([1, "品种汇总", 1, 0, "品种多", 1, 0, "品种空", 1, 0])
    sheet.append(["合计", None, 1, 0, None, 1, 0, None, 1, 0])
    sheet.append([f"合约：AP610     日期：{day}"])
    sheet.append(["名次", "会员简称", "交易量（手）", "增减量", "会员简称", "持买仓量",
                  "增减量", "会员简称", "持卖仓量", "增减量"])
    sheet.append([1, "成交会员", 123, -2, "多头会员", 100, 3, "空头会员", 70, -1])
    sheet.append(["合计", None, 123, -2, None, 100, 3, None, 70, -1])
    sink = BytesIO()
    book.save(sink)
    return sink.getvalue()


def _bronze(tmp_path, *, days=DAYS) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "cn_futures_trading_calendar.json").write_text(json.dumps({
        "schema": CALENDAR_SCHEMA, "provider": CALENDAR_PROVIDER,
        "market": "CN_FUTURE", "retrievedAt": "2026-10-02T10:00:00+00:00",
        "tradingDays": list(days),
    }), encoding="utf-8")
    directory = tmp_path / "bronze" / "czce_workbook_rank_capture"
    directory.mkdir(parents=True)
    for day in days:
        raw = _workbook(day)
        (directory / f"{day}.xlsx").write_bytes(raw)
        (directory / f"{day}.json").write_text(json.dumps({
            "schema": SCHEMA, "publisher": "CZCE", "adapter": ADAPTER,
            "tradingDay": day, "sourceUrl": _source_url(day, "xlsx"),
            "retrievedAt": "2026-10-02T10:00:00+00:00",
            "status": "PASS_RAW", "qualityStatus": "PASS_RAW",
            "authoritativeDataWritten": False, "httpStatus": 200,
            "payloadSha256": sha256(raw).hexdigest(), "bytes": len(raw),
            "rawPath": f"bronze/czce_workbook_rank_capture/{day}.xlsx",
            "contractCount": 1, "rankingRows": 1, "positionRows": 2, "tradeRows": 1,
        }), encoding="utf-8")


def test_prepare_rejects_manifest_sha_and_wrong_report_date(tmp_path) -> None:
    _bronze(tmp_path, days=(DAYS[0],))
    prepared = prepare_czce_workbook_day(tmp_path, DAYS[0])
    assert (prepared.contract_count, len(prepared.positions), len(prepared.trades)) == (1, 2, 1)
    manifest = tmp_path / "bronze" / "czce_workbook_rank_capture" / f"{DAYS[0]}.json"
    content = json.loads(manifest.read_text(encoding="utf-8"))
    content["payloadSha256"] = "0" * 64
    manifest.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(ValueError, match="SHA256"):
        prepare_czce_workbook_day(tmp_path, DAYS[0])
    raw = _workbook(DAYS[1])
    content["payloadSha256"] = sha256(raw).hexdigest()
    (manifest.parent / f"{DAYS[0]}.xlsx").write_bytes(raw)
    content["bytes"] = len(raw)
    manifest.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(ValueError, match="title date"):
        prepare_czce_workbook_day(tmp_path, DAYS[0])


def test_import_resumes_and_preserves_other_source_without_double_counting(tmp_path) -> None:
    _bronze(tmp_path)
    prepared = prepare_czce_workbook_day(tmp_path, DAYS[0])
    legacy_source = "official-czce-member-ranking"
    store = MarketStore(tmp_path)
    try:
        store.replace_futures_member_rank_day_source(
            trading_day=DAYS[0], exchange="CZCE", source=legacy_source,
            positions=[{**row, "source": legacy_source} for row in prepared.positions],
            trades=[{**row, "source": legacy_source} for row in prepared.trades],
            coverage={**prepared.coverage, "source": legacy_source},
        )
    finally:
        store.close()

    first = import_czce_workbook_ranks(tmp_path, start_day=DAYS[0], end_day=DAYS[1],
                                       max_days=1, write=True)
    second = import_czce_workbook_ranks(tmp_path, start_day=DAYS[0], end_day=DAYS[1],
                                        max_days=1, write=True)
    third = import_czce_workbook_ranks(tmp_path, start_day=DAYS[0], end_day=DAYS[1],
                                       max_days=1, write=True)
    assert (first["status"], first["importedDays"]) == ("BATCH_LIMIT", 1)
    assert (second["status"], second["resumedDays"], second["importedDays"]) == ("PASS", 1, 1)
    assert (third["status"], third["resumedDays"], third["importedDays"]) == ("PASS", 2, 0)

    store = MarketStore(tmp_path)
    try:
        ranks = store.list_futures_member_position_ranks(trading_day=DAYS[0], exchange="CZCE")
        coverage = store.list_futures_member_position_coverage(trading_day=DAYS[0], exchange="CZCE")
        trades = store.list_futures_member_trade_ranks(trading_day=DAYS[0], exchange="CZCE")
        assert len(ranks) == 4 and len(trades) == 2 and len(coverage) == 2
        assert {row["source"] for row in ranks} == {legacy_source, CZCE_WORKBOOK_SOURCE}
        selected = select_member_position_source_rows(ranks, coverage)
        assert len(selected) == 2
        assert {row["source"] for row in selected} == {CZCE_WORKBOOK_SOURCE}
    finally:
        store.close()
