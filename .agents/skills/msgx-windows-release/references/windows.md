# Windows克隆、版本与图标

适用：Windows 安装版、源码版与本地轻量启动器。证据路径相对仓库根目录，本地 `data/` 记录不随 Git 分发。历史验收与性能仅描述各日期和对应测量条件。

## 安装版与公开更新

2026-11-23 开发分支的在线安装流程使用 `Setup.exe` 和独立 NapCat ZIP。版本化主程序 EXE、blockmap、latest.yml 和 latest.yml.sig 保持旧客户端的更新协议。构建、附件清单见 README。已用回环 HTTP 验证独立入口下载、坏组件拒绝、缓存复用、无 QQ 组件安装及保留选择的覆盖升级。已发布 1.0.8 的四附件流程仍是历史版本的行为。

安装实现统一在 `installer/`：`build_backend.py` 和 `backend_entry.py` 构建随包后台，`build-installer.mjs` 和 `installer.nsh` 构建 NSIS，`update.cjs` 与 `installed-profile.cjs` 由源码桌面与安装包共用。依赖仍从 `desktop/package.json` 解析。`uninstall.ps1` 只调用当前用户注册的目标卸载器，先用 `-WhatIf` 验证目标；测试实例必须加 `-TestBuild`。旧构建成品不因目录整理删除或搬走。

用户于 2026-11-15 批准独立安装版。Windows x64 NSIS 为当前用户部署 Electron 和业务运行时。开发者继续沿用 Conda 源码链路。程序默认资料根为 `%LOCALAPPDATA%\msgx\profile`。如需沿用源码版资料，先退出原应用，再用安装版的 `--msgx-data-root` 指向原资料根。程序记住选择并原地使用资料。该参数不负责复制、迁移或改写 SQLite 内的文件路径。安装、更新和卸载保留资料。密钥沿用当前 Windows 用户的凭据管理器，此段描述 1.0.7 及以前的外部协议端模式。

1.0.8 的构建脚本 `installer/portable-protocol.mjs` 固定 v4.18.42 Windows.Node 归档及 SHA-256，使用选定目录的 `NapCat/`。原上游已停止发布，此二进制来源是社区留档。版权、原上游和归档来源随组件提供；NapCat 许可限制商用，NTQQ 和 Node 各自保留权利。

2026-11-23 本地验收：从 1.0.8 交付 EXE 解出组件，704 个上游文件逐一比对一致，随包 Node 在仅含系统目录的 PATH 下加载了 NTQQ 原生库。独立安装身份在中文空格路径完成新装及覆盖，原模拟账号和消息库校验值一致。在隔离副本中替换协议入口测试扫码、OneBot 连接、新消息入库、电源启停及火箭重启，真实腾讯登录留给笔记本验收。回归入口为 `tests/test_connection.py`、`tests/test_managed_napcat.py` 和 `tests/electron_smoke.cjs`。

