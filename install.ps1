# install.ps1 - one-shot setup for Jev browser use with any MCP-capable LLM harness.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1
#   powershell -ExecutionPolicy Bypass -File install.ps1 -RepoPath C:\path\to\clone
#
# Idempotent: re-running is safe. It never overwrites an existing .env and never
# duplicates an MCP registration.
param(
  [string]$RepoPath = "$env:USERPROFILE\Documents\browseruse",
  [switch]$NoRegister,
  [switch]$NoConfigure,
  [switch]$SkipVerify
)
# PS 5.1 turns a native command's stderr into a terminating error under "Stop"; every
# step below checks $LASTEXITCODE explicitly instead and calls Fail() on failure.
$ErrorActionPreference = "Continue"
$Kit = Split-Path -Parent $MyInvocation.MyCommand.Path

function Step($m) { Write-Host "`n== $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "   ok   $m" -ForegroundColor Green }
function Warn($m) { Write-Host "   warn $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "   FAIL $m" -ForegroundColor Red; exit 1 }

# PS 5.1 Set-Content defaults to ANSI and -Encoding UTF8 adds a BOM; write UTF-8 no BOM.
function Write-Utf8($Path, $Text) {
  [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function Patch-File($Path, $Find, $Replace, $Marker) {
  $text = [System.IO.File]::ReadAllText($Path)
  if ($text.Contains($Marker)) { Ok "already patched: $(Split-Path $Path -Leaf)"; return }
  if (-not $text.Contains($Find)) { Warn "patch target missing in $(Split-Path $Path -Leaf) - left as is"; return }
  Write-Utf8 $Path $text.Replace($Find, $Replace)
  Ok "patched $(Split-Path $Path -Leaf)"
}

Step "1/7  Python + uv"
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { Fail "python is not on PATH" }
& $python -m uv --version 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
  Warn "uv missing; installing it from PyPI"
  & $python -m pip install --quiet --upgrade uv 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0) { Fail "could not install uv" }
}
Ok (& $python -m uv --version)

Step "2/7  jev-ultrafast source"
if (Test-Path "$RepoPath\.git") {
  Ok "repo present at $RepoPath"
} else {
  $parent = Split-Path $RepoPath -Parent
  if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
  git clone --depth 1 https://github.com/browser-use/jev-ultrafast.git $RepoPath
  if ($LASTEXITCODE -ne 0) { Fail "git clone failed" }
  Ok "cloned to $RepoPath"
}

Step "3/7  apply required patches"

# Google Flights fades dropdowns in over ~2-3s. Upstream finishes its post-interaction
# settle after 2 animation frames (~33ms), so the snapshot catches the menu at opacity 0,
# checkVisibility is false, its options are dropped, and the agent clicks the same control
# until it blocks. Wait a real settle instead.
$browser = "$RepoPath\jev_ultrafast\browser.py"
Patch-File $browser 'let frames=0, stopped=false;' `
  'const t0=performance.now(), settle=autocomplete ? 0 : 3000; let frames=0, stopped=false;' `
  'const t0=performance.now()'
Patch-File $browser 'setTimeout(finish,autocomplete ? 200 : 50);' `
  'setTimeout(finish, 3000);' 'setTimeout(finish, 3000)'
Patch-File $browser 'if (++frames>=2 && (!autocomplete || options.some(e=>{' `
  'if (++frames>=2 && performance.now()-t0>=settle && (!autocomplete || options.some(e=>{' `
  'performance.now()-t0>=settle'

# OpenCode Go (and any client following its spec) requires a stable session id and a real
# user agent, otherwise every text call fails with HTTP 400 MissingSessionID.
$model = "$RepoPath\jev_ultrafast\model.py"
$signatureFind = @'
def post_json(url, key, body):
    for attempt in range(3):
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
'@
$signatureReplace = @'
def post_json(url, key, body, extra_headers=None):
    for attempt in range(3):
        try:
            response = CLIENT.post(
                url, json=body, headers={"Authorization": f"Bearer {key}", **(extra_headers or {})}
            )
'@
Patch-File $model $signatureFind $signatureReplace 'extra_headers=None'

$callFind = @'
    started = time.perf_counter()
    result = post_json(
        base + "/chat/completions",
        key,
        {
            "model": model,
            "max_tokens": 1024,
            "response_format": {"type": "json_object"},
            **reasoning,
            "messages": [
                {"role": "system", "content": TEXT_VALUE},
                {
                    "role": "user",
                    "content": json.dumps(context),
                },
            ],
        },
    )
'@
$callReplace = @'
    # ponytail: OpenCode Go's client spec requires a session id and a real user agent.
    extra = (
        {
            "x-opencode-session": os.environ.get("TEXT_MODEL_SESSION", "jev-ultrafast"),
            "User-Agent": "jev-ultrafast/0.1.0",
        }
        if "opencode.ai" in base
        else None
    )
    started = time.perf_counter()
    result = post_json(
        base + "/chat/completions",
        key,
        {
            "model": model,
            "max_tokens": 1024,
            "response_format": {"type": "json_object"},
            **reasoning,
            "messages": [
                {"role": "system", "content": TEXT_VALUE},
                {
                    "role": "user",
                    "content": json.dumps(context),
                },
            ],
        },
        extra,
    )
'@
Patch-File $model $callFind $callReplace 'x-opencode-session'

Step "4/7  dependencies and the MCP tool"
& $python -m uv sync --quiet --directory $RepoPath 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "uv sync failed" }
Ok "uv sync complete"
& $python -m uv add --quiet --directory $RepoPath mcp 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "uv add mcp failed" }
Ok "mcp SDK installed"
Copy-Item "$Kit\jev_mcp.py" "$RepoPath\jev_mcp.py" -Force
if (Test-Path "$Kit\scripts\jev-search.ps1") {
  New-Item -ItemType Directory -Force -Path "$RepoPath\scripts" | Out-Null
  Copy-Item "$Kit\scripts\jev-search.ps1" "$RepoPath\scripts\jev-search.ps1" -Force
}
Ok "jev_mcp.py installed"

Step "5/7  credentials"
$envFile = "$RepoPath\.env"
if (Test-Path $envFile) {
  Ok ".env exists; left untouched"
} else {
  Copy-Item "$RepoPath\.env.example" $envFile
  Warn "created .env - fill in TYPESAFE_API_KEY and TEXT_MODEL_API_KEY before searching"
}

Step "6/7  register the MCP server"
$py = "$RepoPath\.venv\Scripts\python.exe"
$server = "$RepoPath\jev_mcp.py"
if ($NoRegister) {
  Warn "skipped (-NoRegister)"
} else {
  $oc = "$env:USERPROFILE\.config\opencode\opencode.jsonc"
  if (Test-Path $oc) {
    $t = [System.IO.File]::ReadAllText($oc)
    if ($t.Contains('"jev"')) { Ok "opencode: already registered" }
    elseif ($t.Contains('"mcp": {')) {
      $entry = '    "jev": { "type": "local", "command": ["' + $py.Replace('\', '\\') + '", "' + `
               $server.Replace('\', '\\') + '"], "enabled": true, "timeout": 600000 },'
      Write-Utf8 $oc $t.Replace('"mcp": {', '"mcp": {' + "`n" + $entry)
      Ok "opencode: registered (restart opencode)"
    } else { Warn "opencode: no mcp block found" }
  } else { Warn "opencode config not found" }

  $cx = "$env:USERPROFILE\.codex\config.toml"
  if (Test-Path $cx) {
    $t = [System.IO.File]::ReadAllText($cx)
    if ($t.Contains('[mcp_servers.jev]')) { Ok "Codex: already registered" }
    else {
      Add-Content -LiteralPath $cx -Value "`n[mcp_servers.jev]`ncommand = '$py'`nargs = ['$server']`nstartup_timeout_sec = 120`n"
      Ok "Codex: registered (restart Codex)"
    }
  } else { Warn "Codex config not found" }
}

if (-not $SkipVerify) {
  Step "7/7  verify"
  & $python -m uv run --directory $RepoPath pytest -q 2>&1 | Select-Object -Last 2
  & $python -m uv run --directory $RepoPath ruff check . 2>&1 | Select-Object -Last 2
}

Write-Host ""
& $python -m uv run --directory $RepoPath python jev_mcp.py --browsers 2>&1 | Select-Object -Last 3

if (-not $NoConfigure -and [Environment]::UserInteractive) {
  Write-Host ""
  $answer = Read-Host "Run the API key + browser setup wizard now? [Y/n]"
  if ($answer -eq "" -or $answer -match '^(?i)y') {
    & $python -m uv run --directory $RepoPath python jev_mcp.py --configure
  } else {
    Write-Host "Later:  cd $RepoPath ; python jev_mcp.py --configure" -ForegroundColor Yellow
  }
}

Write-Host "`nDone." -ForegroundColor Green
Write-Host "  1. restart opencode / Codex so they pick up the jev MCP server"
Write-Host "  2. live stats:  python jev_mcp.py --serve   ->  http://127.0.0.1:8767"
Write-Host "  3. a search:    python jev_mcp.py --search ""<goal>"" --url ""<start url>"""
