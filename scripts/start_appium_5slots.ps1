param(
  [string]$AndroidSdk = $env:ANDROID_SDK_ROOT,
  [switch]$OnlyMissing,
  [string]$Ports = "4723,4725,4727,4729,4731"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $AndroidSdk) { $AndroidSdk = "F:\AppDevelop\Android\Sdk" }

$slots = @(
  @{ Name = "Emulator A"; Port = 4723 },
  @{ Name = "Emulator B"; Port = 4725 },
  @{ Name = "Emulator C"; Port = 4727 },
  @{ Name = "Emulator D"; Port = 4729 },
  @{ Name = "USB Device"; Port = 4731 }
)

$requestedPorts = @()
foreach ($token in ($Ports -split ',')) {
  $t = $token.Trim()
  if ($t) { $requestedPorts += [int]$t }
}

function Add-PathIfExists([string]$PathValue) {
  if ($PathValue -and (Test-Path $PathValue)) {
    $currentParts = @($env:PATH -split ';' | Where-Object { $_ })
    if ($currentParts -notcontains $PathValue) {
      $env:PATH = "$PathValue;$env:PATH"
    }
  }
}

function Test-PortListening([int]$TargetPort) {
  $line = netstat -ano | Select-String (":" + $TargetPort + "\s") | Select-String "LISTENING"
  return $null -ne $line
}

function Resolve-AppiumCommand() {
  $npmGlobal = Join-Path $env:APPDATA "npm"
  Add-PathIfExists $npmGlobal
  if ($env:APPIUM_COMMAND -and (Test-Path $env:APPIUM_COMMAND)) { return $env:APPIUM_COMMAND }
  $appiumCmd = Join-Path $npmGlobal "appium.cmd"
  if (Test-Path $appiumCmd) { return $appiumCmd }
  $cmd = Get-Command appium.cmd -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $cmd = Get-Command appium -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  throw "appium command not found. Run: npm i -g appium ; appium driver install uiautomator2"
}

$appium = Resolve-AppiumCommand
Write-Host "Appium command: $appium" -ForegroundColor Cyan
Write-Host "Android SDK   : $AndroidSdk" -ForegroundColor Cyan

$logDir = Join-Path $root "output\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

foreach ($slot in $slots) {
  $port = [int]$slot.Port
  if ($requestedPorts -notcontains $port) { continue }
  if ($OnlyMissing -and (Test-PortListening $port)) {
    Write-Host "SKIP $($slot.Name) : $port already LISTENING" -ForegroundColor DarkYellow
    continue
  }

  $title = "JobRadar Appium - $($slot.Name) : $port"
  $log = Join-Path $logDir "appium_$port.log"
  $launcher = Join-Path $logDir "manual_start_appium_$port.ps1"

  $script = @"
`$ErrorActionPreference = 'Continue'
try { `$host.UI.RawUI.WindowTitle = '$title' } catch { }
`$env:ANDROID_HOME = '$AndroidSdk'
`$env:ANDROID_SDK_ROOT = '$AndroidSdk'
`$env:PATH = '$AndroidSdk\platform-tools;$AndroidSdk\emulator;' + `$env:PATH
`$log = '$log'
`$appium = '$appium'
`$startedAt = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
"[`$startedAt] Starting Appium $($slot.Name) : $port" | Tee-Object -FilePath `$log -Append
"[`$startedAt] Appium command: `$appium" | Tee-Object -FilePath `$log -Append
"[`$startedAt] Android SDK: `$env:ANDROID_SDK_ROOT" | Tee-Object -FilePath `$log -Append
& `$appium --address 127.0.0.1 --port $port --allow-insecure '*:chromedriver_autodownload' 2>&1 | Tee-Object -FilePath `$log -Append
`$exitCode = `$LASTEXITCODE
"[WARN] Appium exited with code `$exitCode" | Tee-Object -FilePath `$log -Append
Read-Host 'Appium exited. Press Enter to close'
"@
  Set-Content -Path $launcher -Value $script -Encoding UTF8

  Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-File", $launcher
  )
  Write-Host "START $($slot.Name) : $port" -ForegroundColor Green
  Start-Sleep -Milliseconds 800
}

Write-Host ""
Write-Host "Check: netstat -ano | findstr ':472'" -ForegroundColor Cyan
Write-Host "Check: docker compose exec jobradar-api python -m jobradar.cli appium-health" -ForegroundColor Cyan
