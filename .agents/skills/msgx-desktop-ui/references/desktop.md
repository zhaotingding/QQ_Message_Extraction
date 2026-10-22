# 桌面流程的隐藏约束

适用：0.9.9；证据路径相对仓库根目录。以下日期描述当时验证，不代表本轮重新运行生产实例。

## 进程与扫码

当前入口约定更新于2026-10-19：主EXE通过本项目独立Conda环境启动，Conda包装PID可能与实际Python PID不同；核对实际解释器、父子进程关系与创建时间，不将包装PID直接当成服务Python PID。清理针对本窗口持有的Popen树；强制重启按明确NapCat安装目录和目标账号处理，保留普通QQ及其它安装实例。历史Windows venv启动器也曾出现包装PID与服务PID不同，其历史验收不能替代当前Conda链路验证。`ECONNREFUSED :6700` 先表示接收端不可连接，不能直接归因为QQ掉线。

GUI和协议操作共用 `napcat.resolve_dir()`。二维码监视在操作前固定时间基线，使用 `qr_gen` 丢弃旧线程事件；图片未写完或展示失败不推进shown mtime，同一码后续重试。只有目标账号本次的新消息确认完成，不能由“连接成功”先关码。

首次待配置提示使用已保存账号与共用目录解析结果；输入框草稿、共享启动脚本存在都不代表个人配置齐全。实际连接、掉线和扫码状态优先于待配置提示，状态刷新不得替用户启动或保存。2026-10-20在独立Conda环境/Python3.13.16中通过 `tests/test_gui_feedback.py`（20项）、`tests/test_gui.py` 和 `tests/test_service_recovery.py`（17项）；测试使用隔离资料与模拟协议/进程，包含真实Tk控件，不代表生产GUI/EXE启动或真实QQ业务已验收。证据：`data/diagnostics/manual-install-20261020/test-results.yaml` 及同目录原始日志。

服务退出0/2、用户停止、关闭与重启中的预期退出不触发有限恢复；恢复不保存编辑框草稿。协议群同步与断线监视Task须在Store关闭前取消并等待。

- 2026-10-13 实际隔离Tk/服务恢复、收消息和停止：`data/diagnostics/v0.9.4.1/service-e2e.json`。
- 2026-10-15 模拟协议/进程与实际Tk回归：`tests/test_service_recovery.py`、`test_napcat_restart.py`、`test_login.py`、`test_connection.py`、`test_gui.py`。不等于再次真实手机扫码。
- 2026-10-12历史两次服务退出码1的触发源仍未确认。保留 `data/logs/{gui,service,service-output,service-fault}.log` 排查；底层exit或外部结束可能无Python栈，不能把恢复机制通过当作历史根因已查明。

## 批注、阅读与反馈

保存返回False可表示仍在后台等待锁。只有真实落库成功才执行切换/关闭的继续动作；失败保留文章与草稿。读回批注携带文章身份和请求序号，旧结果不能覆盖新内容。

坐标与内容版本关联。Tk对emoji的字符索引可能不同于Python切片，恢复时核对选中文字。HTML丢失时从 `summaries.content` 读取；不得为了阅读重新生成。

- 2026-10-15 实际SQLite写锁与Tk交互验证：`tests/test_reader_review_gui.py`，记录在 `data/diagnostics/v0.9.9/ui/reader-after.log`。锁等待期间界面响应，失败保留草稿。
- 保存配置、失败、取消和忙碌反馈：`tests/test_gui_feedback.py`；证据为同目录 `feedback-after.log`。本次技能整理未再次运行这些业务测试。
