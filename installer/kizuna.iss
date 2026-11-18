; Inno Setup script for Kizuna-Setup.exe. Built by .github/workflows/release.yml after PyInstaller made dist\Kizuna:
;     iscc installer\kizuna.iss /DMyAppVersion=1.0.0
; Per-user install (no admin rights). Optional: install FFmpeg, MKVToolNix, Jellyfin and Stremio with winget,
; each in its own visible window. At the end Kizuna starts and opens its setup wizard.
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{6B4F2C1E-9C1A-4A7E-8E57-4B1D0B7A2F10}
AppName=Kizuna
AppVersion={#MyAppVersion}
AppPublisher=Kizuna
AppPublisherURL=https://github.com/mw4z/Kizuna
DefaultDirName={localappdata}\Programs\Kizuna
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Kizuna-Setup
SetupIconFile=..\app.ico
UninstallDisplayIcon={app}\Kizuna.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "autostart"; Description: "Start Kizuna when I sign in to Windows (recommended)"; GroupDescription: "Shortcuts:"
Name: "ffmpeg"; Description: "FFmpeg (required: reads and checks videos)"; GroupDescription: "Install with winget (each opens its own window):"
Name: "mkvtoolnix"; Description: "MKVToolNix (required: adds the subtitle track)"; GroupDescription: "Install with winget (each opens its own window):"
Name: "jellyfin"; Description: "Jellyfin Server (your media library; skip if you have it)"; GroupDescription: "Install with winget (each opens its own window):"
Name: "stremio"; Description: "Stremio (optional: manage your add-ons)"; GroupDescription: "Install with winget (each opens its own window):"; Flags: unchecked

[Files]
Source: "..\dist\Kizuna\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\Kizuna"; Filename: "{app}\Kizuna.exe"; Parameters: "--open"; IconFilename: "{app}\Kizuna.exe"
Name: "{userdesktop}\Kizuna"; Filename: "{app}\Kizuna.exe"; Parameters: "--open"; IconFilename: "{app}\Kizuna.exe"; Tasks: desktopicon
Name: "{userstartup}\Kizuna (background)"; Filename: "{app}\Kizuna.exe"; Tasks: autostart

[Run]
Filename: "cmd.exe"; Parameters: "/c winget install --id Gyan.FFmpeg -e --source winget"; StatusMsg: "Installing FFmpeg..."; Flags: waituntilterminated; Tasks: ffmpeg
Filename: "cmd.exe"; Parameters: "/c winget install --id MoritzBunkus.MKVToolNix -e --source winget"; StatusMsg: "Installing MKVToolNix..."; Flags: waituntilterminated; Tasks: mkvtoolnix
Filename: "cmd.exe"; Parameters: "/c winget install --id Jellyfin.Server -e --source winget"; StatusMsg: "Installing Jellyfin..."; Flags: waituntilterminated; Tasks: jellyfin
Filename: "cmd.exe"; Parameters: "/c winget install --id Stremio.Stremio -e --source winget"; StatusMsg: "Installing Stremio..."; Flags: waituntilterminated; Tasks: stremio
Filename: "{app}\Kizuna.exe"; Description: "Start Kizuna"; Flags: nowait
Filename: "{app}\Kizuna.exe"; Parameters: "--open"; Description: "Open Kizuna and its setup wizard"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "taskkill.exe"; Parameters: "/IM Kizuna.exe /F"; Flags: runhidden; RunOnceId: "StopKizuna"
