<#
.SYNOPSIS
Creates an isolated Python environment and installs SEM Ready.

.EXAMPLE
PowerShell -ExecutionPolicy Bypass -File .\install.ps1
#>
param([switch]$Launch)

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $scriptRoot

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    $pythonCommand = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $pythonCommand) {
    throw "Python 3.10 or newer is required. Install it from https://www.python.org/downloads/"
}

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    if ($pythonCommand.Name -eq "py") {
        & py -3 -m venv .venv
    } else {
        & python -m venv .venv
    }
}

$venvPython = Join-Path $scriptRoot ".venv\Scripts\python.exe"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -e .

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$pathEntries = @($userPath -split ";" | Where-Object { $_ })
if ($pathEntries -notcontains $scriptRoot) {
    $newUserPath = (($pathEntries + $scriptRoot) -join ";")
    [Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
}
if (($env:Path -split ";") -notcontains $scriptRoot) {
    $env:Path = "$scriptRoot;$env:Path"
}

Write-Host "Installation complete. Open a new terminal and run 'sem-ready' from any image folder."
Write-Host "Use .\run_sem_ready.bat to start the desktop interface."
if ($Launch) {
    & $venvPython app.py
}
