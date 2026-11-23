"""用独立资料根验证首次扫码配置、账号复用及进程归属，不登录真实 QQ。"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msgx.managed_napcat import ManagedNapCat, configure, pair_account


class PortableTests(unittest.TestCase):
    def test_installed_paths_config_and_process_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "资料"
            resources = Path(tmp) / "带 空格的安装目录" / "resources"
            runtime = ManagedNapCat(resources)
            for f in ("node.exe", "index.js", "wrapper.node", "napcat/napcat.mjs"):
                file = runtime.runtime / f
                file.parent.mkdir(parents=True, exist_ok=True)
                file.touch()
            runtime.guard.parent.mkdir(parents=True)
            runtime.guard.touch()
            cfg = {"napcat": {}, "onebot": {"ws_port": 1234, "access_token": "test-only"}}
            configure(cfg, root, True)
            work = Path(cfg["napcat"]["managed_dir"])
            with patch.dict(os.environ, {"NAPCAT_QUICK_PASSWORD": "unused"}), patch("subprocess.Popen") as popen:
                popen.return_value.poll.return_value = None
                runtime.start(cfg)
                args, kwargs = popen.call_args
                self.assertEqual(args[0][0], str(runtime.runtime / "node.exe"))
                self.assertNotIn("-q", args[0])
                self.assertNotIn("NAPCAT_QUICK_PASSWORD", kwargs["env"])
                self.assertEqual(kwargs["env"]["NAPCAT_QQ_DATA_DIR"], str(work / "qq-data"))
                self.assertEqual(kwargs["cwd"], runtime.runtime)
                self.assertEqual(json.loads((work / "config/webui.json").read_text())["host"], "127.0.0.1")
                with self.assertRaisesRegex(RuntimeError, "正在运行"):
                    runtime.start(cfg)
                runtime.stop()
                popen.return_value.stdin.close.assert_called_once()
                pair_account(work, "12345")
                configure(cfg, root, True)
                self.assertEqual(cfg["napcat"]["account"], "12345")
                runtime.start(cfg)
                self.assertEqual(popen.call_args.args[0][-2:], ["-q", "12345"])
                runtime.stop()
                runtime.start(cfg, scan=True)
                self.assertNotIn("-q", popen.call_args.args[0])
                entries = json.loads((work / "config/onebot11_12345.json").read_text())["network"]["websocketClients"]
                self.assertEqual([(v["url"], v["token"]) for v in entries], [("ws://127.0.0.1:1234/onebot/ws", "test-only")])
                runtime.stop()
            cfg["napcat"]["dir"] = "existing external installation"
            configure(cfg, root, True)
            self.assertNotIn("managed_dir", cfg["napcat"])
            cfg["napcat"]["dir"] = ""
            configure(cfg, root, False)
            self.assertNotIn("managed_dir", cfg["napcat"])


if __name__ == "__main__":
    unittest.main()
