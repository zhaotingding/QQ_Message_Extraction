# Agent 项目笔记

仅保存当前仍有效、容易在维护中误判的约束。适用基线：0.9.4.1；本次代码核验：2026-10-13（静态核验）。
功能契约和开发流程见 `重构开发规范.md`；发布历史见 `CHANGELOG.md`。
历史证据的原路径按 `docs/开发文件归档清单.md` 查找；旧笔记完整保留在 Git 提交 `5c6a4b4`。

## 配置与密钥

- `config.toml` 保存公开配置；GUI 启动服务通过 `MSGX_PREFS` 叠加本地偏好。
  已保存设置与未提交编辑分开；在途模型任务保留开始时的配置、材料和费率快照。
- 启用 `[api]` 后，密钥按配置标识和完整 API 基础地址从 Windows 凭据管理器读取；
  同地址不同配置不能串用。旧 OpenAI 凭据标识保留，重构不能更名后要求用户重新配置。
  凭据保存和偏好保存是两步，部分失败必须准确反馈，不能丢失用户输入。
- 程序不自动加载 `.env`。旧版环境变量兼容仅读取系统环境中的 `api_key_env`；
  真实密钥不得放入受 Git 管理的配置或示例文件。
- 证据：`config.py`、`credentials.py`、`api_settings.py`；本次静态核验。
  `tests/test_api_settings.py`、`test_gui_feedback.py` 覆盖配置隔离及部分保存失败（模拟验证）。

## 接收、进程与扫码

- 协议连接、心跳和群列表成功不能证明已收到 QQ 群消息；关闭扫码监视须收到目标账号在本次操作后的新消息。
  `ECONNREFUSED :6700` 表示本机接收端不可连接，不能直接归因于 QQ 掉线。
- Windows venv 启动器 PID 与实际服务 PID 不同。回收服务只能处理本窗口持有的 Popen 进程树；
  手动强制重启按明确安装目录/目标账号处理协议进程，不能按 Python/QQ 名称全杀。
- GUI 与协议操作共用 `napcat.resolve_dir()`。二维码操作保留创建前的时间基线及 `qr_gen`；
  图片未完整写入或展示失败时不推进已展示基线，后续重试。Tk 窗口只在主线程操作。
- 0.9.4.1 的异常恢复只重建接收服务，5/15/30 秒最多三次，稳定五分钟后清零；
  手动停止、关闭、强制重启的预期清理及退出码 2 不触发自动恢复；自动恢复不保存未提交编辑。
- 2026-10-12 的两次服务退出码为 1，历史触发源仍未确认。排查保留 `data/logs/gui.log`、
  `service.log`、`service-output.log` 和 `service-fault.log`；底层自行 exit 或外部终止可能没有 Python 故障栈。
- 证据：`gui.py`、`napcat.py`、`main.py`；本次静态核验。
  `data/diagnostics/v0.9.4.1/service-e2e.json`：2026-10-13 独立 Tk/服务实际验收退出后恢复、收消息及手动停止。
  此验收不证明生产环境长期稳定或真实手机扫码已通过。

## 运行中数据库与检索

- 只读工作在线程内创建、关闭连接，使用正常 `mode=ro` 读取运行中的 WAL；
  不使用忽略 WAL 的 `immutable=1`。只读入口不能顺带迁移或回填。
- 同群引用查询保留 `idx_msg_reply` 及大库稀疏命中回归。历史实际库曾因错误查询计划使相关列表超过十秒，
  阻塞接收事件循环；几条合成消息不能证明此查询性能合格。
- 历史关键词先匹配再 LIMIT，不能只扫描屏幕上最近 200 条。OCR/语义还须查询 `message_images`，
  因为父消息聚合文本只包含勾选纳入摘要的图片；历史正则检索在后台只读任务中执行。
- 群活跃度按北京时间消息日期统计未过滤入库记录；聚合基于 `messages`，不展开图片或接收凭据表。
  多图、多账号接收会使 JOIN 后的计数重复。显式空发言人名单有效；删除管家名时一并停用其绑定账号匹配。
