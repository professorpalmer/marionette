# marionette doctor -- verify a source-run install is healthy (Windows).
#
# Source-run trades a frozen binary for local compilation, so the failure modes
# are environmental: wrong Python/Node, a missing C++ toolchain for CodeGraph's
# better-sqlite3, an unbuilt renderer, or a venv that can't import the kernel.
# Exit non-zero if anything critical fails so install.ps1 can gate on it.
$ErrorActionPreference = "Continue"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $RepoRoot
Set-Location $RepoRoot

$Fail = 0
function Ok([string]$Msg) { Write-Host "  ok    $Msg" }
function Bad([string]$Msg) { Write-Host "  FAIL  $Msg" -ForegroundColor Red; $script:Fail = 1 }
function Note([string]$Msg) { Write-Host "  note  $Msg" }

Write-Host "== marionette doctor ($RepoRoot) =="

# --- Python venv + kernel importability -------------------------------------
$Py = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (Test-Path $Py) {
    $ver = & $Py --version 2>&1
    Ok "python venv present ($ver)"
    & $Py -c "import harness" 2>$null
    if ($LASTEXITCODE -eq 0) { Ok "imports 'harness'" } else { Bad "cannot import 'harness' -- run: uv pip install --python .venv -e ." }
    & $Py -c "import puppetmaster" 2>$null
    if ($LASTEXITCODE -eq 0) { Ok "imports 'puppetmaster'" } else { Bad "cannot import 'puppetmaster' -- run: uv pip install --python .venv puppetmaster-ai==1.20.0" }
} else {
    Bad "no .venv\Scripts\python.exe -- run: uv venv .venv && uv pip install --python .venv -e ."
}

# --- Node --------------------------------------------------------------------
$node = Get-Command node -ErrorAction SilentlyContinue
if ($node) {
    $ver = (node -v) -replace '^v', ''
    $major = [int]($ver.Split('.')[0])
    if ($major -ge 20) { Ok "node v$ver" } else { Bad "node v$ver too old (need >= v20)" }
    $nvmrc = Join-Path $RepoRoot ".nvmrc"
    if (Test-Path $nvmrc) { Note "pinned Node is $((Get-Content $nvmrc -Raw).Trim()) (.nvmrc)" }
} else {
    Bad "node not on PATH (need >= v20)"
}

# --- C/C++ toolchain (better-sqlite3 native build) --------------------------
$cl = Get-Command cl -ErrorAction SilentlyContinue
$msbuild = Get-Command msbuild -ErrorAction SilentlyContinue
if ($cl -or $msbuild) {
    Ok "C++ build tools present (CodeGraph native build can compile)"
} else {
    Bad "no C++ compiler detected -- install Visual Studio Build Tools (Desktop development with C++) for better-sqlite3"
}

# --- renderer build ----------------------------------------------------------
$dist = Join-Path $RepoRoot "webapp\dist\index.html"
if (Test-Path $dist) { Ok "renderer built (webapp/dist)" } else { Bad "renderer not built -- run: (cd webapp; npm run build)" }

# --- optional external tools -------------------------------------------------
$rg = Get-Command rg -ErrorAction SilentlyContinue
if ($rg) { Ok "ripgrep (rg) present" } else { Note "ripgrep (rg) not found -- some search is slower without it" }

# --- CodeGraph binding -------------------------------------------------------
if ((Test-Path $Py) -and (& $Py -c "import puppetmaster" 2>$null; $LASTEXITCODE -eq 0)) {
    & $Py -m puppetmaster codegraph status 2>$null
    if ($LASTEXITCODE -eq 0) {
        Ok "CodeGraph binding loads (python -m puppetmaster codegraph)"
    } else {
        Note "CodeGraph status returned non-zero -- may just be an unindexed dir; check: python -m puppetmaster codegraph status"
    }
}

Write-Host ""
if ($Fail -ne 0) {
    Write-Host "doctor: problems found (see FAIL lines above)."
    exit 1
}
Write-Host "doctor: all critical checks passed."
