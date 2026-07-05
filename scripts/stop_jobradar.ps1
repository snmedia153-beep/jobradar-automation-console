param(
  [switch]$StopAppium,
  [switch]$RemoveVolumes
)

$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path "$PSScriptRoot\..")

if ($RemoveVolumes) {
  Write-Warning "This will remove Docker volumes including Postgres and Redis data. Create a backup first."
  $confirm = Read-Host "Type DELETE to continue"
  if ($confirm -ne "DELETE") { throw "Canceled." }
  docker compose --profile worker --profile init --profile manual down -v
} else {
  docker compose --profile worker --profile init --profile manual down
}

if ($StopAppium) {
  & "$PSScriptRoot\stop_appium_5slots.ps1"
}

Write-Host "JobRadar stack stopped." -ForegroundColor Green
