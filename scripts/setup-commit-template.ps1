# Configures this repository to use the project .gitmessage as the commit template.
# Run from anywhere:  powershell -File scripts/setup-commit-template.ps1
# (or from repo root:  .\scripts\setup-commit-template.ps1)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Template = Join-Path $RepoRoot ".gitmessage"
if (-not (Test-Path $Template)) {
    Write-Error ".gitmessage not found at $Template"
}
git -C $RepoRoot config commit.template $Template
Write-Host "Configured commit.template -> $Template"
