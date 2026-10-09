param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PaperArgs
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Candidates = @(
    (Join-Path $Root "lab_paper_intake\.venv\Scripts\python.exe"),
    (Join-Path $Root "condmat-trend-radar\.venv\Scripts\python.exe")
)

$Python = $Candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $Python) {
    $Python = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $Python) {
    throw "Python was not found. Create either project virtual environment first."
}

& $Python (Join-Path $Root "paper_workspace.py") @PaperArgs
exit $LASTEXITCODE