"""摘要朗读：通过 SAPI 自动化接口（IDispatch）驱动 Windows 语音合成，只用标准库。

为什么不用 pywin32 / pyttsx3：项目依赖表是精确锁定的，朗读属于可选能力，
不该为它给全团队增加安装负担。SAPI 是系统自带组件，ctypes 即可直调。

为什么**不**按 vtable 索引调 ISpVoice：索引取决于继承链
（IUnknown 3 + ISpNotifySource 7 + ISpEventSource 3，之后才是 ISpVoice），
手算错一格就会「调用返回成功、但一声不出」——1.0.4 首版把 Speak 写在 20 号槽
（实为 GetRate），正是这个坑。这里改为 `GetIDsOfNames` 按**方法名**解析，
索引由 SAPI 自己给（实测 Speak=12 / Status=1 / Rate=5 / Volume=6），不再依赖手算。

三条硬约束：
  * COM 是 STA —— 创建、朗读、暂停、释放必须在**同一个线程**里，所以朗读线程
    独占一个 voice 对象，主线程只投递命令、接收事件，不直接碰 COM。
  * BSTR 指针必须声明 restype：SysAllocString 默认按 32 位 int 返回，
    x64 上会把指针高位截掉，调用时直接 access violation。
  * ``ISpVoice::Pause`` 是**粘性**的 —— 暂停后 ``Speak`` 只把文本排进队列，
    ``RunningState`` 永远停在 0（既不发声也不结束）。实测确认：暂停过之后
    不再 ``Resume`` 就再也读不出声，线程还会卡死在该段。每次开读前必须复位。
"""
from __future__ import annotations

import ctypes
import queue
import re
import threading
import time
import uuid
from ctypes import (POINTER, Structure, Union, byref, c_byte, c_double, c_long,
                    c_longlong, c_short, c_ulong, c_ushort, c_void_p, c_wchar_p)
from dataclasses import dataclass

CLSID_SPVOICE = "96749377-3391-11D2-9EE3-00C04F797396"
IID_IDISPATCH = "00020400-0000-0000-C000-000000000046"
IID_NULL = "00000000-0000-0000-0000-000000000000"

DISPATCH_METHOD = 1
DISPATCH_PROPERTYGET = 2
DISPATCH_PROPERTYPUT = 4
DISPATCH_PROPERTYPUTREF = 8
DISPID_PROPERTYPUT = -3

VT_I4 = 3
VT_BSTR = 8
VT_DISPATCH = 9
VT_BOOL = 11

SVSF_ASYNC = 1
SVSF_PURGE = 2
# ISpVoice::GetStatus 的 dwRunningState（SPRUNSTATE），探针实测值：
#   0 = 已入队但还没发声（暂停态下 Speak 会永远停在这里）
#   1 = 读完   2 = 正在说   3 = 读完且队列已空（读完瞬间的过渡值）
SRS_IDLE = 0
SRS_DONE = 1
SRS_SPEAKING = 2
SRS_DONE_WITH_QUEUE = 3

# 一段文本入队后迟迟不进入「正在说」就判定引擎卡住（典型原因：暂停没解除、
# 音频设备被抢占）。留足余量，正常的短句几毫秒就会跳到 2，不会误判。
STALL_SECONDS = 3.0

# 单段朗读上限（字符）。超过就按句切开：SAPI 一次喂太长会让「停止」的响应变钝，
# 高亮也会长时间停在同一段上。
SEGMENT_LIMIT = 440

_SPEAK_MEMBERS = ("Speak", "Pause", "Resume", "Status", "Rate", "Volume")

_SENTENCE_RE = re.compile(r"[^。！？；!?;]*[。！？；!?;]|[^。！？；!?;]+$")
_NOT_SPEECH_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")
_RULE_RE = re.compile(r"^[-*_=~#\s]{3,}$")


@dataclass(frozen=True)
class Segment:
    """一段可朗读文本，以及它在 Tk 正文里的字符区间（用于高亮）。"""

    text: str
    start: int
    end: int


