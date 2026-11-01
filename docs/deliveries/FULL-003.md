# FULL-003 交付记录

**任务**：建立统一基线验证入口  
**角色**：独立实现  
**状态建议**：`REVIEW`

## 结果

新增 `scripts/verify.ps1`，以一条命令完成锁定的 Python 环境、依赖健康、Ruff、共享 JSON Schema 夹具、完整 pytest、Android `lintDebug`、`testDebugUnitTest` 与 `assembleDebug` 验证：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify.ps1
```

脚本显式验证并使用 `C:\Users\<user>\.jdks\jbr-21.0.11`（JDK 21.0.11）。Android 子命令开始前自动选择空闲盘符，将仓库临时映射到英文根路径；结束或失败时在 `finally` 中释放映射并恢复进程级 `JAVA_HOME`/`PATH`。任一外部子命令非零均会使脚本以非零退出。质量检查前，脚本还会离线比对已安装版本和 `desktop/requirements.lock` 中全部精确条目。

Python 开发依赖新增精确锁定的 `ruff==0.12.11`，其直接声明、完整锁文件和实际安装版本已一致。Ruff 的规则配置覆盖生产和测试目录；历史测试中的紧凑写法仅在 `tests/**` 精确忽略 `E701`、`E702` 与 `F401`，生产目录没有这些豁免。

## 修改文件

- `scripts/verify.ps1`
- `desktop/pyproject.toml`
- `desktop/requirements.lock`
- `desktop/src/market_monitor/providers/base.py`
- `README.md`
- `desktop/README.md`
- `android/README.md`
- `STATUS.md`
- `docs/deliveries/README.md`
- `docs/deliveries/FULL-003.md`

## 实际验证

| 验证项 | 实际命令/方式 | 真实结果 |
|---|---|---|
| Ruff 锁定与依赖健康 | `desktop\.venv\Scripts\python.exe -m pip install --isolated --index-url https://pypi.org/simple ruff==0.12.11`、`-m pip check`、`-m ruff --version` | PASS：安装与锁定版本均为 `ruff 0.12.11`，`pip check` 无损坏依赖 |
| 锁文件解析 | `-m pip install --dry-run --ignore-installed --isolated --index-url https://pypi.org/simple -c .\desktop\requirements.lock -e '.\desktop[dev]'` | PASS：PyPI HTTPS 解析包括 `ruff==0.12.11` 的全部候选包，均受精确锁文件约束 |
| 统一成功路径（第一次） | `powershell -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS：Python 3.11.0、JBR 21.0.11、`pip check`、Ruff、Schema 13 passed、pytest 49 passed、`lintDebug`、`testDebugUnitTest`、`assembleDebug` 均成功 |
| 受控失败 | `powershell -ExecutionPolicy Bypass -File scripts\verify.ps1 -SimulateFailure` | EXPECTED FAIL：无副作用 Python 子进程退出码为 17；脚本报告该子项失败并以退出码 1 结束；未修改仓库文件或创建 `subst` 映射 |
| 失败后的成功复跑 | `subst; powershell -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS：`subst` 在复跑前无残留映射；之后所有统一子项再次成功，且映射在结束时释放 |
| 变更完整性 | `git diff --check` | PASS：无空白错误 |

完整 pytest 的唯一输出警告是既有签名篡改夹具向 ZIP 追加同名 `manifest.json` 的 `UserWarning`；测试仍为 49 passed，未将该警告伪装为失败。

## 基线暴露问题与最小修复

首次真实执行在 Ruff 子项以非零退出，暴露 12 条既有问题：生产端 `desktop/src/market_monitor/providers/base.py` 有一个未使用的 `dataclasses.field` 导入，历史测试中有一行多语句和未使用导入。依照 FULL-003 的例外边界，仅移除了生产端的无行为影响未使用导入；测试历史格式没有重写，而是以 Ruff 的测试目录精确豁免保持静态分析对生产代码有效。未修改数据源、契约、数据库、行情包或 Android 业务行为。

## 接口、迁移与安全

- **公开接口/数据库/数据包迁移**：无。
- **业务行为**：无；唯一源码改动是删除未使用导入。
- **安全与隐私**：未读取、写入或记录凭据、私钥、个人数据或真实行情文件。`SimulateFailure` 只运行解释器内的 `sys.exit(17)`。
- **已知环境事实**：中文物理路径仍不直接用于 Gradle JVM worker；统一脚本临时使用空闲 `subst` 盘符并在 finally 中清理。

## 风险与后续

- `C:\Users\<user>\.jdks\jbr-21.0.11` 是当前环境中该脚本指定的 JDK 21；缺失时脚本会明确失败，不能以 Android Studio 的 JBR 25 替代。
- Android Gradle 输出仍有既有的 `android.overridePathCheck=true` experimental 警告；本任务未修改该配置，且 `lintDebug`、JVM 单测和 APK 构建均真实成功。
- 实现完成后仅进入 `REVIEW`，等待全新独立审查任务；本实现任务不自行审查、验收或启动 FULL-100。

## 独立审查（2026-08-05）

**角色**：独立审查（非实现者）  
**审查范围**：`e407399..` 的 FULL-003 未提交差异及新增文件。

### 结论

发现 1 个 P2 问题，FULL-003 进入 `CHANGES_REQUIRED`；不得进入验收或启动 FULL-100。

### 问题

- **P2 — 统一入口没有验证已安装开发依赖等于精确锁定版本**：[`scripts/verify.ps1`](../../scripts/verify.ps1#L92-L103) 只运行 `pip check`，随后直接执行 Ruff 和 pytest。`pip check` 只验证已安装包的依赖关系满足元数据，不能证明可选 `dev` 依赖中实际被调用的 `ruff`/`pytest` 与 `desktop/requirements.lock`（以及 `pyproject.toml`）中固定的精确版本一致；例如任意可运行但版本不同的 Ruff 仍会通过第 95–97 行。该脚本对外宣称验证“锁定的 Python 环境”，会把未按锁文件安装的开发环境误报为通过。修复应在执行测试前，以受控的离线/本地元数据比较（至少覆盖锁文件中的项目直接依赖与 dev 依赖）或等效精确校验，失败时返回非零；随后重跑成功路径和 `-SimulateFailure`。

### 独立复核证据

| 复核项 | 实际命令/方式 | 结果 |
|---|---|---|
| 成功路径 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` | 脚本退出 0；Python 3.11.0、JBR 21.0.11、`pip check`、Ruff、Schema 13 passed、pytest 49 passed、`lintDebug`、`testDebugUnitTest`、`assembleDebug` 均真实成功，且无残留 `subst` 映射 |
| 受控失败及清理 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1 -SimulateFailure; subst` | 子进程按设计以 17 失败，脚本退出 1；失败发生于映射前，无 `subst` 残留。代码的 `finally` 在映射后异常时恢复 Location、`JAVA_HOME`/`PATH` 并删除已确认创建的映射 |
| 锁文件与工具链复核 | `python -m pip show ruff`、检查 `desktop/pyproject.toml`/`desktop/requirements.lock`、脚本 JDK/Gradle 调用 | 当前环境中的 Ruff 为 0.12.11，声明和锁文件一致；JDK 路径与文档一致；Ruff 豁免仅限 `tests/**` 的 E701/E702/F401，未发现业务越界 |
| 差异完整性 | `git diff --check` | PASS；未发现空白错误 |

## P2 修复：已安装依赖与锁文件的精确一致性（2026-08-05）

独立审查指出 `pip check` 只验证依赖关系，不能证明调用 Ruff/pytest 的开发环境等于锁文件。修复后，`scripts/verify.ps1` 在执行 Ruff、Schema 和 pytest 前使用临时、离线的 Python 验证器完成以下检查：

- 以 UTF-8 读取 `desktop/requirements.lock`，跳过空白和注释；每个非空条目必须是 `name==version`，否则失败。
- 将锁文件名称和 `importlib.metadata.distributions()` 中的已安装名称按 PEP 503 风格规范化（连续 `-`、`_`、`.` 统一为 `-` 并小写），逐一核对锁文件的全部 50 个精确条目。
- 任何锁内包缺失、版本不相等、重复包版本冲突或非精确锁条目都会使统一脚本非零退出；环境中额外的 bootstrap/工具包不作为失败条件，因为锁文件的既有语义是“项目所需的完整约束”，而不是虚拟环境的唯一内容。
- 验证器写入唯一的系统临时 `.py` 文件以避免 Windows PowerShell 对 `python -c` 引号的破坏；`finally` 在成功和失败路径均清理该临时文件，不访问网络、不修改锁文件或虚拟环境。

| 修复验证项 | 实际命令/方式 | 真实结果 |
|---|---|---|
| 版本错配反证 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1 -SimulateLockMismatch` | EXPECTED FAIL：仅在内存中把 Ruff 期望版本注入为 `0.0.0-controlled-mismatch`，验证器报告 `ruff: expected 0.0.0-controlled-mismatch, installed 0.12.11`，脚本退出 1；未改动锁文件、venv 或仓库 |
| 锁一致性与完整成功路径 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS：离线验证报告 `requirements.lock: 50 exact entries checked`；Python 3.11.0、JBR 21.0.11、Ruff、Schema 13 passed、pytest 49 passed、`lintDebug`、`testDebugUnitTest` 和 `assembleDebug` 成功 |
| 原受控失败仍有效 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1 -SimulateFailure` | EXPECTED FAIL：锁一致性先通过（50 项），随后无副作用子进程退出 17，脚本退出 1；临时锁验证器和 `subst` 均无残留 |
| 失败后的最终复跑 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS：全部 50 个锁项再次一致，所有 Python 与 Android 子项成功 |

P2 修复没有改变公开接口、数据库、数据包、Provider 或 Android 业务行为。实现任务再次结束于 `REVIEW`，等待新的独立审查。

## 独立复审（2026-08-05）

**角色**：全新独立复审（非原实现者、非前次审查者）  
**审查范围**：`e407399..` 的 FULL-003 未提交差异，重点为前次 P2 修复。

### 结论

未发现 P0–P3 问题。前次 P2 已修复；FULL-003 进入 `ACCEPTANCE`，不得由本审查任务自行验收或启动 FULL-100。

### 独立复核证据

| 复核项 | 实际命令/方式 | 结果 |
|---|---|---|
| 锁解析与安装版本 | 阅读 `scripts/verify.ps1` 第 77–128 行并运行统一入口 | PASS：验证器以 PEP 503 规则规范化名称，离线解析 `requirements.lock`，逐项核对全部 50 个精确条目；缺包、错版、非精确条目和同名冲突均会非零失败。环境额外 bootstrap/工具包不作为失败条件，符合约束锁而非虚拟环境内容白名单的既有语义。 |
| 错版反证与临时清理 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1 -SimulateLockMismatch` | EXPECTED FAIL：Ruff 内存期望版本被受控改为 `0.0.0-controlled-mismatch`，报告实际 `0.12.11` 并退出 1；执行前后系统临时目录均无 `market-monitor-lock-verifier-*.py`，无 `subst` 映射残留。 |
| 原受控失败与退出码 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1 -SimulateFailure` | EXPECTED FAIL：锁校验先完成 50 项，子进程按设计退出 17，统一入口退出 1；临时验证器无残留。 |
| 完整成功路径与映射清理 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS：Windows PowerShell 5.1.26100.8875 下 Python 3.11.0、JBR 21.0.11、`pip check`、Ruff、Schema 13 passed、pytest 49 passed、`lintDebug`、`testDebugUnitTest`、`assembleDebug` 均成功；临时 `M:` 映射和验证器文件均在结束后清理。唯一 pytest 输出为既有同名 ZIP 夹具警告。 |
| 范围与工具链复核 | 检查 `e407399..` 差异、JDK 调用和 Ruff 配置；`git diff --check` | PASS：脚本固定并实际验证 JDK 21，PowerShell 5.1 成功执行；业务源码仅删除无用导入，Ruff 豁免仅限 `tests/**`；无空白错误、无公开接口/数据包/Provider/Android 业务越界。 |

## 独立验收（2026-08-05）

**角色**：全新独立验收（非实现者、非两次审查者）  
**验收范围**：FULL-003 的统一基线入口、受控失败语义、临时资源清理与允许修改范围。

### 结论

验收通过，`FULL-003` 可标记为 `ACCEPTED`。其依赖已全部满足，`FULL-100` 成为第一项 `READY` 任务；本验收没有实现或启动该后续任务。

### 独立实测证据

| 验收项 | 实际命令/方式 | 真实结果 |
|---|---|---|
| 默认完整基线 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` | PASS（退出码 0）：Windows PowerShell 5.1.26100.8875、Python 3.11.0、JBR 21.0.11、`pip check`、50 个精确锁项、Ruff、Schema 13 passed、pytest 49 passed、`lintDebug`、`testDebugUnitTest` 与 `assembleDebug` 均真实成功。pytest 仅报告既有的同名 ZIP 夹具 `UserWarning`。 |
| 锁版本错配反证 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1 -SimulateLockMismatch` | EXPECTED FAIL（退出码 1）：离线验证器明确报告 `ruff: expected 0.0.0-controlled-mismatch, installed 0.12.11`，未到达 Android 构建阶段。 |
| 受控子项失败反证 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1 -SimulateFailure` | EXPECTED FAIL（退出码 1）：锁校验、Ruff、Schema 与 pytest 先通过；无副作用 Python 子进程按设计退出 17，统一入口正确失败。 |
| 清理与故障恢复 | 在两条失败路径后检查 `%TEMP%` 的 `market-monitor-lock-verifier-*.py` 和 `subst`，随后再次运行默认入口 | PASS：每次均无临时验证器或盘符映射残留；失败后的最终默认复跑再次以退出码 0 通过全部 Python 和 Android 子项。 |
| 范围与安全 | 检查 FULL-003 差异、`git diff --check`、变更文件敏感值模式扫描、`git ls-files` 大文件扫描 | PASS：无空白错误、无敏感值匹配、无受版本控制文件超过 10 MB；改动位于脚本、Ruff/依赖配置和说明，业务源码仅删除无行为影响的未使用导入，符合 FULL-003 允许范围。 |

已知的 `android.overridePathCheck=true` experimental 警告与 pytest ZIP 夹具警告均未被忽略或伪称为错误；在本验收中相关命令仍真实成功。没有发生公开接口、数据库、数据包、Provider 或 Android 业务行为变更。
