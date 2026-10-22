# 消息查询与迁移证据

适用：0.9.9；证据日期如下。路径相对仓库根目录。历史原路径已归档时按 [归档清单](../../../../docs/开发文件归档清单.md) 查找；本地数据库副本清理不使测量结果成为当前基准。

## 连接、迁移和查询

只读构造不能创建空库、迁移或回填。705首次升级前以SQLite backup保留包含WAL的可恢复快照；旧账号表先走原账号迁移备份，正常再次打开不重放历史图片、接收凭据和群账号回填，704部分图片结果和勾选保持。

`/messages/recent` 的历史关联读取在独立线程连接的只读事务中执行。待处理图片及失败语义计数使用条件索引；批量取图按message_row/image_index归组，不能把JOIN行当消息。保留同群引用索引及大库稀疏命中检查；关键词先匹配再分页，不能仅筛选最近200条。

现有 `messages_between_all(start, end)` 的底层条件为 `ts > start AND ts <= end`；自然日报调用方以 `start-1/end-1` 转为外层左闭右开窗口。改SQL前核对调用方，不能让边界转换重复或漏掉零点消息。源码为 `message_store.py`、`summarize.py`；2026-10-15的自然日边界回归见 `tests/test_period_summary.py`。

显式空发言人名单有效；删除管家名称时一并停用其绑定账号匹配。群活跃度按消息时间的北京日统计未过滤记录；同一条父消息无论关联多少张图片或接收凭据都只计一次。

- 2026-10-14 隔离同规模验证：`data/diagnostics/refactor-20261014/backend/`；查询结果等价、统计/批图成本和并行接收有测量，未代表生产长期验证。
- 2026-10-15 隔离回归：`tests/test_storage_queries.py`、`test_onebot_reads.py`、`test_group_activity.py`、`test_sender_filters.py`、`test_message_keywords.py`、`test_related_messages.py`。覆盖备份/一次回填、线程归属、过滤和稀疏查询。

## 仅排查本人消息缺失时读取

本地支持 `message_sent` 不保证上游会发送本人消息。2026-10-07实际使用的NapCat adapter需要开启 `reportSelfMessage`；当时 `/OB11Config/SetConfig` 的 `config` 参数为JSON字符串，可只修改本项目连接。换账号或adapter后先读回当前配置，不能假定所有版本相同或当前仍开启。

原证据路径为 `data/diagnostics/v0.9.0/napcat-self-report.json`，已归档；大库本人关联证据原路径为 `data/diagnostics/v0.9.1/readonly-query.json`、`data/diagnostics/v0.9.3/verification.json`。这次技能整理没有重新调用上游接口。旧raw可能截断，优先使用已持久化的mentioned_ids；无法确认的关系保持未知。
