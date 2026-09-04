# Replace-the-running-instance dev launcher (Vite hot-reload).
# For side-by-side feature inspection without touching production, use
# scripts/inspect.ps1.
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $RepoRoot

Set-Location (Join-Path $RepoRoot "webapp")

# Clean stale dev stack.
Get-Process -Name "electron" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
$marker = Join-Path $env:USERPROFILE ".pmharness\backend.json"
if (Test-Path $marker) { Remove-Item $marker -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1

Write-Host "Launching Marionette (dev)..."
npm run electron:dev
