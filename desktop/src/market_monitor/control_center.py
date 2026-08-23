"""Desktop HTML control center: fetch logs, storage health and sync filters.

The control center is served by the Python standard library only (no new
runtime dependency).  It exposes a small JSON API consumed by a self-contained
HTML page so the user can watch provider runs, partition ingestion, quarantine
issues and build a validated sync/export filter from a browser.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

from .dashboard import build_health_report
from .dataset_catalog import dataset_index
from .package_builder import latest_package_info
from .industry_graph.f10 import CompanyRepository


_MARKETS = ("CN", "HK", "GLOBAL")


@dataclass(frozen=True)
class SyncFilter:
    """Validated filter used for sync/export planning.

    Numeric filters use the units the user specified: A 股/港股总市值按亿元/亿港元，
    成交额按亿元，沉淀资金按亿元。
    """

    markets: tuple[str, ...] = _MARKETS
    min_market_cap: float | None = None
    min_hk_market_cap: float | None = None
    min_amount: float | None = None
    amount_rank_top_n: int | None = None
    min_futures_capital: float | None = None
    datasets: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "markets": list(self.markets),
            "min_market_cap": self.min_market_cap,
            "min_hk_market_cap": self.min_hk_market_cap,
            "min_amount": self.min_amount,
            "amount_rank_top_n": self.amount_rank_top_n,
            "min_futures_capital": self.min_futures_capital,
            "datasets": list(self.datasets),
        }


def parse_sync_filter(params: Mapping[str, str | Sequence[str]]) -> SyncFilter:
    """Parse and validate control-center query parameters."""

    def first(name: str) -> str | None:
        value = params.get(name)
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, Sequence):
            return str(value[0]) if value else None
        return None

    def positive_float(name: str) -> float | None:
        raw = first(name)
        if raw is None or not raw.strip():
            return None
        try:
            value = float(raw)
        except ValueError as error:
            raise ValueError(f"{name} must be a number") from error
        if value <= 0:
            raise ValueError(f"{name} must be positive")
        return value

    def positive_int(name: str) -> int | None:
        raw = first(name)
        if raw is None or not raw.strip():
            return None
        try:
            value = int(raw)
        except ValueError as error:
            raise ValueError(f"{name} must be an integer") from error
        if value <= 0:
            raise ValueError(f"{name} must be positive")
        return value

    raw_markets = first("markets")
    markets = (
        tuple(part.strip().upper() for part in raw_markets.split(",") if part.strip())
        if raw_markets
        else _MARKETS
    )
    unknown_markets = [market for market in markets if market not in _MARKETS]
    if unknown_markets:
        raise ValueError(f"unknown markets: {', '.join(unknown_markets)}")

    raw_datasets = first("datasets")
    registry = dataset_index()
    datasets = (
        tuple(part.strip().upper() for part in raw_datasets.split(",") if part.strip())
        if raw_datasets
        else ()
    )
    unknown_datasets = [dataset_id for dataset_id in datasets if dataset_id not in registry]
    if unknown_datasets:
        raise ValueError(f"unknown datasets: {', '.join(unknown_datasets)}")

    return SyncFilter(
        markets=markets,
        min_market_cap=positive_float("min_market_cap"),
        min_hk_market_cap=positive_float("min_hk_market_cap"),
        min_amount=positive_float("min_amount"),
        amount_rank_top_n=positive_int("amount_rank_top_n"),
        min_futures_capital=positive_float("min_futures_capital"),
        datasets=datasets,
    )


def build_sync_plan(sync_filter: SyncFilter, *, now: datetime | None = None) -> dict[str, Any]:
    """Return a validated, machine-readable sync/export plan."""

    registry = dataset_index()
    selected_ids = sync_filter.datasets or tuple(registry)
    selected = [registry[dataset_id].to_dict() for dataset_id in selected_ids]
    created_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return {
        "plan_id": f"plan-{uuid4().hex[:12]}",
        "created_at": created_at,
        "status": "PLANNED",
        "filter": sync_filter.to_dict(),
        "selected_datasets": list(selected_ids),
        "selected_dataset_count": len(selected_ids),
        "note": "筛选条件已校验；实际抓取/打包/同步由后续 CLI 步骤执行",
        "datasets": selected,
    }


def build_control_center_report(data_root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Combine the health report with registered datasets and summary stats."""

    health = build_health_report(data_root, now=now)
    fetch_jobs = _load_fetch_jobs(data_root)
    runs = list(health.sources)
    partitions = list(health.partitions)
    quarantine = list(health.quarantine)
    total_rows = sum(int(partition.get("row_count", 0) or 0) for partition in partitions)
    failed_providers = sorted(
        {
            str(source.get("provider", ""))
            for source in runs
            if source.get("status") in {"FAILED", "BLOCKED"}
        }
    )
    return {
        "generated_at": health.generated_at,
        "stale_after_seconds": health.stale_after_seconds,
        "stats": {
            "run_count": len(runs),
            "partition_count": len(partitions),
            "total_rows": total_rows,
            "quarantine_count": len(quarantine),
            "failed_providers": failed_providers,
            "storage_bytes": int(health.storage.get("total", 0)),
        },
        "runs": runs,
        "partitions": partitions,
        "quarantine": quarantine,
        "storage": health.storage,
        "datasets": [definition.to_dict() for definition in dataset_index().values()],
        "fetch_jobs": fetch_jobs,
        "android_package": latest_package_info(data_root),
        "coverage": _coverage_summary(data_root),
        "f10": _f10_summary(data_root),
    }


