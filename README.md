# 行情监控和产业链图谱项目文档入口

当前开发唯一入口：[Plan_R3.md](./Plan_R3.md)。`Plan_R1.md`、`Plan_R2.md`、`Plan.md`、`Plan_full.md` 与 `docs/STATUS.md` 保留历史计划和验收事实。

## 开发前必读

1. [Plan_R3.md](./Plan_R3.md)：第三轮当前任务、状态、验证与下一步。
2. [ARCHITECTURE.md](./docs/ARCHITECTURE.md)：桌面生产端、Web、Android、存储、来源和安全边界。
3. [ADR.md](./docs/ADR.md)：不可擅自修改的项目约束与架构决策索引。
4. [CONTEXT.md](./docs/CONTEXT.md)：项目统一术语。
5. [Experience.md](./docs/Experience.md)：可复用的工程经验、环境约束和踩坑记录。
6. [Log.md](./docs/Log.md)：实际变更与验证日志。
7. [数据源能力矩阵](./docs/DATA_SOURCE_CAPABILITY_MATRIX.md) 与 [TickDB/通达信审计](./docs/TICKDB_TDX_DATA_AUDIT_2026-08-24.md)：真实数据能力、限制与接入门槛。
8. [START_HERE.md](./docs/START_HERE.md)：当前接手顺序及历史 FULL 角色模板。
9. [Plan_full.md](./docs/Plan_full.md) 与 [STATUS.md](./docs/STATUS.md)：只读的 `FULL-*` 正式开发历史规范和证据。
10. [行情监控和产业链图谱项目.md](./docs/行情监控和产业链图谱项目.md)：完整目标、架构和技术方案。
11. [正式开发交接](./docs/正式开发交接.md)、[Plan.md](./docs/Plan.md) 与 [Day0阶段性交接](./docs/deliveries/Day0阶段性交接.md)：更早的历史计划与证据。

发生冲突时，以 `ADR.md` 和单项 ADR 为准；时序图用于理解执行顺序，不替代任务验收标准。

## 当前开发状态

第三轮实时进度只看 [Plan_R3.md](./Plan_R3.md)。旧 `FULL-*` 状态、审查和验收结论完整保留在 [docs/STATUS.md](./docs/STATUS.md) 与 `docs/deliveries/`，但不再作为当前任务队列。

2026-08-24 R3 进展：行情页、画线、市场分类、查询缓存、账户/策略、期货、本地通达信导入、数据源浏览和离线快照已形成尚未提交的实现；定向 106 项 Python 测试及 Vue 生产构建通过。通达信证券日线缩放/成交量单位和 TickDB Raw-to-Silver 仍是明确未完成项，详见 R3 计划与专项审计。

2026-08-12 历史存量基线：桌面后端曾统计 9,937 个标的、3,090,089 条 K 线；此计数会随本地导入变化，当前值必须以运行中的 `/api/market/overview` 为准，不能从本文推断。

> 正式开发口径：项目不再区分 P0/FULL 阶段；`FULL-*` 编号仅作历史任务追踪，不代表优先级或阶段。
> 当前按 5.1–5.9 系列统一开发，完成后再集中测试、审查与验收。

Day 0 已于 2026-08-04 停止执行且未封板。`Plan.md` 与 `docs/deliveries/D0-*` 是历史计划和证据，不是后续会话的自动待办队列。

仍然有效的工作规则：数据源能力未逐项实测时只能写“候选”或“待验证”；任何架构变化必须先由用户批准 ADR；实现、审查和验收由独立任务完成并保留可复核证据。

## 仓库结构（D0-001 建立项目骨架）

- `desktop/`：数据生产端，包含 Provider 探针、标准标的、Bronze/Silver、质量检查、聚合、行情包和签名代码。
- `android/`：Android 13+ 消费端，Kotlin + Jetpack Compose，包含个人/行情库边界、签名行情包导入和离线 K 线容器，`minSdk=33`。
- `contracts/`：D0-002 固化的共享 JSON Schema。
- `tests/fixtures/`：跨端共享测试夹具目录。
- `docs/`：架构、交付、验收模板和阶段性交接文档。
- `scripts/verify.ps1`：统一基线验证入口，依次执行 Python 环境、Ruff、Schema、完整 pytest、Android Lint/JVM 单测与 Debug APK 构建。

## 锁定工具链基线

版本权威清单见 `toolchain.versions.toml`：Python 3.11.0、JDK 21、Gradle Wrapper 8.5、AGP 8.3.2、Kotlin 2.0.0、Android SDK 34（Platform revision 3）和 Build Tools 34.0.0。Python 完整依赖锁见 `desktop/requirements.lock`，Android 传递依赖由 Gradle lockfile 固定。

