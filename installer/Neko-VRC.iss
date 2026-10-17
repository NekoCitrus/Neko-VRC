; Compile with build-installer.ps1; all payloads are local and self-contained.
#ifndef ProductDir
  #error ProductDir is required
#endif
#ifndef ProductVersion
  #error ProductVersion is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif

[Setup]
AppId={{B6104E75-6F7D-4B4E-9D05-AD41251A78EF}
AppName=Neko-VRC
AppVersion={#ProductVersion}
AppPublisher=NekoCitrus
AppPublisherURL=https://github.com/NekoCitrus/Neko-VRC
AppSupportURL=https://github.com/NekoCitrus/Neko-VRC/issues
AppUpdatesURL=https://github.com/NekoCitrus/Neko-VRC/releases
DefaultDirName={localappdata}\Programs\Neko-VRC
DefaultGroupName=Neko-VRC
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19041
DisableDirPage=no
AllowNoIcons=yes
UsePreviousAppDir=yes
AppMutex=Local\NekoVRC.SingleInstance
CloseApplications=no
RestartApplications=no
SetupMutex=NekoVRC.Setup
WizardStyle=modern
SetupIconFile=..\assets\shocking_vrchat.ico
UninstallDisplayIcon={app}\Neko-VRC.exe
LicenseFile=..\LICENSE
OutputDir={#OutputDir}
OutputBaseFilename=Neko-VRC-Setup-v{#ProductVersion}-win-x64
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
SetupLogging=yes
VersionInfoVersion={#ProductVersion}

[Languages]
Name: "chinesesimplified"; MessagesFile: "Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#ProductDir}\*"; DestDir: "{app}"; Excludes: "log\*,config\*,*.pdb"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Neko-VRC"; Filename: "{app}\Neko-VRC.exe"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,Neko-VRC}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Neko-VRC"; Filename: "{app}\Neko-VRC.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\Neko-VRC.exe"; Description: "{cm:LaunchProgram,Neko-VRC}"; Flags: nowait postinstall skipifsilent

; Deliberately no InstallDelete/UninstallDelete, auto-start, firewall edits,
; device access, or config-directory entries. User data is never removed.
