# 正式开发历史状态与 R4 入口

> 2026-08-27 状态说明：当前唯一活动计划为根目录 `Plan_R4.md`。本文 `FULL-*` 表格及 R3 摘要保留历史开发、审查和验收证据，不再作为当前待办入口。

## R4 当前摘要

- `R4-T008 / R4-FUTURES-LONG-SHORT-HEAT` 已完成并进入 `DONE`：新增 `/futures/` 多空热度模块、三 Gauge、用户权重滑块与同图三线历史趋势；底层使用 10 个有效交易日、半衰期 3 的指数衰减，不再沿用早期未锁定的 5 日草案。
- Gold `FUTURES_LONG_SHORT_HEAT` 已真实重建 5,244 个交易日；最新 2026-08-21 方向覆盖 `74/74`、资金覆盖 `73/74=98.65%`，Breadth10=`33.2266`、Fund10=`42.8198`，资金门槛已通过，剩余 1 个品种缺口保持 `PARTIAL`。
- 已持久化 8,797 个统一交易日和 1,285 个精确日期规则快照（82,141 条品种规则/91,633 条合约保证金 override）；API 只读 Gold，用户 40/60 总分不作为唯一历史真值入库。
- 本地月份合约长历史不完整，`FundScore10` 当前可用 237/5,244 日；近 1 年覆盖 222/243，近 5 年覆盖 237/1,211。API 显式返回所选区间覆盖，缺失日为 `null`，不得把品种线的 5,244 日误写成完整三线历史。
- 最终验收：桌面 `771` 项测试、Vue build、完整 Playwright `25/25` 和统一 `verify.ps1` 均通过；多空热度专项在增加三 Gauge/末点逐值一致性和离开页面后重进持久化断言后再次 `6/6` 通过。
- R4 第二条进入 `PLAN_CREATED`：画线工具的线条/文字色和箱体填充色统一升级为固定 80 色预设调色板，并提供自定义颜色和 0%–100% Alpha 调整；现有分图形类型持久偏好必须向后兼容。
- R4 第三条进入 `PLAN_CREATED`：K 线主图新增多点 `brush` 笔刷，支持连续拖动绘制、折线简化、整体移动、颜色/粗细/线型、锁定、跨周期、删除和独立默认样式；旧画线 JSON 无迁移。
- R4-T004～R4-T007 已独立登记四张商品期货结构图：品种市值、席位市值、品种持仓、席位持仓统一采用固定顺序堆叠面积图、固定 `1.5%`“其他”集合及下钻；市值明确为单边持仓名义规模，不乘保证金率或额外乘二。
- `R4-T004` 与席位相关的 `R4-T005/R4-T007` 仍处 `ANALYSIS`，分别等待价格基准和真实多交易所席位覆盖审计；纯持仓聚合的 `R4-T006` 为 `PLAN_CREATED`。本轮仅完成文档计划，没有修改代码或数据。
- 当前任务、接口草案、数据来源边界和验收标准只以 `Plan_R4.md` 为准。

## R3 历史摘要

- 行情页、画线、市场分类、查询缓存、账户/策略、期货、本地通达信导入和数据源界面已形成 R3 实现，详见 `Plan_R3.md`。
- 2026-08-24 完整验证通过：Ruff、共享 Schema、738 项桌面 pytest、Android lint/JVM/APK、Vue build 和 19 项 Playwright；工作区清理、安全审计与 GitHub 发布已完成，R3 主体提交为 `ce91a83`，`R3-T011` 已完成。
- R4-T011 已实现 `tdx-cn-v2` 资产级价格精度、逐行成交量倍率、B 股独立分类、原始值追溯、隔离质量门和可回滚来源替换；2026-08-29 正在执行真实全量暂存重建。TickDB 活动代码已移除，旧专项审计只保留为历史证据。

## 2026-08-09 历史状态

> 以下是当日原始状态摘要：Android 五大页面、数据页与桌面→手机同步链路已完成并实测；Android 同步包下载/手动导入两处报错已修复；真实 K 线覆盖 48 标的 / 72,321 根（部分覆盖，接口如实展示）；720+ 篇研报知识库流水线完成：721 篇 JSON 全部 REVIEWED / 33,193 条事实 / 721 篇核验通过（含 1 篇 OCR 补偿、1 篇源缺失保留），生成图谱页 `/industry/` 与新版全景页 `/industry-v2/`；桌面 pytest 525 项、Android JVM 74 项全部通过；市场板块脏词过滤修复、Atlas v2 重建为 75 条链 / 7,090 家带代码公司 / F10 CN 5,539 + HK 2,806，A 股收入构成已补齐 5,539 家，同步包为 `market-20260809-081649-141aff2e`。

