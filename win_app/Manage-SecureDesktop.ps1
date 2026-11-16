[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][ValidateSet('Prepare', 'Install', 'Grant', 'Stop', 'Disable', 'Remove', 'Status')][string]$Action,
    [string]$SettingsPath,
    [string[]]$PeerIds
)

$ErrorActionPreference = 'Stop'
$ServiceName = 'BeamerSecureDesktop'
$RuleName = 'Beamer Secure Desktop (TCP-In)'
$InstallDir = Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'Beamer Secure Desktop'
$StateDir = Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'BeamerSecureDesktop'
$Executable = Join-Path $InstallDir 'BeamerSecure.exe'
$GrantFile = Join-Path $StateDir 'grants.dat'
$ApprovedKey = 'HKLM:\SOFTWARE\Beamer\SecureDesktop'

function Assert-PlainPath([string]$Path) {
    $part = [IO.Path]::GetFullPath($Path)
    while ($part) {
        if (Test-Path -LiteralPath $part) {
            if ((Get-Item -Force -LiteralPath $part).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Secure desktop refuses reparse points in managed paths'
            }
        }
        $part = [IO.Path]::GetDirectoryName($part)
    }
}

function Set-ProtectedDirectory([string]$Path) {
    Assert-PlainPath $Path
    $admin = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    if (Test-Path -LiteralPath $Path) {
        $owner = (Get-Acl -LiteralPath $Path).GetOwner([Security.Principal.SecurityIdentifier]).Value
        if ($owner -notin @($admin.Value, $system.Value)) { throw 'Secure state was pre-created by another account' }
    } else {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetOwner($admin)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @($admin, $system)) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Assert-ProtectedTree([string]$Path, [switch]$Confidential) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $items = @((Get-Item -Force -LiteralPath $Path)) + @(Get-ChildItem -Force -LiteralPath $Path -Recurse)
    foreach ($item in $items) {
        Assert-PlainPath $item.FullName
        $acl = Get-Acl -LiteralPath $item.FullName
        if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin @('S-1-5-18', 'S-1-5-32-544')) {
            throw 'Secure desktop refuses a user-owned installation or state file'
        }
        foreach ($rule in $acl.Access) {
            $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
            if ($rule.AccessControlType -eq 'Allow' -and $sid -notin @('S-1-5-18', 'S-1-5-32-544')) {
                if ($Confidential -and [int64]$rule.FileSystemRights -ne 0) {
                    throw 'Secure desktop refuses state readable or writable by another account'
                }
                if ([int64]$rule.FileSystemRights -band 0x500D0156) {
                    throw 'Secure desktop refuses an installation or state file writable by another account'
                }
            }
        }
    }
}

function Stop-OwnedService {
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($service) {
        Stop-Service -Name $ServiceName -ErrorAction Stop
        $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(15))
        Set-Service -Name $ServiceName -StartupType Disabled
    }
}

function Invoke-Sc([string[]]$Arguments) {
    & "$env:SystemRoot\System32\sc.exe" @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Secure desktop service configuration failed' }
}

if ($Action -eq 'Status') {
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    [pscustomobject]@{Installed = [bool]$service; State = if ($service) { [string]$service.Status } else { 'Absent' };
        GrantsPresent = Test-Path -LiteralPath $GrantFile}
    exit 0
}

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator approval is required' }
Assert-PlainPath $InstallDir
Assert-PlainPath $StateDir
if (-not $PSCmdlet.ShouldProcess($ServiceName, $Action)) { exit 0 }

switch ($Action) {
    'Prepare' {
        Assert-ProtectedTree $InstallDir
        Assert-ProtectedTree $StateDir -Confidential
        Stop-OwnedService
        Set-ProtectedDirectory $InstallDir
        Set-ProtectedDirectory $StateDir
        Write-Output 'Protected secure desktop directories prepared; service stopped'
    }
    'Stop' {
        Stop-OwnedService
        Disable-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
        Write-Output 'Secure desktop stopped for maintenance'
    }
    'Install' {
        if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { throw 'The secure worker was not installed' }
        Assert-ProtectedTree $InstallDir
        Assert-ProtectedTree $StateDir -Confidential
        Stop-OwnedService
        Set-ProtectedDirectory $StateDir
        $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        if (-not $service) {
            New-Service -Name $ServiceName -DisplayName 'Beamer Secure Desktop' -StartupType Disabled `
                -BinaryPathName ('"' + $Executable + '" --service') `
                -Description 'Optional input from administrator-approved paired desktops at Windows sign-in and UAC.' | Out-Null
        } else {
            Invoke-Sc @('config', $ServiceName, 'binPath=', ('"' + $Executable + '" --service'), 'obj=', 'LocalSystem')
        }
        Invoke-Sc @('sdset', $ServiceName, 'D:P(A;;CCLCSWRPWPDTLOCRRC;;;SY)(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)(A;;CCLC;;;AU)')
        Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
        New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Protocol TCP -LocalPort 24822 `
            -Action Allow -Profile Private -RemoteAddress LocalSubnet -Program $Executable -Enabled False | Out-Null
        Write-Output 'Secure desktop installed disabled; no peer has gained access'
    }
    'Grant' {
        if (-not $SettingsPath -or -not $PeerIds -or $PeerIds.Count -eq 0) { throw 'Select the settings file and paired peer IDs' }
        if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) { throw 'Install secure desktop first' }
        Assert-ProtectedTree $InstallDir
        Assert-ProtectedTree $StateDir -Confidential
        Stop-OwnedService
        try {
            & $Executable --enrol --settings $SettingsPath --peer-id @PeerIds
            if ($LASTEXITCODE -ne 0) { throw 'Secure desktop enrolment refused' }
            Enable-NetFirewallRule -DisplayName $RuleName
            Set-Service -Name $ServiceName -StartupType Automatic
            Start-Service -Name $ServiceName
            (Get-Service -Name $ServiceName).WaitForStatus('Running', [TimeSpan]::FromSeconds(15))
            if (-not (Test-Path -LiteralPath $ApprovedKey)) { New-Item -Path $ApprovedKey -Force | Out-Null }
            New-ItemProperty -Path $ApprovedKey -Name ApprovedPeers -PropertyType MultiString -Value ([string[]]$PeerIds) -Force | Out-Null
        } catch {
            Stop-OwnedService
            Disable-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
            throw
        }
        Write-Output 'Only the selected paired desktops can now use secure input'
    }
    'Disable' {
        Stop-OwnedService
        Disable-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $GrantFile) { Remove-Item -LiteralPath $GrantFile -Force }
        Remove-ItemProperty -Path $ApprovedKey -Name ApprovedPeers -ErrorAction SilentlyContinue
        Write-Output 'Secure desktop stopped and all grants removed'
    }
    'Remove' {
        Stop-OwnedService
        if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) { Invoke-Sc @('delete', $ServiceName) }
        Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
        if (Test-Path -LiteralPath $ApprovedKey) { Remove-Item -LiteralPath $ApprovedKey -Recurse -Force }
        if (Test-Path -LiteralPath $StateDir) {
            foreach ($item in Get-ChildItem -Force -LiteralPath $StateDir -Recurse) { Assert-PlainPath $item.FullName }
            Remove-Item -LiteralPath $StateDir -Recurse -Force
        }
        Write-Output 'Secure desktop service, firewall rule and grants removed'
    }
}
