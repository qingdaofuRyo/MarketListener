# MarketListener 当前架构

最后更新：2026-08-30。本文描述当前仓库的整体运行结构和 R3/R4 已实现能力；不可逆架构约束仍以 `ADR.md` 与 `docs/adr/` 为准，当前工作状态以根目录 `Plan_R4.md` 为准。

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
| Catalog | DuckDB | 运行台账、分区登记、数据集目录、普通 Gold 指标、版本化期货多空热度、交易所会员方向排名、逐交易所排名采集覆盖和固定基准期货结构 Gold。 |
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
- `market_classification.py` 与配置文件集中处理沪深北、港股、指数编制方和期货交易场所分类；显式字段优先，代码区间只作有审计记录的兜底。中证、国证、华证、交易所指数和通达信指数分别展示，期货按具体交易场所展示；未命中规则时返回内部 `unclassified` 状态，不创建公开“其它”分类。

## 数据接入链路

### 网络 Provider

Provider 通过独立能力登记与探针报告声明市场、资产类型、周期、字段、认证和错误。单个接口成功不代表整个 Provider 可用，也不代表全市场覆盖。

### 通达信本地证券

`tdx_local.py` 发现沪深北/港股 `.day/.lc5` 文件，并由 `tdx-cn-v2` 先分类再标准化：A 股/指数、B 股/基金/REIT、转债/回购分别使用经过日线与分钟线核验的价格精度；成交量按行从候选倍率中唯一推断，指数与回购使用独立量纲。金融终端 `vipdoc/ds` 的 `10/12/16/17/18/27/31/38/48/62/69/102#` 映射集中在 `market_classification.json`，其中基本汇率和宏观保留来源原始量纲；`ds_stk.dat` 和证券名称表为新导入提供来源简称，打包的编制方名称表为旧 Silver 行提供展示修复。`27#HZ`、`49#` 与 `98#` 已退役，不进入 Silver 或待分类表。Bar 保留原始值、价格除数、成交量倍率/单位和规则版本；无法解释的行进入隔离区。`--ds-prefix` 支持只处理指定 `ds` 前缀，普通增量完成后仅刷新本次新增分区的查询索引；`--replace-source --full-rescan` 才在独立暂存库执行完整来源重建。

### 通达信期货通与期货备用源

`futures_bulk.py` 只接收期货通的国内衍生品域：28/29/30/47/66 对应郑商所、大商所、上期所、中金所和广期所，30# 中的 SC/NR/LU/BC/EC 归能源中心；另接收 `42#` 商品指数与 `68#` 波动率/期权指数。月份合约、`L7/L8/L9`、大商所 `*-F`、中金所 `L0～L3` 和标的指数使用不同 canonical series；期货通中的证券指数重复前缀不导入。其余期货日历、规则快照、热度、席位和结构链路继续保持来源覆盖与缺失显式化。

### TickDB（历史）

TickDB 从未进入 Silver，本地原始目录已由用户于 2026-08-28 删除，活动下载脚本及专属测试于 R4 移除。2026-08-24 审计只保留为历史证据，不是当前 Provider、回退源或通达信质量依赖；未来若重新接入必须重新立项和验证授权、映射、单位及来源隔离。

## FastAPI 与网页端

