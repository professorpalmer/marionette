# Marionette one-line installer (Windows x64 + ARM64).
#
#   irm https://professorpalmer.github.io/marionette/install.ps1 | iex
#   irm https://raw.githubusercontent.com/professorpalmer/marionette/main/scripts/install.ps1 | iex
#
# Marionette runs from source (the Hermes model): this clones the repo, builds a
# per-machine Python venv with `uv`, installs node deps, builds the renderer, and
# drops `marionette` launchers on your PATH. Re-running is safe: it fast-forwards
# an existing checkout and refreshes the venv + renderer build in place.
$ErrorActionPreference = "Stop"

function Say([string]$Msg) { Write-Host ""; Write-Host "== $Msg ==" }
function Step([string]$Msg) { Write-Host "  -> $Msg" }
function Warn([string]$Msg) { Write-Host "WARN: $Msg" -ForegroundColor Yellow }
function Die([string]$Msg) { Write-Host "ERROR: $Msg" -ForegroundColor Red; exit 1 }

# --- load version pins -------------------------------------------------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VersionsFile = Join-Path $ScriptDir "versions.env"
if (Test-Path $VersionsFile) {
    Get-Content $VersionsFile | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            Set-Item -Path "env:$($Matches[1])" -Value $Matches[2].Trim()
        }
    }
}

$RepoUrl = if ($env:MARIONETTE_REPO_URL) { $env:MARIONETTE_REPO_URL } else { "https://github.com/professorpalmer/marionette.git" }
$MarionetteHome = if ($env:MARIONETTE_HOME) { $env:MARIONETTE_HOME } else { Join-Path $env:LOCALAPPDATA "marionette" }
$Dest = if ($env:MARIONETTE_DEST) { $env:MARIONETTE_DEST } else { Join-Path $MarionetteHome "marionette" }
$Branch = if ($env:MARIONETTE_BRANCH) { $env:MARIONETTE_BRANCH } else { "main" }
$BinDir = if ($env:MARIONETTE_BIN_DIR) { $env:MARIONETTE_BIN_DIR } else { Join-Path $MarionetteHome "bin" }
$NodeMinMajor = if ($env:MARIONETTE_NODE_MIN_MAJOR) { [int]$env:MARIONETTE_NODE_MIN_MAJOR } else { 20 }
$NodeVersion = if ($env:MARIONETTE_NODE_VERSION) { $env:MARIONETTE_NODE_VERSION } else { "22.14.0" }
$MinGitVersion = if ($env:MARIONETTE_MINGIT_VERSION) { $env:MARIONETTE_MINGIT_VERSION } else { "2.55.0" }

$NodeShaX64 = $env:MARIONETTE_NODE_SHA256_WIN_X64
$NodeShaArm64 = $env:MARIONETTE_NODE_SHA256_WIN_ARM64
$GitShaX64 = $env:MARIONETTE_MINGIT_SHA256_WIN_X64
$GitShaArm64 = $env:MARIONETTE_MINGIT_SHA256_WIN_ARM64

$ToolRoot = Join-Path $MarionetteHome "tools"
$NodeDir = Join-Path $ToolRoot "node"
$GitDir = Join-Path $ToolRoot "git"

function Verify-Sha256([string]$File, [string]$Expected) {
    if (-not $Expected) { return }
    $hash = (Get-FileHash -Path $File -Algorithm SHA256).Hash.ToLower()
    if ($hash -ne $Expected.ToLower()) {
        Die "checksum mismatch for $(Split-Path -Leaf $File) (expected $Expected, got $hash)"
    }
    Step "checksum verified ($(Split-Path -Leaf $File))"
}

function Ensure-Dir([string]$Path) {
    if (-not (Test-Path $Path)) { New-Item -ItemType Directory -Path $Path -Force | Out-Null }
}

function Add-SessionPath([string]$Dir) {
    if ($env:PATH -notlike "*$Dir*") {
        $env:PATH = "$Dir;$env:PATH"
    }
}

function Add-UserPath([string]$Dir) {
    $userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    if ($userPath -notlike "*$Dir*") {
        $newPath = if ($userPath) { "$Dir;$userPath" } else { $Dir }
        [Environment]::SetEnvironmentVariable("PATH", $newPath, "User")
        Step "added $Dir to user PATH"
    } else {
        Step "$Dir already on user PATH"
    }
}

function Get-ArchLabel {
    if ([Environment]::Is64BitOperatingSystem) {
        $proc = $env:PROCESSOR_ARCHITECTURE
        if ($proc -eq "ARM64") { return "arm64" }
        return "x64"
    }
    return "x86"
}

