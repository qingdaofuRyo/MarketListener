"""Export a read-only MarketListener static website snapshot.

The generated folder keeps the production Vue assets and visual styling, while
replacing the local API with a small embedded response set.  It can therefore
be carried to another computer without the Python backend or data database.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WEB_DIST = ROOT / "desktop" / "src" / "market_monitor" / "web_dist"
MAX_ROWS = 10
DATA_VIEWS = (
    "market",
    "silver",
    "gold",
    "f10",
    "industry",
    "runs",
    "partitions",
    "quarantine",
    "package",
    "storage",
    "quality",
    "freshness",
)


def _fetch_json(base_url: str, path: str, **params: Any) -> Any:
    query = {key: value for key, value in params.items() if value not in (None, "")}
    url = f"{base_url.rstrip('/')}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    try:
        with urllib.request.urlopen(url, timeout=12) as response:  # noqa: S310 - explicit local URL
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as error:
        print(f"warning: unable to capture {path}: {error}", file=sys.stderr)
        return {}


def _limit(value: Any) -> Any:
    """Limit every displayed collection to ten records, recursively."""

    if isinstance(value, list):
        return [_limit(item) for item in value[:MAX_ROWS]]
    if isinstance(value, dict):
        return {str(key): _limit(item) for key, item in value.items()}
    return value


def _items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("items")
    return [row for row in rows if isinstance(row, dict)][:MAX_ROWS] if isinstance(rows, list) else []


def _local_industry_rows() -> list[dict[str, Any]]:
    """Read ten chain headings directly instead of asking the server to reparse Atlas."""

    for candidate in (ROOT / "data_control" / "industry" / "industry-atlas.json", ROOT / "reports" / "industry" / "industry-atlas.json"):
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        chains = document.get("chains") if isinstance(document, dict) else None
        if isinstance(chains, list):
            return [
                {
                    "chain": chain.get("name"),
                    "stages": len(chain.get("stages") or []),
                    "subChains": len(chain.get("sub_chains") or []),
                }
                for chain in chains[:MAX_ROWS]
                if isinstance(chain, dict)
            ]
    return []


def _fallback_health(instrument_total: int) -> dict[str, Any]:
    """Build a compact local report when the running server is busy."""

    catalog = ROOT / "data_control" / "catalog.duckdb"
    partitions: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    total_rows = 0
    try:
        import duckdb

        connection = duckdb.connect(str(catalog), read_only=True)
        try:
            total_rows = int(connection.execute("SELECT coalesce(sum(row_count), 0) FROM partitions").fetchone()[0])
            partition_columns = ["partition_id", "file_path", "row_count", "data_cutoff", "sha256", "source_run_id", "status", "updated_at"]
            partitions = [dict(zip(partition_columns, row, strict=True)) for row in connection.execute("SELECT * FROM partitions ORDER BY updated_at DESC LIMIT 10").fetchall()]
            run_columns = ["run_id", "provider", "status", "started_at", "completed_at", "detail"]
            runs = [dict(zip(run_columns, row, strict=True)) for row in connection.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 10").fetchall()]
        finally:
            connection.close()
    except Exception:
        pass
    storage_bytes = sum(path.stat().st_size for path in (ROOT / "data_control").rglob("*") if path.is_file())
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "stats": {"run_count": len(runs), "partition_count": len(partitions), "total_rows": total_rows, "quarantine_count": 0, "storage_bytes": storage_bytes},
        "runs": runs,
        "partitions": partitions,
        "quarantine": [],
        "storage": {"data_control": storage_bytes},
        "coverage": {"available": True, "groups": [], "total_instruments": instrument_total, "total_rows": total_rows},
        "android_package": None,
    }


def _data_views(health: Any, companies: Any) -> dict[str, Any]:
    report = health if isinstance(health, dict) else {}
    coverage = report.get("coverage") if isinstance(report.get("coverage"), dict) else {}
    partitions = report.get("partitions") if isinstance(report.get("partitions"), list) else []
    quarantine = report.get("quarantine") if isinstance(report.get("quarantine"), list) else []
    storage = report.get("storage") if isinstance(report.get("storage"), dict) else {}
    package = report.get("android_package")
    industry_rows = _local_industry_rows()
    quality = [
        {"partition_id": row.get("partition_id"), "status": row.get("status"), "issue_count": 0, "stale": row.get("stale", False)}
        for row in partitions
        if isinstance(row, dict)
    ] + quarantine
    freshness = [
        {"partition_id": row.get("partition_id"), "updated_at": row.get("updated_at"), "data_cutoff": row.get("data_cutoff"), "stale": row.get("stale", False)}
        for row in partitions
        if isinstance(row, dict)
    ]
    return {
        "market": {"view": "market", "items": coverage.get("groups") or [], "total": len(coverage.get("groups") or [])},
        "silver": {"view": "silver", "items": partitions, "total": len(partitions)},
        "gold": {"view": "gold", "items": [], "total": 0},
        "f10": {"view": "f10", "items": _items(companies), "total": len(_items(companies))},
        "industry": {"view": "industry", "items": industry_rows, "total": len(industry_rows)},
        "runs": {"view": "runs", "items": report.get("runs") or [], "total": len(report.get("runs") or [])},
        "partitions": {"view": "partitions", "items": partitions, "total": len(partitions)},
        "quarantine": {"view": "quarantine", "items": quarantine, "total": len(quarantine)},
        "package": {"view": "package", "items": [package] if package else [], "total": 1 if package else 0},
        "storage": {"view": "storage", "items": [{"area": key, "bytes": value} for key, value in storage.items()], "total": len(storage)},
        "quality": {"view": "quality", "items": quality, "total": len(quality)},
        "freshness": {"view": "freshness", "items": freshness, "total": len(freshness)},
    }


def _capture(base_url: str) -> dict[str, Any]:
    health = _fetch_json(base_url, "/api/health")
    instruments = _fetch_json(base_url, "/api/market/instruments", page=1, pageSize=MAX_ROWS)
    if not health:
        health = _fallback_health(int(instruments.get("total") or 0) if isinstance(instruments, dict) else 0)
    companies = _fetch_json(base_url, "/api/f10/companies", page=1, page_size=MAX_ROWS, sort="name")
    data_views = _data_views(health, companies)
    report = health if isinstance(health, dict) else {}
    coverage = report.get("coverage") if isinstance(report.get("coverage"), dict) else {}
    coverage_groups = coverage.get("groups") if isinstance(coverage.get("groups"), list) else []
    overview = {
        "instruments": coverage.get("total_instruments", 0),
        "rows": coverage.get("total_rows", 0),
        "markets": {
            str(row.get("market")): int(row.get("instruments") or 0)
            for row in coverage_groups
            if isinstance(row, dict) and row.get("market")
        },
        "assetTypes": {
            str(row.get("asset_type")): int(row.get("instruments") or 0)
            for row in coverage_groups
            if isinstance(row, dict) and row.get("asset_type")
        },
        "periods": ["1d"],
    }
    groups = {
        "items": [
            {
                "categoryKey": f"{row.get('market', '')}-{row.get('asset_type', '')}",
                "market": row.get("market"),
                "assetType": row.get("asset_type"),
                "period": "1d",
                "instruments": row.get("instruments"),
                "rows": row.get("rows"),
                "sources": [],
                "quality": {},
            }
            for row in coverage_groups
            if isinstance(row, dict)
        ]
    }
    return _limit(
        {
            "capturedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "health": health,
            "operations": {"items": []},
            "market": {
                "overview": overview,
                "instruments": instruments,
                "groups": groups,
                "bars": {},
                "watchlist": {"items": []},
            },
            "f10": {"companies": companies, "details": {}},
            "dataSources": {"items": [], "preferences": {}},
            "logs": {"items": [], "total": 0},
            "dataViews": data_views,
            "dashboard": {
                "definitions": {"items": []},
                "payloads": {},
                "personal": {"panels": []},
                "rankings": {},
                "heatmaps": {},
            },
            "strategy": {
                "definitions": {"items": [], "total": 0},
                "history": {"items": [], "total": 0, "limit": MAX_ROWS},
            },
            "stats": {
                "summary": {"available": False},
                "trades": {"items": [], "total": 0},
                "positions": {"items": [], "total": 0},
            },
        }
    )


def _static_runtime(snapshot: dict[str, Any]) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).replace("</script", "<\\/script")
    return f"""/* Generated MarketListener offline data adapter. */
