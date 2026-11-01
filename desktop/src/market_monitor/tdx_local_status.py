"""Cached raw-file bounds for the internal console, separate from Silver."""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import struct
import threading
import time
from typing import Any, Callable

from .tdx_import_workflow import BEIJING, STATE_FILE, now, read_state, save_state


_CACHE_FILE = Path("state/tdx_local_status.json")
_SCHEMA = "tdx-local-file-status-v2"
_PREFIX_LABELS = {"10": "基本汇率", "12": "国际指数", "16": "COMEX期货",
                  "17": "NYMEX期货", "18": "CBOT期货", "27": "港股指数",
                  "31": "港股", "38": "宏观数据", "48": "港股扩展",
                  "62": "中证指数", "69": "华证指数", "102": "国证指数"}
_FUTURE_LABELS = {"28": "郑商所期货", "29": "大商所期货", "30": "上期所/上期能源期货",
                  "47": "中金所期货及标的指数", "66": "广期所期货",
                  "42": "通达信商品指数", "68": "期权波动率指数"}
_ASSET_LABELS = {"STOCK": "A股", "B_SHARE": "B股", "INDEX": "指数", "ETF": "ETF",
                 "LOF": "LOF", "REIT": "REIT", "CONVERTIBLE_BOND": "可转债",
                 "EXCHANGEABLE_BOND": "可交换债", "PLEDGED_REPO": "质押回购", "REPO": "回购"}


def terminal_roots() -> list[dict[str, Any]]:
    from .futures_bulk import resolve_tdx_root as futures_root
    from .tdx_local import resolve_tdx_root as financial_root

    return [
        {"terminal": "financial", "name": "通达信金融终端", "path": str(financial_root() or Path(os.environ.get("TDX_ROOT", r"C:\tongdaxin")))},
        {"terminal": "futures", "name": "通达信期货通", "path": str(futures_root() or Path(os.environ.get("TDX_FUTURES_ROOT", r"C:\new_tdxqh")))},
    ]


def _source_files(terminal: str, root: Path):
    if terminal == "financial":
        from .tdx_local import _files

        yield from _files(root)
        return
    for folder in ("lday", "fzline"):
        directory = root / "vipdoc" / "ds" / folder
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            prefix, separator, code = path.stem.partition("#")
            if separator and prefix in _FUTURE_LABELS and path.is_file() and path.suffix.lower() in {".day", ".lc5"}:
                yield path, {"symbol": code.upper(), "period": "1d" if path.suffix.lower() == ".day" else "5m",
                             "instrument_id": _futures_instrument_id(path) or ""}


def _futures_instrument_id(path: Path) -> str | None:
    """Use the importer's accepted filename rules and venue ownership.

    Unrecognized files may still have raw bounds but cannot claim an
    imported range merely because their symbol resembles another source.
    """
    from . import futures_bulk as importer

    match = next((found for pattern in (
        importer._OPTION_VOLATILITY, importer._CFFEX_REFERENCE, importer._CFFEX_RANKED,
        importer._DCE_F_CONTRACT, importer._DCE_F_SPECIAL, importer._SPECIAL,
        importer._CONTRACT, importer._INDEX,
    ) if (found := pattern.fullmatch(path.name))), None)
    if match is None:
        return None
    market = match.groupdict().get("market") or ("42" if path.name.upper().startswith("42#") else "")
    if market not in {*importer._EXCHANGES, "42", "68"}:
        return None
    code = match.group("code").upper()
    if market == "68":
        canonical = f"CN.TDX_OPTION_VOLATILITY.INDEX.{code}.OPTION_VOLATILITY_INDEX"
    elif market == "42":
        canonical = f"CN.TDX.INDEX.{code}.COMMODITY_INDEX"
    elif importer._CFFEX_REFERENCE.fullmatch(path.name):
        canonical = f"CN.CFFEX.INDEX.{code}.FUTURES_UNDERLYING_INDEX"
    elif rank := match.groupdict().get("rank"):
        canonical = f"CN.CFFEX.FUTURE.{match.group('product').upper()}.RANKED_{rank}"
    elif match.groupdict().get("delivery"):
        product = match.group("product").upper().removesuffix("-F")
        canonical = f"CN.{importer._exchange(market, product)}.FUTURE.{code}.CONTRACT"
    else:
        product = (match.groupdict().get("product") or code[:-2]).upper()
        kind = {"L7": "SECONDARY", "L8": "MAIN", "L9": "WEIGHTED"}[code[-2:]]
        canonical = f"CN.{importer._exchange(market, product.removesuffix('-F'))}.FUTURE.{product}.{kind}"
    return canonical + ".TDX"