function Test-NodeOk {
    $node = Get-Command node -ErrorAction SilentlyContinue
    if (-not $node) { return $false }
    $ver = (& node -v) -replace '^v', ''
    $major = [int]($ver.Split('.')[0])
    return ($major -ge $NodeMinMajor)
}

function Ensure-Node {
    if (Test-NodeOk) {
        Step "node $(node -v) meets minimum (>= v$NodeMinMajor)"
        return
    }
    # Try winget first (fast, system-wide).
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Say "Installing Node.js LTS via winget"
        try {
            & winget install OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements --silent
            # Refresh PATH for this session.
            $machinePath = [Environment]::GetEnvironmentVariable("PATH", "Machine")
            $userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
            $env:PATH = "$machinePath;$userPath"
            if (Test-NodeOk) { Step "node $(node -v) ready (winget)"; return }
        } catch {
            Warn "winget Node install failed; falling back to portable zip"
        }
    }
    $arch = Get-ArchLabel
    if ($arch -eq "x86") { Die "32-bit Windows is not supported; use x64 or ARM64." }
    $zipName = "node-v$NodeVersion-win-$arch.zip"
    $url = "https://nodejs.org/dist/v$NodeVersion/$zipName"
    $expected = if ($arch -eq "arm64") { $NodeShaArm64 } else { $NodeShaX64 }
    Ensure-Dir $ToolRoot
    $zipPath = Join-Path $ToolRoot $zipName
    Say "Downloading Node v$NodeVersion ($arch)"
    if (-not (Test-Path (Join-Path $NodeDir "node.exe"))) {
        Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
        Verify-Sha256 $zipPath $expected
        if (Test-Path $NodeDir) { Remove-Item -Recurse -Force $NodeDir }
        Expand-Archive -Path $zipPath -DestinationPath $ToolRoot -Force
        $extracted = Join-Path $ToolRoot "node-v$NodeVersion-win-$arch"
        if (Test-Path $extracted) { Rename-Item $extracted $NodeDir -Force }
        Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
    } else {
        Step "reusing portable Node at $NodeDir"
    }
    Add-SessionPath $NodeDir
    if (-not (Test-NodeOk)) { Die "Node >= v$NodeMinMajor is required but portable install failed." }
    Step "node $(node -v) ready (portable)"
}

function Ensure-Git {
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) { Step "git already on PATH ($(& git --version))"; return }
    $arch = Get-ArchLabel
    if ($arch -eq "x86") { Die "32-bit Windows is not supported; use x64 or ARM64." }
    $suffix = if ($arch -eq "arm64") { "arm64" } else { "64-bit" }
    $zipName = "MinGit-$MinGitVersion-$suffix.zip"
    $url = "https://github.com/git-for-windows/git/releases/download/v$MinGitVersion.windows.1/$zipName"
    $expected = if ($arch -eq "arm64") { $GitShaArm64 } else { $GitShaX64 }
    Ensure-Dir $ToolRoot
    $zipPath = Join-Path $ToolRoot $zipName
    Say "Downloading portable MinGit $MinGitVersion ($suffix)"
    if (-not (Test-Path (Join-Path $GitDir "cmd\git.exe"))) {
        Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
        Verify-Sha256 $zipPath $expected
        if (Test-Path $GitDir) { Remove-Item -Recurse -Force $GitDir }
        Expand-Archive -Path $zipPath -DestinationPath $GitDir -Force
        Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
    } else {
        Step "reusing portable git at $GitDir"
    }
    Add-SessionPath (Join-Path $GitDir "cmd")
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Die "git is required but portable install failed." }
    Step "git ready ($(& git --version))"
}

function Ensure-Uv {
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) { Step "uv already on PATH ($(& uv --version))"; return }
    Say "Installing uv (Python toolchain manager)"
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Die "uv install failed: $_"
    }
    $uvLocal = Join-Path $env:USERPROFILE ".local\bin"
    Add-SessionPath $uvLocal
    $cargo = Join-Path $env:USERPROFILE ".cargo\bin"
    Add-SessionPath $cargo
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Die "uv installed but not on PATH. Add ~/.local/bin to PATH and re-run."
    }
    Step "uv ready ($(& uv --version))"
}

# --- main install ------------------------------------------------------------
$Arch = Get-ArchLabel
Say "Installing Marionette for Windows/$Arch"

Ensure-Uv
Ensure-Git

