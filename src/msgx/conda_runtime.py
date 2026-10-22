"""项目 Conda 运行入口；仅使用标准库，允许轻量 EXE 单独打包。"""
from __future__ import annotations

import os
import re
import shutil
import site
import sys
from pathlib import Path


def environment_name() -> str:
    name = os.environ.get("MSGX_CONDA_ENV", "msgx").strip()  # 每个项目使用明确命名的独立环境。
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) or name.casefold() in {"base", "root"}:
        raise RuntimeError("MSGX_CONDA_ENV 必须是独立 Conda 环境名，不能使用 base/root 或目录路径；默认使用 msgx。")
    return name


def conda_executable() -> str:
    explicit = os.environ.get("CONDA_EXE", "").strip()  # 显式路径无效时不悄悄改用其它安装。
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.name.casefold() == "conda.exe" and candidate.is_file():
            return str(candidate.resolve())
        raise RuntimeError("CONDA_EXE 未指向可用的 conda.exe；请修正路径并按 README.md 准备项目 Conda 环境。")
    found = shutil.which("conda.exe")
    if found:
        return str(Path(found).resolve())
    wrapper = shutil.which("conda.bat")  # 仅借包装路径定位真正的 Conda EXE，不执行批处理。
    if wrapper:
        path = Path(wrapper)
        candidate = path.parent.parent / "Scripts" / "conda.exe"
        if path.parent.name.casefold() == "condabin" and candidate.is_file():
            return str(candidate.resolve())
    raise RuntimeError("找不到 conda.exe；请先安装 Miniconda/Anaconda，将 Conda 加入 PATH 或设置 CONDA_EXE，详见 README.md。")


def command(*args: str, python: str = "python") -> list[str]:
    name = environment_name()  # 先拒绝公共环境，再发现 Conda 安装。
    return [conda_executable(), "run", "-n", name, "--no-capture-output", python, "-s", *args]


def require_environment() -> Path:
    name = environment_name()
    hint = f"请使用 conda run -n {name} --no-capture-output python -s 启动，安装方法见 README.md。"
    raw_prefix = os.environ.get("CONDA_PREFIX", "").strip()
    if not raw_prefix:
        raise RuntimeError(f"当前进程未进入项目 Conda 环境。{hint}")
    prefix = Path(raw_prefix).resolve()
    actual = Path(sys.prefix).resolve()
    if prefix != actual or not (prefix / "conda-meta").is_dir():
        raise RuntimeError(f"当前 Python 不属于选定的 Conda 环境，不能回退到全局 Python。{hint}")
    active = os.environ.get("CONDA_DEFAULT_ENV", "").strip()
    active_path = Path(active) if active else None
    path_name_matches = (active_path is not None and active_path.is_absolute()
                         and active_path.resolve() == prefix and Path(raw_prefix).name.casefold() == name.casefold())
    if active.casefold() != name.casefold() and not path_name_matches:  # 自定义 envs_dirs 时 Conda 会提供完整路径。
        raise RuntimeError(f"当前环境不是项目专用环境 {name}。{hint}")
    executable = Path(conda_executable())
    if executable.parent.name.casefold() == "scripts" and prefix == executable.parent.parent:
        raise RuntimeError(f"禁止从公共 Conda base 运行项目。{hint}")
    if not sys.flags.no_user_site or site.ENABLE_USER_SITE is not False:
        raise RuntimeError(f"当前 Python 未禁用用户 site-packages。{hint}")
    return prefix  # 调用方只能使用通过归属校验的项目环境。
