# 消息萃取（msgx）

把指定的 QQ 群变成一份**可检索的知识流**。

实时接收 → 结构化落库 → 图片 OCR / 多模态理解 → 价值打分与分级 → 周期摘要 → 本机弹窗提醒。

> **这个项目不会发出任何消息。** 它只读不写，从设计上就去掉了「被风控」和「打扰群友」两条路径。

## 它解决什么问题

技术群里的有效信息密度很低：一天几百上千条，真正值得看的可能只有几条，但它们和闲聊、表情包、接龙混在一起。
事后想找「上周有人提到的那个显存工具叫啥」时，聊天记录已经刷得翻不到了。

本项目的做法是：

1. **全量落库**，一条不丢 —— 之后可按群、按时间、按关键词检索
2. **图片也要能搜** —— 报错截图、架构图经 OCR / 多模态转成文字入库
3. **分级打扰** —— 只有「现在不知道就会造成损失」的消息（被点名、有人回答了你之前的提问、限时机会）才强提醒；
   高价值但可以稍后看的进摘要；其余归档不打扰
4. **周期摘要** —— 每 N 分钟把这段时间的群聊压成结构化要点，丢弃寒暄与情绪输出，保留链接、命令、参数原文

---

## 架构

```
QQ 群
  │  （你在群里，机器人小号也在群里）
  ▼
官方 QQ 客户端（NTQQ）
  │  NapCat 注入 QQ 进程，把消息转成 OneBot 11 协议
  ▼
NapCat ──反向 WebSocket──► 本服务 :6700/onebot/ws
                              │
                              ├─► 归一化 + 去重 ──► SQLite（WAL）
                              │
                              ├─► 图片下载 ──► OCR / 多模态 ──┐
                              │                              │
                              ├─► 规则预筛 ──► 模型复核 ◄────┘
                              │        │
                              │        ├─ P0：本机托盘强提醒
                              │        └─ P1/P2：入库待摘要
                              │
                              └─► 周期摘要 / 日报 ──► HTML + 托盘通知
```

**为什么用独立的机器人小号？** 因为协议端需要注入 QQ 客户端，用主号有风险。小号只用于接收（本项目从不发消息），
主号留在群里正常使用，通过 `[watch] at_qq` 把主号加进「被点名」集合 —— 这样群里 @ 主号时才会触发强提醒。

---

## 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Windows 10 / 11 | 通知走 Windows 托盘。核心逻辑跨平台，通知后端需另写 |
| Python | **>= 3.11** | 用到标准库 `tomllib`（3.10 及以下没有） |
| QQ（NTQQ） | **buildVersion >= 40768** | NapCat 的硬性要求。实测 `9.9.33-52230` 可用 |
| NapCat | `NapCat.Shell.zip` | 见下方安装步骤。**不要**用 OneKey 包 |
| DeepSeek API Key | 可选 | 不配也能跑：退化为「仅规则模式」，不生成摘要、不做模型复核 |

依赖只有 4 个直接包（`aiohttp` / `pillow` / `winotify` / `rapidocr-onnxruntime`），全部支持 pip 直接安装，
不需要 paddle、torch、Node.js。

---

## 快速开始

### 1. 准备环境

```powershell
# 在项目根目录执行。"py -3.13" 换成你那个带 tkinter 的解释器
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
```

`requirements.lock.txt` 是一份**实测跑通的环境快照**（22 个包，含传递依赖）。
不要用 `requirements.txt` 复现环境 —— 它只声明直接依赖，版本范围较宽。

> 如果 pip 装不上，先试默认源（很多情况下本地缓存里就有全部 wheel）；
> 换国内镜像出问题时优先怀疑镜像本身，而不是网络。

### 2. 配置 API Key

```powershell
# 方式一：环境变量（推荐，重启终端后生效）
setx DEEPSEEK_API_KEY "sk-你的key"

# 方式二：复制 .env.example 为 .env 并填写（需自行加载该文件）
```

不配置也能启动，只是不会生成摘要。

### 3. 启动服务

**日常用法（推荐）**：双击项目根目录的 **`消息萃取.exe`** —— 会打开图形界面，点「启动服务」即可。
不需要记命令，也不需要先配环境变量。

