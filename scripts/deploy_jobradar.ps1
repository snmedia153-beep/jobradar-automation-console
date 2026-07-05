param(
  [switch]$Init,
  [switch]$Build,
  [switch]$WithWorker,
  [switch]$StartAppium,
  [switch]$StartHostAgent,
  [switch]$Doctor,
  [switch]$Logs,
  [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path "$PSScriptRoot\..")

function Ensure-Directory($Path) {
  if (!(Test-Path $Path)) { New-Item -ItemType Directory -Path $Path | Out-Null }
}

function Ensure-EnvFile {
  if (!(Test-Path ".env")) {
    if (Test-Path ".env.example") {
      Copy-Item ".env.example" ".env"
      Write-Host "Created .env from .env.example" -ForegroundColor Green
    } else {
      Write-Warning ".env was not found. Copy .env.example to .env before running Docker Compose."
    }
  }
}

Write-Host "JobRadar Automation Console deployment" -ForegroundColor Cyan
Ensure-EnvFile
Ensure-Directory "output"
Ensure-Directory "output\logs"
Ensure-Directory "output\screenshots"
Ensure-Directory "output\sessions"
Ensure-Directory "backups"
Ensure-Directory "backups\postgres"
Ensure-Directory "backups\redis"
Ensure-Directory "backups\logs"

try { docker version | Out-Null } catch { throw "Docker Desktop is not running or docker is not in PATH." }
try { docker compose version | Out-Null } catch { throw "Docker Compose v2 is not available." }

if ($StartHostAgent) {
  Write-Host "Starting Windows Host Agent for emulator window arrangement..." -ForegroundColor Cyan
  & "$PSScriptRoot\start_host_agent.ps1" -NewWindow
}

if ($StartAppium) {
  Write-Host "Starting host Appium 5-slot servers..." -ForegroundColor Cyan
  & "$PSScriptRoot\start_appium_5slots.ps1" -OnlyMissing
}

if ($Build) {
  Write-Host "Building Docker images..." -ForegroundColor Cyan
  docker compose build jobradar-api jobradar-gui jobradar-appium-worker jobradar-playwright-worker
}

Write-Host "Starting Postgres and Redis..." -ForegroundColor Cyan
docker compose up -d postgres redis

if ($Init) {
  Write-Host "Initializing database, default profiles, and 5 slots..." -ForegroundColor Cyan
  docker compose --profile init run --rm jobradar-init
}

if ($WithWorker) {
  Write-Host "Starting API, GUI, and Appium worker..." -ForegroundColor Cyan
  docker compose --profile worker up -d postgres redis jobradar-api jobradar-gui jobradar-appium-worker
} else {
  Write-Host "Starting API and GUI..." -ForegroundColor Cyan
  docker compose up -d postgres redis jobradar-api jobradar-gui
}

Write-Host "Waiting for services..." -ForegroundColor Cyan
Start-Sleep -Seconds 5

Write-Host ""
Write-Host "GUI      : http://localhost:8501" -ForegroundColor Green
Write-Host "FastAPI  : http://localhost:8000" -ForegroundColor Green
Write-Host "API Docs : http://localhost:8000/docs" -ForegroundColor Green
Write-Host "HostAgent : http://localhost:8767/health  (when -StartHostAgent is used)" -ForegroundColor Green
Write-Host ""
Write-Host "Useful checks:" -ForegroundColor Yellow
Write-Host "  docker compose exec jobradar-api python -m jobradar.cli deploy-doctor"
Write-Host "  docker compose exec jobradar-api python -m jobradar.cli appium-health"
Write-Host "  docker compose exec jobradar-api python -m jobradar.cli worker-events --limit 30"

if ($Doctor) {
  & "$PSScriptRoot\jobradar_doctor.ps1"
}

if (!$NoBrowser) {
  Start-Process "http://localhost:8501"
}

if ($Logs) {
  docker compose logs -f jobradar-api jobradar-gui jobradar-appium-worker
}
