"""重新构建现有启动器，始终沿用本机保存的自定义图标。"""
from datetime import datetime
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from msgx import prefs, yaml_config


def selected_icon(root: Path) -> Path | None:
    preferences = prefs.prefs_path(root)
    saved = yaml_config.read(preferences) if preferences.exists() else {}
    path = (saved.get("ui") or {}).get("icon_path")
    if path and Path(path).is_file():
        return Path(path)
    local = root / ".ui" / "app-icon.ico"
    return local if local.is_file() else None


def refresh_shell_icon(target: Path) -> None:
    import ctypes as C
    notify = C.windll.shell32.SHChangeNotify
    notify.argtypes = [C.c_long, C.c_uint, C.c_void_p, C.c_void_p]
    # 单文件 UPDATEITEM 实测不足以刷新 Explorer 大图；同时失效图标/缩略图缓存。
    # 只发送 Shell 通知，不删除缓存文件、修改关联或重启 Explorer。
    notify(0x08000000, 0x1000, None, None)  # SHCNE_ASSOCCHANGED | SHCNF_FLUSH
    notify(0x2000, 0x1005, C.c_wchar_p(str(target)), None)
    notify(0x1000, 0x1005, C.c_wchar_p(str(target.parent)), None)


def main() -> int:
    work = ROOT / ".ui" / "launcher-build"
    work.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--noconsole",
               "--name", "QQ_Message_Extraction", "--distpath", str(work / "dist"),
               "--workpath", str(work / "build"), "--specpath", str(work), str(ROOT / "tools" / "exe_main.py")]
    icon = selected_icon(ROOT)
    if icon:
        command.extend(["--icon", str(icon)])
    subprocess.run(command, cwd=ROOT, check=True)
    target = ROOT / "QQ_Message_Extraction.exe"
    if target.exists():
        backup = ROOT / ".ui" / "launcher-backups"
        backup.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup / f"QQ_Message_Extraction-{datetime.now():%Y%m%d-%H%M%S}.exe")
    staged = target.with_suffix(".new.exe")
    shutil.copy2(work / "dist" / target.name, staged)
    staged.replace(target)
    if sys.platform == "win32":
        refresh_shell_icon(target)
    print(f"启动器已更新：{target}；图标：{icon or '默认图标'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