命令行方式（排查问题时用）：

```powershell
# 在项目根目录执行
.venv\Scripts\python.exe -m msgx.main run
```

看到这样的横幅就是成功了：

```
消息萃取服务已启动
  NapCat 反向 WS  : ws://127.0.0.1:6700/onebot/ws
  OneBot HTTP 上报: http://127.0.0.1:6701/onebot
  健康检查         : http://127.0.0.1:6701/health
```

> **不要重复启动。** 端口 6700 已被占用时，第二次启动会失败并提示
> 「端口无法绑定 —— 最常见的原因：已经有一个服务实例在运行」。
> 判断方法：浏览器打开 `http://127.0.0.1:6701/health`，能返回 JSON 就说明服务已在运行。

### 4. 接上 QQ（见下一节）

---

## 安装 QQ 与 NapCat

这一步是整套流程里最容易卡住的地方，请按顺序来。

### 4.1 安装 QQ（NTQQ）

```powershell
winget install --id Tencent.QQ.NT -e
```

装完后确认版本：

```powershell
winget list --id Tencent.QQ.NT
```

也可从官网下载：<https://im.qq.com/pcqq/index.shtml>

> **注意**：`winget` 不一定装到默认目录（实测此机器装在了 `D:\App\QQ\`）。
> 不要猜路径，用 `winget list` 看版本即可；NapCat 会自己从注册表找 QQ。

### 4.2 下载 NapCat

从 GitHub Releases 下载 **`NapCat.Shell.zip`**：

<https://github.com/NapNeko/NapCatQQ/releases/latest>

| 包名 | 大小 | 说明 |
|---|---|---|
| **`NapCat.Shell.zip`** | ~29 MB | ✅ **用这个**。不需要额外装 Node.js |
| `NapCat.Shell.Windows.Node.zip` | ~117 MB | 自带 Node.js，没有 Node 环境时用 |
| `NapCat.Framework.zip` | ~30 MB | 需要另装 QQ 且走另一套注入方式 |
| `NapCat.Shell.Windows.OneKey.zip` | ~1 MB | ❌ **不要用**。这是个下载器，内部硬编码的下载域名已失效，必然报 404 |

解压到任意目录，例如 `D:\App\NapCat\`。解压后必须能看到这些文件：

```
NapCatWinBootMain.exe     ← 启动器实际调用的是它
NapCatWinBootHook.dll
launcher.bat
napcat.mjs
qqnt.json
```

如果缺文件，说明解压不完整，重新解压一次。

### 4.3 启动 NapCat

> ⚠️ **不要直接右键 `launcher.bat` 选「以管理员身份运行」。**
>
> `launcher.bat` 全程用 `%cd%`（当前工作目录）而不是 `%~dp0`（脚本所在目录）来推导路径。
> 右键管理员运行时，工作目录是 `C:\Windows\System32`，于是它去找
> `System32\NapCatWinBootMain.exe`，找不到就退出 —— 表现就是**窗口一闪而过**。

正确做法是先从注册表读出 QQ 的安装位置，再切到 NapCat 目录启动。本仓库提供了现成的包装脚本：

```cmd
tools\start_napcat.bat
```

它会：修正工作目录 → 自提权 → 自检关键文件 → 调用 `launcher.bat` → 出错时保留窗口以便截图。

启动成功后：

- 会弹出 QQ 登录界面 / 二维码（**用小号扫码登录**）
- WebUI 地址：<http://127.0.0.1:6099/webui/>
  登录 Token 在启动日志里，形如 `[WebUi] Login Token is xxxxxxxx`

### 4.4 让 NapCat 连上本服务

**推荐：预置配置，登录后自动连上，不用手点 WebUI。**

编辑 `NapCat目录\config\onebot11.json`（若文件不存在就新建；账号首次登录时 NapCat 会用它作全局模板）：

```json
{
  "network": {
    "httpServers": [],
    "httpSseServers": [],
    "httpClients": [],
    "websocketServers": [],
    "websocketClients": [
      {
        "name": "msgx",
        "enable": true,
        "url": "ws://127.0.0.1:6700/onebot/ws",
        "messagePostFormat": "array",
        "reportSelfMessage": false,
        "reconnectInterval": 5000,
        "token": "",
        "debug": false,
        "heartInterval": 30000,
        "verifyCertificate": true
      }
    ],
    "plugins": []
  },
  "musicSignUrl": "",
  "enableLocalFile2Url": false,
  "parseMultMsg": false,
  "imageDownloadProxy": "",
  "timeout": {
    "baseTimeout": 10000,
    "uploadSpeedKBps": 256,
    "downloadSpeedKBps": 256,
    "maxTimeout": 1800000
  }
}
```

> **路径必须是 `/onebot/ws`，不能写成 `/onebot`。**
> 本服务的 `/onebot/ws` 是 WebSocket 端点，`/onebot` 是 HTTP POST 上报端点。
> 写错会在第一次握手就失败（实测返回 HTTP 405）。

连上后，服务端窗口会出现：

```
INFO  msgx.onebot   协议端已连接: 127.0.0.1
```

### 4.5 验证

```powershell
# 查看运行状态、今日花费、已收消息数
.venv\Scripts\python.exe -m msgx.main stats