- 证据：`store.py`、`message_keywords.py`；本次静态核验。
  `tests/test_related_messages.py`、`test_group_activity.py`、`test_sender_filters.py`、
  `test_message_keywords.py` 保留相关回归；历史真实库证据见归档的 `v0.9.1/readonly-query.json`（2026-10-07）
  和 `v0.9.3/verification.json`（2026-10-08）。

## 本人消息的上游条件

- 仅支持解析 `message_sent` 不足以收到本人发言。2026-10-07 当时核验的 NapCat adapter 还要求
  `reportSelfMessage` 开启；换账号或 adapter 后须重新读回配置，不能假定当前仍已开启。
- 当时 `/OB11Config/SetConfig` 的 `config` 参数为 JSON 字符串，可热更新目标连接。
  操作只改本项目连接；不能把同一次测试结果当成所有连接的保证。
- 本地关联使用明确账号、协议 @ 对象和同群引用，排除 `at_all`。旧 `raw` 可截断或缺失，
  依赖持久化的 `mentioned_ids`；没有原始引用的历史消息不能凭空补出关联。
- 证据：`models.py`、`store.py`、`tests/test_related_messages.py`；本次静态核验。
  上游配置读取/变更属于 2026-10-07 的实际验证，原件见归档 `data/diagnostics/v0.9.0/napcat-self-report.json`；
  本次没有再次调用管理接口。

## 摘要恢复与账单

- 官方自动日/周/月摘要保留 Responses 后台响应 ID、材料和原配置；断线或重开后只 GET 同一响应。
  无响应 ID 且提交结果未知时保留 blocked，不能自动重新 POST。已有分块/成稿优先复用。
- API 账本与 `accounted` 标记保持同事务提交；落成稿失败应继续恢复，不重复记账。
  手动补生成检查任务最新修订，唤醒原调度，不能另起并行生成循环。
- 保留 `force_close=True` 的可靠性边界：旧实现发生过陈旧连接及重置共享 Session 扩散失败。
  连接复用属于待测优化，先验证断连与并发隔离，再决定替换；旧注释数字不是当前基准。
- 费率输入使用 Standard 基准，按响应实际层级估算；未返回时注明请求档位，未知用量不能计为免费。
  新 API 模式的“刷新账单”刷新本地估算记录，不等于查询平台实际余额或扣费。
- 证据：`llm.py`、`pipeline.py`、`store.py`；本次静态核验。
  `tests/test_background_summary.py`、`test_summary_recovery.py` 覆盖模拟恢复；
  `data/diagnostics/v0.9.4/live-summary.json` 记录 2026-10-09 一次真实补生成。
  已废止的旧流式恢复过程与旧对账差额留在 Git 历史，不作为当前方案。

## 阅读与图标

- HTML 丢失时从 `summaries.content` 读取原文，不能为了阅读再次调用模型。
  批注按摘要及正文版本隔离；坐标属于渲染文字，不能直接套 Markdown 原文。
  Windows Tk 对 emoji 的字符计数与 Python 切片可能不同，恢复时核对选中文字。
- ICO 原样保留各尺寸帧，PNG 各尺寸从原图采样；当前窗口使用 `iconphoto(False, ...)`。
  窗口图标和 EXE 资源图标分别验收。构建用 `tools/build_launcher.py` 沿用已保存图标，
  不直接修改 PyInstaller onefile 而遗漏尾部归档。
- Explorer 曾在资源正确后仍显示旧图标；`SHCNE_ASSOCCHANGED` 加目录刷新才解决。
  资源像素一致不等于已验证 Explorer 显示；也不需要为此删除系统缓存或重启 Explorer。
- 证据：`gui.py`、`summary_review.py`、`tests/test_reader_review_gui.py`、`test_icon_windows.py`；
  本次静态核验。2026-10-09 的实际 Explorer 截图见 `data/diagnostics/v0.9.4/explorer-after.png`；
  2026-10-13 新启动器隔离运行和 16/32 帧像素验收见
  `data/diagnostics/dependency-singlefile-20261013/launcher-verification.json`。