> 状态说明（2026-08-06 正式开发口径）：项目不再区分 P0/FULL 阶段；下文 `FULL-*` 编号仅作历史追踪编号，
> 不代表优先级或阶段。当前开发以 5.1–5.9 系列任务为准，全部完成后统一测试、审查与验收；
> 本表保留为历史状态档案，新进展以交付记录与最终验收报告为准。

## 当前入口

- 当前工作模式：系列统一审查+验收（用户 2026-08-06 指示：完成 5.1–5.9 所有任务后再审查；现非门控实现已全部完成，Android/DSL/图谱集群验收已推进）。
- 当前任务：Android 五大页面（行情/数据/策略/统计/产业链底部导航）与数据页已完成，行情页含“从电脑同步”入口；桌面→手机同步链路（package_builder → 8765 后端 → Android 导入）已实测；历史验收记录：Android/DSL/图谱集群本机重跑（accept_android2）、数据/契约/流水线集群（`docs/reviews/acceptance-data.md`、`docs/reviews/unified-review-data.md`）；FULL-803 本机回归与 Release APK 已完成（进入 `ACCEPTANCE`），FULL-900 等待 803 外部解除条件。
- 当前状态：FULL-400/401/402/403/500/501/502/503/700/701/702/703=`ACCEPTED`；FULL-123/300/301/302/303/404/504/704=`ACCEPTANCE`（真机解除条件未满足）；FULL-110/111/112/113/120/122/600/602/800=`ACCEPTANCE`（外部数据/凭据/网络/连续运行条件未满足）；FULL-803=`ACCEPTANCE`（真实数据/真机/签名/连续运行未满足）；FULL-900=`IN_PROGRESS`（封板准备完成，待 803 外部条件）；`FULL-610`/`FULL-804`=`BLOCKED`（外部条件未满足）。
- 当前证据：`docs/deliveries/FULL-*.md` 全部任务均有交付记录；2026-08-09 本机实测桌面 pytest 525 项（junit XML 记录，0 失败，新增覆盖统计、研报流水线、per-fact 链聚合与 atlas 回归测试）、ruff 通过、Android JVM 74 项（21 suite/0 失败）、lintDebug 0 errors（9 warnings）、assembleDebug 成功、APK `android/app/build/outputs/apk/debug/app-debug.apk`；Android/DSL/图谱验收证据见 `docs/reviews/acceptance-android-dsl-graph.md`；审查证据见 `docs/reviews/review-android-chain.md` 与 `docs/reviews/review-android-fixes-rereview.md`。
- 状态说明：审查/验收按角色分离；真实凭据/设备/网络类任务继续如实记录，不伪造。
- 本次状态更新：非门控实现全部进入 `REVIEW`；`FULL-610`/`FULL-804` 置 `BLOCKED`；`FULL-803` 进入 `IN_PROGRESS`（发布清单与 16KB 检查就绪，全量回归随统一审查推进）。
- 本次状态更新：`FULL-500`~`FULL-504`=`PENDING`→`REVIEW`（5.6 系列实现完成：个人库交易账本数据模型/迁移、录入/导入/修订/持仓计算、统计归因、加密备份恢复、复盘 UI；Android JVM 54 项测试与 lint 全绿；真机流程待设备验收）。
- 本次状态更新：Android 集群 FULL-300/301/302/303/402/403/404/500/501/502/503/504/704 由 `REVIEW` 进入 `ACCEPTANCE`（FULL-501 导入外键顺序/修订状态校验/空账本零值快照与 FULL-504 真实收盘价接线复审通过；其余 P2/P3 保留为验收阶段清单），证据见 `docs/reviews/review-android-fixes-rereview.md`。
- 本次状态更新：accept_android2 独立验收完成——FULL-400/401/402/403/500/501/502/503/700/701/702/703 置 `ACCEPTED`（本机重跑通过）；FULL-123/300/301/302/303/404/504/704 维持 `ACCEPTANCE` 并写明 Android 13+ 真机解除条件；证据见 `docs/reviews/acceptance-android-dsl-graph.md` 与各交付文档“独立验收”章节。
- 本次状态更新：数据链 FULL-110/111/112/113/120/122/600/602/800 由 `REVIEW` 进入 `ACCEPTANCE`（审查补遗与验收记录见 `docs/reviews/unified-review-data.md`、`docs/reviews/acceptance-data.md`，root 代行并如实标注）；FULL-803 由 `IN_PROGRESS` 进入 `ACCEPTANCE`（全量回归 PASS、Release APK 构建与 16KB 检查通过、未签名；见 `docs/deliveries/FULL-803.md` 与 `docs/release-checklist.md`）。
- 本次状态更新：FULL-900 由 `PENDING` 进入 `IN_PROGRESS`——版本一致（0.1.0/versionCode 1）、Release APK 未签名+16KB PASS、封板报告包 `docs/release/`（能力/质量/验收/已知缺口）齐备；封板需 803 外部解除条件、keystore 签名与工作提交后打标签，见 `docs/deliveries/FULL-900.md`。
- 本次状态更新（2026-08-09）：Android 五页与数据页实现完成——`MainActivity` 改底部 NavigationBar 五项；新增 `DataScreen.kt`（分组卡片+搜索+空态）与 `MetricGroups.kt`（纯函数分组/聚合/格式化），`ImportedMarketData.kt` 容错读取 `gold_metrics`；行情页新增“从电脑同步”卡片，输入 `http://<电脑IP>:8765` 下载 `/api/android-package` 并交给导入 Worker；桌面 `package_builder` 打包全量 COMPLETE silver + gold_metrics 并签名激活，后端 8765 在线；工作区保持未提交（用户明确要求不 commit）。
- 本次状态更新（2026-08-09）：Android 同步链路两处报错已修复——`MainActivity.kt` 默认服务器地址改为电脑当前 IPv4 `http://192.168.1.88:8765`，同步/导入成功后刷新产业链 HTML，导入失败返回 `RESULT_ERROR_DETAIL` 明细；`MarketPackageImporter.kt` 白名单与抽取条目加入 `industry/industry-map.html`，手动导入 zip 不再报“行情包结构无效”。
- 本次状态更新（2026-08-09）：720+ 篇研报知识库流水线全量跑通并补齐——`reports/industry/` 现有 721 个 `report_*.json`（全部 REVIEWED，33,193 条事实）；`reports verify` 721 通过 / 0 复核（新增财信证券 AI短剧 PDF 解析 37 事实、银河证券光器件扫描件 OCR 补偿 60 事实、中信期货量化 CTA 源缺失标记 `source_missing` 后保留 42 事实）；`reports chains` 聚合 154 条产业链（22,149 条链上事实）并生成 `industry-map.html`（SVG 图谱，9.6 MB），快照同步 `data_control/industry/` 且已打入同步包；后端 `/industry/`、`/industry/industry-map.html` 实测 200；Android 产业链页改为加载网页快照，不重读研报。
- 本次状态更新（2026-08-09）：重建并激活同步包 `market-20260808-190946-deaecd38`（7,256,011 字节，含 `signature.ed25519`/`signature.ecdsa`/`payload.sqlite`/`industry/industry-map.html`）；后端 8765 重启到最新代码并实测 `/`、`/api/health`（含真实 coverage）、`/industry/`、`/industry/industry-map.html`、`/api/android-package` 全部 200；桌面 pytest 571 项通过（新增 `/api/health` 真实 parquet 覆盖统计测试、研报聚合/核验/SVG 图谱测试与 OCR 回退测试），Android `testDebugUnitTest assembleDebug` BUILD SUCCESSFUL（21 suites / 74 tests / 0 failures）。
- 本次状态更新（2026-08-09）：图谱路由终核验——`/industry/industry-map.html` 返回 9,628,645 字节，经逐字节比对确认为本地新版 9,647,124 字节经 `read_text` 将 18,479 处 CRLF 归一化为 LF 后的内容（与旧版文件无关）；同步包 zip 内 `industry/industry-map.html` 与本地原始文件 SHA256 均为 `785EF2FF0AC4C7709B915ED5A38EF0C1234A521B40CE927FCAB82786D1CAA5D1`；`/`、`/api/health`（48 标的 / 72,321 行）、`/industry/industry-map.html`、`/api/android-package` 实测全部 200。
- 本次状态更新（2026-08-09）：新版券商研报式产业链全景图完成第一版——新增 `desktop/src/market_monitor/industry_atlas.py` 与 CLI `reports atlas`，基于 155 条链 / 22,083 条事实 + 旧参考 HTML 快照生成 `reports/industry/industry-atlas.html/json`（2.7 MB 自包含、零 CDN、浅色分区、悬浮 F10、搜索缩放、证据抽屉），同步 `data_control/industry/`；PC 新增 `/industry-v2/` 与首页“产业链全景图（新版）”入口（实测 200）；Android 打包白名单/`MainActivity` 复制/`GraphScreen` 默认新版接入完成，`assembleDebug`+`testDebugUnitTest` BUILD SUCCESSFUL；全量 `pytest desktop/tests` 500+ 通过（新增 `test_industry_atlas.py` 5 项）；后端 8765 已用 venv 重启并实测 `/industry-v2/` 200。F10 全市场抓取进行中：用户确认 A 股 5000+、港股 1900+ 全部入库，子 Agent 限速/checkpoint/封禁暂停，完成后重跑 `reports atlas` 自动合并进全景图。
- 本次状态更新（2026-08-09）：研报补齐+OCR 重试完成——新增 `desktop/src/market_monitor/report_ocr.py`（PyMuPDF 渲染 + RapidOCR，延迟初始化/线程锁）与 `scripts/retry_report_ocr.py`（无 JSON 的 PDF、force 重跑 0 事实报告、标记 `source_missing`，默认重建 chain_index + industry-map.html，幂等）；财信证券 AI短剧（原缺失）解析 37 事实、银河证券光器件扫描件（原 0 事实）OCR 补偿 60 事实、中信期货量化 CTA 源缺失标记保留；`chain_index.json`/`industry-map.html` 重建为 154 链 / 721 报告 / 22,149 链上事实；`reports verify` 721/721 通过；桌面全量 pytest 571 项通过。另已定位 33,193 条事实中 11,044 条未进入链上统计的根因：抽取阶段 per-fact 链判定与报告级 top-5 链判定不一致；新版 `industry_atlas` 将直接按 `fact.chain` 分组，F10 完成后由主任务重跑 `reports atlas` 合并。