# 探测当前模型是否支持图片输入
.venv\Scripts\python.exe -m msgx.main vision --force
```

双击 `tools\查看状态.bat` 也能看，或者直接看图形界面「运行状态」页。

---

## 配置

所有配置在 `config.toml`，改完重启服务生效。**没有配置的项会用内置默认值**，所以你可以只写需要覆盖的部分。

### 最常改的几项

```toml
[watch]
# 留空 = 处理机器人小号加入的全部群
groups = []
# 你的主号 QQ —— 群里 @ 你时才会触发强提醒
at_qq = ["你的主号QQ"]
# 群号 → 群名（群消息事件不带群名，补上便于阅读）
[watch.names]
123456789 = "某个技术群"

[summary]
interval_minutes = 30      # 多久生成一次摘要

[score]
keywords = ["招聘", "内推", "开源", "踩坑", "论文", "数据集", "复现"]
p0_min_score = 8           # 强提醒门槛，先设高避免误报疲劳
llm_review_min_score = 2   # 规则分达到多少才交给模型复核（直接决定 API 成本）
```

> `[watch] at_qq` **一定要填**。监听用的是小号，别人不会 @ 它；
> 不填的话「被点名」这条硬规则永远不会触发。

### 成本

按实测口径（DeepSeek off-peak：输入 $0.22/M tok、输出 $0.66/M tok）：

- 一次 26 条消息的摘要 ≈ ¥0.003
- 单条消息的模型复核 ≈ ¥0.0014
- 5~10 个群、日均 3000~10000 条消息 ≈ **¥3/天**

`max_daily_cost_cny` 默认 10 元，超出后自动降级为「仅规则」模式，不会失控。

**务必注意**：该模型默认开启思考模式，思考 token 与正文共用 `max_tokens` 且按输出价计费。
本项目对「摘要、图片描述」这类忠实提取任务已显式关闭思考（`think=False`），
实测同一任务从 1500 token 降到 34 token —— 约 **9 倍**差距。改动 `llm.py` 时不要把这个开关去掉。

---

## 日常使用

**主程序只有一个：双击项目根目录的 `消息萃取.exe`。** 图形界面里有启停、状态、日志和全部设置。
下面这些是排查问题或做运维时用的备用入口（都在 `tools\` 里）。

| 操作 | 双击 | 或命令行 |
|---|---|---|
| **主程序（开图形界面）** | **`消息萃取.exe`** | `python -m msgx.main gui` |
| 备用：开图形界面 | `tools\START_HERE.cmd`、`tools\START_HERE.vbs` | `python -m msgx.main gui` |
| 备用：命令行菜单 | `tools\START_HERE.cmd --cli` | — |
| 备用：只跑服务（无界面） | `tools\启动服务.bat` | `python -m msgx.main run` |
| 备用：查看状态与花费 | `tools\查看状态.bat` | `python -m msgx.main stats` |
| 立即生成摘要 | — | `python -m msgx.main summary --kind full` |
| 备用：增量周期摘要 | — | `python -m msgx.main summary --kind periodic` |
| 生成日报 | — | `python -m msgx.main summary --kind daily` |
| 清理过期数据 | — | `python -m msgx.main prune` |
| 灌注演示数据 | — | `python tools/make_demo.py --post --reset` |
| 跑测试 | — | `python tests/run_all.py` |

> 命令行方式需要先 `set PYTHONPATH=src`，或执行一次 `pip install -e .`（之后就不用管 PYTHONPATH 了）。
> 双击脚本已经帮你处理好这些。

摘要 HTML 输出在 `data/reports/YYYYMMDD/`，可直接双击用浏览器打开。

---

## 图形界面

双击项目根目录的 **`消息萃取.exe`** 即可，全程没有黑色命令行窗口。

> `消息萃取.exe` 是个「启动器」：体积约 10MB，只负责找到项目里的 `.venv` 并拉起界面。
> 依赖（onnxruntime / rapidocr 等）仍在 `.venv` 里，**所以改 `src/` 下的代码后不需要重新打包**。
> 重新打包的命令见本节末尾。

### 运行状态

- **状态卡片**：服务进程、协议端（NapCat）连接、已入库条数、今日花费、模型状态
  - 「协议端」一项会明确区分 *服务在跑* 和 *NapCat 已连上* —— 这两件事经常被混淆，
    是排查「为什么没有消息」时最先要看的地方。未连接时会直接提示点哪个按钮
- **按钮**：启动服务 / 停止服务 / 立即生成摘要 / 启动协议端(NapCat) / 打开数据目录
  - **「立即生成摘要」是全量情报总结**：把最近 24 小时所有群的全部消息交给模型，
    先提炼「今日重点」（≤5 条）再按话题归纳 —— 与周期摘要的增量窗口互不影响。
    只要点的时候 24h 内有入库消息，就一定有产出
  - **「启动协议端(NapCat)」** 一键拉起 NapCat（QQ 会一并起来），等价于手动双击启动脚本。
    **会弹一次 UAC，必须点「是」** —— 向 QQ 进程注入 DLL 属于管理员操作，绕不过去。
    路径由 `config.toml` 的 `[napcat] start_script` 指定，**留空则该按钮不显示**。
  - **需要登录时，二维码会自动弹出来给你扫**。NapCat 把登录码写到它的
    `cache/qrcode.png` 就完事了，从不负责展示 —— 界面会盯着那个目录，
    出现即弹窗，登录成功后自动关闭。需配置 `[napcat] dir`。
- **日志**：服务进程的实时输出，等同于控制台里看到的内容

> 服务与协议端是**两个独立进程**：本服务只负责监听协议端连入，不负责启动它
> （OneBot 11 反向 WebSocket 的职责划分）。所以「服务在跑」不等于「能收到消息」。

### 跨群热点

每轮周期先出一份**跨群热点**，再出各群摘要，通知也以热点为主。

同一个话题散在多个群里时，逐群摘要永远体现不出「多群共鸣」—— 这是最容易漏掉的信号
（典型例子：某件事在几个群被分别提过，但因为没有汇总，一直没被注意到）。
所以**重复度由确定性统计算出，不交给模型去发现**：

- 候选词：`score.keywords` ∪ 技术术语（环境变量 / 命令 / 包名）∪ 中英 n-gram
- 热度 = **群数 × 人数 × log(1 + 次数)** —— 群数与人数是整数乘法，权重远高于次数，
  **「跨群 + 多人」才是真信号**，单人在单群刷屏不算
- 硬性门槛：至少跨 2 个群 **且** 2 个不同的人
- 同句碎片（「智了」「模型」这类渣滓，支持集覆盖率 ≥80%）并入最热词，不占榜

报告里每条热点都标注「N 群 · M 人 · K 次」，**密度就是排序依据**。
统计部分零成本、零新增依赖（实现见 `src/msgx/hotspot.py`）。

点「停止服务」时不会强杀进程，而是发一个信号文件让服务自己收尾退出 ——
这样不会打断正在写数据库或下载图片的动作。

### 设置

| 界面项 | 作用 | 落到哪里 |
|---|---|---|
| 关注关键词 | 命中即进入候选池，再由模型判定是否值得提醒 | `score.keywords` |
| 摘要间隔 | 每 N 分钟生成一次摘要（0 = 沿用 `config.toml`） | `summary.interval_minutes` |
| 统计哪些群 | 勾选参与统计的群；**不选 = 全部群** | `watch.groups` |
| 不接收的消息类型 | 纯表情 / 纯图片 / 合并转发 / 语音 / 短于 N 字 | `filters` |

**设置保存在 `.ui/preferences.json`（本人本地，不入版本库），界面永远不会改写 `config.toml`。**
这样配置文件的改动归版本管理，各人的偏好互不干扰 —— 多人协作时不会因为谁改了配置而互相覆盖。

规则是「留空 = 沿用 `config.toml`」而不是「清空」：比如关键词留空，服务仍会用配置文件里的关键词。

> ⚠️ **注意**：这些设置只在**从图形界面启动服务**时生效（界面通过环境变量 `MSGX_PREFS` 传给服务进程）。
> 若改用 `tools\启动服务.bat` 或命令行手动启动，服务只读 `config.toml`，**界面里的设置不会生效**。

### 重新打包 `消息萃取.exe`

只在 `tools\exe_main.py` 本身需要改动时才要重打包（业务代码改动不需要）：

```powershell
.venv\Scripts\python.exe -m PyInstaller --onefile --noconsole `
  --name msgx_launcher --distpath . --workpath build\tmp --specpath build `
  --paths src tools\exe_main.py
