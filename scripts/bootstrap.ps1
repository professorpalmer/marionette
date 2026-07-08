# Compatibility shim: bootstrap.sh has been superseded by install.ps1 (the
# uv-based source-run installer). It delegates so old links keep working.
#
#   irm https://raw.githubusercontent.com/professorpalmer/marionette/main/scripts/install.ps1 | iex
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallScript = Join-Path $ScriptDir "install.ps1"

# Forward all arguments to install.ps1
& $InstallScript @args
exit $LASTEXITCODE