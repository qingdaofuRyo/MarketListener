# MarketListener Android UI 重构验收报告

> 验收时间：2026-08-10
> 仓库：`SUPMColdRain/MarketListener`（工作树：`C:\Users\<user>\Documents\MarketListener`）
> 范围：Android App UI/UX、主题、图表展示、页面结构与必要 UI 架构升级；
> 不重写行情、策略、交易统计、产业链、数据库、同步包、安全校验等业务逻辑。

## 1. 结论摘要

- Phase 1 审计文档已交付：[ANDROID_UI_CURRENT_ANALYSIS.md](ANDROID_UI_CURRENT_ANALYSIS.md)。
- Phase 2～9 代码改造已全部落地，Phase 8 静态 QA 与 Phase 9 性能约束均已按代码级完成。
- Phase 10：单元测试 108 项全绿；`lintDebug`、`assembleDebug` 通过；
  新增 6 个 `androidTest` 文件，`connectedDebugAndroidTest` 已在本地三个 AVD
  （Android 13 / API 33、Android 14 / API 34、Android 17 / API 37）实际执行：
  每个版本 7 项测试全部通过，结果归档于
  `docs/evidence/android-instrumented/`。
- 完整回归 `scripts/verify.ps1`：PASS（pip check、69 项锁依赖、ruff、
  共享 schema、desktop pytest、lintDebug、testDebugUnitTest、assembleDebug）。
- 渲染证据：API 34 AVD 实机截图（Splash、五个页面 × Light/Dark 共 11 张，
  像素采样确认背景色分别等于 `#F5F7FA` / `#0B0E14`），归档于
  `docs/evidence/android-ui-screenshots/`。
- 尚未完成的项目（需要真机/外部资源）：Android 13/14 人工 UI 验收、
  正式 MarketListener Logo 资源替换。

## 2. Phase 对照

| 阶段 | 任务 | 交付/证据 | 状态 |
| --- | --- | --- | --- |
| Phase 1 | T01 审计 | `docs/ANDROID_UI_CURRENT_ANALYSIS.md`（页面组成/MainActivity 职责/主题/导航/K 线/DataScreen/状态管理/可复用组件/影响范围/测试影响） | DONE |
| Phase 2 | T02–T04 Design System | `ui/theme/`：`Color.kt`、`Theme.kt`、`Type.kt`、`Shape.kt`、`Dimensions.kt`、`MarketColors.kt`、`ThemeMode.kt`、`ThemeRepository.kt`；`MarketListenerTheme` 统一包裹；三模式（SYSTEM/LIGHT/DARK）+ DataStore 持久化 | DONE |
| Phase 3 | T05–T06 Splash/Branding | core-splashscreen；`splash_logo_placeholder` 占位、adaptive icon、`postSplashScreenTheme`；正式 Logo 记为待补资源（不生成假 Logo） | DONE（占位） |
| Phase 4 | T07–T10 App Shell | `MarketListenerApp/AppScaffold/AppNavigation`；MainActivity 收敛为业务回调；五 Tab 真 Icon+Label；统一 TopBar；设置对话框三主题切换 | DONE |
| Phase 5 | T11–T15 图表基础设施 | `TradingChartView`（Lightweight Charts 主题注入）、`ChartTheme`、`KlineChartHtml`、`EChartsView`、`EChartsOptionBuilder`、`Sparkline`、`AnimatedRanking`；本地 `assets/echarts.min.js`；保留 `lightweight-charts.standalone.production.js` | DONE |
| Phase 6 | T16–T23 DataScreen | `DataDashboardViewModel`、`DataDashboardMapper`、UI models；LINE_MULTI/AREA/HEATMAP/RANKING 面板；缺失数据不渲染 0；NaN/Infinity 过滤；降采样保留首尾真实点；Ranking 仅真实时间帧 | DONE |
| Phase 7 | T24–T27 其他页面 | 行情（紧凑状态行 + 自选名称/代码/最新价/涨跌幅/Sparkline + K 线中心）、策略、统计（原生净值/回撤图）、产业链接入统一 Design Token 与 WebView 主题桥 | DONE |
| Phase 8 | T28 主题 QA | 静态扫描无页面级硬编码颜色；状态栏/导航栏图标随主题切换（`SideEffect` + `WindowCompat`）；五页面 token 统一 | DONE（代码级） |
| Phase 9 | T29 性能 | WebView 实例受控（K 线 + 当前可见 ECharts 面板 + 产业链）；大列表 LazyColumn；横屏 K 线使用 `chartHeightLarge` | DONE（代码级） |
| Phase 10 | T30–T33 测试 | 单测 108 项全绿；`connectedDebugAndroidTest` 在 Android 13/14/17 三个本地 AVD 分别执行，每个 7 项全部通过（结果归档 `docs/evidence/android-instrumented/`）；lint/assemble 通过 | DONE（单测 + Instrumented 已执行） |