def _f10_summary(data_root: Path) -> dict[str, Any]:
    """Summarize A/H F10 collection progress from local snapshots."""
    result: dict[str, Any] = {"available": False, "markets": {}}
    f10_root = Path(data_root) / "f10"
    if not f10_root.is_dir():
        return result
    result["available"] = True
    for market in ("cn", "hk"):
        directory = f10_root / market
        summary: dict[str, Any] = {}
        summary_path = directory / "summary.json"
        if summary_path.is_file():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                summary = {}
        record_count = 0
        for path in sorted(directory.glob("details_*.jsonl")):
            try:
                record_count += sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
            except OSError:
                continue
        result["markets"][market.upper()] = {
            "record_count": record_count,
            "summary": summary,
        }
    return result


def _coverage_summary(data_root: Path) -> dict[str, Any]:
    """Scan silver parquet partitions and report real K-line coverage."""

    try:
        import duckdb
    except Exception:
        return {"available": False, "error": "duckdb unavailable", "groups": [], "total_instruments": 0, "total_rows": 0}
    # The market query manifest already stores exact per-file coverage.  Use
    # its 50MB metadata table when available instead of rescanning hundreds of
    # millions of Silver rows on every /api/health request.
    manifest = Path(data_root) / "state" / "kline_query.duckdb"
    if manifest.is_file():
        try:
            con = duckdb.connect(str(manifest), read_only=True)
            try:
                groups = [
                    {
                        "market": row[0],
                        "asset_type": row[1],
                        "instruments": int(row[2]),
                        "rows": int(row[3]),
                        "periods": int(row[4]),
                    }
                    for row in con.execute(
                        "SELECT market, asset_type, count(DISTINCT instrument_id), "
                        "sum(row_count)::BIGINT, count(DISTINCT period) "
                        "FROM instrument_file GROUP BY market, asset_type ORDER BY market, asset_type"
                    ).fetchall()
                ]
            finally:
                con.close()
            return {
                "available": True,
                "groups": groups,
                "total_instruments": sum(item["instruments"] for item in groups),
                "total_rows": sum(item["rows"] for item in groups),
                "source": "kline-query-manifest",
            }
        except Exception:
            # A missing/old/corrupt optional manifest must not make health
            # unavailable; retain the legacy Silver fallback for small stores.
            pass
    files = sorted(Path(data_root).joinpath("silver").rglob("*.parquet"))
    if not files:
        return {"available": True, "groups": [], "total_instruments": 0, "total_rows": 0}
    try:
        con = duckdb.connect()
        try:
            con.execute("SET memory_limit='2GB'")
            query = f"""
                SELECT market, asset_type,
                       count(DISTINCT instrument_id) AS instruments,
                       count(*) AS rows,
                       count(DISTINCT period) AS periods
                FROM read_parquet({[str(file) for file in files]!r})
                GROUP BY market, asset_type
                ORDER BY market, asset_type
            """
            groups = [
                {
                    "market": row[0],
                    "asset_type": row[1],
                    "instruments": int(row[2]),
                    "rows": int(row[3]),
                    "periods": int(row[4]),
                }
                for row in con.execute(query).fetchall()
            ]
        finally:
            con.close()
    except Exception as error:
        return {"available": True, "groups": [], "total_instruments": 0, "total_rows": 0, "error": str(error)}
    return {
        "available": True,
        "groups": groups,
        "total_instruments": sum(group["instruments"] for group in groups),
        "total_rows": sum(group["rows"] for group in groups),
    }