# ---------- 文本整理 ----------

def clean_line(line: str) -> str:
    """把一行 Markdown 整理成适合朗读的口语句子；没有可读内容时返回空串。"""
    text = (line or "").strip()
    if not text or _RULE_RE.match(text):
        return ""
    if text.startswith("|") and re.fullmatch(r"\|?[\s:|-]+\|?", text):
        return ""  # 表格分隔行 | --- | --- |，读出来只有噪音
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)          # 图片整条丢弃
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)      # 链接只留文字
    text = re.sub(r"^\s*#{1,6}\s*", "", text)                 # 标题号
    text = re.sub(r"^\s*[-*+•]\s+", "", text)                 # 列表符号
    text = re.sub(r"^\s*\d+[.、)]\s*", "", text)              # 有序列表号
    text = re.sub(r"^\s*>\s?", "", text)                      # 引用号
    for mark in ("**", "__", "`"):
        text = text.replace(mark, "")
    text = text.replace("|", "，")                            # 表格竖线当作停顿
    text = re.sub(r"\s+", " ", text).strip()
    return text if _NOT_SPEECH_RE.search(text) else ""


def segments_from_text(text: str, limit: int = SEGMENT_LIMIT) -> list[Segment]:
    """按行切段；区间取**原始行**的偏移，所以清理掉的 ** 符号不会让高亮错位。"""
    out: list[Segment] = []
    pos = 0
    for raw in (text or "").splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        start = pos
        pos += len(raw)
        body = clean_line(line)
        if not body:
            continue
        if len(body) <= limit:
            out.append(Segment(body, start, start + len(line)))
            continue
        for match in _SENTENCE_RE.finditer(line):
            piece = clean_line(match.group(0))
            if piece:
                out.append(Segment(piece, start + match.start(), start + match.end()))
    return out


# ---------- IDispatch 最小客户端 ----------

class _Guid(Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", c_ushort), ("Data3", c_ushort),
                ("Data4", c_byte * 8)]


def _guid(text: str) -> _Guid:
    value = uuid.UUID(text)
    return _Guid(value.time_low, value.time_mid, value.time_hi_version,
                 (c_byte * 8)(*value.bytes[8:]))


class _VariantUnion(Union):
    _fields_ = [("llVal", c_longlong), ("dblVal", c_double), ("bstrVal", c_void_p),
                ("pdispVal", c_void_p), ("lVal", c_long), ("ulVal", c_ulong),
                ("boolVal", c_short), ("byref", c_void_p), ("pad", c_byte * 16)]


class _Variant(Structure):
    _anonymous_ = ("u",)
    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort),
                ("u", _VariantUnion)]


class _DispParams(Structure):
    _fields_ = [("rgvarg", POINTER(_Variant)), ("rgdispidNamedArgs", POINTER(c_long)),
                ("cArgs", c_ulong), ("cNamedArgs", c_ulong)]


_ole_cache: tuple | None = None


def _ole() -> tuple:
    """惰性加载 ole32/oleaut32，并声明签名（不声明 BSTR 返回类型会在 x64 上崩）。"""
    global _ole_cache
    if _ole_cache is not None:
        return _ole_cache
    ole32 = ctypes.WinDLL("ole32")
    oleaut32 = ctypes.WinDLL("oleaut32")
    oleaut32.SysAllocString.restype = c_void_p
    oleaut32.SysAllocString.argtypes = [c_wchar_p]
    oleaut32.SysFreeString.restype = None
    oleaut32.SysFreeString.argtypes = [c_void_p]
    ole32.CoInitialize.restype = c_long
    ole32.CoInitialize.argtypes = [c_void_p]
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.restype = c_long
    ole32.CoCreateInstance.argtypes = [POINTER(_Guid), c_void_p, c_ulong,
                                       POINTER(_Guid), POINTER(c_void_p)]
    _ole_cache = (ole32, oleaut32)
    return _ole_cache


def _hresult(hr: int) -> str:
    return f"{hr & 0xFFFFFFFF:#010x}"


