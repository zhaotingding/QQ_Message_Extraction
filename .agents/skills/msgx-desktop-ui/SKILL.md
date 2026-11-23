---
name: msgx-desktop-ui
description: 修改或诊断 msgx Electron 的页面导航、消息筛选与定位返回、桌面 IPC、接收启停、扫码和阅读交互时使用，亦适用于旧 Tk 对照。SQL 口径和安装打包分别选对应技能。
---

# 桌面交互与进程归属

默认界面为 Electron。主页、独立日志页和关键词定位按 2026-11-23 开发分支核对。`gui --legacy-tk` 用于旧界面对照。

## 先定位实际链路

| 当前问题 | 修改与读取入口 |
|---|---|
| 窗口、IPC、启动与退出 | `desktop/main.cjs`、`preload.cjs`、`bridge.py`；读[桌面参考](references/desktop.md)“现行 Electron 入口” |
| 主页消息与关键词定位返回 | 读[首页筛选与详情](references/desktop.md#首页筛选与详情)，核对当前页面、筛选条件和上下文。 |
| 设置、阅读或消费页面 | 阅读交互查 `desktop/renderer/reader.js` 和 `desktop/reader_api.py`。设置子页落盘见[配置技能](../msgx-config-credentials/SKILL.md)，原币统计见[摘要与记账](../msgx-summary-accounting/SKILL.md)。 |
| 旧 Tk 控件、坐标或历史故障 | `src/msgx/gui.py`、`ui/`；只读参考中的旧 Tk 部分，不将其当成 Electron 实现 |
| 安装、升级、EXE 或图标交付 | [Windows 安装与发布](../msgx-windows-release/SKILL.md) |

- Electron 页面经现有 preload/IPC 和 Python bridge 调用业务，复用服务、Store、配置和凭据接口；不在 renderer 另建发送、模型调用或 SQLite 路径。保留 `contextIsolation`、sandbox 与禁用 Node 集成的实际边界。
- 后台结果带任务、账号、文章或查询身份；过期结果不能覆盖当前页面。Tk 控件仅在其主线程操作，Electron 不照搬 Tk 定时器。深浅主题在各页面一致，选择局部保存并立即生效；保留图片调用绿底、关键词及本人消息提示，不把颜色当作成功证据。
- `ServiceProcess` 持有服务进程与恢复状态；Electron 由 bridge 管理，旧 Tk 由 `ui/service.py` 接入。只回收自己持有的进程树或明确 NapCat 目录，不按 Python/QQ 名称清场。异常退出按已有 5/15/30 秒、最多三次、稳定 300 秒重置策略恢复；手动停止、关闭、预期退出及退出码 0/2 不拉起，恢复只用已保存配置。
- 扫码完成须有目标账号在本次操作后的新群消息；心跳、群列表或历史计数不能替代。读取参考的二维码换代与失败重试细节后再改相关状态。
- 阅读保存成功后才切换或关闭，失败保留草稿；批注绑定内容版本和实际显示文本。Electron 的文本/字符偏移由 `reader_api.py` 与 `renderer/reader.js` 共同定义，不使用原 Markdown 或直接套 Tk 索引。HTML 缺失时读数据库正文，不调用模型补读。
- 朗读复用 `tts.Narrator` 的 STA 线程；COM 只在该线程使用，界面传普通数据。切换文章或关闭须停读；不重建另一套 SAPI。暂停、恢复、引擎不可用及历史 COM 陷阱按参考读取，保留重新尝试入口。
- 修改后验证所改界面的实际行为；Electron 用[隔离验收入口](../msgx-maintenance/SKILL.md)，仅 Tk 测试通过不能代表 Electron 通过。真实 QQ、凭据和收费调用不纳入默认桌面验收。