- `web_app.py` 注册本机路由并限制写操作只能由 loopback 调用。
- `web_api/market.py` 提供分类、分页标的、内部待分类审计接口、游标 K 线、卡片批量、画线和指标。`unclassified_instruments.py` 只读扫描两个通达信终端中未命中文件名规则的数据，并与 Silver 未分类项合并供诊断；行情页不渲染该结果，正常行情和策略入口在查询边界排除这些项。
- `web_api/sources.py` 提供本地物理表、数据集、字段、Provider 和路由偏好。
- `web_api/stats.py` 与 `web_api/strategy.py` 提供账户、交易、策略绩效和安全公式运行。
- `web_api/futures.py` 只读 `FUTURES_LONG_SHORT_HEAT`、`FUTURES_STRUCTURE_*` Gold、原始会员方向排名、逐交易所排名采集覆盖和已选中的本地 Silver 合约日线；它不保存用户权重下的固定总分，也不临时合成加权合约。会员接口默认不传输全市场大字段，要求先按交易所/品种/合约筛选，并显示每个交易所的来源状态、行数和失败原因；即使最新采集全部失败也会返回该日期的覆盖证据，而不是伪装成上一次成功日。`/contracts` 与 `/contract-series` 仅暴露本地月份/原生加权序列、OHLC 和单边持仓。具体月份合约及 `product-notional` 结构的名义持仓规模固定为 `结算价 × 同交易日交易乘数 × 单边持仓量`，缺少结算价、精确乘数或持仓量时返回 `null + reason`；加权合约不由接口临时拼接。基差和席位市值仍受现货规格/来源与完整席位覆盖质量门控制。
- `web_api/data_sections.py` 为 `/data` 的 A 股、港股、其他数据三分区提供真实可用性元数据，并从本地 `gold_metrics` 读取中国/美国宏观目录、单序列和 A 股总览。宏观观测将本机取得时间作为 `fetchedAt` 返回；缺少可验证权威发布日期时 `releasedAt=null`，绝不混称。外贸、外储和非农的上游仅给出来源日期，目录以 `timeBasis=SOURCE_DATE` 展示，不能误称为统计观察期。A 股总览会归一历史 Gold 中的紧凑/ISO 交易日再合并同日观测；港股总览在用户首次请求时仅聚合通过质量门的本地 `HK/HKEX/STOCK/*.TDX_LOCAL` 日线，返回成交额及按同一标的前收计算的涨/平/跌和覆盖数，故为 `PARTIAL`；首次聚合前、港股市值/涨跌停及没有带生效期的状态名单仍明确返回不可用，而不是合成历史值或复用 A 股指标。`/equities/{market}/lists` 的日期/类型/分页契约在没有带生效期的权威状态记录时返回不可用空集与原因，而不把空集解释成“没有风险或停牌标的”。
- 网页请求层使用数据版本、并发去重、取消、内存/IndexedDB 缓存；权威数据仍在后端。

行情页包括列表、卡片、双看板和全屏详情。画线实例由后端按标的保存；新建图形的默认样式、吸附、跨周期和连续画线偏好由浏览器 `localStorage` 持久化。卡片图只读显示详情页画线，避免误编辑。期货页的总热度权重同样保存在 `localStorage`，仅在浏览器内线性重算当前 Gauge 与历史总线。

## 公式、策略与账户分析

### 当前四类操作信号（R4-T040/T041）

- 当前策略页只消费 `SignalStrategyManager.vue` 与 `web_api/signals.py`：开仓、加仓、减仓、平仓、方向、周期及 Rule AST，不包含市场资产范围、账户资金、仓位、风险或成交配置。新规则复用 `strategy_definition.py` 的类型/函数版本/深度校验和 `evaluate_rule_series` 的纯规则求值；只有信号适配路径关闭资产白名单限定，旧 Definition 校验默认不变，缺行情字段仍保持不可用。
- `signal_monitor.py` 按标的+方向维护独立轮次，开仓开始、加减仓观察、平仓优先结束；同根新开仓后不触发后续动作。首次只消费最新已结束 bar，后续按精确策略版本与时间游标消费新增序列，排除显式 partial 和未来结束时间。跨周期使用结束时间排序，行情页标记按源 bar 时间与对应周期定位。
- `strategies/signals/definitions.json` 保存当前信号定义，递增版本防止迟到编辑覆盖；删除保留 tombstone。`monitor.json` 原子保存轮次、消费游标及最多 2,000 条近期事件。它们是独立个人观察数据，不是账户持仓，不与旧资源文件、历史回测结果混写。
- `/api/signals/scan` 由本地页面显式调用，开仓按市场筛选，已有轮次继续检查所有启用同向后续策略。每批最多 100 标的，每周期取最近 500 根；超过窗口的已知监控缺口报错且不推进游标。按标的游标分页，平仓后从活动集合移除不导致下一批跳号。前端手动启动开仓扫描；行情页打开时每 30 秒检查活动轮次，无页面关闭后的常驻调度。
- 旧策略/回测/执行 API 与不可变定义保留作历史兼容，当前页面移除对应入口及默认列表；未自动删除或把历史策略转换为操作信号。以下回测描述仅适用于该历史子系统。