class _Dispatch:
    """一个 IDispatch 指针：按方法名解析 DISPID，再 Invoke。"""

    def __init__(self, ptr) -> None:
        self.ptr = ptr if isinstance(ptr, c_void_p) else c_void_p(ptr)
        _, oleaut32 = _ole()
        self._oleaut32 = oleaut32
        self._null = _guid(IID_NULL)
        vtbl = ctypes.cast(self.ptr, POINTER(POINTER(c_void_p)))[0]
        self._get_ids = ctypes.WINFUNCTYPE(c_long, c_void_p, POINTER(_Guid),
                                          POINTER(c_wchar_p), c_ulong, c_ulong,
                                          POINTER(c_long))(vtbl[5])
        self._invoke = ctypes.WINFUNCTYPE(c_long, c_void_p, c_long, POINTER(_Guid), c_ulong,
                                          c_ushort, POINTER(_DispParams), POINTER(_Variant),
                                          c_void_p, c_void_p)(vtbl[6])
        self._release_fn = ctypes.WINFUNCTYPE(c_ulong, c_void_p)(vtbl[2])
        self._ids: dict[str, int] = {}

    def dispid(self, name: str) -> int:
        cached = self._ids.get(name)
        if cached is not None:
            return cached
        out = c_long()
        hr = self._get_ids(self.ptr, byref(self._null), (c_wchar_p * 1)(name), 1, 0, byref(out))
        if hr != 0 or out.value == 0:
            raise AttributeError(f"SAPI 接口没有成员 {name}")
        self._ids[name] = out.value
        return out.value

    def _call(self, dispid: int, flags: int, args: tuple, want_result: bool) -> _Variant:
        holder = (_Variant * max(1, len(args)))()
        # DISPPARAMS 的 rgvarg 是**逆序**的：rgvarg[0] 对应最后一个实参。
        for i, (kind, value) in enumerate(reversed(args)):
            holder[i].vt = kind
            if kind == VT_BSTR:
                holder[i].bstrVal = self._oleaut32.SysAllocString(value)
            elif kind == VT_BOOL:
                holder[i].boolVal = -1 if value else 0
            else:
                holder[i].lVal = int(value)
        params = _DispParams()
        params.rgvarg = ctypes.cast(holder, POINTER(_Variant))
        params.cArgs = len(args)
        named = None
        if flags & (DISPATCH_PROPERTYPUT | DISPATCH_PROPERTYPUTREF):
            named = (c_long * 1)(DISPID_PROPERTYPUT)
            params.rgdispidNamedArgs = ctypes.cast(named, POINTER(c_long))
            params.cNamedArgs = 1
        result = _Variant()
        try:
            hr = self._invoke(self.ptr, dispid, byref(self._null), 0, flags, byref(params),
                              byref(result) if want_result else None, None, None)
        finally:
            for i, (kind, _value) in enumerate(reversed(args)):
                if kind == VT_BSTR and holder[i].bstrVal:
                    self._oleaut32.SysFreeString(holder[i].bstrVal)
                    holder[i].bstrVal = None
        if hr != 0:
            raise OSError(f"SAPI 调用失败（hr={_hresult(hr)}）")
        return result

    def method(self, name: str, args: tuple = (), want_result: bool = False) -> _Variant:
        return self._call(self.dispid(name), DISPATCH_METHOD, args, want_result)

    def get(self, name: str) -> _Variant:
        return self._call(self.dispid(name), DISPATCH_PROPERTYGET, (), True)

    def put(self, name: str, value: int) -> None:
        self._call(self.dispid(name), DISPATCH_PROPERTYPUT, ((VT_I4, value),), False)

    def release(self) -> None:
        ptr, self.ptr = self.ptr, None
        if ptr:
            self._release_fn(ptr)


# ---------- COM 封装 ----------

