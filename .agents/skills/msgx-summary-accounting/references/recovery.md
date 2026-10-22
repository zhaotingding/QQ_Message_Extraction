# 恢复链路与账单证据

适用：0.9.9。路径相对仓库根目录；本地忽略资料缺失时核对源码或历史记录，不推定任务已完成。

## 调度与提交

日报总结上一完整北京自然日；周一总结上一自然周的日报；月报使用月内完整周报与月初/月末日报补齐，不混入邻月内容。目标篇幅为D/2D/3D；完整超长输出保留，不截断或无授权追加请求。

官方自动摘要保存Responses后台响应ID、原材料、配置及结果。断线或重开后GET同一响应；查询失败不另记一笔生成请求。没有ID且提交结果未知时，保留待确认，不以自动重试再次POST。清理材料在完整结果、成稿和记账恢复之后。

有ID不表示永远禁止补生成：当前 `retry_summary_job()` 先检查 `confirmed`、blocked状态和最新修订；已有成稿则直接恢复完成，否则只在无ID或已持久化结果的 `finish_reason != "stop"` 分支清理原请求，再交原调度处理。单次取回失败不等于已确认生成失败，也不构成重提授权。此分支为2026-10-15源码静态核对；未据此执行真实取回或补生成。

- 源码：`summarize.py`、`summary_scheduler.py`、`llm.py`、`summary_store.py`、`usage_store.py`。
- 2026-10-15 模拟验证：`tests/test_background_summary.py`、`test_summary_recovery.py`、`test_period_summary.py`、`test_period_scheduler.py`；涵盖断线、重开、分块、保存失败、一次POST/记账与依赖恢复。不是再次真实付费验收。
- 2026-10-09 实际补生成证据：`data/diagnostics/v0.9.4/live-summary.json`；只说明当次请求。已废止流式恢复与旧对账差额留在Git历史，不作当前方案。

## 网络与费用

`force_close=True` 是现有可靠性策略：旧实现发生过陈旧连接和重置共享Session扩散失败。连接复用可作为待测优化，需验证断连和并发隔离；这不是永远禁止改动的规则，旧注释数字也不是当前性能基准。

官方OpenAI的Flex按实际返回服务档位折算，避免再次将已折算金额减半；不把该倍率推广到其它平台。区分平台明细、余额差值、本地估算和未知；新API模式“刷新账单”只刷新本地估算，不等于取得平台余额或扣费。余额差值分辨率和跨充值断档的限制随结果说明，不伪造逐模型归因。

北京00:05的余额读数在模拟UTC主机上曾被算作零点后57900秒；共用 `time_rules.BEIJING` 后为300秒。2026-10-15失败复现和修复回归见 `data/diagnostics/v0.9.9/protocol/timezone-reproduction.log`、`after-cost.log`；未改系统时区、历史账本或费率。费用专项另见 `tests/test_cost.py`、`test_usage.py`、`test_api_settings.py`。
