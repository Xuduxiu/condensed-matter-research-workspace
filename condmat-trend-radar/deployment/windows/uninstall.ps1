[CmdletBinding()]
param([switch]$RemoveUserData)

$ErrorActionPreference = "Stop"
$InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ManifestPath = Join-Path $InstallDir "install-manifest.json"
if (-not (Test-Path -LiteralPath $ManifestPath)) { throw "Install manifest is missing; refusing to remove an unknown directory." }
$Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($Manifest.product_id -ne "CondMatRadar") { throw "Unexpected product id; refusing to uninstall." }

Get-ChildItem -LiteralPath (Join-Path $InstallDir "app") -Filter "CondMatRadar.exe" -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
    try { & $_.FullName --stop | Out-Null } catch { }
}
Start-Sleep -Milliseconds 800

$DesktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "CondMat Radar.lnk"
$StartMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\CondMat Radar"
Remove-Item -LiteralPath $DesktopShortcut -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $StartMenu -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\CondMatRadar" -Recurse -Force -ErrorAction SilentlyContinue

if ($RemoveUserData) {
    $AppHome = [IO.Path]::GetFullPath([string]$Manifest.app_home)
    $LocalRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "CondMatRadar"))
    if ($AppHome -ne $LocalRoot) { throw "Unexpected app data path; refusing to remove user data." }
    Remove-Item -LiteralPath $AppHome -Recurse -Force -ErrorAction SilentlyContinue
}

Set-Location $env:TEMP
Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "CondMat Radar was removed. User data was " -NoNewline
if ($RemoveUserData) { Write-Host "deleted." } else { Write-Host "preserved at $($Manifest.app_home)." }