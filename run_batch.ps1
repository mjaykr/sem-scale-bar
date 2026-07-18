param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$InputPath,

    [Parameter(Position = 1)]
    [string]$OutputPath = "publication_ready",

    [string]$OverridesCsv = "",
    [switch]$Recursive,
    [switch]$Overwrite,
    [switch]$FailFast
)

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$cliPath = Join-Path $scriptRoot "cli.py"
$cliArgs = @($cliPath, $InputPath, "--output", $OutputPath)

if ($Recursive) { $cliArgs += "--recursive" }
if ($Overwrite) { $cliArgs += "--overwrite" }
if ($FailFast) { $cliArgs += "--fail-fast" }
if ($OverridesCsv) { $cliArgs += @("--overrides-csv", $OverridesCsv) }

Set-Location -LiteralPath $scriptRoot
$venvPython = Join-Path $scriptRoot ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $venvPython) {
    & $venvPython @cliArgs
} else {
    & python @cliArgs
}
exit $LASTEXITCODE
