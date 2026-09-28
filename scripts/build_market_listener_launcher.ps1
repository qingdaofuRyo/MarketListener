param(
    [ValidatePattern('^[A-Za-z][A-Za-z0-9._-]*$')]
    [string]$Name = "MarketListenerLauncher"
)

$ErrorActionPreference = "Stop"

$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $RepositoryRoot "desktop\.venv\Scripts\python.exe"
$Source = Join-Path $PSScriptRoot "market_listener_launcher.py"
$IconGenerator = Join-Path $PSScriptRoot "market_listener_icon.py"
$OutputDirectory = Join-Path $RepositoryRoot "artifacts\launcher"
$Icon = Join-Path $OutputDirectory "MarketListener.ico"
$WorkDirectory = Join-Path $OutputDirectory "build"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "项目虚拟环境不存在：$Python"
}
if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
    throw "启动器源码不存在：$Source"
}
if (-not (Test-Path -LiteralPath $IconGenerator -PathType Leaf)) {
    throw "图标生成脚本不存在：$IconGenerator"
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
& $Python $IconGenerator --output $Icon
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Icon -PathType Leaf)) {
    throw "无法生成启动器ICO图标：$Icon"
}
Push-Location $RepositoryRoot
try {
    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --icon $Icon `
        --hidden-import pystray._win32 `
        --name $Name `
        --distpath $OutputDirectory `
        --workpath $WorkDirectory `
        --specpath $OutputDirectory `
        $Source
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 构建失败，退出代码：$LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$Executable = Join-Path $OutputDirectory "$Name.exe"
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "构建结束但未找到可执行文件：$Executable"
}
Get-Item -LiteralPath $Executable | Select-Object FullName, Length, LastWriteTime