- 桌面公式引擎只允许白名单语法和函数，限制表达式大小、幂运算和历史窗口，不允许任意 Python、文件或网络访问。
- 时间序列指标禁止未来数据；缺失历史和除零返回不可用原因而非伪造数值。
- 策略扫描结果仍是观察信号，不是自动交易指令；事件驱动回测仅在隔离的 Backtest 执行适配器中模拟订单、成交、成本和权益，绝不触达真实 Order API。
- 账户分析使用独立本地个人数据，支持 FIFO、持仓、CSV 和回收站；不能随行情包替换而丢失。

版本化策略资源使用单向依赖：

```text
Market Data ------------------> Market Indicator --------> Chart
                \-> Strategy Function -> Derived Indicator -^
External PASS Series ----------> External Market Indicator -^
Strategy Function ---------------------------------------> Strategy -> OrderIntent -> Risk Engine -> Execution Adapter -> Order API
```

- `StrategyFunctionRegistry` 是唯一算法目录。函数确定、无副作用，行情序列、基本面值和参数全部显式传入。
- 由当前行情推导的 Indicator 只依赖精确版本的 Strategy Function 并增加 Plot/样式元数据；External Market Indicator 是显式例外，只能读取受来源门保护的外部标准序列。两者都不能访问账户、持仓、订单或执行端口，策略也不能将任一 Indicator 当作执行依赖。
- `indicator_calculation.py` 只把已注册 Indicator Definition 解析成对齐的 Indicator Instance 序列；单实例错误以不可用状态隔离，Web 适配器不得复制 SMA/ATR 等算法。K 线保存的是 definitionId/version/parameters/style/visible/placement，计算结果不是新的权威行情。
- 高级副图同样通过 Strategy Function：Chaikin Volatility 使用 `EMA(H-L)` 的历史百分比变化，RVI 固定为 Dorsey 1993 的 close-based 变体；两者公开公式、字段、暖机及限制随注册定义返回。Twiggs® 原始算法未公开，`indicator.twiggs_volatility` 明确标记为 `100 × Wilder ATR / close` 的公开 ATR% 变体，不声称与专有实现一致。
- `indicator.volume_profile` 是主图的范围型 Plot：`market.volume_profile@1` 只把当前可见闭区间内每根 bar 的完整 `volume` 分配给其 HLC3 所在等宽价格桶，再以 POC 为起点按相邻桶成交量扩张价值区。响应携带算法、代表价格、归集、范围、桶数和价值区比例版本化元数据；缺失/零成交量保持不可用，绝不从 `amount` 推导。KLineChart 只渲染与本地图窗范围一致的结果，缩放后由行情页防抖重算，不持久化计算结果或屏幕像素。
- `drawing.fibonacci_retracement@1` 是 Strategy Function/Indicator 体系外的版本化 `drawing_tool`。每条文档保存两个逻辑时间/价格锚点及带标签的有限比例；`price = anchorEnd + (anchorStart − anchorEnd) × ratio` 使 0% 对应第二锚点、100% 对应第一锚点，并保留反向画出的波段方向。KLineChart 只由这些逻辑数据绘制线条、价格标签、两个端点及命中区；保存接口限制两个有限锚点、2–16 个不重复的 `[-10,10]` 比例和 `version=1`，不迁移或拒绝历史非 Fibonacci 文档。画线读取以本地编辑修订号防止迟到 GET 覆盖未完成保存。
- `indicator.vix@1` 是 External Market Indicator，不调用 Strategy Function，也不能以当前标的 OHLC 合成。唯一生产者是受控的 `market-monitor vix-sync`：它从固定的 CBOE `VIX_History.csv` 拉取完整 CSV，验证日期/收盘价、日期唯一性和有限数值后，把 URL、内容 SHA-256、取得时间、PASS 与全量点原子写入数据根目录的 `external_market/vix/series.json`。计算层只读取该本地文件，并要求固定 `US.CBOE.INDEX.VIX` 映射、`sourceStatus=PASS`、来源、有限且唯一的交易日点和与最新点一致的 `asOfDate`。它按 bar 的显式交易日精确查找，缺失/美中日期差为 `null`，不作时区重写或前值填充；响应显式保留 `externalInstrumentId/source/asOfDate/coverage/points/status/reason`。浏览器从本地 API 读取结果并显示溯源信息，不直连外部服务；没有该 PASS 文件或当前窗口无交集时只返回结构化不可用状态。
- 策略页的 `StrategyRuleTreeEditor` 仅持有与 `VersionedStrategyDefinition` 同构的 camelCase Rule AST：编辑动作替换树的一小段，复制/排序/撤销也只移动 AST 值；保存边界才将字段转换为契约 snake_case。因此复杂历史树可再次编辑并发布下一版本，任何临时 UI 身份均不会进入权威 JSON。`StrategyOperandEditor` 依函数注册表提供每一输入位的类型、默认值与可选嵌套函数，并先按当前策略资产做本地诊断；`POST /definition/validate` 仍是函数版本、类型、深度和资产的最终可信验证。仓位、止损、止盈、最大回撤、加仓/再入场均属于 Definition，固定价格不采用百分比控件上限。
- Strategy 同样只依赖 Strategy Function，不能以 Indicator 作为执行依赖。桌面 Strategy 可以产生 `OrderIntent`，但只有风险与执行端口能将已接受意图转成外部订单。
- `strategy_execution.py` 是唯一订单意图组合边界：服务端从已持久化的精确 Strategy Definition 生成不可由请求伪造的 Capability Context，随后固定执行 `OrderIntent → Risk Engine → 对应运行模式 Execution Adapter`。适配器要求本次风险许可，不能直接接收未裁决意图；幂等记录按 `intentId` 不可覆盖，审计事件只写允许字段而不写账户权益、请求正文或凭据。
- Backtest 与 Paper 使用两个隔离模拟适配器，结果明确说明没有触达真实 Order API；Live 没有可注册适配器，能力查询返回 `DISABLED`，写请求返回 409。Android、Indicator 与 Strategy Function 均无法取得 `order_intent_create` 能力。旧 `dsl_v1/formula_v1/builder_v1` 定义通过适配层继续读取。
- `strategy_backtest.py` 从本地 Silver 的精确数据版本读取 bar，在信号 bar 收盘后评估 Rule AST，再按定义以下一根 open/close 生成模拟成交；止损止盈、仓位、费用、滑点和合约乘数均写入 `OrderIntent/fill/trade/equity point`。两项内置策略为 `MA Crossover`（复用 `technical.sma@1`）与 `Donchian+ATR`。每次运行把 `definitionHash` 和完整 `dependencyLock` 持久化：策略/每个函数的精确版本与定义哈希、显式空的图表指标锁、参数值/哈希、数据版本/哈希/查询窗口及引擎版本。`technical.atr@1` 与 `@2` 是两个可并存的运行时身份，AST 执行器不会把 v1 调用替换为 v2。
- 已发布的结构化策略仅可写入连续的新 `id@version.json`；删除是生成 `deprecated` 归档版本，不删除历史定义。旧未版本化资源的迁移先把原件存入迁移专属备份，只有没有被回测引用时才能回滚。`POST /api/strategy/backtests/{runId}/reproduce` 只按已存 dependency lock 的精确版本重跑，并先验证策略/函数定义哈希与行情哈希；缺失或变化返回结构化冲突，绝不使用 latest 代替历史版本。
- `strategy_report.py` 只由该运行记录的 equity/fill/trade 明细计算指标，零分母与数值溢出保留结构化不可用原因。预版本锁的旧运行仍可生成报告，但将 `definitionHash/dependencyLock` 明确返回 `null`，不得据此声称可复现；新运行必须具备完整锁。`/api/strategy/backtests/{runId}/report` 和 `/trades.csv` 与图表 marker/交易表消费同一记录，修改参数仅重新回测，不重新请求行情窗口。
- `strategy_transfer.py` 的桌面传输 ZIP 只打包一个已发布 custom Rule-AST Definition、其精确 Strategy Function lock、定义/传输 Manifest Schema 和哈希测试向量；不包含账户、凭据、运行记录、行情或 OrderIntent。`manifest` 为每个内容文件记录 SHA-256，读取器先限制压缩包和总解压尺寸、拒绝重复/未知/路径穿越项并比对本地 allow-list Schema，之后才重新验证 AST 和依赖锁。包可附 Ed25519 manifest 签名并验证嵌入公钥指纹，但 `target=desktop` 永不转化为 Android 兼容声明；Android 仍只接受 ADR-0008 的受信任、声明式 DSL 包。`/packages/preview` 全程无写入，`/packages/import` 只允许无冲突无损导入，或由用户明确改名/创建连续新版本，绝不覆盖 immutable `id@version`。
- `strategy_templates.py` 的三项 builtin 模板是只读元数据和确定 Definition 来源：`MA Crossover`、`Donchian + ATR` 与 Schema 合法但不触发交易的空白起点。创建操作只复制为新的 custom v1 并写入 `templateSource(templateId/version/sourceStrategyVersion/defaultOverrides)`，不会改动内置来源。创建向导先显示精确函数依赖、资产范围、参数和风险预览，以及不构成收益承诺的免责声明。
- Definition 契约为未来 `community/plugin` 预留 `trustMetadata(publisher, signature, trustState, reviewStatus)`，但本轮没有社区服务、下载或信任升级端点。此类资源只有 `disabled` 状态可被识别/展示；不论 fixture 是否声称已签名或 trusted，当前服务端和页面均拒绝其回测、订单、编辑、复制和状态升级。只有未来经 ADR 批准的审核/授权边界才能改变这一默认拒绝。
- 持久化契约是 `contracts/strategy-resource.schema.json`，网页 camelCase 类型位于 `desktop/web/src/domain/strategyTypes.ts`；权限决策见 ADR-0010。

