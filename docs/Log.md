# Log

本文件记录实际发生的变更与验证，不记录凭据、私钥或个人数据。

## 2026-08-03 - Day 0 实现与自动化验证

- 建立桌面数据生产端、Android 消费端、共享契约、Provider 探针、标准标的、Bronze/Silver、质量规则、交易时段聚合、不可变行情包和 Ed25519 签名校验。
- 建立 Android 个人/行情数据边界、签名行情包原子导入、离线 WebView K 线容器和本地 Lightweight Charts 资产。
- 桌面测试实际执行：`desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q`，结果 `48 passed`。
- Android 自动化验证实际执行：`android\gradlew.bat -p android testDebugUnitTest --no-daemon`，通过。
- 真实 Provider 结果：聚宽因缺少凭据失败；Baostock 与 AkShare 受网络/代理阻塞失败；`tdx_quant` 无可安装的已验证发行包，标记 `UNSUPPORTED`。
- Chaquopy 尝试打包 NumPy 失败；依据 Plan 的停止规则，不切换到联网运行时，D0-050 记录为阻塞。

## 2026-08-04 - Android 构建、16 KB 与中文状态修复

- 确认 Gradle Wrapper 为 8.5，使用 JDK 20.0.2 正常运行；记录 Android Studio 应使用英文 junction 路径。
- 将已弃用 SQLCipher 工件迁移为 `net.zetetic:sqlcipher-android:4.6.1`，将 Room 工厂迁为 `SupportOpenHelperFactory`，并显式加载 `sqlcipher` 原生库。
- 实际执行 `android\gradlew.bat -p android testDebugUnitTest assembleDebug --no-daemon`，结果 `BUILD SUCCESSFUL`。
- 实际执行 APK 16 KB ZIP 对齐检查；四种 ABI 的 `libsqlcipher.so` 所有 `PT_LOAD` 对齐均为 `0x4000`。
- 将 Android 可见界面改为中文；导入 Worker 返回真实成功包 ID/截止时间或失败分类；界面不再把“已排队”显示为导入成功。
- 新增交付文档：`docs/deliveries/Android-16KB-compatibility.md`、`docs/deliveries/Android-中文界面与导入状态.md`、`docs/deliveries/Day0阶段性交接.md`。

## 当前状态

- Day 0 已停止执行且未封板，其历史证据见 `docs/deliveries/Day0阶段性交接.md`。本条是 2026-08-04 的历史状态记录；正式计划固化后的实时入口以根目录 `START_HERE.md` 和 `STATUS.md` 为准。

## 2026-08-04 - Provider 隔离、离线 K 线读取与 16 KB Python 复验

- `ProbeRunner` 新增 Provider 级受控超时，CLI 支持 `probe --timeout-seconds`；桌面测试实际执行为 `49 passed`。20 秒全量真实探针生成报告：聚宽缺凭据、Baostock 超时、AkShare 代理连接关闭、tdx_quant 不受支持，没有任何来源 PASS。
- Android 新增激活 `payload.sqlite` 的只读标的/周期/bars 查询、来源/质量展示和真实 candle JSON 到离线 Lightweight Charts 的转换；新增 JVM JSON 解码回归测试。
- 试验 Chaquopy 17.0.0、Python 3.13、NumPy 1.26.2：APK 离线打包成功，但 `connectedDebugAndroidTest` 在 16 KB 模拟器因 `libgfortran.so.3` 的 4096 对齐失败。已按 Plan 停止规则移除试验代码，未引入联网执行。
- 回退后的 Android JVM 测试与 Debug 构建再次通过；`zipalign -c -P 16` 通过，四种 ABI 的 SQLCipher `PT_LOAD` 仍为 `0x4000`。APK 已安装并启动于 16 KB 模拟器，中文界面真实显示未导入状态；修复了状态栏与标题重叠。
- Git 状态检查显示当前仓库没有已跟踪基线，全部项目文件为未跟踪；未擅自执行 `git add`、初始化或提交。

## 2026-08-04 - Day 0 停止与正式开发转场

- 用户决定不再执行 `Plan.md` 的 Day 0 任务。未完成项、失败报告和验收缺口保留原状，不把停止执行写成 Day 0 完成。
- 新增 `docs/正式开发交接.md` 与 `docs/deliveries/README.md`，更新根入口、模块说明、Plan、经验记录和时序图，使后续对话从整个项目目标而非 D0 任务编号开始。
- ADR、领域词汇、共享 Schema、交付模板和历史 D0 单项交付未改写；它们分别保持规范、契约、模板和历史证据性质。

## 2026-08-05 - FULL-002 Git 与工具链基线

- 创建首个 Git 根提交 `b270463bc9fe63932faf4e01858d8d5d870697d9`，保存已验收的 FULL-001 与 Day 0 历史基线；提交前确认无大文件、真实凭据或私钥。
- 锁定 Python 3.11.0及49个运行/测试依赖，固定 setuptools 构建后端；依赖干跑解析、`pip check` 与49项 pytest 通过。
- 锁定 JDK 21、Gradle 8.5及分发哈希、AGP/Kotlin、Android SDK 34 revision 3、Build Tools 34.0.0和151个 Android 传递模块。
- JDK 20 被项目配置明确拒绝；JBR 21.0.11 下6项 Android JVM 测试与 Debug APK 构建通过。中文物理路径仍需临时 `subst` 到英文盘符，单独使用 junction 在 JDK 21 下不足以避免测试类加载失败。

## 2026-08-08/09 - Android 同步修复、真实覆盖展示与 720 篇研报流水线

