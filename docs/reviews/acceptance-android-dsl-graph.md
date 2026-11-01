# Android/DSL/图谱集群独立验收记录（accept_android2，2026-08-06）

**角色**：全新独立验收 Agent（`accept_android2`），只重跑命令并记录证据，未修改任何实现代码。
**范围**：FULL-123/300/301/302/303/400/401/402/403/404/500/501/502/503/504/700/701/702/703/704。

## 本机实测证据

| 验收项 | 实际命令 | 真实结果 |
|---|---|---|
| 桌面 DSL/图谱/契约专项 | `desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_strategy_dsl.py desktop\tests\test_industry_graph_models.py desktop\tests\test_industry_graph_importers.py desktop\tests\test_industry_graph_pipeline.py desktop\tests\test_industry_graph_review.py desktop\tests\test_contracts.py -q` | PASS：115 项收集（43/6/9/9/8/40），全部通过，exit 0 |
| 金标评估（FULL-702） | `desktop\.venv\Scripts\python.exe -m market_monitor.industry_graph.evaluate` | 实体 P/R/F1=1.0/1.0/1.0（10/10/10）；关系 P/R/F1=1.0/1.0/1.0（8/8/8）；阈值 precision≥0.8、recall≥0.7，超阈值 |
| Android JVM 全量 | `gradlew -p android testDebugUnitTest --no-daemon --rerun-tasks`（JDK 21 + 临时 subst 盘，`C:\Users\<user>\.jdks\jbr-21.0.11`） | BUILD SUCCESSFUL：20 个 suite / 68 tests / 0 failures / 0 errors / 0 skipped |
| Android lint | `gradlew -p android lintDebug --no-daemon` | BUILD SUCCESSFUL：0 errors；9 warnings + 1 information（GradleDependency 6、SetJavaScriptEnabled 1、KaptUsageInsteadOfKsp 1、AutoboxingStateCreation 1、MissingApplicationIcon 1，均为存量非阻断项） |
| 临时资源清理 | 验收结束后 `subst` 复检 | 无残留盘符映射 |
| 设备条件 | `adb devices -l` | 无设备连接；Android 13+ / 16 KB 页面真机类验收项本机无法执行，如实保留解除条件 |

## 逐任务结论

| 任务 | 结论 | 依据/解除条件 |
|---|---|---|
| FULL-400 | ACCEPTED | 本机专项 + Android JVM 全量通过；见 `docs/deliveries/FULL-400.md` 独立验收章节 |
| FULL-401 | ACCEPTED | 本机专项 + Android JVM 全量通过；见 `docs/deliveries/FULL-401.md` 独立验收章节 |
| FULL-402 | ACCEPTED | 本机专项 + Android JVM 全量通过；见 `docs/deliveries/FULL-402.md` 独立验收章节 |
| FULL-403 | ACCEPTED | 桌面 3 向量与 Android `DslSharedVectorsTest` 均在本机重跑通过；见 `docs/deliveries/FULL-403.md` 独立验收章节 |
| FULL-500 | ACCEPTED | 迁移/约束/CRUD JVM 证据本机重跑通过；SQLCipher 真机打开作为已知设备缺口如实记录，不视为已实测 |
| FULL-501 | ACCEPTED | 外键顺序/修订状态/空账本修复与本机 JVM 全量通过；Room DAO 设备路径留已知缺口 |
| FULL-502 | ACCEPTED | 统计固定样本本机重跑通过；UTC 自然日 P3 保留为已知缺口 |
| FULL-503 | ACCEPTED | 备份/恢复专项本机重跑通过；真机 SAF/SQLCipher 完整往返留已知设备缺口 |
| FULL-700 | ACCEPTED | 图谱模型/契约专项本机重跑通过；见 `docs/deliveries/FULL-700.md` 独立验收章节 |
| FULL-701 | ACCEPTED | 导入器各格式/失败/重复路径专项本机重跑通过；见 `docs/deliveries/FULL-701.md` 独立验收章节 |
| FULL-702 | ACCEPTED | 金标评估实体与关系 P/R/F1=1.0 本机实测；见 `docs/deliveries/FULL-702.md` 独立验收章节 |
| FULL-703 | ACCEPTED | 审核/修订/审计链专项本机重跑通过；见 `docs/deliveries/FULL-703.md` 独立验收章节 |
| FULL-123 | ACCEPTANCE | 本机可执行部分通过；解除条件=Android 13+ 16 KB 设备（或等价模拟器）断网导入真实签名包并离线显示日线/分钟线（依赖真实 Provider 产物） |
| FULL-300 | ACCEPTANCE | 本机可执行部分通过；解除条件=真机 SQLCipher 打开；替换/删除行情库后个人库数据保留 |
| FULL-301 | ACCEPTANCE | 本机可执行部分通过；解除条件=真机真实包查询、K 线缩放、周期切换与截止时间显示 |
| FULL-302 | ACCEPTANCE | 本机可执行部分通过；解除条件=真机缺失/失败/陈旧状态显示（不得显示为零或正常） |
| FULL-303 | ACCEPTANCE | 本机可执行部分通过；解除条件=真机目标数据量基准与低空间清理；已知 P2（清理 IOException 未捕获）与 P3（StatFs 回退）保留 |
| FULL-404 | ACCEPTANCE | 本机可执行部分通过；解除条件=真机参数编辑→运行→历史与信号解释流程；已知 P2（主线程解释器、历史参数丢失）保留 |
| FULL-504 | ACCEPTANCE | 本机可执行部分通过；解除条件=真机完整录入→复盘→加密备份→清库恢复→错误回滚流程；已知 P2（多标的收盘价回退、备份主线程）保留 |
| FULL-704 | ACCEPTANCE | 本机可执行部分通过；解除条件=真机从关系逐级追溯原始来源位置与确认状态；已知 P2/P3 保留 |

## 状态说明

- 本记录只反映验收角色重跑的证据；设备类解除条件未满足前，对应任务维持 `ACCEPTANCE`，不标 `ACCEPTED`，不伪造真机验收。
- 已知缺口（P2/P3）以各交付文档与审查记录为准，本记录不将它们改写成已完成。