class SapiVoice:
    """一个 SAPI 语音对象；只能在其创建线程里调用（见模块 docstring）。"""

    def __init__(self) -> None:
        ole32, _ = _ole()
        ole32.CoInitialize(None)  # 每个用到 COM 的线程各初始化一次；重复调用返回 S_FALSE 且无害。
        self._com_ready = True
        self._api: _Dispatch | None = None
        ptr = c_void_p()
        hr = ole32.CoCreateInstance(byref(_guid(CLSID_SPVOICE)), None, 1,
                                    byref(_guid(IID_IDISPATCH)), byref(ptr))
        if hr != 0 or not ptr.value:
            ole32.CoUninitialize()
            self._com_ready = False
            raise OSError(f"无法创建 SAPI 语音对象（hr={_hresult(hr)}）")
        api = _Dispatch(ptr)
        for name in _SPEAK_MEMBERS:  # 一次性确认成员齐全，缺就直接降级而不是半途炸
            api.dispid(name)
        self._api = api

    @classmethod
    def create(cls, rate: int = 0, volume: int = 100) -> "SapiVoice | None":
        """打不开语音引擎（非 Windows、SAPI 被裁剪）时返回 None，由调用方降级。"""
        try:
            voice = cls()
        except (OSError, AttributeError, ValueError):
            return None
        try:
            voice.set_rate(rate)
            voice.set_volume(volume)
        except (OSError, AttributeError):
            voice.close()
            return None
        return voice

    @property
    def available(self) -> bool:
        api = getattr(self, "_api", None)
        return bool(api is not None and api.ptr)

    def speak(self, text: str) -> None:
        self._api.method("Speak", ((VT_BSTR, text), (VT_I4, SVSF_ASYNC)))

    def state(self) -> int:
        """返回 SRS_DONE(1) / SRS_SPEAKING(2)；读取失败返回 -1。

        Status 每次返回一个新的自动化对象，读完必须 Release，否则 COM 引用计数只涨不落。
        """
        try:
            holder = self._api.get("Status")
            ptr = holder.pdispVal
            if not ptr:
                return -1
            status = _Dispatch(ptr)
            try:
                return int(status.get("RunningState").lVal)
            finally:
                status.release()
        except (OSError, AttributeError, ValueError):
            return -1

    def pause(self) -> None:
        self._api.method("Pause")

    def resume(self) -> None:
        self._api.method("Resume")

    def stop(self) -> None:
        # 空串 + PURGE：立即清空发声队列，不把剩余内容读完。
        self._api.method("Speak", ((VT_BSTR, ""), (VT_I4, SVSF_PURGE | SVSF_ASYNC)))

    def set_rate(self, rate: int) -> None:
        self._api.put("Rate", max(-10, min(10, int(rate))))

    def set_volume(self, volume: int) -> None:
        self._api.put("Volume", max(0, min(100, int(volume))))

    def close(self) -> None:
        api, self._api = getattr(self, "_api", None), None
        if api is not None:
            try:
                api.method("Speak", ((VT_BSTR, ""), (VT_I4, SVSF_PURGE | SVSF_ASYNC)))
            except (OSError, AttributeError):
                pass
            api.release()
        if getattr(self, "_com_ready", False):
            try:
                _ole()[0].CoUninitialize()
            except (OSError, AttributeError):
                pass
            self._com_ready = False


# ---------- 朗读线程 ----------

class _Generation:
    """一代朗读线程：独占一个命令队列，并带独立的停止信号。

    每一代都有自己的队列，所以即使上一代卡在 COM 调用里没能及时退出，它
    也不可能再抢到新一代的 play —— 否则两个线程轮流取命令，听感就是
    「有时候点了没声音」。停止信号让旧线程返回后立刻自行结束。
    """

    __slots__ = ("cmds", "stopping", "thread")

    def __init__(self) -> None:
        self.cmds: queue.Queue = queue.Queue()
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None


def _drain_cmds(q: queue.Queue):
    while True:
        try:
            yield q.get_nowait()
        except queue.Empty:
            return


def _segment_timeout(text: str) -> float:
    """一段最多允许「正在说」多久；真读完会先回 DONE，超时即视为引擎卡住。"""
    return min(120.0, 8.0 + len(text or "") * 0.6)


