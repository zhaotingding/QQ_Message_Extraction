# 消息查询与迁移证据

保留 0.9.9 起的查询、705 迁移及 1.0.3 统计证据；版本号描述历史实现，不要求当前库重放迁移。路径相对仓库根目录。历史原路径已归档时按 [归档清单](../../../../docs/开发文件归档清单.md) 查找；本地数据库副本清理不使测量结果成为当前基准。

## 列表首屏与组合筛选

`messages.recent` 的 `list` 和 `related` 视图直接走 `_recent()`。服务运行时优先用该实例的 `service_cfg` 确定资料和账号，停止后使用当前配置。一次 `BEGIN` 固定 WAL 读取快照，消息、逐图详情和群下拉选项由同一连接取得。群选项取当前账号的群范围，再应用已保存的统计群选择。查询首屏时无需等待协议健康检查或重算月榜。[桌面消息查询](../../../../desktop/messages_api.py)

群号与发送者 ID 是两个可组合的条件，SQL 在 `LIMIT` 前过滤，再按 `ts DESC, id DESC` 取最近记录。用两个同名发送者、多个群和小页长验证返回的行与顺序，同时令月榜调用在列表测试中直接失败，以发现重查询被意外放回首屏。[隔离查询用例](../../../../tests/test_message_list_view.py)

2026-11-23 开发记录中的一次百万量级本机库测量，列表与 71 个群选项约 19 毫秒，原先同请求内额外的本人关联及月度统计分别约 2.35、1.45 秒。该测量仅覆盖数据库读取。本轮用合成库复核了组合筛选和最近消息顺序，界面行为另由 Electron 夹具覆盖。

## 连接、迁移和查询

1.0.3（2026-11-03）：月度加权榜使用30秒短缓存，键包含账号、统计群、过滤、趋势群/窗口、北京日期及群变更版本；普通消息查询仍读取新快照。手动立即刷新绕过缓存，成功群同步和退群清理递增群版本，旧在途读取不得回填已失效缓存。20万合成行重算统计约1.2～2.0秒，历史定位约3～5.5毫秒；连续两次接口刷新只重算一次月榜，最新消息及本人关联仍查询数据库。定位使用`idx_msg_group_ts`；具体环境测量见本地`data/diagnostics/v1.0.3/performance.yaml`，不替代生产性能结论。

只读构造不能创建空库、迁移或回填。705首次升级前以SQLite backup保留包含WAL的可恢复快照；旧账号表先走原账号迁移备份，正常再次打开不重放历史图片、接收凭据和群账号回填，704部分图片结果和勾选保持。

`/messages/recent` 的历史关联读取在独立线程连接的只读事务中执行。待处理图片及失败语义计数使用条件索引；批量取图按message_row/image_index归组，不能把JOIN行当消息。保留同群引用索引及大库稀疏命中检查；关键词先匹配再分页，不能仅筛选最近200条。

现有 `messages_between_all(start, end)` 的底层条件为 `ts > start AND ts <= end`；自然日报调用方以 `start-1/end-1` 转为外层左闭右开窗口。改SQL前核对调用方，不能让边界转换重复或漏掉零点消息。源码为 `message_store.py`、`summarize.py`；2026-10-15的自然日边界回归见 `tests/test_period_summary.py`。

显式空发言人名单有效；删除管家名称时一并停用其绑定账号匹配。群活跃度按消息时间的北京日统计未过滤记录；同一条父消息无论关联多少张图片或接收凭据都只计一次。

- 2026-10-14 隔离同规模验证：`data/diagnostics/refactor-20261014/backend/`；查询结果等价、统计/批图成本和并行接收有测量，未代表生产长期验证。
- 2026-10-15 隔离回归：`tests/test_storage_queries.py`、`test_onebot_reads.py`、`test_group_activity.py`、`test_sender_filters.py`、`test_message_keywords.py`、`test_related_messages.py`。覆盖备份/一次回填、线程归属、过滤和稀疏查询。

## 仅排查本人消息缺失时读取

本地支持 `message_sent` 不保证上游会发送本人消息。2026-10-07实际使用的NapCat adapter需要开启 `reportSelfMessage`；当时 `/OB11Config/SetConfig` 的 `config` 参数为JSON字符串，可只修改本项目连接。换账号或adapter后先读回当前配置，不能假定所有版本相同或当前仍开启。

原证据路径为 `data/diagnostics/v0.9.0/napcat-self-report.json`，已归档；大库本人关联证据原路径为 `data/diagnostics/v0.9.1/readonly-query.json`、`data/diagnostics/v0.9.3/verification.json`。这次技能整理没有重新调用上游接口。旧raw可能截断，优先使用已持久化的mentioned_ids；无法确认的关系保持未知。
