"""Regression coverage for resumable full-market stock backfills."""

from __future__ import annotations

import json
from pathlib import Path

from market_monitor import full_market


def test_full_stock_backfill_persists_chinese_checkpoint_and_resumes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        full_market,
        "_universe",
        lambda _root, _market: [{"code": "000001", "name": "样本甲"}, {"code": "000002", "name": "样本乙"}],
    )

    def fetcher(_market: str, _code: str, _start_date: str):
        return [{"日期": "2026-08-10", "开盘": 10, "最高": 12, "最低": 9, "收盘": 11, "成交量": 100, "成交额": 1100}]

    first = full_market.run_full_stock_backfill(tmp_path, market="CN", workers=1, batch_size=1, pause_seconds=0.1, fetcher=fetcher)
    assert first["状态"] == "完成"
    assert first["市场"]["CN"]["已完成"] == 2
    state = json.loads((tmp_path / "bulk_stock" / "cn_state.json").read_text(encoding="utf-8"))
    assert state["状态"] == "完成"
    assert state["已完成代码"] == ["000001", "000002"]

    second = full_market.run_full_stock_backfill(tmp_path, market="CN", workers=1, batch_size=1, pause_seconds=0.1, fetcher=fetcher)
    assert second["市场"]["CN"]["待处理"] == 0


def test_full_market_rows_do_not_mislabel_sources():
    rows = [{"date": "2026-10-30", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100}]
    assert full_market._normalize_rows(rows, "CN", "600519", "样本", source="pytdx-full-market-stock")[0]["source"] == "pytdx-full-market-stock"
    assert full_market._normalize_rows(rows, "HK", "00700", "样本", source="akshare-sina-hk-daily")[0]["source"] == "akshare-sina-hk-daily"
    assert full_market._normalize_rows(rows, "CN", "600519", "样本")[0]["source"] == "unverified-custom-fetcher"
    assert full_market._normalize_etf_rows(rows, "510300", "样本", 1)[0]["source"] == "pytdx-full-market-etf"


def test_full_market_does_not_fabricate_missing_or_invalid_ohlc():
    valid = {"date": "2026-10-30", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 0}
    bad = [dict(valid, open=None), dict(valid, high=None), dict(valid, low=None),
           dict(valid, high=10), dict(valid, low=12), dict(valid, volume=-1),
           dict(valid, close=float("inf")), dict(valid, date="2026-02-30")]
    rows = full_market._normalize_rows([*bad, valid], "CN", "600519", "样本")
    assert len(rows) == 1 and rows[0]["volume"] == 0
    assert (rows[0]["open"], rows[0]["high"], rows[0]["low"], rows[0]["close"]) == (10, 12, 9, 11)
