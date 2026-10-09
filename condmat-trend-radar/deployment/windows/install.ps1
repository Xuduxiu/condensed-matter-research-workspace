[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\CondMatRadar"),
    [string]$ApiConfigPath = "",
    [string]$DataDir = "",
    [switch]$NoLaunch,
    [switch]$NoDesktopShortcut
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

if (-not $env:LOCALAPPDATA) { throw "LOCALAPPDATA is not available." }
$ReleaseRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$SourceApp = Join-Path $ReleaseRoot "app"
$SourceExe = Join-Path $SourceApp "CondMatRadar.exe"
if (-not (Test-Path -LiteralPath $SourceExe)) { throw "Release payload is incomplete: app\CondMatRadar.exe is missing." }

$VersionFile = Join-Path $ReleaseRoot "VERSION.json"
$Version = "2.4.0"
if (Test-Path -LiteralPath $VersionFile) {
    try { $Version = [string]((Get-Content -LiteralPath $VersionFile -Raw -Encoding UTF8 | ConvertFrom-Json).version) } catch { }
}
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$AppHome = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "CondMatRadar"))
if ([string]::IsNullOrWhiteSpace($DataDir)) { $DataDir = Join-Path $AppHome "data" }
$DataDir = [IO.Path]::GetFullPath($DataDir)
$ConfigDir = Join-Path $AppHome "config"
$ConfigPath = Join-Path $ConfigDir ".env.local"
$TargetApp = Join-Path (Join-Path $InstallDir "app") $Version
$TargetExe = Join-Path $TargetApp "CondMatRadar.exe"

function Read-EnvFile([string]$Path) {
    $map = [ordered]@{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    foreach ($raw in [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8)) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) { continue }
        $pair = $line.Split(@("="), 2, [StringSplitOptions]::None)
        $name = $pair[0].Trim()
        $value = $pair[1].Trim().Trim('"').Trim("'")
        if ($name -match '^[A-Za-z_][A-Za-z0-9_]*$') { $map[$name] = $value }
    }
    return $map
}

function Write-EnvFile([string]$Path, $Map) {
    $order = @(
        "CONDMAT_RADAR_DATA_DIR", "CONDMAT_RADAR_DB", "CONDMAT_RADAR_CACHE",
        "CONDMAT_RADAR_EXPORT", "CONDMAT_RADAR_LOGS", "CONDMAT_RADAR_LOCKS",
        "CONDMAT_RADAR_API_HOST", "CONDMAT_RADAR_API_PORT",
        "OPENALEX_API_KEY", "OPENALEX_MAILTO", "UNPAYWALL_EMAIL",
        "SEMANTIC_SCHOLAR_API_KEY", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL",
        "DEEPSEEK_MODEL_FAST", "DEEPSEEK_MODEL_PRO"
    )
    $lines = New-Object Collections.Generic.List[string]
    $lines.Add("# Machine-local CondMat Radar configuration. Do not share this file.")
    $written = @{}
    foreach ($name in $order) {
        if ($Map.Contains($name)) {
            $value = [string]$Map[$name]
            if ($value.Contains("`r") -or $value.Contains("`n")) { throw "Invalid newline in configuration value: $name" }
            $lines.Add("$name=$value")
            $written[$name] = $true
        }
    }
    foreach ($name in $Map.Keys) {
        if (-not $written.ContainsKey([string]$name)) {
            $value = [string]$Map[$name]
            if ($value.Contains("`r") -or $value.Contains("`n")) { throw "Invalid newline in configuration value: $name" }
            $lines.Add("$name=$value")
        }
    }
    [IO.File]::WriteAllLines($Path, $lines, (New-Object Text.UTF8Encoding($false)))
}

Write-Host "Installing CondMat Radar $Version for the current Windows user..."
Get-ChildItem -LiteralPath (Join-Path $InstallDir "app") -Filter "CondMatRadar.exe" -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
    try { & $_.FullName --stop | Out-Null } catch { }
}
Start-Sleep -Milliseconds 800

