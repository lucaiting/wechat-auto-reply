# -*- coding: utf-8 -*-
"""Windows DPAPI 加解密（零依赖，ctypes 调 crypt32.dll）。

用途：把 llm.api_key 等敏感值加密后存进 config.yaml，避免明文落盘。
DPAPI 使用"当前 Windows 用户 + 本机"凭据加密：只有同一台机器上同一
用户身份的进程能解密（跨机器/跨用户拷贝密文会解密失败）。

注意：DPAPI 防的是"文件泄漏/误传"，不是"攻击者以你的用户身份运行
代码"——拿到你账户权限的程序同样可以调用解密。
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _protect(data: bytes) -> bytes:
    blob_in = _DATA_BLOB()
    buf = ctypes.create_string_buffer(data)
    blob_in.pbData = ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))
    blob_in.cbData = len(data)
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError(f"CryptProtectData 失败（WinError {ctypes.get_last_error()}）")
    return _blob_to_bytes(blob_out)


def _unprotect(data: bytes) -> bytes:
    blob_in = _DATA_BLOB()
    buf = ctypes.create_string_buffer(data)
    blob_in.pbData = ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))
    blob_in.cbData = len(data)
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError(
            f"CryptUnprotectData 失败（WinError {ctypes.get_last_error()}）"
            "：密文可能来自其他 Windows 用户或另一台机器")
    return _blob_to_bytes(blob_out)


def _blob_to_bytes(blob: _DATA_BLOB) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, int(blob.cbData))
    finally:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def encrypt_text(text: str) -> str:
    """加密明文，返回 base64 文本（可直接写入 config.yaml 的 api_key_encrypted）。"""
    return base64.b64encode(_protect(text.encode("utf-8"))).decode("ascii")


def decrypt_text(encoded: str) -> str:
    """解密 api_key_encrypted 的 base64 文本，返回明文。"""
    try:
        raw = base64.b64decode((encoded or "").strip())
    except Exception as exc:
        raise ValueError(f"api_key_encrypted 不是合法 base64: {exc}")
    return _unprotect(raw).decode("utf-8")
