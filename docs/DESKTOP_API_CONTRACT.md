# MarketListener 桌面终端 API 契约

更新日期：2026-08-24。R3 新增端点和行为以本节及 FastAPI 生成的 `/openapi.json` 为准；下方早期示例继续说明基础响应结构。

本文档定义 `desktop/src/market_monitor/web_api/` 下各受控路由的稳定契约。
所有 mutation 路由（POST/PUT/PATCH/DELETE）由 `web_app.py` 的 loopback 中间件保护，
只允许 `127.0.0.1/::1` 调用；服务端不得执行任意 SQL、shell 或 Python 代码。

## 通用规则

- JSON 输出递归清理 `NaN/Infinity` 为 `null`，禁止泄漏 `undefined/Invalid Date`。
- 分页参数 `page >= 1`、`page_size <= 500`；K 线 `limit <= 5000`。
- 所有数据都来自本地 `data_control`（silver parquet、catalog.duckdb、JSON/JSONL），
  不访问第三方行情/F10 网站。
- 事件写入 `data_control/logs/events-YYYY-MM-DD.jsonl`（`event_log.EventLog`）。
- 响应可带 `dataVersion`；浏览器持久缓存必须按版本失效，不能把导入过程中的临时名称长期缓存。
- 本地查询优先使用 `state/kline_query.duckdb` 文件清单和有界窗口；权威行情仍是 Silver Parquet。

## R3 路由总览

| 前缀 | 主要端点 | 边界 |
| --- | --- | --- |
| `/api/market` | `overview`、`cache-status`、`groups`、`categories`、`instruments`、bars meta/history/batch、`chart`、`indicator-series`、drawings index/batch/read/write/delete | 只读行情来自本地；画线写入个人目录且仅 loopback。 |
| `/api/data-sources` | `providers`、`inventory`、`tdx-local-normalization`、根路径 GET/PUT | 清单、字段、TDX 标准化审计和路由配置；不在页面请求中探测外网 Provider。 |
| `/api/personal` | `watchlist`、`dashboard` | 本机个人配置；写操作仅 loopback。 |
| `/api/stats` | accounts/trash/analysis、snapshots/cashflows/fills/strategy-uses、CSV、summary、trades/positions、strategy-ledger/performance | 个人交易和账户数据，不写入 Silver。 |
| `/api/strategy` | definitions CRUD/mark、indicators/conditions/functions、formula/condition validate、matches、validate/run/history | 只允许白名单 DSL/公式；禁止任意 Python、文件和网络访问。 |
| `/api/dashboard`、`/api/metrics` | 可配置面板及本地指标 | 只读取本地标准化/派生数据。 |

### 行情分页与游标

- 标的列表使用 `page/pageSize`；服务端限制最大页面大小。
- K 线历史优先使用 `before + limit` 的稳定时间游标；结果按时间升序，`before` 指向当前窗口最早 K 线，`hasMore` 表示是否仍有更早数据。
- 可见卡片使用 `/api/market/instruments/bars/batch`，避免每张卡片独立扫描全库。
- `chart` 同时返回 bars、可用周期、指标序列、画线和数据版本，供详情页首屏启动。

### 画线文档

- 图形类型为 `horizontal/vertical/rectangle/text`；点包含时间和价格，样式包含颜色、宽度、线型、填充、文字和锁定等受控字段。
- 后端保存图形实例；浏览器保存“下次新建图形”的默认样式与吸附/跨周期/连续画线偏好。
- 批量读取用于列表/卡片只读显示；批量删除必须显式提供受约束的标的或图形范围。

### 数据源清单

- `/providers` 仅表示代码中注册的能力与配置状态，不等于真实可用或已入库。
- `/inventory` 基于 catalog/Parquet/查询清单返回本地物理表、数据集、字段样本、来源、质量和时间范围。
- `/tdx-local-normalization` 返回最近一次 `tdx-cn-v2` 审计的版本、扫描、正式写入、隔离和异常统计；报告不存在时显式返回不可用，不伪造覆盖率。
- TickDB 已从活动数据源移除；历史审计文件不得作为 Provider 或回退来源显示。

## /api/market

### GET /api/market/overview

返回：

```json
{
  "generatedAt": "2026-08-09T00:00:00+00:00",
  "instruments": 48,
  "rows": 72323,
  "markets": {"CN": 48},
  "assetTypes": {"ETF": 48},
  "periods": ["1d", "30m"],
  "latestBarAt": "2026-08-09T00:00:00+08:00"
}
```

### GET /api/market/instruments

查询参数：`market`、`q`、`page`、`pageSize`（默认 50，最大 500）。

```json
{
  "items": [
    {
      "instrumentId": "CN.SSE.ETF.510300",
      "symbol": "510300",
      "name": "沪深300ETF",
      "market": "CN",
      "assetType": "ETF",
      "period": "1d",
      "lastClose": 3.983,
      "lastBarAt": "2025-05-19T00:00:00+08:00",
      "source": "pytdx",
      "qualityStatus": "PASS",
      "updatedAt": "2026-08-09T14:14:15+00:00"
    }
  ],
  "total": 48,
  "page": 1,
  "pageSize": 50
}
```

### GET /api/market/instruments/{instrument_id}/bars

查询参数：`period`（`1d`/`30m` 等，从 inventory 校验）、`limit`（默认 1000，最大 5000）。
返回升序 K 线：

