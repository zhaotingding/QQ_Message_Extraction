# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

**发版约定**：版本号只有一个来源 —— `src/msgx/__init__.py` 的 `__version__`。
发版流程：改版本号 → 更新本文件 → `git commit` → `git tag vX.Y.Z`。

---

## [0.2.0] - 待发布

计划内容见本文件末尾的「路线图」。

---

## [0.1.0] - 2026-09-19

首个可用版本。链路：**QQ 群 → NapCat(NapCatQQ) → 反向 WebSocket → 本服务 → SQLite → 打分 → 本机托盘提醒 / 周期摘要**。

### 新增

- **接入层**（`onebot.py`）
  - OneBot 11 反向 WebSocket 服务端，端点 `/onebot/ws`
  - HTTP POST 上报备用通道 `/onebot`
  - 本地调试接口 `/inject`、健康检查 `/health`、告警查询 `/alerts`、手动摘要 `/summary/now`
  - 可选 `access_token` 鉴权（查询串或 `Authorization: Bearer`）
- **消息归一化**（`models.py`）
  - 同时支持 `messagePostFormat=array`（段数组）与 CQ 码字符串两种上报格式
  - 识别 `image` / `mface`（表情包同样携带图片 URL）/ `at` / `reply` / `file` / `forward` / `record` / `video`
  - 「被点名」判定使用可配置的 QQ 集合，而非机器人自身 ID（监听用小号，别人不会 @ 它）
- **存储**（`store.py`）
  - SQLite（WAL 模式），表：`groups` / `messages` / `summaries` / `alerts` / `usage`
  - 以 `(group_id, msg_id)` 唯一索引天然去重
  - 按保留策略清理原始消息与媒体文件
- **媒体理解**（`media.py`）
  - 图片下载（支持 `file://` 本地路径，按内容嗅探真实格式，不信任扩展名）
  - SHA256 去重，同图只识别一次
  - RapidOCR 本地 OCR（纯 pip，不依赖 paddle/torch）
  - 可选多模态语义描述，仅在探测到模型支持图片时启用
  - 处理完成后立刻回头打分 —— 报错截图的 OCR 文本正是打分依据
- **打分与分级**（`score.py`）
  - 规则预筛（关键词 / 高价值域名 / 长文 / 可复用配置命令 / 多人转发同一链接 / 图片报错）
  - 模型复核：`score`（值不值得留存）与 `urgent`（值不值得**现在**打断）两个正交维度
  - `at_me`（被点名）为硬规则，不由模型降级
- **摘要**（`summarize.py`）
  - 周期摘要：按群窗口切分，超长自动分块，多块再合并
  - 每日日报：汇总当日全部周期摘要，提炼「今日重点」
  - 输出自包含的 HTML（内嵌样式，可直接双击打开）
- **通知**（`notify.py`）
  - 本机 Windows 托盘通知，`winotify` 失败时自动回退 PowerShell 气泡
  - 预留 webhook（企业微信 / Server酱 / 通用 JSON），留空则不启用
- **配置**（`config.py`）
  - `config.toml` + 标准库 `tomllib`，零额外依赖
  - 内置默认值，配置文件可以只写需要覆盖的项
- **工具与入口**
  - `tools/make_demo.py`：生成仿真群聊数据（含用于 OCR 的合成截图），可在不接 QQ 的情况下跑通全链路
  - `tools/launcher.cmd`、`START_HERE.cmd`、`启动服务.bat`、`查看状态.bat`

### 修复

以下缺陷均在真实/仿真联调中复现并修复：

- **接入队列从未被消费**（`pipeline.py`）—— `_ingest_loop` 定义了却没有 `create_task`。
  后果：WebSocket 收到的消息全部堆在队列里（上限 5000），进程一重启就静默丢失，
  表现为「服务日志显示协议端已连接，但数据库里一条消息都没有」。
  已通过 A/B 对照验证：修复后落库 1 条，修复前 0 条且事件仍卡在队列中。
