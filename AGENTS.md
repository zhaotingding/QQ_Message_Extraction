# 消息萃取项目约定

- 架构与功能契约以 [docs/重构开发规范.md](docs/重构开发规范.md) 为准；现用安装与测试入口见 [README.md](README.md)。
- 应用配置统一 YAML，开发说明统一 Markdown；Python 打包、Git 和外部协议保留各自标准格式。
- 版本唯一来源为 `src/msgx/__init__.py`；中文本地提交，远程 push 需要单独授权。
- 测试使用隔离目录、数据库、端口和模拟凭据；真实聊天、QQ 登录及付费请求不作为默认测试输入。

## 项目技能索引

按当前任务选读匹配技能及必要参考资料；索引不要求每轮全部读取。

| 触发任务 | Skill |
| --- | --- |
| 测试隔离、回归验收、精简或迁移测试用例 | [msgx-maintenance](.agents/skills/msgx-maintenance/SKILL.md) |
| YAML 配置、偏好、API 配置与凭据保存 | [msgx-config-credentials](.agents/skills/msgx-config-credentials/SKILL.md) |
| 消息过滤、活跃度、关键词、本人关联与 SQLite 查询或升级 | [msgx-message-queries](.agents/skills/msgx-message-queries/SKILL.md) |
| 日周月摘要、请求恢复、用量记账与费用口径 | [msgx-summary-accounting](.agents/skills/msgx-summary-accounting/SKILL.md) |
| Tk 交互、接收进程控制、扫码验证与阅读批注 | [msgx-desktop-ui](.agents/skills/msgx-desktop-ui/SKILL.md) |
| Windows 克隆安装、启动入口、打包及图标资源 | [msgx-windows-release](.agents/skills/msgx-windows-release/SKILL.md) |
