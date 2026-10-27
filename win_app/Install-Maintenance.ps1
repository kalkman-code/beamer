[CmdletBinding()]
param(
    [string]$ExePath,
    [string]$UserSid,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

function Resolve-BeamerUserSid([string]$Identity) {
    if ($Identity -match '^S-1-') {
        return ([Security.Principal.SecurityIdentifier]$Identity).Value
    }
    return ([Security.Principal.NTAccount]$Identity).Translate([Security.Principal.SecurityIdentifier]).Value
}

function Get-BeamerAdministratorPreference([string]$UserSid) {
    $key = [Microsoft.Win32.Registry]::Users.OpenSubKey("$UserSid\Software\KalkmanCode\Beamer")
    if ($null -eq $key) { return $false }
    try {
        if ($key.GetValueKind('RunAsAdministrator') -ne [Microsoft.Win32.RegistryValueKind]::DWord) { return $false }
        return $key.GetValue('RunAsAdministrator', 0) -eq 1
    } catch [System.IO.IOException] {
        return $false
    } finally {
        $key.Dispose()
    }
}

function Update-BeamerAutostart([string]$ExePath, [string]$UserSid, [switch]$Remove) {
    $expectedPath = [IO.Path]::GetFullPath($ExePath)
    $runLevel = if (-not $Remove -and (Get-BeamerAdministratorPreference -UserSid $UserSid)) { 'HighestAvailable' } else { 'LeastPrivilege' }
    foreach ($task in @(Get-ScheduledTask -TaskPath '\' -ErrorAction Stop)) {
        if ($task.TaskName -ne 'Beamer' -and $task.TaskName -notlike 'Beamer-*') { continue }
        [xml]$xml = Export-ScheduledTask -TaskName $task.TaskName -TaskPath '\' -ErrorAction Stop
        $ns = New-Object Xml.XmlNamespaceManager($xml.NameTable)
        $ns.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
        $principals = $xml.SelectNodes('/t:Task/t:Principals/t:Principal', $ns)
        $actions = $xml.SelectNodes('/t:Task/t:Actions/*', $ns)
        if ($principals.Count -ne 1 -or $actions.Count -ne 1 -or $actions[0].LocalName -ne 'Exec') { continue }
        $principal = $principals[0]
        if ($principal.LogonType -ne 'InteractiveToken') { continue }
        try {
            if ((Resolve-BeamerUserSid $principal.UserId) -ne $UserSid) { continue }
            $command = [string]$actions[0].Command
            if (-not [IO.Path]::IsPathRooted($command)) { continue }
            if (-not [string]::Equals([IO.Path]::GetFullPath($command), $expectedPath, [StringComparison]::OrdinalIgnoreCase)) { continue }
        } catch { continue }
        if ($Remove) {
            Unregister-ScheduledTask -TaskName $task.TaskName -TaskPath '\' -Confirm:$false -ErrorAction Stop
            continue
        }
        $runLevelNode = $principal.SelectSingleNode('t:RunLevel', $ns)
        $currentRunLevel = if ($null -eq $runLevelNode) { 'LeastPrivilege' } else { $runLevelNode.InnerText }
        if ($currentRunLevel -eq $runLevel) { continue }
        # Keep the existing task definition: disabled startup, arguments and logon triggers are user choices.
        if ($null -eq $runLevelNode) {
            $runLevelNode = $xml.CreateElement('RunLevel', $principal.NamespaceURI)
            [void]$principal.AppendChild($runLevelNode)
        }
        $runLevelNode.InnerText = $runLevel
        Register-ScheduledTask -TaskName $task.TaskName -TaskPath '\' -Xml $xml.OuterXml -Force -ErrorAction Stop | Out-Null
    }
}

function Remove-BeamerFirewall([string]$ExePath) {
    foreach ($name in @('Beamer Receiver (TCP-In)', 'Beamer Pairing (UDP-In)', 'Beamer Pairing (TCP-In)')) {
        foreach ($rule in @(Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)) {
            $filter = $rule | Get-NetFirewallApplicationFilter -ErrorAction Stop
            if ([string]::Equals($filter.Program, $ExePath, [StringComparison]::OrdinalIgnoreCase)) {
                $rule | Remove-NetFirewallRule -ErrorAction Stop
            }
        }
    }
}

function Repair-BeamerFirewall([string]$ExePath) {
    Remove-BeamerFirewall -ExePath $ExePath
    $rules = @(
        @{ Name = 'Beamer Receiver (TCP-In)'; Protocol = 'TCP'; Port = 24820 },
        @{ Name = 'Beamer Pairing (UDP-In)'; Protocol = 'UDP'; Port = 24821 },
        @{ Name = 'Beamer Pairing (TCP-In)'; Protocol = 'TCP'; Port = 24821 }
    )
    foreach ($rule in $rules) {
        New-NetFirewallRule -DisplayName $rule.Name -Direction Inbound -Protocol $rule.Protocol -LocalPort $rule.Port -Action Allow -Profile Private -Program $ExePath -ErrorAction Stop | Out-Null
    }
}

function Repair-BeamerGameInput {
    if (Get-Service -Name 'GameInputSvc' -ErrorAction SilentlyContinue) {
        try {
            Restart-Service -Name 'GameInputSvc' -ErrorAction Stop
        } catch {
            # The receiver can still run; its remote controller reports when a local click is needed.
            Write-Warning 'GameInput could not be restarted. A local click may be needed to release remote controller focus.'
        }
    }
}

function Update-BeamerGameInputRecovery([switch]$Remove) {
    $taskName = 'Beamer-GameInputRecovery'
    if ($Remove) {
        if (Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $taskName -TaskPath '\' -Confirm:$false -ErrorAction Stop
        }
        return
    }

    $scheduler = New-Object -ComObject 'Schedule.Service'
    $scheduler.Connect()
    $folder = $scheduler.GetFolder('\')
    $definition = $scheduler.NewTask(0)
    $definition.RegistrationInfo.Description = 'Restarts GameInput when its service window blocks remote pointer control.'
    $definition.Principal.UserId = 'S-1-5-18'
    $definition.Principal.LogonType = 5
    $definition.Principal.RunLevel = 1
    $definition.Settings.Enabled = $true
    $definition.Settings.AllowDemandStart = $true
    $definition.Settings.DisallowStartIfOnBatteries = $false
    $definition.Settings.StopIfGoingOnBatteries = $false
    $definition.Settings.ExecutionTimeLimit = 'PT30S'
    $definition.Settings.MultipleInstances = 2
    $windows = [Environment]::GetFolderPath([Environment+SpecialFolder]::Windows)
    $action = $definition.Actions.Create(0)
    $action.Path = Join-Path $windows 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $action.Arguments = '-NoLogo -NoProfile -NonInteractive -Command "Restart-Service -Name ''GameInputSvc'' -ErrorAction Stop"'
    $action.WorkingDirectory = Join-Path $windows 'System32'
    # Set the protected ACL at registration: no interval with an elevated, user-writable task.
    $sddl = 'O:SYG:SYD:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGX;;;IU)'
    $folder.RegisterTaskDefinition($taskName, $definition, (0x6 -bor 0x10), 'S-1-5-18', $null, 5, $sddl) | Out-Null
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            throw 'Installer maintenance requires administrator permission.'
        }
        if (-not [IO.Path]::IsPathRooted($ExePath) -or [IO.Path]::GetFileName($ExePath) -ine 'Beamer.exe' -or -not (Test-Path -LiteralPath $ExePath -PathType Leaf)) {
            throw 'Installer maintenance requires the installed Beamer.exe path.'
        }
        $ExePath = [IO.Path]::GetFullPath($ExePath)
        $UserSid = ([Security.Principal.SecurityIdentifier]$UserSid).Value
        Update-BeamerAutostart -ExePath $ExePath -UserSid $UserSid -Remove:$Remove
        if ($Remove) {
            Update-BeamerGameInputRecovery -Remove
            Remove-BeamerFirewall -ExePath $ExePath
        } else {
            Repair-BeamerFirewall -ExePath $ExePath
            Update-BeamerGameInputRecovery
            Repair-BeamerGameInput
        }
        exit 0
    } catch {
        Write-Error $_ -ErrorAction Continue
        exit 1
    }
}
