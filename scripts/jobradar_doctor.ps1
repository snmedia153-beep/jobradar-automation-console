param(
  [switch]$HostOnly
)

$ErrorActionPreference = "Continue"
Set-Location (Resolve-Path "$PSScriptRoot\..")

function Check-Cmd($Name, $Command) {
  Write-Host "[$Name]" -ForegroundColor Cyan
  try { Invoke-Expression $Command; Write-Host "OK: $Name" -ForegroundColor Green }
  catch { Write-Host "FAIL: $Name - $_" -ForegroundColor Red }
  Write-Host ""
}

Check-Cmd "Docker" "docker version"
Check-Cmd "Docker Compose" "docker compose version"
Check-Cmd "Containers" "docker compose ps"
Check-Cmd "Host Appium ports" "netstat -ano | findstr ':472'"

try {
  $api = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://localhost:8000/health"
  Write-Host "OK: FastAPI http://localhost:8000/health HTTP $($api.StatusCode)" -ForegroundColor Green
} catch { Write-Host "FAIL: FastAPI - $_" -ForegroundColor Red }

try {
  $gui = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://localhost:8501/_stcore/health"
  Write-Host "OK: GUI http://localhost:8501 HTTP $($gui.StatusCode)" -ForegroundColor Green
} catch { Write-Host "FAIL: GUI - $_" -ForegroundColor Red }

if (!$HostOnly) {
  Write-Host ""
  Write-Host "[Container deploy doctor]" -ForegroundColor Cyan
  docker compose exec jobradar-api python -m jobradar.cli deploy-doctor
}