## 3. 测试证据

### 3.1 单元测试（T30）

```text
tests=108 failures=0 errors=0 skipped=0 suites=28
```

覆盖：

- 主题：`ThemeModeTest`（SYSTEM/LIGHT/DARK 解析、存储往返、未知值回退 SYSTEM）；
- 数据看板：`DataDashboardMapperTest`（NaN/Infinity 过滤、降采样、时间窗、
  Ranking 真实帧与变化率、Heatmap 归一化、空面板隐藏）；
- 行情自选：`QuoteRowMapperTest`（最新价/涨跌幅来自真实蜡烛、无前收盘时
  涨跌幅为 null、NaN/Infinity 排除且不伪造 0、Sparkline 时间戳映射、
  证券代码从真实 label 提取（无分隔时回退 instrumentId）、价格/涨跌幅
  格式化不出现 `-0.00%`）；
- K 线：`KlineChartHtmlTest`（Dark/Light 颜色注入、空态转义、无 undefined、
  真实 OHLC）；
- ECharts：`EChartsOptionBuilderTest`；
- 导航：`AppNavigationTest`（五个一级 Section、每个都有真 Icon）；
- Splash：`SplashResourcesTest`；
- 业务回归：Verifier、Graph、Trading、Strategy、Storage、Schema 等既有测试全部保留且通过。

### 3.2 Instrumented / Compose UI 测试（T31/T32）

新增 `android/app/src/androidTest/`：

| 文件 | 覆盖 |
| --- | --- |
| `AppScaffoldNavigationTest.kt` | 底部导航五个页面均可进入 |
| `SettingsDialogTest.kt` | SYSTEM/LIGHT/DARK 三选项可选且回调正确 |
| `ThemeRepositoryInstrumentedTest.kt` | 主题选择跨 Repository 实例持久化 |
| `DataScreenEmptyStateTest.kt` | 无数据时显示引导文案，不显示 0 |
| `ChartSurfaceRenderTest.kt` | K 线/ ECharts 两种 WebView 表面可组合 |
| `MarketListenerAppSmokeTest.kt` | 整个 App Shell：五入口存在、进入数据页、设置切深色并持久化 |

已编译产物：

```text
android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk
```

依赖（`androidx.test.ext:junit`、`espresso-core`、`ui-test-junit4`、
`ui-test-manifest`）已写入 `android/app/gradle.lockfile`。

已在本地三个 AVD 执行：

```powershell
android\gradlew.bat -p android :app:connectedDebugAndroidTest --no-daemon
```

每次结果（`android/app/build/outputs/androidTest-results/connected/debug/`）：

```text
tests=7 failures=0 errors=0 skipped=0
```

归档证据：

```text
docs/evidence/android-instrumented/TEST-Android-13-ML_API_33.xml
docs/evidence/android-instrumented/TEST-Android-14-ML_API_34.xml
docs/evidence/android-instrumented/TEST-Android-17-Pixel_10_Pro_XL.xml
docs/evidence/android-ui-screenshots/（00-splash.png、01～10 五个页面 × Light/Dark）
```

