"""自动登录回归：仅模拟本机管理接口，不操作 QQ 或调用模型。"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx import napcat


class LoginTests(unittest.TestCase):
    def run_login(self, states, *, configured="123", accounts=None, fail=False, wait=10):
        client = Mock()
        status = iter(states)
        current = states[-1]

        def call(path, data=None):
            nonlocal current
            if path.endswith("CheckLoginStatus"):
                current = next(status, current)
                return current
            if path.endswith("GetQQLoginInfo"):
                return {"uin": "123"}
            if path.endswith("GetQuickLoginQQ"):
                return configured
            if path.endswith("GetQuickLoginList"):
                return ["123"] if accounts is None else accounts
            if path.endswith("SetQuickLogin") and fail:
                raise RuntimeError("native login rejected")
            return None

        client.call.side_effect = call
        with patch.object(napcat, "_WebUI", return_value=client), \
                patch.object(napcat.time, "sleep"), patch.object(napcat, "launch") as launch, \
                patch.object(napcat, "restart") as restart:
            result = napcat.quick_login({"napcat": {"dir": "mock"}}, wait_seconds=wait)
        launch.assert_not_called()
        restart.assert_not_called()
        return result, client

    def test_online_enables_native_auto_login_without_logging_in_again(self):
        result, client = self.run_login([{"isLogin": True, "coreReady": True}], configured="")
        self.assertTrue(result["ok"])
        client.call.assert_any_call("/QQLogin/SetQuickLoginQQ", {"uin": "123"})
        self.assertFalse(any(c.args[0].endswith("/SetQuickLogin") for c in client.call.call_args_list))

    def test_offline_submits_once_and_waits_until_core_ready(self):
        result, client = self.run_login([
            {"loginPhase": "waiting_qrcode"}, {"loginPhase": "initializing"},
            {"isLogin": True, "coreReady": True}], configured="", accounts=["123"])
        self.assertTrue(result["ok"])
        self.assertEqual(sum(c.args[0].endswith("/SetQuickLogin") for c in client.call.call_args_list), 1)

    def test_rejected_credentials_require_scan_without_retry(self):
        result, client = self.run_login([{"loginPhase": "waiting_qrcode"}], fail=True)
        self.assertFalse(result["ok"])
        self.assertTrue(result["scan"])
        self.assertEqual(sum(c.args[0].endswith("/SetQuickLogin") for c in client.call.call_args_list), 1)

    def test_multiple_accounts_are_not_guessed(self):
        result, client = self.run_login([{"loginPhase": "waiting_qrcode"}], configured="", accounts=["123", "456"])
        self.assertIn("多个", result["note"])
        self.assertFalse(any("/Set" in c.args[0] for c in client.call.call_args_list))

    def test_missing_target_does_not_switch_to_other_account(self):
        result, client = self.run_login([{"loginPhase": "waiting_qrcode"}], accounts=["456"])
        self.assertTrue(result["scan"])
        self.assertFalse(any("/Set" in c.args[0] for c in client.call.call_args_list))

    def test_native_reconnect_timeout_does_not_issue_login_or_restart(self):
        result, client = self.run_login([{"loginPhase": "reconnecting"}], wait=0)
        self.assertFalse(result["ok"])
        self.assertNotIn("scan", {k: v for k, v in result.items() if v})
        self.assertFalse(any("/Set" in c.args[0] for c in client.call.call_args_list))

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
                    client.call("/QQLogin/SetQuickLogin", {"uin": "123"})
            self.assertEqual(json.loads(requests[0].data)["hash"], hashlib.sha256(b"dummy-secret.napcat").hexdigest())
            self.assertEqual(requests[1].get_header("Authorization"), "Bearer dummy-session")
            self.assertTrue(all(r.full_url.startswith("http://127.0.0.1:6099/api/") for r in requests))
            self.assertNotIn("dummy-", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
