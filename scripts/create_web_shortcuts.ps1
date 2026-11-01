param(
    [switch]$Desktop,
    [string]$Destination
)
$ErrorActionPreference = "Stop"
$project = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $project "desktop\.venv\Scripts\pythonw.exe"
$bootstrap = Join-Path $PSScriptRoot "start_marketlistener.py"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "项目Python环境尚未安装" }
& node (Join-Path $project "desktop\admin-web\build.mjs")
if ($LASTEXITCODE -ne 0) { throw "管理台网页构建失败" }
if (-not $Destination) {
    $Destination = if ($Desktop) { [Environment]::GetFolderPath("Desktop") } else { Join-Path $project "artifacts\launch" }
}
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
$iconDir = Join-Path $project "artifacts\launch"
New-Item -ItemType Directory -Path $iconDir -Force | Out-Null
$icon = Join-Path $iconDir "MarketListener.ico"
& (Join-Path $project "desktop\.venv\Scripts\python.exe") (Join-Path $PSScriptRoot "market_listener_icon.py") --output $icon
if ($LASTEXITCODE -ne 0) { throw "图标生成失败" }
$shell = New-Object -ComObject WScript.Shell
foreach ($entry in @(@("MarketListener 管理台", "admin"), @("MarketListener 客户端", "client"))) {
    $shortcutPath = Join-Path $Destination ($entry[0] + ".lnk")
    if (Test-Path -LiteralPath $shortcutPath) {
        $existing = $shell.CreateShortcut($shortcutPath)
        if (-not $existing.TargetPath.StartsWith($project, [StringComparison]::OrdinalIgnoreCase)) {
            throw "同名快捷方式不属于当前项目：$shortcutPath"
        }
    }
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $python
    $shortcut.Arguments = ('"{0}" --target {1}' -f $bootstrap, $entry[1])
    $shortcut.WorkingDirectory = $project
    $shortcut.IconLocation = $icon + ",0"
    $shortcut.Description = "启动或复用MarketListener本机网页服务"
    $shortcut.Save()
}
Get-ChildItem -LiteralPath $Destination -Filter "MarketListener *.lnk" | Select-Object FullName