# 然后把生成的 msgx_launcher.exe 改名为 消息萃取.exe
```

> 「不选任何群」和「选了全部群」效果相同，都表示关注全部群。

---

## 测试

```powershell
.venv\Scripts\python.exe tests\run_all.py
```

三组测试，零额外依赖（不需要 pytest）：

| 测试 | 覆盖内容 |
|---|---|
| `test_filters.py` | 过滤判定与偏好叠加，含「占位符不计入长度」这类关键回归 |
| `test_service.py` | 启动、健康检查、WS 端点契约（含写错路径的反例）、协作退出 |
| `test_gui.py` | 真实构建全部界面控件并用假数据刷新，能查出 `compileall` 查不到的变量绑定错误 |

`test_service.py` 会向系统要一个空闲端口并用临时数据库，**不会碰真实数据，也不会和正在运行的服务抢端口**。

### 不接 QQ 也能试

```powershell
python tools/make_demo.py --post --reset
```

会生成一批仿真群聊消息（含用于 OCR 的合成报错截图），走完整链路。适合改代码后快速验证。

---

## 项目结构

```
消息萃取-MCP/
├── src/msgx/                 ← 全部业务代码
│   ├── __init__.py           ← __version__ 在这里，版本号唯一来源
│   ├── config.py             ← 配置加载、默认值、偏好叠加
│   ├── prefs.py              ← .ui/preferences.json 读写 + 消息过滤判定
│   ├── models.py             ← OneBot 事件 → 内部消息模型
│   ├── store.py              ← SQLite 存储
│   ├── onebot.py             ← WebSocket / HTTP 接入层
│   ├── media.py              ← 图片下载、去重、OCR、多模态
│   ├── llm.py                ← 模型调用、预算控制、多模态探测
│   ├── score.py              ← 规则预筛 + 模型复核
│   ├── summarize.py          ← 周期摘要 / 日报 / HTML 渲染
│   ├── notify.py             ← 本机托盘通知
│   ├── pipeline.py           ← 消息总线，串起上面全部环节
│   ├── gui.py                ← 图形界面（只做交互与进程管理）
│   └── main.py               ← 命令行入口
├── tests/                    ← 测试（零依赖，run_all.py 一键运行）
│   ├── run_all.py
│   ├── test_filters.py
│   ├── test_service.py
│   └── test_gui.py
├── tools/                    ← 辅助脚本与备用入口（不属于业务代码）
│   ├── exe_main.py           ← 主程序「消息萃取.exe」的入口（PyInstaller 冻结它）
│   ├── gui_entry.py          ← 界面启动入口（无控制台，自行兜异常）
│   ├── make_demo.py          ← 仿真数据生成
│   ├── launcher.cmd          ← 环境探测（找 Python、设 PYTHONPATH）
│   ├── start_gui.cmd         ← 用 pythonw 拉起图形界面
│   ├── start_napcat.bat      ← NapCat 启动包装器
│   ├── check_napcat_env.py   ← NapCat 环境体检（只读）
│   ├── START_HERE.cmd/.vbs   ← 备用入口（默认开界面，--cli 进菜单）
│   ├── 启动服务.bat          ← 备用：只跑服务，无界面
│   └── 查看状态.bat          ← 备用：查看状态与用量
├── data/                     ← 运行时产物，不入库
│   ├── msgx.db               ← SQLite 数据库
│   ├── media/                ← 下载的图片
│   └── reports/              ← 生成的摘要 HTML
├── .ui/                      ← 界面本地偏好，不入库
│   └── preferences.json
├── 消息萃取.exe              ← ★ 主程序：双击即打开图形界面
├── config.toml               ← 配置文件（入库，团队共享）
├── requirements.txt          ← 直接依赖（人工维护）
├── requirements.lock.txt     ← 锁定版本（实测快照，不手工改）
├── requirements-dev.txt      ← 只给打包用（PyInstaller），运行时不需要
├── .env.example              ← 环境变量模板
├── CHANGELOG.md
└── pyproject.toml
```

**`消息萃取.exe` 不在版本库里**（它是构建产物，已加入 `.gitignore`）。克隆仓库后按上面「重新打包」一节自行生成即可，或者直接用 `tools\START_HERE.cmd` 打开界面。

**代码为什么放在 `src/`？** 避免测试时把当前目录误当包导入，也让「哪些是业务代码、哪些是脚本」一目了然。
`config.py` 中项目根是**三级上溯**（`src/msgx/config.py` → `src/` → 项目根），少写一层会导致配置读不到、数据库凭空重建。

---

## 协同开发

### 分支与提交

- 主分支 `main`，始终保持可运行
- 功能开发开 `feat/xxx`，修复开 `fix/xxx`
- 提交信息：`<类型>: <中文简述>`（如 `fix: 修复接入队列未被消费导致消息丢失`）
- 合并前请确认 `python -m msgx.main stats` 能正常跑

### 发版

1. 改 `src/msgx/__init__.py` 的 `__version__`
2. 更新 `CHANGELOG.md`
3. `git commit -m "vX.Y.Z: 简述"`
4. `git tag vX.Y.Z`

### 改代码前请先读

- `CHANGELOG.md` 的「修复」一节 —— 里面每一条都是**真实踩过的坑**，且都写了为什么要这么改。
  这些代码看起来可能「多余」，但删掉会重现 bug。
- 尤其注意这几处，它们的注释解释了「为什么不那样写」：
  - `llm.py` 的 `think=False` 与放大重试 —— 关系到成本与空摘要
  - `models.py` 的 `mface` 分支 —— 表情包也带图片 URL
  - `summarize.py` 的 `md_to_html` —— 必须先切片再转义
  - `store.py` 的 `insert_message` —— 必须无条件注册群

### 不要提交

`.gitignore` 已覆盖：`data/`（含真实聊天记录）、`.venv/`、`.env`、`.ui/`、`_install/`、
`消息萃取.exe`（构建产物）与 `build/`（PyInstaller 中间文件）。

**提交前务必确认 `data/` 没有被加入** —— 里面有真实群聊内容。

---

## 排障

### 双击 `消息萃取.exe` 后没有任何反应

多半是当前 Python 解释器没有 tkinter。界面启动失败的原因会写在 `.ui/gui_error.log`，先看它。

```powershell
# 5 秒确认
.venv\Scripts\python.exe -c "import tkinter; print(tkinter.TkVersion)"
```

报 `ModuleNotFoundError` 就换一个带 tkinter 的解释器重建 `.venv`（见「关于 tkinter」一节）。
临时也可以走命令行：`tools\START_HERE.cmd --cli`。

若 `.ui/gui_error.log` 不存在、界面也没出现，说明连 Python 都没拉起来：
检查 `.venv\Scripts\pythonw.exe` 是否存在，以及项目根目录的 `.venv` 是否完整。

### 启动服务时报「端口无法绑定」

**最常见的原因：已经有一个服务实例在运行。** 不需要重复启动。

```powershell
# 能返回 JSON 就说明服务已在运行
curl http://127.0.0.1:6701/health
# 或直接双击 tools\查看状态.bat
```

确认没有实例却仍报错时，用 `netstat -ano | findstr :6700` 看是谁占了端口
（正常情况下只可能是本项目的 Python 进程，或 NapCat 自己的端口冲突）。

### 界面能开，但日志报 `No module named 'msgx'`

`PYTHONPATH` 没设上。双击脚本会自动设置；手动跑命令行时需要先 `set PYTHONPATH=src`，
或在项目根执行一次 `pip install -e .`。

### 界面显示「服务在跑」但「协议端未连接」

这两件事是分开的：服务起来了不等于 NapCat 连上了。

- **NapCat 根本没启动** → 点界面上的「启动协议端(NapCat)」按钮（会弹一次 UAC，点「是」）。
  该按钮的路径来自 `config.toml` 的 `[napcat] start_script`；**看不到这个按钮，就说明那一项是空的**。
- **NapCat 在跑但仍连不上** → 检查它那边的反向 WS 地址是否为
  `ws://127.0.0.1:6700/onebot/ws`（**不是** `/onebot`），端口是否为 6700。