## 状态表

| 任务 | 状态 | 依赖 | 当前说明/解除条件 |
|---|---|---|---|
| FULL-001 | ACCEPTED | 无 | 独立验收已重跑全部文档专项验证并通过，见交付记录“独立验收”章节 |
| FULL-002 | ACCEPTED | 001 | 独立验收已重跑 Python/JDK/Gradle/SDK、锁定解析、安全扫描与 Android 构建并通过，见交付记录“独立验收”章节 |
| FULL-003 | ACCEPTED | 002 | 全新独立验收已重跑成功、错版、受控失败和故障恢复路径；50 个精确锁项与全部 Python/Android 基线子项均通过，见 `docs/deliveries/FULL-003.md` |
| FULL-100 | ACCEPTED | 003 | 全新独立验收已重跑统一验证和 v2/v1 迁移专项反例，确认 Schema/Python 一致、迁移无损且普通 v2 不能伪造迁移身份；见 `docs/deliveries/FULL-100.md` |
| FULL-101 | ACCEPTED | 100 | terra7 5×P1+1×P2 与性能回归全部修复；专项/桌面全量/Ruff/统一验证/Android 68 项全绿；代行验收见 `docs/reviews/acceptance-executed.md` |
| FULL-110 | ACCEPTANCE | 101 | 实现完成：AKShare 快照/日历/资金独立探测（失败不再抹掉其他结果）、日线字段归一与前复权；真实探针日历 PASS 8797 行，快照与资金在最终窗口因东财 `RemoteDisconnected` 如实 FAILED/NETWORK（直接探测曾 PASS 120 行/5976 行），见 `docs/deliveries/FULL-110.md` 与 `artifacts/full-110-akshare/`；解除条件=东财端点恢复后重跑真实探针；审查/验收记录见 `docs/reviews/unified-review-data.md` 与 `docs/reviews/acceptance-data.md` |
| FULL-111 | ACCEPTANCE | 101 | 实现完成：登录/额度/日线/30m/1m/ETF/指数/期货独立探针，新增 `jqdata-query-quota`（额度）；固定响应专项 21 项能力全 PASS，CLI 缺配置退出码 3；本机无 JQDATA 凭据，真实登录如实 `BLOCKED/CONFIGURATION`，见 `docs/deliveries/FULL-111.md`；解除条件=用户配置凭据后真实登录探测 |
| FULL-112 | ACCEPTANCE | 101 | 实现完成：Tushare 适配器 6 项能力（日历/日线/基础资料/财务/分钟/账户积分），`tushare==1.4.29` 依赖锁定并注册 CLI；固定响应专项 PASS，CLI 缺 token 退出码 3；本机无 TUSHARE_TOKEN，真实探测如实 `BLOCKED/CONFIGURATION`，见 `docs/deliveries/FULL-112.md`；解除条件=用户配置 token 后真实权限/积分探测 |
| FULL-113 | ACCEPTANCE | 101 | 实现完成：BaoStock 字段/周期/复权映射固定测试、网络超时与空结果分类；本机 `www.baostock.com:10030` TCP 超时，最新探针（2026-08-06 02:09）如实 FAILED/NETWORK，跨源重叠对比 `BLOCKED`（`row_blending: DISABLED`），见 `docs/deliveries/FULL-113.md` 与 `artifacts/full-113-baostock/`；解除条件=BaoStock 可达后真实日线与跨源对比 |
| FULL-120 | ACCEPTANCE | 110–113 至少三个接受 | 实现完成：按角色烘焙/回退/路由与 BLOCKED 语义（只认 PASS+非空证据，绝不逐行混源），专项 45 项与 Ruff 通过；真实三源门槛当前未满足（AKShare 仅日历 PASS、BaoStock 不可达、JQData/Tushare 缺凭据），真实决策如实 BLOCKED，见 `docs/deliveries/FULL-120.md`；解除条件=至少三个来源真实 PASS+非空证据 |
| FULL-121 | ACCEPTED | 120 | 契约层实现完成并复审通过；专项与 Android 共享夹具全绿；代行验收见 `docs/reviews/acceptance-executed.md` |
| FULL-122 | ACCEPTANCE | 121 | 统一审查 P1-1/P1-2/P2-2/P2-3/P2-5 已修复（时区无损、分区合并、INGEST_FAILED 不打包、Bronze+CLI 链路、质检接线）并复审通过；真实 Provider→签名包仍受外部条件限制，见 `docs/deliveries/FULL-122.md` 修复节与 `docs/reviews/rereview-data-fixes.md` |
| FULL-123 | ACCEPTANCE | 122 | 独立验收本机部分通过（Android JVM 68 项含 decodeMarketCandle）；解除条件=Android 13+ 16 KB 设备断网导入真实签名包并离线显示日线/分钟线（依赖真实 Provider 产物），见 `docs/reviews/acceptance-android-dsl-graph.md` |
| FULL-200 | ACCEPTED | 121 | 主数据/范围规则/点即时成员实现并复审通过；专项与 Ruff 全绿；代行验收见 `docs/reviews/acceptance-executed.md` |
| FULL-201 | ACCEPTED | 121 | 日历/交易时段/复权/公司行动实现并复审通过；固定样本全绿；代行验收见 `docs/reviews/acceptance-executed.md` |
| FULL-202 | ACCEPTED | 200、201 | 统一审查 P1-2/P1-3 修复后复审通过（分区合并、陈旧锁 TTL）；代行验收见 `docs/reviews/acceptance-executed.md` |
| FULL-203 | ACCEPTED | 202 | 统一审查 P1-4/P2-5 修复后复审通过（OHLC/成交量严格校验、时区与跨源接线）；代行验收见 `docs/reviews/acceptance-executed.md` |
| FULL-204 | ACCEPTED | 203 | 桌面层包协议实现并复审通过（DELTA/账本/签名哈希）；Android 端 DELTA 语义列入 FULL-300/303 验收清单；代行验收见 `docs/reviews/acceptance-executed.md` |
| FULL-300 | ACCEPTANCE | 123 | 独立验收本机部分通过；解除条件=真机 SQLCipher 打开/重启，替换或删除行情库后个人库数据保留；已知 P3（热库/删除接口未接线、双 UserDatabase 实例），见 `docs/reviews/acceptance-android-dsl-graph.md` |
| FULL-301 | ACCEPTANCE | 204、300 | 独立验收本机部分通过；解除条件=真机真实包查询、K 线缩放/平移、周期切换与截止时间显示；已知 P3（WebView allowFileAccess、readActive 吞错、无仪器化测试），见 `docs/reviews/acceptance-android-dsl-graph.md` |
| FULL-302 | ACCEPTANCE | 301 | 独立验收本机部分通过；解除条件=真机缺失/失败/陈旧状态显示（不显示为零或正常）；已知 P3（未知 quality_status 不计异常、无 Compose 测试），见 `docs/reviews/acceptance-android-dsl-graph.md` |
| FULL-303 | ACCEPTANCE | 302 | 独立验收本机部分通过；解除条件=真机目标数据量基准与低空间清理（个人库零删除）；已知 P2（清理 IOException 未捕获）与 P3（StatFs 回退），见 `docs/reviews/acceptance-android-dsl-graph.md` |
| FULL-400 | ACCEPTED | 121 | 独立验收（accept_android2）重跑 DSL/图谱/契约专项 115 项与 Android JVM 68 项全通过，见 `docs/reviews/acceptance-android-dsl-graph.md` 与交付文档“独立验收”章节 |
| FULL-401 | ACCEPTED | 203、400 | 独立验收（accept_android2）重跑专项与 Android JVM 全通过，见 `docs/reviews/acceptance-android-dsl-graph.md` 与交付文档“独立验收”章节 |
| FULL-402 | ACCEPTED | 300、400 | 独立验收（accept_android2）重跑通过（附 P3 已知缺口：windowRef 上限、节点数契约不一致），见 `docs/reviews/acceptance-android-dsl-graph.md` 与交付文档“独立验收”章节 |
| FULL-403 | ACCEPTED | 401、402 | 独立验收（accept_android2）重跑桌面 3 向量与 Android `DslSharedVectorsTest` 两端一致，见 `docs/reviews/acceptance-android-dsl-graph.md` 与交付文档“独立验收”章节 |
| FULL-404 | ACCEPTANCE | 301、403 | 独立验收本机部分通过；解除条件=真机参数编辑→运行→历史与信号解释流程；已知 P2（主线程解释器、历史参数丢失），见 `docs/reviews/acceptance-android-dsl-graph.md` |
| FULL-500 | ACCEPTED | 300 | 独立验收（accept_android2）重跑迁移/约束/CRUD 与 JVM 全量通过；SQLCipher 真机打开留已知设备缺口，见 `docs/reviews/acceptance-android-dsl-graph.md` 与交付文档“独立验收”章节 |
| FULL-501 | ACCEPTED | 500 | 独立验收（accept_android2）重跑外键顺序/修订状态/空账本修复与 JVM 全量通过；Room DAO 设备/仪器化路径留已知缺口，见 `docs/reviews/acceptance-android-dsl-graph.md` 与交付文档“独立验收”章节 |
| FULL-502 | ACCEPTED | 501、203 | 独立验收（accept_android2）重跑统计固定样本与 JVM 全量通过（附 P3：UTC 自然日划分），见 `docs/reviews/acceptance-android-dsl-graph.md` 与交付文档“独立验收”章节 |
| FULL-503 | ACCEPTED | 500 | 独立验收（accept_android2）重跑备份/恢复专项与 JVM 全量通过；真机 SAF/SQLCipher 完整往返留已知设备缺口，见 `docs/reviews/acceptance-android-dsl-graph.md` 与交付文档“独立验收”章节 |
| FULL-504 | ACCEPTANCE | 502、503 | 独立验收本机部分通过；解除条件=真机完整录入→复盘→加密备份→清库恢复→错误回滚流程；已知 P2（多标的收盘价回退、备份主线程），见 `docs/reviews/acceptance-android-dsl-graph.md` |
| FULL-600 | ACCEPTANCE | 204 | 实现完成：港股代表样本/列归一/日历/复权/市场范围复用既有引擎，固定样本 5 项 PASS；真实港股拉取因东财断连如实 FAILED/NETWORK，见 `docs/deliveries/FULL-600.md`；解除条件=港股真实跨源闭环 |
| FULL-601 | ACCEPTED | 204 | 期货主力/连续拼接实现并复审通过；IF0 真实 PASS 2317 行+固定样本；代行验收见 `docs/reviews/acceptance-executed.md` |
| FULL-602 | ACCEPTANCE | 204 | 实现完成：市场指标口径/单位/频率/来源/截止时间模型与 ETF 去重（SH>SZ>HK）；同花顺指数真实探测因 akshare 版本无接口如实 FAILED/PROVIDER，见 `docs/deliveries/FULL-602.md`；解除条件=可用 akshare 版本上的真实指标探测 |
| FULL-610 | BLOCKED | 101、用户开通QMT | 外部条件未满足：用户未开通 QMT；解除条件=用户开通后进入 READY 并实测，不阻塞离线正式版 |
| FULL-700 | ACCEPTED | 003 | 独立验收（accept_android2）重跑图谱模型/契约专项与 JVM 全量通过，见 `docs/reviews/acceptance-android-dsl-graph.md` 与交付文档“独立验收”章节 |
| FULL-701 | ACCEPTED | 700 | 独立验收（accept_android2）重跑 HTML/Excel/PDF/公告导入器 9 项与 JVM 全量通过，见 `docs/reviews/acceptance-android-dsl-graph.md` 与交付文档“独立验收”章节 |
| FULL-702 | ACCEPTED | 701 | 独立验收（accept_android2）重跑金标评估：实体与关系 P/R/F1=1.0（10/10 与 8/8），超阈值，见 `docs/reviews/acceptance-android-dsl-graph.md` 与交付文档“独立验收”章节 |
| FULL-703 | ACCEPTED | 702、500 | 独立验收（accept_android2）重跑审核/修订/审计链 8 项与 JVM 全量通过，见 `docs/reviews/acceptance-android-dsl-graph.md` 与交付文档“独立验收”章节 |
| FULL-704 | ACCEPTANCE | 703、300 | 独立验收本机部分通过；解除条件=真机从关系逐级追溯原始来源位置与确认状态；已知 P2（快照无大小限制/解析失败提示）与 P3（重复 entity_id、非懒加载），见 `docs/reviews/acceptance-android-dsl-graph.md` |
| FULL-705 | ACCEPTANCE | 701–704 | 720+ 篇研报知识库流水线与电脑端产业链图谱完成：`reports/industry/` 721 篇 JSON（全部 REVIEWED/33,193 事实/721 核验通过，含 1 篇 OCR 补偿、1 篇源缺失保留），177 条原始子链/33,193 条链上事实（per-fact 链聚合），Atlas v2 展示口径 75 条链 / 7,090 家带代码公司 / 公司索引 7,577 / F10 CN 5,539 + HK 2,806 + legacy 1,017（`industry-atlas.html` 约 20 MB，`/industry-v2/` 上线并同步 `data_control`）；`industry-map.html` 旧版继续在 `/industry/`；解除条件=真机导入并显示图谱快照与新版全景页；产业链环节/产品定义待用户人工校验研报后确认，见 `docs/deliveries/FULL-705.md` |
| FULL-800 | ACCEPTANCE | 202、204 | 实现完成：每晚任务状态机（锁/重试/崩溃恢复）、CLI 白名单入口、run-nightly/install-nightly-task 脚本，本机已创建 `MarketMonitorNightly`（每日 18:30 Ready）；见 `docs/deliveries/FULL-800.md`；解除条件=连续夜间运行与受控中断/恢复演练 |
| FULL-801 | ACCEPTED | 800 | 健康看板实现并复审通过；失败/陈旧/隔离区可见；代行验收见 `docs/reviews/acceptance-executed.md` |
| FULL-802 | ACCEPTED | 801 | 凭据扫描/密钥轮换/备份演练/依赖审计实现并复审通过；代行验收见 `docs/reviews/acceptance-executed.md` |
| FULL-803 | ACCEPTANCE | 所有非门控必选任务 | 2026-08-06 本机全量回归 PASS（桌面 pytest 434 项、Ruff、Android lint/JVM/APK），Release APK 构建且 16KB 对齐检查通过（未签名，SHA256 `BA5E9163…`），清单见 `docs/release-checklist.md` 与 `docs/deliveries/FULL-803.md`；解除条件=真实数据源/Android 13+ 16KB 真机/keystore 签名/连续运行/804 用户批准 |
| FULL-804 | BLOCKED | 本机连续20次成功 | 外部条件未满足：本机连续 20 次成功未达成且付费资源需用户单独书面批准；解除条件=证据达成+用户批准 |
| FULL-900 | IN_PROGRESS | 803 | 封板准备完成：版本一致（0.1.0/versionCode 1）、Release APK 未签名（16KB PASS）、报告包 `docs/release/` 四份齐备，见 `docs/deliveries/FULL-900.md`；封板需 803 外部解除条件、keystore 签名、工作提交后打标签与最终哈希核对 |