- 修复 Android 同步包下载与手动导入两处报错：`MainActivity.kt` 默认服务器地址改为电脑当前 IPv4 `http://192.168.1.88:8765`；同步/导入成功后刷新产业链 HTML；导入失败返回 `RESULT_ERROR_DETAIL` 明细；`MarketPackageImporter.kt` 白名单与抽取条目加入 `industry/industry-map.html`，手动导入 zip 不再报“行情包结构无效”。
- 桌面 `/api/health` 新增真实 K 线覆盖统计（扫描 `data_control/silver/**/*.parquet`）：48 标的、72,321 根 K 线（CN ETF 4/1200、CN FUTURE 15/9435、CN INDEX 5/2170、CN STOCK 5/1515、GLOBAL CRYPTO 2/120、GLOBAL FUTURE 4/15461、GLOBAL INDEX 6/20060、HK INDEX 3/7847、HK STOCK 4/14513）；属部分覆盖，接口与文档如实标注，不宣称全 A 股/全港股/全期货。
- 720 篇研报知识库流水线：`reports/industry/` 生成 720 个 `report_*.json`（717 解析 / 3 跳过 / 0 失败，33,096 条事实，版本 4）；`reports verify` 719 通过 / 1 待复核（`20260712-银河证券-光器件行业深度报告：磷化铟…pdf`，未抽取到事实且警告过多，疑似扫描件，建议 OCR）；`reports chains` 聚合 155 条产业链（22,083 条链上事实）并生成 `industry-map.html`（SVG 图谱，9.6 MB），快照同步 `data_control/industry/`。
- 重建并激活同步包 `market-20260808-190946-deaecd38`（7,256,011 字节，含 `signature.ed25519`/`signature.ecdsa`/`payload.sqlite`/`industry/industry-map.html`，72,321 bars + 25,545 gold_metrics）。
- 后端 8765 重启到最新代码并实测：`/`、`/api/health`、`/industry/`、`/industry/industry-map.html`、`/api/android-package` 全部 200。
- 终核验：`/industry/industry-map.html` 服务字节 9,628,645 = 本地新文件 9,647,124 − 18,479 处 CRLF（`read_text` 统一换行符所致），内容逐字节一致；同步包 zip 内图谱与本地原始文件 SHA256 均为 `785EF2FF0AC4C7709B915ED5A38EF0C1234A521B40CE927FCAB82786D1CAA5D1`；`/`、`/api/health`（48 标的 / 72,321 行）、`/industry/industry-map.html`、`/api/android-package` 实测 200。
- 回归：桌面 `pytest desktop\tests -q` 507 项全部通过（新增 `/api/health` 真实 parquet 覆盖统计测试、研报聚合/规则核验/SVG 图谱生成测试 4 项）；Android `gradlew.bat testDebugUnitTest assembleDebug`（JDK 21）BUILD SUCCESSFUL，21 suites / 74 tests / 0 failures / 0 errors。
- 文档更新：`STATUS.md`、`Plan_full.md`（5.8 补充）、`README.md`、`docs/deliveries/FULL-705.md` 与交付索引；工作区保持未提交（用户明确要求不 commit）。

## 2026-08-09 - 研报补齐与 OCR 重试

- 新增 `desktop/src/market_monitor/report_ocr.py`（PyMuPDF 渲染 + RapidOCR，扫描件自动补偿，延迟初始化/线程锁）；`report_pipeline.py` 增加 `force`/`ocr_fallback` 参数，文本过短时自动 OCR。
- 新增 `scripts/retry_report_ocr.py`（幂等）：处理无 JSON 的 PDF、force 重跑 0 事实报告、标记源缺失，默认重建 chain_index 与 industry-map.html。
- 补齐结果：财信证券 AI短剧 → 37 事实；银河证券光器件扫描件（原 0 事实）→ OCR 60 事实（`ocr_applied=true`）；中信期货量化 CTA（源 PDF 缺失）→ 保留 42 事实并标记 `source_missing`。
- 重建后：`reports/industry/` 现有 721 个 `report_*.json` 全部 REVIEWED、33,193 条事实；`reports verify` 721/721 通过；`chain_index.json` 154 条链 / 721 报告 / 22,149 条链上事实；`industry-map.html` 重新生成（约 9.6 MB）。
- 回归：桌面 `pytest desktop\tests -q` 571 项全部通过（新增 OCR 回退/force/禁用 3 项测试）；重试脚本二次运行零改动（幂等）。
- 定位问题：全量 33,193 条事实中约 11,044 条未进入链上统计——抽取阶段 per-fact 链判定与报告级 top-5 链判定不一致；新版 `industry_atlas` 将直接按 `fact.chain` 分组（F10 完成后重跑 `reports atlas` 合并）。
- 文档更新：`STATUS.md`、`deliveries/FULL-705.md`、`INDUSTRY_GRAPH_ARCHITECTURE.md`、`INDUSTRY_GRAPH_CURRENT_ANALYSIS.md`、`Plan_full.md`；工作区保持未提交（用户明确要求不 commit）。

## 2026-08-09 - 链索引 per-fact 聚合生效与产业链全景图重建

- `report_pipeline.py` `_aggregate_chains` 增加 Pass 2：事实自身 `chain` 不在文档 `primary_chain`/`related_chains` 时仍归入对应产业链（父任务实现，本子任务核验并补测试）。
- 重建结果：`chain_index.json` 154→177 条链、链上事实 22,149→33,193（回收 11,044 条此前被丢弃的事实）、721 篇报告不变；`industry-map.html` 同步重建（约 9.6 MB）。
- 新版全景重建：`market_monitor reports atlas --output-root reports/industry --data-root data_control` SUCCESS——`industry-atlas.json/html` 177 链 / 407 家带证券代码公司 / 公司索引 1,545 条 / F10 CN 609 + HK 659 + legacy 1017，HTML 约 4.66 MB 自包含离线（零 CDN），同步 `data_control/industry/industry-atlas.html`（逐字节一致）。
- 回归：`test_report_pipeline.py` 新增 per-fact 链聚合用例（报告声明链外的事实正确归入新链并计入 report_count/fact_count）；全套桌面 `pytest desktop/tests -q` 521 项通过 / 0 失败（junit XML 记录）。
- 文档更新：`STATUS.md`、`deliveries/FULL-705.md`、`INDUSTRY_GRAPH_ARCHITECTURE.md`、`INDUSTRY_GRAPH_CURRENT_ANALYSIS.md`（v1.2）、`Plan_full.md`；工作区保持未提交（用户明确要求不 commit）。

## 2026-08-09 - F10 抓取与 Atlas v2 重建

- F10 抓取完成：CN `data_control/f10/cn/details_20260809.jsonl` 5539 条（universe 5539、state done 5539 / failed 0）；HK `data_control/f10/hk/details_20260809.jsonl` 2806 条（state done 2784 / failed 0）。
- 收入数据未补齐：CN `revenue_20260809.jsonl` 607 条 + `corrupt-1352.bak` 492 条 + `corrupt-1401.bak` 317 条，去重后约 900+ 唯一覆盖，仍不完整。
- atlas F10 覆盖：CN 5539 + HK 2806 + legacy 1017，含 total_market_cap / industry / profile / main_business。
- `industry_atlas.py` v2 新增 F10 匹配（step 3.5）：
  - `_build_f10_text_index()`：F10 主营/产品/经营范围/简介/亮点/地位等字段建全文索引；
  - `_build_f10_by_industry()`：F10 行业字段反向定位；
  - step 3.5a：按产品名/关键词匹配；
  - step 3.5b：用 F10 行业/主营/产品匹配；
  - 新增 `_GENERIC_CARD_NAMES` 过滤通用卡片名、`_MARKET_BOARD_TERMS` 过滤创业板/科创板/北交所等市场板块词，修复“创业板当通信产品”误判；
  - MAX_CHAIN_COMPANIES 从 120 提高到 200，提升 F10 覆盖。