### 服务起来了，但数据库里一条消息都没有

先看服务端窗口有没有 `协议端已连接`。

- **没有** → NapCat 没连上：先点界面上的「启动协议端(NapCat)」；若已启动，
  再检查反向 WS 地址是否为 `/onebot/ws`（不是 `/onebot`），端口是否为 6700
- **有，但没数据** → 检查 `[watch] groups` 是否限制了群号；确认小号已加入目标群

### NapCat 窗口一闪而过

见 §4.3。根因是 `launcher.bat` 用 `%cd%` 推导路径，右键管理员运行时工作目录变成了 `System32`。
用 `tools\start_napcat.bat` 启动。

### 图片全是 `[图片]` 占位，没有文字

```powershell
python -m msgx.main vision --force
```

若返回「不支持」，说明模型不接收图片输入，会降级为本地 OCR。
若 OCR 也没结果，检查 `rapidocr-onnxruntime` 是否为 `1.2.3`（1.3.x 改了返回结构）。

### 日志报 `image file is truncated` / `unsupported image`

**不是 OCR 的问题，是图片下载不完整。** NapCat 的图床偶发半途断流，半张图被写盘后
OCR 和多模态接口会先后报错 —— 两处都只是结果，根因在下载。

> 版本备注：v0.4.0 加了长度校验但**下载路径没修**（aiohttp 的 `read(n)` 是
> 「有数据就立刻返回」，每张图只拿到第一个 chunk），日志表现为恒报
> 「收到 16384 / 应为 N」然后放弃。v0.4.1 起改为读到 EOF + Pillow 校验，
> 服务启动时还会把上次误判 failed 的图片自动重新入队。

