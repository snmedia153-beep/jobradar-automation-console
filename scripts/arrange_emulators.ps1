param(
  [ValidateSet("grid2x2","horizontal","vertical")]
  [string]$Layout = "grid2x2",
  [int]$X = 20,
  [int]$Y = 40,
  [int]$Width = 430,
  [int]$Height = 780,
  [int]$Gap = 12,
  [int]$Columns = 2,
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (Test-Path ".\.venv\Scripts\Activate.ps1") {
  . .\.venv\Scripts\Activate.ps1
}

$env:PYTHONPATH = $root
if (-not $env:JOBRADAR_HOST_AGENT_URL) {
  $env:JOBRADAR_HOST_AGENT_URL = "http://127.0.0.1:8767"
}

$argsList = @("-m", "jobradar.cli", "arrange-emulators", "--layout", $Layout, "--x", $X, "--y", $Y, "--width", $Width, "--height", $Height, "--gap", $Gap, "--columns", $Columns)
if ($DryRun) { $argsList += "--dry-run" }
python @argsList
