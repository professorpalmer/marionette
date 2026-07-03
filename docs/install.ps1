# Thin shim: fetch and exec the canonical Marionette installer from main.
# Published at https://professorpalmer.github.io/marionette/install.ps1
$ErrorActionPreference = "Stop"
$script = Invoke-RestMethod -Uri "https://raw.githubusercontent.com/professorpalmer/marionette/main/scripts/install.ps1" -UseBasicParsing
Invoke-Expression $script
