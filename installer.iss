; PAPro installer (Inno Setup 6: https://jrsoftware.org/isinfo.php). Built by "Build EXE.bat" -> dist\PAPro-Setup-<version>.exe
#ifndef MyVersion
  #define MyVersion "1.0.2"
#endif

[Setup]
AppId={{7A3E91C4-5D2B-4F68-8C17-A9B10000000A}
AppName=PAPro
AppVersion={#MyVersion}
AppPublisher=sjames1066
AppPublisherURL=https://github.com/sjames1066/papro
AppSupportURL=https://github.com/sjames1066/papro
; per-user install: no administrator needed, and PAPro can write its config.json next to its program
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\PAPro
DefaultGroupName=PAPro
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\papro.exe
SetupIconFile=papro.ico
OutputDir=dist
OutputBaseFilename=PAPro-Setup-{#MyVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startwin"; Description: "Start PAPro quietly in the tray when I sign in to Windows"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "dist\papro\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\PAPro"; Filename: "{app}\papro.exe"
Name: "{group}\Uninstall PAPro"; Filename: "{uninstallexe}"
Name: "{autodesktop}\PAPro"; Filename: "{app}\papro.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "PAPro"; \
  ValueData: """{app}\papro.exe"" --background"; Flags: uninsdeletevalue; Tasks: startwin

[Run]
; no skipifsilent: after an automatic update PAPro starts again by itself
Filename: "{app}\papro.exe"; Description: "Start PAPro now"; Flags: nowait postinstall

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM papro.exe"; Flags: runhidden; RunOnceId: "StopPAPro"

[Code]
// A running PAPro (tray) would keep its files locked: stop it before installing or updating.
// PAPro was called FCAC before: also stop it and remove the old FCAC install (your settings, recordings and packs are kept
// and carried over by PAPro the first time it runs).
function InitializeSetup(): Boolean;
var
  code: Integer;
  uninst: String;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM papro.exe', '', SW_HIDE, ewWaitUntilTerminated, code);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM FCAC.exe', '', SW_HIDE, ewWaitUntilTerminated, code);
  if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{6F1D2B7A-3C5E-4B8A-9E41-FCAC00000001}_is1', 'UninstallString', uninst) then
  begin
    uninst := RemoveQuotes(uninst);
    Exec(uninst, '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', '', SW_HIDE, ewWaitUntilTerminated, code);
  end;
  Result := True;
end;

// Your recordings, packs and settings live in %LOCALAPPDATA%\PAPro and next to the program: they are left alone
// when you uninstall. Delete those folders yourself for a completely clean removal.
