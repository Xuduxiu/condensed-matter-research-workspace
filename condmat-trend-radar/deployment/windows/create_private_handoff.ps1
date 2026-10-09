[CmdletBinding()]
# Keep this file UTF-8 with BOM for Windows PowerShell 5.1 because it contains Chinese literals.
param(
    [Parameter(Mandatory=$true)][string]$ApiConfigPath,
    [string]$ReleaseZip = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$ReleaseDir = Join-Path $ProjectRoot "releases"
$ApiConfigPath = [IO.Path]::GetFullPath($ApiConfigPath)
if (-not (Test-Path -LiteralPath $ApiConfigPath)) { throw "API config file not found: $ApiConfigPath" }
if ([string]::IsNullOrWhiteSpace($ReleaseZip)) {
    $ReleaseZip = Get-ChildItem -LiteralPath $ReleaseDir -Filter "CondMatRadar_v*_Windows_x64.zip" -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $ReleaseZip -or -not (Test-Path -LiteralPath $ReleaseZip)) { throw "Public release ZIP not found." }
$ReleaseZip = [IO.Path]::GetFullPath($ReleaseZip)
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$Kit = Join-Path $ReleaseDir "PRIVATE_Sister_Install_Kit_$stamp"
New-Item -ItemType Directory -Path $Kit, (Join-Path $Kit "private") -Force | Out-Null
Expand-Archive -LiteralPath $ReleaseZip -DestinationPath $Kit -Force
$Stage = Get-ChildItem -LiteralPath $Kit -Directory | Where-Object { $_.Name -like "CondMatRadar_v*_Windows_x64" } | Select-Object -First 1
if (-not $Stage) { throw "Release folder was not found after extraction." }
Copy-Item -LiteralPath $ApiConfigPath -Destination (Join-Path $Kit "private\.env.local") -Force

$wrapper = @"
@echo off
chcp 65001 >nul
setlocal
call "%~dp0$($Stage.Name)\install.cmd" -ApiConfigPath "%~dp0private\.env.local"
"@
[IO.File]::WriteAllText((Join-Path $Kit "一键安装_使用我的API.cmd"), $wrapper, (New-Object Text.UTF8Encoding($false)))
$note = @"
这是私人安装交接目录，包含你的 API 配置，不能发到群里或网盘公开链接。

在师姐电脑上：
1. 把整个文件夹复制到本机。
2. 双击“一键安装_使用我的API.cmd”。
3. 安装完成并确认可用后，删除这个私人交接目录以及 U 盘上的副本。
4. 桌面“CondMat Radar”用于打开；开始菜单“Stop CondMat Radar”用于关闭后台服务。

程序默认安装在当前用户的 LocalAppData，不需要管理员、Python 或 Node。
你的 API 配置位于目标机：%LOCALAPPDATA%\CondMatRadar\config\.env.local
后续换成师姐自己的 API 时只需替换该文件中的对应值并重新启动。
"@
[IO.File]::WriteAllText((Join-Path $Kit "安装后请删除此目录.txt"), $note, (New-Object Text.UTF8Encoding($false)))

$keys = @{}
foreach ($line in [IO.File]::ReadAllLines((Join-Path $Kit "private\.env.local"), [Text.Encoding]::UTF8)) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $keys[$Matches[1]] = -not [string]::IsNullOrWhiteSpace($Matches[2]) }
}
$status = [ordered]@{
    kit = $Kit
    public_release = $Stage.Name
    deepseek_configured = [bool]$keys["DEEPSEEK_API_KEY"]
    openalex_configured = [bool]$keys["OPENALEX_API_KEY"]
    semantic_scholar_configured = [bool]$keys["SEMANTIC_SCHOLAR_API_KEY"]
    contains_private_api_config = $true
    zipped = $false
}
$status | ConvertTo-Json -Depth 4