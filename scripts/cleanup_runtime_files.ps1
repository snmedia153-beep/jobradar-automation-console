# Optional cleanup script. Run from project root when you want to remove old runtime artifacts.
$paths = @(
  ".\output\*.sqlite3",
  ".\output\*.sqlite3-*",
  ".\output\*.json",
  ".\output\*.csv",
  ".\output\debug_*.html",
  ".\output\debug_*.png",
  ".\output\logs\*.log",
  ".\output\screenshots\*.png"
)
foreach ($p in $paths) {
  Remove-Item $p -Force -ErrorAction SilentlyContinue
}
Get-ChildItem -Path . -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem -Path . -Recurse -File -Include "*.pyc","*.pyo" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
Write-Host "Runtime cleanup completed."
