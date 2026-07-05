param(
  [string]$HostAddress = "127.0.0.1",
  [int]$Port = 8767,
  [string]$ProjectRoot = ""
)

# Keep setup strict, but do NOT let normal uvicorn stderr INFO lines kill the runner.
$ErrorActionPreference = "Stop"

if (-not $ProjectRoot) {
  $ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}
Set-Location $ProjectRoot

$logDir = Join-Path $ProjectRoot "output\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "host_agent.log"

function Write-LogLine([string]$Message, [string]$Color = "Gray") {
  $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  $line = "[$ts] $Message"
  Write-Host $line -ForegroundColor $Color
  Add-Content -Path $logFile -Value $line -Encoding UTF8
}

function Resolve-PythonExe() {
  $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
  if (Test-Path $venvPython) { return $venvPython }

  $localPython = Get-Command python.exe -ErrorAction SilentlyContinue
  if ($localPython) { return $localPython.Source }

  $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
  if ($pyLauncher) { return $pyLauncher.Source }

  throw "Python executable not found. Create/activate .venv first, or install Python and ensure python.exe is available."
}

function Add-PathIfExists([string]$PathValue) {
  if ($PathValue -and (Test-Path $PathValue)) {
    $currentParts = @($env:PATH -split ';' | Where-Object { $_ })
    if ($currentParts -notcontains $PathValue) {
      $env:PATH = "$PathValue;$env:PATH"
    }
  }
}

$pythonExe = Resolve-PythonExe

# Make project, venv, Android SDK, and npm global commands visible to this host-side process.
$venvScripts = Join-Path $ProjectRoot ".venv\Scripts"
Add-PathIfExists $venvScripts

$defaultAndroidSdk = "F:\AppDevelop\Android\Sdk"
if (-not $env:ANDROID_SDK_ROOT -and (Test-Path $defaultAndroidSdk)) { $env:ANDROID_SDK_ROOT = $defaultAndroidSdk }
if (-not $env:ANDROID_HOME -and $env:ANDROID_SDK_ROOT) { $env:ANDROID_HOME = $env:ANDROID_SDK_ROOT }
if ($env:ANDROID_SDK_ROOT) {
  Add-PathIfExists (Join-Path $env:ANDROID_SDK_ROOT "platform-tools")
  Add-PathIfExists (Join-Path $env:ANDROID_SDK_ROOT "emulator")
}

$npmGlobal = Join-Path $env:APPDATA "npm"
Add-PathIfExists $npmGlobal
$appiumCmd = Join-Path $npmGlobal "appium.cmd"
if (Test-Path $appiumCmd) { $env:APPIUM_COMMAND = $appiumCmd }

$env:PYTHONPATH = $ProjectRoot
$env:JOBRADAR_HOST_AGENT_URL = "http://127.0.0.1:$Port"
$env:JOBRADAR_HOST_AGENT_TIMEOUT_SECONDS = "30"
$env:EMULATOR_SLOTS = "5"
$env:APPIUM_STATUS_PORTS = "4723,4725,4727,4729,4731"
$env:APPIUM_PORTS = "4723,4725,4727,4729,4731"
$env:USB_APPIUM_PORT = "4731"

try { $host.UI.RawUI.WindowTitle = "JobRadar Host Agent : $Port" } catch { }

Write-LogLine "Starting JobRadar Host Agent" "Green"
Write-LogLine "ProjectRoot=$ProjectRoot" "Cyan"
Write-LogLine "Python=$pythonExe" "Cyan"
Write-LogLine "ANDROID_SDK_ROOT=$env:ANDROID_SDK_ROOT" "Cyan"
Write-LogLine "APPIUM_COMMAND=$env:APPIUM_COMMAND" "Cyan"
Write-LogLine "URL=http://$HostAddress`:$Port" "Cyan"
Write-LogLine "Note: uvicorn may print normal INFO lines to stderr. This runner captures them as log lines, not failures." "DarkGray"
Write-LogLine "Command=& `"$pythonExe`" -m jobradar.cli host-agent --host $HostAddress --port $Port" "DarkGray"

# PowerShell gotcha:
# Do NOT put `2>&1` inside a quoted command string. Python then receives it as a CLI argument.
# Do NOT run native stderr with ErrorActionPreference=Stop. Uvicorn INFO lines can become NativeCommandError.
# Run directly, merge streams at the PowerShell operator level, and treat all output as plain log text.
$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
  & $pythonExe -m jobradar.cli host-agent --host $HostAddress --port $Port 2>&1 | ForEach-Object {
    $text = $_.ToString()
    Write-Host $text
    Add-Content -Path $logFile -Value $text -Encoding UTF8
  }
  $exitCode = $LASTEXITCODE
  if ($null -eq $exitCode) { $exitCode = 0 }
  Write-LogLine "Host Agent exited with code $exitCode" "Yellow"
  exit $exitCode
} catch {
  Write-LogLine "Host Agent failed: $($_.Exception.Message)" "Red"
  Write-LogLine "PATH=$env:PATH" "DarkYellow"
  exit 1
} finally {
  $ErrorActionPreference = $previousErrorActionPreference
}
