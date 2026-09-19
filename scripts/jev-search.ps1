# jev-search.ps1 - one-shot Jev browser search.
# Browser launch, profile choice and port handling all live in jev_mcp.py; this only
# resolves the interpreter and forwards the goal.
#
#   .\scripts\jev-search.ps1 -Goal "Find the cheapest flight from Prague to Barcelona"
#   .\scripts\jev-search.ps1 -Goal "..." -Url "https://www.google.com/imghp?q=nike+air+max+90"
param(
  [Parameter(Mandatory = $true)][string]$Goal,
  [string]$Url = "https://www.google.com/travel/flights?hl=en"
)
$ErrorActionPreference = "Continue"

$repo = Split-Path -Parent $PSScriptRoot
$python = if (Test-Path "$repo\.venv\Scripts\python.exe") { "$repo\.venv\Scripts\python.exe" } else { "python" }

& $python "$repo\jev_mcp.py" --search $Goal --url $Url
exit $LASTEXITCODE
