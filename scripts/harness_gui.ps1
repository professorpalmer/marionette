# Launch the PM-Native Harness GUI. Default driver glm-5.2 needs OPENROUTER_API_KEY.
# For a no-key demo: $env:HARNESS_DRIVER = "stub-oracle-v2"; .\scripts\harness_gui.ps1
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
Set-Location $RepoRoot

$Port = if ($args.Count -gt 0) { $args[0] } else { "8799" }

$Py = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Py)) {
    Write-Error "No .venv found at $RepoRoot\.venv. Run scripts\install.ps1 first."
    exit 1
}

Write-Host "Launching Harness GUI on port $Port..."
& $Py -m harness.server $Port
exit $LASTEXITCODE