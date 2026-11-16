"""Exercise the management script's address selection and firewall update without elevation."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")
SCRIPT = Path(__file__).resolve().parents[1] / "Manage-SecureDesktop.ps1"


@unittest.skipUnless(POWERSHELL, "PowerShell is required")
class SecureFirewallTests(unittest.TestCase):
    def invoke(self, peers, ids, *, script=SCRIPT):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text(json.dumps({"peers": peers}), encoding="utf-8")
            env = dict(os.environ, BEAMER_FIREWALL_SCRIPT=str(script),
                       BEAMER_FIREWALL_SETTINGS=str(settings),
                       BEAMER_FIREWALL_IDS=json.dumps(ids))
            return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", r'''
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($env:BEAMER_FIREWALL_SCRIPT, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Script syntax errors' }
foreach ($name in @('Get-ApprovedPeerAddresses', 'Enable-ApprovedPeerFirewall')) {
    $function = $ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name}, $true)
    if (-not $function) { throw "Missing production function: $name" }
    . ([scriptblock]::Create($function.Extent.Text))
}
$RuleName = 'Beamer Secure Desktop (TCP-In)'
function Set-NetFirewallRule {
    param($DisplayName, $Profile, $RemoteAddress, $Enabled)
    $script:rule = @{name=$DisplayName; profile=$Profile; remote=@($RemoteAddress); enabled=$Enabled}
}
$addresses = @(Get-ApprovedPeerAddresses $env:BEAMER_FIREWALL_SETTINGS (ConvertFrom-Json $env:BEAMER_FIREWALL_IDS))
Enable-ApprovedPeerFirewall $addresses
$script:rule | ConvertTo-Json -Compress
'''], env=env, capture_output=True, text=True, timeout=30)

    def test_public_rule_only_allows_selected_paired_addresses(self):
        result = self.invoke([
            {"id": "approved", "host": "192.0.2.1"},
            {"id": "other", "host": "192.0.2.3"},
            {"id": "v6", "host": "2001:db8::1"},
            {"id": "mapped", "host": "::ffff:192.0.2.1"},
        ], ["approved", "v6", "mapped"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "name": "Beamer Secure Desktop (TCP-In)", "profile": "Any",
            "remote": ["192.0.2.1", "2001:db8::1"], "enabled": "True",
        })

    def test_missing_ambiguous_or_broad_addresses_fail_closed(self):
        cases = [([], ["missing"]),
                 ([{"id": "a", "host": "192.0.2.1"}] * 2, ["a"])]
        cases += [([{"id": "a", "host": host}], ["a"]) for host in
                  ("", "192.0.2.0/24", "*", "0.0.0.0", "::", "127.0.0.1",
                   "224.0.0.1", "255.255.255.255", "ff02::1", "0.1.2.3",
                   "::ffff:0.0.0.0", "::ffff:127.0.0.2", "::ffff:224.0.0.1",
                   "::ffff:255.255.255.255")]
        for peers, ids in cases:
            with self.subTest(peers=peers):
                result = self.invoke(peers, ids)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertNotIn('"enabled"', result.stdout)


if __name__ == "__main__":
    unittest.main()