路径说明：仓库当前位于英文路径 `C:\Users\qingd\Documents\MarketListener`，命令行可直接构建；若以后把仓库克隆或移动到中文路径，JDK 21/Gradle 的测试 worker 会把英文 junction 解析回中文物理路径并导致测试类加载失败，届时可临时执行 `subst M: <仓库绝对路径>`，从 `M:\` 运行 Gradle，结束后执行 `subst M: /D`（盘符 `M:` 已占用时选择其他空闲盘符）。

```powershell
# 数据生产端
py -3.11 -m venv desktop\.venv
desktop\.venv\Scripts\python -m pip install -c desktop\requirements.lock -e "desktop[dev]"
desktop\.venv\Scripts\python -m market_monitor --version
desktop\.venv\Scripts\python -m pytest desktop\tests

# Android
$env:JAVA_HOME = "C:\path\to\jdk-21"
android\gradlew.bat -p android testDebugUnitTest
android\gradlew.bat -p android assembleDebug

# 统一入口（脚本显式使用 JDK 21，并自动映射临时英文盘符运行 Gradle）
powershell -ExecutionPolicy Bypass -File scripts\verify.ps1
```

## Provider 本地配置与安全探针

凭据只可保存在进程环境变量，或在仓库外的显式配置文件中。`.env.example` 只是变量名说明，CLI 不会自动读取仓库内的 `.env`，也不会在输出中回显配置值。

```powershell
# 可选：在仓库外创建配置文件，再显式传入。不要把真实文件放入仓库。
Copy-Item .env.example $env:USERPROFILE\market-monitor.env
desktop\.venv\Scripts\python -m market_monitor probe --config-file $env:USERPROFILE\market-monitor.env --provider joinquant
```

`probe` 总会生成 JSON 和 Markdown 报告，并输出一行机器可读 JSON：`0` 表示没有失败或阻塞，`2` 表示部分能力失败/阻塞，`3` 表示全部选定能力都因本地配置缺失而阻塞，`64` 表示参数或显式配置文件错误。没有真实凭据时，JQData 的用户名和密码分别报告为 `BLOCKED/CONFIGURATION`；该命令不把未探测来源写成成功。

## 全量日线回填与本地后端

全量任务会把数据写到本机的 `data_control/`，该目录及其断点状态、错误日志均被 Git 忽略。命令可安全重跑：已完成标的会从本地检查点跳过。所有页面显示时间统一为中国标准时间，格式为 `YYYY-MM-DD HH:MM:SS`。

### 本地数据存储

本项目采用分层混合存储，而不是把所有内容放进单一数据库：

- `catalog.duckdb`：DuckDB 目录库，保存采集运行、分区登记、数据集定义和 Gold 派生指标，并直接查询 Silver Parquet。
- `silver/**/*.parquet`：A 股、港股、ETF、指数和期货等标准化行情；按市场、资产类型、周期和年份分区。
- `bronze/**/*.json`、`f10/**/*.jsonl`：上游原始响应、F10 明细、收入构成及可恢复采集记录。
- `personal/*.json[l]`、`logs/*.jsonl`：自选、仪表盘、台账和追加式事件日志。
- SQLite：只用于 Android 同步包 `payload.sqlite`、包台账和部分任务检查点，不是电脑端行情主库。

### 期货增量导入

通达信期货通已下载的数据可作为国内期货主源；`L7`、`L8`、`L9` 分别代表次连、主连和原生加权。运行：

```powershell
desktop\.venv\Scripts\python -m market_monitor bulk-futures --data-root data_control --tdx-futures-root C:\new_tdxqh
```

任务将本地 5 分钟/日线、AKShare 国内主连备用、两个 CCIDX 商品指数和受控国外连续合约写入 Silver。检查点位于 `data_control/state/`；不会读取 Cookie、CTP 或 pytdx 期货网络协议。AKShare 的 `品种0` 只作主连备用，`品种9` 不作为加权来源。
- 浏览器 IndexedDB：只保存网页查询缓存，不是权威业务数据源。

`data_control` 出现在仓库根目录，是因为 CLI 的 `--data-root data_control` 使用相对路径；相对路径按启动命令的当前工作目录解析。这是本地开发时期形成的运行约定，并非 DuckDB 或 Parquet 的限制。所有采集和后端命令都可以改传仓库外的绝对路径，例如 `D:\MarketListenerData`。迁移已有数据时，采集、后端、定时任务和脚本必须统一切换到同一个绝对路径，不能只移动目录。

```powershell
# A 股、港股个股日线（可选 CN / HK / BOTH）
desktop\.venv\Scripts\python -m market_monitor bulk-stocks --data-root data_control --market BOTH --workers 4
# 境内 ETF 日线
desktop\.venv\Scripts\python -m market_monitor bulk-etfs --data-root data_control --workers 4
# 同花顺市场宽度与指数快照；受站点反爬/登录策略影响可能部分完成
desktop\.venv\Scripts\python -m market_monitor ths-market --data-root data_control
# 启动仅本机可访问的网页后端
desktop\.venv\Scripts\python -m market_monitor serve --data-root data_control --host 127.0.0.1 --port 8765 --quiet
```

访问 `http://127.0.0.1:8765/`。行情页会显示本地实际覆盖，数据源页会展示来源、周期、字段完整度及本地路由配置；它们不在页面请求时抓取第三方数据。

需要在没有后端和网络的另一台电脑查看时，可生成单文件只读快照：

```powershell
desktop\.venv\Scripts\python scripts\build_offline_html.py --data-root data_control --report-root reports
```

输出位于 `exports/MarketListener-离线快照-*.html`，复制该 HTML 后用新版 Edge 或 Chrome 双击打开即可。文件包含导出时的行情、最近 90 根日 K 线、F10 摘要、指标、产业链摘要、数据目录、任务与日志；它不会联网，也不能更新数据或执行写操作。`exports/` 含本地数据，已被 Git 忽略。

若需要保留生产 Vue 页面结构，可在本机后端运行、前端已构建且已有产业链 PNG 截图时生成“静态网站文件夹”：

```powershell
desktop\.venv\Scripts\python scripts\build_static_site.py --base-url http://127.0.0.1:8765 --atlas-image C:\path\to\industry-atlas.png
```

该脚本复制生产资源并嵌入受限快照，每类列表最多 10 条；`build_offline_html.py` 则直接从本地数据生成单个 HTML。两者用途不同，输出均位于被忽略的 `exports/`，不得提交到 GitHub。

## 研报知识库与产业链图谱

电脑端后端启动后访问 `http://<电脑IP>:8765/industry/` 可查看 SVG 产业链图谱（177 条原始子链，支持搜索公司/产品/原材料/环节与事实定位）；访问 `http://<电脑IP>:8765/industry-v2/` 可查看新版券商研报式产业链全景图（浅色上游/中游/下游/服务分区、公司卡片密集、悬浮 F10、搜索/缩放/证据抽屉，完全离线零 CDN；当前展示口径 75 条链 / 7,090 家带代码公司 / F10 CN 5,539 + HK 2,806）。研报流水线全部本地执行，`行业产业链研报/` 不纳入 Git：

```powershell
# 解析/切块/并发抽取（幂等，已处理自动跳过）
desktop\.venv\Scripts\python -m market_monitor reports process --report-root "行业产业链研报" --output-root reports\industry
# 状态跟踪（每篇 report_*.json 带 status/review 标识）
desktop\.venv\Scripts\python -m market_monitor reports status --output-root reports\industry
# 脚本化核验（schema/事实/证据/链归属/警告）
desktop\.venv\Scripts\python -m market_monitor reports verify --output-root reports\industry
# 按产业链聚合并生成 industry-map.html（SVG 图谱）
desktop\.venv\Scripts\python -m market_monitor reports chains --output-root reports\industry
# 生成新版产业链全景图 industry-atlas.html/json（合并 F10 底表 + 旧快照）
desktop\.venv\Scripts\python -m market_monitor reports atlas --output-root reports\industry --data-root data_control
```

核验为脚本化规则核验；未做真实网络检索核验。当前 1 篇待复核（银河证券磷化铟报告，疑似扫描件，建议 OCR）。产物：`reports/industry/report_*.json`、`batch_summary.json`、`chain_index.json`、`industry-map.html`、`industry-atlas.html/json`，快照同步 `data_control/industry/` 并随同步包下发。F10 底表已抓取：A 股 5,539 / 港股 2,806 全市场限速入库 `data_control/industry/f10/`（A 股含收入构成 `revenue_breakdown`；港股无收入构成数据源），重跑 `reports atlas` 即自动合并进全景图。

## Day 0 历史状态（只读，2026-08-04）

Day 0 已停止执行且未封板。桌面单元测试（49 passed）、Android JVM 测试、Debug APK 构建、16 KB 原生库对齐和 16 KB 模拟器主界面启动已实际通过；Android 已可中文显示行情包导入状态，并具备读取已激活 `payload.sqlite` 的标的、周期和 bars 代码。

未达到验收标准的项目包括：真实 Provider 数据探针（当前四个来源均 FAILED/UNSUPPORTED）、基于真实数据生成并导入的签名行情包、已导入 bars 的 K 线查询/展示、Android 端到端验收，以及 Chaquopy + NumPy 策略运行时（NumPy 在 16 KB 设备加载 `libgfortran` 失败）。ADR-0008 已决定后续 Android 改用声明式策略 DSL，但该决定不把历史失败改写为成功。
