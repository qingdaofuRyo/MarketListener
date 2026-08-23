# 数据源能力矩阵

审计日期：2026-08-24。这里的“已实现”仅指仓库存在 adapter/collector 代码；“当前存量”仅指 `data_control/silver` 已落库数据。两者都不等同于持续可用、实时更新或无缺口的全市场覆盖。TickDB 与通达信本地证券的专项盘点见 `TICKDB_TDX_DATA_AUDIT_2026-08-24.md`。

## 统一口径

- 内部 OHLC 使用标准字段 `open/high/low/close`；界面可以显示为 `Start/High/Low/End`，不得反向改变存储字段。
- `volume`、`amount`、`open_interest`、`pct_change`、`amplitude` 缺失时为 `NULL`/能力不可用，绝不写为零。沉淀资金尚无可靠统一口径，当前不支持。
- 原始基础周期优先存 Silver；`aggregation.py` 已按 CN/HK 股票与 CN 期货交易时段聚合 1/5/15/30/60/120/240 分钟，日线可聚合为 `1w`/`1mo`。派生只在本地查询时进行；尚未验证的跨市场分钟数据不生成虚假周期。
- A 股上涨/平盘/下跌可以从实际每日收盘价计算；**涨停/跌停、连板高度不得用统一 `±9.9%` 阈值计算**。主板、创业板、科创板、北交所、ST 和上市初期规则不同；在接入权威涨停池或完成带证券属性与生效日期的规则模型前，页面不展示该估算。

## 当前本地存量（真实 Silver）

2026-08-13 本机实测 `/api/market/overview` 返回 9,937 个标的、3,090,089 行，基础周期为 `1d` 和 `30m`。其中 CN 7,118、HK 2,807、GLOBAL 12；资产类型为 STOCK 8,343、ETF 1,559、INDEX 14、FUTURE 19、CRYPTO 2。计数会随增量采集变化，具体类别、字段完整度、来源和最后更新时间由 `/api/data-sources` 在运行时读取 parquet 后给出。

| 数据类别 | 当前来源 | 实现/存量 | 当前周期 | 覆盖事实与限制 |
| --- | --- | --- | --- | --- |
| A 股个股 | AKShare、pytdx | 已实现；全量日线回填已完成 | 1d；样本另有 30m | 7,118 个本地 CN 标的（含个股、ETF、指数等）；回填对股票使用 AKShare 历史接口，pytdx 作为可用能力来源。 |
| 港股个股 | AKShare | 已实现；日线回填基本完成 | 1d | 2,807 个本地 HK 标的；1 个标的因上游返回不含日期字段未写入，需后续重试或更换来源。 |
| 境内 ETF | AKShare、pytdx | 已实现；日线回填部分完成 | 1d | 1,559 个 ETF 已写入；14 个 `530xxx` 标的从 pytdx 未返回日线，不能伪造为完成。 |
| A/H 股指数 | AKShare、pytdx、同花顺 | 已实现；当前为部分存量 | 1d | 同花顺指数快照采集已接入；公开访问仅取得首批页面，后续页受登录/反爬限制，不能宣称 575 个指数已完整入库。 |
| 全球指数 | AKShare | 已实现；当前为少量存量 | 1d | `index_global_hist_em` 等适配调用；无全市场承诺。 |
| 国内期货主连、次连、加权、商品指数 | 通达信期货通本地缓存；AKShare 主连备用 | 已实现 `bulk-futures` 增量导入 | 通达信原始 5m/1d；可派生 15m/30m/1h/2h/4h/周月季年 | 本地 `L7=次连`、`L8=主连`、`L9=原生加权`；动态发现文件，主连仅在本地缺失或明显滞后时回退 AKShare `品种0`。AKShare `品种9` 不可验证，禁止当作加权。 |
| 国际重点期货 | AKShare | 已实现受控目录增量导入 | 1d | 东财 `00Y` 真实连续合约与新浪 `AHD/OIL`；无原生加权时明确不支持，不自行构造。 |
| 加密货币 | Binance 公共接口 | 已实现；BTC/ETH 存量 | 1d | `https://data-api.binance.vision/api/v3/klines`；不属于六类股票/期货目标。 |
| 美元指数、VIX | 东方财富 / CBOE，腾讯回退 | 已实现；Gold 指标 | 1d | 东财 kline API、CBOE VIX CSV；当前为指标而不是统一 bars。 |