## R4 标的详情显示层（2026-09-06）

- `chartPresentation.ts` 定义五种图表显示类型与平均 K 线变换。变换只进入 ECharts 主价格序列与显示坐标范围，原始报价、Indicator/Strategy 输入和标准行情不变。
- `KLineChart.vue` 的自由笔刷在相邻 bar 像素位置间插值逻辑时间，保持价格连续，不使用 OHLC 吸附；趋势线保存两个逻辑锚点。`LaserCanvas.vue` 仅保留短时屏幕轨迹，不产生 ChartDrawing，不写本地数据文件。
- `MarketView.vue` 复用策略指标目录提供大弹窗、共享收藏和实例悬浮图例。跨周期及样式属于浏览器实例偏好，不改变注册表定义。回放只消费当前窗口的前缀，指标 API 请求同步裁切；回放期间隐藏既有策略结果和持久画线，退出恢复原窗口。
- R4-T039 已按用户要求移除价格警报界面、轮询及计算函数，历史浏览器偏好不自动删除。回放控件位于底部，图标选择起点、播放/暂停、倍速、单步和快进；图上点击选起点仍裁切所有指标请求，不接入真实账户。
- `QuoteValues.vue` 由详情、列表及卡片复用，无报价日期时间；开收、量额及涨跌分红绿，其余按字段分色。沉淀资金读取已加载窗口末端快照，总/流通市值读取标的最新元数据，三者不随鼠标悬浮改变；缺失仍为破折号。日线以上时间轴仅显示日期。工具组菜单及图表类型使用项目 SVG 图标，不复制参考站源码。

