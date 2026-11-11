param(
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9_-]*$')]
    [string]$Profile = 'desktop',
    [string]$DshHome = $env:DSH_HOME,
    [string]$DesktopExecutable,
    [switch]$Check,
    [ValidateRange(1,65535)][int]$ReloadFromPort
)

$ErrorActionPreference = 'Stop'
if (-not $DesktopExecutable) {
    $DesktopExecutable = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Programs\DeepSeek Harness\DeepSeek Harness.exe'
    if (-not (Test-Path -LiteralPath $DesktopExecutable -PathType Leaf)) {
        $DesktopExecutable = Join-Path $env:USERPROFILE 'AppData\Local\Programs\DeepSeek Harness\DeepSeek Harness.exe'
    }
}
if (-not (Test-Path -LiteralPath $DesktopExecutable -PathType Leaf)) {
    throw 'DeepSeek Harness Desktop was not found. Specify -DesktopExecutable with its full path.'
}
if (-not $DshHome) { $DshHome = Join-Path $env:USERPROFILE '.dsh' }

$installArgs = @((Join-Path $PSScriptRoot 'install.mjs'), $DesktopExecutable, $DshHome, $Profile)
if ($Check) { $installArgs += '--check' }
if ($ReloadFromPort -and -not $Check) { $installArgs += @('--reload', "$ReloadFromPort") }
$previousNodeMode = $env:ELECTRON_RUN_AS_NODE
try {
    $env:ELECTRON_RUN_AS_NODE = '1'
    & $DesktopExecutable @installArgs | Out-Host
    $exitCode = $LASTEXITCODE
} finally {
    $env:ELECTRON_RUN_AS_NODE = $previousNodeMode
}
exit $exitCode
