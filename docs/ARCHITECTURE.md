# MarketListener 当前架构

最后更新：2026-08-27。本文描述当前仓库的整体运行结构和 R3/R4 已实现能力；不可逆架构约束仍以 `ADR.md` 与 `docs/adr/` 为准，当前工作状态以根目录 `Plan_R4.md` 为准。

## 系统边界

```text
外部 Provider / 本地金融终端文件
              │
              ▼
桌面数据生产端（Python 3.11）
  采集 → 原始留存 → 标准化 → 质量检查 → Silver/Gold
              │                         │
              ├──────── FastAPI ────────┤
              │                         ▼
              │                  Vue 3 本地网页终端
              │
              └──── 签名行情包 ────────► Android 13+ 离线消费端
```

- 桌面端负责网络采集、本地文件解析、标准化、全市场聚合、策略研究、账户分析和行情包构建。
- Vue 网页端只访问本机 FastAPI；页面加载行情时不直接访问第三方网站。
- Android 是离线消费端，不持有 Provider 凭据，也不承担数据生产或任意 Python 执行。
- 本地数据、报告、日志、缓存和导出不是源码，不进入 GitHub。

## 仓库模块

| 路径 | 职责 |
| --- | --- |
| `desktop/src/market_monitor/` | Provider、采集、存储、分类、缓存、公式、账户分析、FastAPI 和 CLI。 |
| `desktop/web/src/` | Vue 3 + TypeScript 本地研究终端。 |
| `desktop/tests/` | 后端单元、契约、存储和 API 测试。 |
| `desktop/web/e2e/` | Playwright 可见行为和浏览器持久化测试。 |
| `android/` | Kotlin/Jetpack Compose 离线消费端。 |
| `contracts/` | 桌面端与 Android 共享的 JSON Schema。 |
| `scripts/` | 验证、运维、离线导出和原始数据下载脚本。 |
| `docs/` | ADR、架构、计划、经验、日志、能力矩阵和历史证据。 |

## 数据分层与存储

| 层 | 介质 | 内容 |
| --- | --- | --- |
| Raw/Bronze | gzip JSON、JSON/JSONL | 上游原始响应、可恢复下载和审计证据。 |
| Catalog | DuckDB | 运行台账、分区登记、数据集目录、普通 Gold 指标和版本化期货多空热度。 |
| Silver | Parquet | 标准化、来源隔离的 K 线；完整记录保存在 `bar_json`。 |
| Query cache | DuckDB 清单 + 有界内存窗口 | 文件覆盖索引、游标历史查询和卡片尾部 K 线。 |
| Personal | 本地 JSON/JSONL；Android 加密 Room | 自选、画线、账户、交易、策略参数和个人记录。 |
| Package | SQLite + manifest + 签名 | Android 可验证的离线行情与产业链快照。 |

Silver 的幂等键为 `instrument_id + period + bar_open_time`。权威数据先写 Parquet，成功后再推进数据版本并更新可重建查询缓存；缓存不能反向控制权威写入。

## 标的身份与来源隔离

- 标准标的键由市场、交易所、资产类型和代码组成。
- Provider 的 `source_symbol` 不能直接当作标准标的键；裸代码必须结合交易所和资产类型解析。
- 同一标的的不同来源使用不同物理 ID，例如 `.TDX_LOCAL`；新增来源必须先通过独立的来源隔离与质量门设计。
- 查询层可以聚合为同一标准标的，存储层不得逐字段混拼不同来源。
- `market_classification.py` 与配置文件集中处理沪深北、港股、期货和通达信板块指数分类；显式字段优先，代码区间只作有审计记录的兜底。未命中规则时返回内部 `unclassified` 状态，不创建公开“其它”分类。

## 数据接入链路

### 网络 Provider

Provider 通过独立能力登记与探针报告声明市场、资产类型、周期、字段、认证和错误。单个接口成功不代表整个 Provider 可用，也不代表全市场覆盖。

### 通达信本地证券

`tdx_local.py` 发现沪深北/港股 `.day/.lc5` 文件，并由 `tdx-cn-v2` 先分类再标准化：A 股/指数、B 股/基金/REIT、转债/回购分别使用经过日线与分钟线核验的价格精度；成交量按行从候选倍率中唯一推断，指数与回购使用独立量纲。Bar 保留原始值、价格除数、成交量倍率/单位和规则版本；无法解释的行进入隔离区。`--audit-only` 只生成证据，`--replace-source --full-rescan` 在独立暂存库重建并保留旧分区备份后替换来源。