- 重建 Atlas v2：修复 `_is_market_board_name` 与多段 key 优先匹配后，不再把市场板块名当产品；atlas 现有 75 条链（展示口径）/ 7,095 家带代码公司 / 公司索引 7,582 条 / F10 CN 5539 + HK 2806 + legacy 1017；HTML 17.1 MB 完全离线（零 CDN），同步 `data_control/industry/industry-atlas.html`。
- 说明：atlas v2 当前 75 条链为展示口径，`chain_index.json` 仍为 177 条原始子链；177 条去重/归并及产业链定义待用户人工确认。
- 新增 `test_industry_atlas.py`（6 项：stages/cards/sub_chains/市场板块过滤/离线 HTML）；`pytest desktop/tests` 522 项通过 / 0 失败；不再把创业板/科创板/北交所等市场板块词当产品（0 误判）。
- Android 同步包：先重建 `market-20260809-054754-bc5426cf`（72,321 bars + 25,545 gold_metrics + industry-atlas 约 9.35MB，ed25519+ecdsa 签名）；因 17.1MB atlas 过大，重建 `market-20260809-063402-e8546900`（72,321 bars + 25,545 gold_metrics，12,748,434 字节，ed25519+ecdsa 签名），`/api/android-package` 实测 200。
- 后端实测：`/` 200、`/industry-v2/` 200、`/industry/` 200，`_send_industry_atlas` 供 Android 同步。
- 文档更新：`STATUS.md`、`Log.md` 等；工作区保持未提交（用户明确要求不 commit）。


## 2026-08-09 - 续抓完成：A 股收入构成补齐与 Atlas/同步包重建

- F10 收入构成（revenue）续抓完成：`data_control/f10/cn/revenue_20260809.jsonl` 4,730 条 + `revenue_20260809.corrupt-1352.bak.jsonl` 492 条 + `revenue_20260809.corrupt-1401.bak.jsonl` 317 条 = 5,539 个唯一代码（零重叠、零坏行、全部含 `revenue_breakdown`；两个 bak 为旧中断现场备份，保留不删）。
- F10 底表重新导出：`data_control/industry/f10/cn_f10.jsonl` 5,539 条（5,538 条含非空收入构成）、`hk_f10.jsonl` 2,806 条；港股收入构成无可用数据源（东财无港股主营构成报表，已实测三种传参均失败）。
- Atlas 重建：75 条链 / 7,090 家带代码公司 / 公司索引 7,577 / F10 CN 5,539 + HK 2,806 + legacy 1,017；`industry-atlas.html` 20,018,677 字节（约 20 MB），`revenue_breakdown` 已内嵌（HTML 内 4,932 处），零 CDN（11 处 `https://` 均为公司简介正文官网文字）；`data_control/industry/industry-atlas.html` 与 `reports/industry/industry-atlas.html` SHA256 一致（CFD56983…）；后端 `/industry-v2/` 实测 200。
- Android 同步包重建：`market-20260809-081649-141aff2e`（13,585,044 字节，ed25519+ecdsa 签名）；zip 内 `industry/industry-atlas.html` 与本地哈希一致；后端 `/api/android-package` 实测 200 且下载包哈希一致。
- 回归：桌面 `pytest desktop/tests` 525 项通过 / 0 失败；`ruff check desktop/src desktop/tests` 通过；Android `testDebugUnitTest --rerun-tasks` BUILD SUCCESSFUL（21 suites / 74 tests / 0 failures），`assembleDebug` 成功。
- 文档更新：`STATUS.md`、`Log.md`、`Plan_full.md`、`INDUSTRY_GRAPH_*.md`、`release/known-gaps.md`、`deliveries/FULL-705.md`、`README.md`；工作区保持未提交（用户明确要求不 commit）。

## 2026-08-13 至 2026-08-24 - R3 未提交工作区实现汇总

- 行情数据层新增集中式市场分类、证券/期货名称配置、行情数据版本、文件清单式查询缓存、游标历史窗口、卡片批量尾部 K 线和缓存增量更新；扩展行情、数据源、控制中心和目录接口。
- 新增通达信本地证券导入：沪深北/港股 `.day/.lc5`、名称读取、增量检查点、日期范围、来源隔离和 ETF/LOF/REIT/转债/回购/板块指数分类。
- 新增国内外期货批量链路：通达信期货通次连/主连/原生加权、月份合约、商品指数、AKShare 主连补缺、国际连续合约，以及合约乘数、保证金、沉淀资金和持仓量加权计算。
- 新增账户分析、FIFO 成本、CSV、回收站、策略台账、多空绩效、公式白名单引擎和公式策略运行；更新统计页和策略页。
- 网页行情页形成列表/卡片/双看板/全屏详情结构，加入列宽与面板宽度调整、行标记、迷你 K 线、设置页及本地缓存。
- 画线工具支持水平线、垂直线、箱体和文本；完成卡片显示画线、24 色预置、透明度拇指、箱体说明与端点布局、稳定整体拖动、上下文悬浮工具栏，以及按图形类型持久保存样式/吸附/跨周期/连续画线。
- ETF 名称初次加载改为等待当前数据版本，避免把临时“ETF+代码”写入持久缓存；市场类型补充交易所代码区间与通达信 `880/881` 板块指数。
- 新增离线 HTML/静态构建脚本和 TickDB 可恢复下载脚本；数据源页增加本地表/数据集/字段浏览与字段口径；设置配置 JSON 随 Python 包发布并忽略 `exports/`；`README.md` 与数据源能力矩阵已补充期货和离线快照说明。
- 2026-08-24 定向执行公式、期货、市场分类、查询缓存、通达信、TickDB、行情、统计和策略共 106 项 Python 测试，全部通过；执行 `npm run build` 通过，保留两个约 1 MB 的 Vite chunk 警告。
- 当前仍未提交，完整 Ruff/pytest/Playwright/Android 回归尚未在最终工作区执行；临时 `_patch_*.py`、未引用的 `MarketView-v2.vue` 和 `MarketInstrumentPanel.vue` 已清理。两个导出脚本分别承担“直接读本地数据的单文件”和“复用 Vue 资源的受限静态网站”职责，均保留。

## 2026-08-24 - TickDB/通达信 K 线只读审计