## Provider / Adapter 事实

Provider registration exposed by `/api/data-sources` includes an explicit default `priority` and `enabled` flag. A disabled or unconfigured provider remains visible for traceability but is not evidence that it is usable for collection.

Each local inventory category also exposes `sourceDetails`: its stored source id is joined to the registered endpoint, declared periods, fields, and status. A source id not present in the registry is returned as `UNREGISTERED_SOURCE` with no invented endpoint.

| Provider | 获取方式与实际入口 | 已实现能力 | 认证/授权 | 当前验证状态与限制 |
| --- | --- | --- | --- | --- |
| pytdx（通达信） | TDX TCP/7709，`TdxHq_API.get_security_bars`，服务地址可由 `TDX_SERVERS` 配置 | CN 股票/ETF/指数清单、报价、1m/5m/15m/30m/1h/1d/1w/1mo | 无账户；公网服务稳定性非保证 | 2026-08-12 实测：证券清单、报价、600519 `1d/30m`、510300 `1d` PASS；000001 指数 `1d` 因上游非法日期失败。证据：`artifacts/r1-provider-probe-20260812-pytdx-fixed/`。 |
| AKShare | Python SDK；collector 使用 `stock_hk_hist`、`futures_main_sina`、`futures_index_ccidx`、`futures_foreign_hist` 等 | HK/全球/期货日线、CN 指标与多种公共数据 | 当前调用无 token；上游可变 | 2026-08-12 在 30 秒限制内：健康检查、A 股涨跌家数、交易日历、600519 日线 PASS；涨停/跌停改由东财权威池采集，绝不以涨跌幅阈值近似；市场资金流因东方财富 endpoint 经代理连接被拒绝 FAILED。证据：`artifacts/r1-provider-probe-20260812-akshare-30s/`。 |
| Baostock | `baostock.login`、`query_history_k_data_plus` | CN 股票 1d/30m | SDK 登录 | 2026-08-12 首次 10 秒超时；30 秒复测未在本任务运行时限内产出报告。当前没有可验证 PASS 结论，不能提升为可用来源。 |
| JQData | `jqdatasdk.auth` 和价格接口 | CN 股票/ETF/指数/期货（以探测能力为准） | 用户名/密码、授权 | `BLOCKED_CONFIGURATION`，不能显示为当前可用。 |
| Tushare Pro | `TUSHARE_TOKEN`、`pro_api`、`daily`/`stk_mins`/`stock_basic` | CN 股票日线/分钟、清单与财务（以积分权限为准） | token 与接口积分/权限 | `BLOCKED_CONFIGURATION`，不能显示为当前可用。 |
| Binance | HTTPS JSON：`https://data-api.binance.vision/api/v3/klines` | BTC/ETH 日线 | 公共端点 | collector 已落库；网络可达性需每次会话实测。 |
| 东财/CBOE/腾讯 | 东财 `push2his.eastmoney.com/api/qt/stock/kline/get`；CBOE VIX CSV；腾讯回退 | DXY/VIX 日线指标 | 公共端点 | collector 已实现，TLS/网络可能导致部分失败。 |
| 同花顺行情中心 | `q.10jqka.com.cn` 市场页与指数页快照 | A 股涨跌家数、涨停/跌停家数、昨日涨停平均收益率、指数表格 | 网站会话/反爬策略 | 已实现 `ths-market` 可恢复快照任务；公开页可获取首批指数，翻页请求会返回登录/授权限制。使用已登录浏览器 Cookie 的自动化仍需在可用浏览器会话中另行验证。 |
| 通达信金融终端本地证券文件 | `C:\tongdaxin\vipdoc` 的 `.day/.lc5` | 沪深北及港股日线/五分钟，含股票、指数、ETF、LOF、REIT、转债和回购分类 | 本机已下载文件；无网络认证 | 已实现增量导入和来源隔离，但中国市场日线价格倍率及大成交量文件的手/份单位尚未修复，当前为 **CHANGES_REQUIRED**，不得继续正式导入受影响类型。 |
| TickDB | 历史 K 线 REST 原始 gzip 缓存 | 当前下载含中国 ETF、中国/港股指数的 1d/5m/30m/1h 子集 | API Key 仅来自环境变量 | 原始文件和可恢复下载器已存在；Silver 中来源计数为 0。需要去重、代码映射、量额单位和异常隔离导入器，当前为 **RAW_ONLY**。 |

