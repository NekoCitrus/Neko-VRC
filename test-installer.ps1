param(
    [Parameter(Mandatory=$true)][string]$InstallerPath,
    [Parameter(Mandatory=$true)][string]$ProductDirectory
)
$ErrorActionPreference = 'Stop'
$InstallerPath = (Resolve-Path -LiteralPath $InstallerPath).Path
$payload = Join-Path ([IO.Path]::GetFullPath($ProductDirectory)) 'Neko-VRC-WinUI-win-x64'
$key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{B6104E75-6F7D-4B4E-9D05-AD41251A78EF}_is1'
if (Test-Path -LiteralPath $key) { throw 'Neko-VRC is already installed. Installer validation will not modify an existing installation.' }
$validation = Join-Path $PSScriptRoot ('.validation/installer-' + [guid]::NewGuid().ToString('N'))
$installDir = Join-Path $validation 'app'
$group = 'Neko-VRC Validation ' + [guid]::NewGuid().ToString('N')
New-Item -ItemType Directory -Path $validation | Out-Null
function UserDataHashes {
    $config = Join-Path $env:APPDATA 'Neko-VRC'
    @(if (Test-Path -LiteralPath $config) {
        Get-ChildItem -LiteralPath $config -File -Recurse | Sort-Object FullName | ForEach-Object {
            $_.FullName + ':' + (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
        }
    }) -join "`n"
}
function RunHidden([string]$Executable, [string[]]$Arguments, [int]$Timeout = 55000) {
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit($Timeout)) { throw "Validation timed out: $Executable" }
    if ($process.ExitCode -ne 0) { throw "Validation failed (exit $($process.ExitCode)): $Executable; logs in $validation" }
}
$before = UserDataHashes
$installed = $false
try {
    $arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/TASKS=',
        ('/DIR="' + $installDir + '"'), ('/GROUP="' + $group + '"'), ('/LOG="' + (Join-Path $validation 'install.log') + '"'))
    RunHidden $InstallerPath $arguments
    $installed = $true
    if (-not (Test-Path -LiteralPath $key)) { throw 'Missing per-user uninstall registration' }
    foreach ($file in Get-ChildItem -LiteralPath $payload -Recurse -File) {
        $relative = $file.FullName.Substring($payload.Length + 1)
        if ($relative -match '^(log|config)[\\/]' -or $file.Extension -eq '.pdb') { continue }
        $target = Join-Path $installDir $relative
        if (-not (Test-Path -LiteralPath $target) -or (Get-FileHash -LiteralPath $file.FullName).Hash -ne (Get-FileHash -LiteralPath $target).Hash) { throw "Payload mismatch: $relative" }
    }
    $shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) ($group + '/Neko-VRC.lnk')
    if (-not (Test-Path -LiteralPath $shortcut)) { throw 'Missing Start menu entry' }
    New-Item -ItemType Directory -Path (Join-Path $installDir 'log') | Out-Null
    $sentinel = Join-Path $installDir 'log/preserve-on-upgrade.txt'
    [IO.File]::WriteAllText($sentinel, 'Installer validation only')
    RunHidden $InstallerPath $arguments
    if (-not (Test-Path -LiteralPath $sentinel)) { throw 'Upgrade removed runtime data' }
    $profile = Join-Path $validation 'profile'
    RunHidden (Join-Path $installDir 'Neko-VRC.exe') @('--smoke-test', ('"' + $profile + '"'))
    if (-not (Test-Path -LiteralPath (Join-Path $profile 'result.txt'))) { throw 'Installed application smoke test did not pass' }
    Get-Content -LiteralPath (Join-Path $profile 'result.txt')
} finally {
    if ($installed) {
        RunHidden (Join-Path $installDir 'unins000.exe') @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/LOG="' + (Join-Path $validation 'uninstall.log') + '"'))
        if (Test-Path -LiteralPath $key) { throw 'Uninstall registration was not removed' }
        if (Test-Path -LiteralPath (Join-Path $installDir 'Neko-VRC.exe')) { throw 'Uninstall left the application binary' }
        if (Test-Path -LiteralPath $shortcut) { throw 'Uninstall left the Start menu entry' }
        if (-not (Test-Path -LiteralPath $sentinel)) { throw 'Uninstall removed runtime data' }
    }
    if ((UserDataHashes) -cne $before) { throw 'Installer validation altered user configuration' }
}
Write-Host "PASS: install, payload integrity, Start menu, in-place upgrade, installed UI, uninstall and user-data preservation. $validation"
