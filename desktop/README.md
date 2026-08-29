# desktop（数据生产端）

数据生产端包含 Provider 探针、标准化、质量检查、行情缓存、公式/账户分析、行情包与签名。当前任务从 `../Plan_R4.md` 启动，架构与约束见 `../docs/ARCHITECTURE.md`、`../docs/ADR.md`；R1–R3、旧 Day 0/`FULL-*` 文档只保留历史证据。

## 环境

- Python 3.11.0（`.python-version`；虚拟环境位于 `desktop/.venv`，不入库）
- `requirements.lock` 固定运行时与测试传递依赖；`pyproject.toml` 的直接依赖与锁文件一致

## 命令

```powershell
py -3.11 -m venv desktop\.venv
desktop\.venv\Scripts\python -m pip install -c desktop\requirements.lock -e "desktop[dev]"
desktop\.venv\Scripts\python -m market_monitor --version
desktop\.venv\Scripts\python -m pytest desktop\tests
desktop\.venv\Scripts\python -m market_monitor kline-cache --data-root data_control
desktop\.venv\Scripts\python -m market_monitor futures-calendar-sync --data-root data_control
desktop\.venv\Scripts\python -m market_monitor futures-rule-sync --data-root data_control --lookback-days 10
desktop\.venv\Scripts\python -m market_monitor futures-heat --data-root data_control
```

统一基线验证使用仓库根目录的 `powershell -ExecutionPolicy Bypass -File scripts\verify.ps1`；它也会执行固定为 `ruff==0.12.11` 的静态检查。

Provider 探测报告使用 Contract v2：每项能力独立登记参数化请求、状态、探测时间、证据、限制和错误，不以来源级开关推断可用性。v1 报告必须通过 `market_monitor.providers.migrate_v1_provider_run_result()` 显式迁移，不能静默丢失运行级状态或错误。新的 v2 能力必须有显式登记；旧适配器和 v1 迁移使用受限的私有兼容路径，公共 API 不提供按名称自动登记的逃逸通道。

通达信证券使用 `tdx-cn-v2` 质量门：先按资产类型确定价格精度，再以量额关系逐行验证成交量倍率；原始值、倍率、单位和规则版本随 Bar 保存，失败记录只进入隔离区。完整替换必须使用 `--audit-only` 审计后再执行 `--replace-source --full-rescan`。`Plan_R1.md`、`Plan_R2.md`、`Plan_R3.md`、`docs/Plan.md` 与 `docs/Plan_full.md` 仅为历史记录。
