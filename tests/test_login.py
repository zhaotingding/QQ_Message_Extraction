"""二维码刷新、同账号恢复与本机认证回归：仅模拟管理接口，不操作真实 QQ。"""
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx import napcat


class LoginTests(unittest.TestCase):
    def test_refresh_qr_does_not_restart_or_change_account(self):
        for phase, online, refresh in (("waiting_qrcode", False, True),
                                        ("ready", True, False), ("initializing", False, False)):
            client = Mock()
            client.call.side_effect = lambda path: {
                "/QQLogin/CheckLoginStatus": {"isLogin": online, "coreReady": online, "loginPhase": phase},
                "/QQLogin/GetQQLoginInfo": {"uin": "123"},
                "/QQLogin/RefreshQRcode": {"qrcodeurl": "private-login-url"},
            }[path]
            with patch.object(napcat, "_WebUI", return_value=client), \
                    patch.object(napcat, "restart") as restart, patch.object(napcat, "launch") as launch:
                result = napcat.refresh_login_qrcode({"napcat": {"dir": "mock"}})
            calls = [c.args[0] for c in client.call.call_args_list]
            self.assertEqual("/QQLogin/RefreshQRcode" in calls, refresh)
            self.assertEqual(bool(result.get("scan")), refresh)
            self.assertNotIn("private-login-url", str(result))
            self.assertFalse(any("/Set" in c or "Restart" in c for c in calls))
            restart.assert_not_called()
            launch.assert_not_called()

    def test_recovery_never_restarts_or_logs_in_as_other_account(self):
        for current, saved, cached, expected in (
                ("123", "123", ["456"], False),
                ("456", "123", ["123", "456"], False),
                ("123", "456", ["123", "456"], False),
                ("123", "123", ["123", "456"], True)):
            with self.subTest(current=current, saved=saved, cached=cached):
                client = Mock()
                client.call.side_effect = lambda path: {
                    "/QQLogin/GetQQLoginInfo": {"uin": current},
                    "/QQLogin/GetQuickLoginQQ": saved,
                    "/QQLogin/GetQuickLoginList": cached,
                    "/QQLogin/RestartNapCat": None,
                }[path]
                with patch.object(napcat, "_WebUI", return_value=client), \
                        patch.object(napcat, "restart") as force, patch.object(napcat, "launch") as launch:
                    result = napcat.recover_worker({"napcat": {"dir": "mock"}}, "123")
                self.assertEqual(result["ok"], expected)
                calls = [c.args[0] for c in client.call.call_args_list]
                self.assertEqual(calls.count("/QQLogin/RestartNapCat"), int(expected))
                self.assertFalse(any("/Set" in c for c in calls))
                force.assert_not_called()
                launch.assert_not_called()

    def test_active_other_account_cannot_mask_stale_target_database(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"QQ_DATA_ROOT": tmp}):
            for account, age in (("123", 4000), ("456", 0)):
                p = Path(tmp) / account / "nt_qq/nt_db/nt_msg.db-wal"
                p.parent.mkdir(parents=True)
                p.write_bytes(b"test")
                stamp = time.time() - age
                os.utime(p, (stamp, stamp))
            self.assertLess(napcat.qq_msg_db_age(), 5)
            self.assertGreater(napcat.qq_msg_db_age("123"), 3990)
            self.assertIsNone(napcat.qq_msg_db_age("789"))
            self.assertIsNone(napcat.qq_msg_db_age("../456"))

    def test_request_uses_local_auth_and_never_exposes_secret_in_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "webui.json").write_text(json.dumps({"port": 6099, "token": "dummy-secret"}))
            opener = Mock()
            responses = iter([{"code": 0, "data": {"Credential": "dummy-session"}},
                              {"code": 1, "message": "dummy-secret dummy-session"}])
            requests = []

            def respond(req, **kwargs):
                requests.append(req)
                response = Mock()
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                response.read.return_value = json.dumps(next(responses)).encode()
                return response

            opener.open.side_effect = respond
            with patch.object(napcat.urllib.request, "build_opener", return_value=opener):
                client = napcat._WebUI(root)
                with self.assertRaises(RuntimeError) as failure:
                    client.call("/QQLogin/RefreshQRcode")  # 用当前支持的接口验证认证及错误脱敏。
            self.assertEqual(json.loads(requests[0].data)["hash"], hashlib.sha256(b"dummy-secret.napcat").hexdigest())
            self.assertEqual(requests[1].get_header("Authorization"), "Bearer dummy-session")
            self.assertTrue(all(r.full_url.startswith("http://127.0.0.1:6099/api/") for r in requests))
            self.assertNotIn("dummy-", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
