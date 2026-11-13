# User-run repair for the pre-Qt3 framework left in the installed working directory.
[CmdletBinding()]
param([string]$AppDirectory = (Join-Path $env:LOCALAPPDATA 'ok-RwtD\data\apps\ok-RwtD'))
$ErrorActionPreference = 'Stop'
$AppDirectory = (Resolve-Path -LiteralPath $AppDirectory).Path
$Working = Join-Path $AppDirectory 'working'
$Python = Join-Path $AppDirectory 'python\python.exe'
$Site = Join-Path $AppDirectory 'python\Lib\site-packages'
$State = Get-Content -LiteralPath (Join-Path $AppDirectory 'app.json') -Raw | ConvertFrom-Json
if ($State.current_version -notin @('v1.1.8', 'v1.1.9')) { throw 'This repair requires installed v1.1.8 or v1.1.9.' }
if (!(Test-Path -LiteralPath (Join-Path $Site 'ok\ui\qt\debug\diagnosis_page.py') -PathType Leaf)) {
    throw 'The installed site-packages framework lacks the Qt3 diagnosis page. Do not move the old framework.'
}
$Busy = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -and $_.ExecutablePath.StartsWith($AppDirectory + '\',[StringComparison]::OrdinalIgnoreCase)
})
if ($Busy.Count) { throw "Close OK completely first. Process IDs: $($Busy.ProcessId -join ', ')" }
$LogDirectory = Join-Path (Split-Path $PSScriptRoot -Parent) ('logs\framework-repair-' + (Get-Date -Format 'yyyyMMdd_HHmmss_ffff'))
if (Test-Path -LiteralPath $LogDirectory) { throw "Output exists: $LogDirectory" }
New-Item -ItemType Directory -Path $LogDirectory | Out-Null
$Legacy = Join-Path $Working 'ok'
if (Test-Path -LiteralPath $Legacy) {
    $Resolved = (Resolve-Path -LiteralPath $Legacy).Path
    if (!(Split-Path $Resolved -Parent).Equals($Working,[StringComparison]::OrdinalIgnoreCase)) { throw "Unexpected framework path: $Resolved" }
    $Link = Get-Item -LiteralPath $Resolved
    if ($Link.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Refusing to move a linked framework directory.' }
    # Retain the entire old framework outside Python's import search path.
    Move-Item -LiteralPath $Resolved -Destination (Join-Path $LogDirectory 'legacy-working-ok')
    Write-Host "Legacy framework backed up to $LogDirectory\legacy-working-ok"
}
$Check = Join-Path $LogDirectory 'validate_framework.py'
@'
from pathlib import Path
import importlib
import importlib.metadata as metadata
import sys
working, site = map(Path, sys.argv[1:3])
sys.path.insert(0, str(working))
import ok
actual = Path(ok.__file__).resolve()
expected = (site / 'ok').resolve()
if not actual.is_relative_to(expected):
    raise RuntimeError(f'Unexpected framework provider: {actual}; expected {expected}')
version = metadata.version('ok-script')
if version != '3.0.0b3':
    raise RuntimeError(f'Unexpected framework version: {version}')
print(f'FRAMEWORK_PROVIDER_OK Version={version} Path={actual}', flush=True)
from PySide6 import QtCore, QtSvgWidgets
from ok.ui.qt.debug.diagnosis_page import DiagnosisPage, MAX_SHOTS, Shot, list_shots
from src.config import config
for module, name in config.get('custom_tabs', []):
    getattr(importlib.import_module(module), name)
    print(f'CUSTOM_TAB_IMPORT_OK {module}.{name}', flush=True)
module, name = config['my_app']
getattr(importlib.import_module(module), name)
print(f'GLOBALS_IMPORT_OK {module}.{name}', flush=True)
print(f'QT_FRAMEWORK_IMPORT_OK Qt={QtCore.qVersion()}', flush=True)
'@ | Set-Content -LiteralPath $Check -Encoding utf8
$RunExitCode = $null
$RunCompleted = $false
$Stdout = Join-Path $LogDirectory 'validation.stdout.log'
$Stderr = Join-Path $LogDirectory 'validation.stderr.log'
$Arguments = @($Check,$Working,$Site) | ForEach-Object {
    if ($_.Contains('"')) { throw 'Unexpected quote in argument.' }
    '"' + $_ + '"'
}
$RunProcess = Start-Process -FilePath $Python -ArgumentList $Arguments -WorkingDirectory $Working -Wait -PassThru -NoNewWindow -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr
$RunCompleted = $RunProcess.HasExited
if ($RunCompleted) { $RunExitCode = $RunProcess.ExitCode }
Get-Content -LiteralPath $Stdout
Write-Host "Completed=$RunCompleted ExitCode=$RunExitCode Logs=$LogDirectory"
if (!$RunCompleted -or $RunExitCode -ne 0) {
    Get-Content -LiteralPath $Stderr -Tail 45
    throw "Framework validation failed. Original framework is retained in $LogDirectory. Do not launch FARM yet."
}
Write-Host 'Framework and custom-page imports passed. Full application startup still requires a manual check. No phone or FARM operations were started.'
