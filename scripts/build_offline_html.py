"""生成可在另一台电脑直接打开的 MarketListener 单文件只读快照。"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb


BEIJING = ZoneInfo("Asia/Shanghai")


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _repair_text(value: str) -> str:
    """修复旧产业链导出中 UTF-8 被当作 latin-1 保存的文本。"""
    try:
        repaired = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    markers = ("Ã", "å", "ä", "æ", "ç", "ï")
    return repaired if sum(value.count(item) for item in markers) > sum(repaired.count(item) for item in markers) else value


def _compact_chain(chain: dict[str, Any]) -> dict[str, Any]:
    stages = []
    for stage in chain.get("stages", []):
        cards = stage.get("cards", [])
        stages.append(
            {
                "label": _repair_text(str(stage.get("label", ""))),
                "cards": [
                    {
                        "name": _repair_text(str(card.get("name", ""))),
                        "kind": card.get("kind", ""),
                        "count": card.get("count", 0),
                        "companies": len(card.get("companyRefs", [])),
                    }
                    for card in cards[:30]
                ],
            }
        )
    return {
        "id": chain.get("id", ""),
        "name": _repair_text(str(chain.get("name", ""))),
        "factCount": chain.get("fact_count", 0),
        "reportCount": chain.get("report_count", 0),
        "counts": chain.get("counts", {}),
        "stages": stages,
    }


def _read_logs(data_root: Path, limit: int = 500) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((data_root / "logs").glob("events-*.jsonl"), reverse=True):
        for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
            value = _json(line, None)
            if isinstance(value, dict):
                rows.append(value)
            if len(rows) >= limit:
                return rows
    return rows


def build_payload(data_root: Path, report_root: Path, history_rows: int) -> dict[str, Any]:
    catalog = data_root / "catalog.duckdb"
    if not catalog.is_file():
        raise FileNotFoundError(f"缺少 DuckDB 目录库：{catalog}")
    silver_glob = (data_root / "silver" / "**" / "*.parquet").as_posix().replace("'", "''")
    connection = duckdb.connect(str(catalog), read_only=True)
    try:
        bars = connection.execute(
            f"""
            WITH parsed AS (
              SELECT instrument_id, market, asset_type, bar_open_time,
                     json_extract_string(bar_json, '$.symbol') AS symbol,
                     json_extract_string(bar_json, '$.name') AS name,
                     try_cast(json_extract_string(bar_json, '$.open') AS DOUBLE) AS open,
                     try_cast(json_extract_string(bar_json, '$.high') AS DOUBLE) AS high,
                     try_cast(json_extract_string(bar_json, '$.low') AS DOUBLE) AS low,
                     try_cast(json_extract_string(bar_json, '$.close') AS DOUBLE) AS close,
                     try_cast(json_extract_string(bar_json, '$.pct_change') AS DOUBLE) AS pct_change,
                     try_cast(json_extract_string(bar_json, '$.volume') AS DOUBLE) AS volume,
                     try_cast(json_extract_string(bar_json, '$.amount') AS DOUBLE) AS amount,
                     json_extract_string(bar_json, '$.source') AS source,
                     json_extract_string(bar_json, '$.fetched_at') AS fetched_at
              FROM read_parquet('{silver_glob}', union_by_name=true)
              WHERE bar_period = '1d'
            ), deduplicated AS (
              SELECT * EXCLUDE duplicate_rank FROM (
                SELECT *, row_number() OVER (
                  PARTITION BY instrument_id, bar_open_time ORDER BY fetched_at DESC NULLS LAST
                ) AS duplicate_rank
                FROM parsed
              ) WHERE duplicate_rank = 1
            ), calculated AS (
              SELECT *, coalesce(
                pct_change,
                (close / nullif(lag(close) OVER (PARTITION BY instrument_id ORDER BY bar_open_time), 0) - 1) * 100
              ) AS display_pct
              FROM deduplicated
            ), ranked AS (
              SELECT *, row_number() OVER (PARTITION BY instrument_id ORDER BY bar_open_time DESC) AS recent_rank
              FROM calculated
            )
            SELECT instrument_id, market, asset_type, bar_open_time, symbol, name,
                   open, high, low, close, display_pct, volume, amount, source
            FROM ranked WHERE recent_rank <= ? ORDER BY instrument_id, bar_open_time
            """,
            [history_rows],
        ).fetchall()
        histories: dict[str, list[list[Any]]] = {}
        instruments: dict[str, dict[str, Any]] = {}
        for row in bars:
            instrument_id = row[0]
            histories.setdefault(instrument_id, []).append([row[3], *row[6:13]])
            instruments[instrument_id] = {
                "id": instrument_id,
                "market": row[1],
                "type": row[2],
                "symbol": row[4] or instrument_id.rsplit(".", 1)[-1],
                "name": row[5] or "暂无名称",
                "time": row[3],
                "open": row[6], "high": row[7], "low": row[8], "close": row[9],
                "pct": row[10], "volume": row[11], "amount": row[12], "source": row[13],
            }

        f10: dict[str, dict[str, Any]] = {}
        for row in connection.execute(
            """SELECT code, market, name, org_name, industry_em, industry_csrc,
                      total_market_cap_yi, float_market_cap_yi, profile, business_scope, record, fetched_at
               FROM f10_company ORDER BY market, code"""
        ).fetchall():
            record = _json(row[10], {})
            f10[f"{row[1]}:{row[0]}"] = {
                "code": row[0], "market": row[1], "name": row[2], "orgName": row[3],
                "industry": row[4] or row[5], "totalCap": row[6], "floatCap": row[7],
                "profile": row[8], "businessScope": row[9], "fetchedAt": row[11],
                "mainBusiness": record.get("main_business"), "products": record.get("products"),
                "position": record.get("company_position"), "highlight": record.get("company_highlight"),
                "revenue": (record.get("revenue_breakdown") or [])[:12],
            }

        gold = [
            {"id": row[0], "instrument": row[1], "date": row[2], "period": row[3], "name": row[4], "value": row[5], "method": row[6]}
            for row in connection.execute(
                """SELECT metric_id, instrument_id, trading_date, period, metric_name, value, calculation_method
                   FROM gold_metrics ORDER BY trading_date DESC, metric_id LIMIT 5000"""
            ).fetchall()
        ]
        runs = [
            {"id": row[0], "provider": row[1], "status": row[2], "started": row[3], "completed": row[4], "detail": row[5]}
            for row in connection.execute(
                "SELECT run_id, provider, status, started_at, completed_at, detail FROM runs ORDER BY started_at DESC LIMIT 200"
            ).fetchall()
        ]
        datasets = []
        for row in connection.execute("SELECT dataset_json, registered_at FROM datasets ORDER BY dataset_id").fetchall():
            value = _json(row[0], {})
            value["registeredAt"] = row[1]
            datasets.append(value)
        partition = connection.execute("SELECT count(*), coalesce(sum(row_count), 0), coalesce(sum(file_size), 0) FROM (SELECT row_count, 0 file_size FROM partitions)").fetchone()
    finally:
        connection.close()

    chains: list[dict[str, Any]] = []
    atlas = report_root / "industry" / "industry-atlas.json"
    if atlas.is_file():
        document = json.loads(atlas.read_text(encoding="utf-8"))
        chains = [_compact_chain(item) for item in document.get("chains", [])]

    items = sorted(instruments.values(), key=lambda item: (item["market"], item["type"], item["symbol"]))
    return {
        "meta": {
            "generatedAt": datetime.now(BEIJING).isoformat(timespec="seconds"),
            "historyRows": history_rows,
            "instrumentCount": len(items),
            "barCount": sum(len(value) for value in histories.values()),
            "f10Count": len(f10),
            "partitionCount": int(partition[0]),
            "catalogRows": int(partition[1]),
            "notice": "只读离线快照；数据截至导出时刻，不能更新、下单或执行采集任务。",
        },
        "instruments": items,
        "histories": histories,
        "f10": f10,
        "gold": gold,
        "runs": runs,
        "datasets": datasets,
        "logs": _read_logs(data_root),
        "chains": chains,
    }


HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MarketListener 离线快照</title><style>
:root{font-family:"Microsoft YaHei",system-ui,sans-serif;color:#273142;background:#f4f6f8}*{box-sizing:border-box}body{margin:0}.top{background:#17324d;color:#fff;padding:18px 28px;display:flex;align-items:center;gap:22px;position:sticky;top:0;z-index:5}.top h1{font-size:20px;margin:0}.badge{background:#d88732;border-radius:16px;padding:5px 12px;font-size:12px}.tabs{display:flex;gap:4px;flex-wrap:wrap}.tabs button{border:0;background:transparent;color:#d8e5f0;padding:8px 12px;border-radius:7px;cursor:pointer}.tabs button.active{background:#fff;color:#17324d}.wrap{max-width:1500px;margin:auto;padding:22px}.notice{background:#fff5db;border:1px solid #edcb7a;padding:12px 16px;border-radius:10px;margin-bottom:16px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}.card,.panel{background:#fff;border:1px solid #dfe5eb;border-radius:12px;padding:16px;box-shadow:0 2px 8px #1d334a0d}.card b{display:block;font-size:25px;color:#173f65;margin-top:8px}.panel{margin-top:14px}.controls{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px}input,select{border:1px solid #cbd5df;border-radius:7px;padding:9px 11px;background:#fff}input{min-width:260px}.table-wrap{overflow:auto;max-height:650px}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:9px 10px;border-bottom:1px solid #e8edf2;text-align:left;white-space:nowrap}th{position:sticky;top:0;background:#edf3f7;color:#395268}tr.click{cursor:pointer}tr.click:hover{background:#eef6ff}.up{color:#c62f37}.down{color:#198754}.muted{color:#718096;font-size:12px}.detail-grid{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(320px,1fr);gap:14px}.profile{white-space:pre-wrap;line-height:1.65;max-height:280px;overflow:auto}.chain-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}.tag{display:inline-block;background:#edf3f7;border-radius:13px;padding:4px 8px;margin:3px;font-size:12px}canvas{width:100%;height:300px;border:1px solid #edf1f5;border-radius:8px}.hidden{display:none}.error{max-width:760px;margin:80px auto;background:#fff;padding:28px;border-radius:12px;color:#b42318}@media(max-width:800px){.detail-grid{grid-template-columns:1fr}.top{position:static;align-items:flex-start;flex-direction:column}.wrap{padding:12px}}
</style></head><body><header class="top"><h1>MarketListener 离线快照</h1><span class="badge">只读 · 无需联网</span><nav class="tabs" id="tabs"></nav></header><main class="wrap"><div id="app"><div class="panel">正在解压本地数据…</div></div></main>
<script id="payload" type="application/octet-stream">__PAYLOAD__</script><script>
const $=(s,r=document)=>r.querySelector(s), esc=v=>String(v??'暂无数据').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=(v,d=2)=>Number.isFinite(Number(v))?Number(v).toLocaleString('zh-CN',{maximumFractionDigits:d}):'暂无数据';
const pct=v=>Number.isFinite(Number(v))?`${Number(v).toFixed(2)}%`:'暂无数据'; const cls=v=>Number(v)>0?'up':Number(v)<0?'down':'';
let D, page='首页', selected=null, list=[]; const names=['首页','行情','F10','指标','产业链','数据目录','任务日志'];
async function load(){try{if(!('DecompressionStream'in window))throw Error('当前浏览器不支持本地 gzip 解压，请使用新版 Edge 或 Chrome。');const raw=atob($('#payload').textContent.trim()),bytes=Uint8Array.from(raw,c=>c.charCodeAt(0)),stream=new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));D=JSON.parse(await new Response(stream).text());list=D.instruments;nav();show('首页')}catch(e){$('#app').innerHTML=`<div class="error"><h2>无法打开离线快照</h2><p>${esc(e.message)}</p></div>`}}
function nav(){const n=$('#tabs');n.innerHTML=names.map(x=>`<button data-p="${x}">${x}</button>`).join('');n.onclick=e=>e.target.dataset.p&&show(e.target.dataset.p)}
function show(p){page=p;document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('active',b.dataset.p===p));({首页:home,行情:market,F10:f10,指标:gold,产业链:chains,数据目录:datasets,任务日志:runs}[p])()}
function shell(body){$('#app').innerHTML=`<div class="notice">${esc(D.meta.notice)}　导出时间：${esc(D.meta.generatedAt.replace('T',' '))}</div>${body}`}
function home(){const m=D.meta, markets=Object.groupBy?Object.groupBy(D.instruments,x=>x.market):D.instruments.reduce((a,x)=>((a[x.market]??=[]).push(x),a),{});shell(`<div class="cards"><div class="card">标的数量<b>${num(m.instrumentCount,0)}</b></div><div class="card">嵌入 K 线<b>${num(m.barCount,0)}</b></div><div class="card">F10 公司<b>${num(m.f10Count,0)}</b></div><div class="card">数据分区<b>${num(m.partitionCount,0)}</b></div><div class="card">A 股/境内<b>${num((markets.CN||[]).length,0)}</b></div><div class="card">港股<b>${num((markets.HK||[]).length,0)}</b></div></div><section class="panel"><h2>快照说明</h2><p>此文件包含全部本地标的的最新行情和最近 ${m.historyRows} 根日 K 线、F10 摘要、Gold 指标、产业链摘要、数据目录、任务与日志。数据不会自动更新。</p><p class="muted">源数据目录未嵌入；写入操作、行情采集、策略运行、自选修改及 Android 包构建均不可用。</p></section>`)}
function filters(kind){return `<div class="controls"><input id="q" placeholder="搜索代码、名称或标的 ID"><select id="mk"><option value="">全部市场</option><option>CN</option><option>HK</option><option>GLOBAL</option></select><select id="tp"><option value="">全部类型</option><option>STOCK</option><option>ETF</option><option>INDEX</option><option>FUTURE</option></select><span class="muted" id="count"></span></div><div id="results"></div><div id="detail"></div>`}
function market(){shell(`<section class="panel"><h2>行情</h2>${filters('market')}</section>`);bind(false)}
function f10(){shell(`<section class="panel"><h2>F10 公司资料</h2>${filters('f10')}</section>`);bind(true)}
function bind(onlyF10){const update=()=>{const q=$('#q').value.trim().toLowerCase(),mk=$('#mk').value,tp=$('#tp').value;list=D.instruments.filter(x=>(!onlyF10||D.f10[`${x.market}:${x.symbol}`])&&(!mk||x.market===mk)&&(!tp||x.type===tp)&&(!q||`${x.symbol} ${x.name} ${x.id}`.toLowerCase().includes(q)));$('#count').textContent=`${list.length.toLocaleString()} 条（表格显示前 300 条）`;renderRows(list.slice(0,300),onlyF10)};['q','mk','tp'].forEach(id=>$('#'+id).addEventListener(id==='q'?'input':'change',update));update()}
function renderRows(rows,onlyF10){$('#results').innerHTML=`<div class="table-wrap"><table><thead><tr><th>市场</th><th>类型</th><th>代码</th><th>名称</th><th>${onlyF10?'行业':'日期'}</th><th>${onlyF10?'总市值（亿）':'收盘'}</th><th>${onlyF10?'更新时间':'涨跌幅'}</th></tr></thead><tbody>${rows.map(x=>{const f=D.f10[`${x.market}:${x.symbol}`];return `<tr class="click" data-id="${esc(x.id)}"><td>${esc(x.market)}</td><td>${esc(x.type)}</td><td>${esc(x.symbol)}</td><td>${esc(x.name)}</td><td>${esc(onlyF10?f?.industry:x.time?.slice(0,10))}</td><td>${num(onlyF10?f?.totalCap:x.close)}</td><td class="${onlyF10?'':cls(x.pct)}">${esc(onlyF10?f?.fetchedAt:pct(x.pct))}</td></tr>`}).join('')}</tbody></table></div>`;$('#results').onclick=e=>{const tr=e.target.closest('[data-id]');if(tr)detail(tr.dataset.id)}}
function detail(id){const x=D.instruments.find(v=>v.id===id),f=D.f10[`${x.market}:${x.symbol}`],bars=D.histories[id]||[];$('#detail').innerHTML=`<section class="panel"><h2>${esc(x.name)} <span class="muted">${esc(x.id)}</span></h2><div class="detail-grid"><div><canvas id="chart" width="900" height="300"></canvas><div class="table-wrap" style="max-height:260px"><table><thead><tr><th>日期</th><th>开</th><th>高</th><th>低</th><th>收</th><th>涨跌幅</th><th>成交量</th></tr></thead><tbody>${bars.slice().reverse().map(b=>`<tr><td>${esc(b[0].slice(0,10))}</td><td>${num(b[1])}</td><td>${num(b[2])}</td><td>${num(b[3])}</td><td>${num(b[4])}</td><td class="${cls(b[5])}">${pct(b[5])}</td><td>${num(b[6],0)}</td></tr>`).join('')}</tbody></table></div></div><div><h3>F10 摘要</h3>${f?`<p><b>${esc(f.orgName||f.name)}</b></p><p>行业：${esc(f.industry)}</p><p>总市值：${num(f.totalCap)} 亿　流通市值：${num(f.floatCap)} 亿</p><p>更新时间：${esc(f.fetchedAt)}</p><div class="profile">${esc(f.profile||f.businessScope)}</div>`:'<p class="muted">暂无 F10 数据</p>'}</div></div></section>`;draw(bars)}
function draw(bars){const c=$('#chart');if(!c||!bars.length)return;const g=c.getContext('2d'),v=bars.map(x=>Number(x[4])).filter(Number.isFinite),lo=Math.min(...v),hi=Math.max(...v),pad=24,w=c.width-pad*2,h=c.height-pad*2;g.clearRect(0,0,c.width,c.height);g.strokeStyle='#dbe4ec';g.strokeRect(pad,pad,w,h);g.beginPath();v.forEach((y,i)=>{const x=pad+(i/Math.max(1,v.length-1))*w,py=pad+h-(y-lo)/Math.max(.0001,hi-lo)*h;i?g.lineTo(x,py):g.moveTo(x,py)});g.strokeStyle='#2474a6';g.lineWidth=2;g.stroke();g.fillStyle='#587083';g.fillText(`最高 ${hi.toFixed(2)}`,pad+5,pad+14);g.fillText(`最低 ${lo.toFixed(2)}`,pad+5,pad+h-6)}
function gold(){shell(`<section class="panel"><h2>Gold 派生指标（最近 5,000 条）</h2><div class="controls"><input id="gq" placeholder="搜索指标或标的"></div><div id="gold"></div></section>`);const render=()=>{const q=$('#gq').value.toLowerCase(),rows=D.gold.filter(x=>!q||`${x.name} ${x.instrument} ${x.id}`.toLowerCase().includes(q)).slice(0,500);$('#gold').innerHTML=`<div class="table-wrap"><table><thead><tr><th>日期</th><th>指标</th><th>标的</th><th>周期</th><th>值</th><th>方法</th></tr></thead><tbody>${rows.map(x=>`<tr><td>${esc(x.date)}</td><td>${esc(x.name)}</td><td>${esc(x.instrument)}</td><td>${esc(x.period)}</td><td>${num(x.value,4)}</td><td>${esc(x.method)}</td></tr>`).join('')}</tbody></table></div>`};$('#gq').oninput=render;render()}
function chains(){shell(`<section class="panel"><h2>产业链摘要</h2><div class="controls"><input id="cq" placeholder="搜索产业链或环节"><span class="muted">${D.chains.length} 条展示链</span></div><div class="chain-grid" id="chains"></div></section>`);const render=()=>{const q=$('#cq').value.toLowerCase();$('#chains').innerHTML=D.chains.filter(c=>!q||JSON.stringify(c).toLowerCase().includes(q)).map(c=>`<article class="card"><h3>${esc(c.name)}</h3><p>${num(c.factCount,0)} 条事实 · ${num(c.reportCount,0)} 份报告</p>${c.stages.map(s=>`<div><b>${esc(s.label)}</b><br>${s.cards.slice(0,12).map(x=>`<span class="tag">${esc(x.name)} · ${num(x.companies,0)}家</span>`).join('')}</div>`).join('')}</article>`).join('')};$('#cq').oninput=render;render()}
function datasets(){shell(`<section class="panel"><h2>本地数据目录</h2><div class="table-wrap"><table><thead><tr><th>数据集</th><th>名称</th><th>市场</th><th>类型</th><th>周期</th><th>注册时间</th></tr></thead><tbody>${D.datasets.map(x=>`<tr><td>${esc(x.dataset_id)}</td><td>${esc(x.display_name||x.name)}</td><td>${esc(x.market)}</td><td>${esc(x.asset_type)}</td><td>${esc((x.periods||x.period||[]).toString())}</td><td>${esc(x.registeredAt)}</td></tr>`).join('')}</tbody></table></div></section>`)}
function runs(){shell(`<section class="panel"><h2>采集运行</h2><div class="table-wrap" style="max-height:360px"><table><thead><tr><th>开始时间</th><th>来源</th><th>状态</th><th>详情</th></tr></thead><tbody>${D.runs.map(x=>`<tr><td>${esc(x.started)}</td><td>${esc(x.provider)}</td><td>${esc(x.status)}</td><td>${esc(x.detail)}</td></tr>`).join('')}</tbody></table></div></section><section class="panel"><h2>事件日志（最近 500 条）</h2><div class="table-wrap"><table><thead><tr><th>时间</th><th>类别</th><th>操作</th><th>状态</th><th>详情</th></tr></thead><tbody>${D.logs.map(x=>`<tr><td>${esc(x.timestamp)}</td><td>${esc(x.category)}</td><td>${esc(x.operation)}</td><td>${esc(x.status)}</td><td>${esc(x.detail)}</td></tr>`).join('')}</tbody></table></div></section>`)}
load();
</script></body></html>'''


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 MarketListener 单文件只读离线快照")
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--report-root", type=Path, default=Path("reports"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--history-rows", type=int, default=90)
    args = parser.parse_args()
    if not 10 <= args.history_rows <= 500:
        parser.error("--history-rows 必须在 10 到 500 之间")
    generated = datetime.now(BEIJING)
    output = args.output or Path("exports") / f"MarketListener-离线快照-{generated:%Y%m%d-%H%M%S}.html"
    payload = build_payload(args.data_root.resolve(), args.report_root.resolve(), args.history_rows)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=9, mtime=0)
    document = HTML.replace("__PAYLOAD__", base64.b64encode(compressed).decode("ascii"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print(json.dumps({"output": str(output.resolve()), "bytes": output.stat().st_size, "sha256": digest, **payload["meta"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
