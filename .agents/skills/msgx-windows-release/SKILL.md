---
name: msgx-windows-release
description: 验证或修改 msgx 的 Windows 源码启动、轻量 EXE、安装版、应用更新、本地版本发布或图标时使用。不用于消息业务、凭据编辑或仅整理测试用例。
---

# Windows安装与发布

适用：Windows 源码版和当前用户安装版。历史验收的日期与适用范围见参考资料。

- 开发者自行准备Conda，一项目一个独立环境，默认名 `msgx`；PowerShell统一使用 `conda run -n msgx --no-capture-output python -s ...` 安装、源码运行和测试。业务依赖只维护 `pyproject.toml`，pip使用 `--isolated install --no-user`；不向公共 `base`、全局Python或用户site-packages安装，不创建或嵌套venv。不得借用作者目录、未跟踪EXE、凭据或数据作为启动前提。
- 源码版沿用独立 Conda 和轻量 EXE。NapCat 的 BAT/VBS 和目标重启 PS1 属于协议/UAC 实现，保留现有用途。
- 安装、卸载与更新源码在 `installer/`；新产物在忽略的 `installer/dist/`，旧 `.ui/installer-build/` 成品原位保留。`desktop/package.json` 仍是桌面依赖及 npm 命令入口，不能因移动构建脚本另建依赖清单。
- Windows x64 当前用户 NSIS 随包提供应用与运行时。新装可选空目录，覆盖安装沿用注册路径。个人资料在程序目录之外，首次设置只调整主题和可选 API 密钥。安装版、资料复用及更新任务读取 [安装版与公开更新](references/windows.md#安装版与公开更新)。
- 更新安装说明时，就近标明命令的执行目录、权限和成功标志；普通Conda/GUI操作与NapCat的UAC分开。目录权限、文件占用及工具沙箱错误不能直接推导为“全程管理员运行”。维护README或排障命令时读取 [权限与文档核对](references/windows.md#权限与安装说明维护时读取)。
- 源码版 Python 最低为 3.12，推荐 3.13 x64，Tk 用于旧界面对照。2026-10-19 已隔离验证当时的 Windows Conda、GUI、轻量 EXE 和后台链路，范围与证据见参考资料。
- 启动/依赖变更以真正Git跟踪源码、新路径和独立环境验收；版本同时核对源码、窗口与distribution元数据。缓存下载耗时与GUI启动耗时分开报告。
- 仓库主 EXE 为 onefile 轻量源码启动器，通过 Conda 使用 `msgx` 或 `MSGX_CONDA_ENV` 指定的项目环境。缺少 Conda、环境或源码时明确报错。启动器或图标资源变化时用 `tools/build_launcher.py` 重建，先备份旧 EXE 并沿用已保存图标。安装版构建独立于这条链。
- 安装/版本或图标任务分别读取 [克隆验证、元数据和图标证据](references/windows.md) 对应章节；窗口、EXE资源和Explorer显示分别确认，单层正确不能替代其它层。
