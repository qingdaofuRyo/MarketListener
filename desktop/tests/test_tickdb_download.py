from __future__ import annotations

import gzip
import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path


SCRIPT = Path(__file__).parents[2] / "scripts" / "tickdb_download.py"
SPEC = importlib.util.spec_from_file_location("tickdb_download", SCRIPT)
assert SPEC and SPEC.loader
tickdb_download = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tickdb_download)


def _downloader(tmp_path: Path) -> object:
    return tickdb_download.Downloader(
        tmp_path,
        "test-key",
        datetime.now(tickdb_download.SHANGHAI) + timedelta(hours=1),
        60_000,
    )


def test_cn_hk_index_etf_scope_uses_market_type_and_etf_prefixes() -> None:
    wanted = [
        {"market": "CN", "type": "indices", "symbol": "000001"},
        {"market": "HK", "type": "indices", "symbol": "HSI"},
        {"market": "CN", "type": "stock", "symbol": "510300"},
        {"market": "CN", "type": "stock", "symbol": "159915"},
    ]
    excluded = [
        {"market": "CN", "type": "stock", "symbol": "600519"},
        {"market": "HK", "type": "stock", "symbol": "00700"},
        {"market": "GLOBAL", "type": "indices", "symbol": "SPX"},
    ]

    assert all(tickdb_download.Downloader.in_scope(item, "cn-hk-index-etf") for item in wanted)
    assert not any(tickdb_download.Downloader.in_scope(item, "cn-hk-index-etf") for item in excluded)


def test_latest_refresh_keeps_checkpoint_and_counts_only_new_bars(tmp_path: Path) -> None:
    downloader = _downloader(tmp_path)
    product = {"market": "HK", "type": "indices", "symbol": "HSI", "is_active": True}
    payloads = [
        {"code": 0, "data": {"klines": [{"time": 100}, {"time": 200}]}},
        {"code": 0, "data": {"klines": [{"time": 200}, {"time": 300}]}},
    ]
    downloader.request = lambda _path, _params: payloads.pop(0)  # type: ignore[method-assign]

    arguments = ([product], ("1d",), frozenset(), False)
    downloader.fetch_history(*arguments, latest_only=True, scope="cn-hk-index-etf")
    downloader.fetch_history(
        *arguments,
        latest_only=True,
        scope="cn-hk-index-etf",
        refresh_complete=True,
    )

    assert downloader.state("kline:indices:HSI:1d") == ("complete", 300, 3, 2)
    latest = tmp_path / "raw" / "kline" / "indices" / "1d" / "HSI" / "latest.json.gz"
    with gzip.open(latest, "rt", encoding="utf-8") as handle:
        assert [row["time"] for row in json.load(handle)["data"]["klines"]] == [200, 300]
    downloader.session.close()
    downloader.db.close()


def test_resume_since_skips_tasks_already_refreshed_in_same_run(tmp_path: Path) -> None:
    downloader = _downloader(tmp_path)
    product = {"market": "HK", "type": "indices", "symbol": "HSI", "is_active": True}
    downloader.request = lambda _path, _params: {  # type: ignore[method-assign]
        "code": 0,
        "data": {"klines": [{"time": 100}]},
    }
    started_at = datetime.now(tickdb_download.SHANGHAI) - timedelta(seconds=1)
    arguments = ([product], ("1d",), frozenset(), False)

    downloader.fetch_history(*arguments, latest_only=True, scope="cn-hk-index-etf")
    downloader.request = lambda *_args: (_ for _ in ()).throw(AssertionError("must resume"))  # type: ignore[method-assign]
    downloader.fetch_history(
        *arguments,
        latest_only=True,
        scope="cn-hk-index-etf",
        refresh_complete=True,
        resume_since=started_at,
    )

    assert downloader.state("kline:indices:HSI:1d") == ("complete", 100, 1, 1)
    downloader.session.close()
    downloader.db.close()


def test_audit_scope_uses_catalog_checkpoint_and_file_list_only(tmp_path: Path) -> None:
    catalog = {
        "data": {
            "products": [
                {"market": "CN", "type": "indices", "symbol": "000001", "is_active": True},
                {"market": "HK", "type": "indices", "symbol": "HSI", "is_active": True},
                {"market": "CN", "type": "stock", "symbol": "510300", "is_active": True},
            ]
        }
    }
    catalog_path = tmp_path / "raw" / "catalog" / "00000000.json.gz"
    catalog_path.parent.mkdir(parents=True)
    with gzip.open(catalog_path, "wt", encoding="utf-8") as handle:
        json.dump(catalog, handle)
    downloader = _downloader(tmp_path)
    downloader.update("kline:indices:000001:1d", "complete", 100, 7, 1)
    downloader.update("kline:indices:HSI:1d", "failed", None, 0, 1, "safe error")
    raw = tmp_path / "raw" / "kline" / "indices" / "1d" / "000001" / "latest.json.gz"
    raw.parent.mkdir(parents=True)
    raw.touch()
    downloader.session.close()
    downloader.db.close()

    rows = tickdb_download.audit_scope(tmp_path, "cn-hk-index-etf", ("1d", "5m"))

    assert rows[0]["group"] == "CN_INDEX"
    assert rows[0] | {"latestUpdatedAt": None} == {
        "group": "CN_INDEX",
        "interval": "1d",
        "targetTasks": 1,
        "complete": 1,
        "failed": 0,
        "unsupported": 0,
        "rawFiles": 1,
        "savedRecords": 7,
        "requests": 1,
        "latestBarTime": "1970-01-01T08:00:00.100000+08:00",
        "latestUpdatedAt": None,
    }
    assert next(row for row in rows if row["group"] == "HK_INDEX" and row["interval"] == "1d")["failed"] == 1
