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
; The installer itself still runs per-user; Beamer.exe carries its own requireAdministrator
; manifest (see build.spec) so SendInput can reach elevated windows. Explorer honours that
; manifest automatically (UAC shield on the shortcuts), no change needed here for that part.
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\Beamer.exe
; The single-instance mutex Beamer.exe holds. A running Beamer is elevated, so this per-user setup
; can neither close it nor replace its locked exe; setup asks for it to be quit from the tray first.
AppMutex=Beamer.Receiver
SetupIconFile=Beamer.ico
; Beamer.exe is x64. This refuses 32-bit Windows with the installer's own message; Windows 11 on
; ARM is allowed, as it runs x64 apps under emulation.
ArchitecturesAllowed=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Autostart is asked for, never assumed, so it starts unticked.
Name: "autostart"; Description: "Automatically start Beamer at Windows login"; GroupDescription: "Startup options:"; Flags: unchecked

[Files]
Source: "dist\Beamer.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; The same paths Install-Beamer.ps1 writes, so either installer replaces the other's shortcuts
; instead of adding a second Beamer to the Start Menu.
Name: "{autoprograms}\Beamer"; Filename: "{app}\Beamer.exe"

[Run]
; CreateProcess refuses a requireAdministrator exe from this non-elevated setup (error 740);
; shellexec lets Windows raise the UAC prompt instead.
Filename: "{app}\Beamer.exe"; Description: "{cm:LaunchProgram,Beamer}"; Flags: nowait postinstall skipifsilent shellexec
; A plain Startup-folder shortcut would UAC-prompt on every logon, so autostart is a Scheduled Task
; with RunLevel Highest. Creating one needs elevation this setup does not have, so the step asks
; for it; --hidden brings Beamer up in the tray rather than with a window.
Filename: "schtasks.exe"; Parameters: "/Create /RL HIGHEST /SC ONLOGON /TN Beamer /TR ""\""{app}\Beamer.exe\"" --hidden"" /F"; Tasks: autostart; Flags: runhidden shellexec waituntilterminated; Verb: runas

[UninstallRun]
; The rules and the task were written elevated (by the app, or by the autostart step above), so
; removing them needs elevation the per-user uninstaller does not have: one prompt, one command,
; rather than three deletions that fail silently and leave them behind.
Filename: "cmd.exe"; Parameters: "/c netsh advfirewall firewall delete rule name=""Beamer Receiver (TCP-In)"" & netsh advfirewall firewall delete rule name=""Beamer Pairing (UDP-In)"" & netsh advfirewall firewall delete rule name=""Beamer Pairing (TCP-In)"" & schtasks /Delete /TN Beamer /F"; Flags: runhidden shellexec waituntilterminated; Verb: runas; RunOnceId: "BeamerCleanup"
