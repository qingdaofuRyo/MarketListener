# accept_data 验收指令（2026-08-06）

你是独立验收 Agent（accept_data2）。项目根目录：C:\Users\<user>\Documents\阅读行情监控和产业链图谱项目。验收角色只重跑命令与必要真实操作并写证据，不修改实现代码。

范围：FULL-100/101/110/111/112/113/120/121/122/200/201/202/203/204/600/601/602/800/801/802（桌面集群）。

注意：FULL-101 的 terra7 P1/P2 与性能修复均已完成并通过全量验证（见 docs/deliveries/FULL-101.md 与 docs/reviews/），本轮一并验收其自动化证据；真实凭据/网络类条件如实记录。

必读：Plan_full.md 第 6.2 节与第 9 节、STATUS.md、docs/reviews/unified-review-data.md、docs/reviews/rereview-data-fixes.md、各任务交付文档。

验收动作（只读执行并记录）：
1. `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` 全量（记录各子项）。
2. 逐任务重跑专项：desktop\.venv\Scripts\python.exe -m pytest（configuration/providers/baking/pipeline/incremental/quality/market_package/signing/storage/catalog/calendar/corporate_actions/market_expansion/ops/dashboard/security_audit/contracts/provider_runner）与 ruff。
3. 真实探针复核（NO_PROXY=*、限时）：`python -m market_monitor.cli probe --provider akshare|baostock|joinquant|tushare`；港股/期货/同花顺用 market_expansion 探针；记录与交付文档一致的 FAILED/BLOCKED。
4. 判定：所有验收标准在本机有真实执行证据且无外部阻塞的任务 → 交付文档追加“独立验收（accept_data2）”章节并在 STATUS.md 置 ACCEPTED；存在外部阻塞的（110 快照 FAILED、111/112 缺凭据、113 不可达、120 三源门槛、122 真实包、600 港股 FAILED、602 同花顺 FAILED）→ 保持 ACCEPTANCE/BLOCKED 并写解除条件，不标 ACCEPTED。
5. 更新 STATUS.md 只动你负责的行与“当前入口”说明。最终答案一页验收汇总表（任务/结论/证据/解除条件）。