## 已知外部条件

- JQData、Tushare 凭据由用户在本机配置，不写入仓库。
- QMT 尚未作为已开通条件，`FULL-610` 不得进入主链。
- Android 发布验收需要 Android 13+、16 KB 页面设备或等价模拟环境。
- Day 0 的 Provider 失败、Chaquopy 失败和真机缺口仍是历史事实；本计划没有把它们改写为成功。

## 状态维护规则

状态只能按 `Plan_full.md` 的状态机更新。任何 Agent 开始前先重读本文件：实现角色不得从无 `READY`/`CHANGES_REQUIRED` 状态自动选择 `PENDING`，审查角色只领取 `REVIEW`，验收角色只领取 `ACCEPTANCE`。审查/验收完成后必须写证据链接，再更新状态和下一项入口。

## 2026-08-09 F10 全市场抓取与链索引优化

- F10 全市场抓取与首轮合并完成：CN 609 条、HK 659 条 F10 记录已并入新版 `industry-atlas.json`（原始 details 文件仍在限速续抓，1.0s/条、断点续抓）。
- 文件锁机制已添加到 `f10.py`（`_acquire_market_lock`/`_release_market_lock`）和 `f10_batch.py`，防止同一市场被多个进程同时抓取。
- `cli.py` 增加 `SKIPPED` 状态处理，锁被占用时正常退出而非崩溃。
- `report_pipeline.py` `_aggregate_chains` 增加 Pass 2 逻辑：事实自身的 `chain` 字段即使不在文档声明的 `primary_chain`/`related_chains` 中，也会被归入对应产业链。已生效：链数 154→177，链上事实 22,149→33,193（回收此前被丢弃的 11,044 条事实），并重建 `chain_index.json`、`industry-map.html` 与新版 `industry-atlas.json/html`。
- 521 项桌面 pytest 全部通过（含新增 per-fact 链聚合回归用例）。
- Android 同步包待父任务按新版 atlas 快照重建（本子任务不打包）。