New-Item -ItemType Directory -Path $TargetApp, $ConfigDir, $DataDir -Force | Out-Null
Copy-Item -Path (Join-Path $SourceApp "*") -Destination $TargetApp -Recurse -Force
Copy-Item -LiteralPath (Join-Path $ReleaseRoot "uninstall.ps1") -Destination (Join-Path $InstallDir "uninstall.ps1") -Force

$config = Read-EnvFile $ConfigPath
$config["CONDMAT_RADAR_DATA_DIR"] = $DataDir
$config["CONDMAT_RADAR_DB"] = Join-Path $DataDir "condmat_radar.sqlite"
$config["CONDMAT_RADAR_CACHE"] = Join-Path $DataDir "cache"
$config["CONDMAT_RADAR_EXPORT"] = Join-Path $DataDir "exports"
$config["CONDMAT_RADAR_LOGS"] = Join-Path $DataDir "logs"
$config["CONDMAT_RADAR_LOCKS"] = Join-Path $DataDir "locks"
$config["CONDMAT_RADAR_API_HOST"] = "127.0.0.1"
if (-not $config.Contains("CONDMAT_RADAR_API_PORT")) { $config["CONDMAT_RADAR_API_PORT"] = "8765" }
if (-not $config.Contains("DEEPSEEK_BASE_URL")) { $config["DEEPSEEK_BASE_URL"] = "https://api.deepseek.com" }
if (-not $config.Contains("DEEPSEEK_MODEL_FAST")) { $config["DEEPSEEK_MODEL_FAST"] = "deepseek-v4-flash" }
if (-not $config.Contains("DEEPSEEK_MODEL_PRO")) { $config["DEEPSEEK_MODEL_PRO"] = "deepseek-v4-pro" }

if ([string]::IsNullOrWhiteSpace($ApiConfigPath)) {
    $BundledPrivateConfig = Join-Path $ReleaseRoot "private\.env.local"
    if (Test-Path -LiteralPath $BundledPrivateConfig) { $ApiConfigPath = $BundledPrivateConfig }
}
$AllowedApiKeys = @(
    "OPENALEX_API_KEY", "OPENALEX_MAILTO", "UNPAYWALL_EMAIL",
    "SEMANTIC_SCHOLAR_API_KEY", "S2_API_KEY", "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "DEEPSEEK_MODEL_FAST", "DEEPSEEK_MODEL_PRO"
)
if (-not [string]::IsNullOrWhiteSpace($ApiConfigPath)) {
    $ApiConfigPath = [IO.Path]::GetFullPath($ApiConfigPath)
    if (-not (Test-Path -LiteralPath $ApiConfigPath)) { throw "API configuration file not found: $ApiConfigPath" }
    $api = Read-EnvFile $ApiConfigPath
    foreach ($name in $AllowedApiKeys) {
        if ($api.Contains($name)) { $config[$name] = $api[$name] }
    }
}
Write-EnvFile $ConfigPath $config

try {
    $acl = New-Object Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $system = New-Object Security.Principal.SecurityIdentifier("S-1-5-18")
    $inheritance = [Security.AccessControl.InheritanceFlags]::None
    $propagation = [Security.AccessControl.PropagationFlags]::None
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($user, "FullControl", $inheritance, $propagation, $allow)))
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($system, "FullControl", $inheritance, $propagation, $allow)))
    Set-Acl -LiteralPath $ConfigPath -AclObject $acl
} catch {
    Write-Warning "Could not tighten the config ACL. Keep the file private: $ConfigPath"
}