### 通达信期货通与期货备用源

`futures_bulk.py` 导入本地月份合约、次连 `L7`、主连 `L8` 和原生加权 `L9`，并在本地主连缺失时使用受控 AKShare 备用。`futures_calendar.py` 持久化统一交易日，`futures_rule_sync.py` 按精确交易日快照品种乘数、基础保证金和具体合约 override；`futures_heat_pipeline.py` 离线生成版本化 Gold，缺失规则不回填猜测值。

### TickDB（历史）

TickDB 从未进入 Silver，本地原始目录已由用户于 2026-08-28 删除，活动下载脚本及专属测试于 R4 移除。2026-08-24 审计只保留为历史证据，不是当前 Provider、回退源或通达信质量依赖；未来若重新接入必须重新立项和验证授权、映射、单位及来源隔离。

## FastAPI 与网页端

- `web_app.py` 注册本机路由并限制写操作只能由 loopback 调用。
- `web_api/market.py` 提供分类、分页标的、待分类审计清单、游标 K 线、卡片批量、画线和指标。`unclassified_instruments.py` 只读扫描两个通达信终端中未命中文件名规则的数据，并与 Silver 未分类项合并展示；正常行情和策略入口在查询边界排除这些项。
- `web_api/sources.py` 提供本地物理表、数据集、字段、Provider 和路由偏好。
- `web_api/stats.py` 与 `web_api/strategy.py` 提供账户、交易、策略绩效和安全公式运行。
- `web_api/futures.py` 只读 `FUTURES_LONG_SHORT_HEAT` Gold；它不扫描 Silver，也不保存用户权重下的固定总分。
- 网页请求层使用数据版本、并发去重、取消、内存/IndexedDB 缓存；权威数据仍在后端。

行情页包括列表、卡片、双看板和全屏详情。画线实例由后端按标的保存；新建图形的默认样式、吸附、跨周期和连续画线偏好由浏览器 `localStorage` 持久化。卡片图只读显示详情页画线，避免误编辑。期货页的总热度权重同样保存在 `localStorage`，仅在浏览器内线性重算当前 Gauge 与历史总线。

## 公式、策略与账户分析

- 桌面公式引擎只允许白名单语法和函数，限制表达式大小、幂运算和历史窗口，不允许任意 Python、文件或网络访问。
- 时间序列指标禁止未来数据；缺失历史和除零返回不可用原因而非伪造数值。
- 策略结果是观察信号，不是自动交易指令。
- 账户分析使用独立本地个人数据，支持 FIFO、持仓、CSV 和回收站；不能随行情包替换而丢失。

## Android 数据边界

- Android 只导入本人签名的行情包和声明式策略 DSL。
- 行情库和个人加密库物理隔离；行情更新失败或回滚不能删除个人记录。
- Android 不存储 JQData、Tushare 等 Provider 凭据，不直接连接第三方行情服务。

## 安全与 GitHub 发布边界

- `.env`、API Key、Token、密码、私钥、`local.properties`、本地数据库、Parquet、通达信文件、行情下载、报告、日志、缓存和导出必须被 `.gitignore` 排除。
- `.env.example` 只登记变量名，值保持为空。
- Git 提交前必须扫描实际候选文件和暂存区，检查大文件，并核对远端 URL。
- Android 包内仅允许公开验签公钥；私钥只能存在仓库外。

## 当前已知缺口

- 通达信 `tdx-cn-v2` 已进入全量迁移与真实重导验证；隔离覆盖和跨来源样本仍以最新迁移报告为准。
- TickDB 已从活动代码和数据源能力中移除；历史下载记录不能代表当前可用数据。
- 本轮发布按用户要求执行针对性验证、网页构建和真实迁移完整性检查；完整跨端验证仍可在后续发布前单独执行。
- 期货多空热度最新资金覆盖为 73/74；长历史 `FundScore10` 受月份合约 Silver 限制仅覆盖 237/5,244 日，近 1 年为 222/243。交易规则与统一日历目前来自已本地快照的 AKShare 公共上游，仍需持续监控字段和来源变化。
- 历史 R3 的完整跨端验收记录仍需保留，不应与本轮针对性验证结果混同。
