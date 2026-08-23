# MarketListener 当前架构

最后更新：2026-08-24。本文描述当前仓库的整体运行结构和 R3 新增能力；不可逆架构约束仍以 `ADR.md` 与 `docs/adr/` 为准，当前工作状态以根目录 `Plan_R3.md` 为准。

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
| Catalog | DuckDB | 运行台账、分区登记、数据集目录和 Gold 指标。 |
| Silver | Parquet | 标准化、来源隔离的 K 线；完整记录保存在 `bar_json`。 |
| Query cache | DuckDB 清单 + 有界内存窗口 | 文件覆盖索引、游标历史查询和卡片尾部 K 线。 |
| Personal | 本地 JSON/JSONL；Android 加密 Room | 自选、画线、账户、交易、策略参数和个人记录。 |
| Package | SQLite + manifest + 签名 | Android 可验证的离线行情与产业链快照。 |

Silver 的幂等键为 `instrument_id + period + bar_open_time`。权威数据先写 Parquet，成功后再推进数据版本并更新可重建查询缓存；缓存不能反向控制权威写入。

## 标的身份与来源隔离

- 标准标的键由市场、交易所、资产类型和代码组成。
- Provider 的 `source_symbol` 不能直接当作标准标的键；裸代码必须结合交易所和资产类型解析。
- 同一标的的不同来源使用不同物理 ID，例如 `.TDX_LOCAL`、未来的 `.TICKDB`。
- 查询层可以聚合为同一标准标的，存储层不得逐字段混拼不同来源。
- `market_classification.py` 与配置文件集中处理沪深北、港股、期货和通达信板块指数分类；显式字段优先，代码区间只作有审计记录的兜底。

## 数据接入链路

### 网络 Provider

Provider 通过独立能力登记与探针报告声明市场、资产类型、周期、字段、认证和错误。单个接口成功不代表整个 Provider 可用，也不代表全市场覆盖。

### 通达信本地证券

`tdx_local.py` 发现沪深北/港股 `.day/.lc5` 文件，读取名称、建立增量检查点并写入来源隔离 Silver。R3 审计发现日线价格倍率和大成交量文件的手/份单位问题；修复前禁止继续正式导入受影响类型。

### 通达信期货通与期货备用源

`futures_bulk.py` 导入本地月份合约、次连 `L7`、主连 `L8` 和原生加权 `L9`，并在本地主连缺失时使用受控 AKShare 备用。合约乘数和保证金规则来自带生效日的配置，沉淀资金保留计算中间字段。

### TickDB

`scripts/tickdb_download.py` 只负责可恢复原始下载与审计。当前 TickDB 尚未进入 Silver；后续导入器必须完成代码映射、去重、零价隔离、量额单位标准化和来源隔离。

## FastAPI 与网页端

- `web_app.py` 注册本机路由并限制写操作只能由 loopback 调用。
- `web_api/market.py` 提供分类、分页标的、游标 K 线、卡片批量、画线和指标。
- `web_api/sources.py` 提供本地物理表、数据集、字段、Provider 和路由偏好。
- `web_api/stats.py` 与 `web_api/strategy.py` 提供账户、交易、策略绩效和安全公式运行。
- 网页请求层使用数据版本、并发去重、取消、内存/IndexedDB 缓存；权威数据仍在后端。

行情页包括列表、卡片、双看板和全屏详情。画线实例由后端按标的保存；新建图形的默认样式、吸附、跨周期和连续画线偏好由浏览器 `localStorage` 持久化。卡片图只读显示详情页画线，避免误编辑。

## 公式、策略与账户分析

- 桌面公式引擎只允许白名单语法和函数，限制表达式大小、幂运算和历史窗口，不允许任意 Python、文件或网络访问。
- 时间序列指标禁止未来数据；缺失历史和除零返回不可用原因而非伪造数值。
- 策略结果是观察信号，不是自动交易指令。
- 账户分析使用独立本地个人数据，支持 FIFO、持仓、CSV 和回收站；不能随行情包替换而丢失。

## Android 数据边界

- Android 只导入本人签名的行情包和声明式策略 DSL。
- 行情库和个人加密库物理隔离；行情更新失败或回滚不能删除个人记录。
- Android 不存储 JQData、Tushare、TickDB 等凭据，不直接连接第三方行情服务。

## 安全与 GitHub 发布边界

- `.env`、API Key、Token、密码、私钥、`local.properties`、本地数据库、Parquet、通达信文件、行情下载、报告、日志、缓存和导出必须被 `.gitignore` 排除。
- `.env.example` 只登记变量名，值保持为空。
- Git 提交前必须扫描实际候选文件和暂存区，检查大文件，并核对远端 URL。
- Android 包内仅允许公开验签公钥；私钥只能存在仓库外。

## 当前已知缺口

- 通达信证券日线价格缩放和成交量单位尚待修复、迁移和真实重导验证。
- TickDB 尚无 Raw-to-Silver 导入器，现有下载不是完整历史覆盖。
- 正式 bar Schema 的资产类型枚举尚未完全覆盖运行时的 LOF、REIT、转债和回购细分。
- R3 工作区需要完成完整 Ruff、pytest、Playwright、Android 和真实页面验收后才能封板。
