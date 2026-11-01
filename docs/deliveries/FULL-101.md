# FULL-101 实现交付：本地配置、安全探针与 CLI

日期：2026-08-05  
状态：`REVIEW`

## 范围与边界

本交付仅实现 `FULL-101`：本地凭据配置加载、日志与报告脱敏、受控 Provider runner、CLI 机器可读退出码、示例配置及相应测试。没有执行真实登录、网络探测或后续 `FULL-110` 至 `FULL-113` 的数据源能力任务。

## 实现结果

- 新增 `market_monitor.configuration`：默认仅读取当前进程环境；只有显式 `--config-file` 才解析 env 文件，且该文件必须位于仓库外。配置值只保留在进程内，不写入报告、日志或仓库。
- `.env.example` 仅列出 JQData 的变量名和仓库外显式加载方式；不再暗示仓库内 `.env` 会被自动读取。
- JQData 缺少用户名或密码时，runner 不调用 SDK；每个缺项分别生成一个带 `CONFIGURATION` 错误的 `BLOCKED` capability，符合 ADR-0007 的独立能力语义。
- `ProbeRunner` 为整次 Provider 调用设置显式超时；只有已结束且因 `NETWORK` 或 `RATE_LIMIT` 不能返回能力列表的调用才按有限次数和指数退避重试。超时调用只报告一次，不重复启动仍可能在上游清理中的 daemon worker。退避 sleeper 与 UTC clock 可注入。已返回的部分能力列表不会被重试逻辑覆盖或伪装为来源成功。
- 脱敏覆盖递归映射/列表/元组、敏感键、`token`/`password` 等赋值、Bearer 值、URL 用户信息、认证头及已注册的实际本地值（含短值）。JSON 与 Markdown 报告在输出边界再次递归脱敏。
- `market-monitor probe` 输出一行稳定 JSON：`0=SUCCESS`、`2=PARTIAL_FAILURE`、`3=CONFIGURATION_BLOCKED`、`64=ARGUMENT_ERROR`；未知 Provider 不回显用户输入。报告仍为 JSON 和 Markdown 双格式。

## 自动化证据

| 验证项 | 实际命令 | 结果 |
|---|---|---|
| 配置、超时、重试、限流、脱敏、部分失败与 CLI 专项 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` | PASS，26 项；全部为固定 SDK/Provider，不触网。 |
| 桌面完整回归 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q` | PASS，102 项；仅有既有 ZIP 同名 `manifest.json` 警告。 |
| 静态检查 | `desktop\.venv\Scripts\python.exe -m ruff check desktop\src desktop\tests` | PASS。 |
| 完整统一验证 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS：Python 3.11.0、50 个精确锁定依赖、Ruff、23 个共享 Schema 夹具、102 个桌面测试、Android `lintDebug`、`testDebugUnitTest`、`assembleDebug`。 |
| 变更完整性与敏感扫描 | `git diff --check`；对非测试源码/文档运行凭据特征扫描 | PASS；未发现实际 JQData 值、私钥或 AWS 访问键。测试中的固定假值仅用于验证脱敏，不代表凭据。 |

## 兼容性与安全

Provider Contract v2 的 JSON Schema 和公开字段未改变；`ConfigurationRequirement`、本地配置加载和 runner 重试均为可逆实现层扩展。已有 provider 的默认配置要求为空，JQData 保持直接调用时的明确错误，但错误分类更准确地为 `CONFIGURATION`。真实凭据、网络/账户可用性和数据能力均尚未声称通过，留给后续独立的真实探针任务验收。

## 下一步

实现已完成并重新进入 `REVIEW`。只等待新的独立审查；本实现者不自审、不验收且不启动后续任务。下文保留全部历史审查与修复记录。

## 独立审查（2026-08-05）

结论：`CHANGES_REQUIRED`。

### P1：仓库内 junction 可绕过仓库外显式配置约束

- 位置：`desktop/src/market_monitor/configuration.py:50-60`
- 问题：实现仅检查 `config_file.resolve()` 的最终目标是否位于仓库外。传入路径本身可以位于仓库中并经 Windows junction 指向仓库外，因而违反“显式配置文件必须位于仓库外”的边界，降低防止误提交/误读取仓库路径的保障。
- 复现：创建临时 `repo\linked-outside` junction 指向临时仓库外目录并传入 `repo\linked-outside\credentials.env`；`load_local_configuration()` 成功读取 `JQDATA_PASSWORD=review-only-value`，而非拒绝该仓库内路径。
- 修复要求：同时规范化并校验调用者提供的绝对路径（不跟随链接的词法位置）与最终解析目标均在仓库外；覆盖相对路径、大小写、symlink/junction 和不存在文件的回归测试。

### P1：脱敏遗漏 camelCase 凭据字段，且 CLI 直接回显未脱敏报告路径

- 位置：`desktop/src/market_monitor/providers/runner.py:29-72`；`desktop/src/market_monitor/cli.py:84,88-94`
- 问题：敏感键和赋值正则只识别分隔符形式，`accessToken`、`clientSecret`、`privateKey` 等常见 camelCase 字段不会被脱敏。另一个输出边界把 `--report-dir` 派生路径直接放进 stdout JSON，未调用脱敏；若路径含敏感赋值文本便会原样输出。二者均违背报告、Markdown、JSON 和 CLI 输出不得泄露凭据的要求。
- 复现：`redact_secrets({'accessToken':'LEAK_B','clientSecret':'LEAK_C','privateKey':'LEAK_D'})` 返回原值；以 `apiKey=CLI_LEAK` 为报告目录、固定无网络 provider 执行 `probe`，stdout 的 `reports` 数组包含 `apiKey=CLI_LEAK`。
- 修复要求：采用能覆盖 camelCase/连字符/下划线的键和值策略，并在所有 CLI 输出字段（包括路径）统一递归脱敏；添加 JSON、Markdown、异常、嵌套短值、URL/header 和 CLI 的反例测试。

### P2：重复配置键被静默覆盖

- 位置：`desktop/src/market_monitor/configuration.py:64-79`
- 问题：解析器以 `parsed[name] = value` 接受重复键并采用最后一个值；例如同一文件内两次 `JQDATA_PASSWORD` 读取为第二个值。凭据配置应拒绝歧义输入而不是静默改变实际使用的账户。
- 修复要求：对同一文件内的重复键返回不含值的 `ConfigurationError`，并覆盖 UTF-8 BOM、引号、非法格式和重复键。

### 已复核证据

- 专项：`desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q`：26 passed。
- 全量：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1`：通过 Python/JDK/锁定依赖/Ruff/23 个 Schema 夹具/102 个桌面测试/Android lint、JVM 测试和 Debug APK。桌面测试保留既有 GBK 子线程与 ZIP 重名警告，非本任务引入的失败。
- 质量/安全：`git diff --check` 通过；非测试源码与文档凭据特征扫描未发现真实凭据。上述临时反例只使用 `review-only-value` 与 `*_LEAK` 合成值，未读取、写入或回显任何真实本机凭据。

## 审查 P1/P2 修复（2026-08-05）

本次仅修复上列独立审查的 2 个 P1 和 1 个 P2，未执行真实 Provider、登录或网络探测。

- 配置文件同时按调用者的词法绝对路径（不跟随链接）和 `resolve(strict=True)` 最终目标检查仓库边界；任一位置在仓库内均拒绝。比较使用 `normcase` 与 `commonpath`，能安全处理大小写、`..`、Windows 盘符/UNC 的不同根。配置文件还必须为可读普通文件。
- 配置解析使用 `utf-8-sig` 接受 UTF-8 BOM，变量名规范为大写；同一文件内的大小写等价重复键会报不含值的错误，不再由后值静默覆盖。
- 脱敏键先删除分隔符并 `casefold`，覆盖 camelCase、snake_case 与 kebab-case 的 `accessToken`、`clientSecret`、`privateKey`、`apiKey`、`authorization` 和 `credential` 等。赋值、URL query、URL 用户信息、认证头、嵌套结构及注册的实际值均在输出边界处理。
- 已注册短值也不会泄露：完整 token 会在异常、JSON/Markdown 报告和 CLI 路径中被替换；JSON 属性名与普通词内的子串不会被破坏。CLI 的所有 `_emit` 输出都统一经过递归脱敏。

| 修复验证项 | 实际命令 | 结果 |
|---|---|---|
| 修复专项 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` | PASS，34 项。覆盖相对/`..`/大小写、两向 symlink 或 Windows junction、BOM、重复键、camel/snake/kebab、URL/header、短值异常/双报告和 CLI 合成敏感目录。 |
| 桌面完整回归 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q` | PASS，110 项；仅有既有 ZIP 同名 `manifest.json` 警告。 |
| 完整统一验证 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS：Python 3.11.0、50 个精确锁定依赖、Ruff、23 个共享 Schema 夹具、110 个桌面测试、Android `lintDebug`、`testDebugUnitTest`、`assembleDebug`。 |
| 变更与合成敏感扫描 | `git diff --check`；对源码及普通文档执行凭据特征扫描 | PASS。历史独立审查段中的 `review-only-value` 是明确标注的合成复现值，扫描命中该保留证据而非真实凭据；源码与普通入口文档没有配置值、私钥或 AWS key 命中。 |

修复完成后应重新进入 `REVIEW`，只等待全新的独立审查；本实现者不自审、不验收且不启动后续任务。

## 修复后独立复审（2026-08-05，gpt-5.6-terra high）

结论：`CHANGES_REQUIRED`。

### P1：原始 JSON 与 Markdown 代码格式中的敏感字段值仍会泄露

- 位置：`desktop/src/market_monitor/providers/runner.py:32-35,106-119`。
- 复现：`redact_secrets('{"accessToken":"REVIEW_JSON","clientSecret":"REVIEW_CLIENT","privateKey":"REVIEW_PRIVATE"}')` 原样返回三项合成值；`redact_secrets('`accessToken`: `REVIEW_MARKDOWN`')` 也原样返回。现有 `_COLON_ASSIGNMENT` 只匹配未加引号的键和值，不能处理 JSON 属性名后的引号及 Markdown 代码标记。若上游 SDK 将认证错误或响应摘要作为 JSON/Markdown 文本放进异常、能力详情或报告，`_sanitize_capability()`、JSON/Markdown 报告及 CLI 的递归输出边界都会保留该秘密，违反 FULL-101 的“不泄露凭据”验收条件。
- 修复要求：在保留 JSON 属性名及普通子串不被破坏的前提下，补足带引号 JSON 键/值和 Markdown 代码格式的敏感键赋值脱敏；增加从异常到 JSON 报告、Markdown 报告和 CLI 的反例回归测试，并断言合成值不出现。

### 本次复核证据

- 已复现并确认旧问题均已修复：仓库内 junction 指向仓库外、仓库外 junction 指回仓库、相对/`..` 路径、大小写变体均被拒绝；UTF-8 BOM 可读，重复键不回显配置值并被拒绝；`NUL` 设备文件被拒绝。测试仅使用临时目录与 `REVIEW_*` 合成值。
- Runner 竞态与重试专项：超时仅调用一次，释放 daemon worker 后已返回结果不变；`NETWORK` 与 `RATE_LIMIT` 均恰好三次调用并使用 `0.5, 1.0` 退避；`AUTH` 和已有部分 capability 结果均不重试。
- `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q`：34 passed。
- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1`：通过（Python、锁定依赖、Ruff、23 个 Schema fixture、110 个桌面测试、Android lint/JVM/Debug APK）；仅保留既有 ZIP 同名 `manifest.json` 警告。
- `git diff --check`：通过；非测试源码/普通文档凭据特征扫描未发现真实凭据、私钥或 AWS 访问键。

## JSON / Markdown 原始文本 P1 修复（2026-08-05）

本次仅修复最新独立复审的原始文本脱敏遗漏，未执行真实 Provider、登录或网络探测。

- 对完整 JSON 文本先使用 `json.loads()` 解析、按原有递归规则脱敏、再以 `json.dumps()` 输出，因此嵌套对象、相邻字段和转义字符保持为可再次解析的 JSON，属性名不变而敏感值替换为 `***`。
- 对嵌入普通日志的 JSON 片段补充字段级非贪婪规则：带引号 key 可处理带引号、`null`/布尔和未加引号的值，不跨逗号、闭合符或行界；相邻安全字段不会被吞掉。
- 对 Markdown inline-code 的 `` `key`: `value` `` 格式按相同敏感 key 规范替换 value。既有 assignment、URL/query、header 和短 token 边界替换保持有效。
- 异常进入 runner、机器 JSON 报告、人类 Markdown 报告及 CLI 输出的路径均复用同一输出边界。

| 修复验证项 | 实际命令 | 结果 |
|---|---|---|
| JSON/Markdown 专项 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` | PASS，37 项。覆盖复审三值、嵌套 JSON、转义字符、混合日志、相邻字段、Markdown inline-code、未加引号 JSON 值、报告写入与 `json.loads()`。 |
| 桌面完整回归 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q` | PASS，113 项；仅有既有 ZIP 同名 `manifest.json` 警告。 |
| 完整统一验证 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS：Python 3.11.0、50 个精确锁定依赖、Ruff、23 个共享 Schema 夹具、113 个桌面测试、Android `lintDebug`、`testDebugUnitTest`、`assembleDebug`。 |

修复完成后应重新进入 `REVIEW`，只等待新的独立审查；本实现者不自审、不验收且不启动后续任务。

## 深度与复杂度 P1 修复（2026-08-05）

本次仅修复嵌入 JSON 在深度、破损和高候选数量下的脱敏边界；没有执行真实 Provider、登录或网络探测。

- 递归结构脱敏采用 64 层显式上限。达到上限时以 `[redaction depth limit]` 取代余下分支，避免手工构造的深层 mapping/list 触发 Python 递归异常。
- 嵌入 JSON 扫描最多尝试 128 个候选，每个候选最多解码 8 KiB；`JSONDecodeError`、`ValueError` 和 `RecursionError` 都被视为破损片段。扫描保持前进，后续字段级不跨行规则及已注册 secret 的边界替换仍会运行。
- 破损深层对象中的直接敏感键不会再被外层非敏感字段吞掉；`accessToken` 等敏感赋值被替换为 `"***"`，而安全文本保留。
- 新增 1000 层完整/破损 JSON、1000 层手工 mapping、100 KB 多候选文本及 ProviderError 到 result、JSON/Markdown 报告和 CLI 的全链回归。所有反例断言无异常、无合成 secret，并仅产生受控脱敏输出。

| 修复验证项 | 实际命令 | 结果 |
|---|---|---|
| 深度/复杂度及原有专项 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` | PASS，44 项。 |
| 桌面端完整回归 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q` | PASS，120 项；仅有既有 ZIP 同名 `manifest.json` 警告。 |
| 统一验证 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS：锁定依赖、Ruff、schema fixtures、桌面端测试及 Android lint/JVM/Debug APK 均通过。 |

本实现完成后已将 `FULL-101` 置为 `REVIEW`。只等待新的独立审查；本实现者不自审、不验收且不启动后续任务。

## 深度防护后的独立复审（2026-08-05，gpt-5.6-terra high）

结论：`CHANGES_REQUIRED`。

### P1：JSON 扫描预算耗尽或单候选超过 8 KiB 时，敏感键的嵌套值会泄露

- 位置：`desktop/src/market_monitor/providers/runner.py:113-139`、`desktop/src/market_monitor/providers/runner.py:42-45`。
- 问题：`_redact_embedded_json()` 在尝试 128 个起始候选或对每个候选截断 8 KiB 后停止结构化处理；后续 `_JSON_FIELD` 只接受标量值，不能处理 `"accessToken":{"value":"..."}`。因此，敏感属性承载对象或数组时，没有可用的字段级兜底，原值会越过 `redact_secrets()`。这直接违反了预算耗尽仍不得泄露的输出边界要求。
- 复现（均使用合成值）：`redact_secrets('{"outer":' * 128 + '"accessToken":{"value":"REVIEW_BUDGET_NESTED"}' + '}' * 128)` 保留 `REVIEW_BUDGET_NESTED`；`redact_secrets('{"safe":"' + 'x' * 9000 + '","accessToken":{"value":"REVIEW_CHARS_NESTED"}}')` 保留 `REVIEW_CHARS_NESTED`。两例均在合理时间内返回，但结果泄露。直接标量敏感赋值、1000 层完整/破损 JSON 与 100 KB 多候选文本不抛 `RecursionError`/`MemoryError` 且不泄露，不能覆盖该嵌套值反例。
- 修复要求：预算达到时仍必须采用不会依赖结构化解析的保守兜底；一旦识别敏感键，必须遮蔽其完整值（含对象、数组、带引号/未带引号值），或将剩余无法安全处理的片段替换为不含原文的受控占位符。为 128 候选后、8 KiB 后、嵌套对象/数组及 Provider result → JSON/Markdown/CLI 增加端到端反例。

