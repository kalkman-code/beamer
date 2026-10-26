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
#ifdef CommunityPreview
AppPublisher=Beamer community preview (unofficial)
#else
AppPublisher=Kalkman Code
#endif
DefaultDirName={localappdata}\Beamer
DisableProgramGroupPage=yes
#ifdef CommunityPreview
OutputBaseFilename=Beamer-RU-Preview-Setup-{#AppVersion}
#else
OutputBaseFilename=Beamer-Setup-{#AppVersion}
#endif
Compression=lzma2/max
SolidCompression=yes
; File installation and every Beamer operation stay with the original user. Only the fixed
; maintenance script requests administrator permission.
PrivilegesRequired=lowest
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
; Autostart is asked for, never assumed, so it starts unticked.
Name: "autostart"; Description: "Start Beamer at sign-in"; GroupDescription: "Startup options:"; Flags: unchecked

[Files]
Source: "Invoke-InstallMaintenance.ps1"; Flags: dontcopy
Source: "dist\Beamer.exe"; DestDir: "{app}"; Flags: ignoreversion
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
