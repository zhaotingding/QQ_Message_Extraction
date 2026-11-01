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

1.0.1（2026-10-31，30项API及8项后台恢复隔离测试通过）：只有官方OpenAI已请求Flex且明确返回`429 resource_unavailable`时立即按Standard重新提交；普通429、超时及未知提交不走该分支。回退后的实际提交档位须持久化到请求metadata，重开GET取回时继续按该档位推算，服务端返回档位仍优先；不能保留原Flex半价。

策略依据：[OpenAI Flex资源不足处理](https://developers.openai.com/api/docs/guides/flex-processing)。新任务统一Responses；[DeepSeek官方Responses](https://api-docs.deepseek.com/guides/responses_api/)已提供该格式（2026-10-31查阅），不把官方支持推广到任意代理。旧任务恢复仍用原协议快照，不因编辑配置切换接口或重新POST。

`force_close=True` 是现有可靠性策略：旧实现发生过陈旧连接和重置共享Session扩散失败。连接复用可作为待测优化，需验证断连和并发隔离；这不是永远禁止改动的规则，旧注释数字也不是当前性能基准。

官方OpenAI的Flex按实际返回服务档位折算，避免再次将已折算金额减半；不把该倍率推广到其它平台。区分平台明细、余额差值、本地估算和未知；新API模式“刷新账单”只刷新本地估算，不等于取得平台余额或扣费。余额差值分辨率和跨充值断档的限制随结果说明，不伪造逐模型归因。

北京00:05的余额读数在模拟UTC主机上曾被算作零点后57900秒；共用 `time_rules.BEIJING` 后为300秒。2026-10-15失败复现和修复回归见 `data/diagnostics/v0.9.9/protocol/timezone-reproduction.log`、`after-cost.log`；未改系统时区、历史账本或费率。费用专项另见 `tests/test_cost.py`、`test_usage.py`、`test_api_settings.py`。

## 提示词与报告内容评估

- 1.0.0 的 `prompt_evolution.py` 与原摘要调度共用循环，自动默认关闭；开启后默认每7天分析最近7天成稿，或由用户手动排队。`summary_prompt_versions` 保存内容窗口、报告ID与时间，`summary_prompt_state` 保存冻结请求；恢复/清空只切换动态版本。
- 结构化 focus 至少由两个自然日支持，ads 只归纳营销特征；模型输出不进入 system 指令。`summarize.py` 在准备材料时冻结当前观察，后台恢复仍沿原材料、API和费率；提示词更新自身计入“提示词更新”用途。源码与隔离覆盖见 `test_prompt_evolution.py`、`test_automation_gui.py`。
- 开启自更新时，已接受且尚未完成的 response ID 可由原调度自动 GET 取回，沿原材料、profile与费率，不受新 POST 预算或七天更新间隔阻断；无ID结果未知仍等待明确确认。已完成但校验失败的结果不自动循环处理，关闭自更新后不再自动取回。

- 统计重复内容时，日报按自然日去重；周/月报、滚动纪要及同日旧版本可能复用材料，不能相加当作独立消息或独立证据。关键词命中只说明出现线索，混合段落不等于整段广告。
- 当前技术重点导向：纯营销默认舍弃，不用“未经核实”或“交易活跃、需谨慎”重新包装成固定段落；商业工具和硬件讨论中的具体技术结果、部署条件及安全事件仍按证据保留。不要把群内转述升级为已核实前沿事实。
- 默认提示词唯一来源为 `summarize.DEFAULT_DIGEST_PROMPT`；个人 `summary_prompt` 非空时优先，改默认值不自动覆盖个人设置。运行中窗口仍持有原默认常量；应用新版应明确通过设置粘贴保存或重开后恢复默认，在途任务沿用原快照。
- 日报输入仅含当日材料，不能凭提示词保证跨日去重；只有实际提供历史基线时才比较新增内容。不要为此顺带改变输入范围或重新生成历史报告。
- 2026-10-26：扫描54份本地HTML，按37个自然日日摘要统计并复核营销与技术主题；新版通过21项周期摘要和20项界面反馈隔离测试，未做真实模型A/B或付费生成，广告减少效果待实际日报验收。细节见本地忽略目录 `data/diagnostics/report-prompt-20261026/报告分析与修订说明.md`，不将私人报告纳入技能或仓库。