- TickDB 原始目录有 9,702 个 gzip 文件、约 50.7 万条唯一 K 线；存在 latest/incremental 重复、4 个空文件、159073 的 4 根零价五分钟线、两个失败 K 线任务和不完整 ETF 前缀范围。Silver 中尚无 TickDB 来源记录。
- 通达信支持 12,207 个日线和 11,930 个五分钟文件。1,490 只同日重叠 ETF 验证表明当前日线价格统一 `/100` 后全部比 TickDB 放大 10 倍；转债和质押式回购抽样放大 100 倍。
- TickDB ETF 成交量通常为手；通达信大部分文件为份，但高成交量标的可能也存手。正式入库前必须统一单位并修复已有错误分区。
- 完整审计和接入门槛写入 `docs/TICKDB_TDX_DATA_AUDIT_2026-08-24.md`；本次没有执行数据导入或数据库修改。

## 2026-08-24 - R3 全量回归、文档、安全审计与 GitHub 发布

- `scripts/verify.ps1` 完整通过：Python 3.11.0、JDK 21.0.11、依赖锁/pip check、Ruff、共享 Schema、738 项桌面 pytest、Android `lintDebug/testDebugUnitTest/assembleDebug` 全部成功。
- `npm run build` 通过；完整 Playwright 19/19 通过，覆盖行情画线偏好、ETF 首屏名称、产业链、策略、账户、数据源和全部终端路由。
- 新增脚本 `build_offline_html.py`、`build_static_site.py`、`tickdb_download.py` 的 `--help` 均可启动；Windows 当前控制台对脚本中文帮助显示乱码属于终端编码表现，源码为 UTF-8。
- 清理 `_patch_*.py`、未引用的 `MarketView-v2.vue` 和 `MarketInstrumentPanel.vue`；两个导出脚本职责确认不同并保留。
- 新增 `docs/ARCHITECTURE.md`，统一 README、AGENTS、ADR、API 契约、组件 README、R3、STATUS、Experience、数据源矩阵和历史入口。
- 安全审计：精确暂存区共 86 个变更文件，新增密钥模式、禁传路径/扩展名和超过 5 MiB 文件均为 0；全仓索引命中仅为既有网页夹具、合成测试凭据或历史示例；本地数据库、Parquet、TDX 文件、报告、日志、导出、`.env` 和 `local.properties` 均被 `.gitignore` 排除。
- 指定 GitHub 仓库 `qingdaofuRyo/MarketListener` 的远端 `master` 在发布前为 `1bc76c2`，与本地基线一致；`origin` 已切换到该仓库，两个指向旧仓库的重复远端已移除。
- R3 主体以提交 `ce91a83328e28fd8398704b5cf76f624ee5abd62` 推送到 `origin/master`；首次推送后通过 `git ls-remote` 核对远端 SHA 与本地 HEAD 完全一致。本文档记录提交完成后再执行最终远端一致性核验。

## 2026-08-26 - 开启 R4 与国内期货数据页第一条计划

- 新建 `Plan_R4.md` 并切换为唯一活动计划；R1–R3 与 `STATUS.md` 转为历史证据，R3 未完成的数据契约、通达信证券和 TickDB 问题由 R4 显式承接。
- 用户确认“持仓品牌”指“持仓品种”；多空热度覆盖所有有效月份合约，最终输出单一近 5 日合成热度分。
- 领域模型区分“全月份合约范围/合约广度”和“持仓品种沉淀资金”，避免同品种多月份合约被误称为多个品种。
- R4-T001 登记市场热度、全市场/品种沉淀资金、席位结构资金和商品持仓席位龙虎榜，并记录现有固定 `RB2610/2026-08-07` 样例不能作为正式全市场数据。
- 数量与资金占比、五日精确权重、热度标签阈值、最低资金覆盖率及席位人工分类仍处于 `ANALYSIS`；本次只更新计划与领域文档，没有修改业务代码、数据库或本地数据。

## 2026-08-26 - R4 第二条：画线工具 80 色预设调色板

- 根据用户附图登记 `R4-T002`，统一名称为“画线颜色选择器”，其中包含 80 色预设调色板、色块网格、自定义颜色选择器和 Alpha 不透明度控件。
- 从附图读取并在 `Plan_R4.md` 中锁定 `10 列 × 8 行` 的 80 个有序 HEX 色值，防止实现阶段因临时图片失效或人工近似而改变色表。
- 明确点击预设色只替换 RGB 并保留当前 Alpha；自定义颜色不改变固定色表；线条/文字色和箱体填充色分别支持 0%–100% 不透明度。
- 明确 80 色升级必须兼容当前按水平线、垂直线、箱体线和文本框分别保存的浏览器偏好，删除、重建、刷新或重启浏览器都不能丢失最近样式。
- 本次只更新 R4 计划、历史状态摘要和日志，没有修改网页业务代码、测试、数据库或本地数据。

## 2026-08-26 - R4 国内期货七模块细化与笔刷画线计划

- 按用户新清单将 `R4-T001` 页面收敛为七个明确模块：多空热度、品种/席位市值分布、品种/席位持仓分布、商品合约和商品合约席位分布。
- 新增市值、品种/席位分布、合约四指标同图、基差、席位四方向持仓所需的数据集、API 草案、真实探针和性能验收；不将“市值”静默解释为“沉淀资金”。
- 分布图按用户要求锁定为默认同图显示全部有效系列而不静默 Top-N；另记录六个实现前必须锁定的口径类别：热度粒度/权重与阈值、市值参考价、席位默认持仓、K 线或收盘线、多 Y 轴以及基差方向/现货源。
- 新增 `R4-T003`：K 线主图笔刷使用多点时间/价格折线，收笔时简化并一次保存，最多 2,048 点；复用画线颜色选择器，支持粗细、线型、锁定、跨周期、删除、整体拖动和独立偏好。
- 已审查现有画线模型、绘图交互、后端 JSON 保存和期货计算/导入基线；参考站公开页面在当前网络环境未成功加载，因此未将未观测交互写成事实。
- 本次只修改活动计划与相关状态/经验/日志文档，未修改网页、后端、测试、数据库或本地数据。

## 2026-08-27 - R4 四张商品期货结构图计划

