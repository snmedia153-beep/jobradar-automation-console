param(
  [switch]$IncludeRedis,
  [switch]$IncludeLogs
)

$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path "$PSScriptRoot\..")

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$pgDir = "backups\postgres"
$redisDir = "backups\redis"
$logDir = "backups\logs"
New-Item -ItemType Directory -Force -Path $pgDir, $redisDir, $logDir | Out-Null

$pgFile = Join-Path $pgDir "jobradar_postgres_$timestamp.sql"
Write-Host "Creating Postgres backup: $pgFile" -ForegroundColor Cyan

docker compose up -d postgres | Out-Null
Start-Sleep -Seconds 2

docker exec -e PGPASSWORD=jobradar jobradar-postgres pg_dump -U jobradar -d jobradar --clean --if-exists --no-owner --no-privileges | Out-File -FilePath $pgFile -Encoding utf8

if (!(Test-Path $pgFile) -or ((Get-Item $pgFile).Length -lt 100)) {
  throw "Postgres backup looks empty or failed: $pgFile"
}
Write-Host "Postgres backup complete." -ForegroundColor Green

if ($IncludeRedis) {
  $redisFile = Join-Path $redisDir "jobradar_redis_$timestamp.rdb"
  Write-Host "Creating Redis dump: $redisFile" -ForegroundColor Cyan
  docker compose up -d redis | Out-Null
  docker exec jobradar-redis redis-cli BGSAVE | Out-Null
  Start-Sleep -Seconds 2
  docker cp jobradar-redis:/data/dump.rdb $redisFile
  Write-Host "Redis backup complete." -ForegroundColor Green
}

if ($IncludeLogs) {
  $zipFile = Join-Path $logDir "jobradar_logs_$timestamp.zip"
  if (Test-Path "output\logs") {
    Compress-Archive -Path "output\logs\*" -DestinationPath $zipFile -Force
    Write-Host "Log archive complete: $zipFile" -ForegroundColor Green
  }
}

Write-Host "Backup finished." -ForegroundColor Green