# Clone or update (git must be on PATH now).
Ensure-Dir $MarionetteHome
if (Test-Path (Join-Path $Dest ".git")) {
    Say "Existing checkout at $Dest -- fetching $Branch"
    & git -C $Dest fetch --no-tags origin $Branch
    & git -C $Dest checkout $Branch
    try { & git -C $Dest merge --ff-only "origin/$Branch" } catch { Warn "local changes present; skipped fast-forward" }
} else {
    Say "Cloning $RepoUrl -> $Dest"
    & git clone --branch $Branch $RepoUrl $Dest
}

Set-Location $Dest
Ensure-Node

Say "Provisioning Python via uv (reads .python-version)"
& uv python install
if (Test-Path ".venv") {
    Step "reusing existing .venv"
} else {
    & uv venv .venv
}

Say "Installing Marionette (editable) + Puppetmaster into .venv"
& uv pip install --python .venv -e .
$puppetSpec = if ($env:MARIONETTE_PUPPETMASTER_SPEC) { $env:MARIONETTE_PUPPETMASTER_SPEC } else { "puppetmaster-ai==1.20.0" }
& uv pip install --python .venv $puppetSpec

Say "Installing node deps + building the renderer"
Push-Location webapp
try {
    & npm ci
    & npm run build
} finally {
    Pop-Location
}

Say "Installing launchers into $BinDir"
Ensure-Dir $BinDir

$CmdLauncher = Join-Path $BinDir "marionette.cmd"
@(
"@echo off",
"setlocal",
"set MARIONETTE_DEST=$Dest",
"if ""%~1""==""doctor"" goto doctor",
"if ""%~1""==""dev"" goto dev",
"if ""%~1""==""update"" goto update",
"if ""%~1""==""desktop"" goto desktop",
"if ""%~1""=="""" goto desktop",
"echo usage: marionette [desktop^|dev^|doctor^|update] 1>&2",
"exit /b 2",
":doctor",
"powershell -NoProfile -ExecutionPolicy Bypass -File ""$Dest\scripts\doctor.ps1""",
"exit /b %ERRORLEVEL%",
":dev",
"powershell -NoProfile -ExecutionPolicy Bypass -File ""$Dest\scripts\dev.ps1""",
"exit /b %ERRORLEVEL%",
":update",
"git -C ""$Dest"" pull --ff-only",
"if errorlevel 1 exit /b 1",
"pushd ""$Dest\webapp""",
"call npm run build",
"popd",
"exit /b 0",
":desktop",
"powershell -NoProfile -ExecutionPolicy Bypass -File ""$Dest\scripts\start.ps1""",
"exit /b %ERRORLEVEL%"
) | Set-Content -Path $CmdLauncher -Encoding ASCII

$PsLauncher = Join-Path $BinDir "marionette.ps1"
$psContent = @"
param([string]`$Command = "desktop")
`$Dest = "$Dest"
switch (`$Command) {
    "doctor"  { & "`$Dest\scripts\doctor.ps1"; exit `$LASTEXITCODE }
    "dev"     { & "`$Dest\scripts\dev.ps1"; exit `$LASTEXITCODE }
    "update"  {
        git -C `$Dest pull --ff-only
        if (`$LASTEXITCODE -ne 0) { exit `$LASTEXITCODE }
        Push-Location "`$Dest\webapp"
        npm run build
        Pop-Location
        exit 0
    }
    { `$_ -in @("", "desktop") } { & "`$Dest\scripts\start.ps1"; exit `$LASTEXITCODE }
    default { Write-Error "usage: marionette [desktop|dev|doctor|update]"; exit 2 }
}
"@
Set-Content -Path $PsLauncher -Value $psContent -Encoding UTF8

Add-UserPath $BinDir
Add-SessionPath $BinDir

Say "Verifying the install"
try {
    & powershell -NoProfile -ExecutionPolicy Bypass -File "$Dest\scripts\doctor.ps1"
} catch {
    Warn "doctor reported problems above -- Marionette may not run correctly until they are fixed."
}

Write-Host @"

Marionette is installed at: $Dest

Launch it:
  marionette            # daily use (built renderer, in-app updates)
  marionette dev        # contributor hot-reload (Vite HMR)
  marionette doctor     # re-check the environment
  marionette update     # git pull + rebuild

If 'marionette' is not found, open a new terminal (PATH was updated) or run:
  $CmdLauncher

Set your API keys in the app's Settings pane, or in $env:USERPROFILE\.pmharness\keys.json.
"@