- 按用户粘贴的第四轮计划，将“品种市值分布、席位市值分布、品种持仓分布、席位持仓分布”从 `R4-T001` 页面模块拆为 `R4-T004～R4-T007` 四个可独立追踪任务；外部任务别名 `R4-FUTURES-MARKET-CAP-STACKED-AREA` 归入 `R4-T004`。
- 四图统一为普通数值堆叠面积图，使用最新完整交易日生成服务端固定顺序；基准日占比低于 `1.5%` 的成员进入固定“其他”集合，并支持点击下钻查看全部真实成员。每日增量不得自动重排或重分类。
- 锁定“市值”业务口径为名义持仓规模：`合约价格 × 合约乘数 × 交易所单边持仓量`，不乘保证金率且不额外乘二；商品期货默认排除中金所金融期货。价格采用结算价还是收盘价仍需覆盖审计，因此相关任务保持 `ANALYSIS`。
- 审计确认本地 Silver 已有期货日线价格/结算价/持仓量字段，现有后端仅实现沉淀资金和固定样例席位榜，尚无四图所需的日度 Gold 与固定基准元数据；Web 已有 ECharts 6，可复用但需补固定堆叠、“其他”下钻和悬浮联动。
- 同步更新 `Plan_R4.md`、领域词汇、历史状态摘要、README 与可复用经验；本次没有修改业务代码、测试、数据库或本地数据，也没有宣称参考站数据或交互已经验证。

## 2026-08-27 - R4 中国商品期货多空热度实现与真实回算

- 新增集中配置、品种方向/沉淀资金/10 交易日指数衰减计算、统一交易日同步、逐日合约规则快照、Silver→Gold 离线任务、复合主键存储、数据集登记、只读 `/api/futures/heat` 和三条 CLI 命令。
- 初次规则同步最近 10 个交易日，共保存 740 条品种规则和 1,424 条具体合约保证金 override；统一日历保存 8,797 个交易日。规则和日历均写入被 Git 忽略的 `data_control/state/`，页面请求不联网。
- Gold 真实回算读取 3,690 个 Parquet、329,349 条来源记录，准备 266,969 条标准输入；统一日历排除 `2006-01-26/27` 的 10 条非交易日记录，当前公式以原子替换写入 5,244 个交易日。
- 最新 2026-08-21 共 74 个商品期货品种，方向有效 74、资金有效 73，方向覆盖 100%、资金覆盖 98.65%；Breadth10=33.2265866769、Fund10=42.8198412950、Divergence=-9.5932546180，窗口已满 10 日。剩余 1 个资金缺口保持 `PARTIAL`。
- Vue 新增 `/futures/`、三半圆 Gauge、集中七档状态/绿空红多配色、单滑块、`localStorage` 恢复默认、三线历史图、七档范围、固定 Y 轴/0 线、图例、同日 Tooltip 和响应式布局；5,000 点用例验证滑块不新增 API 请求。
- 最终真实 Silver→Gold 重跑继续读取 3,690 个 Parquet / 329,349 条来源记录，原子写入 5,244 个交易日；方向覆盖 100%、资金覆盖 98.65%，实际 API 返回 5,244 点并明确提示 73/74 的资金覆盖限制。
- 完成规则长历史回补：本地保存 1,285 个精确日期快照、82,141 条品种规则和 91,633 条合约保证金 override；公共上游对 15 个日期稳定返回空表。同步器现可保留同批成功日、列出失败日并在重跑时只请求缺日，不会因单日错误丢弃整批证据。
- Gold 重建后确认：品种热度保留 5,244 日；资金/总热度为 237 日，近 1 年 222/243、近 5 年 237/1,211。根因是历史月份合约 Silver 不完整，不是规则快照不足；API 已增加所选区间历史覆盖提示，缺失日保持 `null`。
- 最终验收通过：当前桌面套件收集并通过 771 项测试；Ruff、共享 Schema、Vue 生产构建、完整 Playwright 25/25 均通过。增加三 Gauge/历史末点逐值一致性及离开页面后重进持久化断言后，多空热度专项再次 6/6 通过。
- `powershell -ExecutionPolicy Bypass -File scripts\verify.ps1` 完整通过，包括锁定依赖、Ruff、共享 Schema、桌面 pytest、Android lint、Android JVM 单测和 Debug APK 构建；仅保留既有篡改签名夹具产生的重复 `manifest.json` 警告及 Vite 大 chunk 提示，不影响验收。

## 2026-08-28 - R4 行情“其它”分类清退与待分类审计表

- 确认用户已删除本地 TickDB 文件目录；项目中仍无 TickDB Silver，因此没有执行额外数据库删除。
- 将未命中市场规则的内部结果由公开 `other` 改为非公开 `unclassified`，从市场分类配置和 Vue 兜底分类中删除“其它”；正常行情接口与策略扫描均排除待分类项。
- 新增只读 `/api/market/unclassified`：合并 Silver 原“其它”记录与通达信金融终端/期货通 `vipdoc/ds` 中未命中文件名规则的记录，展示名称、代码、来源代码、最新收盘价、数据时间、周期、终端和原因。
- 本机快照审计得到 12,244 个 Silver 逻辑标的中的 703 个待分类项，以及两个通达信终端的 8,330 个原始未识别标的，统一表合计 9,033 项。原始扫描不写库、不移动终端文件。
- R3-T008 仍阻止通达信证券正式导入：统一 `/100` 会使已核对 ETF 价格放大 10 倍、转债/质押式回购样本放大 100 倍；32 位成交量字段又会按资产/数量级表现为股/份或手，不能统一乘 100。修复前继续保持质量门禁。
- 定向 Python 回归 49 项通过，Ruff 通过，Vue 生产构建通过，新增待分类表 Playwright 用例 1/1 通过；Vite 仅保留既有大 chunk 警告。

## 2026-08-29 - TDX `tdx-cn-v2` 正式迁移

- 全量扫描 `C:\tongdaxin` 的 24,159 个文件，在独立暂存库完成可回滚重建；提升 7,963 个新版 TDX 分区，活动目录替换 7,478 个旧分区，旧分区保留在 `data_control/tdx_local_migration/backups/20260829-011550-a87eeef2`。
- 正式写入 200,454,925 根通过质量门的 K 线；1,813,037 根无法唯一解释或异常的记录进入隔离区，未进入 Silver。K 线缓存重建完成，目录记录 7,963 个 TDX 分区、357,220,840 行。
- 本次按用户要求只运行针对性 Ruff、TDX/契约/API/分类测试（97 项）和 Vue 生产构建；未运行完整 pytest、Playwright、Android 或 `verify.ps1`。

## 2026-08-29 - R4 画线颜色选择器