$Shell = $null
try {
    $Shell = New-Object -ComObject WScript.Shell
    $StartMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\CondMat Radar"
    New-Item -ItemType Directory -Path $StartMenu -Force | Out-Null
    $OpenShortcut = $Shell.CreateShortcut((Join-Path $StartMenu "CondMat Radar.lnk"))
    $OpenShortcut.TargetPath = $TargetExe
    $OpenShortcut.WorkingDirectory = $TargetApp
    $OpenShortcut.IconLocation = "$TargetExe,0"
    $OpenShortcut.Save()
    $StopShortcut = $Shell.CreateShortcut((Join-Path $StartMenu "Stop CondMat Radar.lnk"))
    $StopShortcut.TargetPath = $TargetExe
    $StopShortcut.Arguments = "--stop"
    $StopShortcut.WorkingDirectory = $TargetApp
    $StopShortcut.IconLocation = "$TargetExe,0"
    $StopShortcut.Save()
} catch {
    Write-Warning "Start menu shortcuts could not be created. The program is still installed at: $TargetExe"
}
if (-not $NoDesktopShortcut) {
    try {
        if (-not $Shell) { $Shell = New-Object -ComObject WScript.Shell }
        $Desktop = [Environment]::GetFolderPath("Desktop")
        if ([string]::IsNullOrWhiteSpace($Desktop)) { throw "Windows did not return a Desktop path." }
        $DesktopShortcut = $Shell.CreateShortcut((Join-Path $Desktop "CondMat Radar.lnk"))
        $DesktopShortcut.TargetPath = $TargetExe
        $DesktopShortcut.WorkingDirectory = $TargetApp
        $DesktopShortcut.IconLocation = "$TargetExe,0"
        $DesktopShortcut.Save()
    } catch {
        Write-Warning "Desktop shortcut could not be created. Use the Start menu or launch: $TargetExe"
    }
}

$manifest = [ordered]@{
    product_id = "CondMatRadar"
    version = $Version
    install_dir = $InstallDir
    app_home = $AppHome
    data_dir = $DataDir
    installed_at = [DateTime]::UtcNow.ToString("o")
}
[IO.File]::WriteAllText((Join-Path $InstallDir "install-manifest.json"), ($manifest | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))

try {
    $UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\CondMatRadar"
    New-Item -Path $UninstallKey -Force | Out-Null
    Set-ItemProperty -Path $UninstallKey -Name DisplayName -Value "CondMat Radar"
    Set-ItemProperty -Path $UninstallKey -Name DisplayVersion -Value $Version
    Set-ItemProperty -Path $UninstallKey -Name Publisher -Value "CondMat Radar Lab"
    Set-ItemProperty -Path $UninstallKey -Name InstallLocation -Value $InstallDir
    Set-ItemProperty -Path $UninstallKey -Name DisplayIcon -Value $TargetExe
    $UninstallCommand = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $InstallDir "uninstall.ps1") + '"'
    Set-ItemProperty -Path $UninstallKey -Name UninstallString -Value $UninstallCommand
    Set-ItemProperty -Path $UninstallKey -Name NoModify -Type DWord -Value 1
    Set-ItemProperty -Path $UninstallKey -Name NoRepair -Type DWord -Value 1
} catch {
    Write-Warning "Windows app registration was skipped; program shortcuts are still available."
}

$DeepSeekConfigured = $config.Contains("DEEPSEEK_API_KEY") -and -not [string]::IsNullOrWhiteSpace([string]$config["DEEPSEEK_API_KEY"])
$OpenAlexConfigured = $config.Contains("OPENALEX_API_KEY") -and -not [string]::IsNullOrWhiteSpace([string]$config["OPENALEX_API_KEY"])
Write-Host "Install complete."
Write-Host "  Program: $TargetExe"
Write-Host "  Data:    $DataDir"
Write-Host "  Config:  $ConfigPath"
Write-Host "  DeepSeek configured: $DeepSeekConfigured"
Write-Host "  OpenAlex configured: $OpenAlexConfigured"
if (-not $NoLaunch) { Start-Process -FilePath $TargetExe -WindowStyle Hidden }