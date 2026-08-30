# 数据源能力矩阵

审计日期：2026-08-30。这里的“已实现”仅指仓库存在 adapter/collector 代码；“当前存量”仅指 `data_control/silver` 已落库数据。两者都不等同于持续可用、实时更新或无缺口的全市场覆盖。2026-08-24 TickDB/通达信专项盘点是历史问题发现证据，当前通达信口径以 `tdx-cn-v2` 审计和迁移报告为准。

## 统一口径

- 内部 OHLC 使用标准字段 `open/high/low/close`；界面可以显示为 `Start/High/Low/End`，不得反向改变存储字段。
- `volume`、`amount`、`open_interest`、`pct_change`、`amplitude` 缺失时为 `NULL`/能力不可用，绝不写为零。沉淀资金尚无可靠统一口径，当前不支持。
- 原始基础周期优先存 Silver；`aggregation.py` 已按 CN/HK 股票与 CN 期货交易时段聚合 1/5/15/30/60/120/240 分钟，日线可聚合为 `1w`/`1mo`。派生只在本地查询时进行；尚未验证的跨市场分钟数据不生成虚假周期。
- A 股上涨/平盘/下跌可以从实际每日收盘价计算；**涨停/跌停、连板高度不得用统一 `±9.9%` 阈值计算**。主板、创业板、科创板、北交所、ST 和上市初期规则不同；在接入权威涨停池或完成带证券属性与生效日期的规则模型前，页面不展示该估算。

## 当前本地存量（真实 Silver）

2026-08-13 的历史快照曾为 9,937 个标的、3,090,089 行。2026-08-30 金融终端增量导入后，K 线查询缓存索引为 432,730,853 行；其总量包含既有多来源物理分区，不能直接等同于逻辑标的数或已去重行情行。库存默认不再截断为按代码排序的前 20,000 个标的，具体类别、字段完整度、来源和最后更新时间由 `/api/data-sources` 在运行时读取本地 Silver 后给出。

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
| AKShare / BEA | Python SDK；collector 使用 `stock_hk_hist`、`futures_main_sina`、`futures_index_ccidx`、`futures_foreign_hist` 等；BEA 进口额从官网发布页发现当前 XLSX，核心 PCE 从公共 NIPA 季度文本读取 | HK/全球/期货日线、CN 指标与多种公共数据、美国季调商品和服务进口、核心 PCE 年化季率终值 | 当前调用无 token；上游可变 | 2026-08-12 在 30 秒限制内：健康检查、A 股涨跌家数、交易日历、600519 日线 PASS；涨停/跌停改由东财权威池采集，绝不以涨跌幅阈值近似；市场资金流因东方财富 endpoint 经代理连接被拒绝 FAILED。2026-08-30 宏观单任务先写入 20,857 条、后续写入 241 条全社会用电量、414 条美国季调进口和 268 条核心 PCE，当前本机 Gold 共 21,780 条：除 M0/M1/M2、CPI/PPI、PMI、DR007、中美 10 年期和美联储利率外，还包括中国美元计进口/出口/贸易差额、社零同比/环比、外储、全社会用电量、美国季调进口金额、核心 PCE 终值和季调后非农。用电量原始值经国家能源局 2026-07 累计 `61,399` 亿千瓦时交叉验证为万千瓦时，入库为年内累计亿千瓦时；BEA Table 1 的 Imports/Total 为月度季调、百万美元，实际范围 1992-01～2026-06。核心 PCE 从 NIPA 2.3.4 `DPCCRG` 指数按年化环比计算，只纳入当前 GDP 页面已进入 Third Estimate 的季度，实际范围 1959-Q2～2026-Q1；历史值是 BEA 当前修订口径。外贸、外储与非农仅记录来源日期，不能当作统计观察期。每条数据的本机取得时间与权威发布日期分列，未提供发布日期时为 `null`。证据：`artifacts/r1-provider-probe-20260812-akshare-30s/`（历史）及本轮本地控制台输出（运行时，不纳入 Git）。 |
| Baostock | `baostock.login`、`query_history_k_data_plus` | CN 股票 1d/30m | SDK 登录 | 2026-08-12 首次 10 秒超时；30 秒复测未在本任务运行时限内产出报告。当前没有可验证 PASS 结论，不能提升为可用来源。 |
| JQData | `jqdatasdk.auth` 和价格接口 | CN 股票/ETF/指数/期货（以探测能力为准） | 用户名/密码、授权 | `BLOCKED_CONFIGURATION`，不能显示为当前可用。 |
| Tushare Pro | `TUSHARE_TOKEN`、`pro_api`、`daily`/`stk_mins`/`stock_basic` | CN 股票日线/分钟、清单与财务（以积分权限为准） | token 与接口积分/权限 | `BLOCKED_CONFIGURATION`，不能显示为当前可用。 |
| Binance | HTTPS JSON：`https://data-api.binance.vision/api/v3/klines` | BTC/ETH 日线 | 公共端点 | collector 已落库；网络可达性需每次会话实测。 |
| 东财/CBOE/腾讯 | 东财 `push2his.eastmoney.com/api/qt/stock/kline/get`；CBOE VIX CSV；腾讯回退 | DXY/VIX 日线指标 | 公共端点 | collector 已实现，TLS/网络可能导致部分失败。 |
| 同花顺行情中心 | `q.10jqka.com.cn` 市场页与指数页快照 | A 股涨跌家数、涨停/跌停家数、昨日涨停平均收益率、指数表格 | 网站会话/反爬策略 | 已实现 `ths-market` 可恢复快照任务；公开页可获取首批指数，翻页请求会返回登录/授权限制。使用已登录浏览器 Cookie 的自动化仍需在可用浏览器会话中另行验证。 |
| 通达信金融终端本地证券文件 | `C:\tongdaxin\vipdoc` 的 `.day/.lc5` | 沪深北及港股日线/五分钟，含 A/B 股、指数、ETF、LOF、REIT、转债和回购分类；代码亦识别 `ds` 的国际/港股/中证/华证/国证指数与外盘期货前缀 | 本机已下载文件；无网络认证 | `tdx-cn-v2` 已实现资产级价格精度、逐行成交量倍率、原始值追溯、隔离质量门、只读审计和可回滚来源替换。`12/16/17/18/27/31/48/62/69/102#` 的浮点日线已由只读审计后增量写入 4,043 个文件/38,663,985 根 PASS K 线；907 个文件/108,456 根记录隔离、138 个拒绝。该集合尚未另行执行新的全量来源替换。外盘期货金额/连续合约语义、汇率、宏观、基金及未知前缀仍不提升为正式 Silver。 |
| TickDB | 无活动来源 | 无本地原始目录、无 Silver、无活动下载器 | 不适用 | **REMOVED**；只保留 2026-08-24 历史审计，不参与正式导入、回退或质量判定。 |