- R4-T002 进入 `TESTING`：预设色表改为唯一的 10×8、80 色有序 TypeScript 常量；线条、文字和箱体填充继续共享同一选择器，并保留现有按图形类型保存的颜色/透明度默认值。
- 选择器新增清晰的当前色与自定义色入口，预设点击保留 Alpha；Vue 类型检查与生产构建通过，保留既有 Vite 大 chunk 提示。
- 本机浏览器实际打开行情详情页，验证笔刷工具栏可见、在主图拖出路径后生成悬浮栏；线条颜色弹层实际渲染 80 个色块与 1 个不透明度控件。此为本机可见行为证据，完整 Playwright 回归仍未执行。
- Playwright 定向回归 `rectangle drawing|brush drawing` 2/2 通过；既有箱体用例已更新为 80 色断言，新增笔刷用例验证真实路径拖动、PUT 保存、点数边界、悬浮工具栏与共享调色板。
- 后续定向回归 `data workbench|brush drawing` 2/2 通过：笔刷选色后独立 `brush` 默认值写入本地偏好，锁定与跨周期状态可见；数据页展示且可切换 A 股、港股、其他数据三分区，并露出中国/美国宏观入口。

## 2026-08-29 - R4 期货结构口径与席位来源审计

- 对本机 Silver 的 `2026-08-21` 具体月份合约日线审计确认，1,426 条记录均同时具有结算价、收盘价和持仓量；覆盖完整不构成价格口径选择，因此品种市值结构保持阻塞，等待用户在“结算价”与“收盘价”之间锁定单一 `priceBasis`。
- 源码审计确认席位采集仍固定为 `RB2610 / 2026-08-07` 样例，尚未形成跨交易所、全品种、全月份合约的可追溯排名覆盖；两张席位结构图保持阻塞，未生成会误导为全市场的数据。
- 品种持仓结构不依赖价格或席位来源，继续作为可实现的下一项结构图任务。

## 2026-08-29 - R4 品种持仓结构 Gold、接口与页面

- 新增 `FUTURES_STRUCTURE_DAILY / FUTURES_STRUCTURE_BASELINE`、`futures-structure` 离线命令及只读结构接口。品种持仓结构由 DuckDB 直接聚合 Silver 至交易日×具体月份合约，避免将 12,111 个期货日线 Parquet 的完整 JSON 物化到 Python 内存。
- 全量构建在本机写入 18,080 条成员日度记录，覆盖 2023-09-05 至 2026-08-27；2026-08-27 基准固定为 22 个主图层。范围改为显式白名单 `SHFE / INE / DCE / CZCE / GFEX` 后重建基准，核验 API 与基准中均无 `TDX.*` 伪交易所键。
- 网页端新增固定顺序堆叠面积图、1/3/5 年与全部范围、“其他”下钻、覆盖和未分类新品种提示。Ruff、52 项相关 Python 测试和 Vue 生产构建通过；本机浏览器验证主图、范围切换和下钻可见。未运行完整测试矩阵，任务保持 `VERIFYING`。

## 2026-08-29 - R4 数据页三分区与宏观只读契约

- `/data` 增加 A 股、港股、其他数据三个一级业务分区，现有个人仪表盘、排行、热图与数据浏览器仍作为数据工具保留，没有删除或伪装成第四个市场分区。
- 新增中国/美国宏观目录和单序列接口；只有本地 `gold_metrics` 中的真实观测才标记可用。M2 季节视图按年份输出固定 1～12 月并保留缺月 `null`，其他尚未探针或未采集的 R4 序列返回 `UNAVAILABLE`。
- 新接口使用 camelCase 查询参数并通过定向 API 测试；A/H 股完整总览、状态名单和缺失宏观来源尚未达到真实覆盖门槛，R4-T009 保持 `CODING`。
- A 股分区现直接读取本地 Gold 快照：2026-08-21 沪深京总市值 1,294,193.64 亿元、成交额 18,922.61 亿元、上涨/平盘/下跌 2,505/182/2,862；涨停/跌停权威池为 54/13。网页同时明确四板块拆分、一字板和炸板率尚未验证，未以全市场汇总冒充分类数据。定向 API 测试、Vue 构建、数据页 Playwright 与本机页面刷新验证通过。
- A 股、港股、其他数据三分区的当前选择同步到 `/data/?section=cn|hk|other`，可刷新恢复和复制链接；定向 Playwright 覆盖“其他数据”切换和 URL 直达港股分区。
- 新增 `/api/data/equities/{cn|hk}/overview`。本机 Gold 同时存在 `YYYY-MM-DD` 与 `YYYYMMDD` 交易日，接口先归一再合并，实际返回 2026-08-07、2026-08-21 两个 A 股全市场点；最新点为总市值 1,294,193.64 亿元、成交额 18,922.61 亿元、上涨/下跌 2,505/2,862、涨停/跌停 54/13。港股接口明确 `available=false`，没有复用 A 股指标。数据页将已存在的 A 股市值与成交额历史接入图表；定向 API、Vue 构建和数据页 Playwright 通过。

## 2026-08-29 - R4 交易所席位排名与席位持仓质量门

- 移除 `RB2610 / 2026-08-07` 固定榜样例；新增 `FUTURES_MEMBER_POSITION_DAILY` DuckDB 表与数据集契约，逐条保存交易所公布的合约、交易所、方向、名次、原始/规范席位名、持仓、增减、来源与采集时间。未进入另一方向公开榜单的席位在 API 中保持 `null`，不再按零仓位计算净持仓。
- 真实 `2026-08-28` 定向采集持久化 9,488 条方向排名：CFFEX 880、CZCE 4,856、SHFE/INE 3,272、GFEX 480；DCE 下载端点持续返回 `BadZipFile: File is not a zip file`，任务如实为 `PARTIAL_FAILURE`。商品范围的 8,608 条记录可查询，但 API 显示缺失 DCE，不能宣称五所全覆盖。
- 新增 `futures-member-structure`：对 long、short、gross、net-long、net-short 各自建立固定基准，净方向仅在同交易所/合约/席位的多空排名均公布时计算。实跑写入 391/326/336/194/194 条 Gold 结构记录；由于大商所缺失，五个方向均为 `NO_COMPLETE_COVERAGE`，未建立可展示的正式基准。
- `/futures/` 增加席位持仓方向/范围结构面板及按交易所、品种、月份合约的席位明细表；默认不传输全市场席位大字段，表中明确显示来源与空方向。Ruff、22 项相关 Python 测试、Vue 构建及三条定向 Playwright 用例通过；没有运行全套验证。

## 2026-08-29 - R4 商品合约四指标容器与不可用口径

- 新增只读 `GET /api/futures/contracts` 与 `GET /api/futures/contract-series`。前者只列出本地 Silver 中五个商品交易所的具体月份或原生加权序列；后者以精确交易所、品种和月份合约读取日线 OHLC 与单边持仓量，不扫描第三方页面，也不临时拼接不同来源。
- `/futures/` 新增交易所、品种、月份/加权联动选择器和 ECharts 多轴容器：价格保留 K 线形式，持仓量为橙色线，名义持仓规模和基差保留固定图例/轴位但当前均为 `null`。未锁定“结算价或收盘价”的全轮 `priceBasis` 时，不以收盘价计算名义规模；缺少已审计的现货规格、方向、单位与来源时，不计算基差。
- API 定向测试 2 项、相关席位/结构测试 6 项、Ruff、Vue 构建与新增合约图 Playwright 交互用例均通过。该结果不替代全套验证，也不解除市值与基差的用户口径/来源阻塞。

