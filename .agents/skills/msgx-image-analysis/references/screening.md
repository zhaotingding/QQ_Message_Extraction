# 图片分析的调用边界

适用：1.0.0；检查日期：2026-10-31。源码是当前行为依据，历史测试不保证真实图片语义准确。

- `media.py` 下载/复用 OCR；`image_policy.py` 只对 OCR 与附近文字评分，表情类型优先于内容分数。
- `image_analysis.py` 独立串行处理本地初筛及自动候选；`image_analysis_store.py` 复用 Store 连接保存分数、理由、claim 与结果未知状态。字段迁移不重跑旧图片下载。
- 默认关闭自动上传，阈值70、每天20张、1元本地估算上限，近7天候选。预算不是供应商硬封顶；单次预留采用有界输入和实际图片输出上限，未知用量暂停当天自动队列。
- 账号依据接收凭据，当前群依据 `account_group_snapshots`；多账号共收同一父消息时，上下文必须使用当前接收账号，不能只用父行最早的 received_by。
- 同图语义仅在相同群、相同配文及近邻时间内复用，防止将另一段讨论的上下文解释误贴过来。已有 OCR 则可按图片摘要复用。
- 已保存的 `image_api_profile` 与 `active_api_profile` 分别决定图片和摘要；LLM.snapshot(role="image") 为图片冻结配置，Windows凭据仍按 profile 与基础地址隔离。配置中未在界面展示的参数须保留，不能保存时被默认值覆盖。
- `自动图片理解` 与 `提示词更新` 都进入现有用量账本和消费页面；图片自动队列的 running/unknown 不因重开而变回未尝试。
- 相关隔离覆盖：`tests/test_image_analysis.py`、`test_automation_gui.py`、`test_media.py`、`test_api_settings.py`；完整套件与测量记录见本地忽略目录 `data/diagnostics/v1.0.0/`。不把模拟API或规则案例当作真实付费识别准确率。
