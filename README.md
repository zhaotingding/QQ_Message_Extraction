# 消息萃取（msgx）

在主页查看和筛选 QQ 群消息、阅读图片 OCR。可按需启用日报、周报和月报，也可选一个技术热点，将分散在多份日报里的进展汇成持续更新的主题报。
群发消息需先预览、逐群勾选并确认。生成报告、更新提示词或调用图片分析时，相应材料会发送给你配置的 API 服务商。

**适用：Windows 10 / 11 x64。** 日常用户使用安装版。开发者可从源码运行，沿用项目专用 Conda 环境。

当前源码版本 **1.10.0**。这一版支持 Windows 轻量在线安装、手动增量主题报和按原币统计的 API 账单。[完整更新日志](CHANGELOG.md)

| 你现在要做什么 | 从这里开始 |
|---|---|
| 安装或更新桌面应用 | [安装版](#安装版) |
| 从源码开发、测试 | [源码快速开始](#快速开始) |
| 启动或更新源码版 | [日常启动](#日常启动) · [更新项目](#更新项目) |
| 亲自核对安装与升级是否成功 | [两条手动验收路线](docs/使用与排障.md#手动验收与恢复个人设置) |
| 接入自己的 QQ 群 | [接入 QQ 与 NapCat](#接入-qq-与-napcat) |
| 报权限错误、不收消息、摘要失败 | [常见问题](#常见问题) |
| 改代码、跑测试、构建 EXE | [参与开发](#参与开发) |
| 查功能细节、配置或日志 | [使用与排障](docs/使用与排障.md) |

## 安装版

打开对应版本的 **Assets**，下载并双击 `Setup.exe`。它会取得并校验主程序，然后让你选择安装目录和 QQ 接收组件。要收消息就勾选组件，只读历史可跳过，以后也能补装。较早的 Release 如果只提供版本化安装程序，下载 `msgx-setup-<版本>-x64.exe` 即可。[GitHub Releases](https://github.com/zhaotingding/QQ_Message_Extraction/releases)

1. 双击 Windows x64 安装包，为当前用户安装。安装向导默认显示原程序目录或新装默认位置，也可以点「浏览」选择新的或空的程序目录。手动换目录时，确认后由原卸载器移除旧程序，再安装到新位置，个人资料留在原地。程序和业务运行时随包提供，无需先安装 Conda、Node.js 或 Git。
2. 选装 QQ 接收组件后，向导会将 NapCat 和 NTQQ 放入所选程序目录的 `NapCat/`。首次打开选「开始扫码登录」，用手机 QQ 确认。在主页看到目标账号收到的一条新群消息，即已验证接收成功。程序会记住账号，已有的外部 NapCat 配置继续生效。只收消息可留空 API 密钥。
3. 应用原地沿用已有资料。首次从已有资料的源码仓库 `installer/` 下运行安装包时，可自动识别该仓库。没有原资料的用户默认使用 `%LOCALAPPDATA%\msgx\profile`。见 [资料位置与复用](docs/使用与排障.md#安装版资料位置与复用)。

如果想把安装后的程序和源码放在同一个项目里，在目录页选择项目根下的 `installed/`。例如项目在 `D:\Dev\MCP\消息萃取-MCP`，程序便装到 `D:\Dev\MCP\消息萃取-MCP\installed`。该子目录不入库，既有资料继续在原处读写。

安装版启动约 10 秒后、运行期间每 6 小时，以及点击「帮助 → 检查更新」时，查询 GitHub 最新稳定 Release。客户端先用内置公钥验证更新清单，发现更高版本时显示蓝点。选择「更新并重启」后才下载、核对安装包 SHA-512，再保存编辑、停止接收服务并原位升级。签名用于更新清单；未使用 Windows 代码签名证书时，首次安装仍可能显示“未知发布者”。

安装、卸载和更新实现统一在 [installer/](installer/)；新安装包输出到 `installer/dist/release/<版本>/`，隔离验收包在 `installer/dist/test/<版本>/`。已有 `.ui/installer-build/` 成品保留原位。测试包只用于本机更新验收，不作为正式发行包。

卸载时先正常关闭目标应用，再在 Windows「设置 → 应用 → 已安装的应用」选择「消息萃取」。仓库用户也可先运行 `./installer/uninstall.ps1 -WhatIf` 核对目标，再运行 `./installer/uninstall.ps1` 并确认。该入口只调用当前用户注册的卸载器，保留个人资料；测试实例需显式加 `-TestBuild`。详见 [卸载说明](docs/使用与排障.md#卸载安装版)。

## 先看权限要求
源码版在普通 PowerShell 中运行。创建环境和安装依赖需要目标目录的写权限。

| 操作 | 使用什么权限 | 需要注意什么 |
|---|---|---|
| 创建 Conda 环境、安装或更新依赖 | 目录可写时用普通 PowerShell；受保护目录需有写权限的窗口 | 不要用向 `base` 或用户 site 安装来绕过权限问题 |
| 打开界面、运行接收服务、测试 | 普通 PowerShell | 项目目录及配置的数据目录必须可写 |
| 构建主 EXE | 普通 PowerShell | 先关闭当前主 EXE，确保输出文件可替换 |
| 安装 QQ / Conda | 取决于安装方式 | 安装器如请求管理员权限，按其提示处理 |
| 内置协议端启动或重启 | 普通 Windows 用户 | 点击绿色圆形 ⏻ 电源键启动，点击红色电源键停止；火箭按钮重启协议端。旧式外部 Shell 重启仍可能请求 UAC |

`PermissionError`、`Access is denied` 或 `[WinError 5]` **不能单凭错误名称判断原因**。
先看报错指向的是环境目录、项目文件、被占用的 EXE，还是提权的 QQ 进程；处理方式见
[权限错误](docs/使用与排障.md#权限错误)。

## 快速开始

以下步骤适用于源码版。

### 1. 环境要求
- Windows 10 / 11，64 位。
- 自行安装 Miniconda 或 Anaconda，并准备 Git（克隆仓库时使用）。
- Python 推荐 **3.13**，最低 **3.12**；下一步会把它安装进独立 Conda 环境。
- 首次安装桌面依赖需要 **Node.js 22.12 或更新版本**。日常由项目中安装的 Electron 运行界面。
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
npm.cmd ci --prefix desktop
npm.cmd run setup-runtime --prefix desktop
conda run -n msgx --no-capture-output python -s -m msgx.main gui
```

前两条安装桌面依赖并生成带项目名称和默认图标的 Electron 界面程序。首次安装及桌面依赖锁文件变化后，先关闭项目窗口再执行这两条命令。
**成功标志**是出现「消息萃取」窗口，左栏可以切换主页、实时消息、运行日志、用量明细、摘要阅读和设置。
干净安装先只核对“待配置”状态，不点击启动服务；不复制旧 `.ui/` 或 `data/`。
没有 API 密钥也能打开界面；同一 Windows 用户的新克隆仍可能读到已有凭据，不能据此认定密钥已隔离。

## 接入 QQ 与 NapCat

安装版使用内置组件时，只需在首次设置中扫码；也可以稍后用主页的绿色开关启动。以下步骤适用于源码版，或主动选用外部 NapCat Shell 的用户。

### 1. 配置已有的外部协议端

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

设置保存在资料根的 `.ui/preferences.yaml`。具体偏好位置显示在设置页。
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

在应用“主页”点击 **启动服务**。
这会启动接收及后台任务；旧数据或配置存在时可能补摘要、取回模型响应、重试图片下载和清理过期数据，先读[恢复说明](docs/使用与排障.md#手动验收与恢复个人设置)。

- 已登录且运行中的 NapCat 会按连接设置接入本服务。
- 需要重新启动并扫码时，再点击 **强制重启**。确认目标账号后，允许 UAC 授权并完成手机扫码。
- **成功标志**是服务运行、协议端已连接，且主页消息列表出现目标账号收到的新群消息。

“已连接”只证明通信链路建立，不单独证明 QQ 登录和消息接收成功。
若不收消息，按[接收排障](docs/使用与排障.md#服务运行但没有新消息)逐层检查，避免连续点击重启。

## 日常启动

**执行位置：仓库根目录；权限：普通 PowerShell。** 已安装后只需：

```powershell
$env:CONDA_ENVS_PATH = 'D:\Dev\Anaconda\envs' # 使用首次安装时确认的父目录
$env:MSGX_CONDA_ENV = 'msgx'
conda run -n msgx --no-capture-output python -s -m msgx.main gui
```

已有设置随原目录读取。首次填写或修改接收设置时，先点击“保存设置”，再到主页点击“启动服务”。

**也可以使用仓库里的轻量 EXE：** Conda 能从系统 PATH 或 `CONDA_EXE` 找到，且默认 `msgx` 指向目标环境时，
直接双击仓库根目录的 `QQ_Message_Extraction.exe`。桌面使用快捷方式，不移动 EXE。
若环境名或目录只在当前 PowerShell 中临时指定，从同一窗口执行 `& .\QQ_Message_Extraction.exe`；
这些临时变量不会传给已打开的资源管理器，不能仅凭双击成功认定用了指定位置的环境。
EXE 从完整仓库调用项目 Conda 环境和已安装的 Electron。需要对照旧界面时，可用 `conda run -n msgx --no-capture-output python -s -m msgx.main gui --legacy-tk`。同一资料目录的接收服务只使用一个窗口启动。

| 页面 | 主要用途 |
|---|---|
| 主页 | 启停服务，中央浏览最近消息，右侧查看接收状态、费用和摘要进度 |
| 运行日志 | 查看服务日志和协议端日志，支持切换、刷新与打开目录 |
| 实时消息 | 默认打开群活跃度，也可看与我相关、关键词命中或预览群发 |
| 设置 | 左侧搜索或选择 QQ 登录与连接、群组与消息过滤、模型与 API 密钥、日报与摘要、图片识别、外观与文件 |
| 用量明细 | 按 API 和模型查看 Token 与支出，美元、人民币各自汇总，按实际调用日期显示柱状图 |
| 摘要阅读 | 阅读日报、周报和月报，保存批注或朗读。切换到「主题报」可按关键词手动生成、更新 |

消息列表中，单击群名或发送者会高亮同一标识，再点一次应用筛选。群和发送者可同时筛选，点「清除筛选」恢复全部消息。首页单行显示预览，点正文可看全文。群发间隔默认 30 秒，可在预览前修改。

**停止接收。** 在主页点击红色「停止服务」，等开关变回绿色。随包启动的协议进程一起退出，外部 QQ / NapCat 独立运行。
服务未运行时不会生成自动摘要，但仍可查看已经保存的历史内容。

### 可选：开启模型摘要

在“设置 → API 配置”填写参数，点击“保存 API 参数”；需要另建一套时，点击“以当前参数新增配置”。
输入密钥后单独点击“保存 / 替换密钥”。配置可切换、删除，参数与密钥分别保存；详情见[API 配置管理](docs/使用与排障.md#费用与-api-配置)。
应看到带时间的保存结果和密钥配置状态；**本地保存成功不等于 API 已验证可用**。

- 密钥保存在当前 Windows 用户的凭据管理器；设置页显示配置文件路径、配置标识和系统凭据目标，不会把密钥写入 YAML。
- 新任务统一使用 Responses；选择模型前确认该地址支持 Responses、图片输入及所选服务档位。摘要与提示词更新共用一套选择，图片可独立选择另一套。
- 官方 OpenAI 的费率填写 Standard 基准价；程序对 Flex 按 `0.5` 倍估算，不要手动再减半。该倍率不用于 DeepSeek 等其它平台。
- 官方 Flex 明确返回资源不足时，立即改用 Standard，本次可能按更高费率计费；超时或结果未知不会据此重发。
- 官方 GPT-6.2 Sol 和 Astra 请选 `default`（Standard）。更换摘要档案不会更改已单独选定的图片档案。参见 [OpenAI Flex 适用模型说明](https://developers.openai.com/api/docs/guides/flex-processing)。
- “工作日高峰时段（UTC）”必须填写 UTC 时间；北京时间需减 8 小时后填写，不能原样照抄北京时间时段。保存新费率或时段不会自动重算已记录费用。
- 费率和汇率是可修改的本地估算参数，不是实时价格；实际费用以服务商账单为准。
- 在“设置 → 图片识别”可查看、编辑主题、普通词／正则及每条分数，并本地试匹配；22 项报告主题仅作初值。点击本模块“保存图片分析设置”将参数保存到界面显示的本机 YAML，更新源码保留已保存值，详见[图片规则与个人设置](docs/使用与排障.md#编辑主题规则与保存个人设置)。
- 达阈值后等待 15 分钟并补收同群上下文；发起过 API 图片理解的消息标绿，成功语义供摘要使用，原 OCR 保留。绿色标记不代表生成成功，仍需看状态。
- “设置 → 外观”可切换深色／浅色，立即生效并保存本机选择；没有选择时使用深色，Windows 原生系统对话框仍由系统决定外观。

| 报告 | 计划时间（北京时间） | 使用材料 |
|---|---|---|
| 日报 | 每天 00:00 | 前一天有效消息和选入的图片文字 |
| 周报 | 每周一 00:00 | 上周日报 |
| 月报 | 每月 1 日 00:00 | 上月完整周报及月初、月末的日报 |

生成依赖服务持续运行，完成时间取决于模型响应。重开后可能补做漏期报告并产生费用；
补做范围、失败恢复和批注规则见[摘要说明](docs/使用与排障.md#自动摘要与补生成)。

**手动主题报。** 打开「摘要阅读 → 主题报」，从近 14 天已完成日报提取的最多 4 个候选中选择，或输入自己的技术词。启动接收服务后点击生成。首次汇总历史相关日报，后续把旧稿和新材料一起更新；补生成的早期日报也会纳入。新增或改写的句子显示绿色。查看、选择候选和没有新材料的更新只读取本地资料；确认生成会调用摘要 API。

「设置 → 日报与摘要」可分别修改两套写作提示词。已有个人日报提示词的用户，可点「恢复默认提示词」再保存，选用新版简洁写法。

### 可选：动态提示词与自动图片分析

这两项新增收费功能**默认关闭**，需先选择和保存所需 API，再明确启用。

- **提示词自更新：** 在“自动摘要”中启用，默认每 7 天用摘要 API 分析最近 7 天已完成日报，提炼重复出现的重点及广告特征。结果作为动态补充，保留你的基础提示词；可立即更新、查看带时间的记录、应用旧版本或清空补充。
- **图片分析：** 在“图片本地初筛与独立 API”选择已保存的图片配置，可与摘要使用不同模型。默认本地重要度阈值 70，每天最多自动分析 20 张、预算 1 元，自动候选限最近 7 天；成功语义入库后纳入摘要。
- 初筛使用已有 OCR、有限群聊上下文和规则评分，**不是额外的本地视觉模型**；会占用 CPU／内存，也可能漏判或误判。表情包直接过滤，广告降权、同图复用、调用次数与估算预算共同限制上传；请求结果未知时不自动重发。
- 图片预算是按已配置费率计算的本地限制，不是服务商硬封顶，也不自动核验订阅赠送额度。先用保守阈值观察本地评分，再决定是否开启上传。

“群活跃度”只展示当前账号及已选择统计的群，点击群可看趋势；清理或未接收期间的缺数据不等于真实零活跃。
“只保留当前账号的群”可用于隐藏已退出群，历史消息与报告保留。操作细节见[使用与排障](docs/使用与排障.md#实时消息与过滤)。

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
如果 `desktop/package-lock.json` 更新，执行“打开界面”前两条 npm 命令更新桌面依赖。
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

在仓库根目录用普通 PowerShell 运行实际 Electron 验收。它打开临时窗口、通过独立端口启动接收服务，使用合成数据库和模拟群发。

```powershell
conda run -n msgx --no-capture-output node tests/electron_smoke.cjs
```

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
构建优先使用个人图标，否则使用仓库的 [默认图标](assets/app-icon.ico)。窗口与 EXE 共用选择规则；
别人克隆后无需复制你的 `.ui/` 也有默认图标，个人仍可在设置中更换。构建产物不进入版本库。

### 构建 Windows 安装包

**协作分工。** 开发者按功能提交、测试并提 PR。负责人审查后 Squash 合入 `main`，决定何时发布，合并 PR 不会自动发版。发版前在 PR 中同步源码版本和 CHANGELOG。协作者用公钥构建，用独立测试密钥验收，正式私钥只由发布者保管。

**更新签名密钥。** 仓库随附客户端使用的 Ed25519 公钥，协作者克隆后即可构建。正式签名由发布者在保存配对私钥的机器上执行，需使用项目 Conda 环境中的 OpenSSL 3。更换发布机器时，从离线备份恢复原加密私钥。下方命令默认它位于 `%LOCALAPPDATA%\msgx-release-signing\update-private.pem`。[发布公钥](installer/update-public.pem)

**每次发布。** 在已审核的 `main` 对应提交、仓库根目录的普通 PowerShell 中操作。先确认项目 Conda 环境已安装 `.[build]`、Node 满足桌面要求，使用已入库的发布公钥。构建成功会产生 EXE、同名 `.blockmap` 和 `latest.yml`。失败时提供失败命令及日志末尾。

构建机会校验固定的 [NapCat Windows x64 组件](https://github.com/XingYunStar/NapCatQQ/releases/tag/v4.18.42)，将原始 ZIP 单独输出为 Release 附件。安装器先检查同目录附件或先前校验的组件缓存，缺少时从当前版本的 Release 下载，校验和解压完成才替换旧程序。NapCat 归档附带非商用许可；公开再分发时另核对 NTQQ 组件授权。

```powershell
conda run -n msgx --no-capture-output python -s installer/build_backend.py
if ($LASTEXITCODE) { throw '后台构建失败' }
npm.cmd ci --prefix desktop
if ($LASTEXITCODE) { throw '桌面依赖安装失败' }
npm.cmd run build:installer --prefix desktop
if ($LASTEXITCODE) { throw '安装包构建失败' }
```

**核对并签名。** 在仓库根目录的普通 PowerShell 中，按当前源码版本选择构建目录。签名工具核对清单、EXE 和包内公钥，成功后生成 `latest.yml.sig`。OpenSSL 会在本机提示输入密钥口令。遇到错误时提供报错文字，私钥和口令由发布者保管。

```powershell
$key = Join-Path $env:LOCALAPPDATA 'msgx-release-signing/update-private.pem'
$releaseVersion = conda run -n msgx --no-capture-output python -s -c "import msgx; print(msgx.__version__)"
if ($LASTEXITCODE) { throw '读取项目版本失败' }
$releaseDir = Join-Path 'installer/dist/release' $releaseVersion
conda run -n msgx --no-capture-output node installer/sign-release.mjs $releaseDir $key
```

**在 GitHub 发布。** 新建稳定 Release，标签对应打包的提交和源码版本。上传 `Setup.exe`、版本化主程序 EXE、同名 `.exe.blockmap`、`latest.yml`、`latest.yml.sig` 以及 `msgx-napcat-v4.18.42-x64.zip` 六个附件。新用户只需下载 `Setup.exe`；离线安装可将入口、主程序和组件 ZIP 放在同一目录。版本化主程序 EXE 仍供已安装客户端验签更新。开发构建不要替换同版本的已发布附件。

本地安装向导验收可在最后一条命令添加 `-- --local-preview`，输出到 `installer/dist/release/<版本>-project-local/`，包名带 `unsigned`，沿用同一安装标识，在线更新关闭。隔离的双版本更新验证见 [安装版与公开更新](.agents/skills/msgx-windows-release/references/windows.md#安装版与公开更新)。

### 文件分工

| 位置 | 用途 |
|---|---|
| [src/msgx/](src/msgx/) | Python 业务、进程控制与 Tk 对照界面 |
| [desktop/](desktop/) | Electron 界面、进程通信与桌面依赖锁文件 |
| [installer/](installer/) | 安装构建、NSIS 扩展、卸载入口及更新实现；`dist/` 为忽略的构建产物 |
| [tests/](tests/) | 隔离测试 |
| [tools/](tools/) | 构建、诊断和协议端辅助实现 |
| [pyproject.toml](pyproject.toml) | Python 依赖声明和包配置 |
| [config.yaml](config.yaml) | 团队共享默认配置 |
| [assets/](assets/) | 随源码分发的默认应用图标 |
| `.ui/` | 可选本机主配置、界面偏好、图标、启动器备份；不提交 |
| `data/` | 聊天数据库、图片、报告、日志；不提交 |

应用配置用 YAML，说明用 Markdown；Python 打包、Git、编辑器和外部协议保留各自标准格式。
`.gitignore` 管理哪些文件不提交，`.gitattributes` 管理文件字节与换行，作用不同。

开发约定见 [重构开发规范](docs/重构开发规范.md)，项目 Skill 按 [AGENTS.md](AGENTS.md) 的索引选读。
版本唯一来源是 [src/msgx/__init__.py](src/msgx/__init__.py)，历史变更见 [CHANGELOG.md](CHANGELOG.md)。
提交前检查差异，不提交个人路径、账号、密钥或聊天数据；本地提交与远程 push 分别处理。

## 验证范围与许可

1.0.3 完整 **42 组隔离回归通过**，安全审查及统计缓存收尾后相关 **9 组复验通过**，包含19项群操作、31项实时Tk和19项活跃度测试；已核对5张合成界面截图，并完成20万条合成消息查询测量。未执行真实群发、退群或付费API；这些外部操作须在应用中针对实际目标确认，不能把模拟测试当作生产验收。

1.0.1 在项目 Conda 环境通过完整 **39 组隔离回归**，包含勾选样式、API 配置、图片延迟队列及 Flex 回退；真实图片筛选准确率、服务商请求和生产 QQ 仍须另行验收。

2026-10-31 的 1.0.0 完整 38 组隔离回归通过，随后补充的提示词后台 ID 自动恢复已通过相关三组复验。
新图片链路使用合成图片与模拟 API 验证筛选、独立配置、限额、去重和恢复；规则在真实群聊中的准确率仍需实际观察。
没有以本次测试代替真实模型收费、QQ 登录或生产资料恢复验收。

2026-10-19 已在 Windows / 独立 Conda / Python 3.13.16 下验证安装、中文空格路径克隆、
源码 GUI、轻量 EXE、后台服务和 35 组回归；真实凭据实写测试默认跳过。
这不代表所有 QQ / NapCat 新版本或其它操作系统均已验证。
2026-10-20 在指定的新 Conda 目录完成安装与依赖检查，普通权限下通过配置、界面反馈、GUI 和服务恢复四组回归；
新环境的真实业务和用户手动验收另行确认。2026-10-22 归并版本说明并核对文档，不代表本日重新运行了全部业务测试。详细边界见
[项目发布参考](.agents/skills/msgx-windows-release/references/windows.md)。

2026-10-31 默认图标选择及实际 Windows 窗口像素测试 6 项、既有图标导入反馈测试 2 项通过；
本次未以真实付费调用或生产 QQ 会话验证新功能效果。

项目元数据中的许可为 `Proprietary`；仓库访问权限不等于开放源代码许可。