## 2026-08-30 - R4 笔刷指针手势补全

- 画线颜色选择器的预设色块补充 `aria-pressed` 和焦点环；不透明度轨道叠加透明棋盘与当前 RGB 渐变，0% Alpha 的滑块圆点仍可见。所有图形和笔刷继续共享冻结的 80 色常量与同一选择器。定向浏览器用例已用键盘选择预设色，并依次验证 0%/20%/100% Alpha 的保存与显示。
- `KLineChart` 在有效笔刷起笔后捕获原生指针，离开图表、触摸系统取消或其他 `pointercancel` 时，已有至少两个逻辑点的路径安全收笔；无效单击取消。`Escape`、工具切换和组件卸载均释放捕获，避免遗留触摸手势状态。
- 笔刷拖动期间禁用滚轮缩放冲突，原有 2 CSS 像素采样、收笔简化、自适应 2,048 点压缩和单次保存语义保持不变。Playwright 定向回归覆盖 `pointercancel` 收笔和 10,000 次同步移动：路径不超过 2,048 点、仅发送一次收笔 PUT、派发耗时低于 5 秒（2/2 通过），Vue 生产构建通过；触摸/主题人工验收尚未执行，任务维持 `VERIFYING`。

## 2026-08-30 - R4 数据页状态名单契约与面板覆盖

- A/H 分区现列出本轮登记的全部市场面板，并为高振幅低涨幅、北向、两融、炸板率、风险/监管/停牌名单及港股状态面板展示具体不可用原因；没有将缺失指标压成零值或从 A 股复用到港股。
- 新增 `GET /api/data/equities/{market}/lists?type=&asOfDay=&segment=&page=&pageSize=`。它校验名单类型、日期和分页；在尚无带生效期、公告来源和抓取时间的权威观测时，返回 `available=false`、`items=[]`、`total=null` 和限制说明，避免空表被误读为“当前没有风险标的”。
- 定向 API 测试、Ruff、Vue 构建及数据页 Playwright 均通过；状态名单真实采集与历史生效区间仍未具备，R4-T009 保持 `CODING`。

## 2026-08-30 - R4 品种持仓结构大系列回归

- 新增浏览器合成样本：100 个固定顺序成员 × 500 个交易日（50,000 点）。结构接口完整返回全部成员，ECharts 渲染和滚轮缩放后图表仍可见，整个页面加载低于 15 秒；测试没有以 Top-N 截断或隐藏成员来取得性能结果。
- 该回归补足了 R4-T006 的百品种量级可见性证据，但不替代真实交易所长期覆盖与全套浏览器矩阵，任务继续为 `VERIFYING`。

## 2026-08-30 - R4 宏观 M0 链路与真实来源探针

- `macro_china_money_supply` 已将“流通中的现金 (M0) 同比增长”映射为注册的 `M0_MONEY_SUPPLY`，与 M1/M2 共享月度口径和 Gold 持久化链路；离线桩测试覆盖三者同时写入。
- 对 AkShare 宏观采集执行不落库探针后返回 `PASS`、18,308 条、14 个已登记序列；随后仅持久化该批宏观 Gold 观测。页面 API 复核为中国 8/15、美国 1/4 条登记序列可用，M0 最近观测为 2026-07 的 11.6%。
- 宏观接口不再把本机 `timestamp` 冒充权威发布日期：每个观测返回 `releasedAt=null` 和独立 `fetchedAt`。定向 pytest 8 项、Ruff、Vue 生产构建及数据页 Playwright 通过；未运行全套验证。
- M2 选择器新增“时间序列/季节图”：后者使用 `1～12 月` 横轴、每年一条线，空缺月份以 `null` 保留断点。图表组件的序列点现在允许 `null`，不会将缺月改绘为零；数据页 Playwright 已实际切换并验证季节图标题。
- 宏观目录对每个已落库序列聚合并显示 `latestObservationPeriod` 与 `latestFetchedAt`；两者来自本地 Gold，仍不替代 `releasedAt`。对应 API 测试、Vue 生产构建与数据页 Playwright 通过。
- 本地 `HSGT_FLOW` 审计显示 `CN.HSGT.北向` 未同时保存日净买额、累计净买额和持股市值，完整三字段反而落在 `CN.HSGT.南向`。数据页只有三字段同在一个方向键时才允许显示 `PARTIAL`；当前北向继续 `UNAVAILABLE`，不以错误方向标签或零值字段拼接。

## 2026-08-30 - 通达信金融终端 `ds` 浮点数据分类边界

- 对本机 `C:\tongdaxin\vipdoc\ds` 的实际日线记录，以整数证券布局和浮点布局交叉解码，确认 `12/16/17/18/27/31/48/62/69/102#` 为浮点 OHLC。导入器据此按集中配置的金融终端前缀识别国际指数、COMEX/NYMEX/CBOT、港股指数/个股、中证、华证和国证，而不把它们当作 A 股整数价格。
- 外盘期货的金额字段未通过量额语义验证：正式行只保留 `raw_amount`，业务 `amount=null`，成交量标为 `TDX_FOREIGN_FUTURE_RAW`，序列种类为 `UNVERIFIED_CONTINUOUS`；因此不会进入任何名义市值或“主连”计算。
- 汇率 `10#`、宏观 `38#`、港股基金 `49#`、`98#` 与金融终端下未知 `47#` 继续列入只读待分类表。文件系统仍实际发现 429 个 `49#` 文件，和此前“已删除”描述不一致，未作删除或导入。
- 重跑只读待分类清单后，Silver 未分类 696 项、金融终端原始待确认 1,558 项、期货通原始待确认 3,390 项，总计 5,644 项。期货通中出现的 `12/62/69/102#` 等同名数字前缀没有套用金融终端规则，仍待用户确认终端/市场归属。
- 固定二进制夹具覆盖安全前缀、浮点价格、外盘期货原始量纲和待分类排除；TDX/待分类/市场 API 定向 pytest 50 项及 Ruff 通过。没有执行新的全量导入、来源替换或全套验证。

## 2026-08-30 - 通达信金融终端 `ds` 增量正式导入与库存可见性

