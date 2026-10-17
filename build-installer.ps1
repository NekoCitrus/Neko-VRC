param(
    [string]$ISCC,
    [string]$ProductDirectory,
    [switch]$SkipAppBuild,
    [switch]$SkipValidation
)
$ErrorActionPreference = 'Stop'
[xml]$project = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'desktop/NekoVRC.WinUI/NekoVRC.WinUI.csproj') -Raw
$version = [string]$project.Project.PropertyGroup.Version
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid release version' }
$productRoot = if ($ProductDirectory) { [IO.Path]::GetFullPath($ProductDirectory) } else { Join-Path $PSScriptRoot "products/v$version" }
$payload = Join-Path $productRoot 'Neko-VRC-WinUI-win-x64'
if (-not $ISCC) {
    $candidates = @(
        (Join-Path $PSScriptRoot '.tools/inno-6.7.3/ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6/ISCC.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs/Inno Setup 6/ISCC.exe')
    )
    $ISCC = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $ISCC) { $ISCC = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source }
}
if (-not $ISCC -or -not (Test-Path -LiteralPath $ISCC)) { throw 'Install Inno Setup 6.7.3+, or supply -ISCC path/to/ISCC.exe.' }
if (-not $SkipAppBuild) {
    & (Join-Path $PSScriptRoot 'build-winui.ps1') -ProductDirectory $productRoot -SkipValidation:$SkipValidation
    if ($LASTEXITCODE -ne 0) { throw 'Application build failed' }
}
$frontend = Join-Path $payload 'Neko-VRC.exe'
$backend = Join-Path $payload 'backend/Neko-VRC.Backend.exe'
foreach ($file in @($frontend, $backend)) {
    if (-not (Test-Path -LiteralPath $file)) { throw "Missing payload: $file" }
    if ((Get-Item -LiteralPath $file).VersionInfo.ProductVersion -notmatch ('^' + [regex]::Escape($version) + '(\+|$)')) { throw "Stale payload version: $file" }
}
& $ISCC "/DProductDir=$payload" "/DProductVersion=$version" "/DOutputDir=$productRoot" (Join-Path $PSScriptRoot 'installer/Neko-VRC.iss')
if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed' }
$installer = Join-Path $productRoot "Neko-VRC-Setup-v$version-win-x64.exe"
if (-not $SkipValidation) {
    & (Join-Path $PSScriptRoot 'test-installer.ps1') -InstallerPath $installer -ProductDirectory $productRoot
}
$artifacts = @($installer, (Join-Path $productRoot "Neko-VRC-WinUI-v$version-win-x64.zip"))
$lines = foreach ($file in $artifacts) {
    if (Test-Path -LiteralPath $file) { (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() + '  ' + (Split-Path -Leaf $file) }
}
[IO.File]::WriteAllLines((Join-Path $productRoot 'SHA256SUMS.txt'), $lines, [Text.Encoding]::ASCII)
Write-Host "Installer: $installer"