class Narrator:
    """朗读调度：主线程投递 play/pause/resume/stop，后台线程独占 COM 推进播放。

    ``notify(kind, value)`` 由后台线程调用，实现方必须只做线程安全投递
    （界面里就是 ``events.put``），不得直接碰控件。
    """

    def __init__(self, notify, voice_factory=SapiVoice.create, rate: int = 0, volume: int = 100) -> None:
        self._notify = notify
        self._factory = voice_factory
        self._rate = rate
        self._volume = volume
        self._lock = threading.Lock()
        self._gen: _Generation | None = None
        self.state = "idle"
        self.index = 0
        self.total = 0

    # --- 主线程侧 ---

    def play(self, segments: list[str], start: int = 0, rate: int | None = None) -> bool:
        texts = [text for text in (segments or []) if text and text.strip()]
        if not texts:
            return False
        gen = self._spawn()
        gen.cmds.put(("play", {"segments": texts, "start": max(0, min(int(start), len(texts) - 1)),
                               "rate": rate}))
        return True

    def pause(self) -> None:
        self._put(("pause", None))

    def resume(self) -> None:
        self._put(("resume", None))

    def stop(self) -> None:
        self._put(("stop", None))

    def set_rate(self, rate: int) -> None:
        """改语速：正在读的段也会立刻跟着变（SAPI 支持运行中改速率）。"""
        self._rate = int(rate)
        self._put(("rate", int(rate)))

    def close(self) -> None:
        with self._lock:
            gen, self._gen = self._gen, None
        if gen is None:
            return
        gen.stopping.set()  # 旧线程即使此刻卡在 COM 里，返回后也会立刻自行退出
        gen.cmds.put(("quit", None))
        thread = gen.thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)

    def _put(self, item) -> None:
        gen = self._current()
        if gen is not None:
            gen.cmds.put(item)

    def _current(self) -> _Generation | None:
        gen = self._gen
        return gen if gen is not None and gen.thread is not None and gen.thread.is_alive() else None

    def _spawn(self) -> _Generation:
        with self._lock:
            gen = self._gen
            if gen is not None and gen.thread is not None and gen.thread.is_alive():
                return gen
            gen = _Generation()
            gen.thread = threading.Thread(target=self._run, args=(gen,), name="msgx-tts", daemon=True)
            self._gen = gen
            gen.thread.start()
            return gen

    def _emit(self, kind: str, value) -> None:
        try:
            self._notify(kind, value)
        except Exception:  # noqa: BLE001 - 界面已关闭时投递失败不能拖死朗读线程
            pass

    # --- 后台线程侧 ---

    def _run(self, gen: _Generation) -> None:
        def open_voice():
            try:
                return self._factory(self._rate, self._volume)
            except Exception:  # noqa: BLE001 - 引擎建不起来就降级，不能把线程炸掉
                return None

        def reopen():
            """丢弃当前语音对象重建一个：设备被抢占、对象处于异常态时用它兜底。"""
            nonlocal voice
            try:
                voice.close()
            except Exception:  # noqa: BLE001
                pass
            voice = open_voice()
            return voice

        voice = open_voice()
        if voice is None:
            self.state = "unavailable"
            self._emit("tts_state", ("unavailable", "系统未安装可用的语音合成引擎"))
            return

        segments: list[str] = []
        index = 0
        speaking = False
        paused = False
        started_at = 0.0
        stalls = 0
        bad_reads = 0
        try:
            while not gen.stopping.is_set():
                for cmd, payload in _drain_cmds(gen.cmds):
                    if cmd == "quit":
                        try:
                            voice.stop()
                        except (OSError, AttributeError):
                            pass
                        self._emit("tts_state", ("idle", ""))
                        return
                    if cmd == "play":
                        try:
                            voice.stop()    # 清掉上一轮残留（暂停时排队的文本也要丢）
                            voice.resume()  # 关键：解除粘性暂停，否则新文本永远不发声
                            if payload.get("rate") is not None:
                                voice.set_rate(payload["rate"])
                        except (OSError, AttributeError):
                            if reopen() is None:
                                self.state = "unavailable"
                                self._emit("tts_state", ("unavailable", "语音引擎无响应，已停止朗读"))
                                return
                        segments = payload["segments"]
                        index = payload["start"]
                        speaking, paused = False, False
                        stalls = bad_reads = 0
                        self.total = len(segments)
                        self.state = "playing"
                        self._emit("tts_state", ("playing", ""))
                    elif cmd == "pause":
                        if speaking and not paused:
                            paused = True
                            self.state = "paused"
                            try:
                                voice.pause()
                            except (OSError, AttributeError):
                                pass
                            self._emit("tts_state", ("paused", ""))
                    elif cmd == "resume":
                        if speaking and paused:
                            paused = False
                            started_at = time.time()  # 暂停时长不计入「这一段读了多久」
                            self.state = "playing"
                            try:
                                voice.resume()
                            except (OSError, AttributeError):
                                pass
                            self._emit("tts_state", ("playing", ""))
                    elif cmd == "stop":
                        if speaking:
                            try:
                                voice.stop()
                            except (OSError, AttributeError):
                                pass
                        segments, index, speaking, paused = [], 0, False, False
                        self.state = "idle"
                        self._emit("tts_state", ("idle", ""))
                    elif cmd == "rate":
                        try:
                            voice.set_rate(payload)
                        except (OSError, AttributeError):
                            pass

                if not segments or index >= len(segments):
                    if segments:
                        segments, speaking, paused = [], False, False
                        self.state = "idle"
                        self._emit("tts_state", ("idle", ""))
                    time.sleep(0.05)
                    continue

                if not speaking:
                    self.index = index
                    try:
                        voice.speak(segments[index])
                    except (OSError, AttributeError):
                        if reopen() is None:
                            self.state = "unavailable"
                            self._emit("tts_state", ("unavailable", "语音引擎无响应，已停止朗读"))
                            return
                        try:
                            voice.stop()
                            voice.resume()
                            voice.speak(segments[index])
                        except (OSError, AttributeError):
                            self.state = "unavailable"
                            self._emit("tts_state", ("unavailable", "语音引擎无法发声，已停止朗读"))
                            return
                    speaking, paused = True, False
                    started_at = time.time()
                    stalls = bad_reads = 0
                    self._emit("tts_seg", (index, len(segments)))
                    time.sleep(0.02)
                    continue

                # 段刚开始的瞬间状态可能还停在上一段的 DONE，先给 SAPI 一点起步时间。
                if time.time() - started_at < 0.2:
                    time.sleep(0.05)
                    continue
                state = voice.state()
                elapsed = time.time() - started_at
                if state in (SRS_DONE, SRS_DONE_WITH_QUEUE):
                    speaking = False
                    index += 1
                elif state < 0:
                    # 单次读不到状态不算故障（COM 忙），连续读不到才判引擎失效。
                    bad_reads += 1
                    if bad_reads >= 40:
                        self.state = "unavailable"
                        self._emit("tts_state", ("unavailable", "语音引擎状态读取失败，已停止朗读"))
                        segments, index, speaking, paused = [], 0, False, False
                elif not paused and ((state == SRS_IDLE and elapsed > STALL_SECONDS)
                                     or (state == SRS_SPEAKING and elapsed > _segment_timeout(segments[index]))):
                    # 已入队却始终不发声：上一版就是对这种状态不设超时，结果永久卡在
                    # 该段、一点声音都没有。先重建语音对象重试一次，仍不行就明确报错。
                    if stalls == 0 and reopen() is not None:
                        stalls = 1
                        started_at = time.time()
                        try:
                            voice.speak(segments[index])
                            continue
                        except (OSError, AttributeError):
                            pass
                    self.state = "unavailable"
                    self._emit("tts_state", ("unavailable", "语音引擎没有发声，已停止朗读"))
                    segments, index, speaking, paused = [], 0, False, False
                time.sleep(0.05)
        finally:
            try:
                voice.close()
            except Exception:  # noqa: BLE001
                pass
