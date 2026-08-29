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
