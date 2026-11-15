"""从开发环境构建安装版后台目录，供桌面安装包收集。"""
from __future__ import annotations

import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from msgx.conda_runtime import require_environment


def main() -> None:
    require_environment()
    # RapidOCR 根据 YAML 用这些顶层模块名动态加载识别器。
    ocr = Path(find_spec("rapidocr_onnxruntime").origin).parent
    stage = ROOT / "installer/dist/backend-build"
    stage.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        sys.executable, "-s", "-m", "PyInstaller", "--noconfirm", "--onedir", "--console",
        "--name", "msgx-backend", "--distpath", str(ROOT / "installer/dist/backend"),
        "--workpath", str(stage / "build"), "--specpath", str(stage),
        "--paths", str(ROOT / "src"), "--paths", str(ROOT / "desktop"),
        "--paths", str(ocr), "--hidden-import", "ch_ppocr_v3_det",
        "--hidden-import", "ch_ppocr_v2_cls", "--hidden-import", "ch_ppocr_v3_rec",
        "--collect-data", "rapidocr_onnxruntime", "--collect-binaries", "onnxruntime",
        str(ROOT / "installer/backend_entry.py"),
    ], cwd=ROOT, check=True)
    print(f"安装版后台已生成：{ROOT / 'installer/dist/backend/msgx-backend'}")


if __name__ == "__main__":
    main()