程序现在会整包读回再用 Pillow 校验可解码性，**坏图不落盘**。
若仍频繁出现，多半是网络不稳，检查代理或重连。

### 协议端断开后消息停了（日志见「协议端断开」）

QQ 会话掉线 / NapCat 进程退出后，NapCat 不会再自动重连（5s 重连只在它自己活着时有效），
而服务侧毫无异常，唯一征兆是状态卡片「协议端」变回未连接。
v0.4.1 起断开超过 60 秒会弹 P0 气泡提醒；看到提醒后点「启动协议端(NapCat)」即可。

### 界面群列表显示「(未命名) 754842541」

群名之前只能靠 `config.toml` 的 `[watch] names` 手填。v0.4.1 起协议端连上后会自动
调 `get_group_list` 拉真实群名；也可以在界面点「刷新群列表」（需服务与协议端都在线）。

### 摘要全是空的

该模型默认开启思考。如果 `llm.py` 里 `think=False` 被去掉，思考 token 会吃满预算导致正文为空。
另外确认 `[summary] min_messages` 不要设得比实际消息量还高。

### 一键体检

```powershell
python tools/check_napcat_env.py            # 静态检查
python tools/check_napcat_env.py --probe    # 附带端口连通性探测
```

检查项：QQ 安装与版本、NapCat 文件完整性、`onebot11.json` 配置正确性、端口占用。
**只读，不会修改任何东西。**

---

## 许可

私有项目，未开源。