## R4 行情列表与详情统一状态（2026-09-06）

- 市场目录仅由 `market_classification.json` 公开；`cn-future-main` 和 `cn-future-weighted` 直接消费标准 `seriesKind`，夜盘保留为采集/交易时段 metadata。`web_api/market.py` 以数据版本缓存批量日线快照与近 3/5/10/22/44 交易日收益率，缺失值携带原因并保持为空。
- `domain/marketList.ts` 是排序、默认市场、星期格式和输入焦点边界的共享纯逻辑。全部行情通过 `/market/all/`、目标行情通过 `/market/targets/`、详情通过 `/market/instrument/:instrumentId/`，服务端把详情 URL 交回 SPA shell；列表排序和类别由同一上下文供详情左栏复用。
- 全部行情自动完成 API 分页并去重，再用固定行高的可视窗口渲染。重复/空页导致无法达到服务端总数时展示不完整错误，不把局部集合说成完整市场。
- 详情以固定视口 Grid 呈现：左侧当前市场列表、中间图表、最右绘图栏。`QuoteValues` 是图表 overlay；量和额/持仓使用独立坐标轴、重叠柱和主题 token。指针更新采用“保存最新位置、每帧消费一次”的通道，避免由十字光标触发全 series 重建。

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

- 通达信 `tdx-cn-v2` 已完成一次可回滚全量迁移与后续金融终端 `ds` 增量重导；隔离覆盖和跨来源样本仍以最新迁移报告为准，完整跨端验收尚未执行。
- TickDB 已从活动代码和数据源能力中移除；历史下载记录不能代表当前可用数据。
- 本轮发布按用户要求执行针对性验证、网页构建和真实迁移完整性检查；完整跨端验证仍可在后续发布前单独执行。
- 期货多空热度最新资金覆盖为 73/74；长历史 `FundScore10` 受月份合约 Silver 限制仅覆盖 237/5,244 日，近 1 年为 222/243。交易规则与统一日历目前来自已本地快照的 AKShare 公共上游，仍需持续监控字段和来源变化。
- 历史 R3 的完整跨端验收记录仍需保留，不应与本轮针对性验证结果混同。

