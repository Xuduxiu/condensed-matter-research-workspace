$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
Set-Location $Root

$backend = Start-Process -FilePath $Python -WindowStyle Hidden -PassThru -ArgumentList @(
  "-m", "backend.api.main"
)

try {
  Set-Location "$Root\frontend"
  npm.cmd run dev
} finally {
  if (-not $backend.HasExited) {
    Stop-Process -Id $backend.Id
  }
}
