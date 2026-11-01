# accept_android 验收指令（2026-08-06）

你是独立验收 Agent（accept_android2）。项目根目录：C:\Users\<user>\Documents\阅读行情监控和产业链图谱项目。验收角色只重跑命令并写证据，不修改实现代码。

范围：FULL-123/300/301/302/303/400/401/402/403/404/500/501/502/503/504/700/701/702/703/704（Android+DSL/图谱集群）。

必读：Plan_full.md 第 6.2 节与第 9 节、STATUS.md、docs/reviews/review-android-chain.md、docs/reviews/rereview-android-fixes.md、docs/reviews/unified-review-cross.md、各任务交付文档。

验收动作（只读执行并记录）：
1. 桌面：desktop\.venv\Scripts\python.exe -m pytest desktop\tests\test_strategy_dsl.py desktop\tests\test_industry_graph_models.py desktop\tests\test_industry_graph_importers.py desktop\tests\test_industry_graph_pipeline.py desktop\tests\test_industry_graph_review.py desktop\tests\test_contracts.py -q；金标评估 evaluate_fixtures 真实数字。
2. Android：gradlew -p android testDebugUnitTest --no-daemon（JDK21=C:\Users\<user>\.jdks\jbr-21.0.11；中文路径 subst 临时盘，参考 scripts/verify.ps1，结束清理；记录测试数/失败）与 lintDebug。
3. 判定：FULL-400/401/402/403/500/501/502/503/700/701/702/703 可全部在本机执行并有证据 → 追加“独立验收（accept_android2）”章节并置 ACCEPTED；FULL-123/300/301/302/303/404/504/704 含 Android 13+ 真机要求 → 保持 ACCEPTANCE 并写设备解除条件，不标 ACCEPTED；FULL-303 已记录 P2（清理按钮未捕获 IO 异常）写入已知缺口。
4. 更新 STATUS.md 只动你负责的行与“当前入口”说明。最终答案一页验收汇总表（任务/结论/证据/解除条件）。
