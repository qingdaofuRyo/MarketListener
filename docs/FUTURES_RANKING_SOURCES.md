# 国内期货龙虎榜来源与覆盖口径

## 事实来源

“期货席位分布”和“席位持仓分布”读取的是同一批交易所已公布的会员方向排名；两个视图均包含商品与CFFEX金融期货。首选事实来源为各交易所官网当日的“日交易排名 / 成交持仓排名”，按交易日、交易所、品种、月份合约、方向、公布名次和会员席位留存；东方财富、文华财经、同花顺等聚合网站只作人工交叉核对。它们不能替代缺失的交易所公布记录，也不应写成交易所原始来源。

用户所称“交易资金”已明确为**成交额**。逐合约成交额、市场总持仓量与结算价应从各交易所“每日行情 / 日交易快讯”读取；席位多空持仓从该所“日交易排名 / 成交持仓排名”读取。这是两种不同报表，不能把排名表内已公布席位持仓相加冒充合约全市场持仓。示例：[郑商所每日行情](https://www.czce.com.cn/cn/jysj/mrhq/H077003001index_1.htm)来自`czce.com.cn`；大商所官网是`dce.com.cn`。成交额必须保存官方原单位以及单边或双边统计说明，再进行人民币换算与跨合约汇总；当前期货通原始K线的成交额字段不足以替代交易所日行情采集。

| 交易所 | 官网发布入口 | 现行直接适配器 | 独立成交排名状态 |
|---|---|---|
| 上期所 SHFE | [官方日交易排名JSON](https://www.shfe.com.cn/data/tradedata/future/dailydata/pm20260828.dat) | `official-shfe-rank-v1`，同一官方响应拆分成交/多头/空头，按合约识别INE | 旧AKShare事实仅留历史兼容 |
| 上期能源 INE | 由[上期所官网排名JSON](https://www.shfe.com.cn/data/tradedata/future/dailydata/pm20260828.dat)发布，记录publisher=INE | `official-shfe-rank-v1`按INE具体合约归类 | 同一响应按INE独立存储 |
| 大商所 DCE | [官方成交持仓排名](https://www.dce.com.cn/dalianshangpin/xqsj/tjsj26/rtj/rcjccpm/index.html)；采集文件入口为官网`memberDealPosi/batchDownload` | `official-dce-rank-v1`，ZIP内成交/多头/空头分开解析 | HTTP 412留失败，不回退聚合站 |
| 郑商所 CZCE | [官方成交排名](https://www.czce.com.cn/cn/jysj/cjpm/H077003005index_1.htm)；[官方持仓排名](https://www.czce.com.cn/cn/jysj/ccpm/H077003004index_1.htm)，持仓文件为`FutureDataHolding.xlsx` | `official-czce-rank-v1`当前解析持仓表；成交榜文件/API仍待成功官网探针确认 | 不从持仓榜推算成交榜 |
| 广期所 GFEX | [官方日成交持仓排名](https://www.gfex.com.cn/gfex/rcjccpm/hqsj_tjsj.shtml)；官网数据接口位于`u/interfacesWebVariety`与`u/interfacesWebTiMemberDealPosiQuotes` | `official-gfex-rank-v1`分别请求成交/多头/空头，拒绝校验HTML | 校验页返回时暂停本所 |
| 中金所 CFFEX | [官方成交持仓排名](https://www.cffex.com.cn/cn/ccpm.html) | `official-cffex-rank-v1`按品种CSV独立保存成交与持仓排名；进入两张席位图 | 上榜阈值/前20名限制来自交易所发布规则 |

排名表中的`publisher`、`sourceUrl`、`adapter`、`retrievedAt`、`payloadSha256`、`qualityStatus`彼此独立。旧记录回填已知交易所发布方时保留原始`akshare-*`来源于`legacyAdapter`，质量标记`LEGACY_WRAPPED`且不重写原排名数值；官网直连行记录官网URL与官网读取器。原始成功响应先入Bronze，携带内容SHA-256、解析版本和质量状态。官网不可访问时单独记失败，不用聚合网站补位，也不把失败解释成没有持仓。

失败探针仍保存交易日、官网URL、取得时间、HTTP/解析错误、响应内容SHA-256和`FAILED`质量状态。只有通过预期媒体类型检查的文件/JSON响应正文写入Bronze；412响应和会话校验HTML只留摘要及错误，不归档可能含临时挑战参数的正文。

## 已实测状态（2026-09-27）

直接官网同步实际取样日期为2026-08-28，入库7,288条方向排名：SHFE 2,951、INE 321、CZCE 4,016。DCE官方批量下载返回HTTP 412；GFEX HTTP 200响应实际是混淆校验HTML，未当JSON解析、未写排名，且未固化临时校验参数。单所失败没有影响另外三类官方响应入库。历史包装覆盖还显示：旧的2026-08-28采集曾有CFFEX 880和GFEX 480，但它们不等于本次直连接口成功；生产排名以各自官网探针为准。成交排名独立写入`futures_member_trade_ranks`，不混入持仓或全市场成交量计算；CZCE成交榜静态数据下载路径尚需一次成功官网探针才能宣称已回补。

2026-09-24按新速率限制实际探针：席位持仓排名入库7,789条（CFFEX 800、CZCE 4,045、SHFE 2,649、INE 295）；DCE为HTTP 412，GFEX为HTTP 200但返回`text/html`校验页，均无事实行。独立成交排名入库1,876条（CFFEX 400、SHFE 1,330、INE 146）；DCE/CZCE/GFEX当前无成交榜事实行，尤其CZCE需要先确认其“成交排名”页面对应的可下载静态报表/API，不能把其持仓表复用成成交数据。实际数据最新有行日期因此由2026-08-28推进至2026-09-24，但仍只覆盖4个排名发布方。

旧本地采集观察日2026-09-25在可用包装路径上均无可用排名行；页面单独保留该失败观察日，同时默认排行回退到最近有真实排名行的2026-09-24，不把最新失败日误当作最新有持仓日。

交易所排名往往只公布满足条件的合约和前若干席位；某会员未上榜的方向是“未公布”，不是零持仓。期货席位分布按`exchange.product`品种键/具体合约查上榜席位；席位持仓分布按完全相同的规范化席位名跨所反查该席位上榜的商品。同名规范键可汇总，未经显式别名表不得模糊匹配。两种视角都保留来源交易所、交易日、公布名次、方向和来源覆盖，合计仅代表已公布排名范围。CFFEX纳入两张席位图及品种结构图。

## 逐合约官方成交额

月份合约热度的成交额与会员龙虎榜是不同事实表：成交额由各所“每日行情/日交易快讯”取得，龙虎榜持仓由会员排名报表取得。SHFE/INE/CZCE/GFEX/CFFEX当前经`akshare.get_futures_daily`按交易日抓取；DCE改为直接请求官网JSON。经合约代码映射为具体交割月份后保存来源URL、原始成交额值、人民币换算值、单位、单边口径、收盘核对、响应摘要和逐所覆盖状态。没有通过日期/合约/收盘匹配的行不会计入评分。

对DCE，项目现在直接向官方 [`/dcereport/publicweb/dailystat/dayQuotes`](http://www.dce.com.cn/dcereport/publicweb/dailystat/dayQuotes) 发送`tradeDate`、`tradeType=1`、`statisticsType=0`、`varietyId=all`等JSON请求，超时15秒；返回成功时先将`contractId/clearPrice/openInterest/turnover`等原始字段写入Bronze供审计。2026-09-26在本机按同一公开格式直接POST仍返回HTTP 412；旧官方门户`portal.dce.com.cn/publicweb/quotesdata/dayQuotesCh.html`返回502。用户提供的`VoGRv6Ir`长查询参数属于网页会话校验形态，不能固化为API凭据或宣称为开放接口。官方接口可访问时仍需验证成交额单位、单/双边口径及具体合约的结算/持仓字段后再升为PASS；当前归一器明确拒绝DCE未核验数值。

来源隔离的官方逐合约表新增`openInterest`，与已存的`settlement`、`volume`、`close`、成交额原值和人民币值一同可通过本机`/api/futures/official-daily?trading_day=...&exchange=...&contract_code=...`核对。旧记录的新字段为`null`，不能用期货通值逐字段混拼成“交易所官方持仓”。

目前归一器只接受2026年及以后、经初步审计为“万元、单边”的数据；更早历史需另行核验各所历史单位与口径。用户给的 [郑商所每日行情](https://www.czce.com.cn/cn/jysj/mrhq/H077003001index_1.htm) 是CZCE；大商所官方入口为 [DCE每日行情](http://www.dce.com.cn/dce/channel/list/168.html)。两者不可混为同一所。

2026-09-08 实测：SHFE 305行（256行有正成交额）、INE 67（48）、CZCE 239（190）、GFEX 48（41）、CFFEX 28（28）；DCE未取得有效表，官方网页直接请求返回HTTP 412。与本地有效月份合约收盘核对后，488/749个有方向合约有可用成交额，覆盖65.15%，并非完整全市场。评分只表示核验过的官方来源样本；低于80%时数据质量为`PARTIAL`，图表同时显示覆盖率和缺失交易所。缺失成交额不能补零、不能用成交量推算，也不能未经用户确认改用第三方聚合源。

“更新期货热度”会增量导入期货通本地月份合约，仅同步最新Silver交易日的官方日行情与规则并重建派生序列。它不会自动补齐所有历史官方日行情；在大商所接入/API问题解决且历史单位完成审计前，旧历史点仍可能没有成交额热度，后续每日成功采集才逐步延长可用趋势。
