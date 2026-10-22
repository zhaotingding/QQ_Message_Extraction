# Windows克隆、版本与图标

适用：Windows源码克隆与本地轻量启动器；约定更新：2026-10-20。证据路径相对仓库根目录，本地 `data/` 记录不随Git分发。历史验收与性能只描述其日期和测量条件，不代表本轮已验证。

## 权限与安装说明维护时读取

- README负责首次安装、日常入口和简短排障；条件性细节放在 [使用与排障](../../../../docs/使用与排障.md)，历史版本过程由CHANGELOG保存。避免恢复重复价目表、单机速度保证和过时的下载故障断言。
- 2026-10-20静态核对：`conda_runtime.py`、GUI与服务入口没有管理员要求；普通操作需环境、缓存和项目目录可写。`napcat.py`的`_shell_execute_runas()`及定向重启会请求UAC，不能由此要求整个应用预先提权。遇到拒绝访问先核对路径、动作、占用和实际令牌，不把工具沙箱授权当作Windows管理员权限证据。
- 首次NapCat需按上游流程初始化：`restart_napcat_target.ps1`读取已有的`config/webui.json`，界面强制重启不能代替首次安装。配置实际账号的反向WS和`reportSelfMessage: true`；不要用“已连接”代替新消息验收。
- 2026-10-20参数及副作用核对：`check_napcat_env.py --probe`必须带WS/HTTP两个端口，探测会建立短连接且检查输出不能仅凭退出码判断；`make_demo.py --reset`删除目标数据库，`diagnose_freeze.py`会发送探针。日常快速开始不放演示重置或全退QQ命令。
- 上游[Shell安装说明](https://napneko.github.io/guide/boot/Shell)在2026-10-20仍列出OneKey入口；删除旧“必然404”的普遍结论，不把文档链接可访问冒充本项目与上游最新版的端到端兼容验证。
- 2026-10-20文档验收：非管理员Windows令牌（`IsUserAnAdmin=False`）下通过现有Conda环境校验、Tk导入、`pip check`及CLI帮助；18段PowerShell静态语法、48个本地链接/锚点、Skill元数据校验通过。`--probe 6700 6701`实际参数解析通过，注册表和网络操作使用替身；临时Conda目录变量已通过只读`config --show`确认，不创建新环境。本轮未重装、重启生产服务、重新扫码或进行付费验证。记录：`data/diagnostics/readme-20261020/验收记录.md`；不声称原生Skill调用验收通过。

## 安装与版本验证时读取

用户要求亲自安装时，由用户确认环境名与父目录并执行创建、安装和启动。用同一PowerShell中明确设置的 `CONDA_ENVS_PATH` 选择父目录，核对 `sys.executable`、`sys.prefix`、`CONDA_PREFIX`、依赖位置和 `msgx.__file__`，不能只看环境名。源码换目录后需在新根目录更新editable关联；仅切换工作目录不会修复旧映射。首次空数据验收与原位置升级保留分开，具体步骤复用README与使用指南；Agent静态／隔离测试与用户手动验收分别记录。

2026-10-20经用户追加授权，Agent在指定的 `D:\Dev\Anaconda\envs\msgx` 创建环境并按唯一依赖声明安装。该父目录普通权限创建失败，实际UAC提权后完成；未修改目录ACL。回到普通Windows令牌后，解释器/前缀/依赖与源码位置、禁用用户site、Tk和 `pip check` 通过，配置/反馈/GUI/服务恢复四组隔离回归退出码均为0。此结果只适用于该目录的实际权限，不要求日常管理员运行；新克隆手动安装、生产GUI/EXE、后台与QQ业务仍待用户验收。证据：`data/diagnostics/manual-install-20261020/执行记录.md`、`final-checks.yaml`、`test-results.yaml`。Skill元数据校验通过不等于原生调用验证。

选择此前不存在的中文/空格路径，执行本地或已授权来源的Git克隆；不复制原 `.venv/.ui/data`。清除继承的 `MSGX_ROOT/MSGX_PREFS/PYTHONPATH` 和模型密钥环境，在新目录的PowerShell使用本项目独立Conda环境。尚无环境时执行 `conda create -n msgx python=3.13 pip tk`，再执行 `conda run -n msgx --no-capture-output python -s -m pip --isolated install --no-user -e .`。不向公共 `base`、全局Python或用户site-packages安装，不再创建venv。

核对 `sys.executable`、`sys.prefix`、`CONDA_PREFIX`、模块位置与用户site-packages禁用状态；安装、Tk和隔离服务检查均用 `conda run -n msgx --no-capture-output python -s ...`，依赖一致性使用其 `-m pip --isolated check`。GUI、服务与状态分别运行 `-m msgx.main gui`、`-m msgx.main run`、`-m msgx.main stats`。主EXE默认 `msgx`，仅允许 `MSGX_CONDA_ENV` 指向本项目独立环境，拒绝 `base`；Conda只通过 `CONDA_EXE` 或PATH发现（可由condabin定位同安装的Scripts/conda.exe），不硬编码机器路径。EXE与后台须实测使用同一环境，缺少环境时可见失败，不使用全局Python或旧 `.venv`。

使用自定义项目环境名（如 `msgx-dev`）时，须在所有源码、测试、构建和EXE入口执行前统一设置 `$env:MSGX_CONDA_ENV = 'msgx-dev'`，并将命令中的 `-n msgx` 同步替换为 `-n msgx-dev`。PowerShell的进程环境变量不会传给已经打开的Explorer；用自定义环境启动EXE时，应在设置了该变量的同一PowerShell中执行 `& .\QQ_Message_Extraction.exe`。

自定义 `envs_dirs` 时，`CONDA_DEFAULT_ENV` 可为环境绝对路径，不能只与环境短名作字符串比较；还须结合实际 `sys.prefix`、`CONDA_PREFIX` 和所选环境归属核对，不能接受另一目录的同名环境。2026-10-19已用 `tests/test_startup.py` 的 `conda_runtime` 用例及本机Conda源码静态检查确认此边界。

源码 `__version__`、`DISPLAY_VERSION` 与 `importlib.metadata.version('msgx')` 需一致。源码已更新不保证旧editable元数据同步；确需更新且依赖已满足时，可在本仓库执行 `conda run -n msgx --no-capture-output python -s -m pip --isolated install --no-user --no-index --no-deps --no-build-isolation -e .`，仅更新本项目，不借此升级第三方包。

默认入口验收要实际打开窗口并正常关闭。Conda包装PID可能与实际Python PID不同；观察器限定本次Conda/Python进程树与精确克隆入口路径，核对实际解释器、登记PID/创建时间或进程句柄。仅按命令行文本查找会把包含该文本的PowerShell查询自身计入残留，已在旧验收中确认并修正。

- 2026-10-15 历史venv流程的实际Git克隆和独立安装、版本/Tk/默认窗口/正常退出：`data/diagnostics/v0.9.9/clone/验收记录.md`、`clone-verification.yaml`。使用现有pip缓存，不能据此许诺新机器首次下载速度；临时克隆已清理。该记录不替代当前Conda链路验收。
- 2026-10-13 历史venv流程的实际独立依赖安装、OCR、wheel和构建验证：`data/diagnostics/dependency-singlefile-20261013/verification.json`。不等于当前Conda链路或所有Python发行版和硬件已测。
- 2026-10-19 Windows/独立Conda环境 `msgx` / Python3.13.16：依赖安装与 `pip check` 通过、用户site禁用；中文空格隔离源码目录中的PowerShell GUI和主EXE分别实际启动后台，健康检查、正常停止和退出通过；缺失环境显示原生错误弹窗并退出1。使用凭据替身，GUI/服务探测阻断外网，付费请求0。完整35组回归通过，真实凭据实写仍为默认跳过。证据：`data/diagnostics/powershell-20261019/conda-verification.yaml`、`conda-all-tests.log`；该隔离验收不替代生产切换验证，旧环境清理另行核验。
- 2026-10-19 生产切换后核对：新主EXE启动的GUI和后台实际解释器同属独立Conda环境 `msgx`，接收端口归属该后台，健康接口正常且协议已连接。证据：`data/diagnostics/powershell-20261019/production-migration.yaml`、同目录 `验收记录.md`。该检查描述本次恢复后的运行状态，不证明旧环境清理已完成。

## 图标修改或构建时读取

ICO原样保存作者提供的尺寸帧；PNG各尺寸直接由原图采样，避免从已缩小帧再次放大。运行窗口使用 `iconphoto(False, ...)`。导入后保存本地副本，升级保留 `.ui`，不能让新版本回到默认图标。

`tools/build_launcher.py` 从偏好或 `.ui/app-icon.ico` 选图标、备份旧EXE并重建。窗口与任务栏、EXE资源像素、Explorer目录图标分别验证。历史上资源正确仍曾显示旧大图，发送 `SHCNE_ASSOCCHANGED` 并刷新目录后解决；无需删除系统缓存或重启Explorer。

- 2026-10-09 实际Explorer显示：`data/diagnostics/v0.9.4/explorer-after.png`。
- 2026-10-13 新启动器隔离运行及16/32帧验证：`data/diagnostics/dependency-singlefile-20261013/launcher-verification.json`。
- 2026-10-15 导入帧与实际窗口句柄检查：`tests/test_gui_feedback.py`、`test_icon_windows.py`；实际测试记录见 `data/diagnostics/v0.9.9/ui/`。资源像素正确不单独证明当前Explorer显示已更新。
- 2026-10-19 重建onefile的项目模块仅含 `msgx` 初始化及 `msgx.conda_runtime`，未打包aiohttp、NumPy、Pillow等业务依赖；10项图标资源与已备份旧EXE逐项一致。证据：`data/diagnostics/powershell-20261019/artifact-verification.yaml`。该资源验证不替代Explorer显示检查。