### P1：带引号的非敏感外层赋值会吞掉内部敏感赋值

- 位置：`desktop/src/market_monitor/providers/runner.py:41-44,95-101`。
- 问题：`_EQUALS_ASSIGNMENT` 对普通键匹配完整带引号值后原样返回，随后正则引擎从该匹配尾部继续，故值内的 `accessToken=...`、`clientSecret=...` 不再有机会被单独匹配。该输入既不是完整 JSON 也不是已注册 secret 时，后续规则也无法挽回。
- 复现（均使用合成值）：`redact_secrets('detail="accessToken=REVIEW_SWALLOWED"')` 保留 `REVIEW_SWALLOWED`；`redact_secrets("detail='clientSecret=REVIEW_SWALLOWED_2'")` 保留 `REVIEW_SWALLOWED_2`。这类 SDK/HTTP 异常文本经 `_sanitize_capability()` 后可进入 in-memory result，并由 `write_reports()`、CLI `_emit()` 传递到机器 JSON、Markdown 和 stdout。
- 修复要求：普通非敏感赋值的值也必须继续扫描其内部文本，或在主替换前采用不会产生吞噬效应的策略；覆盖双引号、单引号、JSON 转义片段及全部输出边界，并断言合成值不出现在 result、JSON、Markdown 或 CLI。

### 本轮复审证据

- 1000 层完整/破损 JSON、1000 层手工 mapping、128 候选与 100 KB 多候选文本均在合理时限内返回；短注册 secrets `a`、`xy`、`s3` 均按完整 token 遮蔽；现有配置 BOM/重复键、camel/Markdown/URL/header、超时、`NETWORK`/`RATE_LIMIT` 有界重试、部分结果不重试及 CLI 退出码回归均通过。
- 专项回归：`desktop\.venv\Scripts\python.exe -m pytest tests\test_configuration.py tests\test_provider_runner.py tests\test_cli.py tests\test_joinquant_provider.py -q`，PASS（44 passed），但现有测试未覆盖上述两个反例。
- 统一验证：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1`，PASS：Python 3.11.0、精确锁定依赖、Ruff、23 个共享 Schema fixture、桌面端测试、Android `lintDebug`、`testDebugUnitTest`、`assembleDebug` 均通过；仅有既有 ZIP 重名 `manifest.json` 警告。`git diff --check` 通过。

本复审未实施修复、不验收且不启动后续任务。`FULL-101` 已更新为 `CHANGES_REQUIRED`；实现者只能修复以上 P1 后重新进入独立审查。

`git diff --check` 已通过。非测试源码、README、示例配置及交付索引的凭据特征扫描未发现实际凭据、私钥或 AWS 访问键；交付审查历史中的 `review-only-value` 是既有合成复现值，不是有效凭据。

## 修复后新独立复审（2026-08-05，gpt-5.6-terra high）

结论：`CHANGES_REQUIRED`。

### P1：深度破损 JSON 会使脱敏边界抛出 `RecursionError`

- 位置：`desktop/src/market_monitor/providers/runner.py:141-171` 与 `desktop/src/market_monitor/providers/runner.py:384-395`。
- 问题：`_redact_embedded_json()` 仅捕获 `json.JSONDecodeError`。对极深的不完整对象，`json.JSONDecoder.raw_decode()` 在实际解析前抛出 `RecursionError`，该异常从输出边界逃逸。对应 Provider 的 `NETWORK` 异常既不产生受控的 `ProviderRunResult`，也不会生成 JSON/Markdown 报告或 CLI 输出，违反 FULL-101 对异常路径的超时、失败及日志脱敏可控性要求。这也不满足本次复审对大型合成文本“无死循环/在合理时限内完成”的要求。
- 复现：对 `redact_secrets('{"outer":' * 1000 + 'bad')` 抛出 `RecursionError: maximum recursion depth exceeded while decoding a JSON object from a unicode string`。以同一字符串作为固定 Provider 的 `ProviderError(ErrorCategory.NETWORK, ...)` 运行 `ProbeRunner().run([provider])` 也抛出同一异常。本复审未使用任何真实凭据。
- 修复要求：为嵌入 JSON 扫描增加明确的深度/规模界限，并将解码器的递归失败视为破损片段而非让其逃出输出边界。对无法结构化的片段，必须仍能线性地前进并用字段级兜底规则隐藏直接敏感赋值。增加深度完整和破损 JSON、合成大文本与 Provider 异常到 result/JSON/Markdown/CLI 全链的回归测试，断言不抛异常、不泄漏和安全文本可保留。

### 本次复审证据

- 原嵌入 JSON 漏洞已复现为已修复：前后日志 + 多层对象/数组、相邻多个 JSON、字符串内 `{}`/`[]`、转义引号/反斜杠/Unicode、CRLF、破损外层内层完整、完全破损的 direct sensitive 字段、Markdown 中的 JSON 均不泄漏；完整 JSON 在脱敏后仍可 `json.loads()` 且安全字段保留。
- 专项测试 `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` 通过（40 passed）。统一验证 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` 通过；桌面测试与 Android lint/JVM/Debug APK 均成功（仅保留既有 ZIP 重名 `manifest.json` 警告）。
- `git diff --check` 通过；对非测试源代码和普通文档的凭据特征扫描未发现真实凭据、私钥或 AWS 访问键。

本复审未实施修复、不验收且不启动后续任务。`FULL-101` 必须保持 `CHANGES_REQUIRED`，待新的实现者仅修复上述 P1 后重新进入独立审查。

## 修复后独立复审（2026-08-05，gpt-5.6-terra high，第 2 次）

结论：`CHANGES_REQUIRED`。

### P1：普通日志中嵌套的 JSON 片段仍泄露敏感字段，并进入两种报告和 CLI 输出

- 位置：`desktop/src/market_monitor/providers/runner.py:58-60,115-124`。
- 问题：`_redact_complete_json()` 只处理完整 JSON 文本；普通日志前后包裹 JSON 时，会依赖 `_JSON_FIELD` 的正则。该正则可将非敏感的外层字段（例如 `"outer"`）连同其对象值一次匹配并保留，导致内部的 `accessToken` 不再被下一轮匹配，仍以原值输出。`_sanitize_capability()`、`write_reports()` 和 CLI `_emit()` 复用该脱敏边界，故该值会进入运行结果、JSON 报告、Markdown 报告以及机器可读 CLI 输出。此行为违反 FULL-101 的日志、报告与 CLI 均不得泄露凭据的验收要求。
- 复现：`redact_secrets('SDK failed: {"outer":{"accessToken":"REVIEW_EMBED_TOKEN","clientSecret":"REVIEW_EMBED_SECRET"},"safe":"ok"}')` 仍含 `REVIEW_EMBED_TOKEN`。以此字符串作为固定 Provider 的 `NETWORK` 错误运行 `ProbeRunner` 后，运行结果、`provider-capabilities.json` 和 `provider-capabilities.md` 均含该合成值；用相同字符串调用 CLI `_emit()` 的 stdout 也含该值。`clientSecret` 恰好被该正则的另一匹配替换，不能证明外层嵌套字段安全。
- 修复要求：对日志中的 JSON 片段执行结构化、递归的对象/数组识别与脱敏，或采用不会吞掉子对象的等价策略；覆盖多个嵌套层级、对象数组、前后日志文本、相邻安全字段、转义内容与 JSON/Markdown/CLI 输出边界。测试必须断言所有合成敏感值消失，同时完整 JSON 仍能 `json.loads()`、属性名和非敏感值不变。

### 复审执行与未发现项

