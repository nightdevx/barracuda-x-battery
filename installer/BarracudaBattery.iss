; Inno Setup script. Build with build.ps1, which first makes the one-folder app in build\app.
; Per-user install: no administrator rights, files under %LOCALAPPDATA%\Programs.

#define AppName "Barracuda X Pil"
#define AppVersion "1.2.0"
#define AppExe "BarracudaBattery.exe"
#define RunKey "Software\Microsoft\Windows\CurrentVersion\Run"

[Setup]
AppId={{71F93EA0-BCDB-43C9-98CA-E391A1EE41C5}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=nightdijital
AppPublisherURL=https://nightdijital.com
AppSupportURL=https://nightdijital.com
AppContact=iletisim@nightdijital.com
DefaultDirName={localappdata}\Programs\BarracudaBattery
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=BarracudaBattery-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; The running app is stopped in PrepareToInstall; its tray process has no window to close.
CloseApplications=no

[Languages]
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"

[Tasks]
Name: "startup"; Description: "Windows açılınca arka planda başlat (pil kaydı kesintisiz sürer)"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\build\app\BarracudaBattery\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Files of an earlier version that this one no longer ships.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; Same value the app's "Windows ile başlat" switch writes.
Root: HKCU; Subkey: "{#RunKey}"; ValueType: string; ValueName: "BarracudaBattery"; ValueData: """{app}\{#AppExe}"" ""--tray"""; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM {#AppExe}"; Flags: runhidden; RunOnceId: "StopApp"

[Code]
procedure StopApp();
var
  Code: Integer;
begin
  { The tray process and an open window; an older copy run from elsewhere has the same name and stops too. }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(500);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopApp();
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  { The app's own switch may have written the sign-in entry even when the installer task was off. }
  if CurUninstallStep = usPostUninstall then
    RegDeleteValue(HKEY_CURRENT_USER, '{#RunKey}', 'BarracudaBattery');
end;
