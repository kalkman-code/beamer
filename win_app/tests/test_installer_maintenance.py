"""Installer checks use mocked mutations and a harmless maintenance handoff stub."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


WIN_APP = Path(__file__).parents[1]
MAINTENANCE = WIN_APP / "Install-Maintenance.ps1"


class InstallerMaintenanceContractTests(unittest.TestCase):
    def test_both_installers_use_one_narrow_maintenance_entry_point(self):
        setup = (WIN_APP / "Beamer-Setup.iss").read_text(encoding="utf-8")
        install = (WIN_APP / "Install-Beamer.ps1").read_text(encoding="utf-8")
        self.assertTrue(MAINTENANCE.is_file(), "The installer-owned maintenance script is missing")
        for source in (setup, install):
            self.assertIn("Invoke-InstallMaintenance.ps1", source)
            self.assertIn("-ExePath", source)
        launcher = (WIN_APP / "Invoke-InstallMaintenance.ps1").read_text(encoding="utf-8")
        self.assertIn("-UserSid", launcher)
        self.assertIn("-Verb RunAs", launcher)
        self.assertIn("-File", launcher)
        self.assertNotIn("-Command", launcher)
        self.assertIn("PrivilegesRequired=lowest", setup)
        self.assertNotIn("Start-Process -FilePath $ExeDestination -WorkingDirectory $InstallDir -Verb RunAs", install)
        self.assertNotIn("New-NetFirewallRule", install)
        self.assertIn('$_.Path -eq $ExeDestination', install)
        self.assertNotIn("--uninstall-cleanup", setup)
        self.assertNotIn('[Run]', setup)
        self.assertIn('-Remove', setup)

    def test_elevation_cannot_launch_the_app_or_accept_an_arbitrary_command(self):
        self.assertTrue(MAINTENANCE.is_file(), "The installer-owned maintenance script is missing")
        source = MAINTENANCE.read_text(encoding="utf-8")
        self.assertIn("GameInputSvc", source)
        self.assertIn("Restart-Service", source)
        self.assertIn("LeastPrivilege", source)
        self.assertIn("InteractiveToken", source)
        self.assertIn("Register-ScheduledTask", source)
        self.assertNotIn("Start-Process", source)
        self.assertNotIn("Invoke-Expression", source)
        self.assertNotIn("EncodedCommand", source)
        self.assertNotIn("settings.json", source)


@unittest.skipUnless(os.name == "nt", "PowerShell behaviour is checked on Windows")
class MaintenanceBehaviourTests(unittest.TestCase):
    def run_probe(self, probe):
        self.assertTrue(MAINTENANCE.is_file(), "The installer-owned maintenance script is missing")
        path = str(MAINTENANCE).replace("'", "''")
        source = f"$ErrorActionPreference = 'Stop'\n. '{path}'\n" + probe
        with tempfile.TemporaryDirectory(prefix="beamer-maintenance-") as directory:
            script = Path(directory) / "probe.ps1"
            script.write_text(source, encoding="utf-8-sig")
            result = subprocess.run(
                [shutil.which("powershell") or "pwsh", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-File", str(script)],
                capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_explicit_administrator_preference_survives_upgrade(self):
        self.run_probe(r'''
$script:registered = @()
function Get-BeamerAdministratorPreference($UserSid) { return $true }
function Resolve-BeamerUserSid($Identity) { return $Identity }
function Get-ScheduledTask { param($TaskPath, $ErrorAction)
    return @([pscustomobject]@{ TaskName='Beamer-owner'; TaskPath='\' })
}
function Export-ScheduledTask { param($TaskName, $TaskPath, $ErrorAction)
    return '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Triggers><LogonTrigger><Enabled>false</Enabled></LogonTrigger></Triggers><Principals><Principal><UserId>S-1-5-21-123</UserId><LogonType>InteractiveToken</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals><Settings><Enabled>false</Enabled></Settings><Actions><Exec><Command>C:\Beamer\Beamer.exe</Command><Arguments>--hidden --keep-this</Arguments></Exec></Actions></Task>'
}
function Register-ScheduledTask { param($TaskName, $TaskPath, $Xml, [switch]$Force, $ErrorAction)
    $script:registered += $Xml
}
Update-BeamerAutostart -ExePath 'C:\Beamer\Beamer.exe' -UserSid 'S-1-5-21-123'
if ($script:registered.Count) { throw 'Upgrade rewrote an opted-in administrator task' }
''')

    def test_upgrade_preserves_omitted_default_runlevel_without_registration(self):
        self.run_probe(r'''
$script:registered = @()
function Get-BeamerAdministratorPreference($UserSid) { return $false }
function Resolve-BeamerUserSid($Identity) { return $Identity }
function Get-ScheduledTask { param($TaskPath, $ErrorAction)
    return @([pscustomobject]@{ TaskName='Beamer-owner'; TaskPath='\' })
}
function Export-ScheduledTask { param($TaskName, $TaskPath, $ErrorAction)
    return '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Principals><Principal><UserId>S-1-5-21-123</UserId><LogonType>InteractiveToken</LogonType></Principal></Principals><Actions><Exec><Command>C:\Beamer\Beamer.exe</Command></Exec></Actions></Task>'
}
function Register-ScheduledTask { param($TaskName, $TaskPath, $Xml, [switch]$Force, $ErrorAction)
    $script:registered += $Xml
}
Update-BeamerAutostart -ExePath 'C:\Beamer\Beamer.exe' -UserSid 'S-1-5-21-123'
if ($script:registered.Count) { throw 'Upgrade rewrote the existing default-privilege task' }
''')

    def test_upgrade_materialises_omitted_runlevel_only_for_administrator_preference(self):
        self.run_probe(r'''
$script:registered = @()
$script:original = '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Triggers><LogonTrigger><Enabled>false</Enabled></LogonTrigger></Triggers><Principals><Principal id="Author"><UserId>S-1-5-21-123</UserId><LogonType>InteractiveToken</LogonType></Principal></Principals><Settings><Enabled>false</Enabled></Settings><Actions Context="Author"><Exec><Command>C:\Beamer\Beamer.exe</Command><Arguments>--hidden --keep-this</Arguments></Exec></Actions></Task>'
function Get-BeamerAdministratorPreference($UserSid) { return $true }
function Resolve-BeamerUserSid($Identity) { return $Identity }
function Get-ScheduledTask { param($TaskPath, $ErrorAction)
    return @([pscustomobject]@{ TaskName='Beamer-owner'; TaskPath='\' })
}
function Export-ScheduledTask { param($TaskName, $TaskPath, $ErrorAction) return $script:original }
function Register-ScheduledTask { param($TaskName, $TaskPath, $Xml, [switch]$Force, $ErrorAction)
    $script:registered += $Xml
}
Update-BeamerAutostart -ExePath 'C:\Beamer\Beamer.exe' -UserSid 'S-1-5-21-123'
if ($script:registered.Count -ne 1) { throw 'Expected exactly one administrator task update' }
[xml]$actual = $script:registered[0]
$ns = New-Object Xml.XmlNamespaceManager($actual.NameTable)
$ns.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
$runLevel = $actual.SelectSingleNode('/t:Task/t:Principals/t:Principal/t:RunLevel', $ns)
if ($null -eq $runLevel -or $runLevel.InnerText -ne 'HighestAvailable') { throw 'Upgrade did not add the namespaced administrator run level' }
[void]$runLevel.ParentNode.RemoveChild($runLevel)
[xml]$wanted = $script:original
if ($actual.OuterXml -ne $wanted.OuterXml) { throw 'Upgrade changed existing task choices' }
''')

    def test_gameinput_recovery_registers_only_fixed_system_action_and_run_permissions(self):
        self.run_probe(r'''
$script:registrations = @()
$script:action = [pscustomobject]@{ Path=''; Arguments=''; WorkingDirectory='' }
$actions = [pscustomobject]@{ Count=0 }
$actions | Add-Member ScriptMethod Create {
    param($type)
    if ($type -ne 0) { throw 'Recovery must use one executable action' }
    $this.Count++
    return $script:action
}
$script:definition = [pscustomobject]@{
    RegistrationInfo=[pscustomobject]@{ Description='' }
    Principal=[pscustomobject]@{ UserId=''; LogonType=0; RunLevel=0 }
    Settings=[pscustomobject]@{ Enabled=$false; AllowDemandStart=$false; DisallowStartIfOnBatteries=$true; StopIfGoingOnBatteries=$true; ExecutionTimeLimit=''; MultipleInstances=0 }
    Triggers=[pscustomobject]@{ Count=0 }
    Actions=$actions
}
$script:folder = [pscustomobject]@{}
$script:folder | Add-Member ScriptMethod RegisterTaskDefinition {
    param($name, $definition, $flags, $userId, $password, $logonType, $sddl)
    $script:registrations += [pscustomobject]@{ Name=$name; Definition=$definition; Flags=$flags; UserId=$userId; Password=$password; LogonType=$logonType; Sddl=$sddl }
}
$script:scheduler = [pscustomobject]@{}
$script:scheduler | Add-Member ScriptMethod Connect {}
$script:scheduler | Add-Member ScriptMethod GetFolder {
    param($path)
    if ($path -ne '\') { throw 'Recovery task escaped the root folder' }
    return $script:folder
}
$script:scheduler | Add-Member ScriptMethod NewTask {
    param($flags)
    if ($flags -ne 0) { throw 'Unexpected task definition flags' }
    return $script:definition
}
function New-Object { param($ComObject)
    if ($ComObject -ne 'Schedule.Service') { throw 'Unexpected COM server' }
    return $script:scheduler
}
Update-BeamerGameInputRecovery
if ($script:registrations.Count -ne 1) { throw 'Expected exactly one task registration' }
$registration = $script:registrations[0]
if ($registration.Name -ne 'Beamer-GameInputRecovery') { throw 'Recovery task name is not fixed' }
if ($registration.UserId -ne 'S-1-5-18' -or $registration.Password -or $registration.LogonType -ne 5) { throw 'Recovery must run as SYSTEM without a password' }
if ($script:definition.Principal.UserId -ne 'S-1-5-18' -or $script:definition.Principal.LogonType -ne 5) { throw 'Task principal disagrees with registration' }
if (($registration.Flags -band 6) -ne 6) { throw 'Reinstall must replace the task definition' }
if ($script:definition.Triggers.Count -ne 0 -or -not $script:definition.Settings.AllowDemandStart -or -not $script:definition.Settings.Enabled) { throw 'Recovery must be enabled and on demand only' }
if ($script:definition.Settings.DisallowStartIfOnBatteries -or $script:definition.Settings.StopIfGoingOnBatteries) { throw 'Recovery must work on battery power' }
if ($script:definition.Settings.MultipleInstances -ne 2 -or $script:definition.Settings.ExecutionTimeLimit -ne 'PT30S') { throw 'Recovery must not queue or run indefinitely' }
if ($actions.Count -ne 1) { throw 'Expected exactly one action' }
$windows = [Environment]::GetFolderPath([Environment+SpecialFolder]::Windows)
if ($script:action.Path -cne (Join-Path $windows 'System32\WindowsPowerShell\v1.0\powershell.exe')) { throw 'Privileged action must use the absolute Windows PowerShell path' }
if ($script:action.Arguments -cne '-NoLogo -NoProfile -NonInteractive -Command "Restart-Service -Name ''GameInputSvc'' -ErrorAction Stop"') { throw 'Recovery action accepts something other than the constant service restart' }
if ($script:action.WorkingDirectory -cne (Join-Path $windows 'System32')) { throw 'Recovery uses a user-writable working directory' }
$sd = [Security.AccessControl.RawSecurityDescriptor]::new($registration.Sddl)
if ($sd.Owner.Value -ne 'S-1-5-18' -or $sd.Group.Value -ne 'S-1-5-18') { throw 'Interactive user can own or change the task' }
if (-not ($sd.ControlFlags -band [Security.AccessControl.ControlFlags]::DiscretionaryAclProtected)) { throw 'Task permissions can inherit write access' }
if ($sd.DiscretionaryAcl.Count -ne 3) { throw 'Unexpected task permission grants' }
$expected = @{ 'S-1-5-18'=268435456; 'S-1-5-32-544'=268435456; 'S-1-5-4'=-1610612736 }
foreach ($ace in $sd.DiscretionaryAcl) {
    $sid = $ace.SecurityIdentifier.Value
    if (-not $expected.ContainsKey($sid) -or $ace.AccessMask -ne $expected[$sid] -or $ace.AceQualifier -ne [Security.AccessControl.AceQualifier]::AccessAllowed) { throw 'Task permissions allow changing or deleting the privileged action' }
    $expected.Remove($sid)
}
if ($expected.Count) { throw 'Missing recovery task permissions' }
''')

    def test_gameinput_recovery_uninstall_removes_only_fixed_task_and_is_idempotent(self):
        self.run_probe(r'''
$script:removed = @()
$script:present = $true
function New-Object { throw 'Uninstall must not create a task definition' }
function Get-ScheduledTask { param($TaskName, $TaskPath, $ErrorAction)
    if ($TaskName -ne 'Beamer-GameInputRecovery' -or $TaskPath -ne '\') { throw 'Uninstall queried unrelated tasks' }
    if ($script:present) { return [pscustomobject]@{ TaskName=$TaskName; TaskPath=$TaskPath } }
}
function Unregister-ScheduledTask { param($TaskName, $TaskPath, $Confirm, $ErrorAction)
    $script:removed += [pscustomobject]@{ Name=$TaskName; Path=$TaskPath }
    $script:present = $false
}
Update-BeamerGameInputRecovery -Remove
Update-BeamerGameInputRecovery -Remove
if ($script:removed.Count -ne 1 -or $script:removed[0].Name -ne 'Beamer-GameInputRecovery' -or $script:removed[0].Path -ne '\') { throw 'Uninstall removed another task or failed when already absent' }
''')

    def test_migration_preserves_choices_and_ignores_other_users_and_actions(self):
        self.run_probe(r'''
$script:registered = @()
$script:fixtures = @{}
function Resolve-BeamerUserSid($Identity) { return $Identity }
function Get-ScheduledTask { param($TaskPath, $ErrorAction)
    return @($script:fixtures.Keys | ForEach-Object { [pscustomobject]@{ TaskName = $_; TaskPath = '\' } })
}
function Export-ScheduledTask { param($TaskName, $TaskPath, $ErrorAction) return $script:fixtures[$TaskName] }
function Register-ScheduledTask { param($TaskName, $TaskPath, $Xml, [switch]$Force, $ErrorAction)
    $script:registered += [pscustomobject]@{ Name = $TaskName; Xml = $Xml }
}
$template = '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Triggers><LogonTrigger><Enabled>false</Enabled><UserId>S-1-5-21-123</UserId></LogonTrigger></Triggers><Principals><Principal id="Author"><UserId>{0}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals><Settings><Enabled>false</Enabled><ExecutionTimeLimit>PT0S</ExecutionTimeLimit></Settings><Actions Context="Author"><Exec><Command>{1}</Command><Arguments>--hidden --keep-this</Arguments><WorkingDirectory>C:\A &amp; B</WorkingDirectory></Exec>{2}</Actions></Task>'
$script:fixtures['Beamer'] = $template -f 'S-1-5-21-123', 'C:\A &amp; B\Beamer.exe', ''
$script:fixtures['Beamer-other-user'] = $template -f 'S-1-5-21-999', 'C:\A &amp; B\Beamer.exe', ''
$script:fixtures['Beamer-other-exe'] = $template -f 'S-1-5-21-123', 'C:\Other\Beamer.exe', ''
$script:fixtures['Beamer-extra-action'] = $template -f 'S-1-5-21-123', 'C:\A &amp; B\Beamer.exe', '<Exec><Command>C:\Other.exe</Command></Exec>'
$script:fixtures['Unrelated'] = $script:fixtures['Beamer']
Update-BeamerAutostart -ExePath 'C:\A & B\Beamer.exe' -UserSid 'S-1-5-21-123'
if ($script:registered.Count -ne 1 -or $script:registered[0].Name -ne 'Beamer') { throw 'Changed an unrelated task or missed the matching task' }
$expected = $script:fixtures['Beamer'].Replace('HighestAvailable', 'LeastPrivilege')
[xml]$actual = $script:registered[0].Xml
[xml]$wanted = $expected
if ($actual.OuterXml -ne $wanted.OuterXml) { throw 'Migration changed existing user choices' }
$script:registered = @()
$script:fixtures = @{}
Update-BeamerAutostart -ExePath 'C:\A & B\Beamer.exe' -UserSid 'S-1-5-21-123'
if ($script:registered.Count) { throw 'Created startup when no task exists' }
''')

    def test_firewall_is_fixed_scope_and_service_recovery_is_optional(self):
        self.run_probe(r'''
$script:rules = @()
$script:restarted = @()
function Get-NetFirewallRule { param($DisplayName, $ErrorAction) return @() }
function Remove-NetFirewallRule { param([Parameter(ValueFromPipeline=$true)]$InputObject) }
function New-NetFirewallRule { param($DisplayName, $Direction, $Protocol, $LocalPort, $Action, $Profile, $Program, $ErrorAction)
    $script:rules += [pscustomobject]@{ Name=$DisplayName; Port=$LocalPort; Protocol=$Protocol; Profile=$Profile; Program=$Program }
}
function Get-Service { param($Name, $ErrorAction) return $null }
function Restart-Service { param($Name, [switch]$Force, $ErrorAction) $script:restarted += $Name }
Repair-BeamerFirewall -ExePath 'C:\A & B\Beamer.exe'
Repair-BeamerGameInput
if ($script:rules.Count -ne 3) { throw 'Expected exactly three firewall rules' }
if (@($script:rules | Where-Object { $_.Profile -ne 'Private' -or $_.Program -ne 'C:\A & B\Beamer.exe' }).Count) { throw 'Rule scope broadened' }
if (($script:rules | ForEach-Object { "$($_.Protocol):$($_.Port)" }) -join ',' -ne 'TCP:24820,UDP:24821,TCP:24821') { throw 'Unexpected ports or protocols' }
if ($script:restarted.Count) { throw 'Restarted absent GameInput service' }
function Get-Service { param($Name, $ErrorAction) return [pscustomobject]@{ Name='GameInputSvc' } }
Repair-BeamerGameInput
if ($script:restarted.Count -ne 1 -or $script:restarted[0] -ne 'GameInputSvc') { throw 'Did not recover only GameInputSvc' }
''')

    def test_uninstall_removes_only_matching_tasks_and_program_rules(self):
        self.run_probe(r'''
$script:removedTasks = @()
$script:removedRules = @()
function Resolve-BeamerUserSid($Identity) { return $Identity }
function Get-ScheduledTask { param($TaskPath, $ErrorAction) return [pscustomobject]@{ TaskName='Beamer'; TaskPath='\' } }
function Export-ScheduledTask { param($TaskName, $TaskPath, $ErrorAction)
    return '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Principals><Principal><UserId>S-1-5-21-123</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals><Actions><Exec><Command>C:\Beamer\Beamer.exe</Command></Exec></Actions></Task>'
}
function Unregister-ScheduledTask { param($TaskName, $TaskPath, $Confirm, $ErrorAction) $script:removedTasks += $TaskName }
function Get-NetFirewallRule { param($DisplayName, $ErrorAction)
    return @([pscustomobject]@{ Name='ours' }, [pscustomobject]@{ Name='other-install' })
}
function Get-NetFirewallApplicationFilter { param([Parameter(ValueFromPipeline=$true)]$InputObject)
    process { if ($InputObject.Name -eq 'ours') { [pscustomobject]@{ Program='C:\Beamer\Beamer.exe' } } else { [pscustomobject]@{ Program='C:\Other\Beamer.exe' } } }
}
function Remove-NetFirewallRule { param([Parameter(ValueFromPipeline=$true)]$InputObject) process { $script:removedRules += $InputObject.Name } }
Update-BeamerAutostart -ExePath 'C:\Beamer\Beamer.exe' -UserSid 'S-1-5-21-123' -Remove
Remove-BeamerFirewall -ExePath 'C:\Beamer\Beamer.exe'
if ($script:removedTasks.Count -ne 1 -or $script:removedTasks[0] -ne 'Beamer') { throw 'Matching task was not removed' }
if ($script:removedRules.Count -ne 3 -or @($script:removedRules | Where-Object { $_ -ne 'ours' }).Count) { throw 'Removed another install firewall rule' }
''')


@unittest.skipUnless(os.name == "nt", "Launcher handoff is checked on Windows")
class LauncherArgumentHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (os.environ.get("SSH_CLIENT") or os.environ.get("SSH_CONNECTION")):
            raise unittest.SkipTest("Real launcher handoff requires an SSH session")
        cls.powershell = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) /
                             "System32/WindowsPowerShell/v1.0/powershell.exe")
        probe = r'''
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
[pscustomobject]@{
    elevated = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    session = (Get-Process -Id $PID).SessionId
    sid = $identity.User.Value
} | ConvertTo-Json -Compress
'''
        result = subprocess.run(
            [cls.powershell, "-NoProfile", "-NonInteractive", "-Command", probe],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode:
            raise unittest.SkipTest("Could not establish a safe noninteractive launcher context")
        context = json.loads(result.stdout)
        if not context["elevated"] or context["session"] != 0:
            raise unittest.SkipTest("Real launcher handoff requires an elevated SSH process in session 0")
        cls.caller_sid = context["sid"]

    def prepare_launcher(self, directory):
        root = Path(directory)
        launcher = root / "Invoke-InstallMaintenance.ps1"
        shutil.copyfile(WIN_APP / launcher.name, launcher)
        (root / "Install-Maintenance.ps1").write_text(r'''
[CmdletBinding()]
param([string]$ExePath, [string]$UserSid, [switch]$Remove)
$ErrorActionPreference = 'Stop'
[pscustomobject]@{ ExePath=$ExePath; UserSid=$UserSid; Remove=[bool]$Remove } |
    ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'received.json') -Encoding UTF8
''', encoding="utf-8-sig")
        return launcher

    def run_file(self, script, *arguments):
        return subprocess.run(
            [self.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script), *arguments],
            capture_output=True, text=True, timeout=30,
        )

    def test_file_handoff_keeps_metacharacters_sid_and_remove_literal(self):
        with tempfile.TemporaryDirectory(prefix="ADMIN2 launcher ' & $(1+1) ; ", dir=WIN_APP.parent) as directory:
            launcher = self.prepare_launcher(directory)
            exe = str(Path(directory) / "Receiver ' & $(2+2) ; literal" / "Beamer.exe")
            for remove in (False, True):
                with self.subTest(remove=remove):
                    result = self.run_file(launcher, "-ExePath", exe, *(["-Remove"] if remove else []))
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    received = json.loads((Path(directory) / "received.json").read_text(encoding="utf-8-sig"))
                    self.assertEqual(received, {"ExePath": exe, "UserSid": self.caller_sid, "Remove": remove})

    def test_quote_bearing_exe_path_is_rejected_before_child_launch(self):
        with tempfile.TemporaryDirectory(prefix="ADMIN2 invalid argument ", dir=WIN_APP.parent) as directory:
            self.prepare_launcher(directory)
            driver = Path(directory) / "invoke-invalid.ps1"
            driver.write_text(r'''
$ErrorActionPreference = 'Stop'
$badPath = $PSScriptRoot + '\bad"argument\Beamer.exe'
& (Join-Path $PSScriptRoot 'Invoke-InstallMaintenance.ps1') -ExePath $badPath
exit $LASTEXITCODE
''', encoding="utf-8-sig")
            result = self.run_file(driver)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Invalid installer path", " ".join((result.stdout + result.stderr).split()))
            self.assertFalse((Path(directory) / "received.json").exists(), "The maintenance child ran before path validation")


if __name__ == "__main__":
    unittest.main()
