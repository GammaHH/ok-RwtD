# User-run, foreground only. Does not start OK or FARM.
[CmdletBinding()]
param([string]$AppDirectory = (Join-Path $env:LOCALAPPDATA 'ok-RwtD\data\apps\ok-RwtD'))
$ErrorActionPreference = 'Stop'
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
if (!$Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Open PowerShell 7 as administrator under the same Windows user, then run this command again. The launcher requires elevation.'
}
$AppDirectory = (Resolve-Path -LiteralPath $AppDirectory).Path
$AppFile = Join-Path $AppDirectory 'app.json'
$State = Get-Content -LiteralPath $AppFile -Raw | ConvertFrom-Json
if ($State.current_version -notin @('v1.1.7','v1.1.8')) { throw "Expected v1.1.7 or v1.1.8, found $($State.current_version)." }
$Launcher = Join-Path (Split-Path (Split-Path (Split-Path $AppDirectory -Parent) -Parent) -Parent) 'ok-RwtD.exe'
if (!(Test-Path -LiteralPath $Launcher -PathType Leaf)) { throw "Launcher missing: $Launcher" }
$Busy = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -and ($_.ExecutablePath.StartsWith($AppDirectory + '\',[StringComparison]::OrdinalIgnoreCase) -or $_.ExecutablePath.Equals($Launcher,[StringComparison]::OrdinalIgnoreCase))
})
if ($Busy.Count) { throw "Fully close installed OK and its launcher first. Process IDs: $($Busy.ProcessId -join ', ')" }
$LogDirectory = Join-Path (Split-Path $PSScriptRoot -Parent) ('logs\upgrade-118-' + (Get-Date -Format 'yyyyMMdd_HHmmss_ffff'))
if (Test-Path -LiteralPath $LogDirectory) { throw "Output exists: $LogDirectory" }
New-Item -ItemType Directory -Path $LogDirectory | Out-Null
Copy-Item -LiteralPath $AppFile -Destination (Join-Path $LogDirectory 'app.before.json')
Copy-Item -LiteralPath (Join-Path $AppDirectory 'working\requirements.txt') -Destination (Join-Path $LogDirectory 'requirements.before.txt')
if ($State.current_version -eq 'v1.1.7') {
    $Response = Join-Path $LogDirectory 'update-response.json'
    $UpdateExitCode = $null
    $UpdateCompleted = $false
    Write-Host "Updating source and dependencies to v1.1.8. Logs=$LogDirectory"
    $UpdateProcess = Start-Process -FilePath $Launcher -ArgumentList @('--update-to-version','v1.1.8','--response-file',('"' + $Response + '"')) -WorkingDirectory (Split-Path $Launcher -Parent) -Wait -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $LogDirectory 'update.stdout.log') -RedirectStandardError (Join-Path $LogDirectory 'update.stderr.log')
    $UpdateCompleted = $UpdateProcess.HasExited
    if ($UpdateCompleted) { $UpdateExitCode = $UpdateProcess.ExitCode }
    Write-Host "UpdateCompleted=$UpdateCompleted UpdateExitCode=$UpdateExitCode Logs=$LogDirectory"
    if (!$UpdateCompleted -or $UpdateExitCode -ne 0) { throw "Launcher update failed. Logs=$LogDirectory" }
    if (!(Test-Path -LiteralPath $Response -PathType Leaf)) { throw "Launcher did not produce a response. Logs=$LogDirectory" }
    $Result = Get-Content -LiteralPath $Response -Raw | ConvertFrom-Json
    if (!$Result.updated -or $Result.error) { throw "Launcher did not confirm update. Inspect $Response" }
}
$State = Get-Content -LiteralPath $AppFile -Raw | ConvertFrom-Json
if ($State.current_version -ne 'v1.1.8') { throw "Update metadata is not v1.1.8. Logs=$LogDirectory" }
# Replace the shared namespace as a complete coordinated set, never alongside minimal.
& (Join-Path $PSScriptRoot 'Repair-InstalledQt.ps1') -AppDirectory $AppDirectory -RequiredAppVersion 'v1.1.8' -OfficialQtVersion '6.12.0'
& (Join-Path $PSScriptRoot 'Repair-InstalledFramework.ps1') -AppDirectory $AppDirectory
Write-Host "Completed=True Version=v1.1.8 QtProvider=Official Qt=6.12.0 Logs=$LogDirectory"
Write-Host 'Local compatibility repair only: public v1.1.8 still declares minimal Qt. Reinstalling its dependencies may undo this repair. Full OK startup must be checked manually.'
