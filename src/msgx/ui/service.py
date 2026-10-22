"""本地服务的 Tk 操作适配；由 App 组合，控件仅在主线程操作。"""
from __future__ import annotations

import threading
import time
from ..config import ROOT, abs_path
from ..local_client import request_json


class ServiceActions:
    def start_service(self, *, automatic: bool = False) -> bool:
        if self.proc is not None:
            return self.proc.poll() is None
        if not automatic:
            self._cancel_service_recovery()
            self.service.wanted = False
            if not self.save_prefs(silent=False):
                return False
            if self._login_config() is None:
                return False
            self.service.recoveries = 0

        self._log_ui("正在启动服务…\n")
        self._trace_gui("请求启动消息服务")
        try:
            self.service.spawn(root=ROOT, prefs_file=self.prefs_file,
                               stop_file=self.stop_file, config_path=self.config_path, automatic=automatic)
        except (OSError, RuntimeError) as exc:
            self.status.set(f"服务启动失败：{exc}")
            self._log_ui(f"[界面] 服务启动失败：{exc}\n")
            self._trace_gui(f"服务启动失败：{exc}")
            return False

        self.v_service.set("启动中")
        self.service.wanted = True
        self.service.recovering = automatic
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.status.set("服务进程已创建，正在等待接收端口就绪…")
        self._trace_gui(f"服务进程已创建 pid={self.proc.pid}")
        threading.Thread(target=self._read_pipe, args=(self.proc,), daemon=True).start()
        return True


    def stop_service(self) -> None:
        self.service.restart_cancelled = True
        self._cancel_service_recovery()
        if self.proc is None:
            self.service.wanted = False
            self.btn_stop.configure(state="disabled")
            self.status.set("已取消自动恢复，服务保持停止")
            return
        try:
            self.service.request_stop(self.stop_file)
            self._trace_gui("用户请求停止消息服务，已写入停止信号")
        except OSError as exc:
            self.status.set(f"服务停止失败：{exc}")
            self._log_ui(f"[界面] 服务停止失败：{exc}\n")
            return
        self.btn_stop.configure(state="disabled")
        self.status.set("已发出停止信号，正在等待服务安全退出…")


    def _read_pipe(self, proc):
        path = abs_path(self.cfg, "db_path").parent / "logs/service-output.log"
        self.service.read_output(proc, path, self.events)


    def _cancel_service_recovery(self) -> None:
        if self._service_restart_id is not None:
            self.root.after_cancel(self._service_restart_id)
            self._service_restart_id = None


    def _schedule_service_recovery(self) -> None:
        delay = self.service.recovery_delay()
        if delay is None:
            note = "接收服务连续异常退出，已停止自动恢复；请查看日志并手动启动服务"
        else:
            self._service_restart_id = self.root.after(delay * 1000, self._recover_service)
            self.btn_stop.configure(state="normal")
            note = f"接收服务异常退出，{delay} 秒后自动恢复（{self.service.recoveries}/3）；可点击停止服务取消"
        self.status.set(note)
        self._log_ui(f"[界面] {note}\n")
        self._trace_gui(note)


    def _recover_service(self) -> None:
        self._service_restart_id = None
        if self.closing or not self.service.wanted or self.proc is not None:
            return
        if not self.start_service(automatic=True):
            self.service.wanted = False
            self.btn_stop.configure(state="disabled")
            self.status.set("接收服务自动恢复未能启动；请查看日志并手动启动服务")


    def _tick_health(self) -> None:
        if not self.health_busy and self.proc is not None:
            self.health_busy = True
            threading.Thread(target=self._fetch_health, args=(self.proc,), daemon=True).start()
        elif self.proc is None:
            setup_hint = self._protocol_setup_hint()  # 未配置时先说明缺项，不引导反复点击启动。
            self.v_service.set("等待自动恢复" if self._service_restart_id is not None else
                               "待配置" if setup_hint else "未运行")
            self.v_peer.set("消息服务未运行 · 请完成设置" if setup_hint else "消息服务未运行 · 协议端无法接入")
            self.v_protocol_detail.set(setup_hint or "请启动服务；强制重启也会先启动接收服务。此状态不表示 QQ 登录失效。")
            self.v_stored.set("—")
            self.v_cost.set("—")
            self.v_total.set("—")
            self.v_balance.set("—")
            self.v_llm.set("—")
            self.v_summary_schedule.set("自动摘要：服务未运行，任务不会执行")
            self.v_summary_next.set("")
        self.root.after(2000, self._tick_health)
        if self.nb.select() == str(self.report_tab) and time.time() - self._report_history_checked >= 30:
            self.refresh_report_history()
        # 协议端日志是**快照**，不像服务日志那样有流 —— 显示着它就顺手刷新一下。
        # 读的是本地文件尾部，2 秒一次的开销可以忽略；不显示时一个字节都不读。
        if self.v_log_src.get() == "协议端日志":
            self._refresh_alt_log()


    def _fetch_health(self, proc) -> None:
        port = int(self.cfg["onebot"]["http_port"])
        data = None
        try:
            data = request_json(port, "/health", timeout=2.5)
        except Exception:  # noqa: BLE001
            data = None
        finally:
            self.health_busy = False
        self.events.put(("health", (proc, data)))
