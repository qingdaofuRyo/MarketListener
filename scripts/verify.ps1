<#
.SYNOPSIS
Runs the reproducible FULL-003 baseline checks.

.DESCRIPTION
The script validates the locked Python environment, style, shared JSON Schema
fixtures, the complete desktop test suite, and Android lint/tests/APK build.
Android Gradle work is run from a temporary subst drive because Gradle/JDK 21
cannot reliably load JVM test classes from this repository's Chinese path.

.PARAMETER SimulateFailure
Runs a harmless Python child process that exits 17. This is only for verifying
that a failed child command makes this script fail; it changes no repository
files and still releases any temporary resources.

.PARAMETER SimulateLockMismatch
Injects a temporary expected version for Ruff that disagrees with the lock
file. This verifies the offline lock-versus-installed version check and does
not modify the lock file, virtual environment, or repository.
#>
[CmdletBinding()]
param(
    [switch]$SimulateFailure,
    [switch]$SimulateLockMismatch
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$pythonExe = Join-Path $repoRoot "desktop\.venv\Scripts\python.exe"
$requirementsLock = Join-Path $repoRoot "desktop\requirements.lock"
$profileJdk = Join-Path $env:USERPROFILE ".jdks\jbr-21.0.11"
$jdkHome = if (Test-Path -LiteralPath (Join-Path $profileJdk "bin\java.exe")) {
    $profileJdk
} elseif ($env:JAVA_HOME) {
    $env:JAVA_HOME
} else {
    $profileJdk
}
$javaExe = Join-Path $jdkHome "bin\java.exe"
$originalJavaHome = $env:JAVA_HOME
$originalPath = $env:PATH
$originalLocation = Get-Location
$substDrive = $null
$substMounted = $false
$pushedLocation = $false
$lockVerifierFile = $null
$exitCode = 0

function Invoke-ExternalStep {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )

    Write-Host "`n== $Name =="
    & $Action
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE."
    }
}

