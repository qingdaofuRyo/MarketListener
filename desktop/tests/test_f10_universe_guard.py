from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from market_monitor import f10


class Frame:
    def __init__(self, rows):
        self.rows = rows

    def iterrows(self):
        return iter(enumerate(self.rows))


def save_cache(path: Path, rows: list[dict], *, stale: bool = True) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    if stale:
        old = time.time() - 8 * 86400
        os.utime(path, (old, old))
    return path.read_bytes()


@pytest.fixture
def rows(monkeypatch):
    monkeypatch.setattr(f10, "_MIN_A_SHARE_UNIVERSE", 3)
    return [{"code": f"{i:06d}", "name": f"样本{i}"} for i in range(10)]


@pytest.mark.parametrize("problem", ["small", "shrink", "duplicate", "bad_code", "missing_name"])
def test_bad_provider_universe_preserves_valid_cache(tmp_path: Path, monkeypatch, rows, problem):
    path = tmp_path / "universe.jsonl"
    original = save_cache(path, rows)
    candidate = [dict(item) for item in rows]
    if problem == "small":
        candidate = candidate[:2]
    elif problem == "shrink":
        candidate = candidate[:8]
    elif problem == "duplicate":
        candidate[-1]["code"] = candidate[0]["code"]
    elif problem == "bad_code":
        candidate[-1]["code"] = "bad"
    else:
        candidate[-1]["name"] = "nan"
    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(stock_info_a_code_name=lambda: Frame(candidate)))
    assert f10.load_a_share_universe(tmp_path) == rows
    assert path.read_bytes() == original


def test_incomplete_provider_and_invalid_cache_fail_closed(tmp_path: Path, monkeypatch, rows):
    path = tmp_path / "universe.jsonl"
    original = save_cache(path, rows[:2])
    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(stock_info_a_code_name=lambda: Frame(rows[:2])))
    with pytest.raises(RuntimeError, match="no validated cache"):
        f10.load_a_share_universe(tmp_path)
    assert path.read_bytes() == original


def test_valid_new_universe_is_atomic_and_reports_cache_date(tmp_path: Path, monkeypatch, rows):
    path = tmp_path / "f10" / "cn" / "universe.jsonl"
    original = save_cache(path, rows)
    candidate = rows + [{"code": "000010", "name": "新样本"}]
    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(stock_info_a_code_name=lambda: Frame(candidate)))
    real_replace = f10.os.replace
    def fail_replace(*_args):
        raise OSError("synthetic replacement failure")
    monkeypatch.setattr(f10.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic"):
        f10.load_a_share_universe(path.parent)
    assert path.read_bytes() == original
    monkeypatch.setattr(f10.os, "replace", real_replace)
    assert f10.load_a_share_universe(path.parent) == candidate
    status = f10.read_a_share_universe_cache_status(tmp_path)
    assert status["count"] == 11 and status["status"] == "RECENT_CACHE" and status["savedAt"]


def test_stale_and_invalid_cache_status(tmp_path: Path, monkeypatch, rows):
    path = tmp_path / "f10" / "cn" / "universe.jsonl"
    save_cache(path, rows)
    assert f10.read_a_share_universe_cache_status(tmp_path)["status"] == "STALE_CACHE"
    path.write_text("invalid json", encoding="utf-8")
    assert f10.read_a_share_universe_cache_status(tmp_path) == {
        "status": "INVALID_OR_MISSING", "count": None, "savedAt": None
    }


def test_future_cache_timestamp_does_not_prevent_refresh(tmp_path: Path, monkeypatch, rows):
    path = tmp_path / "universe.jsonl"
    save_cache(path, rows, stale=False)
    future = time.time() + 86400
    os.utime(path, (future, future))
    candidate = rows + [{"code": "000010", "name": "新样本"}]
    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(stock_info_a_code_name=lambda: Frame(candidate)))
    assert f10.load_a_share_universe(tmp_path) == candidate


def test_hk_universe_status_reports_file_time_not_completeness(tmp_path: Path):
    assert f10.read_hk_universe_file_status(tmp_path) == {"status": "MISSING", "savedAt": None}
    path = tmp_path / "f10" / "hk" / "hkex_list.xlsx"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic file")
    status = f10.read_hk_universe_file_status(tmp_path)
    assert status["status"] == "LOCAL_FILE_PRESENT" and status["savedAt"]
    assert set(status) == {"status", "savedAt"}
