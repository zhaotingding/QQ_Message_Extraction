"""Manual restart checks. No live QQ, NapCat, or API is started."""
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
import time
import unittest
import uuid
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx import napcat


class ManualRestartTests(unittest.TestCase):
    def setUp(self):
        workspace = Path(__file__).resolve().parents[1]
        self.root = workspace / 'data/test-temp/napcat-restart' / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.assertTrue(self.root.resolve().is_relative_to(workspace))
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / 'tools').mkdir()
        (self.root / 'tools/restart_napcat_target.ps1').write_text('mock', encoding='utf-8')
        self.cfg = {'napcat': {'dir': str(self.root), 'account': '123'}}

    def run_restart(self, outcome, *, live='123', launch_code=42, timeout=1):
        client = Mock()
        client.call.return_value = {'uin': live}

        def launch(exe, params, show):
            self.assertEqual(exe, 'powershell.exe')
            self.assertEqual(show, 0)
            request = next((self.root / '.ui/napcat-restart').glob('*.request.json'))
            data = json.loads(request.read_text(encoding='utf-8'))
            self.assertEqual(data['account'], '123')
            self.assertNotIn('token', data)
            if outcome is not None:
                payload = {**outcome}
                if payload.get('scan'):
                    payload.setdefault('qr_since', time.time())
                Path(data['result']).write_text(json.dumps(payload), encoding='utf-8')
            return launch_code

        with patch.object(napcat, 'ROOT', self.root), \
                patch.object(napcat, '_WebUI', return_value=client), \
                patch.object(napcat, 'preflight', return_value={'ok': True, 'qq': 'QQ.exe'}), \
                patch.object(napcat, '_shell_execute_runas', side_effect=launch) as start:
            result = napcat.restart_target(self.cfg, wait_seconds=timeout)
        return result, client, start

    def test_success_is_scan_pending_and_not_login_success(self):
        result, client, start = self.run_restart({'scan': True})
        self.assertFalse(result['ok'])
        self.assertTrue(result['scan'])
        self.assertEqual(result['account'], '123')
        self.assertGreater(result['qr_since'], 0)
        start.assert_called_once()
        self.assertEqual(list((self.root / '.ui/napcat-restart').iterdir()), [])
        self.assertEqual(client.call.call_args_list[0].args[0], '/QQLogin/GetQQLoginInfo')
        self.assertFalse(any('RestartNapCat' in c.args[0] for c in client.call.call_args_list))

    def test_other_account_is_never_restarted(self):
        result, _, start = self.run_restart({'scan': True}, live='456')
        self.assertFalse(result['ok'])
        start.assert_not_called()

    def test_helper_failure_and_timeout_do_not_claim_connection(self):
        for outcome in ({'scan': False, 'note': 'mock failed'}, None):
            result, _, _ = self.run_restart(outcome, timeout=0.01)
            self.assertFalse(result['ok'])
            self.assertFalse(result.get('scan'))

    def test_uac_cancel_is_not_reported_as_launched(self):
        result, _, _ = self.run_restart(None, launch_code=5)
        self.assertFalse(result.get('scan'))
        self.assertIn('UAC', result['note'])

    def test_uses_helper_qr_time_and_preserves_restore_warning(self):
        result, _, _ = self.run_restart({'scan': True, 'qr_since': 1700000000.25, 'note': 'restore warning'})
        self.assertEqual(result['qr_since'], 1700000000.25)
        self.assertIn('restore warning', result['note'])

    def test_request_write_error_is_safe_failure(self):
        with patch.object(napcat, '_WebUI', side_effect=OSError), \
                patch.object(napcat, 'ROOT', self.root), \
                patch.object(napcat, 'preflight', return_value={'ok': True, 'qq': 'QQ.exe'}), \
                patch.object(Path, 'mkdir', side_effect=PermissionError), \
                patch.object(napcat, '_shell_execute_runas') as launch:
            result = napcat.restart_target(self.cfg)
        self.assertFalse(result['ok'])
        launch.assert_not_called()

    def test_missing_management_api_uses_scoped_helper(self):
        with patch.object(napcat, '_WebUI', side_effect=OSError), \
                patch.object(napcat, 'ROOT', self.root), \
                patch.object(napcat, 'preflight', return_value={'ok': True, 'qq': 'QQ.exe'}), \
                patch.object(napcat, '_shell_execute_runas', return_value=5) as launch:
            result = napcat.restart_target(self.cfg)
        launch.assert_called_once()
        self.assertFalse(result['ok'])

    @unittest.skipUnless(os.name == 'nt', 'PowerShell selection check requires Windows')
    def test_process_selection_keeps_ordinary_qq_and_other_installation(self):
        script = Path(__file__).resolve().parents[1] / 'tools/restart_napcat_target.ps1'
        command = r'''
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'PowerShell parse failed' }
$function = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Select-NapCatProcessIds' }, $false)
Invoke-Expression $function.Extent.Text
$items = @(
 [pscustomobject]@{ProcessId=10;ParentProcessId=1;Name='NapCatWinBootMain.exe';ExecutablePath='D:\NapCat\NapCatWinBootMain.exe'},
 [pscustomobject]@{ProcessId=11;ParentProcessId=10;Name='QQ.exe';ExecutablePath='D:\QQ\QQ.exe'},
 [pscustomobject]@{ProcessId=12;ParentProcessId=11;Name='QQ.exe';ExecutablePath='D:\QQ\QQ.exe'},
 [pscustomobject]@{ProcessId=13;ParentProcessId=11;Name='msedge.exe';ExecutablePath='D:\Browser\msedge.exe'},
 [pscustomobject]@{ProcessId=20;ParentProcessId=1;Name='QQ.exe';ExecutablePath='D:\QQ\QQ.exe'},
 [pscustomobject]@{ProcessId=30;ParentProcessId=1;Name='NapCatWinBootMain.exe';ExecutablePath='D:\Other\NapCatWinBootMain.exe'},
 [pscustomobject]@{ProcessId=31;ParentProcessId=30;Name='QQ.exe';ExecutablePath='D:\QQ\QQ.exe'},
 [pscustomobject]@{ProcessId=40;ParentProcessId=1;Name='QQ.exe';ExecutablePath='D:\QQ\QQ.exe'},
 [pscustomobject]@{ProcessId=41;ParentProcessId=40;Name='QQ.exe';ExecutablePath='D:\QQ\QQ.exe'}
)
@(Select-NapCatProcessIds $items 'D:\NapCat' @(40)) | Sort-Object | ConvertTo-Json -Compress
'''
        # Only the pure selector function is evaluated; the helper body never executes.
        check = self.root / 'selection.ps1'
        check.write_text(command, encoding='utf-8-sig')
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-File', str(check), str(script)],
                                capture_output=True, text=True, timeout=20, creationflags=napcat._CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [10, 11, 12, 40, 41])

    @unittest.skipUnless(os.name == 'nt', 'PowerShell helper check requires Windows')
    def test_helper_flow_with_mock_processes_and_local_api(self):
        script = Path(__file__).resolve().parents[1] / 'tools/restart_napcat_target.ps1'
        install = self.root / 'napcat'
        (install / 'config').mkdir(parents=True)
        (install / 'cache').mkdir()
        (install / 'napcat.mjs').write_text('mock', encoding='utf-8')
        (install / 'loadNapCat.js').write_text('preserve existing loader', encoding='utf-8')
        webui = install / 'config/webui.json'
        webui.write_text(json.dumps({'token': 'synthetic-private-token', 'autoLoginAccount': '123', 'other': 7}), encoding='utf-8')
        response = self.root / 'helper-result.json'
        request = self.root / 'helper-request.json'
        request.write_text(json.dumps({'dir': str(install), 'account': '123', 'qq': 'D:\\QQ\\QQ.exe', 'result': str(response)}), encoding='utf-8')
        # Replace only API transport and process cmdlets. All workflow, JSON settings,
        # mutex, QR timestamp and restore logic are the actual helper implementation.
        wrapper = self.root / 'mock_helper.ps1'
        wrapper.write_text(r'''
param($Source, $JobFile, $Patched, $Scenario)
$global:fixture = Get-Content -LiteralPath $JobFile -Raw -Encoding UTF8 | ConvertFrom-Json
$global:scenario = $Scenario
function Get-CimInstance { @() }
function Get-Process { param($Name, $Id, $ErrorAction) @() }
function Stop-Process { throw 'No process may be stopped in this fixture' }
function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle)
    if ($WindowStyle -ne 'Hidden') { throw 'Visible launch' }
    if ($env:NAPCAT_QUICK_ACCOUNT -or $env:NAPCAT_QUICK_PASSWORD -or $env:NAPCAT_QUICK_PASSWORD_MD5) { throw 'Quick login environment was inherited' }
    $config = Get-Content -LiteralPath (Join-Path $WorkingDirectory 'config\webui.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($config.autoLoginAccount) { throw 'Quick login was not paused' }
    if ($global:scenario -eq 'fail') { throw 'Synthetic launch failure' }
}
$env:NAPCAT_QUICK_ACCOUNT = '123'
$env:NAPCAT_QUICK_PASSWORD = 'synthetic-password'
$env:NAPCAT_QUICK_PASSWORD_MD5 = 'synthetic-md5'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
$api = $ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Call-LocalApi'}, $false)
$mock = @'
function Call-LocalApi([string]$Path, $Body = @{}) {
    switch ($Path) {
        '/auth/login' { return @{ Credential = 'synthetic-credential' } }
        '/QQLogin/CheckLoginStatus' { return @{ isLogin = $false } }
        '/QQLogin/RefreshQRcode' {
            [IO.File]::WriteAllText((Join-Path $global:fixture.dir 'cache\qrcode.png'), 'synthetic-new-qr')
            return @{ restarting = $false }
        }
        '/QQLogin/SetQuickLoginQQ' { return $null }
        default { throw 'Unexpected API request' }
    }
}
'@
$code = Get-Content -LiteralPath $Source -Raw -Encoding UTF8
$code = $code.Remove($api.Extent.StartOffset, $api.Extent.EndOffset - $api.Extent.StartOffset).Insert($api.Extent.StartOffset, $mock)
[IO.File]::WriteAllText($Patched, $code, [Text.UTF8Encoding]::new($true))
& $Patched -RequestFile $JobFile
''', encoding='utf-8-sig')
        for scenario in ('success', 'fail'):
            with self.subTest(scenario=scenario):
                result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-File', str(wrapper), str(script),
                                         str(request), str(self.root / 'patched-helper.ps1'), scenario], capture_output=True, text=True,
                                        timeout=20, creationflags=napcat._CREATE_NO_WINDOW)
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(response.read_text(encoding='utf-8-sig'))
                self.assertEqual(data['scan'], scenario == 'success', data)
                if scenario == 'success':
                    self.assertGreater(data['qr_since'], time.time() - 20)
                else:
                    self.assertIn('启动扫码登录', data['note'])
                self.assertEqual(json.loads(webui.read_text(encoding='utf-8-sig')),
                                 {'token': 'synthetic-private-token', 'autoLoginAccount': '123', 'other': 7})
                self.assertEqual((install / 'loadNapCat.js').read_text(encoding='utf-8'), 'preserve existing loader')
                self.assertNotIn('synthetic-private-token', result.stdout + result.stderr + str(data))


if __name__ == '__main__':
    unittest.main()