- 在完成只读审计后，`import-tdx-local --data-root data_control --tdx-root C:\tongdaxin --batch-rows 250000` 以增量模式写入 4,043 个来源文件、38,663,985 根 `tdx-cn-v2` PASS K 线；跳过未变化文件 24,020 个，隔离 907 个文件/108,456 根记录，拒绝 138 个文件，错误数为 0。该过程没有执行 `--replace-source`，不会替换 2026-08-29 的可恢复备份。
- K 线查询缓存重建完成，修订为 `r-20260830T000027184723Z-c01d9909f5`，索引 432,730,853 个物理 K 线记录，构建耗时约 275.29 秒。
- 发现并修复库存 API 的固定 20,000 标的截断：默认库存现包含全部本地物理标的，避免按代码排序使新导入的 GLOBAL/HK 标的无法被逻辑行情接口解析。真实 API 抽样已返回国际指数、COMEX 外盘期货（`amount=null`、`TDX_FOREIGN_FUTURE_RAW`）、恒指、港股股票及中证指数的 `tdx-cn-v2` 规范化字段。
- 定向 pytest（TDX、待分类、市场 API、库存边界）通过，Ruff 通过；按用户要求未运行完整 pytest、跨端 Playwright、Android 或 `verify.ps1`。

## 2026-08-30 - 港股本地日线总览（按需）

- `/api/data/equities/hk/overview` 不再借用 A 股数值：它仅在用户切换到港股分区时，聚合本机 `HK/HKEX/STOCK/*.TDX_LOCAL` 的通过 `tdx-cn-v2` 质量门日线，按同一标的相邻交易日收盘价计算上涨、持平、下跌，并把成交额换算为亿港元。
- 真实本机第一次计算得到 9,079 个交易日；最新为 2026-08-18，覆盖 2,706 个股票、成交额 1,943.23 亿港元、上涨/持平/下跌 1,059/574/1,073。每点返回 close、成交额和广度覆盖数；港股总/流通市值、涨跌停和状态名单仍没有时间点完备的来源，保持 `null`/`UNAVAILABLE`。
- 为避免默认 A 股页面扫描港股全历史，`/api/data/sections` 只读取已存在的港股聚合缓存；切换港股后完成一次按需计算并刷新卡片。确定性 API 测试、Ruff 和 Vue 生产构建通过，未运行全套验证。

## 2026-08-30 - R4 宏观序列补齐与日期语义

- 仅执行 `MACRO_SERIES` 单任务采集，成功写入 20,857 条本地 Gold 观测且无采集错误；新增中国美元计进口/出口/贸易差额、社零同比/环比、外储和美国季调后非农的标准化登记与采集。非农上游以万人给值，入库统一转换为千人。
- 中国目录现有 14/15 条、美国目录现有 2/4 条本地可用序列。外贸、外储和非农的来源只给出公布日期，API 明示 `timeBasis=SOURCE_DATE`，页面显示“来源日期”，避免把该日期伪称为统计观察期。社会用电量的原始单位、美国进口金额及核心 PCE 年化季率终值未找到同口径字段，保持 `UNAVAILABLE`。

## 2026-08-30 - 大商所会员排名来源复测

- 对 `20260828` 的大商所官方 `memberDealPosi/batchDownload` 入口，以 HTTPS、官方 Referer 及浏览器 User-Agent 进行只读直连探针；请求在网络超时前未返回有效 ZIP。AkShare 同日期调用仍为 `BadZipFile`，两种路径都未形成可解析的官方排名文件。
- 因此维持 DCE `FAILED` 和商品席位结构的 `NO_COMPLETE_COVERAGE`；没有写入、替换或提升任何 DCE 席位数据，也没有采用未审计的第三方镜像作为回退。

## 2026-08-30 - 数据页 Gold 缓存失效修复

- 浏览器实测发现：AkShare 宏观单任务已经写入的 20,857 条 Gold 观测可由 API 正确返回，但数据页会先返回过期 IndexedDB 缓存并在后台刷新，刷新结果未重新绑定界面，导致新增序列仍显示 `UNAVAILABLE`。
- R4 市场、宏观、港股总览和状态名单读取改为强制查询当前本机 API；保留其余页面的常规缓存策略。重载后的真实页面已即时显示 M0、外贸、社零、外储等新增序列，并正确把外贸/外储显示为“来源日期”。Vue 生产构建通过。

## 2026-08-30 - R4 调色板主题定向浏览器验收

- 本机浏览器在 `/data/?section=other` 真实打开主题菜单，确认菜单的三个可达选项为“跟随系统/浅色/深色”。选择深色后 `documentElement` 主题为 `dark`，页面背景为 `rgb(11, 14, 20)`；随后显式切回浅色，主题恢复为 `light`、背景为 `rgb(245, 247, 250)`，未保留测试偏好。
- 这只补足调色板共用主题的实际切换证据；没有以此替代各图形类型的刷新、浏览器重启、触摸与色板视觉人工验收，R4-T002/R4-T003 继续为 `VERIFYING`。

## 2026-08-30 - 金融终端外盘期货后缀与证券分类修订

- 依照本机分类规范中已确认的文件含义，金融终端 `16#/17#/18#` 的 `00W` 映射为 `MAIN`、`00Y` 映射为 `CONTINUOUS`、其余后缀映射为具体月份 `CONTRACT`。外盘成交量仍为 `TDX_FOREIGN_FUTURE_RAW`、金额仍为未知，故不进入名义市值、品种/席位持仓等聚合。
- 同步补齐沪市 `550/580` ETF，以及深市国证指数 `470～487/921～923/970/971/978/980～989` 的配置和导入分类；相关 TDX、市场分类和期货数据定向测试共 30 项通过，Ruff 通过。
- 这是一项待受控重扫后才会写入历史 Silver 的代码/配置修订：没有运行新的来源替换、原地改写、全量迁移或全套验证。既有 `UNVERIFIED_CONTINUOUS` 记录保持可追溯，直到下一次可回滚来源替换完成。

## 2026-08-30 - R4 画线调色板键盘回归修复

- 审计发现原矩形画线偏好 Playwright 仍断言 24 色和已移除的色值，不能证明 R4 80 色调色板。更新为冻结色板中的 `#9C27B0` 和 `#4CAF50`，覆盖线条/填充色、20% Alpha、删除重建和刷新持久化。
- 实测发现按 `Escape` 后首个颜色浮层没有关闭，致使第二个选择器的两个同名透明度滑块同时可见。`DrawingColorPicker` 现捕获该键并关闭自身浮层；定向矩形偏好 Playwright 2/2 和笔刷用例 1/1 通过，Vue 生产构建通过。没有运行完整 Playwright 或跨端验证。
