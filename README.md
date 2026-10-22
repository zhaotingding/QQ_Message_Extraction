# 消息萃取（msgx）

接收 QQ 群消息，在本机检索、查看图片 OCR，并按需调用模型生成日报、周报和月报。
应用不向 QQ 群发送消息；启用模型摘要或手动图片识别时，会把相应材料发送给你配置的 API 服务商。

**适用：Windows 开发者。运行方式：源码 + 一个项目专用 Conda 环境 + 可选的轻量 EXE 启动器。**

当前版本 **0.9.9**：模块职责拆分、查询与界面响应优化、统一 Conda 启动链；完整变更见 [更新日志](CHANGELOG.md)。

| 你现在要做什么 | 从这里开始 |
|---|---|
| 第一次克隆，先把界面打开 | [首次安装](#快速开始) |
| 已装好，只想每天使用 | [日常启动](#日常启动) |
| 更新源码，保留原设置和历史 | [更新项目](#更新项目) |
| 亲自核对安装与升级是否成功 | [两条手动验收路线](docs/使用与排障.md#手动验收与恢复个人设置) |
| 接入自己的 QQ 群 | [接入 QQ 与 NapCat](#接入-qq-与-napcat) |
| 报权限错误、不收消息、摘要失败 | [常见问题](#常见问题) |
| 改代码、跑测试、构建 EXE | [参与开发](#参与开发) |
| 查功能细节、配置或日志 | [使用与排障](docs/使用与排障.md) |


## 先看权限要求
**日常使用普通 PowerShell；创建环境和安装依赖是否需要管理员，取决于选定目录的写权限。**

| 操作 | 使用什么权限 | 需要注意什么 |
|---|---|---|
| 创建 Conda 环境、安装或更新依赖 | 目录可写时用普通 PowerShell；受保护目录需有写权限的窗口 | 不要用向 `base` 或用户 site 安装来绕过权限问题 |
| 打开界面、运行接收服务、测试 | 普通 PowerShell | 项目目录及配置的数据目录必须可写 |
| 构建主 EXE | 普通 PowerShell | 先关闭当前主 EXE，确保输出文件可替换 |
| 安装 QQ / Conda | 取决于安装方式 | 安装器如请求管理员权限，按其提示处理 |
| 启动或强制重启 NapCat 协议端 | **协议端需要提权** | 当前应用会请求 UAC 授权；确认弹窗即可，不必预先把整个应用以管理员身份启动 |

`PermissionError`、`Access is denied` 或 `[WinError 5]` **不能单凭错误名称判断原因**。
先看报错指向的是环境目录、项目文件、被占用的 EXE，还是提权的 QQ 进程；处理方式见
[权限错误](docs/使用与排障.md#权限错误)。

## 快速开始

### 1. 环境要求
- Windows 10 / 11，64 位。
- 自行安装 Miniconda 或 Anaconda，并准备 Git（克隆仓库时使用）。
- Python 推荐 **3.13**，最低 **3.12**；下一步会把它安装进独立 Conda 环境。
- 把仓库放在当前用户可写的开发目录，避免放在需要提权才能写入的位置。

**执行位置：仓库根目录；权限：普通 PowerShell。** 根目录应能看到 `pyproject.toml`、`config.yaml` 和 `src`。
打开该目录中的 PowerShell，先检查：

```powershell
Get-Location
Test-Path .\pyproject.toml
conda --version
```

**成功标志：** 第二条输出 `True`，第三条输出 Conda 版本。
如果提示找不到 `conda`，先按[找不到 Conda](docs/使用与排障.md#找不到-conda)处理。

### 2. 创建独立环境并安装依赖

**执行位置：仓库根目录；首次安装时执行。先确认环境名和完整位置，再运行创建命令。**
以下示例使用 `msgx`、`D:\Dev\Anaconda\envs\msgx`；其他协作者可把父目录改为自己确认的位置。
变量只影响当前窗口及其子进程，不修改全局 Conda 配置；以后新开窗口也要设置相同选择。

**权限：** 选定目录和包缓存可写时用普通 PowerShell。
本机已确认 `D:\Dev\Anaconda\envs` 的普通 `Users` 组仅有读取／执行权限，因此创建、安装和更新依赖应使用有写权限的窗口，通常为**管理员 PowerShell**；打开 GUI 仍用普通窗口。

```powershell
$env:CONDA_ENVS_PATH = 'D:\Dev\Anaconda\envs'
$env:MSGX_CONDA_ENV = 'msgx'
conda env list
Test-Path (Join-Path $env:CONDA_ENVS_PATH 'msgx\conda-meta')
```

**成功标志：** 环境目录是你确认的目标。已有有效的项目环境时，跳过下一条 `create`；存在不明目录或同名旧环境时，先核对，不覆盖或删除它。

```powershell
conda create -n msgx python=3.13 pip tk
conda run -n msgx --no-capture-output python -s -m pip --isolated install --no-user -e .
```

**成功标志：** Conda 创建前列出的 `environment location` 为确认的目标，安装正常结束。
若位置不同，取消创建；若报拒绝访问，先按[权限错误](docs/使用与排障.md#权限错误)检查具体环境／缓存目录。
随后在同一目录和环境选择下检查（普通 PowerShell 可执行）：

```powershell
conda run -n msgx python -s -m pip --isolated check
conda run -n msgx python -B -s -c "import msgx; from pathlib import Path; print('msgx:', msgx.__file__); print('expected source:', Path.cwd() / 'src' / 'msgx' / '__init__.py')"
```

应看到 `No broken requirements found.`，且 `msgx` 与 `expected source` 指向当前仓库同一文件。
这两条命令检查依赖兼容性和源码关联；发现旧源码路径时，先确认环境归属，再在当前仓库重新执行 `-e .` 安装。

- 依赖唯一声明在 [pyproject.toml](pyproject.toml)，`-e .` 让后续源码修改直接生效。
- 不向 `base`、全局 Python 或用户级目录安装项目依赖；不创建 `.venv`。
- 每次命令都用 `conda run -n msgx` 指定环境，无需先 `conda activate`。
- `-s` 禁用用户级包导入；pip 的 `--isolated`、`--no-user` 用于保持安装路径明确。
- 若使用其他环境名，先读[自定义环境名](docs/使用与排障.md#自定义环境名)，所有入口都要一致。

### 3. 打开界面

**执行位置：仓库根目录；权限：普通 PowerShell。**
如果刚才使用管理员窗口安装，现在新开普通窗口，进入仓库并重新设置前述两个环境变量。

```powershell
conda run -n msgx --no-capture-output python -s -m msgx.main gui
```

**成功标志：** 出现“消息萃取”窗口，可浏览设置。此时还不等于已经接入 QQ。
干净安装先只核对“待配置”状态，不点击启动服务；不复制旧 `.ui/` 或 `data/`。
没有 API 密钥也能打开界面；同一 Windows 用户的新克隆仍可能读到已有凭据，不能据此认定密钥已隔离。

## 接入 QQ 与 NapCat

### 1. 完成协议端的首次安装

先安装 QQ，再按 [NapCat 官方 Shell 安装说明](https://napneko.github.io/guide/boot/Shell)
完成 Windows Shell 的解压、首次启动和登录。下载入口见 [官方 Releases](https://github.com/NapNeko/NapCatQQ/releases/latest)。
QQ 与 NapCat 的版本兼容关系以相应发行说明为准，本项目不自动安装或升级它们。

**权限：NapCat 启动涉及提权，出现 UAC 时确认授权。** 首次初始化完成后应有 NapCat 的 `config/webui.json`，
并能打开其 WebUI。本项目的“强制重启”用于已初始化的协议端，不能代替这一步。

当前集成预检要求所选 NapCat 目录包含：

```text
NapCatWinBootMain.exe
NapCatWinBootHook.dll
launcher.bat
napcat.mjs
qqnt.json
```

这是本项目当前支持的目录结构，不是对所有未来 NapCat 安装方式的承诺。
已在使用协议端的用户可直接复用现有安装，不要再启动一份。

### 2. 在应用中保存本机设置

打开“设置”，填写并保存：

| 字段 | 填什么 |
|---|---|
| 目标 QQ 号 | 实际由 NapCat 登录、用于接收消息的账号 |
| 本机 NapCat 目录 | 上一步初始化完成的 Shell 目录 |
| QQ.exe 路径（可选） | 自动定位失败时再指定 |
| 统计哪些群 | 选择要关注的群；不选表示全部群 |

设置保存在本机 `.ui/preferences.yaml`，不会改写仓库的共享 `config.yaml`。
需要自定义端口、OneBot Token 或数据路径时，用可选的 [`.ui/config.local.yaml`](docs/使用与排障.md#配置保存位置)，不修改共享文件。
群列表尚未出现时，先完成连接，再点“刷新群列表”。

### 3. 配置反向 WebSocket

在 NapCat WebUI 的 OneBot 网络设置中，为**实际登录的账号**创建或检查一条反向 WebSocket 客户端连接：

| 项目 | 默认配置 |
|---|---|
| 启用连接 | 开启 |
| 连接地址 | `ws://127.0.0.1:6700/onebot/ws` |
| 消息格式 | `array` |
| 上报自身消息 | 开启，配置字段为 `reportSelfMessage: true` |
| Token | 与本项目 `onebot.access_token` 一致；默认双方为空 |

端口改过时，双方一起修改。`/onebot/ws` 是 WebSocket 地址，不能写成 HTTP 上报端点 `/onebot`。
已有连接只修改原配置，不重复添加。NapCat 配置仍使用其自身要求的 JSON 格式。

### 4. 启动接收并确认成功

在应用“运行状态”页点击 **启动服务**。
这会启动接收及后台任务；旧数据或配置存在时可能补摘要、取回模型响应、重试图片下载和清理过期数据，先读[恢复说明](docs/使用与排障.md#手动验收与恢复个人设置)。

- 已登录且运行中的 NapCat 会按连接设置接入本服务。
- 需要重新启动并扫码时，再点击 **强制重启**。确认目标账号后，允许 UAC 授权并完成手机扫码。
- **成功标志：** 服务运行、协议端已连接，并且“实时消息”出现目标账号收到的新群消息。

“已连接”只证明通信链路建立，不单独证明 QQ 登录和消息接收成功。
若不收消息，按[接收排障](docs/使用与排障.md#服务运行但没有新消息)逐层检查，避免连续点击重启。

## 日常启动

**执行位置：仓库根目录；权限：普通 PowerShell。** 已安装后只需：

```powershell
$env:CONDA_ENVS_PATH = 'D:\Dev\Anaconda\envs' # 使用首次安装时确认的父目录
$env:MSGX_CONDA_ENV = 'msgx'
conda run -n msgx --no-capture-output python -s -m msgx.main gui
```

无需重新创建环境或填写设置。然后按需点击“启动服务”。

**也可以使用已有的主 EXE：** Conda 能从系统 PATH 或 `CONDA_EXE` 找到，且默认 `msgx` 指向目标环境时，
直接双击仓库根目录的 `QQ_Message_Extraction.exe`。桌面使用快捷方式，不移动 EXE。
若环境名或目录只在当前 PowerShell 中临时指定，从同一窗口执行 `& .\QQ_Message_Extraction.exe`；
这些临时变量不会传给已打开的资源管理器，不能仅凭双击成功认定用了指定位置的环境。
EXE 依赖完整仓库源码和 Conda 环境，不能作为独立安装包复制使用；新克隆没有 EXE 是正常的，可直接运行源码或按下文构建。

| 页面 | 主要用途 |
|---|---|
| 运行状态 | 启停服务、查看协议连接、自动摘要进度和日志 |
| 实时消息 | 按群查看消息和 OCR，手动识别图片，查看活跃度、与我相关及关键词消息 |
| 设置 | API、过滤规则、目标 QQ、NapCat 路径、自定义图标 |
| 消费明细 | 每次请求的 Token、费用估算、结果及消费趋势 |
| 摘要全文 | 阅读日报／周报／月报，保存阅读状态、重点和心得 |

**停止接收：** 点击“停止服务”，等待完成提示；这与退出 QQ / NapCat 是不同的操作。
服务未运行时不会生成自动摘要，但仍可查看已经保存的历史内容。

### 可选：开启模型摘要

在“设置 → API 配置”选择服务商，填写模型与参数，输入密钥后点击“保存配置 / 替换密钥”。
应看到带时间的保存结果和密钥配置状态；**本地保存成功不等于 API 已验证可用**。

- 密钥保存在当前 Windows 用户的凭据管理器，不要写入共享配置或提交到 Git。
- 选择模型前确认该服务商支持相应接口、图片输入及所选服务档位。
- 官方 OpenAI 的费率填写 Standard 基准价；程序对 Flex 按 `0.5` 倍估算，不要手动再减半。该倍率不用于 DeepSeek 等其它平台。
- “工作日高峰时段（UTC）”必须填写 UTC 时间；北京时间需减 8 小时后填写，不能原样照抄北京时间时段。保存新费率或时段不会自动重算已记录费用。
- 费率和汇率是可修改的本地估算参数，不是实时价格；实际费用以服务商账单为准。
- 图片默认做本地 OCR，只有手动点击“识别”才调用图片模型。“纳入摘要”是独立选项。

| 报告 | 计划时间（北京时间） | 使用材料 |
|---|---|---|
| 日报 | 每天 00:00 | 前一天有效消息和选入的图片文字 |
| 周报 | 每周一 00:00 | 上周日报 |
| 月报 | 每月 1 日 00:00 | 上月完整周报及月初、月末的日报 |

生成依赖服务持续运行，完成时间取决于模型响应。重开后可能补做漏期报告并产生费用；
补做范围、失败恢复和批注规则见[摘要说明](docs/使用与排障.md#自动摘要与补生成)。

## 更新项目

复用原环境、原仓库和原数据位置；保留 `.ui/`、`data/`、凭据和 NapCat 安装。不要为了更新删除环境。
由你正常停止接收服务并关闭 GUI 后更新源码，再重新打开；启动器实现或图标不变时无需重建 EXE。
先保留自己的 Git 改动，不用强制重置覆盖它们。

**执行位置：更新后的仓库根目录；权限：环境可写的窗口。** 只有依赖声明变化或源码目录变化时重新安装：

```powershell
conda run -n msgx --no-capture-output python -s -m pip --isolated install --no-user -e .
conda run -n msgx --no-capture-output python -s -m pip --isolated check
```

前提是已设置首次安装时的环境选择；成功后按日常方式启动，核对原设置和历史。
跨目录复制数据库不属于普通升级，库内绝对路径不会自动改写；恢复清单及验收见[手动验收与恢复个人设置](docs/使用与排障.md#手动验收与恢复个人设置)。

## 常见问题

先按下面的现象找入口，不要把所有问题都归因于权限。

| 现象 | 先检查什么 |
|---|---|
| 找不到 `conda` | Conda 是否已安装、当前 PowerShell 是否能找到它；[处理方法](docs/使用与排障.md#找不到-conda) |
| 环境不存在或提示当前环境不符 | `conda env list`，以及环境名与 `MSGX_CONDA_ENV` 是否一致 |
| 拒绝访问、`PermissionError`、`WinError 5` | 报错路径、目录可写性、文件占用；[权限处理](docs/使用与排障.md#权限错误) |
| 双击 EXE 没打开界面 | `.ui/exe_error.log`、`.ui/gui_error.log`，再用源码命令查看错误 |
| 端口无法绑定 | 是否已有本项目服务占用端口；不要再启动第二个实例 |
| 服务运行但没消息 | 目标账号、NapCat 连接、QQ 登录、新事件和过滤规则；[接收排障](docs/使用与排障.md#服务运行但没有新消息) |
| 日报“已停止重试” | 先看具体错误和用量，再决定是否“确认补生成”；[摘要恢复](docs/使用与排障.md#摘要失败) |
| 消费估算与平台账单不同 | 对比 Token、币种、单价、缓存及倍率；高峰时段字段按 UTC 判断，平台扣费与本地估算分别核对 |
| 图片只显示占位符 | 查看下载和 OCR 日志；[图片排障](docs/使用与排障.md#图片与-ocr) |

排障时提供：**执行的命令、完整错误文字、是否管理员窗口、报错涉及的路径**。
不要提供 API 密钥、登录令牌或私人聊天正文。

## 参与开发

### 测试与修改

**执行位置：仓库根目录；权限：普通 PowerShell；需已安装依赖并设置上述环境选择。**

```powershell
conda run -n msgx --no-capture-output python -s tests\run_all.py
```

套件清单由 [tests/run_all.py](tests/run_all.py) 维护，使用隔离数据与模拟 API。
真实凭据实写测试默认跳过。文档修改不需要反复执行全部业务测试；
代码修改先运行相关测试，再按影响范围决定是否完整回归。

修改 `src/` 后重新打开应用并重启接收服务，以加载新代码；普通业务修改无需重建 EXE。
涉及环境和个人设置的变化按[更新项目](#更新项目)处理。

### 构建可选主 EXE

**执行位置：仓库根目录；先设置上述环境选择。** 安装构建依赖需要环境写权限，构建本身可用普通 PowerShell。
先正常关闭当前主程序，避免 EXE 被占用。

```powershell
conda run -n msgx --no-capture-output python -s -m pip --isolated install --no-user -e ".[build]"
conda run -n msgx --no-capture-output python -s tools\build_launcher.py
```

**成功标志：** 根目录生成 `QQ_Message_Extraction.exe`，构建命令正常结束。
它只通过 Conda 运行源码，不附带业务依赖目录。已有 EXE 会先备份到 `.ui/launcher-backups/`，
构建沿用本机保存的图标。构建产物不进入版本库。

### 文件分工

| 位置 | 用途 |
|---|---|
| [src/msgx/](src/msgx/) | 业务代码、界面和进程控制 |
| [tests/](tests/) | 隔离测试 |
| [tools/](tools/) | 构建、诊断和协议端辅助实现 |
| [pyproject.toml](pyproject.toml) | 唯一依赖声明和 Python 包配置 |
| [config.yaml](config.yaml) | 团队共享默认配置 |
| `.ui/` | 可选本机主配置、界面偏好、图标、启动器备份；不提交 |
| `data/` | 聊天数据库、图片、报告、日志；不提交 |

应用配置用 YAML，说明用 Markdown；Python 打包、Git、编辑器和外部协议保留各自标准格式。
`.gitignore` 管理哪些文件不提交，`.gitattributes` 管理文件字节与换行，作用不同。

开发约定见 [重构开发规范](docs/重构开发规范.md)，项目 Skill 按 [AGENTS.md](AGENTS.md) 的索引选读。
版本唯一来源是 [src/msgx/__init__.py](src/msgx/__init__.py)，历史变更见 [CHANGELOG.md](CHANGELOG.md)。
提交前检查差异，不提交个人路径、账号、密钥或聊天数据；本地提交与远程 push 分别处理。

## 验证范围与许可

2026-10-19 已在 Windows / 独立 Conda / Python 3.13.16 下验证安装、中文空格路径克隆、
源码 GUI、轻量 EXE、后台服务和 35 组回归；真实凭据实写测试默认跳过。
这不代表所有 QQ / NapCat 新版本或其它操作系统均已验证。
2026-10-20 在指定的新 Conda 目录完成安装与依赖检查，普通权限下通过配置、界面反馈、GUI 和服务恢复四组回归；
新环境的真实业务和用户手动验收另行确认。2026-10-22 归并版本说明并核对文档，不代表本日重新运行了全部业务测试。详细边界见
[项目发布参考](.agents/skills/msgx-windows-release/references/windows.md)。

项目元数据中的许可为 `Proprietary`；仓库访问权限不等于开放源代码许可。
