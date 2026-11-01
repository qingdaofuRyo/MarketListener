[CmdletBinding(SupportsShouldProcess)]
param()
$ErrorActionPreference = "Stop"
$project = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$expectedScript = Join-Path $PSScriptRoot "serve.cmd"
$task = Get-ScheduledTask -TaskName "MarketListener_Serve" -ErrorAction SilentlyContinue
if ($task) {
    $known = @($task.Actions).Count -eq 1 -and
        $task.Actions[0].Execute.EndsWith("cmd.exe", [StringComparison]::OrdinalIgnoreCase) -and
        $task.Actions[0].Arguments.Contains($expectedScript)
    if (-not $known) { throw "旧服务任务的动作不符合本项目已知入口，停止迁移" }
    if ($PSCmdlet.ShouldProcess($task.TaskName, "停用旧脚本启动；保留任务以便恢复")) {
        Disable-ScheduledTask -TaskName $task.TaskName | Select-Object TaskName,State
    }
}