自 1.0.7，正式更新以安装包内发布公钥验证 latest.yml.sig，严格识别同一版本的 Windows x64 EXE 后按清单 SHA-512 下载。发布工作流、四个附件和首次密钥配置见 [README](../../../../README.md#构建-windows-安装包)。公钥可提交，正式私钥由发布者保存。Windows Authenticode 可选。

手动安装显示程序目录页，接受新建或空目录。覆盖安装默认显示已注册的原路径。换目录时先验证目标写权限、原卸载记录及新旧程序目录与实际资料目录的关系，再确认迁移。源码仓库兼作资料根时，允许它直属的专用 `installed/`，不入 Git。卸载只管理程序目录，数据库、图片和报告不可设在其中。NSIS 复用旧卸载器保留资料，卸载失败不得在新目录解包。静默更新仍沿用原路径。资料先复用已记住的根或默认目录中的已有资料，再考虑从仓库 `installer/` 下启动安装包时可识别的源码资料。手动安装的完成页可以直接打开基础设置，自动更新重启进入工作台。基础设置复用现有主题和凭据接口，留空保持原密钥。使用 `npm.cmd run build:installer --prefix desktop -- --local-preview` 生成同安装身份的未签名本地验收包，输出到 `installer/dist/release/<版本>-project-local/`，该包关闭在线更新。

实际验证使用隔离资料根，核对安装程序及后台解释器归属、设置和数据库保留、系统卸载入口、更新重启后的版本及收尾。记录本地源验证和公开 Release 验证的各自结果。用户步骤见 [README 安装版](../../../../README.md#安装版) 和 [安装版资料使用指南](../../../../docs/使用与排障.md#安装版资料位置与复用)。

使用独立测试密钥验证新链路。先构建后台并安装桌面依赖，为下面的 `$testPub`、`$testKey` 指定一对临时 Ed25519 公私钥路径。使用空闲的测试版本目录。

```powershell
npm.cmd run build:installer --prefix desktop -- --test-feed http://127.0.0.1:17391/ --test-version 1.0.5 --test-public-key $testPub
npm.cmd run build:installer --prefix desktop -- --test-feed http://127.0.0.1:17391/ --test-version 1.0.6 --test-public-key $testPub
node installer/sign-release.mjs installer/dist/test/1.0.6 $testKey
conda run -n msgx --no-capture-output python -s -m http.server 17391 --bind 127.0.0.1 --directory installer/dist/test/1.0.6
```

安装低版本测试包，在回环更新源提供带签名的高版本。核对应用内提示、下载校验、重启版本及原资料读回。缺失签名、签名损坏、使用不同私钥时禁止交给安装器。历次旧版验收记录仅适用于当时实现。

2026-11-23 已用临时 Ed25519 密钥和独立 Windows 安装标识验证 91.0.1 → 91.0.2。伪签名触发零次安装包请求；有效签名配错误文件时下载校验拒绝安装，原窗口继续工作；正确附件经用户动作触发原位升级，新进程使用同一隔离资料根，标记文件保留。正式构建模式用测试密钥验证四个产物。此记录不涉及个人发布密钥或 GitHub 公开投放。持续回归入口为 `tests/test_desktop_update.cjs`。

2026-11-15 实际验收（Windows x64，回环更新源）：1.0.8 在检测到 1.0.9 时显示蓝点，点击前未下载安装包。点击更新后接收服务以退出码 0 关停，原位置的 1.0.9 自动重开并读回显式选择的资料根、QQ 设置、历史摘要和筛选偏好。卸载后安装文件和注册项移除，隔离数据库与偏好文件 SHA-256 不变。测试安装包在本机 `.ui/installer-build/test/`，无正式签名及公开发布验证。

2026-11-15 目录整理后，以 `installer/dist/test/` 的 1.0.10 和 1.0.11 复验回环升级。隔离后台就绪后，应用显示蓝点，确认才下载，正常退出并在原位置重开 1.0.11。新版注册项经 `uninstall.ps1 -TestBuild -WhatIf` 核对，实际卸载移除主程序与注册项，隔离数据库和两份配置的 SHA-256 保持一致。本机日志在 `.ui/installer-layout-20261115-194755/`。实测 NSIS 卸载项的 `DisplayName` 带版本号，主程序路径取 `DisplayIcon`。脚本执行前拒绝同名进程仍在运行的情况，避免 NSIS 回退按名称终止进程，这一保护已通过卸载入口的替身测试。正式签名及 GitHub 更新未验收。

安装向导本地验收见 `installer/dist/wizard-verification/in-place.json`。隔离的 1.0.12 装在 A，1.0.13 带 `/D=B` 覆盖后只有 A 的 EXE 和唯一注册项。NSIS 会先创建显式 `/D` 的空目录，实际文件仍写入注册位置。资料选择测试及隔离窗口验证覆盖已有主题、档案回显、局部保存和首次设置状态。正式身份的交互安装留给使用者确认。

目录页与首次设置的后续验收见 `installer/dist/wizard-feedback/nsis-pages.log` 和 `open-setup.log`。对隔离安装器 1.0.14 操作了实际控件，确认新装可选尚不存在的非默认目录，覆盖安装显示原地址且浏览禁用，完成页可见且默认勾选基础设置。完成页启动参数在隔离应用中验证能打开基础设置，即使已有完成标记。比较目录时使用 Win32 `GetFullPathNameW`，NSIS 的同名指令会因新目录尚不存在而返回空值。

2026-11-16 交互迁移验收见 `installer/dist/relocation-verification/interactive.log`。对隔离 1.0.15 包验证了覆盖时原路径预填、浏览可用、迁移确认、A 到 B 迁移。旧主程序已移除，Windows 只保留 B 的安装记录，开始菜单快捷方式指向 B，合成偏好、数据库和资料路径指针的哈希不变。`tests/test_installer_relocation.ps1` 单独验证目录、旧卸载登记和资料保护。验收使用隔离身份，正式身份的安装留给使用者。

2026-11-16 项目内安装验收见 `installer/dist/project-verification/interactive.log`。隔离 1.0.16 从外部 A 原位资料迁入合成源码项目的 `installed/`，注册与快捷方式只留新地址。实际随包后台从项目根读回原主题。卸载只移除 `installed/` 内程序，合成源码仍在，偏好、数据库和资料路径指针哈希不变。正式身份留给用户验收。

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

1.0.0 的公共默认图标为 Git 管理的 `assets/app-icon.ico`；`app_icon.selected_icon()` 是窗口与 `tools/build_launcher.py` 共用的选择入口，顺序为个人有效路径、偏好旁的本机图标副本、公共默认资源。默认资源按源码位置定位，不受个人 `MSGX_ROOT` 影响。`.ui/` 继续忽略，新克隆无需复制维护者个人偏好即可用默认图标，用户仍可自行更换。

构建会备份旧EXE并重建。窗口与任务栏、EXE资源像素、Explorer目录图标分别验证。历史上资源正确仍曾显示旧大图，发送 `SHCNE_ASSOCCHANGED` 并刷新目录后解决；无需删除系统缓存或重启Explorer。

- 2026-10-31：默认图标原样复制本机已选ICO，逐字节相同，保留16/20/24/32/40/48/64/128/256九种尺寸与透明度。`tests/test_icon_windows.py` 6项通过，包含无个人.ui的默认选择、个人覆盖、构建选择一致性与实际Windows大小窗口像素；既有自定义图标导入反馈2项通过。测试在沙箱外普通Windows令牌下运行，以绕过沙箱对Python新建0700临时目录的访问限制；未启动生产应用或覆盖旧EXE。上述选择器与窗口测试不等于本次新EXE资源和Explorer显示已验收。

- 2026-10-09 实际Explorer显示：`data/diagnostics/v0.9.4/explorer-after.png`。
- 2026-10-13 新启动器隔离运行及16/32帧验证：`data/diagnostics/dependency-singlefile-20261013/launcher-verification.json`。
- 2026-10-15 导入帧与实际窗口句柄检查：`tests/test_gui_feedback.py`、`test_icon_windows.py`；实际测试记录见 `data/diagnostics/v0.9.9/ui/`。资源像素正确不单独证明当前Explorer显示已更新。
- 2026-10-19 重建onefile的项目模块仅含 `msgx` 初始化及 `msgx.conda_runtime`，未打包aiohttp、NumPy、Pillow等业务依赖；10项图标资源与已备份旧EXE逐项一致。证据：`data/diagnostics/powershell-20261019/artifact-verification.yaml`。该资源验证不替代Explorer显示检查。
