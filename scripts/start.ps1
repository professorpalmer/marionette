# Launch Marionette for daily use from this checkout (production renderer).
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $RepoRoot
$Py = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Py)) {
    Write-Error "No .venv found at $RepoRoot\.venv. Run scripts\install.ps1 first."
    exit 1
}

Set-Location (Join-Path $RepoRoot "webapp")

$dist = Join-Path $RepoRoot "webapp\dist\index.html"
if (-not (Test-Path $dist)) {
    Write-Host "Building renderer (first run)..."
    npm run build
}

$marker = Join-Path $env:USERPROFILE ".pmharness\backend.json"
if (Test-Path $marker) { Remove-Item $marker -Force -ErrorAction SilentlyContinue }

Write-Host "Launching Marionette..."
npm run electron
