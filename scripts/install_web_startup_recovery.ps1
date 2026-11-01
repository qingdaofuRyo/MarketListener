# Register a second, delayed current-user startup path for the local web services.
# The existing HKCU Run entry remains the first attempt. This task retries after
# logon or workstation unlock if that attempt did not leave both ports ready.
[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw '此启动恢复任务仅支持 Windows。' }

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$python = [System.IO.Path]::GetFullPath((Join-Path $projectRoot 'desktop\.venv\Scripts\pythonw.exe'))
$entry = [System.IO.Path]::GetFullPath((Join-Path $projectRoot 'scripts\start_marketlistener.py'))
if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or
    -not (Test-Path -LiteralPath $entry -PathType Leaf)) {
    throw '项目锁定的 Python 后台运行时或网页启动脚本不存在。'
}

$taskName = 'MarketListenerServicesRecovery'
$userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$arguments = '"' + $entry + '" --target none --startup-watchdog'
$escape = { param([string]$value) [Security.SecurityElement]::Escape($value) }
$xmlPython = & $escape $python
$xmlArgs = & $escape $arguments
$xmlRoot = & $escape $projectRoot
$xmlSid = & $escape $userSid
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Restore the current user's loopback MarketListener services after logon or unlock.</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><Delay>PT1M</Delay><UserId>$xmlSid</UserId></LogonTrigger>
    <SessionStateChangeTrigger><Enabled>true</Enabled><Delay>PT1M</Delay><UserId>$xmlSid</UserId><StateChange>SessionUnlock</StateChange></SessionStateChangeTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>$xmlSid</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <RestartOnFailure><Interval>PT5M</Interval><Count>3</Count></RestartOnFailure>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled><Hidden>true</Hidden>
    <ExecutionTimeLimit>PT3M</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author"><Exec><Command>$xmlPython</Command><Arguments>$xmlArgs</Arguments><WorkingDirectory>$xmlRoot</WorkingDirectory></Exec></Actions>
</Task>
"@

# Parse the task with Task Scheduler itself before changing any registered task.
$scheduler = New-Object -ComObject 'Schedule.Service'
$scheduler.Connect()
$definition = $scheduler.NewTask(0)
$definition.XmlText = $xml
if ($definition.Triggers.Count -ne 2 -or $definition.Actions.Count -ne 1) {
    throw '本机任务计划未能核验两个触发器与唯一启动动作。'
}

$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    if ($existing.Actions.Count -ne 1 -or
        $existing.Actions[0].Execute -ne $python -or
        $existing.Actions[0].Arguments -ne $arguments -or
        $existing.Actions[0].WorkingDirectory -ne $projectRoot) {
        throw '同名任务属于其他启动命令，拒绝覆盖。'
    }
}

if ($PSCmdlet.ShouldProcess("当前用户 $userSid 的 $taskName", '注册登录与解锁恢复任务')) {
    Register-ScheduledTask -TaskName $taskName -Xml $xml -Force | Out-Null
    $registered = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    if ($registered.Actions.Count -ne 1 -or
        $registered.Actions[0].Execute -ne $python -or
        $registered.Actions[0].Arguments -ne $arguments -or
        $registered.Triggers.Count -ne 2) {
        throw '任务计划注册后身份或触发器核验失败。'
    }
    Write-Output "STARTUP_RECOVERY_INSTALLED task=$taskName triggers=logon,unlock user=$userSid"
} else {
    Write-Output "STARTUP_RECOVERY_PREVIEW task=$taskName triggers=logon,unlock user=$userSid"
}
