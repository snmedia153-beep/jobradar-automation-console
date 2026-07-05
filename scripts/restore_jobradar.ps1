param(
  [Parameter(Mandatory=$true)][string]$PostgresBackup,
  [string]$RedisBackup = "",
  [switch]$Force
)

$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path "$PSScriptRoot\..")

if (!(Test-Path $PostgresBackup)) { throw "Postgres backup not found: $PostgresBackup" }

if (!$Force) {
  Write-Warning "This will overwrite the current JobRadar Postgres database."
  $confirm = Read-Host "Type RESTORE to continue"
  if ($confirm -ne "RESTORE") { throw "Canceled." }
}

docker compose up -d postgres | Out-Null
Start-Sleep -Seconds 3

Write-Host "Resetting Postgres schema..." -ForegroundColor Cyan
docker exec -e PGPASSWORD=jobradar jobradar-postgres psql -U jobradar -d jobradar -c "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public; GRANT ALL ON SCHEMA public TO jobradar; GRANT ALL ON SCHEMA public TO public;" | Out-Null

Write-Host "Restoring Postgres backup: $PostgresBackup" -ForegroundColor Cyan
Get-Content -Raw $PostgresBackup | docker exec -i -e PGPASSWORD=jobradar jobradar-postgres psql -U jobradar -d jobradar | Out-Null
Write-Host "Postgres restore complete." -ForegroundColor Green

if ($RedisBackup) {
  if (!(Test-Path $RedisBackup)) { throw "Redis backup not found: $RedisBackup" }
  Write-Host "Restoring Redis dump: $RedisBackup" -ForegroundColor Cyan
  docker compose stop redis | Out-Null
  docker cp $RedisBackup jobradar-redis:/data/dump.rdb
  docker compose up -d redis | Out-Null
  Write-Host "Redis restore complete." -ForegroundColor Green
}

Write-Host "Running init-db to ensure latest schema..." -ForegroundColor Cyan
docker compose run --rm jobradar-api python -m jobradar.cli init-db
Write-Host "Restore finished." -ForegroundColor Green
