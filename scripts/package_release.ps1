param(
  [string]$Output = "release\jobradar_release.zip"
)

$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path "$PSScriptRoot\..")

$releaseDir = Split-Path $Output -Parent
if ($releaseDir) { New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null }

$exclude = @(
  ".venv/*", "venv/*", "__pycache__/*", "*.pyc", ".pytest_cache/*", ".git/*",
  "output/*.sqlite3", "output/*.bak*", "output/logs/*", "output/screenshots/*", "output/sessions/*",
  "backups/*", "release/*"
)

if (Test-Path $Output) { Remove-Item $Output -Force }

$files = Get-ChildItem -Recurse -File | Where-Object {
  $rel = $_.FullName.Substring((Get-Location).Path.Length + 1).Replace('\\','/')
  if ($rel -eq ".env.example") { return $true }
  if ($rel -eq ".env" -or $rel -like ".env.*") { return $false }
  foreach ($pattern in $exclude) {
    if ($rel -like $pattern) { return $false }
  }
  return $true
}

Compress-Archive -Path $files.FullName -DestinationPath $Output -Force
Write-Host "Release package created: $Output" -ForegroundColor Green
