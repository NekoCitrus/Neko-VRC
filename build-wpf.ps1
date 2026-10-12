param(
    [string]$Dotnet = "$PSScriptRoot\.tools\dotnet\dotnet.exe",
    [string]$Python = "$PSScriptRoot\.venv\Scripts\python.exe"
)
$ErrorActionPreference = 'Stop'
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
if (-not (Test-Path -LiteralPath $Dotnet)) { $Dotnet = 'dotnet' }
$project = Join-Path $PSScriptRoot 'desktop\NekoVRC.Desktop\NekoVRC.Desktop.csproj'
[xml]$projectXml = Get-Content -LiteralPath $project -Raw
$releaseVersion = [string]$projectXml.Project.PropertyGroup.Version
if ($releaseVersion -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid release version' }
$releaseDir = Join-Path $PSScriptRoot "dist\Neko-VRC-v$releaseVersion"
$publishObj = Join-Path (Split-Path -Parent $project) 'obj\ReleasePackaging\'
Push-Location $PSScriptRoot
try {
    # IDE design-time restores can replace the shared assets file mid-publish.
    # Keep release restore and XAML intermediates separate from the IDE build.
    & $Dotnet publish $project -c Release -r win-x64 --self-contained true -o $releaseDir --nologo "-p:MSBuildProjectExtensionsPath=$publishObj" '-p:IntermediateOutputPath=obj/ReleasePackaging/Release/'
    if ($LASTEXITCODE -ne 0) { throw 'WPF publish failed' }
    & $Python -m PyInstaller --clean --noconfirm neko_vrc.spec
    if ($LASTEXITCODE -ne 0) { throw 'Backend build failed' }
    $backendDir = Join-Path $releaseDir 'backend'
    New-Item -ItemType Directory -Force -Path $backendDir | Out-Null
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'dist\Neko-VRC.Backend.exe') -Destination $backendDir
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination $releaseDir
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README_en.md') -Destination $releaseDir
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'doc') -Destination $releaseDir -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'assets') -Destination $releaseDir -Recurse -Force
    if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'LICENSE')) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'LICENSE') -Destination $releaseDir
    }
    Write-Host "Ready: $releaseDir\Neko-VRC.exe"
    $archive = Join-Path $PSScriptRoot "dist\Neko-VRC-v$releaseVersion-win-x64.zip"
    Compress-Archive -LiteralPath $releaseDir -DestinationPath $archive -Force
    Write-Host "Archive: $archive"
} finally { Pop-Location }
