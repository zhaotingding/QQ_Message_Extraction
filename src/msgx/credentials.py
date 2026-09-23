"""Windows Generic Credentials；以完整 API 基础地址绑定密钥，无明文文件回退。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import os

from .api_settings import endpoint


class CredentialError(RuntimeError):
    pass


class _Credential(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]


def _api():
    if os.name != "nt":
        raise CredentialError("安全密钥存储需要 Windows 凭据管理器")
    dll = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    dll.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    dll.CredWriteW.restype = wintypes.BOOL
    dll.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_Credential))]
    dll.CredReadW.restype = wintypes.BOOL
    dll.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    dll.CredDeleteW.restype = wintypes.BOOL
    dll.CredFree.argtypes = [ctypes.c_void_p]
    return dll


def _target(url: str, profile_id: str = "") -> str:
    # openai 保持初版 0.7.0 的目标名，已有安全密钥无需重新输入。
    scope = "" if profile_id in ("", "openai") else profile_id + "\n"
    return "msgx/api/" + hashlib.sha256((scope + endpoint(url)).encode()).hexdigest()


def save(url: str, key: str, *, profile_id: str = "") -> None:
    key = key.strip()
    if not key or any(c.isspace() for c in key) or len(key.encode("utf-8")) > 2500:
        raise CredentialError("密钥为空、包含空白或长度不合法")
    dll = _api()
    blob = (ctypes.c_ubyte * len(key.encode("utf-8"))).from_buffer_copy(key.encode("utf-8"))
    credential = _Credential(Type=1, TargetName=_target(url, profile_id), CredentialBlobSize=len(blob),
                             CredentialBlob=blob, Persist=2, UserName="msgx")
    try:
        if not dll.CredWriteW(ctypes.byref(credential), 0):
            raise CredentialError("Windows 无法保存密钥；未写入任何明文配置")
    finally:
        ctypes.memset(blob, 0, len(blob))


def read(url: str, *, profile_id: str = "") -> str:
    dll = _api()
    pointer = ctypes.POINTER(_Credential)()
    if not dll.CredReadW(_target(url, profile_id), 1, 0, ctypes.byref(pointer)):
        if ctypes.get_last_error() == 1168:
            return ""
        raise CredentialError("Windows 无法读取密钥，请检查当前登录用户")
    try:
        value = pointer.contents
        return ctypes.string_at(value.CredentialBlob, value.CredentialBlobSize).decode("utf-8")
    finally:
        ctypes.memset(pointer.contents.CredentialBlob, 0, pointer.contents.CredentialBlobSize)
        dll.CredFree(pointer)


def delete(url: str, *, profile_id: str = "") -> None:
    if not _api().CredDeleteW(_target(url, profile_id), 1, 0) and ctypes.get_last_error() != 1168:
        raise CredentialError("Windows 无法删除该接口的密钥")
