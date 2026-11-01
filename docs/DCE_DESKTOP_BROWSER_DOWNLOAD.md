# 大商所桌面浏览器批量排名 ZIP

这条路径用于大商所官网“日成交持仓排名”的**批量下载**，独立于雷电模拟器和逐合约授权 API。它只处理会员成交/持仓排名 ZIP，不替代日行情或结算参数采集。

1. 在平时能正常下载的可见 Edge 或 Chrome 会话中打开大商所官网排名页，选定交易日并点击“批量下载”。保留原 ZIP 文件，不重命名或重新压缩。
2. 完全退出用于下载的浏览器，让其 `History` 数据库释放锁。只读核验时需指定该浏览器实际 profile 的 `History` 路径；程序只查询目标下载文件记录，不读取浏览记录或凭据。若仍有后台进程占锁，命令返回 `DCE_DESKTOP_HISTORY_LOCKED`，不要使用 SQLite `immutable=1` 绕过。
3. 从仓库根目录运行以下示例，替换日期、文件和 profile 路径。先省略 `--capture` 做只读核验；确认 `VALIDATED` 后才加 `--capture` 保存独立 Bronze。

```powershell
$zipPath = 'C:\path\to\downloaded.zip'
$edgeHistory = Join-Path $env:LOCALAPPDATA 'Microsoft\Edge\User Data\Default\History'
$tradeDay = 'YYYY-MM-DD'
desktop\.venv\Scripts\python scripts\audit_dce_desktop_download.py --zip $zipPath --history $edgeHistory --browser edge --trade-date $tradeDay
desktop\.venv\Scripts\python scripts\audit_dce_desktop_download.py --zip $zipPath --history $edgeHistory --browser edge --trade-date $tradeDay --capture
```

Chrome 的 `--browser` 改为 `chrome`，`--history` 指向实际 Chrome profile。核验要求该文件的下载记录为完成状态，大小与时间匹配，标签页为官网排名页，下载 URL 来源为官网 Blob；ZIP 还须通过交易日、CRC、合约、三方向排名与大小限制。浏览器 History **不包含 ZIP 响应的哈希或 HTTP 状态**，因此这是“浏览器记录关联 + 原件内容校验”，不伪称抓到了服务端 HTTP 200 响应。

如果已有雷电同日 `PASS_RAW` 原件，捕获会先核验旧清单和 ZIP，再逐成员比较；内容一致返回 `SKIPPED_EXISTING_PASS`，内容不同则报冲突，不覆盖。新日期返回 `CAPTURED_RAW` 后，先只读检查离线导入；只有产品服务和授权采集均停止、确保单写者窗口时，才显式加 `--write` 写入独立来源：

```powershell
desktop\.venv\Scripts\python scripts\import_dce_desktop_batch_ranks.py --data-root data_control --start-day $tradeDay --end-day $tradeDay
desktop\.venv\Scripts\python scripts\import_dce_desktop_batch_ranks.py --data-root data_control --start-day $tradeDay --end-day $tradeDay --write
```

只读检查未通过时不要执行 `--write`。正式消费者只选择同一交易日一份完整排名来源，雷电 ZIP 优先于桌面 ZIP，桌面 ZIP 优先于逐合约授权 API；来源事实始终隔离保存。