## R2 分层实测（2026-08-13）

验证层级依次为：代码已实现、真实连接探针、单标的真实数据、跨交易所/资产抽样、批量任务、数据库落库、API 查询、前端显示。较低层 PASS 不会自动提升为全量支持。

| 类别 | 代码/连接/单标的 | 批量/落库/API/前端 | 结论 |
| --- | --- | --- | --- |
| A 股个股日线 | AKShare 真实探针：A 股现货 5,543、600519 日线 5,981、日历 PASS；pytdx 600519 日线 PASS | A 股全量日线已入 Silver，市场 API 已验证 | **日线 PARTIAL→主要覆盖**；分钟仅 pytdx 600519 `30m` 样本 PASS，不是全市场分钟承诺。 |
| A 股 ETF 日线 | pytdx 510300 日线 PASS | 1,559 ETF 已入 Silver；14 个 `530xxx` 缺上游日线 | **日线 PARTIAL**；ETF 分钟线未完成独立实测/批量/落库。 |
| 港股个股 | AKShare 日线回填已使用；金融终端 `31#`/`48#` 浮点日线与 5 分钟布局已按 `tdx-cn-v2` 入库 | 本机港股数据页按需聚合本地 TDX 日线；2026-08-18 覆盖 2,706 个股票，成交额与涨跌家数可用并携带覆盖数 | **港股日线 PARTIAL**；不宣称全市场市值或完整上市名单。港股分钟线已存在本地文件但尚未完成独立市场时段/全量合理性验收。 |
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
| 国内期货品种收益、沉淀资金与多空热度 | PARTIAL | 月份合约与原生加权日线、双边沉淀资金公式、统一交易日、逐日乘数/保证金快照和版本化 Gold 已接通；2026-08-21 方向覆盖 74/74、资金覆盖 73/74。长历史月份合约不足使 `FundScore10` 仅覆盖 237/5,244 日（近 1 年 222/243）；规则/日历为 AKShare 公共上游快照且有 15 个规则空表日。 |
| 国内外商品分类指数全覆盖 | BLOCKED | 当前只有 CCIDX/少量公开数据；同花顺、Wind、QMT、文华等未实现或需授权，不能伪装接入。 |