- 已复现并确认完整嵌套 JSON、camel/snake/kebab 键、数组、转义引号/反斜杠/Unicode、相邻字段、Markdown inline-code、quoted/unquoted assignment、URL query、认证头和 CRLF 逐项检查；完整 JSON 的结构与非敏感字段保持可解析。短注册 secret、配置文件 BOM/重复键、超时单调用、`NETWORK`/`RATE_LIMIT` 有界重试、部分结果不重试以及现有 CLI 退出码专项也均通过。
- `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q`：PASS（37 项），但未覆盖普通日志中的多层嵌套 JSON 反例。
- `desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q`：PASS（113 项；仅既有 ZIP 同名 `manifest.json` 警告）。`desktop\.venv\Scripts\python.exe -m ruff check desktop\src desktop\tests` 和 `git diff --check`：PASS。
- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1`：PASS（Python 3.11.0、50 个精确锁定依赖、Ruff、23 个共享 Schema fixture、113 个桌面测试、Android lint/JVM/Debug APK）。对非测试源码和普通文档的凭据特征扫描未发现真实凭据；本节全部 `REVIEW_*` 值均为合成反例。

本复审未实施修复、不验收且不启动后续任务。`FULL-101` 应保持 `CHANGES_REQUIRED`，待新的实现者仅修复以上 P1 后重新进入独立审查。

## 嵌套嵌入 JSON P1 修复（2026-08-05）

本次仅修复普通日志中嵌套 JSON 片段的脱敏遗漏，未执行真实 Provider、登录或网络探测。

- 文本扫描器遇到任意 `{` 或 `[` 时使用 `json.JSONDecoder.raw_decode()` 识别完整对象/数组；成功后递归脱敏并以紧凑 JSON 替换，游标直接推进到 decoder 返回的结束位置。前后普通文本、CRLF 和多个相邻片段均保留。
- 对不能解析的外层片段，扫描器每次仅前进一步，使其后完整的内层对象/数组仍可被结构化处理；循环始终前进且不会反复扫描已替换的片段。
- 破损 JSON 的字段级兜底规则不再允许外层值以 `{` 或 `[` 开头，避免非敏感外层 key 吞掉内层敏感字段；既有 Markdown、URL/header、assignment 和短 token 边界脱敏保持不变。
- Provider `NETWORK` error 到 in-memory result、机器 JSON 报告、人类 Markdown 报告和 CLI `_emit` stdout 的端到端反例均确认没有合成敏感值。

| 修复验证项 | 实际命令 | 结果 |
|---|---|---|
| 嵌套 JSON 专项 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` | PASS，40 项。覆盖多层对象、对象数组、转义/Unicode、多个 JSON 片段、CRLF、破损外层恢复、相邻安全字段及 Provider→报告→CLI stdout。 |
| 桌面完整回归 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q` | PASS，116 项；仅有既有 ZIP 同名 `manifest.json` 警告。 |
| 完整统一验证 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS：Python 3.11.0、50 个精确锁定依赖、Ruff、23 个共享 Schema 夹具、116 个桌面测试、Android `lintDebug`、`testDebugUnitTest`、`assembleDebug`。 |

修复完成后应重新进入 `REVIEW`，只等待新的独立审查；本实现者不自审、不验收且不启动后续任务。

## 残余敏感赋值 fail-closed P1 修复（2026-08-05）

本次仅修复最新独立复审指出的候选预算、长片段及复杂值场景下输出边界可能泄露的问题；没有执行真实 Provider、登录或网络探测。

- 完整合法 JSON 在任何嵌入片段预算之前先整体解析、递归脱敏并重新序列化，因此大于 8 KiB 的合法对象/数组仍保持可解析结构及安全字段。
- 对其余自由文本，既有嵌入片段、字段规则和注册值替换完成后，增加独立线性残余检测。它识别裸、单/双引号和反引号包裹的 camelCase、snake_case 与 kebab-case 键及 `:`/`=`，允许键和值之间跨 CRLF；该扫描不受 JSON 候选数、单候选大小或正则匹配位置影响。
- 只有明确的安全哨兵 `***`（可由一层单/双引号或反引号包裹）会被证明安全。敏感键后仍是对象、数组、复杂值、跨行值或未确认标量时，整个输出一律替换成 `[redacted sensitive text]`，不保留原片段或原值。
- 已注册的短值仍按完整 token 边界精准替换；`token_count=42` 与普通说明文本不属于敏感键赋值，不会触发占位。由于 fail-closed 策略，任何无法证明安全的敏感键赋值（即使上游已尝试部分脱敏）均允许保守隐藏整段。
- 新增 128 候选耗尽、超过 8 KiB、敏感键承载对象/数组、`detail` 的单双引号内部赋值、CRLF、多敏感字段、短注册 secret、完整大 JSON 与 ProviderError→result→JSON/Markdown/CLI 全链反例；均断言不抛异常且不泄露合成值。

| 修复验证项 | 实际命令 | 结果 |
|---|---|---|
| 最新 P1 及既有专项 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` | PASS，55 项。 |
| 桌面端完整回归 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q` | PASS，131 项；仅有既有 ZIP 同名 `manifest.json` 警告。 |
| 统一验证 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS：锁定依赖、Ruff、23 个 schema fixtures、桌面端 131 项及 Android lint/JVM/Debug APK 均通过。 |

本实现完成后应重新进入 `REVIEW`，只等待新的独立审查；本实现者不自审、不验收且不启动后续任务。

## 原始文本 preflight 修复后的独立复审（2026-08-05，gpt-5.6-terra high）

结论：`CHANGES_REQUIRED`。

### P1：转义 JSON 文本绕过 preflight 并泄露敏感赋值

- 位置：`desktop/src/market_monitor/providers/runner.py:111-137, 186-250`。
- 问题：原始自由文本的 preflight 只将未转义的单/双引号或反引号作为 quoted key 边界。包含反斜杠转义引号的 JSON 片段不会被完整 JSON 解析，也不会被 `_read_assignment_key()` 或字段正则识别。因此，SDK/网关异常中常见的 `gateway payload {\"accessToken\":\"<synthetic>\"}` 形状会原样通过；这不是本轮承诺的 `:`/`=` 分隔符以外的语义，而是相同 JSON 键值形状的常规字符串转义表示。
- 复现：以 `TERRA_ESCAPED_JSON_LEAK` 作为合成值，调用 `redact_secrets(r'gateway payload {\"accessToken\":\"TERRA_ESCAPED_JSON_LEAK\",\"safe\":\"ok\"}')` 后仍含该值。将同一文本作为固定 Provider 的 `NETWORK` 错误运行，内存 `ProviderRunResult`、`provider-capabilities.json`、`provider-capabilities.md` 和 CLI `_emit()` stdout 四个边界均包含该合成值。
- 修复要求：在任何字段替换、嵌入 JSON 扫描或已注册值替换之前，让 preflight 以有界、不会递归失控的方式识别转义 quoted key/value 形式，或对包含无法证明安全的此类敏感赋值的文本 fail closed。补充 bare/quoted/backticked key 与 `:=`、`: =`、`:`、`=` 的既有变异回归之外的 escaped-quote JSON 反例，并覆盖内存结果、JSON 报告、Markdown 报告、CLI stdout；不得把 Unicode colon-like 字符等未承诺分隔符描述为已支持或已安全。

### 已复核通过的范围

- 对明确支持的 ASCII `:=`（含 colon/equal 之间和 key/value 两侧的空格、tab、LF、CRLF）、`:`, `=` 与 `: =`，以及 bare/single-quoted/double-quoted/backticked camelCase、snake_case、kebab-case key 和 scalar/quoted/object/array/`***evil` value，2,880 个组合均在原始 preflight 阶段没有泄露合成值；无赋值的 `accessToken` 文本和 `token_count=42` 未被误伤。
- 额外确认 NBSP、EM SPACE 会作为 Python `isspace()` 支持的空白参与上述已支持语义；全角冒号和 ratio colon 等 colon-like 字符不属于当前明确支持的分隔符，未将其未被拦截表述为安全。
- `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` 通过（68 passed）；`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` 通过（含 Ruff、23 个 Schema fixtures、144 个桌面测试和 Android lint/JVM/Debug APK）。`git diff --check` 通过，非测试源码与普通文档的凭据特征扫描未发现真实凭据。

本复审未实施修复、未验收且未启动后续任务。FULL-101 必须保持 `CHANGES_REQUIRED`，仅待新的实现角色修复上述 P1 后重新进入独立审查。

`git diff --check` 已通过。非测试源码、README、示例配置与交付索引的凭据特征扫描未发现实际凭据、私钥或 AWS 访问键；交付审查历史中唯一的 `review-only-value` 命中是既有合成复现值，不是有效凭据。

`git diff --check` 已通过。非测试源码、README、示例配置与交付索引的凭据特征扫描未发现实际凭据、私钥或 AWS 访问键；交付审查历史中唯一的 `review-only-value` 命中是既有合成复现值，不是有效凭据。

## 最终独立复审（2026-08-05，gpt-5.6-terra high）

结论：`CHANGES_REQUIRED`。

### P1：跨行 `:=` 分隔绕过 residual fail-closed 检测并泄漏敏感值

- 位置：`desktop/src/market_monitor/providers/runner.py:52-53,156-185`。
- 问题：冒号赋值正则允许 `:` 后的首字符为 `=`；当 `:=` 后紧跟 CRLF/LF，正则只将该 `=` 视为值并替换为 `***`，实际敏感值留在下一行。残余检测随后把前缀 `accessToken:***` 视为安全哨兵，不再关联下一行，因此不会返回固定的 fail-closed 占位符。该原始错误文本会经 runner 的结果、JSON/Markdown 报告及 CLI 输出边界传播，违反 FULL-101 的日志、报告与 CLI 不得泄漏凭据要求。
- 复现：`redact_secrets('accessToken\\r\\n:=\\r\\n<synthetic-secret>')` 返回 `accessToken:***\\n<synthetic-secret>`；同样的 LF、带引号键、带引号值和对象/数组值变体均可保留下一行原片段。以该合成值构造固定 `NETWORK` ProviderError 后，runner 内存结果、`provider-capabilities.json`、`provider-capabilities.md` 和 CLI `_emit()` 的四个输出边界均仍可检出该值。
- 修复要求：将 `:=` 作为一个赋值分隔语义整体处理，或由残余检测在看到此类分隔时一律 fail closed；不得在替换单个 `=` 后将后续跨行值误判为已脱敏。新增 CRLF/LF、bare/quoted/backticked key、quoted/object/array value 以及 ProviderError→result→JSON/Markdown/CLI 的端到端回归，断言敏感原文不存在且复杂值输出为固定占位符。

### 已复核的通过项

- 现有专项回归：`desktop\\.venv\\Scripts\\python.exe -m pytest desktop\\tests\\test_configuration.py desktop\\tests\\test_provider_runner.py desktop\\tests\\test_cli.py desktop\\tests\\test_joinquant_provider.py -q`，PASS（55 passed）。
- 完整统一验证：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\\verify.ps1`，PASS；Python 3.11.0、50 个精确锁定依赖、Ruff、23 个共享 Schema fixture、131 个桌面测试及 Android `lintDebug`/JVM/Debug APK 均通过（仅有既有 ZIP 重名 `manifest.json` 警告）。
- 复核了完整 JSON、嵌入 JSON、对象/数组、预算耗尽、128 候选后、普通 `=`/`:`、registered 短值、大小写/分隔符、BOM/重复键、超时/重试和 CLI 状态码等既有反例；本 P1 是仍可复现的跨行 `:=` 旁路。

本复审未实施修复、未验收且未启动后续任务。`FULL-101` 已更新为 `CHANGES_REQUIRED`；只能修复以上 P1 后重新进入独立审查。

## 原始文本 preflight 与跨行 `:=` P1 修复（2026-08-05）

本次仅修复最新独立复审指出的跨行 `:=` 旁路及检查顺序；没有执行真实 Provider、登录或网络探测。

- 完整合法 JSON 仍优先完整解析、递归脱敏并重新序列化，保持其合法结构。
- 其余原始自由文本在任何嵌入 JSON、字段正则或注册值替换之前先经过独立线性 preflight。preflight 按最长匹配将 `:=`（冒号与等号之间可含空格、LF 或 CRLF）作为一个分隔语义处理，再处理 `:` 与 `=`；键和值之间也允许相同空白。
- 检测支持裸键、单/双引号键和反引号键，且只认可完全终止的 `***`，或一层一致单/双引号或反引号包裹的 `***`。对象、数组、标量、`***evil`、错误包裹及多敏感字段都会在原始证据尚未被替换前直接输出 `[redacted sensitive text]`。
- 既有最终 residual 检查仍保留为第二道防线。没有赋值的 `accessToken` 字样和 `token_count=42` 不触发；CLI 报告目录这类原始敏感路径也按相同 fail-closed 策略隐藏。
- 新增 CRLF/LF、`:=`/`: =`/单独 `:` 和 `=`、裸/quoted/backticked key、quoted/object/array value、`***evil`、多个字段以及 ProviderError 到内存 result、JSON/Markdown 报告和 CLI 的四边界回归。

| 修复验证项 | 实际命令 | 结果 |
|---|---|---|
| 最新 P1 及既有专项 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` | PASS，68 项。 |
| 桌面端完整回归 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q` | PASS，144 项；仅有既有 ZIP 同名 `manifest.json` 警告。 |
| 统一验证 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS：锁定依赖、Ruff、23 个 schema fixtures、桌面端 144 项及 Android lint/JVM/Debug APK 均通过。 |

本实现完成后应重新进入 `REVIEW`，只等待新的独立审查；本实现者不自审、不验收且不启动后续任务。

## 转义 JSON preflight 视图 P1 修复（2026-08-05）

本次仅修复最新独立复审指出的转义 JSON 文本可绕过原始文本 preflight 的问题；没有执行真实 Provider、登录或网络探测。

- 原始输出文本从不被检测视图替换或规范化。完整合法 JSON 仍先解析并递归脱敏；其余文本的原始 preflight 会额外检查至多 4 个转义检测视图。
- 每一层仅线性剥离 `\\`、反斜杠转义的双/单引号和反引号，并解码可打印 ASCII 范围（`U+0020`–`U+007E`）的 `\uXXXX`。不使用 `unicode_escape`，不解码任意控制字符；不完整或非十六进制 `\u` 转义保守返回固定 fail-closed 占位。
- 检测视图输入上限为 512 KiB；超过上限而含上述转义标记的文本无法证明安全，直接 fail-closed。层数和输入界限均明确，过程没有递归或可能逃逸的解析异常。
- 覆盖 1–4 层转义 JSON、`access\u0054oken`、敏感值部分 Unicode 转义、原生与转义混合多字段、对象/数组、前后日志、malformed escape、100 KiB 性能和 ProviderError→result→JSON/Markdown/CLI 四边界。完整转义的安全 `***` 按明确策略继续传递原文。

| 修复验证项 | 实际命令 | 结果 |
|---|---|---|
| 最新 P1 及既有专项 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` | PASS，77 项。 |
| 桌面端完整回归 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q` | PASS，153 项；仅有既有 ZIP 同名 `manifest.json` 警告。 |
| 统一验证 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS：锁定依赖、Ruff、23 个 schema fixtures、桌面端 153 项及 Android lint/JVM/Debug APK 均通过。 |

本实现完成后应重新进入 `REVIEW`，只等待新的独立审查；本实现者不自审、不验收且不启动后续任务。

`git diff --check` 已通过。非测试源码、README、示例配置与交付索引的凭据特征扫描未发现实际凭据、私钥或 AWS 访问键；交付审查历史中唯一的 `review-only-value` 命中是既有合成复现值，不是有效凭据。

## 转义检测视图修复后的独立复审（2026-08-05，gpt-5.6-terra high）

结论：`CHANGES_REQUIRED`。

### P1：第五层及以上转义 JSON 绕过检测视图并泄露敏感值

- 位置：`desktop/src/market_monitor/providers/runner.py:160-187, 238-294`。
- 问题：`_has_unsafe_preflight_sensitive_assignment()` 只剥离四层转义视图；第 5 层仍保留反斜杠转义引号，不能被 `_has_unsafe_residual_sensitive_assignment()` 识别，且后续嵌入 JSON/字段规则同样不能处理。因此，超出明确层数的 credential-shaped JSON 不是 fail-closed，而是被原样输出。
- 复现：将 `{"accessToken":"BOUNDARY_ESCAPED_5"}` 连续按现有测试辅助函数转义五次，并作为 `gateway ` 前缀的自由文本传给 `redact_secrets()`；输出仍包含 `BOUNDARY_ESCAPED_5`。以同一合成值构造固定 Provider 的 `NETWORK` 错误后，`ProviderRunResult`、`provider-capabilities.json`、`provider-capabilities.md` 及 CLI `_emit()` stdout 四个边界均包含该值。
- 修复要求：超过四层或任何检测层数/预算无法安全处理时，必须输出固定 fail-closed 占位符，不能保留原始敏感文本；加入第 5 层及更深转义 JSON 的单测和 Provider→result/JSON/Markdown/CLI 全链回归。保留原始输出而只使用检测视图的原则可以继续，但不得成为泄露路径。

### P2：普通 Windows 路径被误判为 malformed `\u` 转义并整段隐藏

- 位置：`desktop/src/market_monitor/providers/runner.py:189-202`。
- 问题：`_has_malformed_preflight_unicode_escape()` 将任何 `\u` 都按 Unicode 转义处理。正常日志 `C:\users\<user>\normal\app.log` 中的 `\u` 后接 `s`，会被判为 malformed 并返回 `[redacted sensitive text]`，即使没有敏感键或值。该行为会不必要地抹除典型 Windows 路径和普通反斜杠日志，削弱 CLI/报告可观测性。
- 复现：`redact_secrets(r'log C:\users\<user>\normal\app.log plain \\server\\share')` 返回固定占位符；无凭据的 `r'normal \\u12ZZ no credentials'` 也同样整段隐藏。
- 修复要求：只将实际处于受支持转义 JSON 语境、且确实可能形成敏感赋值的 malformed Unicode escape 作为 fail-closed 条件，或采用能保留非敏感普通日志的等价策略；为 `C:\users`、UNC/普通反斜杠和无敏感 malformed `\u` 添加回归。若仍选择更保守的全段隐藏，须在用户可见 CLI/报告文档中明确其影响并证明不造成常规路径日志不可用。

### 本轮复核证据

- 1–4 层转义 JSON、ASCII `\uXXXX` 敏感 key/value、原生与转义混合、数组/对象、前后日志、`***evil`、超过 512 KiB 带转义内容，以及 Provider 四输出边界都已复跑；前述范围未发现泄露。超过 512 KiB 即使为无敏感转义安全文本也会保守 fail-closed。
- 专项回归：`desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` 通过（77 项），但没有覆盖第五层转义或上述 Windows 路径反例。
- 统一验证：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` 通过：Python 3.11.0、50 个精确锁定依赖、Ruff、23 个共享 Schema fixture、桌面端测试、Android `lintDebug`/JVM/Debug APK；仅有既有 ZIP 同名 `manifest.json` 警告。
- `git diff --check` 通过；非测试源码与普通文档的凭据特征扫描未发现实际凭据、私钥或 AWS 访问键。上述 `BOUNDARY_ESCAPED_5` 为合成反例，不是本机凭据。

本复审未实施修复、不验收且未启动后续任务。`FULL-101` 已更新为 `CHANGES_REQUIRED`；实现者只能修复以上 P1/P2 后重新进入独立审查。

## 转义层数边界与反斜杠日志 P1/P2 修复（2026-08-05）

本次仅修复最新独立复审指出的第五层以上转义泄露和普通反斜杠日志误判；没有执行真实 Provider、登录或网络探测。

- 正常检测预算仍是 4 个线性转义视图。完成预算后只生成一次额外 probe view，不做无限解码；若 probe 仍有受支持的转义变化，当前或 probe 视图中忽略反斜杠、单双引号和反引号包装后的敏感键候选、未安全残余赋值或已注册 secret 都会使原始输出固定为 `[redacted sensitive text]`。
- 边界探测也对一次 ASCII Unicode 归一后的候选生效，因此 5、6、10 层受支持转义的敏感 JSON 不会因超过预算而原样泄露；没有候选的普通更深转义文本不会因此无限展开。
- 不再将任何不完整或非十六进制 `\u` 视为全局 fail-closed 条件。只解码完整、可打印 ASCII `\uXXXX`；普通 `C:\users\<user>` 路径、UNC 路径、普通反斜杠日志及无敏感字段的 `\u12ZZ` 保留原文。畸形转义与显式敏感 key 赋值或经候选归一后形成敏感 key 共存时仍 fail-closed。
- 新增 5/6/10 层、10 层 Provider→result→JSON/Markdown/CLI 四边界、普通路径/UNC/畸形 Unicode 保留及畸形转义与敏感赋值共存反例。

| 修复验证项 | 实际命令 | 结果 |
|---|---|---|
| 最新 P1/P2 及既有专项 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` | PASS，83 项。 |
| 桌面端完整回归 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q` | PASS，159 项；仅有既有 ZIP 同名 `manifest.json` 警告。 |
| 统一验证 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS：锁定依赖、Ruff、23 个 schema fixtures、桌面端 159 项及 Android lint/JVM/Debug APK 均通过。 |

本实现完成后应重新进入 `REVIEW`，只等待新的独立审查；本实现者不自审、不验收且不启动后续任务。

`git diff --check` 已通过。非测试源码、README、示例配置与交付索引的凭据特征扫描未发现实际凭据、私钥或 AWS 访问键；交付审查历史中唯一的 `review-only-value` 命中是既有合成复现值，不是有效凭据。

## 转义层数边界修复后的独立复审（2026-08-05，gpt-5.6-terra high）

结论：`CHANGES_REQUIRED`。

### P1：多层转义叠加 ASCII Unicode 敏感键仍可越过单次边界 probe

- 位置：`desktop/src/market_monitor/providers/runner.py:178-201, 220-302`。
- 问题：实现只剥离四层视图并额外生成一次 probe。对于键名本身含可打印 ASCII `\uXXXX` 的转义 JSON，五次处理后仍可能保留足以阻止候选归一化的反斜杠层；`_contains_sensitive_key_candidate()` 因而没有识别该 key。第六层和第十层的完整 Unicode key 或部分 Unicode key 都会原样越过 preflight，后续字段/嵌入 JSON 替换同样不能覆盖这种文本。
- 复现：以合成值构造 `{"access\u0054oken":"TERRA_UNICODE_ESCAPE_<N>"}`，再用当前测试辅助函数逐层转义。`N=5` 返回固定占位符，但 `N=6` 与 `N=10` 均保留合成值；完整 Unicode key（`\u0061...\u006e`）的相同 6/10 层变体也泄露。将 `N=6`、`N=10` 文本作为固定 `NETWORK` `ProviderError` 后，内存 `ProviderRunResult`、`provider-capabilities.json`、`provider-capabilities.md` 和 CLI `_emit()` stdout 四个输出边界均包含合成值。
- 影响：这违反 FULL-101 的“密钥不入日志、报告或 CLI 输出”边界，属于 P1。仅用敏感键候选做一次额外 probe 不能保证任意奇偶层数与 Unicode 转义的组合被 fail-closed。
- 修复要求：将超过已验证预算、或仍含转义包装且无法证明安全的 credential-shaped key/value 统一 fail-closed；不要把单次 probe 当作多层 Unicode 组合的安全证明。新增完整/部分 Unicode key 及 Unicode value 与 5/6/10 层交叉测试，并覆盖 Provider result、JSON、Markdown、CLI 四个边界；至少覆盖能在 512 KiB 以内表达的偶/奇层数。保持正常 Windows 路径、UNC、无敏感 malformed `\u` 和完整 `***` 哨兵的可观测性。

### 已复核的通过项与证据

- 5/6/10/20 层纯转义 JSON、普通反斜杠、注册短值和 `***` 变体专项中，纯转义敏感 key 不泄露；实际 20 层输入约 4.19 MiB，在 1.09 秒内因 512 KiB 预算受控返回固定占位符。100 层逐次完整序列化需要指数级文本，无法作为可物化的输入；现有 512 KiB 边界对任何可接收的超长转义文本 fail-closed，但不能替代上述 6/10 层可物化绕过的修复。
- 正常 `C:\users\<user>\normal\app.log`、UNC `\\server\share\logs`、普通反斜杠与无敏感 `\u12ZZ` 均保持原文；带敏感候选的 malformed Unicode、`accessToken=***evil` 和已注册 `xy` 均受控隐藏。ASCII Unicode value-only 变体因原生敏感 key 存在而隐藏。
- 专项命令 `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` 通过（83 项），但未覆盖本 P1 的多层 Unicode-key 组合。
- 完整命令 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` 通过：Python 3.11.0、50 个精确锁定依赖、Ruff、23 个 Schema fixture、159 个桌面测试和 Android `lintDebug`/JVM/Debug APK 均通过；仅有既有 ZIP 同名 `manifest.json` 警告。
- `git diff --check` 通过；对非测试源码与普通文档的凭据特征扫描未发现实际凭据、私钥或 AWS 访问键。本文所有 `TERRA_*` 均为合成审查值。

本复审未实施修复、未验收且未启动后续任务。`FULL-101` 已更新为 `CHANGES_REQUIRED`；仅可修复上述 P1 后重新进入独立审查。

## 层数无关转义归一化 P1 修复（2026-08-05）

本次仅修复最新独立复审指出的多层转义叠加 Unicode 敏感键可越过单次边界 probe 的问题；没有执行真实 Provider、登录或网络探测。

- 检测只使用归一化副本，原文从不被改写。归一化视图一次折叠任意长度的反斜杠串（无论奇偶层数），并解码可打印 ASCII（`U+0020`–`U+007E`）的 `\uXXXX`；不再把“4 层预算 + 单次 probe”当作任意多层 Unicode 组合的安全证明。
- 对归一化视图执行与原文相同的 residual preflight；完成固定轮次后若视图仍含受支持转义且同时存在敏感键候选、未安全残余赋值或已注册 secret，原始输出统一 fail-closed 为 `[redacted sensitive text]`。候选检测自身也解码可打印 ASCII `\uXXXX` 并忽略反斜杠/引号包装，因此完整或部分 Unicode 编码的 `accessToken` 等键、Unicode 冒号/等号分隔符及 Unicode 值无需无限展开即可被识别；只有与敏感候选共存的 malformed `\u` 才 fail-closed。
- 正常 Windows 路径（`C:\users\...`）、UNC 路径、普通反斜杠日志和无敏感 malformed `\u` 保持原文；完整转义的 `***` 哨兵继续原样传递，`***evil` 等变体仍 fail-closed。
- 新增 6/10/20/100 层完整/部分 Unicode key、Unicode 分隔符、Unicode 编码反斜杠嵌套、奇/偶反斜杠串、100 层安全哨兵与 100 KB 前缀的性能/四边界回归，并补充按审查辅助函数逐层转义（`_escape_quotes_once`）与完整/部分 Unicode key 的 5/6/10 层交叉反例；全部覆盖 Provider result、JSON 报告、Markdown 报告和 CLI stdout。

| 修复验证项 | 实际命令 | 结果 |
|---|---|---|
| 最新 P1 及既有专项 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` | PASS，104 项。 |
| 桌面端完整回归 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q` | PASS，180 项；仅有既有 ZIP 同名 `manifest.json` 警告。 |
| 静态检查 | `desktop\.venv\Scripts\python.exe -m ruff check desktop\src desktop\tests` | PASS。 |
| 统一验证 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS：Python 3.11.0、50 个精确锁定依赖、Ruff、23 个共享 schema fixtures 与桌面端 180 项（合计 203 项）及 Android `lintDebug`/JVM/Debug APK 均通过；仅有既有 ZIP 重名 `manifest.json` 警告。 |

本实现完成后已将 `FULL-101` 置为 `REVIEW`。只等待新的独立审查；本实现者不自审、不验收且不启动后续任务。

`git diff --check` 已通过。非测试源码、README、示例配置与交付索引的凭据特征扫描未发现实际凭据、私钥或 AWS 访问键；本文所有 `TERRA_*`、`REVIEW_*` 均为合成审查值。

### P2 畸形 `\u` 赋值判定补修（2026-08-05）

独立复审的 P2 精确复现 `redact_secrets(r'normal \u12ZZ no credentials')` 此前仍返回固定占位符：原实现的畸形 `\u` 分支把独立单词 `credentials` 也当作敏感键候选，即使其后没有任何 `:`/`=` 赋值也 fail-closed。本轮补修将其改为“归一化后仍存在真实敏感赋值才 fail-closed”：

- 新增检测专用 `_normalize_malformed_preflight_unicode_escapes(text)`：对反斜杠串后跟 `u` 且其后 4 个字符不全为十六进制（或不足 4 个）的畸形序列，连同其前导反斜杠串整体从检测视图中移除；合法 `\uXXXX` 保持不变。检测副本只用于判定，原文从不改写。
- `_has_unsafe_preflight_sensitive_assignment` 的畸形 `\u` 分支改为：先取已折叠视图（最多 4 轮），再做畸形归一化，最后对该归一化视图执行 `_has_unsafe_residual_sensitive_assignment`；只有归一化后敏感 key 后仍跟 `:`/`=` 且值不是完整 `***` 哨兵时才 fail-closed。
- 保留既有保守策略：超过 512 KiB 的文本含敏感候选仍 fail-closed；受支持转义（合法 `\uXXXX` 或引号/反引号转义）+ 敏感候选仍 fail-closed。

行为矩阵（逐条实跑通过）：

| 输入 | 输出 |
|---|---|
| `normal \u12ZZ no credentials` | 原样保留 |
| `ordinary \u12ZZ log`、`C:\users\<user>`、`\\server\share\logs`、`plain \\ backlog` | 原样保留 |
| `access\u12ZZToken=REVIEW_MALFORMED_VALUE` | `[redacted sensitive text]` |
| `gateway {\"access\u12ZZToken\":\"REVIEW_MALFORMED_KEY\"}` | `[redacted sensitive text]` |
| 5 层 `_escape_quotes_once` 转义 + `{"access\u12ZZToken":"REVIEW_MALFORMED_LAYER5"}` | `[redacted sensitive text]` |
| 6/10/20/100 层合法转义 + 完整/部分 Unicode 敏感键（既有 P1 回归） | `[redacted sensitive text]` |

| 补修验证项 | 实际命令 | 结果 |
|---|---|---|
| 最新专项（含新增 `test_malformed_unicode_at_fifth_escape_layer_fails_closed` 与精确复现断言） | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` | PASS，105 项。 |
| 桌面端完整回归 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q` | PASS，181 项；仅有既有 ZIP 同名 `manifest.json` 警告。 |
| 静态检查 | `desktop\.venv\Scripts\python.exe -m ruff check desktop\src desktop\tests` | PASS。 |
| 统一验证 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS：Python 3.11.0、JDK 21.0.11、50 个精确锁定依赖、Ruff、23 个共享 schema fixtures 与桌面端 181 项（合计 204 项）及 Android `lintDebug`/JVM/Debug APK 均通过；仅有既有 ZIP 重名 `manifest.json` 警告。 |

本轮仅修改 `desktop/src/market_monitor/providers/runner.py`、`desktop/tests/test_provider_runner.py` 与本文档；未执行真实 Provider、登录或网络探测（留给后续独立真实探针任务）。`FULL-101` 保持 `REVIEW`，等待新的独立审查。

## 层数无关与畸形 \u 修复后的新独立复审（2026-08-05，gpt-5.6-terra high）

结论：`CHANGES_REQUIRED`。

### P1：完整合法 JSON 中两层及以上转义的敏感键绕过层数无关归一化

- 位置：`desktop/src/market_monitor/providers/runner.py:113-158`（`_redact_text` 先调用 `_redact_complete_json` 并在成功时直接返回；`_redact_complete_json` 只按 JSON 解码一次的键名判断敏感性，从不进入层数无关 preflight）。
- 问题：当整个消息是合法 JSON 且敏感键经过两层或更多转义时，键在 `json.loads()` 后只是字面量 `\u0061\u0063...` 文本（再解码一次才成为 `accessToken`），`_is_sensitive_key` 不识别该键，值原样保留。这直接绕过“层数无关转义归一化”声称覆盖的 5/6/10 层场景，违反 FULL-101 的“密钥不入日志、报告或 CLI 输出”边界。
- 复现（合成值）：`redact_secrets('{"\\u0061\\u0063\\u0063\\u0065\\u0073\\u0073\\u0054\\u006F\\u006B\\u0065\\u006E":"REVIEW_DOUBLE_CLEAN"}')` 返回含 `REVIEW_DOUBLE_CLEAN` 的 JSON 文本。同一合成值作为固定 Provider 的 `NETWORK` `ProviderError` 后，内存 `ProviderRunResult`、`provider-capabilities.json`、`provider-capabilities.md` 与 CLI `_emit()` stdout 四个边界均包含该值。
- 修复要求：完整 JSON 路径也必须对键名应用与自由文本一致的层数无关归一化/敏感候选判定（或对解析后的键递归应用同一检测视图），并增加两层、三层转义键的完整 JSON 单测与 Provider→result/JSON/Markdown/CLI 四边界回归；无法安全证明的剩余转义键值继续 fail-closed。

### P1：全角冒号/等号与零宽/不可见分隔符绕过赋值检测

- 位置：`desktop/src/market_monitor/providers/runner.py:49-57`（`_EQUALS_ASSIGNMENT`/`_COLON_ASSIGNMENT` 只接受 ASCII `=`/`:`）、`:385-414`（`_read_assignment_value_start` 只接受 ASCII `:`/`=`，且 `isspace()` 不覆盖零宽空格与 BOM）、`:336-362`（残余检测同样依赖 ASCII 分隔符）。
- 问题：`accessToken：REVIEW_FW_COLON`（U+FF1A 全角冒号）、`accessToken＝REVIEW_FW_EQUALS`（U+FF1D 全角等号）、`accessToken\u200B=REVIEW_ZWSP`（U+200B 零宽空格）、`accessToken\uFEFF=REVIEW_FEFF`（U+FEFF）均不被识别为敏感赋值，合成值在四个输出边界原样出现。全角标点在中文 SDK/日志中是常规写法，零宽字符可作为日志注入手段；这违反“Unicode 冒号/等号分隔符”覆盖与 fail-closed 输出边界。
- 复现：上述四个输入分别经固定 Provider `NETWORK` 错误进入 `ProbeRunner`，`ProviderRunResult`、JSON 报告、Markdown 报告和 CLI stdout 均保留合成值。
- 修复要求：将全角 `：`/`＝`（及 `:`/`=` 的常见 Unicode 变体）归一化后再做赋值检测，或把零宽格式字符视为分隔/包装处理；为全角分隔符、ZWSP、FEFF 及四边界添加反例测试并断言合成值不出现。保留非敏感中文文本（如 `账户：普通文本`）的可观测性。

### P2：全角同形键绕过键名归一化

- 位置：`desktop/src/market_monitor/providers/runner.py:102-110`（`_normalise_key` 保留全角字母，`_is_sensitive_key` 无法识别 `ａｃｃｅｓｓＴｏｋｅｎ`）。
- 复现：`redact_secrets('ａｃｃｅｓｓＴｏｋｅｎ=REVIEW_FW_KEY')` 原样保留值；四个输出边界均泄露。
- 修复要求：键名归一化时对全角 ASCII 变体做 NFKC/兼容归一化（或显式映射 U+FF01–U+FF5E 到 ASCII），并添加全角键单测与四边界回归。

### P2：XML 元素文本形式的凭据无检测

- 位置：`desktop/src/market_monitor/providers/runner.py:336-414`（残余检测只处理 `key :=/: /= value` 赋值形态；`_JSON_FIELD`、`_MARKDOWN_INLINE_FIELD` 只覆盖 JSON/Markdown）。
- 复现：`redact_secrets('<password>REVIEW_XML_PASSWORD</password>')` 与 `<token>REVIEW_XML_TOKEN</token>` 原样保留；四个输出边界均泄露。SOAP/XML 响应体是 SDK 错误文本的现实序列化之一。
- 修复要求：对 `<敏感键>值</敏感键>` 元素内容及常见 XML 属性形态增加脱敏或 fail-closed，并添加单测与四边界回归；若判定超出 FULL-101 范围，须在交付记录中明确排除并说明理由。

### P2：带内空格的引号键绕过键读取

- 位置：`desktop/src/market_monitor/providers/runner.py:364-383`（`_read_assignment_key` 对引号键要求内容首字符为字母，`" accessToken "` 不满足；后续所有规则均无法匹配）。
- 复现：`redact_secrets('" accessToken "=REVIEW_QSPACE')` 原样保留值；四个输出边界均泄露。
- 修复要求：读取引号键时剥离外层引号后按归一化键名判断敏感性，键内容含空白时应视为非标准输入并 fail-closed 或正确脱敏，并添加对应单测与四边界回归。

### 本轮复核证据

- 历史反例全部复跑通过：5/6/10 层逐层转义 + 部分/完整 Unicode 键、6/10/20/100 层反斜杠串、CRLF `:=`、malformed `\u` 与敏感赋值共存、`***`/`***evil` 哨兵、Windows/UNC 路径、注册短值等均按预期 fail-closed 或保留原文。
- 专项：`desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` → PASS（105 项），但未覆盖上述新反例。
- 桌面全量：`desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q` → PASS（181 项；仅有既有 ZIP 同名 `manifest.json` 警告）。
- 统一验证：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` → PASS（Python 3.11.0、JDK 21.0.11、50 个精确锁定依赖、Ruff、23 个共享 schema fixtures、桌面 181 项、Android `lintDebug`/JVM/Debug APK）。
- `desktop\.venv\Scripts\python.exe -m ruff check desktop\src desktop\tests` → PASS；`git diff --check` 通过。
- 本文所有 `REVIEW_*` 均为合成审查值，未读取、写入或回显任何真实本机凭据。

本复审未修改实现、未验收且未启动后续任务。`FULL-101` 已更新为 `CHANGES_REQUIRED`；实现者只能修复以上 P1/P2 后重新进入独立审查。

## terra7 P1/P2 全部泄露面修复（2026-08-06）

按用户新指示（先完成 5.1–5.9 整个系列再统一审查），本轮由 root 编排者承担实现角色，修复 `full101_review_terra7` 列出的 5 个 P1 与 1 个 P2；只改 `desktop/src/market_monitor/providers/runner.py` 与 `desktop/tests/test_provider_runner.py`，未执行真实 Provider、登录或网络探测。

### 修复内容

- **P1-1 URL userinfo 同形冒号**：`_URL_USERINFO_VIEW` 与 `_URL_CREDENTIALS` 已同源覆盖 U+2236/U+A789/U+02D0/U+1361/全角冒号；`&ratio;`、`&#x2236;` 经解码视图 fail-closed，字面同形冒号由 URL 凭据替换为 `***:***`。新增 6 个自由文本回归。
- **P1-2 字面 Cf 格式字符**：`_SEPARATOR_PADDING`/`_is_ignorable_between`/`_read_assignment_key`/`_read_assignment_value_start` 已覆盖 U+00AD、U+200E/U+200F、U+202A–202E、U+2061–2064、U+034F、U+061C 等，插入键与分隔符之间或复合键内均 fail-closed；完整 JSON 值内同样生效。新增分隔符、键内与完整 JSON 三组回归。
- **P1-3 键归一化缺口**：`_normalise_key` 拆分为 strict/loose 两视图。strict 保留 ASCII 数字并按尾部数字剥离（修复 `password1`、`accessToken\U00110000` 视图 `accesstoken0000` 的既有回归）；loose 应用 leet 数字/符号映射（`passw0rd`/`p4ssword`/`t0ken`/`p@ssword`）；NFKC+同形映射+NFKD+组合标记剥离+非 ASCII 数字转 ASCII 覆盖 `pässword`/`password١`；对含非 ASCII 字母的非纯 CJK 键增加与敏感词表的有界 Levenshtein（≤2）近匹配，`passwérd`/`paшword`/`pas𝐡ord` 全部 fail-closed。键读取与候选扫描同时允许 `@`。
- **P1-4 auth/key 复合键与认证头**：`_SENSITIVE_KEY_PARTS`/`_SENSITIVE_SUFFIXES` 已含 `auth`/`authkey`/`consumerkey`/`sessionkey`/`masterkey`/`signingkey`/`encryptionkey`；`X-Auth`、`X-Auth-Key` 经归一化 `xauth`/`xauthkey` 命中。新增 9 个赋值/头部回归。
- **P1-5 Bearer/Basic/Digest 字面不可见分隔符**：`_AUTH_SCHEME_TOKEN` 分隔符集合已含全部 Cf 格式字符；`Bearer<ZWSP>`、`Bearer<LRM>`、`Basic<ZWSP>`、`Digest<软连字符>`、`Bearer<CGJ>` 均替换为 `<scheme> ***`。新增 5 个回归。
- **P2-1 中文凭据键**：`_CJK_CREDENTIAL_MAP` 将 密码/口令→password、密钥→secret、令牌→token、私钥→privatekey，`密码=`、`密钥：`、`令牌：`、`口令=`、`私钥=` 全部 fail-closed；控制组 `账户：普通文本` 保持原样。
- **控制组**：`token_count=42`、`user_profile.name=alice`、`<name>alice</name>`、`账户：普通文本` 保持原样；URL 同形冒号回归中主机名标记允许保留（非凭据），凭据段 `user`/`pass` 必须消失。

### 修复验证

| 验证项 | 实际命令 | 结果 |
|---|---|---|
| terra7 专项（含新增 24 个反例函数/参数化用例，覆盖 6 类泄露面与四边界端到端） | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` | PASS |
| 桌面端完整回归 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q` | PASS；仅有既有 ZIP 同名 `manifest.json` 警告 |
| 静态检查 | `desktop\.venv\Scripts\python.exe -m ruff check desktop\src desktop\tests` | PASS |
| 统一验证 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS：Python 3.11.0、JDK 21.0.11、50 个锁定依赖、Ruff、23 个共享 Schema 夹具、桌面全量、Android `lintDebug`/`testDebugUnitTest`/`assembleDebug` |
| 变更检查 | `git diff --check` | PASS |

四边界端到端回归把 `&ratio;` URL、LRM 分隔符、leet 键、authkey、ZWSP Bearer 与中文密码键组合为固定 Provider 的 `NETWORK` 错误，断言内存 `ProviderRunResult`、机器 JSON 报告、人类 Markdown 报告与 CLI stdout 均无合成值。

按用户指示，本轮修复后 `FULL-101` 进入 `REVIEW` 但独立审查统一推迟到 5.1–5.9 全部实现完成后执行；实现者不自审、不验收、不伪造通过。

## 大文本正则回溯性能修复（2026-08-06）

并行实现 Agent 在桌面全量回归中发现两个大文本测试超时（约 222 秒）。cProfile 定位为脱敏预检中的 URL userinfo 正则 `_URL_USERINFO_VIEW`/`_URL_USERINFO_REMAINING` 在 10 万字符连续字母文本上发生 O(n²) 回溯。本轮由 root 修复（只改 `desktop/src/market_monitor/providers/runner.py`）：

- `_has_unsafe_url_userinfo` 改为线性扫描：按 `://` 与 `@` 分段，检查段内冒号族字符且无空白/斜杠；不再依赖回溯正则。
- `_has_encoded_url_userinfo` 改为线性扫描：按 `://` 后首个空白/`/`/`@` 截取 token，检查编码标记（`%HH`/`%u`/数字与命名实体/`\u`/`\x`/`\U`），与旧 `_URL_USERINFO_REMAINING` 语义一致（`https://example.com/a%3Db/page` 仍保留）。
- `_URL_CREDENTIALS.sub` 与两个赋值正则增加线性预过滤（无对应分隔符或 URL userinfo 形态时跳过），防止同类回溯面。

验证：`test_layer_independent_normalization_is_bounded_on_large_deeply_escaped_text_and_protects_four_boundaries` 由 216.8 秒降至 0.96 秒；脱敏专项 163 项与桌面全量回归全部通过；Ruff 通过。URL 同形冒号、深层编码、`%3D` 路径保留等历史反例与控制组行为不变。

### 编码分隔符/复合键/Bearer 编码修复（2026-08-05）

第四轮独立复审（结论 CHANGES_REQUIRED）发现 3 个 P1 与 1 个 P3；本轮由 root 编排者承担实现角色，全部修复（仅改 `desktop/src/market_monitor/providers/runner.py` 与 `desktop/tests/test_provider_runner.py`）：

- Bearer 编码分隔符：`_BEARER_TOKEN` 扩展为同时识别 `%20`/`%09`/`%0A`/`%0D`、字面 `\u0020`/`\u0009`/`\u000a`/`\u000d`、`\x20`/`\x09`/`\x0a`/`\x0d`、`\U00000020` 及 HTML 实体分隔符；`Bearer\u0020…`、`Bearer%20…` 与完整 JSON 值内变体均被替换为 `Bearer ***`。
- 复合敏感键：`_read_assignment_key` 的裸键读取扩展为可跨越空白、点号、控制符与零宽字符（`user name`、`user.name`、`pass word`），引号键也允许点号；`_is_sensitive_key` 的 NFKC/同形归一化与之一致，`"user.name"=…` fail-closed。
- 编码分隔符：新增检测专用 `_decode_percent_escapes`（`%HH`）与 `_decode_html_entities`（`&equals;`、`&colon;`、`&#61;`、`&#x3D;` 等命名/数字实体）；`_collapse_preflight_escape_runs` 新增 `\xHH` 与 `\U00000000` 解码。preflight 对“原文→折叠视图→百分号视图→实体视图”统一执行残余赋值检测，`accessToken%3D…`、`accessToken&equals;…`、`access\x54oken=…`、`access\U00000054oken=…` 全部 fail-closed。
- 既有“完全转义安全哨兵”语义保持：`gateway {\"accessToken\":\"***\"} trailing` 与 100 层 `***` 哨兵仍原样传递；`Bearer token goes here` 这类句子按既有保守策略将 Bearer 后 token 替换为 `***`（与修复前一致，非本轮回归）。

修复后行为矩阵（逐条实跑通过）：第四轮 13 个反例全部不再出现 `REVIEW_*` 合成值；`token_count=42`、`user_profile.name=alice`、`<name>alice</name>`、`https://example.com/a%3Db/page`、Windows/UNC 路径与 `normal \u12ZZ no credentials` 等非敏感文本保持原样；全部历史反例（1–10/20/100 层转义、CRLF `:=`、malformed `\u`、全角/零宽分隔符、XML、`***` 哨兵等）仍按预期。

| 修复验证项 | 实际命令 | 结果 |
|---|---|---|
| 最新专项（含 3 个新增回归测试函数；`--collect-only` 实测 123 项） | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` | PASS，123 项。 |
| 桌面端完整回归 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q` | PASS，199 项；仅有既有 ZIP 同名 `manifest.json` 警告。 |
| 静态检查 | `desktop\.venv\Scripts\python.exe -m ruff check desktop\src desktop\tests` | PASS。 |
| 统一验证 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS：Python 3.11.0、JDK 21.0.11、50 个精确锁定依赖、Ruff、23 个共享 schema fixtures 与桌面端 199 项（合计 222 项）及 Android `lintDebug`/JVM/Debug APK 均通过；仅有既有 ZIP 重名 `manifest.json` 警告。 |

计数更正：第三轮记录“专项 128 项”有误；`--collect-only` 实测为 120 项（第四轮复审确认），本轮新增 3 项后为 123 项。历史文档中的 122/128 均为先前轮次误记，以每次 `--collect-only` 实测为准。

本轮未执行真实 Provider、登录或网络探测（留给后续独立真实探针任务）。`FULL-101` 已重新置为 `REVIEW`，等待全新的独立审查；本实现者不自审、不验收且不启动后续任务。

### 解码预算耗尽/实体扩充/冒号同形/同形键扩充/独立认证方案值修复（2026-08-05）

第六轮独立复审（结论 CHANGES_REQUIRED）发现 4 个 P1 与 1 个 P2；本轮由 root 编排者承担实现角色，全部修复（仅改 `desktop/src/market_monitor/providers/runner.py` 与对应测试）：

- 组合解码预算耗尽：新增 `_REMAINING_ENCODING` 与 `_has_unsafe_remaining_encoding`——当 4 轮解码预算耗尽后视图仍含有效编码（`%HH`/`%uHHHH`、数字/命名实体、`\u`/`\x`/`\U`）且同时存在敏感键候选、认证方案、URL userinfo 或部分编码 userinfo 时统一 fail-closed；`Bearer%2525255Cu0020…`、更深层 `%25` 嵌套与多层编码 URL userinfo 均不再泄露。认证方案视图 padding 同时接受 `%`/`&`，覆盖预算耗尽后残留标记的 `Bearer%% …` 形态。
- HTML 命名实体扩充：`&bsol;`（反斜杠）、`&ratio;`（U+2236）、`&Colon;`、`&period;`、`&grave;`、`&comma;`/`&semi;`/`&lsqb;`/`&rsqb;`/`&lcub;`/`&rcub;`/`&lpar;`/`&rpar;` 加入解码表；Bearer、冒号赋值、复合键与反引号键的实体变体不再绕过。
- 冒号同形分隔符：`U+A789`、`U+02D0`、`U+1361` 加入 `_COLON_ASSIGNMENT` 与 `_read_assignment_value_start`。
- 同形键映射扩充：希腊 `α→a`、`ε→e`、`τ→t`、`σ/ς→s` 与拉丁扩展 `ɑ→a` 加入 `_HOMOGLYPH_MAP`，`pαssword`、`pɑssword`、`tokεn`、`τoken` 均识别为敏感键。
- 独立认证方案值（P2）：`_AUTH_SCHEME_TOKEN` 替换原 `_BEARER_TOKEN`，`Bearer`/`Basic`/`Digest` 前缀后的凭据值（含常见编码分隔符）统一替换为 `<scheme> ***`；无头上下文的独立 `Basic <base64>`、`Digest <value>` 不再泄露。

修复后行为矩阵（逐条实跑通过）：第六轮 16 个反例全部不再出现 `REVIEW_*` 合成值；`normal \u12ZZ no credentials`、Windows/UNC 路径、`token_count=42`、`user_profile.name=alice`、`<name>alice</name>`、`https://example.com/a%3Db/page`、完全转义 `***` 哨兵、原文 `Bearer token goes here`（`Bearer *** goes here`）与 `https://user:pass@host/path`（`https://***:***@host/path`）保持既有行为；1–10/20/100 层转义、CRLF `:=`、malformed `\u`、全角/零宽分隔符、XML、组合编码、URL userinfo、复合认证头等历史反例仍按预期。 （本行中的凭据均为合成示例。）

| 修复验证项 | 实际命令 | 结果 |
|---|---|---|
| 最新专项（新增 5 个回归测试函数；`--collect-only` 实测 132 项） | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` | PASS，132 项。 |
| 桌面端完整回归 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q` | PASS，208 项；仅有既有 ZIP 同名 `manifest.json` 警告。 |
| 静态检查 | `desktop\.venv\Scripts\python.exe -m ruff check desktop\src desktop\tests` | PASS。 |
| 统一验证 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS：Python 3.11.0、JDK 21.0.11、50 个精确锁定依赖、Ruff、23 个共享 schema fixtures 与桌面端 208 项（合计 231 项）及 Android `lintDebug`/JVM/Debug APK 均通过；仅有既有 ZIP 重名 `manifest.json` 警告。 |

本轮未执行真实 Provider、登录或网络探测（留给后续独立真实探针任务）。`FULL-101` 已重新置为 `REVIEW`，等待全新的独立审查；本实现者不自审、不验收且不启动后续任务。

### 组合编码/URL userinfo/复合认证头/配置注册修复（2026-08-05）

第五轮独立复审（结论 CHANGES_REQUIRED）发现 3 个 P1 与 1 个 P2；本轮由 root 编排者承担实现角色，全部修复（仅改 `desktop/src/market_monitor/providers/runner.py`、`desktop/src/market_monitor/configuration.py` 与对应测试）：

- 组合编码分隔符：preflight 改为迭代解码管线（每轮依次做反斜杠转义折叠 → `%HH`/`%uHHHH` 百分号解码 → 命名/数字 HTML 实体解码，最多 4 轮），并对每个不同于原文的中间视图执行残余赋值检测、注册值检测、认证方案检测与 URL userinfo 检测。`Bearer%5Cu0020…`、`Bearer&#92;u0020…`、`Bearer%5Cx20…`、`Bearer%u0020…`、`accessToken%5Cu0020=…`、`accessToken\U0020=…`（大写 4 位 `\U`）全部 fail-closed；`\U00110000` 越界码点不崩溃（敏感键场景仍 fail-closed）。
- URL userinfo 编码分隔符：新增 `_has_unsafe_url_userinfo`，对解码视图匹配 `scheme://user:pass@`；`https://user%3Apass@host`、`https://user:pass%40host`、`https://user&#58;pass@host` 全部 fail-closed；未编码 URL 保持既有 `http://***:***@host` 替换行为，普通 URL 路径（含 `%3D`）不受影响。 （本行中的凭据均为合成示例。）
- 复合认证/Cookie 头：`_SENSITIVE_SUFFIXES` 新增 `authorization` 与 `cookie`，`Proxy-Authorization: Basic …`、`X-Authorization: Basic …`、`Set-Cookie: session=…` 全部 fail-closed；新增 `_has_unsafe_auth_scheme` 对解码视图识别 `Bearer`/`Basic`/`Digest` + token（仅视图与原文不同时生效，普通 `Bearer xyz` 保持既有替换语义）。
- 配置注册集合不一致（P2）：`configuration.py` 的 `_is_sensitive_configuration_name` 与 runner 键集合对齐（新增 `passwd`/`pwd`/`secretkey`/`accesskey`/AWS 键/`bearer`/`cookie`/`passphrase`/`refreshtoken` 等与后缀集合、尾部数字剥离），`LocalConfiguration.secret_values` 会注册这些配置值以便孤立值在错误消息中同样被脱敏。

修复后行为矩阵（逐条实跑通过）：第五轮 12 个反例全部不再出现 `REVIEW_*` 合成值；`Bearer token goes here`（原文）仍为 `Bearer *** goes here`、`https://user:pass@host/path` 仍为 `https://***:***@host/path`、`https://example.com/a%3Db/page` 与全部历史可用性用例保持原样；1–10/20/100 层转义、CRLF `:=`、malformed `\u`、全角/零宽分隔符、XML、`***` 哨兵等历史反例仍按预期。 （本行中的凭据均为合成示例。）

| 修复验证项 | 实际命令 | 结果 |
|---|---|---|
| 最新专项（新增 4 个回归测试函数；`--collect-only` 实测 127 项） | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` | PASS，127 项。 |
| 桌面端完整回归 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q` | PASS，203 项；仅有既有 ZIP 同名 `manifest.json` 警告。 |
| 静态检查 | `desktop\.venv\Scripts\python.exe -m ruff check desktop\src desktop\tests` | PASS。 |
| 统一验证 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS：Python 3.11.0、JDK 21.0.11、50 个精确锁定依赖、Ruff、23 个共享 schema fixtures 与桌面端 203 项（合计 226 项）及 Android `lintDebug`/JVM/Debug APK 均通过；仅有既有 ZIP 重名 `manifest.json` 警告。 |

本轮未执行真实 Provider、登录或网络探测（留给后续独立真实探针任务）。`FULL-101` 已重新置为 `REVIEW`，等待全新的独立审查；本实现者不自审、不验收且不启动后续任务。

## 独立复审（2026-08-05，gpt-5.6-terra high，full101_review_terra4）

结论：`CHANGES_REQUIRED`。历史反例全部复跑通过，但新独立审查发现 3 个 P1 泄露面与 1 个 P3 记录偏差；均可在 Provider 内存结果、JSON 报告、Markdown 报告与 CLI stdout 四个边界复现（全部使用 `REVIEW_*` 合成值，未触碰真实凭据）。

### P1：Bearer 前缀的分隔符编码绕过 Bearer 脱敏

- 位置：`desktop/src/market_monitor/providers/runner.py:84`（`_BEARER_TOKEN` 只匹配字面 `\s+`）、`:221`（替换只在原文本执行）、`:247-281`（`_has_unsafe_preflight_sensitive_assignment` 的 fail-closed 门要求归一化视图仍含可折叠转义，完全折叠/不含反斜杠时不再兜底）、`:419-445`（残余检测只认 `key=value` 赋值形态，不认 Bearer 前缀）。
- 问题：`Bearer\u0020…`、`Bearer\u0009…`、`Bearer%20…`（字面反斜杠、制表符转义、百分号编码空格）以及完整合法 JSON 值 `{"message":"Bearer\\u0020…"}`、二次转义 `{"message":"Bearer\\\\u0020…"}` 均不匹配 `_BEARER_TOKEN`；preflight 归一化视图已变成普通空格后，`_has_supported_preflight_escape(view)` 为 False，候选检查永不触发，值原样保留。这违背交付声称的 “Bearer 值”与“层数无关转义归一化”覆盖。
- 复现（合成值）：`redact_secrets(r'Bearer\u0020REVIEW_BEARER_TOKEN')`、`redact_secrets(r'Bearer\u0009REVIEW_BEARER_TAB')`、`redact_secrets('Bearer%20REVIEW_BEARER_PCT')`、`redact_secrets(r'{"message":"Bearer\\u0020REVIEW_JSON_BEARER"}')`、`redact_secrets(r'{"message":"Bearer\\\\u0020REVIEW_DOUBLE_BEARER"}')` 均原样返回；作为固定 Provider 的 `NETWORK` 错误后，内存 `ProviderRunResult`、`provider-capabilities.json`、`provider-capabilities.md` 与 CLI `_emit()` stdout 四个边界均包含合成值。
- 修复要求：Bearer 前缀采用与赋值检测一致的层数无关视图（至少覆盖 `\uXXXX`、`\xXX`、`\UXXXXXXXX`、`%XX`、HTML 实体），或对含合法转义/编码变体的 Bearer 形态统一 fail-closed；为自由文本与完整 JSON 值、四边界添加反例测试并断言合成值不出现。

### P1：敏感复合键被空白、点号或控制符拆分后绕过键归一化

- 位置：`desktop/src/market_monitor/providers/runner.py:447-469`（`_read_assignment_key` 只读连续 `[A-Za-z0-9_-]` 或引号键）、`:72-81`（`_EQUALS_ASSIGNMENT`/`_COLON_ASSIGNMENT` 键 token 不含空白/点号）、`:470-520`（`_read_assignment_value_start` 把空白/零宽/控制符只当作键与分隔符之间的填充）、`:328-377`（`_contains_sensitive_key_candidate` 同样不合并拆分键）；与 `:118-128`（`_normalise_key`/`_is_sensitive_key` 把 `user name`/`user.name` 归一化为 `username`、把 `pass word`/`pass.word` 归一化为 `password`）不一致。
- 问题：当敏感复合键被空白、点号、转义空格或控制符拆成若干段、且没有任何单段本身属于敏感集合时（`user`/`name`、`pass`/`word` 都不是敏感单段），赋值正则、候选扫描与残余检测全部失配，值原样保留。对照 `access token=…`/`client secret=…` 已 fail-closed（因为 `token`/`secret` 单段即敏感），可见缺口是复合键归一化而非单段敏感覆盖。
- 复现（合成值）：`redact_secrets(r'user\u0020name=REVIEW_SPLIT_NAME')`、`redact_secrets(r'pass\u0020word=REVIEW_SPLIT_PASS')`、`redact_secrets('user name=REVIEW_ACTUAL_SPACE')`、`redact_secrets('user.name=REVIEW_DOT_BARE')`、`redact_secrets('"user.name"=REVIEW_DOT_QUOTED')`、`redact_secrets(r'user\u0000name=REVIEW_NUL_SPLIT')` 均原样返回；四个输出边界均泄露。
- 修复要求：键读取后必须按与 `_is_sensitive_key` 相同的归一化规则（合并空白、点号、零宽/控制符、转义解码）判断敏感性，或对含内部空白/点号/转义码位的键名统一 fail-closed；添加四边界回归并保留 `user_profile.name=alice`、`token_count=42` 等既有非敏感文本。

### P1：百分号、HTML 实体与 `\x`/`\U` 编码分隔符绕过赋值检测

- 位置：`desktop/src/market_monitor/providers/runner.py:72-81`（赋值正则只接受字面 `=`/`:`/全角变体）、`:470-520`（`_read_assignment_value_start` 同）、`:379-412`（`_collapse_preflight_escape_runs` 只处理小写 `\uXXXX` 与引号/反引号）、`:328-377`（`_contains_sensitive_key_candidate` 同）。
- 问题：`accessToken%3D…`（URL 百分号编码等号）、`accessToken&equals;…`/`accessToken&#61;…`（HTML 命名/数字实体）、`access\x54oken=…`（`\x` 十六进制转义）、`access\U00000054oken=…`（`\U` 八位转义）均不被归一化、不被赋值正则匹配、不被残余检测识别，值原样保留。百分号编码直接否定交付声称的 “URL query” 覆盖；HTML 实体是 XML/HTML 错误体与现实 SDK 日志的常见序列化。
- 复现（合成值）：`redact_secrets(r'api?accessToken%3DREVIEW_PERCENT')`、`redact_secrets(r'accessToken&equals;REVIEW_HTML_ENTITY')`、`redact_secrets(r'accessToken&#61;REVIEW_HTML_NUM')`、`redact_secrets(r'access\x54oken=REVIEW_HEX')`、`redact_secrets(r'access\U00000054oken=REVIEW_U8')` 均原样返回；四个输出边界均泄露。
- 修复要求：赋值检测前对 `%XX`、HTML 实体（命名与数字）、`\x`、`\U`（及既有 `\u`）做层数无关归一化，或对含这些编码且带敏感键候选的文本统一 fail-closed；添加四边界反例测试。

### P3：交付记录与状态表中的专项测试数量与实际不符

- 位置：`docs/deliveries/FULL-101.md` 与 `STATUS.md`（“128 项专项”）。
- 实际复测：`desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -o addopts='' --collect-only -q` 收集 120 项；同集合实跑 120 passed。桌面全量 196 项与 `verify.ps1` 通过记录一致。
- 修复要求：把专项数量更新为实际收集/运行数（120），或补齐缺失的 8 项测试并更新记录。

### 本轮复核证据

- 专项实跑：`desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -o addopts='' -q --tb=no` → PASS（120 passed），未覆盖上述 P1 反例（对测试源码检索 `0020`/`%3D`/HTML 实体/`\U000000`/`\x5` 无命中）。
- 桌面全量：`desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q --tb=no` → PASS（196 passed；仅有既有 ZIP 同名 `manifest.json` 警告）。
- 统一验证：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` → PASS（Python 3.11.0、JDK 21.0.11、50 个精确锁定依赖、Ruff、23 个共享 schema fixtures、桌面 196 项、Android `lintDebug`/JVM/Debug APK）。
- 静态与差异检查：`desktop\.venv\Scripts\python.exe -m ruff check desktop\src desktop\tests` → PASS；`git diff --check` → PASS。
- 配置/CLI 抽查：缺 JQData 配置 `probe` → 退出码 3 且 `CONFIGURATION_BLOCKED`；未知 provider → 64；`--timeout-seconds 0` → 64；仓库内 junction、重复键、BOM、NUL 设备文件等历史反例由既有 120 项测试覆盖并通过。
- 本文所有 `REVIEW_*` 均为合成审查值，未读取、写入或回显任何真实本机凭据。

本复审未修改实现、未验收且未启动后续任务。`FULL-101` 已更新为 `CHANGES_REQUIRED`；实现者只能修复以上 P1 后重新进入独立审查。

### 全角/零宽/XML/多层转义键泄露修复（2026-08-05）

本轮由 root 编排者承担实现角色，针对最新独立复审的 8 个绕过面逐一修复（仅改 `desktop/src/market_monitor/providers/runner.py` 与 `desktop/tests/test_provider_runner.py`）：

- 完整/嵌入 JSON 的双层及以上转义敏感键：`_redact_value` 对 Mapping 键同时使用 `_is_sensitive_key` 与 `_contains_sensitive_key_candidate`，因此 `"\\u0061\\u0063..."` 这类解码两次才是 `accessToken` 的键也会把值替换为 `***`，输出仍为合法 JSON。
- 全角/零宽分隔符：`_EQUALS_ASSIGNMENT`/`_COLON_ASSIGNMENT` 允许零宽字符（`U+200B`、`U+200C`、`U+200D`、`U+FEFF`）并接受全角 `＝`/`：`；`_read_assignment_value_start` 同样跳过零宽字符并接受全角分隔符，`accessToken：X`、`accessToken＝X`、`accessToken\u200B=X`、`accessToken\uFEFF=X` 均 fail-closed。
- 全角同形键：`_normalise_key` 先做 NFKC 归一化再过滤与 casefold，`ａｃｃｅｓｓＴｏｋｅｎ=…` 归一化后等同 `accesstoken`，fail-closed。
- XML 元素文本：新增 `_XML_ELEMENT` 与 `redact_xml_element`，`<password>…</password>`、`<token …>…</token>`、带命名空间的敏感元素文本替换为 `***`；非敏感元素保持原样。
- 带内空格的引号键：`_read_assignment_key` 对引号/反引号键做空白与零宽字符剥离，`" accessToken "=…` fail-closed。

修复后行为矩阵（逐条实跑通过）：上述 8 个反例全部不再出现 `REVIEW_*` 合成值；`normal \u12ZZ no credentials`、Windows/UNC 路径、`ordinary \u12ZZ log` 等历史保留用例仍原样；1–10/20/100 层转义、CRLF `:=`、malformed `\u`、`***` 哨兵历史反例仍按预期 fail-closed 或保留。

| 修复验证项 | 实际命令 | 结果 |
|---|---|---|
| 最新专项（含 9 个新增回归测试） | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` | PASS，122 项。 |
| 桌面端完整回归 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q` | PASS，190 项；仅有既有 ZIP 同名 `manifest.json` 警告。 |
| 静态检查 | `desktop\.venv\Scripts\python.exe -m ruff check desktop\src desktop\tests` | PASS。 |
| 统一验证 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS：Python 3.11.0、JDK 21.0.11、50 个精确锁定依赖、Ruff、23 个共享 schema fixtures 与桌面端 190 项（合计 213 项）及 Android `lintDebug`/JVM/Debug APK 均通过；仅有既有 ZIP 重名 `manifest.json` 警告。 |

本轮未执行真实 Provider、登录或网络探测（留给后续独立真实探针任务）。`FULL-101` 已重新置为 `REVIEW`，等待全新的独立审查；本实现者不自审、不验收且不启动后续任务。

### 非可打印转义/常用凭据键/XML 结构/扩展分隔符修复（2026-08-05）

第三轮独立复审（结论 CHANGES_REQUIRED）发现 2 个 P1 与 3 个 P2 泄露面；本轮由 root 编排者承担实现角色，全部修复（仅改 `desktop/src/market_monitor/providers/runner.py` 与 `desktop/tests/test_provider_runner.py`）：

- P1 非可打印 `\uXXXX` 字面转义：`_collapse_preflight_escape_runs` 与 `_contains_sensitive_key_candidate` 现在解码任意合法 `\uXXXX`（不再限于可打印 ASCII），因此 `accessToken\u200b=…`、`accessToken\uFF1A=…`、`accessToken\u000a=…` 等字面转义以及完整 JSON 内二次转义文本均 fail-closed。
- P1 常用凭据键名：`_SENSITIVE_KEY_PARTS`/`_SENSITIVE_SUFFIXES` 新增 `passwd`、`pwd`、`secretkey`、`accesskey`、`awssecretaccesskey`、`awsaccesskeyid`、`bearer`；`_is_sensitive_key` 支持去除尾部数字后再匹配（`password1` 等）；键归一化增加 NFKC 与常见西里尔/希腊同形字映射（`ｐａｓｓｗｏｒｄ`、`pаssword` 等）。
- P2 XML：`_XML_ELEMENT` 支持 CDATA/嵌套子元素（正文按 `.*?` 整体捕获，敏感标签整段替换为 `***`）、Unicode/全角/同形标签名、大小写不敏感；新增 `_UNCLOSED_XML_ELEMENT` 对未闭合敏感标签同样 fail-closed；XML 属性内敏感赋值由赋值检测覆盖。
- P2 带空格引号键：`_read_assignment_key` 允许键内空白（`"access token"`、`'client secret'`）。
- P2 扩展分隔符/控制符：赋值正则与残余检测支持 `U+FE66`、`U+2236`、`U+FE55`、`U+2A75`、`U+180E`、NUL/SOH 等 C0/C1 控制符与零宽字符作为键值分隔填充。

修复后行为矩阵（逐条实跑通过）：上述 5 组反例共 27 个变体全部不再出现 `REVIEW_*` 合成值；`token_count=42`、`user_profile.name=alice`、`<name>alice</name>`、Windows/UNC 路径与 `normal \u12ZZ no credentials` 等非敏感文本保持原样；1–10/20/100 层转义、CRLF `:=`、malformed `\u`、`***` 哨兵等历史反例仍按预期。

| 修复验证项 | 实际命令 | 结果 |
|---|---|---|
| 最新专项（含 6 个新增回归测试函数） | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` | PASS，120 项（经 `--collect-only` 复核；此前误记为 128，第四轮复审已更正）。 |
| 桌面端完整回归 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q` | PASS，196 项；仅有既有 ZIP 同名 `manifest.json` 警告。 |
| 静态检查 | `desktop\.venv\Scripts\python.exe -m ruff check desktop\src desktop\tests` | PASS。 |
| 统一验证 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS：Python 3.11.0、JDK 21.0.11、50 个精确锁定依赖、Ruff、23 个共享 schema fixtures 与桌面端 196 项（合计 219 项）及 Android `lintDebug`/JVM/Debug APK 均通过；仅有既有 ZIP 重名 `manifest.json` 警告。 |

本轮未执行真实 Provider、登录或网络探测（留给后续独立真实探针任务）。`FULL-101` 已重新置为 `REVIEW`，等待全新的独立审查；本实现者不自审、不验收且不启动后续任务。

## 独立复审（2026-08-05，gpt-5.6-terra high，full101_review_terra3）

结论：`CHANGES_REQUIRED`。历史反例全部复跑通过，但新独立审查发现 2 个 P1、3 个 P2 泄露面，均可在 Provider 内存结果、JSON 报告、Markdown 报告与 CLI 输出边界复现（全部使用 `REVIEW_*` 合成值，未触碰真实凭据）。

### P1：非可打印 `\uXXXX` 字面转义绕过层数无关归一化（含完整合法 JSON）

- 位置：`desktop/src/market_monitor/providers/runner.py:312-345`（`_collapse_preflight_escape_runs` 只解码 `U+0020–U+007E` 可打印 ASCII）、`:178-210`（`_has_unsafe_preflight_sensitive_assignment` 兜底门 `_has_supported_preflight_escape(view) and _contains_sensitive_key_candidate(view)` 对非可打印转义为 False，永不触发）、`:50-58` 与 `:382-432`（赋值正则与残余检测只认字面字符）。
- 问题：`accessToken\u200B=…`、`accessToken\uFEFF=…`、`accessToken\uFF1A=…`、`accessToken\uFF1D=…`、`accessToken\u000A=…`（字面反斜杠 + `uXXXX` 六字符形式）不被折叠、不被候选检测、不被残余检测识别，值原样保留；完整合法 JSON `{"message":"accessToken\\u200b=…"}`（JSON 二次转义后的字面量）同样泄露。这直接违背本轮声称的“Unicode 冒号/等号分隔符”与“层数无关转义归一化”覆盖。
- 复现（合成值）：`redact_secrets(r'accessToken\u200b=REVIEW_ZWSP_ESCAPED')` 原样返回；`r'accessToken\uFF1A=REVIEW_ESCAPED_FULLWIDTH'`、`r'accessToken\u000a=REVIEW_LF_ESCAPED'`、`'{"message":"accessToken\\\\u200b=REVIEW_JSON_ESC"}'` 均保留合成值。作为固定 Provider 的 `NETWORK` 错误后，内存 `ProviderRunResult`、`provider-capabilities.json`、`provider-capabilities.md` 与 CLI `_emit()` stdout 均包含合成值；`--report-dir` 路径经 `_emit` 输出时同样泄露。
- 修复要求：归一化视图应把任意合法 `\uXXXX`（或至少 ZWSP/FEFF/全角分隔符/控制符等可作分隔的码位）解码后再做残余检测，或对含未折叠合法转义且带敏感候选的文本统一 fail-closed；为自由文本与完整 JSON 值、四边界添加字面 `\u200B`/`\uFEFF`/`\uFF1A`/`\uFF1D`/`\u000A` 反例测试。

### P1：常见凭据键名未纳入敏感键集合

- 位置：`desktop/src/market_monitor/providers/runner.py:30-49`（`_SENSITIVE_KEY_PARTS`）与 `:117-121`（`_is_sensitive_key` 只做精确集合/固定后缀匹配）。
- 问题：`secret_key=…`、`access_key=…`、`passwd=…`、`pwd=…`、`password1=…`、`AWS_SECRET_ACCESS_KEY=AKIA…`、`AWS_ACCESS_KEY_ID=AKIA…`、`accessKey=…`、`bearer: …` 等无混淆的常见凭据键不匹配现有集合/后缀，值原样保留；四个输出边界均泄露，且 `--report-dir` 路径经 `_emit` 输出时同样泄露。
- 复现：`redact_secrets('secret_key=REVIEW_SECRET_KEY')`、`redact_secrets('AWS_ACCESS_KEY_ID=AKIA_REVIEW_AWS')`、`redact_secrets('passwd=REVIEW_PASSWD')` 均原样返回；完整 JSON `{"message":"secret_key=REVIEW_JSON_SECRET"}` 也泄露。
- 修复要求：补充常见凭据键（至少 `secretkey`/`accesskey`/`passwd`/`pwd`/`awssecretaccesskey`/`awsaccesskeyid` 及 `bearer`）的精确或结构化匹配，并保持 `token_count=42` 等非凭据键不受影响；添加四边界回归。

### P2：XML CDATA 与嵌套元素文本绕过 XML 元素脱敏

- 位置：`desktop/src/market_monitor/providers/runner.py:60`（`_XML_ELEMENT` body 为 `[^<]*?`，不进入子元素/CDATA）与 `:140-144`（`redact_xml_element`）。
- 复现：`<password><![CDATA[REVIEW_CDATA]]></password>`、`<password><value>REVIEW_NESTED</value></password>`、`<token><inner>REVIEW_NESTED_TOKEN</inner></token>` 均原样保留；四个输出边界泄露。
- 修复要求：对敏感元素按 XML 解析语义（CDATA、嵌套元素）保守 fail-closed 或结构化脱敏，并添加四边界回归。

### P2：引号敏感键含内部空白绕过

- 位置：`desktop/src/market_monitor/providers/runner.py:387-394`（`_read_assignment_key` 引号路径要求每个字符为字母数字/`_-`，`"access token"` 被拒绝；裸键扫描随后看不到分隔符）。
- 复现：`"access token" = "REVIEW_SPACE_KEY"`、`"access token": "REVIEW_SPACE_KEY_COLON"`、`"client secret" = "REVIEW_SPACE_SECRET"`、`"private key" = "REVIEW_SPACE_PRIVATE"` 均原样保留；四个输出边界泄露。
- 修复要求：引号键在剥离外层引号后按归一化键名判断，内部空白视为非标准输入 fail-closed，并添加四边界回归。

### P2：其他 Unicode 分隔符/控制符与全角同形 XML 标签

- 位置：`desktop/src/market_monitor/providers/runner.py:50-58`（分隔符正则只接受 `=`/`：`/`＝`）、`:69`（`_ZERO_WIDTH_CHARS` 仅 4 个码位）、`:403-432`（`_read_assignment_value_start`）。
- 复现：`accessToken﹦…`（U+FE66）、`accessToken∶…`（U+2236）、`accessToken﹕…`（U+FE55）、`accessToken⩵…`（U+2A75）、`accessToken᠎=…`（U+180E）、`accessToken\x00=…`、`accessToken\x01=…` 及全角 XML 标签 `<ｐａｓｓｗｏｒｄ>…</ｐａｓｓｗｏｒｄ>`（Cyrillic 同形同理）均原样保留；四个输出边界泄露。
- 修复要求：分隔符/键归一化采用 Unicode 兼容等价或显式码位集合，控制符与全角/同形字母按非标准输入 fail-closed，并添加四边界回归。

### 本轮复核证据

- 专项：`desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` → PASS（122 项），但未覆盖上述新反例。
- 桌面全量：`desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q` → PASS（190 项；仅有既有 ZIP 同名 `manifest.json` 警告）。
- 静态检查：`desktop\.venv\Scripts\python.exe -m ruff check desktop\src desktop\tests` → PASS。
- 统一验证：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` → PASS（Python 3.11.0、JDK 21.0.11、50 个精确锁定依赖、Ruff、23 个共享 schema fixtures、桌面 190 项及 Android `lintDebug`/JVM/Debug APK）。
- 历史反例复跑：5/6/10 层转义 + 部分/完整 Unicode 键（可打印 ASCII）、CRLF `:=`、malformed `\u`、Windows/UNC 路径、`***`/`***evil` 哨兵、注册短值均按预期 fail-closed 或保留原文；说明本轮缺口是覆盖不足而非回归。
- `git diff --check` 通过；本文所有 `REVIEW_*` 均为合成审查值，未读取、写入或回显任何真实本机凭据。

本复审未修改实现、未验收且未启动后续任务。`FULL-101` 已更新为 `CHANGES_REQUIRED`；实现者只能修复以上 P1/P2 后重新进入独立审查。

## 独立复审（2026-08-05，gpt-5.6-terra high，full101_review_terra5）

结论：`CHANGES_REQUIRED`。上一轮（full101_review_terra3）全部 27 个反例复跑通过，专项 123 项、桌面 199 项与统一验证均通过；但新独立审查发现 3 个 P1、1 个 P2 泄露面，均可在 Provider 内存结果、JSON 报告、Markdown 报告与 CLI 输出边界复现（全部使用 `REVIEW_*` 合成值，未触碰真实凭据）。

### P1：百分号/HTML 实体编码的反斜杠与 `\uXXXX`/`\xXX`/`%u` 分隔符组合绕过全部脱敏

- 位置：`desktop/src/market_monitor/providers/runner.py:271-289`（preflight 视图只对解码后的 percent/entity 视图做 residual 检测，不再折叠 `\u`/`\x` 转义）、`:84-86`（`_BEARER_TOKEN` 的编码分隔符枚举不含 `%5C`/`&#92;`/`&#x5c;`/`%u`）、`:188-206`（文本级替换只认字面分隔符）。
- 问题：`Bearer%5Cu0020REVIEW_TOKEN`、`Bearer&#92;u0020REVIEW_TOKEN`、`Bearer&#x5c;u0020REVIEW_TOKEN`、`Bearer%5Cx20REVIEW_TOKEN`、`Bearer%u0020REVIEW_TOKEN`、`Bearer%5C%75%30%30%32%30REVIEW_FULL_PCT`、`Bearer%255Cu0020REVIEW_TOKEN`、`accessToken%5Cu0020=REVIEW_ASSIGN`、`accessToken&#92;u0020=REVIEW_LEAK`、`{"message":"Bearer%5Cu0020REVIEW_JSON"}` 均原样返回；`accessToken\U0020=REVIEW_UPPER`（大写 4 位 `\U` 赋值形式）同样绕过，因为折叠只认小写 `\u` 与 8 位 `\U`，Bearer 正则的大小写不敏感恰好掩盖同一缺陷。作为固定 Provider 的 `NETWORK` 错误后，内存 `ProviderRunResult`、`provider-capabilities.json`、`provider-capabilities.md` 与 CLI `_emit()` stdout 均包含合成值。
- 修复要求：对 percent/entity 解码视图再迭代执行转义折叠与解码（有界轮数）并同时做 residual 与敏感键候选检测，或对解码视图仍含受支持转义且邻近敏感候选的文本统一 fail-closed；覆盖自由文本、完整 JSON 与四边界回归。

### P1：URL userinfo 的编码分隔符绕过 URL 凭据脱敏

- 位置：`desktop/src/market_monitor/providers/runner.py:87`（`_URL_CREDENTIALS` 只匹配字面 `:` 与 `@`）。
- 问题：`https://user%3Apass@host/path`、`https://user:pass%40host/path`、`https://user&#58;pass@host/path`、`https://user&colon;pass@host/path`、`https://user%3Apass%40host/path` 均原样保留；percent/entity 视图只用于检测，且 residual 不把 `user:pass@` 视为敏感赋值，四个输出边界均泄露。 （本行中的凭据均为合成示例。）
- 修复要求：对解码视图执行 URL userinfo 替换，或扩展 URL 匹配以识别 `%3A`/`%40`/`&#58;`/`&colon;` 等编码分隔符并 fail-closed；添加四边界回归。

### P1：复合认证/Cookie 头键绕过认证头脱敏

- 位置：`desktop/src/market_monitor/providers/runner.py:57-64`（`_SENSITIVE_SUFFIXES` 缺少 `authorization`/`cookie`）、`:177-184`（`_is_sensitive_key`）、`:88`（`_HEADER`）。
- 问题：`Proxy-Authorization: Basic dXNlcjpwYXNz`、`X-Authorization: Basic dXNlcjpwYXNz`、`Set-Cookie: session=REVIEW_COOKIE` 原样保留：归一化后的 `proxyauthorization`/`xauthorization`/`setcookie` 既非精确集合成员也不匹配任何后缀，`Basic` 凭据又不在仅认 `Bearer` 的 token 正则覆盖内；四个输出边界均泄露。
- 修复要求：把 `authorization`/`cookie` 纳入后缀或复合键匹配，并对认证头下的 `Basic`/`Digest` 值整体替换；添加四边界回归。

### P2：配置敏感名注册集合与脱敏键集合不一致

- 位置：`desktop/src/market_monitor/configuration.py:98-112`（`_is_sensitive_configuration_name`）。
- 问题：runner 已支持 `passwd`/`pwd`/`secretkey`/`accesskey`/AWS 键等常见凭据键，但配置注册集合没有对应名称，`PASSWD=REVIEW_PW` 的 `secret_values` 不含该值，错误消息中孤立值 `REVIEW_PW` 原样泄露；当前项目实际使用的 `JQDATA_USERNAME`/`JQDATA_PASSWORD` 与 `*_TOKEN` 均覆盖。
- 修复要求：让配置注册逻辑与 runner 的敏感键判定共用同一规则（或补全名称集合），并添加注册/孤立值替换回归。

### 本轮复核证据

- 历史反例：上一轮 27 个反例全部复跑通过（`\u200B`/`\uFEFF`/`\uFF1A`/`\uFF1D`/`\u000A` 字面转义、JSON 二次转义、常见凭据键、XML CDATA/嵌套、引号内空格键、扩展分隔符/控制符、全角同形 XML 标签），说明本轮缺口是覆盖不足而非回归。
- 专项：`desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` → PASS，123 项（`--collect-only` 复核 123）。
- 桌面全量：`desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q --tb=no` → PASS，199 项；仅有既有 ZIP 同名 `manifest.json` 警告。
- 静态检查：`desktop\.venv\Scripts\python.exe -m ruff check desktop\src desktop\tests` → PASS。
- 统一验证：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` → PASS（Python 3.11.0、JDK 21.0.11、50 个精确锁定依赖、Ruff、23 个共享 schema fixtures、桌面 199 项及 Android `lintDebug`/JVM/Debug APK）。
- CLI 抽检：缺 JQData 配置 `probe --provider joinquant` → 退出码 3 `CONFIGURATION_BLOCKED`；`--timeout-seconds 0` 与未知 provider → 64。
- 本文所有 `REVIEW_*` 均为合成审查值，未读取、写入或回显任何真实本机凭据。

本复审未修改实现、未验收且未启动后续任务。`FULL-101` 已更新为 `CHANGES_REQUIRED`；实现者只能修复以上 P1/P2 后重新进入独立审查。

## 独立复审（2026-08-05，gpt-5.6-terra high，full101_review_terra6）

结论：`CHANGES_REQUIRED`。上一轮（full101_review_terra5）全部 12 个反例复跑通过，专项 127 项、桌面 203 项与统一验证均通过；但本独立审查新发现 4 个 P1 与 1 个 P2 泄露面，均可在 Provider 内存结果、JSON 报告、Markdown 报告与 CLI stdout 四个边界复现（全部使用 `REVIEW_*` 合成值与临时目录，未读取、写入或回显任何真实本机凭据）。

### P1：组合编码迭代预算（4 轮）耗尽后未 fail-closed，深层百分号编码仍可绕过

- 位置：`desktop/src/market_monitor/providers/runner.py:102`（`_MAX_PREFLIGHT_NORMALIZATION_ROUNDS = 4`）、`:273-278`（固定轮数循环）、`:280-290`（仅对已生成视图做检测，无“视图仍含受支持转义且邻近敏感候选”的兜底）。
- 问题：`Bearer%2525255Cu0020…`（三层 `%25` 后接 `%5C`）在 4 轮后视图仍是 `Bearer\u0020…`，再折叠一次才成为 `Bearer …`；第 5 轮不会发生，视图检测全部失配，原始文本原样返回。URL userinfo 同理：`https://user%252525253Apass@host/path` 四轮后视图仍为 `%3A`，`_has_unsafe_url_userinfo` 看不到 `:`。这直接违反第五轮修复要求中“对解码视图仍含受支持转义且邻近敏感候选的文本统一 fail-closed”的明确条件。
- 复现（合成值）：`redact_secrets(r'Bearer%2525255Cu0020REVIEW_DEEP_PCT')` 与 `redact_secrets(r'https://user%252525253Apass@REVIEW_URL_DEEP4/path')` 均原样返回。
- 修复要求：预算耗尽后若视图仍含受支持转义（`\u`/`\x`/`\U`/`%HH`/`%uHHHH`/HTML 实体）且存在敏感键候选、认证方案或 URL userinfo 形态，必须输出固定 fail-closed 占位符；为深层 Bearer、URL userinfo 与赋值变体增加单测和四边界回归。

### P1：HTML 命名实体集合过小，`&bsol;`/`&ratio;`/`&Colon;`/`&period;`/`&grave;` 绕过解码

- 位置：`desktop/src/market_monitor/providers/runner.py:370-379`（`_HTML_NAMED_ENTITY` 只含 `equals|colon|Tab|NewLine|nbsp|sol|quest|num|apos|quot`）、`:385-399`（`_decode_html_entities`）。
- 问题：`&bsol;`（U+005C 反斜杠）、`&ratio;`（U+2236）、`&Colon;`（U+2237）、`&period;`（U+002E）、`&grave;`（U+0060）均为合法 HTML5 命名实体，但不在解码表中。`Bearer&bsol;u0020…` 无法折叠为 Bearer 空格；`accessToken&ratio;…`/`accessToken&Colon;…` 无法识别为冒号赋值；`user&period;name=…` 无法合并为 `username` 复合键；`&grave;accessToken&grave;=…` 无法还原为反引号包裹键。这些输入在残余检测、正则替换与全部四个输出边界均原样保留。
- 复现（合成值）：`Bearer&bsol;u0020REVIEW_ENTITY_BSOL`、`accessToken&ratio;REVIEW_ENTITY_RATIO`、`accessToken&Colon;REVIEW_ENTITY_COLON`、`user&period;name=REVIEW_ENTITY_PERIOD`、`&grave;accessToken&grave;=REVIEW_ENTITY_GRAVE`、`https://user&ratio;pass@REVIEW_URL_RATIO/path` 均原样返回。
- 修复要求：补足与分隔符/键结构相关的命名实体（至少 `bsol`、`ratio`、`Colon`、`period`、`lowbar`、`grave`、`semi`、`comma` 等），或对含未解码实体且邻近敏感候选的文本 fail-closed；添加四边界回归并保留普通 HTML 文本可观测性。

### P1：冒号/等号分隔符同形字（U+A789、U+02D0、U+1361 等）未覆盖

- 位置：`desktop/src/market_monitor/providers/runner.py:79-84`（`_COLON_ASSIGNMENT` 仅 `:`/`：`/`﹕`/`∶`）、`:576-609`（`_read_assignment_value_start` 同集合）。
- 问题：U+A789（MODIFIER LETTER COLON）、U+02D0（TRIANGULAR COLON）、U+1361（ETHIOPIC WORDSPACE）等视觉同形分隔符既不是空白、也不是枚举分隔符；U+A789/U+02D0 还是字母类字符，会被并入键名，使归一化后的键不再是 `accesstoken`。残余检测与正则替换均失配，值原样保留。
- 复现（合成值）：`accessToken꞉REVIEW_MODIFIER_COLON`、`accessTokenːREVIEW_TRIANGULAR_COLON`、`accessToken፡REVIEW_ETHIOPIC` 均原样返回。
- 修复要求：分隔符判定采用 Unicode 兼容等价或显式码位集合（至少覆盖上述码位），键读取后按与 `_is_sensitive_key` 相同的归一化判定；添加四边界回归。

### P1：同形键字符映射不全（希腊 α/ε/τ、拉丁 ɑ 等）绕过敏感键判定

- 位置：`desktop/src/market_monitor/providers/runner.py:112-131`（`_HOMOGLYPH_MAP` 缺 U+03B1/U+03B5/U+03C4/U+0251 等）、`:174-177`（`_normalise_key` 依赖该映射）。
- 问题：`pαssword`、`tokεn`、`τoken`、`passɑword` 归一化后不是 `password`/`token`，`_is_sensitive_key` 不命中；赋值正则的键类不含这些字母，残余检测失配，值原样保留。这与“全角/西里尔同形键 fail-closed”的既有承诺同类，属于覆盖缺口。
- 复现（合成值）：`pαssword=REVIEW_GREEK_ALPHA`、`tokεn=REVIEW_GREEK_EPSILON`、`τoken=REVIEW_GREEK_TAU`、`passɑword=REVIEW_LATIN_ALPHA` 均原样返回。
- 修复要求：补充常见希腊/拉丁同形字母映射（至少 α/ε/τ/υ/χ 与 ɑ 等），或对含无法证明安全的类键字母文本 fail-closed；添加四边界回归并保留 `token_count=42` 等非敏感文本。

### P2：无头上下文的独立 `Basic <base64>` 认证值未脱敏

- 位置：`desktop/src/market_monitor/providers/runner.py:402-411`（`_AUTH_SCHEME_VIEW`/`_has_unsafe_auth_scheme` 只在视图与原文不同的 preflight 分支 `:287` 被调用）、`:86-89`（`_BEARER_TOKEN` 只覆盖 Bearer）。
- 问题：编码变体（如 `Basic%20…`）在解码视图下被拦截，但原始文本 `Basic dXNlcjpwYXNz`（Base64 的 `user:pass`）在 SDK/异常消息中作为独立凭据出现时，没有任何规则命中：认证头规则需要 `key: value`，Bearer 规则不匹配 Basic。四个输出边界均保留该值。`Authorization: Basic …`、`Proxy-Authorization: Basic …` 等既有头部形态已正确替换，本缺口仅限无头部前缀的独立值。
- 复现（合成值）：`redact_secrets(r'Basic dXNlcjpwYXNz')` 原样返回；作为固定 Provider 的 `NETWORK` 错误进入 runner 后，内存结果、JSON 报告、Markdown 报告与 CLI stdout 均含该值。
- 修复要求：对原文同样执行认证方案检测（至少 `Basic`/`Digest` 后接凭据 token 的形态），并评估对普通英文句子的误伤边界；添加四边界回归。

### 本轮复核证据

- 历史反例：第五轮 12 个反例全部复跑通过（组合 `%5C`+`\u`/`\x`/`%u`、`&#92;`、大写 `\U`、URL userinfo 编码、复合认证/Cookie 头、配置注册集合），说明本轮缺口是覆盖不足而非回归。
- 新反例端到端：将 `Bearer&bsol;u0020REVIEW_E2E_BSOL accessToken&ratio;REVIEW_E2E_RATIO pαssword=REVIEW_E2E_ALPHA Basic dXNlcjpwYXNz` 作为固定 Provider 的 `NETWORK` 错误，`ProviderRunResult`、`provider-capabilities.json`、`provider-capabilities.md` 与 CLI `_emit()` stdout 均检出全部四个合成标记。
- 专项：`desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` → PASS（127 项），但未覆盖上述反例（对测试源码检索 `&bsol;`/`&ratio;`/`&grave;`/`&period;`/`꞉`/`ː`/`α`/`ε`/`τ`/`ɑ`/`Basic dXN` 均无命中，仅存在字面 U+2236 的既有用例）。
- 桌面全量：`C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe -Command "& .\desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q"` → PASS（203 项；仅有既有 ZIP 同名 `manifest.json` 警告）。
- 静态检查：`desktop\.venv\Scripts\python.exe -m ruff check desktop\src desktop\tests` → PASS；`git diff --check` → PASS。
- 统一验证：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` → PASS（Python 3.11.0、JDK 21.0.11、50 个精确锁定依赖、Ruff、23 个共享 schema fixtures、桌面 203 项及 Android `lintDebug`/JVM/Debug APK）。
- 对照组：`token_count=42`、`C:\users\<user>\normal\app.log`、`账户：普通文本`、`accessToken: ***`、`https://example.com/a%3Db/page`、`user_profile.name=alice`、`<name>alice</name>` 均保留；`Authorization: Basic dXNlcjpwYXNz` → `[redacted sensitive text]`、`Bearer xyz` → `Bearer ***` 等既有安全语义保持。

本复审未修改实现、未验收且未启动后续任务。`FULL-101` 已更新为 `CHANGES_REQUIRED`；实现者只能修复以上 P1/P2 后重新进入独立审查。

## 独立复审（2026-08-05，gpt-5.6-terra high，full101_review_terra7）

结论：`CHANGES_REQUIRED`。上一轮（full101_review_terra6）要求的修复大部分生效：历史反例复跑中，除下述 P1-1 外全部通过；专项 132 项、桌面 208 项、Ruff、`git diff --check` 与完整统一验证均重新通过（证据见下）。但本独立审查新发现 5 个 P1 与 1 个 P2 泄露面，均用 `REVIEW_*` 合成值在 Provider 内存结果、JSON 报告、Markdown 报告与 CLI stdout 四个边界端到端复现（未读取、写入或回显任何真实本机凭据）。

### P1-1：第六轮明确列出的 `&ratio;` URL userinfo 反例仍未修复，同形冒号 URL 全部绕过

- 位置：`desktop/src/market_monitor/providers/runner.py:428`（`_URL_USERINFO_VIEW` 只认 ASCII `:`）、`:444-447`（`_has_unsafe_url_userinfo`）。
- 问题：`&ratio;`、`&#x2236;` 解码为 U+2236，字面 U+A789/U+02D0/U+1361 也仍是键值分隔符集合成员（见 `:634`），但 URL userinfo 检测视图只接受 ASCII 冒号，因此解码后 `https://user∶pass@…` 仍不被视为凭据 URL，原始文本原样返回。这正是第六轮复审 P1 “HTML 命名实体集合过小”中列出的反例 `https://user&ratio;pass@REVIEW_URL_RATIO/path`，修复声明“16 个反例全部不再出现”与实际不符。
- 复现（合成值，四个边界均确认泄露）：`redact_secrets('https://user&ratio;pass@REVIEW_URL_RATIO/path')`、`https://user&#x2236;pass@REVIEW_URL_NUM2236/path`、`https://user꞉pass@REVIEW_URL_A789/path`（U+A789）、`https://userːpass@REVIEW_URL_02D0/path`（U+02D0）、`https://user፡pass@REVIEW_URL_1361/path`（U+1361）均原样返回；作为固定 Provider 的 `NETWORK` 错误后，内存 `ProviderRunResult`、`provider-capabilities.json`、`provider-capabilities.md` 与 CLI `_emit()` stdout 均含合成值。 （本行中的凭据均为合成示例。）
- 修复要求：URL userinfo 检测视图（`_URL_USERINFO_VIEW`/`_URL_USERINFO_REMAINING`）与 `_URL_CREDENTIALS` 同源支持全部已登记冒号同形分隔符（至少 U+2236、U+A789、U+02D0、U+1361），或对解码视图仍含“userinfo + 同形冒号”的文本 fail-closed；为自由文本与完整 JSON 值、四边界添加回归，并保留 ASCII 冒号实体（`&colon;`/`&Colon;`/`&#58;`/`&#x3a;`）与 `%3A` 既有语义。

### P1-2：字面 Unicode 格式/不可见字符（Cf）插在敏感键与分隔符之间，或插入复合键内，绕过全部检测

- 位置：`desktop/src/market_monitor/providers/runner.py:73`（`_SEPARATOR_PADDING`）、`:97-98`（`_ZERO_WIDTH_CHARS`/`_IGNORABLE_SEPARATOR_CHARS`）、`:106-109`（`_is_ignorable_between`）、`:596-616`（`_read_assignment_key`）、`:619-648`（`_read_assignment_value_start`）。
- 问题：可忽略字符集合只含 `\u200b\u200c\u200d\u2060\ufeff\u180e` 与 C0/C1 控制符，遗漏 U+00AD（软连字符）、U+200E/U+200F（LRM/RLM）、U+202A-202E（双向控制符）、U+2061-2064（不可见运算符）、U+034F（组合字素连接符）、U+061C 等 Unicode 格式字符。`accessToken<LRM>=…` 中键读取在 LRM 处结束、`_read_assignment_value_start` 不把它当分隔符，残余检测、赋值正则与候选扫描全部失配，值原样保留；`pass<LRM>word=…` 同样因键被拆成 `pass`/`word` 两段而失配。
- 复现（合成值，四个边界均确认泄露）：`accessToken\u200E=REVIEW_LRM_SEP`、`accessToken\u00AD=REVIEW_SOFT_HYPHEN`、`accessToken\u2061=REVIEW_INVISIBLE_OP`、`accessToken\u202E=REVIEW_BIDI`、`accessToken\u034F=REVIEW_CGJ`、`pass\u200Eword=REVIEW_LRM_KEY`、`pass\u00ADword=REVIEW_SH_KEY`、`pass\u034Fword=REVIEW_CGJ_KEY` 均原样返回；完整合法 JSON `{"message":"accessToken\\u200e=REVIEW_JSON_LRM"}` 也原样保留。以 `accessToken\u200E=…` 作固定 Provider `NETWORK` 错误时，内存结果、JSON/Markdown 报告与 CLI stdout 均含合成值。
- 修复要求：把 Unicode Cf 格式字符（至少 U+00AD、U+200E、U+200F、U+202A-202E、U+2061-2064、U+034F、U+061C）纳入键读取/分隔符跳过语义，或对含这些字符且邻近敏感键候选的文本 fail-closed；添加自由文本与完整 JSON 值、四边界回归，并保持 Windows/UNC 路径等既有控制不变。

### P1-3：敏感键归一化仍缺变音符、更多同形字母、leet 数字与非 ASCII 数字，`passwérd`/`passw0rd` 等直接泄露

- 位置：`desktop/src/market_monitor/providers/runner.py:112-141`（`_HOMOGLYPH_MAP`）、`:180-182`（`_normalise_key`）、`:185-193`（`_is_sensitive_key`）。
- 问题：归一化只做 NFKC + 少量希腊/西里尔/拉丁映射 + 去非字母数字 + 去尾部 ASCII 数字。带变音拉丁字母（é/ä）、未映射同形字母（ш、数学字母数字）、leet 替换（`passw0rd`、`p4ssword`、`t0ken`、`p@ssword`）以及非 ASCII 数字（`password١`）归一化后都不是 `password`/`token`，键判定失配，值原样保留。这与已确立的“对含无法证明安全的类键字母文本 fail-closed”承诺同类。
- 复现（合成值）：`passwérd=REVIEW_ACCENT`、`pässword=REVIEW_UMLAUT`、`paшword=REVIEW_CYRILLIC_SHA`、`pas𝐡ord=REVIEW_MATH_H`、`p@ssword=REVIEW_AT`、`passw0rd=REVIEW_LEET0`、`p4ssword=REVIEW_LEET4`、`t0ken=REVIEW_LEET_TOKEN`、`password١=REVIEW_ARABIC_DIGIT` 均原样返回；`passw0rd`/`passwérd` 的四边界端到端均泄露。
- 修复要求：键归一化补充 NFKD/组合标记剥离、常见拉丁变音与剩余同形字母映射、leet 数字→字母映射及非 ASCII 数字→ASCII 数字映射，或对无法证明安全的类键字母文本 fail-closed；添加四边界回归，并保持 `token_count=42` 等非敏感键不受影响。

### P1-4：常见凭据键名与认证头仍缺 `auth`/`key` 复合形态，`X-Auth`、`X-Auth-Key`、`authkey` 等直接泄露

- 位置：`desktop/src/market_monitor/providers/runner.py:30-56`（`_SENSITIVE_KEY_PARTS`）、`:57-72`（`_SENSITIVE_SUFFIXES`）、`:90`（`_HEADER`）。
- 问题：`auth` 不在敏感集合，普通 `key` 也不在后缀集合，因此 `auth_key`/`authkey`、`consumer_key`、`session_key`、`master_key`、`signing_key`、`encryption_key` 及 `X-Auth: <token>`、`X-Auth-Key: <token>` 头部值均不命中；这些是真实 API/SDK 错误文本中的常见凭据形态。
- 复现（合成值）：`auth_key=REVIEW_AUTH_KEY`、`authkey=REVIEW_AUTHKEY`、`consumer_key=REVIEW_CONSUMER`、`session_key=REVIEW_SESSION`、`master_key=REVIEW_MASTER`、`signing_key=REVIEW_SIGNING`、`encryption_key=REVIEW_ENC`、`X-Auth-Key: REVIEW_XAUTHKEY`、`X-Auth: REVIEW_XAUTH` 均原样返回；`authkey` 四边界端到端泄露。
- 修复要求：把 `auth`/`authkey`/`consumerkey`/`sessionkey`/`masterkey`/`signingkey`/`encryptionkey` 等常见凭据键纳入精确或结构化匹配（并评估普通 `key` 后缀的误伤边界），`X-Auth`/`X-Auth-Key` 等复合认证头由 `_HEADER`/`_is_sensitive_key` 覆盖；添加四边界回归并保留 `user_profile.name=alice` 等非敏感文本。

### P1-5：字面不可见字符作 Bearer/Basic/Digest 分隔符绕过认证方案替换

- 位置：`desktop/src/market_monitor/providers/runner.py:86-88`（`_AUTH_SCHEME_TOKEN` 分隔符枚举不含字面 U+200B/U+200E/U+00AD/U+034F 等）、`:277-304`（`_has_unsafe_preflight_sensitive_assignment` 只对“与原文不同的视图”调用 `_AUTH_SCHEME_VIEW`，字面格式字符不改写视图，故 `_AUTH_SCHEME_VIEW` 的宽分隔符集合永不生效）。
- 问题：`Bearer<ZWSP>eyJ…` 在界面上与 `Bearer eyJ…` 不可区分，但 `_AUTH_SCHEME_TOKEN` 不把 U+200B/U+200E/U+00AD/U+034F 当作分隔符，视图又与原文相同，认证方案检测被跳过，token 原样保留。
- 复现（合成值）：`Bearer\u200BeyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9REVIEW_ZWSP`、`Bearer\u200EReview_LRM`、`Basic\u200BdXNlcjpwYXNzREVIEW_BASIC_ZWSP`、`Digest\u00ADREVIEW_DIGEST` 均原样返回；`Bearer<ZWSP>` 四边界端到端泄露。编码变体（`%E2%80%8B`、`&#x200B;` 等）仍被 preflight 拦截，仅字面字符存在缺口。
- 修复要求：`_AUTH_SCHEME_TOKEN` 与原文检测路径共享与 P1-2 相同的 Cf 格式字符分隔符集合（或对含认证方案前缀 + 未证明安全分隔符的文本 fail-closed）；添加四边界回归。

### P2-1：中文凭据键名（密码/密钥/令牌等）未纳入敏感键集合

- 位置：`desktop/src/market_monitor/providers/runner.py:185-193`（`_is_sensitive_key` 无 CJK 凭据词表）。
- 问题：本产品面向中文 SDK/日志，`密码`、`密钥`、`令牌`、`口令`、`私钥` 是 `password`/`secret`/`token` 的直接中文等价键；当前 `_read_assignment_key` 可读中文键，但归一化后不命中任何集合，值原样保留。
- 复现（合成值）：`密码=REVIEW_CHINESE_PASSWORD`、`密钥：REVIEW_CHINESE_SECRET`、`令牌：REVIEW_CHINESE_TOKEN` 均原样返回；`密码=…` 四边界端到端泄露。
- 修复要求：为明确凭据中文键（至少 密码/密钥/口令/令牌/私钥）增加映射或精确匹配并添加四边界回归；必须保持既有控制 `账户：普通文本` 原样保留（如需覆盖 `账户` 请单列设计并更新控制）。

### 本轮复核证据

- 历史反例：第六轮列出的反例中，除 `https://user&ratio;pass@…`（P1-1）外全部复跑通过（`Bearer%2525255Cu0020…`、`user&period;name=…`、`pαssword`、`Basic dXNlcjpwYXNz` 等）；第五轮 12 个、第四轮 13 个及第三轮 27 个反例抽样复跑均通过；Windows/UNC 路径、`normal \u12ZZ no credentials`、`token_count=42`、`user_profile.name=alice`、`<name>alice</name>`、`https://example.com/a%3Db/page`、`账户：普通文本`、完全转义 `***` 哨兵等控制保持原样。
- 专项：`desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_configuration.py desktop\tests\test_provider_runner.py desktop\tests\test_cli.py desktop\tests\test_joinquant_provider.py -q` → PASS（132 项，`--collect-only` 复核 132）。现有测试未覆盖上述新反例（对测试源码检索 `200e`/`00ad`/`034f`/`authkey`/`passw0rd`/`密码` 无命中，仅存在 `&ratio;` 的赋值形态用例）。
- 桌面全量：`desktop\.venv\Scripts\python.exe -m pytest desktop\tests -q` → PASS（208 项；仅有既有 ZIP 同名 `manifest.json` 警告）。
- 静态检查：`desktop\.venv\Scripts\python.exe -m ruff check desktop\src desktop\tests` → PASS；`git diff --check` → 无输出（通过）。
- 统一验证：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` → PASS（Python 3.11.0、JDK 21.0.11、50 个精确锁定依赖、Ruff、23 个共享 schema fixtures、桌面 208 项、Android `lintDebug`/`testDebugUnitTest`/`assembleDebug`）。
- 四边界端到端：`accessToken\u200E=…`、`passw0rd=…`、`Bearer\u200B…`、`authkey=…`、`https://user&ratio;pass@…`、`密码=…`、`passwérd=…` 作为固定 Provider `NETWORK` 错误时，内存 `ProviderRunResult`、`provider-capabilities.json`、`provider-capabilities.md` 与 CLI `_emit()` stdout 均检出对应 `REVIEW_E2E_*` 合成标记。
- 本文所有 `REVIEW_*` 均为合成审查值，未读取、写入或回显任何真实本机凭据。

本复审未修改实现、未验收且未启动后续任务。`FULL-101` 已更新为 `CHANGES_REQUIRED`；实现者只能修复以上 P1/P2 后重新进入独立审查。