def _file_bounds(path: Path) -> dict[str, Any]:
    """Only the two 32-byte endpoint records are read, never full history."""
    before = path.stat()
    signature = {"size": before.st_size, "mtime_ns": before.st_mtime_ns}
    if before.st_size == 0:
        return {**signature, "status": "EMPTY", "start": None, "end": None}
    if before.st_size % 32:
        return {**signature, "status": "INVALID_LENGTH", "start": None, "end": None}
    with path.open("rb") as stream:
        first = stream.read(32)
        stream.seek(before.st_size - 32)
        last = stream.read(32)
    after = path.stat()
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        return {**signature, "status": "WRITING", "start": None, "end": None}

    def timestamp(record: bytes) -> str:
        if path.suffix.lower() == ".day":
            encoded = struct.unpack_from("<I", record)[0]
            return date(encoded // 10000, encoded // 100 % 100, encoded % 100).isoformat()
        encoded, minutes = struct.unpack_from("<HH", record)
        if not 0 <= minutes < 1440:
            raise ValueError("分钟时间无效")
        return date(encoded // 2048 + 2004, encoded % 2048 // 100, encoded % 2048 % 100).isoformat()

    start, end = timestamp(first), timestamp(last)
    if start > end:
        raise ValueError("文件尾部日期早于首部")
    return {**signature, "status": "ENDPOINTS_READ", "start": start, "end": end}


def _group(terminal: str, path: Path, metadata: dict[str, str]) -> tuple[str, str, str]:
    prefix = path.parent.parent.name.lower()
    if prefix == "ds":
        prefix = path.stem.partition("#")[0]
        label = (_PREFIX_LABELS if terminal == "financial" else _FUTURE_LABELS).get(prefix, f"扩展市场 {prefix}")
        key = f"ds.{prefix}"
    else:
        exchange = {"sh": "沪市", "sz": "深市", "bj": "北交所"}.get(prefix, prefix)
        asset = metadata.get("asset_type", "")
        label = exchange + _ASSET_LABELS.get(asset, asset)
        key = f"{prefix}.{asset}"
    return key + "." + metadata["period"], label, prefix


def _import_key(terminal: str, metadata: dict[str, str]) -> tuple[str, str, str]:
    if terminal == "financial":
        instrument = ".".join(metadata[key] for key in ("market", "exchange", "asset_type", "symbol")) + ".TDX_LOCAL"
    else:
        instrument = metadata.get("instrument_id", "")
    return terminal, instrument, metadata["period"]


def scan_local_status(data_root: Path, *, roots: list[dict[str, Any]] | None = None,
                      previous: dict[str, Any] | None = None,
                      imported: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(data_root)
    previous = previous if (previous or {}).get("schema") == _SCHEMA else {}
    old_files = previous.get("files", {})
    imported = imported or {}
    imported_rows = {
        (str(item.get("terminal")), str(item.get("instrumentId")), str(item.get("period"))): item
        for item in imported.get("items", []) if isinstance(item, dict) and item.get("instrumentId")
    }
    files: dict[str, Any] = {}
    result: list[dict[str, Any]] = []
    for terminal in roots or terminal_roots():
        key, install = str(terminal["terminal"]), Path(str(terminal["path"]))
        if key == "financial":
            from .tdx_local import _state_path

            checkpoint_path = _state_path(root, install, start_date=None, end_date=None)
        else:
            checkpoint_path = root / "state" / "futures_import.json"
        checkpoint = read_state(checkpoint_path).get("files", {})
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        labels: dict[str, tuple[str, str]] = {}
        mtimes: list[int] = []
        pending = 0

        def inspect_file(source: tuple[Path, dict[str, str]]) -> tuple[Path, dict[str, str], dict[str, Any], int | None]:
            path, metadata = source
            relative = path.relative_to(install).as_posix()
            cache_key = key + ":" + str(install) + ":" + relative
            modified = None
            try:
                stat = path.stat()
                previous_row = old_files.get(cache_key, {})
                if previous_row.get("status") == "ENDPOINTS_READ" and previous_row.get("size") == stat.st_size and previous_row.get("mtime_ns") == stat.st_mtime_ns:
                    row = dict(previous_row)
                else:
                    row = _file_bounds(path)
                modified = stat.st_mtime_ns
            except (OSError, ValueError, struct.error) as error:
                row = {"status": "UNREADABLE", "start": None, "end": None, "detail": type(error).__name__}
            return path, metadata, row, modified

        # Four read-only endpoint workers reduce Windows per-file latency.
        # Official import writers stay serial in the operation queue.
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="tdx-file-endpoint") as pool:
            for path, metadata, row, modified in pool.map(inspect_file, _source_files(key, install)):
                relative = path.relative_to(install).as_posix()
                cache_key = key + ":" + str(install) + ":" + relative
                files[cache_key] = row
                if modified is not None:
                    mtimes.append(modified)
                old = checkpoint.get(relative, {}) if isinstance(checkpoint, dict) else {}
                imported_signature = row.get("size") is not None and old.get("size") == row.get("size") and old.get("mtime_ns") == row.get("mtime_ns")
                if key == "financial":
                    imported_signature = imported_signature and old.get("normalization_version") == "tdx-cn-v2"
                pending += int(not imported_signature)
                group_key, label, source_code = _group(key, path, metadata)
                labels[group_key] = label, source_code
                fact = imported_rows.get(_import_key(key, metadata), {})
                groups[group_key].append({**row, "importStart": fact.get("earliestDate"), "importEnd": fact.get("latestDate")})
        group_rows = []
        for group_key, rows in sorted(groups.items()):
            starts = [str(row["start"]) for row in rows if row.get("start")]
            ends = [str(row["end"]) for row in rows if row.get("end")]
            common = sorted(Counter(ends).items(), key=lambda pair: (-pair[1], pair[0]), reverse=False)
            imported_starts = [str(row["importStart"]) for row in rows if row.get("importStart")]
            imported_ends = [str(row["importEnd"]) for row in rows if row.get("importEnd")]
            invalid = sum(row.get("status") not in {"ENDPOINTS_READ", "EMPTY"} for row in rows)
            label, code = labels[group_key]
            group_rows.append({"key": group_key, "label": label, "source": str(terminal["name"]) + "本地文件",
                               "sourceCode": code, "period": group_key.rsplit(".", 1)[1], "fileCount": len(rows),
                               "earliestDate": min(starts) if starts else None, "latestDate": max(ends) if ends else None,
                               "commonLatestDate": common[0][0] if common else None,
                               "commonLatestCount": common[0][1] if common else 0, "invalidFileCount": invalid,
                               "rawStatus": "PARTIAL" if invalid else "ENDPOINTS_ONLY",
                               "importedEarliestDate": min(imported_starts) if imported_starts else None,
                               "importedLatestDate": max(imported_ends) if imported_ends else None})
        if not imported.get("available"):
            old_terminal = next((item for item in (previous or {}).get("roots", []) if item.get("terminal") == key), {})
            old_groups = {item.get("key"): item for item in old_terminal.get("groups", [])}
            for item in group_rows:
                old = old_groups.get(item["key"], {})
                item["importedEarliestDate"] = old.get("importedEarliestDate")
                item["importedLatestDate"] = old.get("importedLatestDate")
                item["importedRangeStale"] = bool(old)
        last_import = (imported.get("lastRuns") or {}).get(key)
        if last_import is None:
            old_terminal = next((item for item in (previous or {}).get("roots", []) if item.get("terminal") == key), {})
            last_import = old_terminal.get("lastImport")
        result.append({**terminal, "available": (install / "vipdoc").is_dir(), "groups": group_rows,
                       "lastFileModifiedAt": datetime.fromtimestamp(max(mtimes) / 1e9, BEIJING).isoformat(timespec="seconds") if mtimes else None,
                       "lastImport": last_import, "pendingFiles": pending})
    return {"schema": _SCHEMA, "checkedAt": now(), "roots": result, "files": files,
            "inventoryAvailable": bool(imported.get("available")),
            "inventoryStale": bool(imported.get("stale")),
            "inventoryRevision": imported.get("revision"),
            "scope": "原始文件只读首尾日期；不证明中间交易日连续。入库范围取质量通过的Silver查询清单，未扫描全量K线。"}


class TdxLocalStatus:
    """The admin process owns only JSON/files, never the market database."""

    def __init__(self, data_root: Path, fetch_imported: Callable[[], dict[str, Any]] | None = None):
        self.root = Path(data_root)
        self.fetch_imported = fetch_imported
        self._lock = threading.RLock()
        self._snapshot = read_state(self.root / _CACHE_FILE)
        if self._snapshot.get("schema") != _SCHEMA:
            self._snapshot = {}
        self._scanning = False
        self._next_scan = 0.0
        self._error: str | None = None

    def refresh(self) -> None:
        with self._lock:
            if self._scanning:
                return
            self._scanning = True
            self._next_scan = time.monotonic() + 60
        threading.Thread(target=self._scan, name="tdx-local-file-bounds", daemon=True).start()

    def _scan(self) -> None:
        try:
            imported = self.fetch_imported() if self.fetch_imported else {}
        except Exception:
            imported = {}
        try:
            value = scan_local_status(self.root, previous=self._snapshot, imported=imported)
            save_state(self.root / _CACHE_FILE, value)
            with self._lock:
                self._snapshot, self._error = value, None
        except Exception as error:
            with self._lock:
                self._error = f"{type(error).__name__}: {error}"[:300]
        finally:
            with self._lock:
                self._scanning = False

    def snapshot(self, *, refresh: bool = False, enabled: bool = True,
                 trigger_scan: bool = True) -> dict[str, Any]:
        if trigger_scan and (refresh or time.monotonic() >= self._next_scan):
            self.refresh()
        with self._lock:
            value = deepcopy({key: item for key, item in self._snapshot.items() if key != "files"})
            value.update(scanning=self._scanning, stale=self._scanning or bool(self._error)
                         or not value.get("inventoryAvailable") or bool(value.get("inventoryStale")), error=self._error)
        if not value.get("roots"):
            value["roots"] = [{**item, "available": (Path(item["path"]) / "vipdoc").is_dir(), "groups": []} for item in terminal_roots()]
        workflow = read_state(self.root / STATE_FILE)
        download = read_state(self.root / "state" / "tdx_download_ui" / "latest.json")
        checkpoints = read_state(self.root / "state" / "tdx_download_ui" / "checkpoints.json")
        from .tdx_afterhours import latest_slot
        from .tdx_download_ui import FINANCIAL, FUTURES, terminal_completion

        now = datetime.now(BEIJING)
        slot = latest_slot(now)
        next_slot = (datetime.fromisoformat(slot) + timedelta(days=1)).isoformat()
        for terminal in value["roots"]:
            key = terminal["terminal"]
            item = deepcopy(workflow.get("terminals", {}).get(key, {}))
            if not item:
                item = deepcopy(terminal.get("lastImport") or {})
            if item:
                item["pendingFiles"] = terminal.get("pendingFiles")
            terminal["lastImport"] = item or None
            raw_entries = checkpoints.get(key)
            entries = ({name: item for name, item in raw_entries.items() if isinstance(item, dict)}
                       if isinstance(raw_entries, dict) else {})
            batches = FINANCIAL if key == "financial" else FUTURES
            completion = terminal_completion(entries, slot, batches)
            from .tdx_gui_auto_refresh import financial_cn_coverage, _futures_weekend_coverage
            slot_day = datetime.fromisoformat(slot).astimezone(BEIJING).date()
            skip_reason = (_futures_weekend_coverage(self.root, slot_day, now) if key == "futures"
                           else financial_cn_coverage(self.root, slot_day, now))
            reported_items = download.get("terminals")
            reported = (reported_items.get(key) if download.get("slot") == slot
                        and isinstance(reported_items, dict) else None)
            download_status = (completion if completion != "NOT_DOWNLOADED"
                               else reported or completion)
            finished = [item.get("finishedAt") for item in entries.values()
                        if isinstance(item, dict) and item.get("slot") == slot
                        and item.get("status") == "UI_DONE_UNVERIFIED" and item.get("finishedAt")]
            terminal["lastDownload"] = {"slot": slot, "finishedAt": max(finished) if finished else None,
                                        "status": download_status, "source": "终端下载驱动检查点"}
            complete = completion in {"UI_DONE_UNVERIFIED", "COMPLETE_WITH_SKIPS", "NO_GUI_RAW_RECENT"}
            terminal["downloadSchedule"] = {"currentSlot": slot,
                                             "nextCheckSlot": next_slot if complete or key == "futures" and skip_reason else slot,
                                             "currentSlotComplete": complete,
                                             "needsReconciliation": completion == "PREVIOUS_RUN_NEEDS_RECONCILIATION",
                                             "skipReason": skip_reason,
                                             "skippedBatches": (["cn-day", "cn-5m"] if key == "financial" and skip_reason else
                                                                 [batch.key for batch in FUTURES] if key == "futures" and skip_reason else [])}
        value["startupImport"] = {"enabled": enabled, "status": workflow.get("status", "NOT_RECORDED"),
                                   "detail": "两款终端各自按盘后窗口自动下载；下载结束后增量导入稳定文件。界面完成、原文件和Silver质量分别核验。"}
        try:
            summary = json.loads((self.root / "state" / "futures_financial_coverage.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            summary = None
        value["financialCoverage"] = summary if isinstance(summary, dict) else None
        api = read_state(self.root / "state" / "dce_official_api_status.json")
        ranks = read_state(self.root / "state" / "dce_authorized_ranks" / "progress.json")
        if ranks.get("status") == "RUNNING" and ranks.get("accountStatus"):
            api = {"status": "RUNNING", "accounts": ranks["accountStatus"]}
        value["dceApi"] = {"status": api.get("status"), "accounts": api.get("accounts", {})} if api else None
        return value
