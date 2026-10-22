---
name: msgx-windows-release
description: 验证或修改 msgx 的Windows克隆安装、启动入口、本地版本发布、EXE构建或窗口/Explorer图标资源时使用。不用于消息业务、凭据编辑或仅整理测试用例。
---

# Windows安装与发布

适用：Windows源码克隆与本地轻量启动器；约定更新：2026-10-20。历史验收的日期与适用范围见参考资料。

- 开发者自行准备Conda，一项目一个独立环境，默认名 `msgx`；PowerShell统一使用 `conda run -n msgx --no-capture-output python -s ...` 安装、源码运行和测试。业务依赖只维护 `pyproject.toml`，pip使用 `--isolated install --no-user`；不向公共 `base`、全局Python或用户site-packages安装，不创建或嵌套venv。不得借用作者目录、未跟踪EXE、凭据或数据作为启动前提。
- 不新增安装器、应用安装/启动的CMD/BAT包装或自动环境管理层。NapCat的BAT/VBS和目标重启PS1属于协议/UAC实现；不得按扩展名将其作为项目入口一并清理。
- 更新安装说明时，就近标明命令的执行目录、权限和成功标志；普通Conda/GUI操作与NapCat的UAC分开。目录权限、文件占用及工具沙箱错误不能直接推导为“全程管理员运行”。维护README或排障命令时读取 [权限与文档核对](references/windows.md#权限与安装说明维护时读取)。
- 当前固定依赖最低要求Python3.12，推荐3.13 x64、含Tk。2026-10-19已测Windows/独立Conda环境/Python3.13.16的安装、源码GUI、主EXE和隔离后台链路；历史venv验收环境为Python3.13.5。范围与证据见参考资料，不将普通wheel安装或其它系统称为已支持的数据根入口。
- 启动/依赖变更以真正Git跟踪源码、新路径和独立环境验收；版本同时核对源码、窗口与distribution元数据。缓存下载耗时与GUI启动耗时分开报告。
- 主EXE保持onefile轻量源码启动器，运行时不另附分发目录或捆绑业务依赖；通过Conda使用本项目环境，默认 `msgx`，允许 `MSGX_CONDA_ENV` 显式指定本项目独立环境且拒绝 `base`。Conda从 `CONDA_EXE` 或PATH查找，不硬编码作者安装路径。后台沿用同一实际Python；缺少Conda、指定环境或源码时可见报错，不回退到全局Python或旧 `.venv`。改业务代码无需重复打包；仅启动器或图标资源变化时用 `tools/build_launcher.py` 重建，先备份旧EXE并沿用已保存图标，不直接改onefile而破坏尾部归档。
- 安装/版本或图标任务分别读取 [克隆验证、元数据和图标证据](references/windows.md) 对应章节；窗口、EXE资源和Explorer显示分别确认，单层正确不能替代其它层。
