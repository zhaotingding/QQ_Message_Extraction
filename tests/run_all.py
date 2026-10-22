"""一键运行全部测试。

用法（在项目根目录）：
    conda run -n msgx --no-capture-output python -s tests\\run_all.py

各组测试使用独立子进程运行，避免模拟对象和临时配置跨组泄漏。
现有脚本使用标准库 unittest 或行为断言，不需要额外测试框架。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent
sys.path.insert(0, str(PROJ / "src"))  # 套件不借用外层项目的运行入口。
from msgx import conda_runtime

SUITES = [
    ("项目 Conda 环境、轻量 EXE 与离线诊断", "test_startup.py"),
    ("存储查询与迁移工作量", "test_storage_queries.py"),
    ("后台只读与协议任务关闭", "test_onebot_reads.py"),
    ("YAML 配置与持久化", "test_yaml_config.py"),
    ("大号分支与历史数据隔离", "test_accounts.py"),
    ("API 配置与密钥安全", "test_api_settings.py"),
    ("过滤规则", "test_filters.py"),
    ("可编辑发言人过滤名单", "test_sender_filters.py"),
    ("摘要流式响应与确认恢复", "test_summary_recovery.py"),
    ("后台摘要断线与重启恢复", "test_background_summary.py"),
    ("计费口径", "test_cost.py"),
    ("逐请求消费明细", "test_usage.py"),
    ("每日消费趋势", "test_usage_chart.py"),
    ("LLM 调用层", "test_llm.py"),
    ("大摘要 HTTP 上传", "test_transport.py"),
    ("日周月摘要、完整材料与分块恢复", "test_period_summary.py"),
    ("摘要阅读元数据", "test_summary_review.py"),
    ("摘要批注与阅读界面", "test_reader_review_gui.py"),
    ("自动周期任务与失败恢复", "test_period_scheduler.py"),
    ("媒体校验", "test_media.py"),
    ("群列表属主", "test_groups.py"),
    ("当日群活跃度与群筛选", "test_group_activity.py"),
    ("实时消息详情与筛选界面", "test_realtime_gui.py"),
    ("关键词消息与只读历史检索", "test_message_keywords.py"),
    ("无额外推理与过滤链路", "test_pipeline.py"),
    ("服务生命周期", "test_service.py"),
    ("服务日志与脱敏", "test_service_logging.py"),
    ("接收服务异常恢复与原始输出", "test_service_recovery.py"),
    ("断线原因与重连", "test_connection.py"),
    ("二维码刷新与协议恢复", "test_login.py"),
    ("手动强制重启与进程隔离", "test_napcat_restart.py"),
    ("界面构建", "test_gui.py"),
    ("界面操作反馈", "test_gui_feedback.py"),
    ("Windows 图标实际像素", "test_icon_windows.py"),
    ("与我相关的消息", "test_related_messages.py"),
]


def main() -> int:
    try:
        conda_runtime.require_environment()
        prefix = conda_runtime.command()  # 每组测试显式进入同一个项目 Conda 环境。
    except RuntimeError as exc:
        print(str(exc))
        return 1

    env = os.environ.copy()
    env.update(MSGX_CONDA_ENV=conda_runtime.environment_name(), PYTHONNOUSERSITE="1", PYTHONPATH=str(PROJ / "src"))
    results: list[tuple[str, int]] = []
    for title, script in SUITES:
        print("=" * 62)
        print(f"  {title}  ({script})")
        print("=" * 62)
        r = subprocess.run([*prefix, str(HERE / script)], cwd=str(PROJ), env=env)
        results.append((title, r.returncode))
        print()

    print("=" * 62)
    print("  汇总")
    print("=" * 62)
    for title, rc in results:
        print(f"  [{'通过' if rc == 0 else '失败'}] {title}")

    failed = [t for t, rc in results if rc != 0]
    if failed:
        print(f"\n有 {len(failed)} 组测试失败：{', '.join(failed)}")
        return 1
    print("\n全部测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