def _load_fetch_jobs(data_root: Path) -> dict[str, Any]:
    """Read the latest collection-session summary produced by ``fetch``."""

    target = Path(data_root) / "control_summary.json"
    if not target.is_file():
        return {"latest": None, "tasks": []}
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"latest": None, "tasks": []}
    latest_keys = (
        "session_id",
        "started_at",
        "completed_at",
        "status",
        "task_count",
        "passed",
        "partial_failure",
        "failed",
        "blocked",
        "total_rows",
    )
    latest = {key: document.get(key) for key in latest_keys} if isinstance(document, dict) else None
    tasks = document.get("tasks") if isinstance(document, dict) else None
    if not isinstance(tasks, list):
        tasks = []
    return {"latest": latest, "tasks": tasks}


class ControlCenterHandler(BaseHTTPRequestHandler):
    """HTTP handler exposing the control-center page and JSON API."""

    server_version = "MarketMonitorControlCenter/0.1"
    data_root: Path
    quiet: bool = False
    company_repository: CompanyRepository

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(_INDEX_HTML)
            return
        if parsed.path == "/api/health":
            self._send_json(build_control_center_report(self.data_root))
            return
        if parsed.path == "/api/f10/companies":
            self._send_f10_companies(parse_qs(parsed.query))
            return
        if parsed.path.startswith("/api/f10/companies/"):
            self._send_f10_company(unquote(parsed.path.removeprefix("/api/f10/companies/")))
            return
        if parsed.path == "/api/datasets":
            self._send_json([definition.to_dict() for definition in dataset_index().values()])
            return
        if parsed.path == "/api/sync-filter" or parsed.path == "/api/sync-plan":
            try:
                sync_filter = parse_sync_filter(parse_qs(parsed.query))
            except ValueError as error:
                self._send_json({"error": str(error)}, status=400)
                return
            payload = sync_filter.to_dict() if parsed.path == "/api/sync-filter" else build_sync_plan(sync_filter)
            self._send_json(payload)
            return
        if parsed.path == "/api/android-package-info":
            info = latest_package_info(self.data_root)
            if info is None:
                self._send_json({"error": "no active Android package"}, status=404)
                return
            self._send_json(info)
            return
        if parsed.path == "/api/android-package":
            self._send_android_package()
            return
        if parsed.path in {"/industry", "/industry/"}:
            self._send_industry_atlas()
            return
        if parsed.path in {"/industry-v2", "/industry-v2/"}:
            self.send_response(307)
            self.send_header("Location", "/industry/")
            self.end_headers()
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        self._send_json({"error": "this control center exposes read-only JSON endpoints"}, status=405)

    def log_message(self, format: str, *args: object) -> None:
        if not self.quiet:
            super().log_message(format, *args)

    def _send_json(self, payload: Any, *, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, *, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_android_package(self) -> None:
        info = latest_package_info(self.data_root)
        if info is None:
            self._send_json({"error": "no active Android package"}, status=404)
            return
        package_path = Path(self.data_root) / "packages" / f"{info['package_id']}.zip"
        try:
            body = package_path.read_bytes()
        except OSError:
            self._send_json({"error": "active Android package file is missing"}, status=404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{info["package_id"]}.zip"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_f10_companies(self, params: Mapping[str, Sequence[str]]) -> None:
        """Return a bounded, server-filtered company summary page."""

        def first(name: str, default: str = "") -> str:
            values = params.get(name)
            return str(values[0]) if values else default

        try:
            page = self.company_repository.list_companies(
                query=first("q"),
                market=first("market") or None,
                page=int(first("page", "1")),
                page_size=int(first("page_size", "50")),
                sort=first("sort", "name"),
                descending=first("direction", "asc").lower() == "desc",
            )
        except (TypeError, ValueError) as error:
            self._send_json({"error": str(error)}, status=400)
            return
        self._send_json(page.to_dict())

    def _send_f10_company(self, instrument_key: str) -> None:
        company = self.company_repository.company(instrument_key)
        if company is None:
            self._send_json({"error": "company not found"}, status=404)
            return
        self._send_json(company.to_dict())

    def _send_industry_atlas(self) -> None:
        candidates = (
            Path(self.data_root) / "industry" / "industry-atlas.html",
            Path(self.data_root).parent / "reports" / "industry" / "industry-atlas.html",
        )
        for candidate in candidates:
            try:
                if candidate.is_file():
                    self._send_html(candidate.read_text(encoding="utf-8"))
                    return
            except OSError:
                continue
        self._send_json({"error": "industry atlas not built yet"}, status=404)


def make_handler(data_root: Path, *, quiet: bool = False) -> type[ControlCenterHandler]:
    """Build a handler bound to a concrete data root."""

    class _Handler(ControlCenterHandler):
        pass

    _Handler.data_root = data_root
    _Handler.quiet = quiet
    _Handler.company_repository = CompanyRepository(data_root)
    return _Handler


def serve_control_center(
    data_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    timeout_seconds: float | None = None,
    quiet: bool = False,
) -> tuple[str, int]:
    """Run the control center until interrupted or the optional timeout fires."""

    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    server = ThreadingHTTPServer((host, port), make_handler(data_root, quiet=quiet))
    actual_port = int(server.server_address[1])
    timer: threading.Timer | None = None
    if timeout_seconds is not None:
        timer = threading.Timer(timeout_seconds, server.shutdown)
        timer.daemon = True
        timer.start()
    try:
        if not quiet:
            print(f"market-monitor control center: http://{host}:{actual_port}/")
        server.serve_forever(poll_interval=0.2)
    finally:
        if timer is not None:
            timer.cancel()
        server.server_close()
    return host, actual_port


_INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MarketListener 数据生产控制中心</title>
<style>
:root { color-scheme: dark; }
body { font-family: system-ui, "Microsoft YaHei", sans-serif; margin: 0; background: #0f1117; color: #e6e9f0; }
header { padding: 18px 24px; background: #161a24; border-bottom: 1px solid #2a3040; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
header h1 { font-size: 18px; margin: 0; }
header .meta { color: #8b93a7; font-size: 12px; }
button, input { font: inherit; }
button { background: #2f6fed; color: #fff; border: 0; border-radius: 6px; padding: 7px 14px; cursor: pointer; }
button:hover { background: #4a82f2; }
main { padding: 18px 24px; max-width: 1400px; margin: 0 auto; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 18px; }
.card { background: #161a24; border: 1px solid #2a3040; border-radius: 10px; padding: 14px 16px; }
.card .label { color: #8b93a7; font-size: 12px; }
.card .value { font-size: 22px; font-weight: 600; margin-top: 4px; }
.panel { background: #161a24; border: 1px solid #2a3040; border-radius: 10px; padding: 14px 16px; margin-bottom: 18px; }
.panel h2 { font-size: 15px; margin: 0 0 10px; }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #232939; vertical-align: top; }
th { color: #8b93a7; font-weight: 500; }
code, pre { font-family: ui-monospace, Consolas, monospace; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }
.ok { background: #163b24; color: #5fd68a; }
.warn { background: #423516; color: #e7c15f; }
.bad { background: #471b1b; color: #f07a7a; }
.muted { color: #8b93a7; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 12px; }
label { display: block; font-size: 12.5px; margin: 8px 0 3px; color: #b8bfd0; }
input[type=number], input[type=text] { width: 100%; box-sizing: border-box; background: #0f1117; border: 1px solid #2a3040; color: #e6e9f0; border-radius: 6px; padding: 7px 9px; }
.checks { display: flex; flex-wrap: wrap; gap: 8px 16px; }
.checks label { display: inline-flex; align-items: center; gap: 6px; margin: 4px 0; }
pre#plan-result { background: #0f1117; border: 1px solid #2a3040; border-radius: 8px; padding: 10px; overflow: auto; max-height: 360px; white-space: pre-wrap; }
.auto { margin-left: auto; }
</style>
</head>
<body>
<header>
  <h1>MarketListener 数据生产控制中心</h1>
  <span class="meta" id="generated-at">等待数据…</span>
  <a href="/industry/" style="color:#4a82f2;font-size:13px;text-decoration:none;">产业链图谱</a>
  <div class="auto">
    <button id="refresh">立即刷新</button>
    <label style="display:inline-flex; align-items:center; gap:6px; margin:0 0 0 12px;">
      <input type="checkbox" id="auto-refresh" checked> 60 秒自动刷新
    </label>
  </div>
</header>
<main>
  <section class="cards" id="cards"></section>
  <section class="panel">
    <h2>行情数据覆盖（K线入库）</h2>
    <div class="muted" id="coverage-meta" style="margin-bottom:8px;">加载中...</div>
    <div style="overflow-x:auto;"><table id="coverage-table"></table></div>
  </section>
  <section class="panel">
    <h2>数据采集任务进展（最新会话）</h2>
    <div class="muted" id="fetch-session-meta" style="margin-bottom:8px;">尚未运行采集会话。</div>
    <div style="overflow-x:auto;"><table id="fetch-tasks-table"></table></div>
  </section>
  <section class="panel">
    <h2>Android 同步包</h2>
    <div class="muted" id="android-package-panel">正在加载...</div>
    <div style="margin-top:12px;">
      <a id="android-package-download" href="/api/android-package" download style="display:none;">下载最新同步包</a>
    </div>
    <div class="muted" style="margin-top:10px;font-size:12px;">
      手机端“行情”页输入电脑局域网地址即可同步，例如 http://192.168.1.88:8765
    </div>
  </section>
  <section class="panel">
    <h2>F10 基础资料采集（A/H 股）</h2>
    <div class="muted" id="f10-panel" style="margin-bottom:8px;">正在加载...</div>
    <div style="overflow-x:auto;"><table id="f10-table"></table></div>
  </section>
  <div class="grid2">
    <section class="panel">
      <h2>数据源运行状态机（抓取日志）</h2>
      <div style="overflow-x:auto;"><table id="runs-table"></table></div>
    </section>
    <section class="panel">
      <h2>分区入库（Silver）</h2>
      <div style="overflow-x:auto;"><table id="partitions-table"></table></div>
    </section>
  </div>
  <div class="grid2">
    <section class="panel">
      <h2>隔离区（洗数失败）</h2>
      <table id="quarantine-table"></table>
    </section>
    <section class="panel">
      <h2>存储占用</h2>
      <table id="storage-table"></table>
    </section>
  </div>
  <section class="panel">
    <h2>数据集登记（Data Catalog）</h2>
    <div style="overflow-x:auto;"><table id="datasets-table"></table></div>
  </section>
  <section class="panel">
    <h2>同步 / 导出筛选</h2>
    <form id="filter-form">
      <div class="grid2">
        <div>
          <label>市场（多选）</label>
          <div class="checks" id="market-checks"></div>
          <label>A 股总市值下限（亿元）</label>
          <input type="number" name="min_market_cap" min="0" step="any" placeholder="例如 200">
          <label>港股总市值下限（亿港元）</label>
          <input type="number" name="min_hk_market_cap" min="0" step="any" placeholder="例如 200">
          <label>当日成交额下限（亿元）</label>
          <input type="number" name="min_amount" min="0" step="any" placeholder="例如 100">
        </div>
        <div>
          <label>A 股成交额排名前 N</label>
          <input type="number" name="amount_rank_top_n" min="1" step="1" placeholder="例如 10">
          <label>期货主力沉淀资金下限（亿元）</label>
          <input type="number" name="min_futures_capital" min="0" step="any" placeholder="例如 20">
          <label>数据集（多选，留空=全部）</label>
          <div class="checks" id="dataset-checks"></div>
        </div>
      </div>
      <div style="margin-top: 14px;"><button type="submit">生成同步 / 导出计划</button></div>
    </form>
    <pre id="plan-result">尚未生成计划。</pre>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
function badge(status) {
  const value = String(status || "UNKNOWN");
  const cls = value === "PASS" || value === "COMPLETE" || value === "ACCEPTED" ? "ok"
    : value === "FAILED" || value === "BLOCKED" ? "bad" : "warn";
  return `<span class="badge ${cls}">${value}</span>`;
}
function cell(value) {
  const text = value === null || value === undefined ? "" : String(value);
  return `<td>${text.replace(/&/g, "&amp;").replace(/</g, "&lt;")}</td>`;
}
function renderStats(stats) {
  const items = [
    ["累计入库行数", stats.total_rows],
    ["分区数", stats.partition_count],
    ["运行次数", stats.run_count],
    ["失效/阻塞源", stats.failed_providers.length ? stats.failed_providers.join(", ") : "无"],
    ["隔离区问题", stats.quarantine_count],
    ["存储字节", stats.storage_bytes],
  ];
  $("cards").innerHTML = items.map(([label, value]) =>
    `<div class="card"><div class="label">${label}</div><div class="value">${value}</div></div>`).join("");
}
function renderCoverage(coverage) {
  if (!coverage || !coverage.available) {
    $("coverage-meta").textContent = "覆盖统计暂不可用。";
    $("coverage-table").innerHTML = `<tr><td class="muted">暂无数据</td></tr>`;
    return;
  }
  const groups = coverage.groups || [];
  $("coverage-meta").textContent =
    `共 ${coverage.total_instruments} 个标的、${coverage.total_rows} 行 K 线（按 silver 分区真实统计）`;
  $("coverage-table").innerHTML = groups.length
    ? `<tr><th>市场</th><th>资产类型</th><th>标的数</th><th>行数</th><th>周期数</th></tr>` +
      groups.map((g) => `<tr>${cell(g.market)}${cell(g.asset_type)}${cell(g.instruments)}${cell(g.rows)}${cell(g.periods)}</tr>`).join("")
    : `<tr><td class="muted" colspan="5">silver 目录暂无 parquet 分区</td></tr>`;
}
function renderRuns(runs) {
  $("runs-table").innerHTML = runs.length ? `<tr><th>来源</th><th>状态</th><th>开始</th><th>完成</th><th>详情</th></tr>` +
    runs.map((r) => `<tr><td>${r.provider}</td><td>${badge(r.status)}</td>${cell(r.started_at)}${cell(r.completed_at)}${cell(r.detail)}</tr>`).join("")
    : `<tr><td class="muted" colspan="5">暂无运行记录</td></tr>`;
}
function renderPartitions(partitions) {
  $("partitions-table").innerHTML = partitions.length ? `<tr><th>分区</th><th>行数</th><th>数据截止</th><th>状态</th><th>入库时间</th><th>陈旧</th></tr>` +
    partitions.map((p) => `<tr><td>${p.partition_id}</td>${cell(p.row_count)}${cell(p.data_cutoff)}<td>${badge(p.status)}</td>${cell(p.updated_at)}${cell(p.stale ? "是" : "否")}</tr>`).join("")
    : `<tr><td class="muted" colspan="6">暂无分区</td></tr>`;
}
function renderQuarantine(quarantine) {
  $("quarantine-table").innerHTML = quarantine.length ? `<tr><th>分区</th><th>问题数</th><th>阻断</th></tr>` +
    quarantine.map((q) => `<tr><td>${q.partition_id}</td>${cell(q.issue_count)}${cell(q.blocking ? "是" : "否")}</tr>`).join("")
    : `<tr><td class="muted" colspan="3">无隔离分区</td></tr>`;
}
function renderStorage(storage) {
  $("storage-table").innerHTML = Object.entries(storage).map(([key, value]) =>
    `<tr><td>${key}</td>${cell(value)}</tr>`).join("");
}
function renderFetchJobs(jobs) {
  const latest = jobs.latest;
  if (!latest) {
    $("fetch-session-meta").textContent = "尚未运行采集会话（运行 market_monitor fetch 后显示）。";
    $("fetch-tasks-table").innerHTML = `<tr><td class="muted" colspan="8">暂无任务记录</td></tr>`;
    return;
  }
  $("fetch-session-meta").textContent =
    `会话 ${latest.session_id}：${latest.status}，任务 ${latest.task_count}，通过 ${latest.passed}，` +
    `部分失败 ${latest.partial_failure}，失败 ${latest.failed}，阻塞 ${latest.blocked}，总行数 ${latest.total_rows}；` +
    `${latest.started_at} → ${latest.completed_at}`;
  const rows = jobs.tasks || [];
  $("fetch-tasks-table").innerHTML = rows.length
    ? `<tr><th>数据集</th><th>名称</th><th>来源</th><th>状态</th><th>行数</th><th>开始</th><th>完成</th><th>错误</th></tr>` +
      rows.map((t) => `<tr><td>${t.dataset_id}</td>${cell(t.dataset_name)}${cell(t.source)}<td>${badge(t.status)}</td>${cell(t.rows)}${cell(t.started_at)}${cell(t.completed_at)}${cell(t.error || t.detail || "")}</tr>`).join("")
    : `<tr><td class="muted" colspan="8">暂无任务记录</td></tr>`;
}
function renderDatasets(datasets) {
  $("datasets-table").innerHTML = `<tr><th>数据集</th><th>名称</th><th>市场</th><th>资产</th><th>频率</th><th>主键</th><th>同步策略</th></tr>` +
    datasets.map((d) => `<tr><td>${d.dataset_id}</td>${cell(d.dataset_name)}${cell(d.market)}${cell(d.asset_type)}${cell(d.frequency)}${cell((d.primary_key || []).join(", "))}${cell(d.sync_policy)}</tr>`).join("");
  $("dataset-checks").innerHTML = datasets.map((d) =>
    `<label><input type="checkbox" name="datasets" value="${d.dataset_id}" checked> ${d.dataset_id}</label>`).join("");
}
function renderAndroidPackage(info) {
  const panel = $("android-package-panel");
  const link = $("android-package-download");
  if (!info) {
    panel.textContent = "尚无已构建的同步包。请运行 market_monitor package 生成。";
    link.style.display = "none";
    return;
  }
  panel.innerHTML =
    `包 ${info.package_id}（${info.status}，${(info.package_bytes / 1024 / 1024).toFixed(1)} MB，构建于 ${info.built_at}）`;
  link.style.display = "inline-block";
}
function renderF10(f10) {
  const panel = $("f10-panel");
  if (!f10 || !f10.available) {
    panel.textContent = "尚无 F10 采集数据。请运行 market_monitor f10 --market CN/HK 开始抓取。";
    $("f10-table").innerHTML = "";
    return;
  }
  const markets = Object.entries(f10.markets || {});
  panel.textContent = "腾讯批量行情 + 东财 F10 公司概况（断点续抓、限速防封）。";
  $("f10-table").innerHTML = `<tr><th>市场</th><th>已抓详情</th><th>最新状态</th><th>开始</th><th>完成</th><th>失败</th></tr>` +
    markets.map(([market, item]) => {
      const summary = item.summary || {};
      return `<tr><td>${market}</td><td>${item.record_count}</td>` +
        `<td>${badge(summary.status || "RUNNING")}</td>` +
        `${cell(summary.started_at)}${cell(summary.completed_at)}${cell(summary.failed_codes)}</tr>`;
    }).join("");
}
function renderMarkets() {
  $("market-checks").innerHTML = ["CN", "HK", "GLOBAL"].map((m) =>
    `<label><input type="checkbox" name="markets" value="${m}" checked> ${m}</label>`).join("");
}
async function loadHealth() {
  try {
    const report = await (await fetch("/api/health")).json();
    $("generated-at").textContent = "生成时间：" + report.generated_at;
    renderStats(report.stats);
    renderRuns(report.runs);
    renderPartitions(report.partitions);
    renderQuarantine(report.quarantine);
    renderStorage(report.storage);
    renderFetchJobs(report.fetch_jobs);
    renderAndroidPackage(report.android_package);
    renderCoverage(report.coverage);
    renderF10(report.f10);
  } catch (error) {
    $("generated-at").textContent = "健康数据加载失败：" + error;
  }
}
async function loadDatasets() {
  try {
    renderDatasets(await (await fetch("/api/datasets")).json());
  } catch (error) {
    $("datasets-table").innerHTML = `<tr><td class="muted">数据集加载失败：${error}</td></tr>`;
  }
}
$("refresh").addEventListener("click", loadHealth);
$("filter-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const params = new URLSearchParams(new FormData(event.target));
  try {
    const response = await fetch("/api/sync-plan?" + params.toString());
    const plan = await response.json();
    $("plan-result").textContent = JSON.stringify(plan, null, 2);
  } catch (error) {
    $("plan-result").textContent = "计划生成失败：" + error;
  }
});
renderMarkets();
loadHealth();
loadDatasets();
setInterval(() => { if ($("auto-refresh").checked) loadHealth(); }, 60000);
</script>
</body>
</html>
"""
