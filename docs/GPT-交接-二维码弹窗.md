# 交给 GPT 的完整交接提示词：修复「重启协议端后二维码图片窗口不弹出」

> 历史交接记录：以下任务和代码位置属于当时版本，不代表当前待办。当前约束见
> [项目维护 Skill](../.agents/skills/msgx-maintenance/SKILL.md)；旧诊断工具和本地证据的位置见 [开发文件归档清单](开发文件归档清单.md)。

> 用法：把本文件全文作为提示词发给 GPT（或任何 AI），让它按「待查方向」继续修。
> 本文档自包含：项目背景、环境、已做过的全部工作、证据、代码位置、约束、验收标准。

---

## 0. 你的任务（一句话）

修复本项目的 Tkinter 界面 bug：用户点击「重启协议端」按钮后，NapCat 确实在重新登录并
把二维码写到了 `D:\App\NapCat\cache\qrcode.png`（文件确实在更新），**但界面从不弹出
二维码图片窗口**。用户因此反复点击按钮，造成多实例并存的次生灾害。这个问题已经迭代
两版（v0.5.5、v0.5.6）都没修好，**15:20 用户又试了两次，仍然没弹**。

---

## 1. 项目背景（自包含，不需要外部上下文）

**项目**：`消息萃取-MCP`（代码根目录 `D:\Dev\MCP\消息萃取-MCP`）
QQ 群消息萃取服务：NapCatQQ（注入桌面 QQ 的 OneBot 11 协议端）→ 反向 WebSocket →
本服务（aiohttp）接收 → SQLite 落库 → 图片 OCR/视觉理解（DeepSeek）→ 打分 → 摘要 →
Windows 托盘通知。**绝不自动应答**（降风控）。

**界面**：Tkinter GUI（`src/msgx/gui.py`，约 1660 行），功能包括启停服务、启动/重启
协议端（NapCat）、日志查看（服务日志/协议端日志下拉切换、按行上色）、实时消息页、
摘要生成、计费显示。监听 NapCat 的登录二维码文件并在检测到新码时弹出图片窗口。

**进程链**（重要，决定「改代码要不要重新打包」）：

```
消息萃取.exe（约 8MB，PyInstaller --onefile --noconsole 的"启动器"，内嵌 python313.dll，
             只做一件事：找到 .venv 并 Popen 子进程；包内没有 tkinter/PIL/msgx，勿往这方面猜）
  └─ Popen: D:\Dev\MCP\消息萃取-MCP\.venv\Scripts\pythonw.exe  tools\gui_entry.py
       └─ sys.path.insert(src) → from msgx.gui import main → App(Tk) → mainloop()
            └─ GUI 再以子进程启动服务（pythonw -m msgx...），通过 HTTP /health 每 2s 轮询
```

⇒ **改 `src/` 下的代码不需要重新打包 exe**，但 **必须完全关闭界面进程并重新双击
exe** 才会加载新代码（旧界面进程继续跑旧代码）。GUI 窗口标题栏显示 `__version__`
（`src/msgx/__init__.py`），可用来确认跑的是哪一版。

**环境**：
- 唯一 Python 环境：项目内 `.venv`，基础解释器是 **Anaconda Python 3.13.5**
  （有 tkinter、PIL.ImageTk —— 两个都已实测 import OK）。
- ⚠️ 系统里另一个受管 Python（`C:\Users\john1\.workbuddy\binaries\python\...3.13.12`）
  是精简构建、**没有 tkinter**，不要用它跑界面测试。
- 跑测试：`cd D:\Dev\MCP\消息萃取-MCP && .venv\Scripts\python.exe tests\run_all.py`
  （当前 9 组 357 项，全绿）。
