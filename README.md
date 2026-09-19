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
# 在项目根目录执行
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

也可以直接双击 `启动服务.bat`（内部会设好 Python 路径与 `PYTHONPATH`，不需要你手动配环境）。

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

双击 `查看状态.bat` 也能看。

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

| 操作 | 双击 | 或命令行 |
|---|---|---|
| 启动服务 | `启动服务.bat` | `python -m msgx.main run` |
| 查看状态与花费 | `查看状态.bat` | `python -m msgx.main stats` |
| 立即生成摘要 | — | `python -m msgx.main summary --kind periodic` |
| 生成日报 | — | `python -m msgx.main summary --kind daily` |
| 清理过期数据 | — | `python -m msgx.main prune` |
| 灌注演示数据 | — | `python tools/make_demo.py --post --reset` |

> 命令行方式需要先 `set PYTHONPATH=src`，或执行一次 `pip install -e .`（之后就不用管 PYTHONPATH 了）。
> 双击脚本已经帮你处理好这些。

摘要 HTML 输出在 `data/reports/YYYYMMDD/`，可直接双击用浏览器打开。

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
│   ├── config.py             ← 配置加载与默认值
│   ├── models.py             ← OneBot 事件 → 内部消息模型
│   ├── store.py              ← SQLite 存储
│   ├── onebot.py             ← WebSocket / HTTP 接入层
│   ├── media.py              ← 图片下载、去重、OCR、多模态
│   ├── llm.py                ← 模型调用、预算控制、多模态探测
│   ├── score.py              ← 规则预筛 + 模型复核
│   ├── summarize.py          ← 周期摘要 / 日报 / HTML 渲染
│   ├── notify.py             ← 本机托盘通知
│   ├── pipeline.py           ← 消息总线，串起上面全部环节
│   └── main.py               ← 命令行入口
├── tools/                    ← 辅助脚本（不属于业务代码）
│   ├── make_demo.py          ← 仿真数据生成
│   ├── launcher.cmd          ← 环境探测（找 Python、设 PYTHONPATH）
│   ├── start_napcat.bat      ← NapCat 启动包装器
│   └── check_napcat_env.py   ← NapCat 环境体检（只读）
├── data/                     ← 运行时产物，不入库
│   ├── msgx.db               ← SQLite 数据库
│   ├── media/                ← 下载的图片
│   └── reports/              ← 生成的摘要 HTML
├── config.toml               ← 配置文件（入库）
├── requirements.txt          ← 直接依赖（人工维护）
├── requirements.lock.txt     ← 锁定版本（实测快照，不手工改）
├── .env.example              ← 环境变量模板
├── START_HERE.cmd            ← 菜单入口
├── 启动服务.bat / 查看状态.bat
├── CHANGELOG.md
└── pyproject.toml
```

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

`.gitignore` 已覆盖：`data/`（含真实聊天记录）、`.venv/`、`.env`、`.ui/`、`_install/`。

**提交前务必确认 `data/` 没有被加入** —— 里面有真实群聊内容。

---

## 排障

### 服务起来了，但数据库里一条消息都没有

先看服务端窗口有没有 `协议端已连接`。

- **没有** → NapCat 没连上：检查反向 WS 地址是否为 `/onebot/ws`（不是 `/onebot`），端口是否为 6700
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