## R2 分层实测（2026-08-13）

验证层级依次为：代码已实现、真实连接探针、单标的真实数据、跨交易所/资产抽样、批量任务、数据库落库、API 查询、前端显示。较低层 PASS 不会自动提升为全量支持。

| 类别 | 代码/连接/单标的 | 批量/落库/API/前端 | 结论 |
| --- | --- | --- | --- |
| A 股个股日线 | AKShare 真实探针：A 股现货 5,543、600519 日线 5,981、日历 PASS；pytdx 600519 日线 PASS | A 股全量日线已入 Silver，市场 API 已验证 | **日线 PARTIAL→主要覆盖**；分钟仅 pytdx 600519 `30m` 样本 PASS，不是全市场分钟承诺。 |
| A 股 ETF 日线 | pytdx 510300 日线 PASS | 1,559 ETF 已入 Silver；14 个 `530xxx` 缺上游日线 | **日线 PARTIAL**；ETF 分钟线未完成独立实测/批量/落库。 |
| 港股个股 | AKShare 日线回填已使用；本轮未做港股分钟真实探针 | 2,807 标的日线入 Silver，1 个缺口 | **港股日线 PARTIAL**；港股分钟线 **BLOCKED**，缺连接/样本/批量/落库证据。 |
| A 股/港股/全球指数 | pytdx 上证指数日线探针因上游非法日期 FAILED；同花顺分页受登录限制 | 只有部分日线/快照 | **BLOCKED**，不能宣称指数全集或分钟线。 |
| 国内期货主力/次连/加权 | 通达信期货通 `.day/.lc5` 本地文件，AKShare 主连备用 | `bulk-futures` 动态扫描品种，不以固定数量承诺覆盖 | **本地缓存覆盖范围内可用**；任务报告实际发现数、失败文件与更新时间。 |
| 国内期货加权/连续 | 通达信期货通 `L9` 原生加权 | 与主连/次连物理隔离入库 | **可用（以本地客户端已下载文件为准）**；不把 AKShare `品种9` 误标为加权。 |

本轮 pytdx 报告：`artifacts/r2-probe-pytdx/provider-capabilities.json`（连接、清单、报价、600519 `1d/30m`、510300 `1d` PASS；000001 `1d` FAILED）。AKShare 报告：`artifacts/r2-probe-akshare/provider-capabilities.json`（连接/现货、涨跌家数、交易日历、资金流、600519 日线 PASS）。报告目录为本地 artifact，不纳入 Git。

## 候选 Adapter 研究结论

| 候选 | 结论 | 原因 |
| --- | --- | --- |
| GitHub `quantitative-finance` Topic | 仅作项目发现入口 | Topic 不是数据提供方；每个项目均须单独审查 License、活跃度、底层来源、条款和真实探针，不能直接接入。 |
| AData | 候选，未提升 | 目标网页本轮抓取返回 404，未获得底层来源、许可、登录、频控、分钟深度和 Windows 兼容性证据。 |
| mootdx/easy_tdx | 候选，未提升 | 可能封装 TDX 协议，不会改善上游 TDX 的数据边界；未完成 License、维护与真实分钟落库验证。 |
| JoinQuant / Tushare | 可选配置，BLOCKED_CONFIGURATION | 当前无本地授权凭据；代码声明的分钟能力未经过本机真实连接、权限、深度和批量验证。 |

## 未满足目标与准确原因

| 目标能力 | 状态 | 原因 / 解除条件 |
| --- | --- | --- |
| 全部 A 股、HK 股、ETF 的 30m/1h/2h/4h/1d/1w/1m | PARTIAL | A/H 个股和大部分 ETF 的日线已落库；API 可由已存 30m 或 1d 派生部分周期。仍缺全量分钟基础数据、14 个 ETF 上游缺口及 1 个港股缺口。 |
| 全部指数同周期与成交量 | BLOCKED | 当前仅部分指数日线/快照；同花顺 575 指数分页受登录限制，部分上游不提供成交量；需按字段能力保留 NULL。 |
| 国内期货主力+加权连续的全周期与沉淀资金 | BLOCKED | 当前仅主力日线/部分商品指数；未定义可验证的沉淀资金计算口径和连续合约来源。 |
| 国内外商品分类指数全覆盖 | BLOCKED | 当前只有 CCIDX/少量公开数据；同花顺、Wind、QMT、文华等未实现或需授权，不能伪装接入。 |
