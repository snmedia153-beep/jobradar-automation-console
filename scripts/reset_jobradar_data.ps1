param(
  [switch]$BackupFirst,
  [switch]$Force
)

$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path "$PSScriptRoot\..")

if ($BackupFirst) {
  & "$PSScriptRoot\backup_jobradar.ps1" -IncludeRedis -IncludeLogs
}

if (!$Force) {
  Write-Warning "This will delete Docker volumes for Postgres and Redis."
  $confirm = Read-Host "Type RESET to continue"
  if ($confirm -ne "RESET") { throw "Canceled." }
}

docker compose --profile worker --profile init --profile manual down -v
Write-Host "Docker data volumes removed." -ForegroundColor Green
Write-Host "Run .\scripts\deploy_jobradar.ps1 -Init -Build to recreate the stack." -ForegroundColor Yellow
