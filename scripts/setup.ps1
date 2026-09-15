param(
  [Parameter(Mandatory=$true)][string]$RepoPath,
  [Parameter(Mandatory=$true)][string]$RepoSlug,
  [Parameter(Mandatory=$true)][string]$BaseBranch
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
python autopilot.py init --repo $RepoPath --repo-slug $RepoSlug --base-branch $BaseBranch
python autopilot.py doctor
Write-Host "\nAhora ejecuta: python autopilot.py bootstrap-labels" -ForegroundColor Green
