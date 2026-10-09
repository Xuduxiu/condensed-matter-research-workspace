$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"

Set-Location $Root
& $Python ".\scripts\run_daily_update.py" --apply
exit $LASTEXITCODE
