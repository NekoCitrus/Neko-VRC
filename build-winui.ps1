param(
    [string]$Dotnet = "$PSScriptRoot\.tools\dotnet\dotnet.exe",
    [string]$Python = "$PSScriptRoot\.venv\Scripts\python.exe",
    [switch]$SkipValidation
)
$ErrorActionPreference = 'Stop'
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
if (-not (Test-Path -LiteralPath $Dotnet)) { $Dotnet = 'dotnet' }
$project = Join-Path $PSScriptRoot 'desktop\NekoVRC.WinUI\NekoVRC.WinUI.csproj'
[xml]$projectXml = Get-Content -LiteralPath $project -Raw
$releaseVersion = [string]$projectXml.Project.PropertyGroup.Version
if ($releaseVersion -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid release version' }
# Versioned product directories keep final packages separate from intermediates.
$productRoot = Join-Path $PSScriptRoot "products\v$releaseVersion"
$releaseDir = Join-Path $productRoot 'Neko-VRC-WinUI-win-x64'
$backendBuild = Join-Path $PSScriptRoot 'build\winui-backend'
$publishObj = Join-Path (Split-Path -Parent $project) 'obj\ReleasePackaging\'
Push-Location $PSScriptRoot
try {
    & $Dotnet publish $project -c Release -r win-x64 --self-contained true -o $releaseDir --nologo '-p:Platform=x64' "-p:MSBuildProjectExtensionsPath=$publishObj" '-p:IntermediateOutputPath=obj/ReleasePackaging/Release/'
    if ($LASTEXITCODE -ne 0) { throw 'WinUI publish failed' }
    foreach ($markup in @('Neko-VRC.pri', 'App.xbf', 'MainWindow.xbf', 'Views\GeneralPage.xbf', 'Views\DevicePage.xbf', 'Views\DeviceView.xbf', 'Views\DebugPage.xbf', 'Views\AboutPage.xbf')) {
        if (-not (Test-Path -LiteralPath (Join-Path $releaseDir $markup))) { throw "Missing compiled XAML: $markup" }
    }
    & $Python -m PyInstaller --clean --noconfirm --distpath (Join-Path $backendBuild 'dist') --workpath (Join-Path $backendBuild 'work') neko_vrc.spec
    if ($LASTEXITCODE -ne 0) { throw 'Backend build failed' }
    $backendDir = Join-Path $releaseDir 'backend'
    New-Item -ItemType Directory -Force -Path $backendDir | Out-Null
    Copy-Item -LiteralPath (Join-Path $backendBuild 'dist\Neko-VRC.Backend.exe') -Destination $backendDir
    foreach ($item in @('README.md', 'README_en.md', 'LICENSE', 'THIRD-PARTY-NOTICES.txt')) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $item) -Destination $releaseDir
    }
    foreach ($directory in @('doc', 'assets')) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $directory) -Destination $releaseDir -Recurse -Force
    }
    if (-not $SkipValidation) {
        foreach ($testMode in @('--smoke-test', '--smoke-test-close')) {
            $testProfile = Join-Path $PSScriptRoot ('.validation\winui-package-' + [guid]::NewGuid().ToString('N'))
            $testProcess = Start-Process -FilePath (Join-Path $releaseDir 'Neko-VRC.exe') -ArgumentList $testMode, ('"' + $testProfile + '"') -WindowStyle Hidden -PassThru
            if (-not $testProcess.WaitForExit(55000)) {
                $testProcess.Kill()
                throw "Packaged WinUI integration test timed out: $testMode"
            }
            if ($testProcess.ExitCode -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $testProfile 'result.txt'))) {
                throw "Packaged WinUI integration test failed (exit $($testProcess.ExitCode)); see $testProfile"
            }
            Get-Content -LiteralPath (Join-Path $testProfile 'result.txt')
        }
    }
    $archive = Join-Path $productRoot "Neko-VRC-WinUI-v$releaseVersion-win-x64.zip"
    Compress-Archive -LiteralPath $releaseDir -DestinationPath $archive -Force
    Write-Host "Ready: $releaseDir\Neko-VRC.exe"
    Write-Host "Archive: $archive"
} finally { Pop-Location }