```json
{
  "instrumentId": "CN.SSE.ETF.510300",
  "period": "1d",
  "bars": [
    {
      "barOpenTime": "2025-05-19T00:00:00+08:00",
      "open": 3.984,
      "high": 3.988,
      "low": 3.965,
      "close": 3.983,
      "volume": 556298112,
      "amount": 2212057856,
      "source": "pytdx",
      "qualityStatus": "PASS"
    }
  ],
  "total": 1000,
  "lastBarAt": "2025-05-19T00:00:00+08:00"
}
```

## /api/personal/watchlist

持久化：`data_control/personal/watchlist.json`。

### GET /api/personal/watchlist

```json
{"items": [{"instrumentId": "CN.SSE.ETF.510300", "addedAt": "...", "note": ""}]}
```

### POST /api/personal/watchlist

Body（`extra="forbid"`）：`{"instrumentId": "...", "note": ""}`。
`instrumentId` 必须存在于 silver inventory，否则 400。重复添加返回已存在条目。

### DELETE /api/personal/watchlist/{instrument_id}

删除不存在的条目返回 404。

## /api/strategy

策略定义存于 `data_control/strategies/definitions/*.json`（Strategy DSL v1 文档）；
运行记录由 `strategy_dsl.scanner.write_run_record()` 写入
`data_control/strategies/runs/{run_id}.json`。

### GET /api/strategy/definitions

```json
{"items": [{"strategyId": "...", "strategyVersion": "1", "inputs": ["close"], "parameters": {...}, "updatedAt": "..."}]}
```

### POST /api/strategy/validate

Body：完整 DSL 文档。合法返回 200 `{"valid": true, "strategyId": "...", "inputs": [...], "parameters": {...}}`；
非法返回 400 `{"detail": {"kind": "...", "message": "..."}}`。只校验不写盘。

### POST /api/strategy/run

Body（`extra="forbid"`）：

```json
{"strategyId": "demo", "parameters": {}, "period": "1d", "limitInstruments": 200, "limitPerInstrument": 500, "timeoutSeconds": 2.0, "maxOps": 500000}
```

从磁盘加载定义（不允许内联任意文档），对 silver 每标的执行 `scan_strategy`，
写运行记录，返回 `{report, signals}`；`signals` 每标的最多 50 条。

### GET /api/strategy/history

参数 `limit`（默认 50，最大 200）。按运行时间倒序返回运行记录摘要。

## /api/stats

账本：`data_control/personal/ledger.jsonl`，格式与 Android 导入兼容：
首行 `{"type": "header", "source_label": "..."}`，随后 `type` 为
`strategy`/`trade`/`cash`。trade 字段：
`instrument_id`、`side(BUY|SELL)`、`quantity`、`price`、`executed_at`、
`fees[{"kind","amount"}]`、`strategy_id`、`order_group_id`、`note`。
cash `kind`：DEPOSIT/WITHDRAWAL/DIVIDEND/TAX_REFUND/OTHER。

### GET /api/stats/summary

```json
{
  "available": true,
  "navCurve": [{"t": "...", "nav": 100000}],
  "totalReturnPct": 1.2,
  "maxDrawdownPct": -2.3,
  "winRatePct": 55.5,
  "profitFactor": 1.8,
  "grossProfit": 1000,
  "grossLoss": 550,
  "feesTotal": 12,
  "realizedTotal": 450,
  "averageExposurePct": null,
  "maxExposurePct": null,
  "realizedByStrategy": {},
  "realizedByInstrument": {},
  "generatedAt": "..."
}
```

无账本时 `available: false`，数值字段为 `null`（不允许伪造 0 为“正常数据”）。

### GET /api/stats/trades

分页返回 trade 条目（`pageSize <= 500`）。

### GET /api/stats/positions

```json
{"items": [{"instrumentId": "...", "quantity": 100, "averageCost": 3.9, "marketValue": 390, "unrealizedPnl": 8.3, "updatedAt": "..."}], "total": 1}
```

### POST /api/stats/import

Body（`extra="forbid"`）：`{"lines": [{"type": "trade", ...}]}`，最多 10000 条。
逐行校验后追加到 ledger.jsonl，返回 `{"imported": n, "skipped": m, "total": n+m}`。

### GET /api/stats/export

返回 `text/plain` JSONL 完整账本。

## /api/dashboard 与 /api/metrics

### GET /api/dashboard/definitions

基于真实数据可用性返回面板：

```json
{"items": [{"id": "market-breadth", "title": "市场广度", "category": "breadth", "available": true, "description": "..."}]}
```

允许的 id：`market-breadth`、`futures-breadth`、`gold-metrics`、
`storage`、`quality`、`freshness`、`runs`、`partitions`。

### GET /api/dashboard/{id}

```json
{
  "id": "market-breadth",
  "title": "市场广度",
  "unit": "家数",
  "series": [{"name": "上涨", "points": [{"t": "...", "value": 3000}]}],
  "generatedAt": "...",
  "source": "local-computed"
}
```

每序列最多 1000 点，服务端降采样；无数据返回 `{"available": false}`。

### GET /api/metrics/ranking

参数 `category`（`futures`/`gold`/`breadth`）、`limit`（默认 20，最大 100）。
返回真实排名帧：

```json
{"category": "futures", "frames": [{"t": "...", "items": [{"name": "rb", "value": 123}]}]}
```

没有真实数据时 `{"available": false, "frames": []}`，禁止伪造排名。

### GET /api/metrics/heatmap

参数 `category`、`limit`。返回：

```json
{"category": "breadth", "available": true, "x": ["2026-08-01"], "y": ["上涨"], "cells": [{"x": 0, "y": 0, "value": 3000}]}
```

只有存在真实序列时返回数据，否则 `available: false`。