## 2026-09-07 - R4 续2行情页面架构

本节保留续2历史；报价槽、副图和详情周期当前规则见下方续3。

- 顶部导航直接路由到全部行情和目标行情；全部、目标、详情为独立 route component。市场分类、搜索、排序、选中标的与看板周期由 Pinia 市场状态承接，目标信号监控由独立 store 保存，避免导航卸载即丢失监控上下文。
- K 线图新增 `fill` 容器模式并保留显式高度兼容。布局根据 ResizeObserver 的实际容器高度计算，图表 Overlay 与工具控件位于 Canvas 上方，非控件 Overlay 不截获指针。
- 成交量与成交额/持仓量为同中心 custom-series 矩形，宽度和 Y 轴独立；柱形几何在 `chartLayout.ts` 集中，避免页面 CSS 或 ECharts 内部默认 barGap 决定业务可读性。

## 2026-09-08 - R4 续3共享显示规则

- `marketQuote.ts`集中有限数、百分比与颜色；列表和详情左栏共用。`QuoteValues`使用104px公共槽、14列/7列两排；不足只在报价区域横滚，非控件Overlay指针穿透，支持Shift+滚轮和键盘浏览。
- `chartTime.ts`分离简洁轴日期与完整十字日期/星期/分钟时间，沿用标准数据自然日期，不重写交易日或来源时间。
- `chartSubchart.ts`构建独立Y轴的成交量bar与额/持仓line，线在上层、缺失断开。能力解析结合metadata和有限真实观测，Pinia保留同一标的已确认持仓能力。
- 实测Overlay包含报价与图例；ResizeObserver/rAF在尺寸或DPR变化时刷新，卸载统一释放。主图镜像轴和原逻辑画线坐标保留。
- 详情底部10周期nav复用请求取消/缓存；左栏名称首行，代码/价格/涨幅第二行。画线工具、画线实例及指标实例共用 `chartIcons.ts` 跨周期定义。

## 2026-09-10 - R4 续4桌面图表布局（当前规则）

- `quoteFieldGroups.ts`统一开/收、高/低、涨幅/振幅、涨跌/结、量/额、持仓/沉淀资金、总/流通市值七组。`QuoteValues`采用固定比例七列、每列双行；ResizeObserver随容器宽度更新字号预算，报价变化不改变列位置。较窄看板同时保留14字段，长值缩小字号，不省略、不依赖横滚；缺失为`--`。
- 名称/代码、报价、actions周期控件位于同一Canvas顶部Overlay。图例位于独立绝对定位层，复用主图顶部坐标，但不参与主图高度测量。详情主副图gap为0，量/额持仓标题在副图内部；普通看板保留原间距。
- 价格与副图各轴格式化文本共同测量gutter，字体及label margin统一；保留镜像价格轴和双独立量纲。成交量透明度集中为0.8，额/持仓仍为上层折线。
- 详情价幅右列上下排列；36px单行底部周期导航仅可横向滚动。文本按钮复用active状态，持久绘图和激光收尾共用`finishDrawing`。激光退出后继续自然渐隐，active关闭、取消、lost capture及卸载统一释放捕获。
- 无数据迁移、无移动端设计；续3布局作为历史保留。本批证据见`Plan_R4.md`。
