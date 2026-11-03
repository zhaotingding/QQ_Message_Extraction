# 操作契约与验证入口

适用：1.0.3；核对日期：2026-11-03。用户明确要求二次确认及完整接收群清单，见项目 AGENTS.md 首条。

- 服务端 `GroupActions.prepare` 读取真实账号与 `get_group_list(no_cache=true)`，群发取明确统计名单交集，生成5分钟一次性令牌；预览不调用发送/退群。
- `confirm` 消耗令牌并校验范围，执行前与每群之间复核。正文用 OneBot text segment，不解析 CQ 码或隐含 @全体。
- `ProtocolSession.call_action(connection=...)` 固定连接；`ActionRejected` 表示明确失败，超时/不完整响应保持未知。已完成发送用 message_id 证明协议确认，不能据此保证群友已阅读。
- `group_action_jobs` 在提交前及每群完成后持久化；重启将未明提交标未知、未提交群标未发送，不重放。取消仅停止尚未提交的群，不能冒充撤回。
- `clear_departed_group` 使用单事务清理 messages/message_images/message_receipts/alerts，保留 groups 历史名称与 summaries/summary_reviews/api_calls；成员快照只移除当前账号关系。
- 普通刷新只同步当前成员，不删除历史。只有本模块明确确认退群且协议成功的路径允许清理。

隔离验证入口：`tests/test_group_actions.py`（状态机、临时SQLite及本机HTTP）、`tests/test_realtime_gui.py`（真实Tk确认窗口/取消/历史定位）；原始运行结果在本地 `data/diagnostics/v1.0.3/`。
这些检查不代表真实QQ发送、生产数据清理或Skill原生调用已验证。2026-11-03独立只读审查发现并复核修复了取消核对竞态、YAML缺字段沿用旧范围、异步受理误报失败、退群待响应时丢消息、服务切换后控件不恢复五项问题；实际回归结果另见验收记录。

协议主来源：[NapCat 官方接口](https://napneko.github.io/onebot/api)。退群固定 `is_dismiss=false`，群主须自行先转让，不能将退群扩展为解散。