## 2026-08-09 收尾：市场板块脏词过滤与 Atlas v2 重建（用户指示暂停产业链提炼）

- 用户反馈“创业板被当作通信产业链产品”等产业链/环节/产品定义不理想，决定之后自行阅读研报 PDF 人工校验；自动化产业链提炼暂停，不再派新子任务；F10/revenue 抓取随后由计划任务限速续抓完成（见下）。
- 根因：`chain_index.json` 自身存在脏数据（16 条 products、32 条 cooccurrences、28 条 facts 把“创业板”抽成 PRODUCT）。
- 修复（`desktop/src/market_monitor/industry_atlas.py`）：新增 `_MARKET_BOARD_TERMS` 市场板块词表与 `_is_market_board_name()` 过滤（精确+子串，“主板”仅精确过滤避免误伤电子行业 motherboard）；`_f10_chain_candidates()` 改为多段 key 优先，命中即不回退短段；`_norm_industry_segment()` 行业分段归一化，避免剥掉“生物科技/科技”导致行业映射失败。
- 验证：`desktop\tests\test_industry_atlas.py` 9/9 通过；全量 `pytest desktop/tests` 0 失败；atlas 重建后所有卡片对“创业板/科创板/沪深/中证/主板/北交所/…/上市/指数”命中 0；HTML 完全离线（11 处 `https://` 均为公司简介正文官网文字，非资源引用）。
- 重建产物：`reports/industry/industry-atlas.json/.html` 75 条链 / 7,090 家带代码公司 / 公司索引 7,577 / F10 CN 5,539 + HK 2,806 + legacy 1,017；`data_control/industry/industry-atlas.html` 已同步；后端 `/industry-v2/` 每次请求读磁盘，刷新即可见新版。
- 未完成项（如实记录）：177 条原始子链去重/归并未完成；产业链定义与环节/产品归属需用户人工校验；Android 真机导入验收未解除。A 股收入构成（revenue）已补齐（见下），港股收入构成无可用数据源（东财无港股主营构成报表，已实测）。
- Android 同步包已按新版 atlas 重建：`market-20260809-081649-141aff2e`（13,585,044 字节，ed25519+ecdsa 签名），后端 `/api/android-package` 实测 200；真机导入验收仍待解除。
- 进程清理：旧子 Agent 已收工；revenue 续抓由计划任务 `MarketListener_Revenue_CN`（1.0s/条限速）完成并于 16:03 正常退出（`revenue_cn.log` 记录 PASS）；后端 8765（PID 30108/35652）保留在线。