覆盖 App Shell 五入口、进入数据页、设置切深色并持久化、无数据空态、
两种图表 WebView 组合、三主题选项、ThemeRepository 跨实例持久化。

### 3.3 构建/静态检查（T33）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify.ps1
```

结果：`FULL-003 baseline verification passed.`，包含：

- pip check / 69 项锁依赖校验；
- ruff 静态分析；
- 共享 JSON Schema fixtures；
- desktop pytest 全量；
- `lintDebug` BUILD SUCCESSFUL；
- `testDebugUnitTest` BUILD SUCCESSFUL（108 项）；
- `assembleDebug` BUILD SUCCESSFUL，APK：
  `android\app\build\outputs\apk\debug\app-debug.apk`。

另执行：

```powershell
android\gradlew.bat -p android :app:assembleDebugAndroidTest --write-locks --no-daemon
```

结果：BUILD SUCCESSFUL（androidTest APK 编译通过）。

## 4. 业务逻辑保护

- 未重写行情数据库/`payload.sqlite` 读取；`ImportedMarketDataReader`、
  `MarketPackageImporter/Verifier/Worker`、签名/哈希/结构校验链路保持不变；
- `WatchlistRepository`、`GraphRepository`、`StrategyViewModel`、
  `TradingRepository`、`PositionCalculator`、`TradingStatsCalculator`、
  `StoragePolicy` 语义未改；
- 保留 `lightweight-charts.standalone.production.js`，未引入 CDN/远程字体；
- 新增本地 `echarts.min.js`（同仓库 desktop asset 复制，Apache-2.0，
  已在 `THIRD_PARTY_NOTICES.md` 记录）；
- desktop 后端改动为同一仓库既有重构工作，本次 Android 回归通过未破坏其测试。

## 5. 明确禁止项核查

| 禁止项 | 核查结果 |
| --- | --- |
| 底部导航文字冒充 Icon | 已改为真 Icon + Label |
| 只有首页支持 Dark Mode | 五页面均走 `MarketListenerTheme`/`MarketTheme` |
| 使用默认 Material 动态色 | 自建 `LightColorScheme/DarkColorScheme`，禁用 dynamic color |
| 主题选择无法保存 | DataStore `ThemeRepository` + instrumented 持久化测试 |
| SYSTEM 模式不生效 | `resolveIsDark` 单测覆盖 |
| K 线颜色与主题冲突 | `KlineChartHtmlTest` 验证 Dark/Light 注入 |
| 缺失数据显示为 0 | Mapper 过滤 + 空态文案；`DataScreenEmptyStateTest` 覆盖 |
| 编造 Ranking/Heatmap | Mapper 仅使用真实 metric 时间帧，单测验证无插值 |
| 每页硬编码颜色 | 静态扫描 `Color(...)` 无页面级命中（仅 theme/图表主题文件） |
| 一屏大量 WebView | 面板级 WebView 受控；K 线单 WebView |
| MainActivity 当所有 UI 容器 | 已拆 `MarketListenerApp/AppScaffold/AppNavigation` |
| 使用 CDN/远程字体/第三方 Logo | 未引入；Logo 为占位资源 |
| 删除 Lightweight Charts | 保留在 assets |

## 6. 剩余人工验收项

自动化覆盖已完成（Android 13/14/17 三个 AVD 的 instrumented 测试均通过）。
以下项需要 Android 13/14 真机人工观察（模拟器已做主要功能覆盖）：

1. Splash 显示 Logo 占位图、不白屏、进入主界面自然；
2. 五个页面在 Light/Dark/System 下逐页切换检查（含 WebView 图表配色）；
3. K 线、热力图、折线图横屏验证；
4. DataScreen 长列表滚动流畅度与 WebView 数量观察；
5. 正式 MarketListener Logo 资源替换（当前为占位，待用户提供品牌资源）。

上述完成前，Android 侧“真机人工验收”一项不标记 DONE；代码、单测、lint、
构建与 instrumented 测试（已在 Android 13/14/17 三个 AVD 执行通过）已全部交付。
