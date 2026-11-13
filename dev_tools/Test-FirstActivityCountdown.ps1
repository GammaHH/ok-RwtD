$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Repository .venv Python missing; do not use the installed FARM interpreter for these checks.' }
$LogDir = Join-Path $Root ('logs\first-activity-countdown-' + (Get-Date -Format 'yyyyMMdd_HHmmss_ffff'))
if (Test-Path -LiteralPath $LogDir) { throw "Output exists: $LogDir" }
New-Item -ItemType Directory -Path $LogDir | Out-Null
$Stdout = Join-Path $LogDir 'unittest.stdout.log'
$Stderr = Join-Path $LogDir 'unittest.stderr.log'
$TestExitCode = $null
$TestCompleted = $false
$TestProcess = Start-Process -FilePath $Python -ArgumentList @('-m','unittest','-v',
    'test_activity_settings','test_farm_activity','test_farm_activity_task','test_activity_countdown_editor') `
    -Environment @{PYTHONPATH=(Join-Path $Root 'tests\rwtd'); QT_QPA_PLATFORM='offscreen'} `
    -WorkingDirectory $Root -Wait -PassThru -NoNewWindow `
    -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr
$TestCompleted = $TestProcess.HasExited
if ($TestCompleted) { $TestExitCode = $TestProcess.ExitCode }
Write-Host "Completed=$TestCompleted ExitCode=$TestExitCode Logs=$LogDir"
Get-Content -LiteralPath $Stdout -Tail 20
Get-Content -LiteralPath $Stderr -Tail 45
if (!$TestCompleted -or $TestExitCode -ne 0) { throw "First activity countdown checks failed. Logs=$LogDir" }
