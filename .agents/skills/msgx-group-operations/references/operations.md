# 操作契约与验证入口

群发预览冻结本次群间隔，缺省 30 秒，界面可改为 1～3600 的整数秒。首群立即提交，每次有结果后再等待下个接收群；等待中可以停止。2026-11-23 已用本机模拟协议验证预览、停止和逐群状态，见 `tests/test_group_actions.py`。

包含 1.0.3 的操作契约及 1.0.5 的逐群失败处理；各日期只证明原验证范围。用户明确要求二次确认及完整接收群清单，见项目 AGENTS.md 首条。Electron 与旧 Tk 均须沿用同一服务端契约。

- 服务端 `GroupActions.prepare` 读取真实账号与 `get_group_list(no_cache=true)`，群发取明确统计名单交集，生成5分钟一次性令牌；预览不调用发送/退群。
- `confirm` 消耗令牌并校验范围；**群发必须带 `groups`（界面勾选的群号），只允许是预览名单的子集**——服务端空选/不传/含名单外的群一律拒绝（`_narrow`），绝不把空选择当全部。收窄后再逐个核对「群仍属于当前账号且群名未变」，执行前与每群之间复核。正文用 OneBot text segment，不解析 CQ 码或隐含 @全体。勾选结果仅作为下次预览的预填（偏好 `broadcast_groups`），不绕过任何一次确认。
- `ProtocolSession.call_action(connection=...)` 固定连接；`ActionRejected` 表示明确失败，超时/不完整响应保持未知。已完成发送用 message_id 证明协议确认，不能据此保证群友已阅读。
- **失败粒度**（1.0.5 起）：群发时 `ActionRejected` 只把当前群标为 `failed`（附 `retcode` + 首句原因，由 `protocol_session.reject_text` 压成一行，原始响应只进日志），**继续发后续群且不重试该群**；只有连接断开、超时/异步受理（结果未知）、账号或名单变化才判 `fatal` 并停止整批。任务结束时用 `_summarize` 给出 `completed`／`partial`（全部尝试完但有失败）／`stopped`（还有群没被尝试）与计数文案，界面状态栏只显示计数，逐群原因留在表格。
- `group_action_jobs` 在提交前及每群完成后持久化；重启将未明提交标未知、未提交群标未发送，不重放。取消仅停止尚未提交的群，不能冒充撤回。
- `clear_departed_group` 使用单事务清理 messages/message_images/message_receipts/alerts，保留 groups 历史名称与 summaries/summary_reviews/api_calls；成员快照只移除当前账号关系。
- 普通刷新只同步当前成员，不删除历史。只有本模块明确确认退群且协议成功的路径允许清理。

隔离验证入口：`tests/test_group_actions.py`（状态机、临时SQLite及本机HTTP）；Electron 通过 `tests/electron_smoke.cjs` 检查实际客户端链路；`tests/test_realtime_gui.py` 仅覆盖旧 Tk 确认窗口/取消/历史定位。原始 1.0.3 运行结果在本地 `data/diagnostics/v1.0.3/`，不作为新增 Electron 入口已通过的证据。
这些检查不代表真实QQ发送、生产数据清理或Skill原生调用已验证。2026-11-03独立只读审查发现并复核修复了取消核对竞态、YAML缺字段沿用旧范围、异步受理误报失败、退群待响应时丢消息、服务切换后控件不恢复五项问题；实际回归结果另见验收记录。

协议主来源：[NapCat 官方接口](https://napneko.github.io/onebot/api)。退群固定 `is_dismiss=false`，群主须自行先转让，不能将退群扩展为解散。
