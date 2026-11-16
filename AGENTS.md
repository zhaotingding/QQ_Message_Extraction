# 消息萃取项目约定

- **群发安全置顶：群发必须先展示目标 QQ、完整正文、候选接收群名与群号，由用户**逐群勾选**后再二次明确确认才能发送；默认不预选任何群，勾选结果本地记忆但仅作预填。仅允许勾选已明确保存的统计群与当前账号实时群列表的交集；一个群都没勾时界面禁用确认、服务端也必须拒绝，绝不将空选择扩展为全部群。确认绑定本次正文、账号、勾选名单及连接，变化后重新预览；逐群记录结果，未知结果不得自动重发。开发测试只用模拟发送，真实群发需用户针对具体正文与名单另行确认。**
- 日常开发、修复与版本迭代统一沿用现有 `zhaoting.ding` 分支；未经用户明确要求，不按版本另建 `feature/*` 分支或新建工作树。开始修改前核对当前分支与未提交改动，保留已有工作。
- main 仅通过 PR 合入，由项目负责人审查。AGENTS.md、项目 Skill 与 CODEOWNERS 的改动同样适用；远程操作仍须遵守用户授权范围。
- 后续改动以小而精准、便于 PR 审阅为准。每笔提交围绕一个清楚的目的，包含使该改动可运行、可验证的必要代码与测试。独立的整理和功能改动分开提交。
- 架构与功能契约以 [docs/重构开发规范.md](docs/重构开发规范.md) 为准；现用安装与测试入口见 [README.md](README.md)。
- 应用配置统一 YAML，开发说明统一 Markdown；Python 打包、Git 和外部协议保留各自标准格式。
- 版本唯一来源为 `src/msgx/__init__.py`；中文本地提交，远程 push 需要单独授权。
- 测试使用隔离目录、数据库、端口和模拟凭据；真实聊天、QQ 登录及付费请求不作为默认测试输入。

## 项目技能索引

按当前任务选读匹配技能及必要参考资料；索引不要求每轮全部读取。

| 触发任务 | Skill |
| --- | --- |
| 群发二次确认、逐群结果、退群及消息清理 | [msgx-group-operations](.agents/skills/msgx-group-operations/SKILL.md) |
| 测试隔离、Electron／旧 Tk 验收、精简或迁移测试用例 | [msgx-maintenance](.agents/skills/msgx-maintenance/SKILL.md) |
| YAML 配置、偏好、API 配置与凭据保存 | [msgx-config-credentials](.agents/skills/msgx-config-credentials/SKILL.md) |
| 图片本地初筛、独立图片 API、自动分析限额及语义入库 | [msgx-image-analysis](.agents/skills/msgx-image-analysis/SKILL.md) |
| 消息过滤、活跃度、关键词、本人关联与 SQLite 查询或升级 | [msgx-message-queries](.agents/skills/msgx-message-queries/SKILL.md) |
| 日周月摘要、请求恢复、用量记账与费用口径 | [msgx-summary-accounting](.agents/skills/msgx-summary-accounting/SKILL.md) |
| Electron 桌面与 Python 通信、服务控制、扫码、阅读批注和朗读；旧 Tk 对照 | [msgx-desktop-ui](.agents/skills/msgx-desktop-ui/SKILL.md) |
| Windows 克隆安装、启动入口、打包及图标资源 | [msgx-windows-release](.agents/skills/msgx-windows-release/SKILL.md) |