- NapCat 在 `D:\App\NapCat\`（v4.18.28），QQ 在 `D:\App\QQ\`（QQNT v9.9.33）。
  QQ 数据目录 `D:\App\QQ\QQ聊天消息\Tencent Files\<uin>\nt_qq\nt_db\nt_msg.db-wal`。
- 本机两个 QQ 账号：大号 `1371109209`（勿动）、**监听小号 `1443685390`（扫码要扫这个）**。

**Git**：main 分支，当前版本 **v0.5.6**（commit `0393236`），357 项测试全绿。

---

## 2. 现象与证据时间线（2026-09-21）

| 时间 | 事件 | 证据 |
|---|---|---|
| 10:48 | 最后一次成功收消息（2860 条库最后一秒） | 本库 `MAX(ts)` 与小号 `nt_msg.db-wal` mtime 一致 |
| 11:33:43~11:35:06 | 用户连点 4 次「重启协议端」+ 11:35:13 一次「启动协议端」 | `data/logs/napcat.log`（每次都成功请求提权） |
| 11:37 | 诊断：2 个注入器 / 8 个 QQ.exe / `ws_peers=[]` / **qrcode.png 每 2~7 秒被重写** / 服务 0 连接 | `tools/diagnose_freeze.py` 输出 |
| 12:10 | v0.5.6 修复发布（commit `0393236`） | CHANGELOG.md |
| 15:20:25 / 15:20:58 | 用户又点 2 次重置 —— **仍然没有弹出二维码** | napcat.log 尾部两条 |
| 15:23 | 最新诊断（全文见 `data/_diag.txt`，可运行 `tools/diagnose_freeze.py --no-probe` 复现）：**1 个注入器 / 4 个 QQ.exe（干净）/ 服务 uptime 195s / `ws_peers=[]` `ws_conns=0` / qrcode.png mtime 25 秒前、6 秒内稳定**（单实例写了一次码就停，等扫码） | 同上 |

**核心矛盾**：二维码文件确实被 NapCat 写出来了（15:23 实测 25 秒前），界面却没有任何
弹窗（日志区也没有「已弹出扫码窗口」或「二维码读取失败」的输出——至少用户没看到）。

---

## 3. 已经做过、不要重复的工作

### v0.5.5（commit `6716c0b`）
- 重置协议端改隐藏启动：`ShellExecuteW(..., SW_HIDE)` + 环境变量 `MSGX_RESTART_SILENT=1`
  （跳过 bat 末尾 `pause`，否则隐藏窗口里留看不见的僵尸进程）。→ 已生效（用户确认无黑框）。
- 二维码监视线程加 `fresh_only`（重置只认重置后新写的码）。

### v0.5.6（commit `0393236`）—— 修了三个竞态，但 15:20 实测仍不弹
1. **保护窗** `qr_hold_until`：`_start_qrcode_watch`（每次启动/重置都会调）设
   `now + 120s`；窗口期内 `_apply_health` 不许因「ws_peers 非空」停掉监视线程。
   （理由：冻结形态 =「连着但不推」，点重置瞬间旧连接还挂着，2 秒内就会把刚武装的
   监视线程掐死，而 bat 要 20~60s 后才写新码。）
2. **换代** `qr_gen`：每次 `_start_qrcode_watch` 都换代 +1 并起新线程；旧线程发现
   代数不符自行退出（原来「在跑就不重开」的单例守卫会吞掉 fresh_only 新基线请求）。
3. **展示成功才推进基线** `qr_shown_mtime`：`_show_qrcode` 成功后回填；失败不推进，
   监视线程下一秒重试同一张（失败日志按 `_qr_fail_logged` 去重）。
4. **事件泵加固**：`_drain` 拆出 `_dispatch_event`，逐事件 try/except（原来任何一个
   handler 抛异常都会跳过结尾的 `after()` 自续 → 泵死 → 健康刷新/日志/弹窗全失效，
   界面看起来还活着）。

### 已排除的假设（有证据，别再查）
- ❌ exe 包内缺 tkinter/ImageTk：exe 只是启动器（`tools/inspect_pyinstaller.py` 解析过
  CArchive，只有 67 个引导条目），真正跑的是 `.venv`，而 `.venv` 的 tkinter 和
  PIL.ImageTk 实测 import OK。
- ❌ `_show_qrcode` 的 PIL 读取路径本身：用 `.venv` python 直接建 Tk + PhotoImage 成功。
- ❌ NapCat 没写码：qrcode.png 实测在被写（11:37 时每 2~7s 刷；15:23 时 25 秒前刚写）。
- ❌ 端口/服务问题：服务在跑、诊断脚本通；这是纯界面层问题。

---

## 4. 关键代码位置（v0.5.6 行号，`src/msgx/gui.py`）

| 函数 | 行号 | 说明 |
|---|---|---|
| `restart_napcat` | 1254 | 重置按钮处理：确认框 → `napcat_mod.restart()` → `if self.napcat_dir(): self._start_qrcode_watch(fresh_only=True)` |
| `_start_qrcode_watch` | 1316 | 换代 + 保护窗 + 起线程 |
| `_watch_qrcode` | 1334 | 子线程：每 1s stat `D:\App\NapCat\cache\qrcode.png`，`mtime != seen 且 10 分钟内` → `self.events.put(("qrcode", path))` |
| `_show_qrcode` | 1378 | 主线程：`_close_qrcode()` → PIL 打开 + thumbnail + `ImageTk.PhotoImage` → `tk.Toplevel(topmost)` 弹窗；成功回填 `self.qr_shown_mtime` 并打日志「已弹出扫码窗口」 |
| `_tick_health` | 1433 | 每 2s 起线程 GET `/health` → `events.put(("health", data))` |
| `_drain` | 1482 | 事件泵：`get_nowait()` 循环 + `_dispatch_event`（逐事件 try/except）+ 结尾 `root.after(100, self._drain)` 自续 |
| `_dispatch_event` | 1509 | 事件分发（qrcode → `_show_qrcode`） |
| `_apply_health` | 1538 | 保护窗逻辑在这里（`time.time() >= self.qr_hold_until` 才许停监视） |

相关：`src/msgx/napcat.py` 的 `restart()`（约 481 行起，`_shell_execute_runas("cmd.exe", params, show=0)`，
`params = '/c ""<脚本>" "<目录>""'`，环境变量 `NAPCAT_HOME` + `MSGX_RESTART_SILENT=1`）；
`tools/重启协议端.bat`（GBK+CRLF：杀注入器→杀 QQ→等 2s 补刀→复查→`call launcher.bat`）。
相关状态属性在 `App.__init__`（约 83~100 行）：`qr_watching / qr_gen / qr_hold_until /
qr_shown_mtime / _qr_fail_logged / qr_win / events(queue.Queue)`。

---

## 5. 待查方向（按优先级，供你继续排查）

1. **⚠️ 首先确认：15:20 那两次点击时，界面进程跑的到底是不是 v0.5.6 代码？**
   服务 uptime 195s 说明 15:20 左右服务（可能连带界面）重启过，但无法证实界面进程
   加载了新代码。第一件事：给 `gui_entry.py` / `App.__init__` 加一行**启动日志**（含
   `__version__` + 进程 pid，写到 `data/logs/gui.log`），并考虑在 `_watch_qrcode`
   线程启动/退出、`_show_qrcode` 进入时都落一行到同一文件 —— 下一轮现象发生时
   就能立刻分辨「没检测到」/「检测到没弹」/「根本没跑新代码」。
2. **对「检测到了但没弹」做兜底**：无论 Tk 弹窗是否成功，检测到新码时**同时**
   `os.startfile(str(qr))`（用系统图片查看器打开）或复制到 `data/qrcode_latest.png`
   再 startfile。这是保底可见性，能一劳永逸地绕开 Tk 层的一切问题。注意 Windows 上
   `Image.open` 一个正被写入的文件可能读到半张 —— startfile 前可先 `Image.open().load()`
   试读一次，失败则下一轮重试。
3. **审查 `restart_napcat` → `_start_qrcode_watch` 的调用链在真实 GUI 里是否真的执行**：
   在 `_start_qrcode_watch` 入口落日志（含 fresh_only 与新 gen）。有可能是
   `self.napcat_dir()` 在某次配置读取下返回 None（config 加载顺序/prefs 叠加），
   或确认框后的分支提前 return。
4. **线程生命周期**：v0.5.6 每次调用都起一个新线程，旧线程靠 `gen == self.qr_gen`
   退出 —— 确认没有「旧线程把 `qr_watching` 置 False（被健康轮询停）→ 新线程循环
   条件 `while self.qr_watching ...` 直接不进」的顺序问题：**注意 `_watch_qrcode`
   的循环条件同时依赖 `qr_watching` 和 `gen`，若健康轮询在保护窗之外把
   `qr_watching=False`，而此刻新一轮重置的线程还没起，会出现「永远没人盯文件」**。
   建议把「循环活着」与「qr_watching 开关」解耦（例如线程只看自己的 gen 和 closing）。
5. **`_apply_health` 的保护窗判据**：`qr_hold_until` 是 GUI 进程内的时间，跨天/休眠
   恢复后 `time.time()` 跳变可能造成怪象（低优先级）。
6. **Tk 主线程假设**：确认 `_show_qrcode` 只会被 `_drain`（`root.after` 回调 = 主线程）
   调用；`ImageTk.PhotoImage` 必须在主线程、且 Tk 已初始化。（目前看是这样，但值得
   用日志确认。）
7. **如果以上都查无异常**：写一个最小复现脚本（`.venv` python + 假 qrcode.png +
   每几秒改 mtime），把 `_watch_qrcode → events → _drain → _show_qrcode` 全链路跑通，
   再逐段换成真实现，定位断点。

---

## 6. 约束（务必遵守）

1. **不破坏现有 357 项测试**：改完跑 `.venv\Scripts\python.exe tests\run_all.py`，
   必须全绿；新行为要在 `tests/test_gui.py` 里加契约测试（该文件是脚本式测试，
   `check(name, ok, detail)` 风格，注意保持）。
2. `.bat`/`.cmd` 必须 **GBK + CRLF** 落盘；`.vbs` 必须纯 ASCII。
3. 用户是低容忍度、直接反馈型：修 bug 时**不要引入新的弹窗/确认框**（他已经受够了
   乱弹窗）；日志文案要可读、不吓人。
4. **绝不自动应答/自动登录/自动重启用户账号**：自动化只到「弹二维码」为止（风控）。
5. 不要动 `config.toml`（历史文件名，现已迁移为 `config.yaml`）里与本案无关的字段；不要动大号（1371109209）相关的一切。
6. 代码风格与现仓库一致：中文注释、注释讲「为什么」、每个修复注明版本与根因；
   改完更新 `CHANGELOG.md`（发版 = 改 `src/msgx/__init__.py` 的 `__version__` →
   CHANGELOG → commit → `git tag vX.Y.Z`）。
7. 本机安全策略会拦：`reg.exe`、`wmic.exe`、从 Bash 调 `powershell`（连带 heredoc 内容里
   含 powershell 字样也拦）。查进程用 `tasklist /FI ... /FO CSV /NH`，读注册表用
   Python `winreg`。

---

## 7. 验收标准

1. 完全关闭界面 → 双击 `消息萃取.exe` → 点一次「重启协议端」→ UAC 点「是」→
   **60 秒内**二维码图片窗口自动弹出（或系统图片查看器打开二维码），扫小号
   `1443685390` 能登录成功，服务「协议端」显示已连接，消息恢复入库。
2. 在「旧连接还挂着」的冻结状态下点重置，弹窗依然可靠出现（保护窗生效）。
3. 图片正被 NapCat 重写时点击/检测，不会永久丢码（重试生效）。
4. 人为让 `_dispatch_event` 抛异常（临时代码验证即可），健康刷新与后续弹窗不死（泵兜底生效）。
5. 全部 357+ 项测试通过，`CHANGELOG.md` 有本版条目。

---

## 8. 快速复现/诊断命令

```bat
:: 诊断（进程/连接/二维码/上游 QQ 库，一条命令出结论）
cd /d D:\Dev\MCP\消息萃取-MCP
.venv\Scripts\python.exe tools\diagnose_freeze.py --no-probe

:: 协议端启动/重置时间线（用户每次点按钮都有记录）
type data\logs\napcat.log

:: 跑全部测试
.venv\Scripts\python.exe tests\run_all.py

:: 手动观察二维码文件是否在被重写（PowerShell 或 python stat 循环均可）
```
