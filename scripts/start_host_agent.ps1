param(
  [string]$HostAddress = "127.0.0.1",
  [int]$Port = 8767,
  [switch]$NewWindow,
  [switch]$NoRestart,
  [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Get-PortPids([int]$TargetPort) {
  $lines = netstat -ano -p TCP | Select-String ":$TargetPort"
  $found = @()
  foreach ($line in $lines) {
    $text = $line.ToString()
    if ($text -match "LISTENING\s+(\d+)\s*$") {
      $found += [int]$Matches[1]
    }
  }
  $found | Sort-Object -Unique
}

function Test-Endpoint([string]$Url, [int]$TimeoutSec = 5) {
  try {
    return Invoke-RestMethod -Uri $Url -TimeoutSec $TimeoutSec
  } catch {
    return $null
  }
}

$healthUrl = "http://127.0.0.1:$Port/health"
$routesUrl = "http://127.0.0.1:$Port/debug/routes"
$appiumUrl = "http://127.0.0.1:$Port/appium/status?timeout=0.7"

if ($CheckOnly) {
  Write-Host "Checking Host Agent: $healthUrl" -ForegroundColor Cyan
  $health = Test-Endpoint $healthUrl 5
  if ($null -eq $health) {
    Write-Host "FAIL: Host Agent is not responding." -ForegroundColor Red
    Write-Host "Hint: .\scripts\start_host_agent.ps1 -NewWindow" -ForegroundColor Yellow
    exit 1
  }

  $version = $health.version
  if (-not $version) { $version = "old/unknown" }
  Write-Host "Health OK. version=$version" -ForegroundColor Green

  $routes = Test-Endpoint $routesUrl 5
  if ($null -eq $routes) {
    Write-Host "WARN: /debug/routes not available. Host Agent may be old." -ForegroundColor Yellow
  } else {
    $routePaths = @($routes.routes | ForEach-Object { $_.path })
    if ($routePaths -notcontains "/appium/status") {
      Write-Host "FAIL: /appium/status route is missing. Restart Host Agent with: .\scripts\start_host_agent.ps1 -NewWindow" -ForegroundColor Red
      exit 2
    }
    Write-Host "Route OK: /appium/status" -ForegroundColor Green
  }

  $status = Test-Endpoint $appiumUrl 20
  if ($null -eq $status) {
    Write-Host "FAIL: /appium/status did not respond within 20 seconds." -ForegroundColor Red
    Write-Host "Check log: .\output\logs\host_agent.log" -ForegroundColor Yellow
    exit 3
  }
  Write-Host "Appium status endpoint OK. running=$($status.running) / count=$($status.count)" -ForegroundColor Green
  exit 0
}

$pids = @(Get-PortPids $Port)
if ($pids.Count -gt 0 -and -not $NoRestart) {
  Write-Host "Existing Host Agent/process found on port ${Port}: $($pids -join ', '). Stopping it first..." -ForegroundColor Yellow
  foreach ($procId in $pids) {
    try {
      taskkill /PID $procId /F /T | Out-Null
      Write-Host "Stopped PID $procId" -ForegroundColor DarkYellow
    } catch {
      Write-Host "Could not stop PID ${procId}: $($_.Exception.Message)" -ForegroundColor Red
    }
  }
  Start-Sleep -Milliseconds 800
} elseif ($pids.Count -gt 0) {
  Write-Host "Port ${Port} is already in use. Use default restart mode or stop the process manually." -ForegroundColor Yellow
}

$runner = Join-Path $PSScriptRoot "run_host_agent.ps1"
if (-not (Test-Path $runner)) {
  throw "Missing runner script: $runner"
}

if ($NewWindow) {
  Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-File", $runner,
    "-HostAddress", $HostAddress,
    "-Port", "$Port",
    "-ProjectRoot", $root
  )
  Write-Host "Host Agent window started: http://127.0.0.1:$Port" -ForegroundColor Green
  Write-Host "Wait 3 seconds, then run: .\scripts\start_host_agent.ps1 -CheckOnly" -ForegroundColor Cyan
} else {
  & $runner -HostAddress $HostAddress -Port $Port -ProjectRoot $root
}
