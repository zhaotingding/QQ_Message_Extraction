"""一键运行全部测试。

用法（在项目根目录）：
    .venv\\Scripts\\python.exe tests\\run_all.py

刻意不依赖 pytest —— 本项目的设计原则是依赖越少越好，
而这三个测试都是自包含的脚本，用子进程逐个跑就能得到清晰的隔离。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent
PY = PROJ / ".venv" / "Scripts" / "python.exe"

SUITES = [
    ("大号分支与历史数据隔离", "test_accounts.py"),
    ("API 配置与密钥安全", "test_api_settings.py"),
    ("过滤规则", "test_filters.py"),
    ("可编辑发言人过滤名单", "test_sender_filters.py"),
    ("摘要流式响应与确认恢复", "test_summary_recovery.py"),
    ("计费口径", "test_cost.py"),
    ("逐请求消费明细", "test_usage.py"),
    ("每日消费趋势", "test_usage_chart.py"),
    ("LLM 调用层", "test_llm.py"),
    ("大摘要 HTTP 上传", "test_transport.py"),
    ("纪要长度", "test_digest.py"),
    ("自然日日报与手动入口停用", "test_daily_summary.py"),
    ("日周月摘要与月界材料", "test_period_summary.py"),
    ("摘要阅读元数据", "test_summary_review.py"),
    ("摘要批注与阅读界面", "test_reader_review_gui.py"),
    ("每日自动补做与日期接口", "test_daily_scheduler.py"),
    ("自动周期任务与失败恢复", "test_period_scheduler.py"),
    ("媒体校验", "test_media.py"),
    ("群列表属主", "test_groups.py"),
    ("当日群活跃度与群筛选", "test_group_activity.py"),
    ("实时消息详情与筛选界面", "test_realtime_gui.py"),
    ("无额外推理与过滤链路", "test_pipeline.py"),
    ("服务生命周期", "test_service.py"),
    ("服务日志与脱敏", "test_service_logging.py"),
    ("断线原因与重连", "test_connection.py"),
    ("缓存凭证自动登录", "test_login.py"),
    ("手动强制重启与进程隔离", "test_napcat_restart.py"),
    ("界面构建", "test_gui.py"),
    ("界面操作反馈", "test_gui_feedback.py"),
    ("Windows 图标实际像素", "test_icon_windows.py"),
    ("与我相关的消息", "test_related_messages.py"),
]


def main() -> int:
    if not PY.exists():
        print(f"找不到项目虚拟环境：{PY}")
        print("请先执行：")
        print("    py -3.13 -m venv .venv")
        print("    .venv\\Scripts\\python.exe -m pip install -r requirements.lock.txt")
        return 1

    results: list[tuple[str, int]] = []
    for title, script in SUITES:
        print("=" * 62)
        print(f"  {title}  ({script})")
        print("=" * 62)
        r = subprocess.run([str(PY), str(HERE / script)], cwd=str(PROJ))
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
