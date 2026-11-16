; Inno Setup Script for Beamer Windows Receiver
; Built by build_win_app.ps1 -Release. The version is read from the repo-root VERSION file, the one
; place it is set for both apps.

#define VersionFile FileOpen(AddBackslash(SourcePath) + "..\VERSION")
#define AppVersion Trim(FileRead(VersionFile))
#expr FileClose(VersionFile)
; A beta such as 1.5.0-beta.1 names itself in the shown version, but the file version resource
; takes four integers, so it carries the release number.
#define NumericVersion Copy(AppVersion, 1, Pos("-", AppVersion + "-") - 1)

[Setup]
AppId={{9A4E2B1F-4309-4E65-B86D-C7B9F4F64D01}
AppName=Beamer
AppVersion={#AppVersion}
VersionInfoVersion={#NumericVersion}.0
VersionInfoProductTextVersion={#AppVersion}
AppPublisher=Kalkman Code
DefaultDirName={localappdata}\Beamer
DisableProgramGroupPage=yes
OutputBaseFilename=Beamer-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
; File installation and every Beamer operation stay with the original user. Only the fixed
; maintenance script requests administrator permission.
#ifdef SecureDesktop
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
#else
PrivilegesRequired=lowest
#endif
UninstallDisplayIcon={app}\Beamer.exe
; An older elevated receiver must still be quit before its executable can be replaced.
AppMutex=Beamer.Receiver
SetupIconFile=Beamer.ico
; Beamer.exe is x64. This refuses 32-bit Windows with the installer's own message; Windows 11 on
; ARM is allowed, as it runs x64 apps under emulation.
ArchitecturesAllowed=x64compatible
; When build_win_app.ps1 passes /DSignInstaller and /Sbeamer=..., ISCC signs the setup engine
; extracted to %TEMP% during install and the uninstaller (SignedUninstaller). Without the define,
; local builds without a certificate still compile an unsigned installer.
#ifdef SignInstaller
SignTool=beamer
SignedUninstaller=yes
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
#ifdef SecureDesktop
Name: "securedesktop"; Description: "Install experimental secure desktop support (administrator peer approval required)"; GroupDescription: "Windows sign-in and UAC:"; Flags: unchecked
#endif
; Autostart is asked for, never assumed, so it starts unticked.
Name: "autostart"; Description: "Start Beamer at sign-in"; GroupDescription: "Startup options:"; Flags: unchecked

[Files]
Source: "Invoke-InstallMaintenance.ps1"; Flags: dontcopy
Source: "dist\Beamer.exe"; DestDir: "{app}"; Flags: ignoreversion
#ifdef SecureDesktop
Source: "Manage-SecureDesktop.ps1"; DestName: "SecureDesktopSetup.ps1"; Flags: dontcopy
Source: "dist\BeamerSecure\*"; DestDir: "{commonpf64}\Beamer Secure Desktop"; Flags: ignoreversion recursesubdirs createallsubdirs; Tasks: securedesktop
Source: "Manage-SecureDesktop.ps1"; DestDir: "{commonpf64}\Beamer Secure Desktop"; Flags: ignoreversion; Tasks: securedesktop
#endif
Source: "Install-Maintenance.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "Invoke-InstallMaintenance.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; The same paths Install-Beamer.ps1 writes, so either installer replaces the other's shortcuts
; instead of adding a second Beamer to the Start Menu.
Name: "{autoprograms}\Beamer"; Filename: "{app}\Beamer.exe"

[InstallDelete]
; Remove pre-task autostart entries so there is only one starter.
Type: files; Name: "{userstartup}\Beamer.lnk"

[Registry]
; The same legacy Run entry has the same UAC behaviour as the old Startup shortcut.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "Beamer"; Flags: deletevalue

[UninstallRun]
; The same fixed maintenance entry removes only this user's tasks and this executable's rules.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""{app}\Invoke-InstallMaintenance.ps1"" -ExePath ""{app}\Beamer.exe"" -Remove"; Flags: runhidden waituntilterminated; RunOnceId: "BeamerCleanup"

[Code]
#ifdef SecureDesktop
var SecureSetupScript: String;

function GetFileAttributesW(const FileName: String): LongWord;
  external 'GetFileAttributesW@kernel32.dll stdcall';

function SecurePathIsPlain(Path: String): Boolean;
var Attributes: LongWord;
begin
  Result := True;
  while Length(Path) > 3 do begin
    Attributes := GetFileAttributesW(Path);
    if (Attributes <> $FFFFFFFF) and ((Attributes and $400) <> 0) then begin
      Result := False;
      Exit;
    end;
    Path := ExtractFileDir(Path);
  end;
end;

function RunSecureAction(Action: String): Boolean;
var ExitCode: Integer; Script: String;
begin
  Script := SecureSetupScript;
  if Script = '' then
    Script := ExpandConstant('{commonpf64}\Beamer Secure Desktop\Manage-SecureDesktop.ps1');
  Result := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + Script + '" -Action ' + Action,
    '', SW_HIDE, ewWaitUntilTerminated, ExitCode) and (ExitCode = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not SecurePathIsPlain(ExpandConstant('{commonpf64}\Beamer Secure Desktop')) or
     not SecurePathIsPlain(ExpandConstant('{commonappdata}\BeamerSecureDesktop')) then begin
    Result := 'Secure desktop refuses a redirected installation or state directory.';
    Exit;
  end;
  if WizardIsTaskSelected('securedesktop') or FileExists(ExpandConstant('{commonpf64}\Beamer Secure Desktop\Manage-SecureDesktop.ps1')) then begin
    ExtractTemporaryFile('SecureDesktopSetup.ps1');
    SecureSetupScript := ExpandConstant('{tmp}\SecureDesktopSetup.ps1');
    if not RunSecureAction('Prepare') then
      Result := 'Secure desktop could not be prepared safely. No service files have been replaced.';
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usUninstall) and FileExists(ExpandConstant('{commonpf64}\Beamer Secure Desktop\Manage-SecureDesktop.ps1')) then begin
    if not SecurePathIsPlain(ExpandConstant('{commonpf64}\Beamer Secure Desktop')) or
       not RunSecureAction('Remove') then
      RaiseException('Secure desktop could not be removed. Its installed files have been retained.');
  end;
end;
#endif

function OperationFailure(const Action: String; const Started: Boolean; const ResultCode: Integer): String;
begin
  if (not Started) and (ResultCode = 1223) then
    Result := 'You cancelled the permission request.'
  else if not Started then
    Result := 'Windows could not start the installer operation: ' + SysErrorMessage(ResultCode)
  else
    Result := 'Windows returned exit code ' + IntToStr(ResultCode) + '.';
  Result := Action + ' ' + Result;
end;

function PowerShellPath: String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

function InitializeSetup: Boolean;
var
  ResultCode: Integer;
  Started: Boolean;
begin
  ExtractTemporaryFile('Invoke-InstallMaintenance.ps1');
  Started := Exec(PowerShellPath, '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    ExpandConstant('{tmp}\Invoke-InstallMaintenance.ps1') + '" -CheckIdentityOnly', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := Started and (ResultCode = 0);
  if not Result then
    MsgBox('Beamer must be installed by the signed-in user. Run the installer normally; give administrator permission only when maintenance asks.', mbError, MB_OK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  Started: Boolean;
  Failure: String;
begin
#ifdef SecureDesktop
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('securedesktop') then
    if not RunSecureAction('Install') then
      RaiseException('Secure desktop installation failed; leave the service disabled.');
#endif
  if CurStep = ssPostInstall then
  begin
    Started := Exec(PowerShellPath, '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
      ExpandConstant('{app}\Invoke-InstallMaintenance.ps1') + '" -ExePath "' +
      ExpandConstant('{app}\Beamer.exe') + '"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    if (not Started) or (ResultCode <> 0) then
      RaiseException(OperationFailure('Beamer was installed, but administrator maintenance did not complete.', Started, ResultCode));
    if not WizardIsTaskSelected('autostart') then Exit;
    Started := Exec(ExpandConstant('{app}\Beamer.exe'), '--enable-autostart', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    if (not Started) or (ResultCode <> 0) then
    begin
      Failure := OperationFailure('Could not run Beamer sign-in setup.', Started, ResultCode);
      Log(Failure);
      RaiseException('Beamer was installed, but start at sign-in was not configured. ' + Failure);
    end;
  end;
end;