window.__MARKETLISTENER_STATIC_SNAPSHOT__={payload};
(() => {{
  const snapshot = window.__MARKETLISTENER_STATIC_SNAPSHOT__;
  const originalFetch = window.fetch.bind(window);
  const json = (value, status = 200) => new Response(JSON.stringify(value), {{ status, headers: {{ "Content-Type": "application/json; charset=utf-8" }} }});
  const readonly = () => json({{ detail: "这是只读静态快照，不能执行更新或写入操作。" }}, 405);
  const list = (value) => Array.isArray(value) ? value.slice(0, {MAX_ROWS}) : [];
  const lowered = (value) => String(value ?? "").toLowerCase();
  const marketRows = () => list(snapshot.market?.instruments?.items);
  const marketList = (url) => {{
    const q = lowered(url.searchParams.get("q"));
    const market = lowered(url.searchParams.get("market"));
    const assetType = lowered(url.searchParams.get("assetType"));
    const rows = marketRows().filter((row) => (!q || Object.values(row).some((value) => lowered(value).includes(q))) && (!market || lowered(row.market) === market) && (!assetType || lowered(row.assetType) === assetType));
    const items = rows.length ? rows : (!q && !market && !assetType ? marketRows() : []);
    return {{ items, total: items.length, page: 1, pageSize: {MAX_ROWS} }};
  }};
  const companyList = (url) => {{
    const q = lowered(url.searchParams.get("q"));
    const market = lowered(url.searchParams.get("market"));
    const rows = list(snapshot.f10?.companies?.items).filter((row) => (!q || Object.values(row).some((value) => lowered(value).includes(q))) && (!market || lowered(row.market) === market));
    return {{ items: rows, total: rows.length, page: 1, pageSize: {MAX_ROWS} }};
  }};
  const route = (url) => {{
    const path = url.pathname;
    if (path === "/api/health") return snapshot.health || {{}};
    if (path === "/api/operations") return snapshot.operations || {{ items: [] }};
    if (path === "/api/market/overview") return snapshot.market?.overview || {{}};
    if (path === "/api/market/groups") return snapshot.market?.groups || {{ items: [] }};
    if (path === "/api/market/instruments") return marketList(url);
    const barMatch = path.match(/^\\/api\\/market\\/instruments\\/([^/]+)\\/bars$/);
    if (barMatch) {{
      const id = decodeURIComponent(barMatch[1]);
      const stored = snapshot.market?.bars?.[id];
      if (stored) return stored;
      return {{ instrumentId: id, period: "1d", availablePeriods: [], bars: [], total: 0 }};
    }}
    if (path === "/api/personal/watchlist") return snapshot.market?.watchlist || {{ items: [] }};
    if (path === "/api/f10/companies") return companyList(url);
    const companyMatch = path.match(/^\\/api\\/f10\\/companies\\/([^/]+)$/);
    if (companyMatch) return snapshot.f10?.details?.[decodeURIComponent(companyMatch[1])] || {{ detail: "静态快照中没有该公司资料" }};
    if (path === "/api/data-sources") return snapshot.dataSources || {{}};
    if (path === "/api/logs") return snapshot.logs || {{ items: [], total: 0 }};
    const dataMatch = path.match(/^\\/api\\/data\\/([^/]+)$/);
    if (dataMatch) return snapshot.dataViews?.[decodeURIComponent(dataMatch[1])] || {{ items: [], total: 0 }};
    if (path === "/api/dashboard/definitions") return snapshot.dashboard?.definitions || {{ items: [] }};
    if (path === "/api/personal/dashboard") return snapshot.dashboard?.personal || {{ panels: [] }};
    const dashboardMatch = path.match(/^\\/api\\/dashboard\\/([^/]+)$/);
    if (dashboardMatch) return snapshot.dashboard?.payloads?.[decodeURIComponent(dashboardMatch[1])] || {{ available: false }};
    if (path === "/api/metrics/ranking") return snapshot.dashboard?.rankings?.[url.searchParams.get("category") || "futures"] || {{ available: false, frames: [] }};
    if (path === "/api/metrics/heatmap") return snapshot.dashboard?.heatmaps?.[url.searchParams.get("category") || "breadth"] || {{ available: false, x: [], y: [], cells: [] }};
    if (path === "/api/strategy/definitions") return snapshot.strategy?.definitions || {{ items: [] }};
    if (path === "/api/strategy/history") return snapshot.strategy?.history || {{ items: [] }};
    if (path === "/api/stats/summary") return snapshot.stats?.summary || {{ available: false }};
    if (path === "/api/stats/trades") return snapshot.stats?.trades || {{ items: [], total: 0 }};
    if (path === "/api/stats/positions") return snapshot.stats?.positions || {{ items: [], total: 0 }};
    return null;
  }};
  window.fetch = async (input, init = {{}}) => {{
    const raw = input instanceof Request ? input.url : String(input);
    let url;
    try {{ url = new URL(raw, "https://marketlistener.static"); }} catch {{ return originalFetch(input, init); }}
    if (!url.pathname.startsWith("/api/")) return originalFetch(input, init);
    const method = String(init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (method !== "GET") return readonly();
    const value = route(url);
    return value === null ? json({{ detail: "静态快照没有该接口的数据" }}, 404) : json(value);
  }};
}})();
"""


def _patch_assets(output: Path) -> None:
    index = output / "index.html"
    content = index.read_text(encoding="utf-8")
    content = content.replace('src="/assets/', 'src="./assets/').replace('href="/assets/', 'href="./assets/')
    content = content.replace("<head>", '<head>\n    <script src="./snapshot-data.js"></script>', 1)
    index.write_text(content, encoding="utf-8")

    main_asset = next((output / "assets").glob("index-*.js"), None)
    api_asset = next((output / "assets").glob("api-*.js"), None)
    industry_asset = next((output / "assets").glob("IndustryView-*.js"), None)
    if not main_asset or not api_asset or not industry_asset:
        raise RuntimeError("expected production Vue assets were not found")
    main = main_asset.read_text(encoding="utf-8")
    main = main.replace("history:g1(),routes:", "history:g1(`#`),routes:", 1)
    # This helper runs inside assets/index-*.js, so its dynamic CSS URLs must
    # climb one level back to the static folder rather than duplicate assets/.
    main = main.replace("o0=function(e){return`/`+e}", "o0=function(e){return`../`+e}", 1)
    main_asset.write_text(main, encoding="utf-8")

    api = api_asset.read_text(encoding="utf-8")
    api = api.replace(
        "new URL(n,window.location.origin)",
        "new URL(n,window.location.origin===`null`?`https://marketlistener.static`:window.location.origin)",
        1,
    )
    api_asset.write_text(api, encoding="utf-8")

    industry = industry_asset.read_text(encoding="utf-8").replace("src:`/api/industry/atlas`", "src:`./industry-atlas.html`", 1)
    industry_asset.write_text(industry, encoding="utf-8")


def _write_atlas_wrapper(output: Path, atlas_image: Path | None) -> None:
    if atlas_image and atlas_image.is_file():
        shutil.copy2(atlas_image, output / "industry-atlas.png")
    else:
        raise FileNotFoundError("an industry atlas PNG is required; provide --atlas-image")
    (output / "industry-atlas.html").write_text(
        """<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>产业链图谱（静态快照）</title><style>html,body{height:100%;margin:0;background:#f5f7fa}body{display:flex;align-items:flex-start;justify-content:center}img{display:block;width:100%;height:auto;object-fit:contain}</style></head><body><img src=\"./industry-atlas.png\" alt=\"产业链图谱静态快照\"></body></html>""",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a read-only static MarketListener website folder")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765", help="running local MarketListener backend URL")
    parser.add_argument("--output", type=Path, help="target directory (default: exports/MarketListener-静态网站-<timestamp>)")
    parser.add_argument("--atlas-image", type=Path, required=True, help="PNG screenshot of the industry atlas")
    args = parser.parse_args()
    if not WEB_DIST.is_dir():
        raise FileNotFoundError("production web assets are missing; run npm run build in desktop/web first")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = args.output or ROOT / "exports" / f"MarketListener-静态网站-{timestamp}"
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    snapshot = _capture(args.base_url)
    shutil.copytree(WEB_DIST, output)
    _patch_assets(output)
    _write_atlas_wrapper(output, args.atlas_image)
    (output / "snapshot-data.js").write_text(_static_runtime(snapshot), encoding="utf-8")
    (output / "说明.txt").write_text(
        "MarketListener 静态网站快照\n\n"
        "打开方式：优先双击“打开网站.cmd”。该脚本只让 Edge 读取本文件夹，不会启动或连接 MarketListener 后端。\n"
        "数据：每类列表最多保留 10 条；产业链图谱为静态图片；所有写入、更新、任务执行功能均为只读。\n"
        f"快照生成时间：{snapshot['capturedAt']}\n",
        encoding="utf-8",
    )
    (output / "打开网站.cmd").write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        "set \"SITE=%~dp0index.html\"\r\n"
        "set \"EDGE=%ProgramFiles(x86)%\\Microsoft\\Edge\\Application\\msedge.exe\"\r\n"
        "if not exist \"%EDGE%\" set \"EDGE=%ProgramFiles%\\Microsoft\\Edge\\Application\\msedge.exe\"\r\n"
        "if exist \"%EDGE%\" (start \"MarketListener 静态快照\" \"%EDGE%\" --allow-file-access-from-files \"%SITE%\") else (start \"MarketListener 静态快照\" \"%SITE%\")\r\n"
        "endlocal\r\n",
        encoding="utf-8",
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
