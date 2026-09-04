# Side-by-side fixture inspect launcher. See scripts/inspect.sh.
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $RepoRoot

$hasher = [System.Security.Cryptography.SHA256]::Create()
$bytes = [System.Text.Encoding]::UTF8.GetBytes($RepoRoot)
$slug = ([System.BitConverter]::ToString($hasher.ComputeHash($bytes)) -replace "-", "").Substring(0, 12).ToLowerInvariant()
$inspectRoot = Join-Path $env:USERPROFILE ".pmharness\inspect\$slug"

$env:HARNESS_INSPECT = "1"
$env:HARNESS_STATE_DIR = Join-Path $inspectRoot "state"
$env:HARNESS_USER_DATA_DIR = Join-Path $inspectRoot "user-data"
New-Item -ItemType Directory -Force -Path $env:HARNESS_STATE_DIR, $env:HARNESS_USER_DATA_DIR | Out-Null

Set-Location (Join-Path $RepoRoot "webapp")
Write-Host "Launching Marionette inspect (isolated state, no production credentials)..."
Write-Host "  HARNESS_STATE_DIR=$($env:HARNESS_STATE_DIR)"
npm run electron:inspect
