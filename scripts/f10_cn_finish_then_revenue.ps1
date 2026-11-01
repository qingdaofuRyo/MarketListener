$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$cnDir = Join-Path $root 'data_control\f10\cn'
$logsDir = Join-Path $root 'data_control\f10\logs'
$detailsPath = Join-Path $cnDir 'details_20260809.jsonl'
$statePath = Join-Path $cnDir 'state.json'
$marker = Join-Path $logsDir 'revenue_cn.running'
$revenueLog = Join-Path $logsDir 'revenue_cn.log'
$revenueErr = Join-Path $logsDir 'revenue_cn.err.log'
$doneMarker = Join-Path $logsDir 'revenue_cn.done'

New-Item -ItemType Directory -Path $logsDir -Force | Out-Null

if (Test-Path $doneMarker) {
    Add-Content -Path $revenueLog -Value ("[{0}] revenue already finished, skip" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
    exit 0
}
if (Test-Path $marker) {
    Add-Content -Path $revenueLog -Value ("[{0}] revenue already running, skip" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
    exit 0
}

Set-Content -Path $marker -Value (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')

function Get-DetailsCount {
    if (Test-Path $detailsPath) {
        try { return (Get-Content $detailsPath).Count } catch { return -1 }
    }
    return -1
}

$deadline = (Get-Date).AddHours(2)
$workerPid = 30120

Add-Content -Path $revenueLog -Value ("[{0}] waiting for CN F10 survey to finish (worker pid {1})" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $workerPid)

# 1) Wait for the CN survey worker to exit, with a count sanity check.
while ((Get-Date) -lt $deadline) {
    $proc = Get-Process -Id $workerPid -ErrorAction SilentlyContinue
    $count = Get-DetailsCount
    if (-not $proc) {
        Add-Content -Path $revenueLog -Value ("[{0}] CN survey worker exited; details count={1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $count)
        break
    }
    if ($count -ge 5539) {
        Add-Content -Path $revenueLog -Value ("[{0}] CN details reached 5539; waiting for worker exit" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
    }
    Start-Sleep -Seconds 15
}

if ((Get-Date) -ge $deadline) {
    Add-Content -Path $revenueLog -Value ("[{0}] ERROR: timed out waiting for CN survey" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
    exit 2
}

# 2) Give the final state/export a few seconds to settle, then verify.
Start-Sleep -Seconds 8
$count = Get-DetailsCount
$doneCount = -1
if (Test-Path $statePath) {
    try {
        $state = Get-Content -Raw $statePath | ConvertFrom-Json
        $doneCount = $state.done.Count
    } catch {
        Add-Content -Path $revenueLog -Value ("[{0}] WARN: state.json read failed: {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $_.Exception.Message)
    }
}
Add-Content -Path $revenueLog -Value ("[{0}] CN survey verified: details={1} state.done={2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $count, $doneCount)

if ($count -lt 5539) {
    Add-Content -Path $revenueLog -Value ("[{0}] ERROR: CN details incomplete ({1}/5539); revenue fetch NOT started" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $count)
    exit 3
}

# 3) Run the serial revenue-only fetch (about 1 s per company), then export atlas F10.
$venvPython = Join-Path $root 'desktop\.venv\Scripts\python.exe'
$pythonArgs = @(
    '-m', 'market_monitor',
    'f10',
    '--data-root', 'data_control',
    '--market', 'CN',
    '--revenue-only',
    '--revenue-limit', '6000',
    '--detail-delay-seconds', '1.0'
)

Add-Content -Path $revenueLog -Value ("[{0}] starting revenue-only fetch" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
$proc = Start-Process -FilePath $venvPython `
    -ArgumentList $pythonArgs `
    -WorkingDirectory $root `
    -RedirectStandardOutput $revenueLog `
    -RedirectStandardError $revenueErr `
    -PassThru `
    -WindowStyle Hidden
$proc.WaitForExit()
Add-Content -Path $revenueLog -Value ("[{0}] revenue fetch exit={1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $proc.ExitCode)

if ($proc.ExitCode -eq 0) {
    Set-Content -Path $doneMarker -Value (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
}
Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue
exit $proc.ExitCode
