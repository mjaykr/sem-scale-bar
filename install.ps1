<#
.SYNOPSIS
Creates an isolated Python environment and installs SEMfig.

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

$desktopPath = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktopPath "SEMfig GUI.lnk"
$iconPath = Join-Path $scriptRoot "assets\semfig.ico"
$launcherPath = Join-Path $scriptRoot "run_semfig.bat"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $launcherPath
$shortcut.WorkingDirectory = $scriptRoot
$shortcut.Description = "Turn raw SEM images into publication-ready figures"
if (Test-Path -LiteralPath $iconPath) {
    $shortcut.IconLocation = "$iconPath,0"
}
$shortcut.Save()

Write-Host "Installation complete. Open a new terminal and run 'semfig' from any image folder."
Write-Host "Double-click 'SEMfig GUI' on the desktop to start the graphical interface."
if ($Launch) {
    & $venvPython app.py
}
