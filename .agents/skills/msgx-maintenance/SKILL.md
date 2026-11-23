---
name: msgx-maintenance
description: 选择或维护 msgx 的隔离测试、Electron 交互验收、旧 Tk 回归及测试覆盖迁移时使用。不作为所有业务修改的前置技能，不处理生产运行与发布。
---

# 隔离回归与测试整理

本技能负责项目测试入口与隔离经验。2026-11-16 按 README、测试文件和现行桌面入口静态校准；旧运行结果只证明原日期的对应范围。

## 按受影响行为选入口

命令在仓库根目录的普通 PowerShell、项目独立 Conda 环境执行；默认环境名 `msgx`，沿用项目已确定的实际名称。

| 改动 | 验证入口与范围 |
|---|---|
| Python 业务或缺陷 | `conda run -n msgx --no-capture-output python -s tests/test_<domain>.py`，先选能够暴露该行为的现有组 |
| Electron 页面、IPC、桌面后端或接收进程联动 | `conda run -n msgx --no-capture-output node tests/electron_smoke.cjs`。父脚本打开隔离窗口，并向 reader、settings、messages 子检查传入同一窗口的验收接口。 |
| 旧 Tk 对照界面 | 对应 `test_gui*`、`test_realtime_gui.py` 或阅读/主题组；结果只覆盖旧 Tk |
| 跨模块整合 | 确有必要时运行 `conda run -n msgx --no-capture-output python -s tests/run_all.py`；该清单不替代 Electron 入口 |
| 安装、更新或启动器 | [安装与发布](../msgx-windows-release/SKILL.md)中的相应隔离验证，不因普通业务改动重装 |

- 先核对入口的依赖与隔离方式，不把运行命令当成通过。Electron 入口使用合成资料、临时端口和模拟群发；不得换成生产资料或真实发送来弥补用例缺口。
- 导入应用前隔离 `MSGX_ROOT`、`MSGX_PREFS`；只改数据库路径仍可能写真实日志、VBS 或读个人图标。使用凭据替身并隔离网络；清除不适用的外层偏好、密钥与代理，不读取真实凭据验证隔离。
- 服务验收用临时库、随机端口、测试协议目录，禁用真实 QQ 恢复与模型请求。收尾等待所属进程、关闭管道和页面/Tk 回调后，再回收本次临时目录，不影响已有运行实例。
- 整理测试前对照当前调用链，区分废弃、重复与独有覆盖。完整材料、恢复、费用或状态检查迁到现用入口后才能删旧用例；不靠新增文件数证明质量，也不为适配旧断言改回废弃业务。
- 断言实际结果：相同字典键不能覆盖失败检查；进程退出不等于连接建立，保存受理不等于落盘。凭据实写等显式跳过项不记通过。
- 分别报告模拟接口、实际 Electron/Tk、本机 HTTP 与真实平台的覆盖；必要检查通过后交付，不因加载技能重跑全套。
- 核对单页验收时，从父脚本确认相应子检查实际执行。直接用 Node 打开 `electron_settings_smoke.cjs` 等导出模块不会运行断言。Python 的 `test_usage.py` 则是带退出码的独立断言脚本，按脚本入口执行。2026-11-23 核对。

涉及导入隔离、旧断言或测试迁移时，读取[历史问题与覆盖依据](references/confirmed-workflows.md)对应部分。业务预期仍以当前业务契约为准。
