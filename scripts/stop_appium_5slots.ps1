param(
  [switch]$AllNode
)

$ports = @(4723, 4725, 4727, 4729, 4731)

if ($AllNode) {
  taskkill /F /IM node.exe /T
  exit
}

foreach ($port in $ports) {
  $lines = netstat -ano | Select-String (":" + $port + "\s") | Select-String "LISTENING"
  foreach ($line in $lines) {
    $parts = ($line.ToString() -split "\s+") | Where-Object { $_ }
    $pid = $parts[-1]
    if ($pid -match "^\d+$") {
      Write-Host "KILL Appium port $port PID $pid"
      taskkill /PID $pid /F
    }
  }
}
