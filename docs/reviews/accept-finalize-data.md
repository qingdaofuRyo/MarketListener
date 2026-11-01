# accept_data2 数据链验收落盘指令（第二轮，2026-08-06）

你是独立验收 Agent（accept_data2）。项目根目录：C:\Users\<user>\Documents\阅读行情监控和产业链图谱项目。
只做验收记录与 STATUS 更新，不修改实现代码。

## BaoStock 分类核实结论（本轮已由 root 复核，请直接采信并落盘）

你上一轮结论写的是“baostock 当前为 CONFIGURATION_BLOCKED”，该结论与最新证据不符，请按下述事实修正：

1. 最新真实探针报告 `reports/provider-capabilities.json`（2026-08-06 02:09:16 生成）显示
   BaoStock 仅一个运行级能力 `provider-run-error`，status=`FAILED`、error.category=`NETWORK`、
   message=`网络接收错误。` —— 与交付记录 `docs/deliveries/FULL-113.md` 的 FAILED/NETWORK 分类一致。
2. 代码事实（`desktop/src/market_monitor/cli.py` 第 77-83 行）：CLI 的 `CONFIGURATION_BLOCKED`
   只在“所有能力 status 均为 BLOCKED”时输出；BaoStock 适配器没有任何
   `missing_configuration_requirements`（见 `desktop/src/market_monitor/providers/baostock.py`），
   其错误由 `ProbeRunner._run_error_capability` 固定标记为 FAILED，因此 BaoStock 不可能产生
   CONFIGURATION_BLOCKED。`CONFIGURATION_BLOCKED` 仅适用于缺本地凭据的 JQData/Tushare
   （`reports/full111-cli-check`、`reports/full112-cli-check` 可复核）。
3. 可选的再次实测（若你想自己确认，限时 90 秒）：
   `desktop\.venv\Scripts\python.exe -m market_monitor.cli probe --provider baostock --report-dir reports\baostock-accept-final --timeout-seconds 30`
   预期：JSON 报告 status=FAILED/NETWORK，CLI 退出码 2（PARTIAL_FAILURE）。

## 需要你完成的落盘产物

1. 新建 `docs/reviews/acceptance-data.md`：逐任务验收汇总表（任务/结论/证据/解除条件），
   并写明本次 BaoStock 分类核实过程与结论。
2. 给数据链 19 个交付文档（FULL-100/101/110/111/112/113/120/121/122/200/201/202/203/204/600/601/602/800/801/802）
   追加“独立验收（accept_data2）”章节（已在 STATUS 为 ACCEPTED 的项记录本机重跑证据；
   外部阻塞项记录真实状态与解除条件）。
3. 更新 STATUS.md 只动数据链行与“当前入口”：
   - 保持 ACCEPTED：FULL-100/101/121/200/201/202/203/204/601/801/802（现状已 ACCEPTED，补“独立验收”说明）。
   - 改为 ACCEPTANCE（审查通过、外部条件未满足）：FULL-110/111/112/113/120/122/600/602/800，
     解除条件照抄现有行说明（东财断连/缺凭据/BaoStock 不可达/三源门槛/真实包/港股/同花顺/连续夜间运行）。
   - 不要动 FULL-123/300-303/404/504/704（accept_android2 已处理）。
   - FULL-610/804 保持 BLOCKED；FULL-803/900 不要动。
4. 最终答案给一页验收汇总表。

请勿重跑 `scripts/verify.ps1`（root 即将为 FULL-803 跑全量回归，避免并发污染）；专项 pytest 与
真实探针可以跑，但优先把上述文档落盘。
