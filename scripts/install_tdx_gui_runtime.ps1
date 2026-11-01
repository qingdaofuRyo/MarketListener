param(
    [string]$DataRoot = "data_control"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath $DataRoot).Path
$tools = Join-Path $root "tools"
$runtime = Join-Path $tools "python311-x86"
$python = Join-Path $runtime "python.exe"
$archive = Join-Path $tools "python-3.11.9-win32.zip"
$url = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-win32.zip"
# Official windows-3.11.9.json, pythoncore-3.11-32 entry.
$expectedSha256 = "339221fac02f0ad112640dbcd150d41f819b5990a7eb6e9ac766d07a6fd8f580"

New-Item -ItemType Directory -Force -Path $tools | Out-Null
if (-not (Test-Path -LiteralPath $archive)) {
    Invoke-WebRequest -Uri $url -OutFile $archive -TimeoutSec 180
}
$actualSha256 = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualSha256 -ne $expectedSha256) {
    throw "Official Python x86 archive SHA-256 mismatch"
}
if (-not (Test-Path -LiteralPath $python)) {
    Expand-Archive -LiteralPath $archive -DestinationPath $runtime -Force
}
$bits = & $python -c "import struct; print(struct.calcsize('P') * 8)"
if ($LASTEXITCODE -ne 0 -or $bits.Trim() -ne "32") {
    throw "TDX GUI runtime must be 32-bit Python"
}
& $python -m pip install --disable-pip-version-check "pywinauto==0.6.9" "pywin32==312" "comtypes==1.4.17" "six==1.17.0"
if ($LASTEXITCODE -ne 0) {
    throw "TDX GUI dependency installation failed"
}
& $python -c "import pywinauto, win32gui; print('TDX_GUI_RUNTIME_READY', pywinauto.__version__)"
if ($LASTEXITCODE -ne 0) {
    throw "TDX GUI runtime verification failed"
}
