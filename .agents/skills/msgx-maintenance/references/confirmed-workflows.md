# 测试隔离与覆盖迁移

适用：0.9.9；经验验证日期：2026-10-15。以下路径相对仓库根目录；本地忽略的验收产物不会随克隆分发，缺失时核对源码或历史提交，不将缺失解释为通过。

## 导入前隔离

`config.ROOT` 在模块导入时确定。旧 GUI 综合测试只替换数据库，仍会向项目 `data/logs/gui.log` 和 VBS 位置写文件。现 `tests/test_gui.py` 在导入前设置临时 `MSGX_ROOT/MSGX_PREFS`，使用 `ExitStack` 关闭控件、取消定时回调、还原补丁及回收目录；`test_gui_feedback.py` 还隔离初始化时的图标、协议目录和端口。

服务测试需清除继承的 `MSGX_ROOT/MSGX_PREFS`、模型密钥及不适用代理，显式关闭模型与真实 QQ 看门狗。仅禁用 LLM 不能证明外层偏好没有覆盖测试配置。正常退出后读取并关闭管道，再删除临时库。

- 证据：`tests/test_gui.py`、`test_gui_feedback.py`、`test_service.py`。
- 级别：2026-10-15 实际 Tk 和本机隔离服务验证；记录在 `data/diagnostics/v0.9.9/ui/`、`service-final-review.log`。未操作生产 QQ 或真实模型。

## 删除旧脚本前保留行为

0.9.9 清理旧手动摘要测试时，材料超过8000条、逐图 OCR/语义、长文本尾部、分块恢复、来源ID、完整HTML和失败残稿检查迁入 `test_period_summary.py`；日期接口移入 `test_summary_recovery.py`。历史 `partial` 报告的读取兼容与真实 `daily_status()` 的 done/missing/empty、跨日和只读检查仍需保留，不能仅以接口 Mock 代替。

发现并修正的无效写法：`check(..., True)` 永远通过；用实际输出作为字典键可能覆盖失败用例；WS客户端等待后检查“有HOLDING输出或进程已退出”会把连接失败当作成功。改为有序输入/期望对及真实连接证据。

- 证据：`tests/test_groups.py`、`test_gui.py`、`test_service.py`、`test_period_summary.py`。
- 级别：2026-10-15 隔离回归与独立审查后定向复验；`data/diagnostics/v0.9.9/all-tests.log`、`summary-final-review.log`、`service-final-review.log`。该次结果不是后续改动的通过证明。

## 选择执行入口

当前入口约定更新于2026-10-19：一项目一个独立Conda环境，默认 `msgx`。相关组使用 `conda run -n msgx --no-capture-output python -s tests\test_<domain>.py`；确有集成验收需要时使用 `conda run -n msgx --no-capture-output python -s tests\run_all.py`。不再调用旧 `.venv`，不使用 `base` 或用户site-packages；自定义项目环境名须与 `MSGX_CONDA_ENV` 保持一致。Windows控制台需要UTF-8时，仅为测试子进程设置 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`；不修改系统编码。上文2026-10-15历史验收不代表当前Conda入口已通过。

Windows受限执行器曾因新临时目录ACL拒绝SQLite写入和清理。这是当时执行环境的确认限制；先核对具体错误，若确属该限制且测试范围已授权，可在正常用户权限下运行隔离测试，不修改应用以迁就沙箱，也不把它作为所有失败的解释。