## 2026-08-09 续抓完成：A 股收入构成补齐与 Atlas/同步包重建

- F10 收入构成（revenue）续抓完成：`data_control/f10/cn/revenue_20260809.jsonl` 4,730 条 + `revenue_20260809.corrupt-1352.bak.jsonl` 492 条 + `revenue_20260809.corrupt-1401.bak.jsonl` 317 条 = 5,539 个唯一代码（零重叠、零坏行、全部含 `revenue_breakdown`；两个 bak 为旧中断现场备份，保留不删）。
- F10 底表重新导出：`data_control/industry/f10/cn_f10.jsonl` 5,539 条（5,538 条含非空收入构成）、`hk_f10.jsonl` 2,806 条；港股收入构成无可用数据源（东财无港股主营构成报表，已实测三种传参均失败）。
- Atlas 重建：75 条链 / 7,090 家带代码公司 / 公司索引 7,577 / F10 CN 5,539 + HK 2,806 + legacy 1,017；`industry-atlas.html` 20,018,677 字节（约 20 MB），`revenue_breakdown` 已内嵌（HTML 内 4,932 处），零 CDN（11 处 `https://` 均为公司简介正文官网文字）；`data_control/industry/industry-atlas.html` 与 `reports/industry/industry-atlas.html` SHA256 一致（CFD56983…）；后端 `/industry-v2/` 实测 200。
- Android 同步包重建：`market-20260809-081649-141aff2e`（13,585,044 字节，ed25519+ecdsa 签名）；zip 内 `industry/industry-atlas.html` 与本地哈希一致；后端 `/api/android-package` 实测 200 且下载包哈希一致。
- 回归：桌面 `pytest desktop/tests` 525 项通过 / 0 失败；`ruff check desktop/src desktop/tests` 通过；Android `testDebugUnitTest --rerun-tasks` BUILD SUCCESSFUL（21 suites / 74 tests / 0 failures），`assembleDebug` 成功。
- 文档更新：本文件、`Log.md`、`Plan_full.md`、`INDUSTRY_GRAPH_*.md`、`release/known-gaps.md`、`deliveries/FULL-705.md`、`README.md`；工作区保持未提交（用户明确要求不 commit）。
