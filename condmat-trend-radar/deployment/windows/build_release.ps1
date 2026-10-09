[CmdletBinding()]
param(
    [string]$Version = "2.4.0",
    [switch]$SkipFrontendBuild,
    [switch]$SkipApplicationBuild
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { throw "Project virtual environment is missing: $Python" }
if (-not $SkipFrontendBuild) {
    Push-Location (Join-Path $ProjectRoot "frontend")
    try { & npm.cmd run build; if ($LASTEXITCODE -ne 0) { throw "Frontend build failed." } }
    finally { Pop-Location }
}
$FrontendDist = Join-Path $ProjectRoot "frontend\dist"
if (-not (Test-Path -LiteralPath (Join-Path $FrontendDist "index.html"))) { throw "frontend\dist is missing." }
$BuildRoot = Join-Path $ProjectRoot "build\windows-release"
$Work = Join-Path $BuildRoot "work"
$Dist = Join-Path $BuildRoot "dist"
$Spec = Join-Path $BuildRoot "spec"
if (-not $SkipApplicationBuild) {
& $Python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) { throw "PyInstaller is missing. Install requirements-build.txt into the project venv first." }

New-Item -ItemType Directory -Path $Work, $Dist, $Spec -Force | Out-Null
$Entry = Join-Path $ProjectRoot "backend\desktop.py"
$Schema = Join-Path $ProjectRoot "backend\db\schema.sql"
$addFrontend = "$FrontendDist;frontend_dist"
$addSchema = "$Schema;backend\db"
$arguments = @(
    "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed", "--noupx",
    "--name", "CondMatRadar", "--distpath", $Dist, "--workpath", $Work, "--specpath", $Spec,
    "--paths", $ProjectRoot,
    "--add-data", $addFrontend, "--add-data", $addSchema,
    "--collect-all", "pymupdf", "--collect-submodules", "fitz",
    "--hidden-import", "uvicorn.logging", "--hidden-import", "uvicorn.loops.auto",
    "--hidden-import", "uvicorn.protocols.http.auto", "--hidden-import", "uvicorn.protocols.websockets.auto",
    "--hidden-import", "uvicorn.lifespan.on", $Entry
)
& $Python @arguments
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
}
$BuiltApp = Join-Path $Dist "CondMatRadar"
if (-not (Test-Path -LiteralPath (Join-Path $BuiltApp "CondMatRadar.exe"))) { throw "Packaged executable is missing." }

$ReleaseDir = Join-Path $ProjectRoot "releases"
$StageName = "CondMatRadar_v${Version}_Windows_x64"
$Stage = Join-Path $ReleaseDir $StageName
$Zip = Join-Path $ReleaseDir "$StageName.zip"
if (Test-Path -LiteralPath $Stage) { Remove-Item -LiteralPath $Stage -Recurse -Force }
if (Test-Path -LiteralPath $Zip) { Remove-Item -LiteralPath $Zip -Force }
New-Item -ItemType Directory -Path (Join-Path $Stage "app"), (Join-Path $Stage "config"), (Join-Path $Stage "docs") -Force | Out-Null
Copy-Item -Path (Join-Path $BuiltApp "*") -Destination (Join-Path $Stage "app") -Recurse -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "install.ps1") -Destination (Join-Path $Stage "install.ps1")
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "install.cmd") -Destination (Join-Path $Stage "install.cmd")
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "uninstall.ps1") -Destination (Join-Path $Stage "uninstall.ps1")
Copy-Item -LiteralPath (Join-Path $ProjectRoot ".env.example") -Destination (Join-Path $Stage "config\.env.example")
Copy-Item -LiteralPath (Join-Path $ProjectRoot "docs\WINDOWS_INSTALL_HANDOFF.md") -Destination (Join-Path $Stage "docs\WINDOWS_INSTALL_HANDOFF.md")
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "README_INSTALL.txt") -Destination (Join-Path $Stage "README_INSTALL.txt")

$versionPayload = [ordered]@{
    product = "CondMat Radar"
    version = $Version
    architecture = "windows-x64"
    build_date = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    runtime = "PyInstaller onedir"
    frontend = "React production build, served by FastAPI"
    secrets_included = $false
}
[IO.File]::WriteAllText((Join-Path $Stage "VERSION.json"), ($versionPayload | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))

$ForbiddenNames = @(".env", ".env.local", "deepseek_api.txt", "papers.db", "condmat_radar.sqlite")
$badNames = Get-ChildItem -LiteralPath $Stage -Recurse -File | Where-Object { $ForbiddenNames -contains $_.Name.ToLowerInvariant() }
if ($badNames) { throw "Forbidden files in public release: $($badNames.FullName -join ', ')" }
$TextExtensions = @(".ps1", ".cmd", ".txt", ".md", ".json", ".example", ".yaml", ".yml")
$secretPattern = '(?i)(?:sk-|api[_-]?key[ \t]*[=:][ \t]*["'']?)[A-Za-z0-9_-]{20,}'
$secretHits = @()
Get-ChildItem -LiteralPath $Stage -Recurse -File | Where-Object { $TextExtensions -contains $_.Extension.ToLowerInvariant() } | ForEach-Object {
    $content = [IO.File]::ReadAllText($_.FullName, [Text.Encoding]::UTF8)
    if ($content -match $secretPattern) { $secretHits += $_.FullName }
}
if ($secretHits.Count -gt 0) { throw "Possible secret material in public release: $($secretHits -join ', ')" }

$files = Get-ChildItem -LiteralPath $Stage -Recurse -File | Sort-Object FullName | ForEach-Object {
    [ordered]@{
        path = $_.FullName.Substring($Stage.Length + 1).Replace('\', '/')
        size = $_.Length
        sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}
$manifest = [ordered]@{
    product = "CondMat Radar"
    version = $Version
    generated_at = [DateTime]::UtcNow.ToString("o")
    file_count = @($files).Count
    files = @($files)
}
[IO.File]::WriteAllText((Join-Path $Stage "release-manifest.json"), ($manifest | ConvertTo-Json -Depth 6), (New-Object Text.UTF8Encoding($false)))
Compress-Archive -LiteralPath $Stage -DestinationPath $Zip -CompressionLevel Optimal

Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::OpenRead($Zip)
try {
    $badZip = @($archive.Entries | Where-Object {
        $name = [IO.Path]::GetFileName($_.FullName).ToLowerInvariant()
        $ForbiddenNames -contains $name
    })
    if ($badZip.Count -gt 0) { throw "Forbidden entries in final ZIP." }
} finally { $archive.Dispose() }

$result = [ordered]@{
    stage = $Stage
    zip = $Zip
    zip_size = (Get-Item -LiteralPath $Zip).Length
    zip_sha256 = (Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash.ToLowerInvariant()
    secrets_included = $false
}
$result | ConvertTo-Json -Depth 4