- **`file://` 路径解析**（`media.py`）—— 原先用 `url[7:]`，在 Windows 下会得到 `/D:/...` 而失效。
  改用 `url2pathname(unquote(urlparse(url).path))`。
- **表情包被丢弃**（`models.py`）—— `mface` 段同样携带图片 URL，原先未采集。
- **`@我` 永远不触发**（`models.py`）—— 监听账号是独立小号，没人会 @ 它。
  改为通过 `[watch] at_qq` 配置需要关注的主号。
- **P0 告警泛滥**（`score.py`）—— 提示词同时写着「高价值 = 7-10 分」和「score >= 8 即 P0」，
  两条互斥，导致凡是被判高价值的消息自动成为 P0（实测 10 条告警里 9 条是 P0，提醒形同失效）。
  已拆成正交两维，仅 `urgent=true` 才升级为 P0。
- **规则召回缺口**（`score.py`）—— 纯靠链接和关键词会漏掉「配置 / 命令类解答」。
  新增 `TECH_RE`：环境变量赋值、长命令行开关、安装命令。
- **摘要功能静默失效**（`store.py`）—— `insert_message` 原先只在群名非空时注册群，
  而摘要调度以 `groups` 表为遍历源，未配置群名时该表恒为空 → 摘要永不生成。
  已改为无条件注册（`upsert_group` 内部已保护空名不覆盖已有名字）。
- **摘要漏掉群内第一条消息**（`summarize.py`）—— 窗口下界是开区间（`ts > start`），
  首个窗口用 `first_ts` 作下界会永久排除第一条。已改用 `first_ts - 1`。
- **摘要 HTML 排版错乱**（`summarize.py`）—— `html.escape` 会把 `>` 变成 4 字符的 `&gt;`，
  若用未转义串的下标去切转义串，会切出 `t; ...` 这样的残渣。已改为先切片再转义。
- **空摘要静默产出**（`llm.py`）—— 模型默认开启思考，思考 token 与正文共用 `max_tokens`
  且按输出价计费。预算被思考吃满时会返回 `finish_reason=length` 且正文为空。
  已加 `think=False` 开关（注入 `reasoning_effort: "none"`），并保留「正文为空且被截断 → 放大 4 倍重试」的兜底。
- **多模态探测假阴性**（`llm.py`）—— 探测请求原先只给 `max_tokens=32`，会被思考吃光，
  导致把支持图片的模型误判为不支持。已提高到 512。
- **服务端路由契约** —— `/onebot/ws` 是 WebSocket，`/onebot` 是 HTTP POST。
  两者混淆会让 NapCat 首次握手直接失败（实测返回 405）。已在文档与配置注释中明确。

### 已知限制

- **语音消息不转文字**：需要 ffmpeg，当前仅记录为 `[语音]` 占位。
- **合并转发只记占位符**：内容未展开，仅记录 `[合并转发]`。
- **OCR 对英文术语不可靠**：RapidOCR 中文模型会把英文词切碎（`Trace back`、`C UDAout`）。
  可读性够用，但不要把英文术语写进关键词规则 —— 交给模型判断。
- **无界面**：所有配置通过编辑 `config.toml` 完成（0.2.0 提供图形界面）。
- **仅支持 OneBot 11 的群消息**：私聊、频道消息直接跳过。

---

## 路线图

### 0.2.0（下一步）

- 图形界面（Tkinter，标准库，不引入新依赖）
- 界面内可调：关注关键词、摘要间隔、参与统计的群、不接收的消息类型
- 界面偏好写入 `.ui/preferences.json`，**不改写受版本管理的 `config.toml`**，避免多人协作冲突
- 消息类型过滤（表情包 / 纯图片 / 合并转发 / 语音 / 短于 N 字）

### 待评估

- 语音转文字（依赖 ffmpeg，需先确认体积与安装成本）
- 展开合并转发内容
- 向量检索历史消息
- 语义去重（同一问题被反复提问时归并）
