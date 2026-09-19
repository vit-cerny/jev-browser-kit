# scan-secrets.ps1 - standalone secret scanner for jev-browser-kit.
# Pure Windows PowerShell 5.1, pure ASCII, deterministic, non-interactive.
#
#   .\scripts\scan-secrets.ps1
#   .\scripts\scan-secrets.ps1 -Path C:\some\dir -FailOnFind
#
# Exit codes: 0 = clean (or findings without -FailOnFind), 1 = findings with -FailOnFind.

[CmdletBinding()]
param(
  [string]$Path,
  [switch]$FailOnFind
)
$ErrorActionPreference = "Stop"

if (-not $Path) {
  $scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
  $Path = Split-Path -Parent $scriptDir
}

$skipDirs = @(".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "artifacts")

# Secret patterns. Each pattern is written so it cannot match this file's own
# source: in every pattern string the literal prefix (e.g. "apikey_", "sk-",
# "AKIA") is immediately followed by regex metacharacters in the source text,
# so the regex cannot match the text that defines it. The private-key header
# pattern is assembled from three string literals for the same reason.
$patterns = @(
  "apikey_[A-Za-z0-9_]{20,}",
  "sk-[A-Za-z0-9]{20,}",
  "gh[opsu]_[A-Za-z0-9]{20,}",
  "github_pat_[A-Za-z0-9_]{20,}",
  "AKIA[0-9A-Z]{16}",
  "api[_-]?key\s*[=:]\s*[""']?[A-Za-z0-9_\-]{20,}",
  ("-----BEGIN " + "[A-Z ]*" + "PRIVATE KEY-----")
)

# Matched values containing any of these words are placeholders, not secrets.
$placeholderPattern = "(?i)(example|placeholder|your|xxx|changeme|dummy|sample|fake|redacted)"

function Test-SkippedPath {
  param([string]$FullName, [string]$Root)
  $rel = $FullName.Substring($Root.Length) -replace "^[\\/]+", ""
  foreach ($seg in ($rel -split "[\\/]")) {
    if ($skipDirs -contains $seg) { return $true }
  }
  return $false
}

function Test-BinaryFile {
  param([string]$FullName)
  $fs = [System.IO.File]::OpenRead($FullName)
  try {
    $buf = New-Object byte[] 8000
    $n = $fs.Read($buf, 0, 8000)
    for ($i = 0; $i -lt $n; $i++) {
      if ($buf[$i] -eq 0) { return $true }
    }
    return $false
  } finally {
    $fs.Dispose()
  }
}

function Get-Redacted {
  param([string]$Value)
  if ($Value.Length -le 6) { return $Value }
  return $Value.Substring(0, 4) + ("*" * ($Value.Length - 6)) + $Value.Substring($Value.Length - 2)
}

function Test-Placeholder {
  param([string]$Value)
  return ($Value -match $placeholderPattern)
}

if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
  Write-Output "[scan-secrets] ERROR: path not found: $Path"
  exit 1
}
$root = (Resolve-Path -LiteralPath $Path).Path

$files = Get-ChildItem -LiteralPath $root -Recurse -File -Force -ErrorAction SilentlyContinue |
  Where-Object { -not (Test-SkippedPath $_.FullName $root) } |
  Sort-Object -Property FullName

$findingCount = 0
$fileCount = 0
$scannedCount = 0

foreach ($file in $files) {
  $isEnv = ($file.Name -eq ".env") -or (($file.Name -like ".env.*") -and ($file.Name -ne ".env.example"))
  try {
    if (Test-BinaryFile $file.FullName) { continue }
    $lines = [System.IO.File]::ReadAllLines($file.FullName)
  } catch {
    Write-Output "[scan-secrets] WARNING: cannot read $($file.FullName)"
    continue
  }
  $scannedCount++
  $fileHadFinding = $false
  if ($isEnv) {
    $first = ""
    if ($lines.Count -gt 0) { $first = $lines[0].Trim() }
    if ($first -eq "") {
      Write-Output ("{0}:1: <empty>" -f $file.FullName)
    } else {
      Write-Output ("{0}:1: {1}" -f $file.FullName, (Get-Redacted $first))
    }
    $findingCount++
    $fileHadFinding = $true
  }
  for ($i = 0; $i -lt $lines.Count; $i++) {
    foreach ($p in $patterns) {
      $ms = [regex]::Matches($lines[$i], $p)
      foreach ($m in $ms) {
        if (Test-Placeholder $m.Value) { continue }
        Write-Output ("{0}:{1}: {2}" -f $file.FullName, ($i + 1), (Get-Redacted $m.Value))
        $findingCount++
        $fileHadFinding = $true
      }
    }
  }
  if ($fileHadFinding) { $fileCount++ }
}

if ($findingCount -eq 0) {
  Write-Output "[scan-secrets] OK: scanned $scannedCount file(s), no secrets found"
  exit 0
}
if ($FailOnFind) {
  Write-Output "[scan-secrets] FAIL: $findingCount finding(s) in $fileCount file(s)"
  exit 1
}
Write-Output "[scan-secrets] WARNING: $findingCount finding(s) in $fileCount file(s); pass -FailOnFind to exit 1"
exit 0