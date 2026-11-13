# Run manually in PowerShell 7 after fully closing the installed application.
[CmdletBinding()]
param(
    [string]$AppDirectory = (Join-Path $env:LOCALAPPDATA 'ok-RwtD\data\apps\ok-RwtD'),
    [ValidateSet('6.12.0')][string]$OfficialQtVersion,
    [string]$RequiredAppVersion
)
$ErrorActionPreference = 'Stop'
$AppDirectory = (Resolve-Path -LiteralPath $AppDirectory).Path
$Python = Join-Path $AppDirectory 'python\python.exe'
$Site = Join-Path $AppDirectory 'python\Lib\site-packages'
$Requirements = Join-Path $AppDirectory 'working\requirements.txt'
if (!(Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Python not found: $Python" }
if ($RequiredAppVersion) {
    $AppState = Get-Content -LiteralPath (Join-Path $AppDirectory 'app.json') -Raw | ConvertFrom-Json
    if ($AppState.current_version -ne $RequiredAppVersion) { throw "Expected $RequiredAppVersion, found $($AppState.current_version)." }
}
$Pins = @{}
foreach ($Line in Get-Content -LiteralPath $Requirements) {
    if ($Line -match '^(PySide6|PySide6[-_]Addons|PySide6[-_]Essentials|shiboken6)==([^;\s]+)\s*$') {
        $Pins[$Matches[1].Replace('_','-').ToLowerInvariant()] = $Matches[2]
    }
}
$Names = @('pyside6','pyside6-addons','pyside6-essentials','shiboken6')
if ($OfficialQtVersion) {
    if ($RequiredAppVersion -ne 'v1.1.8') { throw 'The explicit Qt replacement is only approved here for v1.1.8.' }
    foreach ($Name in $Names) { $Pins[$Name] = $OfficialQtVersion }
}
foreach ($Name in $Names) {
    if (!$Pins.ContainsKey($Name)) { throw "Missing exact official Qt pin: $Name. This helper targets the official-PySide requirements, not v1.1.8 minimal Qt." }
}
if (@($Pins.Values | Sort-Object -Unique).Count -ne 1) { throw 'Qt and shiboken pins must match.' }
function Assert-AppClosed {
    $Busy = @(Get-CimInstance Win32_Process | Where-Object {
        ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($AppDirectory + '\', [StringComparison]::OrdinalIgnoreCase)) -or
        ($_.CommandLine -and $_.CommandLine.Contains((Join-Path $AppDirectory 'working\main.py'))) -or
        ($_.CommandLine -and $_.CommandLine.Contains((Join-Path $AppDirectory 'working\web_main.py')))
    })
    if ($Busy.Count) { throw "Close installed OK completely first. Process IDs: $($Busy.ProcessId -join ', ')" }
}
Assert-AppClosed
$LogDirectory = Join-Path (Split-Path $PSScriptRoot -Parent) ('logs\qt-repair-' + (Get-Date -Format 'yyyyMMdd_HHmmss_ffff'))
if (Test-Path -LiteralPath $LogDirectory) { throw "Output already exists: $LogDirectory" }
New-Item -ItemType Directory -Path $LogDirectory | Out-Null
$Wheels = Join-Path $LogDirectory 'wheels'
$Backup = Join-Path $LogDirectory 'backup'
New-Item -ItemType Directory -Path $Wheels,$Backup | Out-Null
Copy-Item -LiteralPath $Requirements -Destination (Join-Path $Backup 'requirements.txt')
function Invoke-RepairPython([string]$Stage, [string[]]$Arguments) {
    $RunExitCode = $null
    $RunCompleted = $false
    $Stdout = Join-Path $LogDirectory "$Stage.stdout.log"
    $Stderr = Join-Path $LogDirectory "$Stage.stderr.log"
    # Each argument is quoted; all generated arguments have no embedded quotes.
    $Quoted = foreach ($Argument in $Arguments) {
        if ($Argument.Contains('"')) { throw 'Unexpected quote in process argument.' }
        '"' + $Argument + '"'
    }
    Write-Host "Running $Stage. Logs=$LogDirectory"
    $RunProcess = Start-Process -FilePath $Python -ArgumentList $Quoted -Wait -PassThru -NoNewWindow -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr
    $RunCompleted = $RunProcess.HasExited
    if ($RunCompleted) { $RunExitCode = $RunProcess.ExitCode }
    Write-Host "Stage=$Stage Completed=$RunCompleted ExitCode=$RunExitCode"
    if (!$RunCompleted -or $RunExitCode -ne 0) {
        Get-Content -LiteralPath $Stderr -Tail 35
        throw "$Stage failed; stop here. Logs=$LogDirectory"
    }
}
$Packages = @($Names | ForEach-Object { "$_==$($Pins[$_])" })
Invoke-RepairPython 'inventory' @('-m','pip','list','--format=json')
# Download before touching the current environment. No automatic retries.
Invoke-RepairPython 'download' (@('-m','pip','download','--retries','0','--only-binary=:all:','--no-deps','--dest',$Wheels) + $Packages)
if (@(Get-ChildItem -LiteralPath $Wheels -Filter '*.whl').Count -ne 4) { throw 'Expected four Qt wheels; environment not changed.' }
Assert-AppClosed
$Affected = @(Get-ChildItem -LiteralPath $Site | Where-Object {
    $_.Name -in @('PySide6','shiboken6') -or
    $_.Name -match '^(ok_pyside_minimal|pyside6|pyside6_addons|pyside6_essentials|shiboken6)-.+\.dist-info$'
})
foreach ($Item in $Affected) { Copy-Item -LiteralPath $Item.FullName -Destination $Backup -Recurse }
Invoke-RepairPython 'uninstall' @('-m','pip','uninstall','-y','ok-pyside-minimal','PySide6','PySide6-Addons','PySide6-Essentials','shiboken6')
$Leftovers = Join-Path $LogDirectory 'quarantined-leftovers'
New-Item -ItemType Directory -Path $Leftovers | Out-Null
foreach ($Item in $Affected) {
    if (Test-Path -LiteralPath $Item.FullName) {
        $Resolved = (Resolve-Path -LiteralPath $Item.FullName).Path
        if (!(Split-Path $Resolved -Parent).Equals($Site, [StringComparison]::OrdinalIgnoreCase)) { throw "Unexpected cleanup target: $Resolved" }
        Move-Item -LiteralPath $Resolved -Destination $Leftovers
    }
}
Invoke-RepairPython 'install' (@('-m','pip','install','--no-index','--find-links',$Wheels,'--no-deps') + $Packages)
$Validation = Join-Path $LogDirectory 'validate_qt.py'
@'
import importlib.metadata as metadata
from pathlib import Path
import sys
from PySide6 import QtCore, QtGui, QtWidgets, QtSvg, QtSvgWidgets, QtNetwork, QtTest
expected = sys.argv[1]
for package in ('PySide6', 'PySide6-Addons', 'PySide6-Essentials', 'shiboken6'):
    actual = metadata.version(package)
    print(f'{package}={actual}', flush=True)
    if actual != expected:
        raise RuntimeError(f'{package}: expected {expected}, got {actual}')
if QtCore.qVersion() != expected:
    raise RuntimeError(f'Qt DLL version {QtCore.qVersion()} != {expected}')
app = QtWidgets.QApplication([])
widget = QtSvgWidgets.QSvgWidget()
print(f'QT_IMPORT_AND_WIDGET_OK Qt={QtCore.qVersion()} Path={Path(QtCore.__file__)}', flush=True)
try:
    metadata.version('ok-pyside-minimal')
except metadata.PackageNotFoundError:
    print('NO_MINIMAL_PROVIDER_OK', flush=True)
else:
    raise RuntimeError('Conflicting minimal provider is still installed')
'@ | Set-Content -LiteralPath $Validation -Encoding utf8
Invoke-RepairPython 'validate' @($Validation,$Pins['pyside6'])
Get-Content -LiteralPath (Join-Path $LogDirectory 'validate.stdout.log')
Write-Host "Completed=True Qt=$($Pins['pyside6']) Logs=$LogDirectory"
Write-Host 'Qt import/widget validation passed. Full OK startup and FARM have not been tested. User data and working/configs were not modified.'
