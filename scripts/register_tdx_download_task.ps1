[CmdletBinding(SupportsShouldProcess)]
param(
    [switch]$EnableAfterAcceptance
)
$ErrorActionPreference = 'Stop'
$repoPath = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonPath = Join-Path $repoPath 'desktop\.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Missing desktop virtual environment pythonw.exe'
}
if ((Get-TimeZone).Id -ne 'China Standard Time') {
    throw 'This schedule requires Windows China Standard Time (23:00 Beijing).'
}
$taskName = 'MarketListener-TDX-AfterHours'
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw 'Task already exists; inspect it before updating. This script never overwrites it.'
}
$accountName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$statePath = Join-Path $repoPath 'data_control\state\tdx_download_ui'
$arguments = '-m market_monitor.tdx_download_ui --execute --state-dir "{0}"' -f $statePath
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument $arguments -WorkingDirectory $repoPath
$daily = New-ScheduledTaskTrigger -Daily -At '23:00'
$logon = New-ScheduledTaskTrigger -AtLogOn -User $accountName
$logon.Delay = 'PT2M'
$principal = New-ScheduledTaskPrincipal -UserId $accountName -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 13) -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 15) -Disable:(-not $EnableAfterAcceptance)
$task = New-ScheduledTask -Action $action -Trigger @($daily, $logon) -Settings $settings `
    -Principal $principal -Description 'TDX six-batch download; no automatic login or database import. Requires desktop acceptance.'
if ($PSCmdlet.ShouldProcess($taskName, 'Register daily 23:00 and missed-run/logon task')) {
    Register-ScheduledTask -TaskName $taskName -InputObject $task | Select-Object TaskName, State
}