function Get-FreeSubstDrive {
    foreach ($letter in @("M", "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z")) {
        $candidate = "${letter}:"
        if (-not (Test-Path "${candidate}\")) {
            return $candidate
        }
    }

    throw "No free drive letter is available for the temporary Android build mapping."
}

$lockVerifier = @'
from __future__ import annotations

import re
import sys
from importlib.metadata import distributions
from pathlib import Path
from packaging.requirements import Requirement


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(path: Path) -> dict[str, str]:
    expected: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            requirement = Requirement(line)
        except ValueError:
            raise SystemExit(
                f"requirements lock contains a non-exact or unparseable entry at line {line_number}: {raw_line!r}"
            )
        specs = list(requirement.specifier)
        if requirement.url or requirement.extras or len(specs) != 1 or specs[0].operator != "==" or "*" in specs[0].version:
            raise SystemExit(
                f"requirements lock contains a non-exact or unparseable entry at line {line_number}: {raw_line!r}"
            )
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        name, version = normalize(requirement.name), specs[0].version
        previous = expected.get(name)
        if previous is not None and previous != version:
            raise SystemExit(f"requirements lock has conflicting versions for {name}: {previous} and {version}")
        expected[name] = version
    if not expected:
        raise SystemExit("requirements lock has no exact package entries")
    return expected


lock_path = Path(sys.argv[1])
expected = parse_lock(lock_path)
if len(sys.argv) == 3:
    override_name, override_version = sys.argv[2].split("==", 1)
    expected[normalize(override_name)] = override_version

installed: dict[str, str] = {}
for distribution in distributions():
    name = distribution.metadata.get("Name")
    if name:
        installed[normalize(name)] = distribution.version

missing = sorted(name for name in expected if name not in installed)
mismatched = sorted(
    f"{name}: expected {expected[name]}, installed {installed[name]}"
    for name in expected
    if name in installed and expected[name] != installed[name]
)
if missing or mismatched:
    details = []
    if missing:
        details.append("missing: " + ", ".join(missing))
    if mismatched:
        details.append("version mismatch: " + "; ".join(mismatched))
    raise SystemExit("locked dependency verification failed: " + " | ".join(details))

print(f"locked dependency versions match {lock_path.name}: {len(expected)} exact entries checked")
'@

try {
    if (-not (Test-Path -LiteralPath $pythonExe)) {
        throw "Locked Python virtual environment is missing at $pythonExe. Create it and install desktop[dev] with desktop/requirements.lock."
    }
    if (-not (Test-Path -LiteralPath $requirementsLock)) {
        throw "Python requirements lock is missing at $requirementsLock."
    }
    if (-not (Test-Path -LiteralPath $javaExe)) {
        throw "Required JDK 21 is missing at $jdkHome."
    }

    $pythonVersion = & $pythonExe --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to execute the locked Python interpreter at $pythonExe."
    }
    Write-Host "== Python runtime =="
    Write-Host $pythonVersion
    if (($pythonVersion -join " ") -notmatch "Python 3\.11\.0") {
        throw "Expected Python 3.11.0, got: $($pythonVersion -join ' ')"
    }

    # java -version writes its banner to stderr. Route it through cmd so
    # PowerShell 5.1 captures it as normal output rather than a terminating error.
    $javaVersion = & cmd.exe /d /c "`"$javaExe`" -version 2>&1"
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to execute the required JDK at $javaExe."
    }
    Write-Host "`n== JDK runtime =="
    $javaVersion | ForEach-Object { Write-Host $_ }
    if (($javaVersion -join " ") -notmatch 'version "21\.') {
        throw "Expected JDK 21, got: $($javaVersion -join ' ')"
    }
    $env:JAVA_HOME = $jdkHome
    $env:PATH = "$jdkHome\bin;$originalPath"

    Invoke-ExternalStep "Python dependency health" {
        & $pythonExe -m pip check
    }
    $lockVerifierFile = Join-Path ([System.IO.Path]::GetTempPath()) ("market-monitor-lock-verifier-" + [System.Guid]::NewGuid().ToString("N") + ".py")
    [System.IO.File]::WriteAllText($lockVerifierFile, $lockVerifier, [System.Text.UTF8Encoding]::new($false))
    $lockVerifierArguments = @($lockVerifierFile, $requirementsLock)
    if ($SimulateLockMismatch) {
        $lockVerifierArguments += "ruff==0.0.0-controlled-mismatch"
    }
    Invoke-ExternalStep "Locked Python dependency versions" {
        & $pythonExe @lockVerifierArguments
    }
    Invoke-ExternalStep "Ruff static analysis" {
        & $pythonExe -m ruff check (Join-Path $repoRoot "desktop\src") (Join-Path $repoRoot "desktop\tests")
    }
    Invoke-ExternalStep "Shared JSON Schema fixtures" {
        & $pythonExe -m pytest (Join-Path $repoRoot "desktop\tests\test_contracts.py") -q
    }
    Invoke-ExternalStep "Desktop pytest suite" {
        & $pythonExe -m pytest (Join-Path $repoRoot "desktop\tests") -q
    }

    if ($SimulateFailure) {
        Invoke-ExternalStep "Controlled child-process failure" {
            & $pythonExe -c "import sys; sys.exit(17)"
        }
    }

    $substDrive = Get-FreeSubstDrive
    Write-Host "`n== Android path preparation =="
    & subst $substDrive $repoRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to map $repoRoot to $substDrive."
    }
    $substMounted = $true
    $mappedRoot = "${substDrive}\"
    $androidRoot = Join-Path $mappedRoot "android"
    $gradlew = Join-Path $androidRoot "gradlew.bat"
    if (-not (Test-Path -LiteralPath $gradlew)) {
        throw "Gradle wrapper is missing at $gradlew."
    }
    Push-Location $mappedRoot
    $pushedLocation = $true

    Invoke-ExternalStep "Android lintDebug (JDK 21, temporary $substDrive mapping)" {
        & $gradlew -p $androidRoot lintDebug --no-daemon
    }
    Invoke-ExternalStep "Android testDebugUnitTest (JDK 21, temporary $substDrive mapping)" {
        & $gradlew -p $androidRoot testDebugUnitTest --no-daemon
    }
    Invoke-ExternalStep "Android assembleDebug (JDK 21, temporary $substDrive mapping)" {
        & $gradlew -p $androidRoot assembleDebug --no-daemon
    }

    Write-Host "`nFULL-003 baseline verification passed."
}
catch {
    $exitCode = 1
    Write-Error $_
}
finally {
    if ($pushedLocation) {
        Pop-Location
    }
    else {
        Set-Location $originalLocation
    }

    if ($substMounted) {
        & subst $substDrive /D
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Failed to remove temporary subst mapping $substDrive."
            $exitCode = 1
        }
    }

    if ($lockVerifierFile -and (Test-Path -LiteralPath $lockVerifierFile)) {
        try {
            Remove-Item -LiteralPath $lockVerifierFile -Force -ErrorAction Stop
        }
        catch {
            Write-Error "Failed to remove temporary lock verifier ${lockVerifierFile}: $($_.Exception.Message)"
            $exitCode = 1
        }
    }

    $env:JAVA_HOME = $originalJavaHome
    $env:PATH = $originalPath
}

exit $exitCode
