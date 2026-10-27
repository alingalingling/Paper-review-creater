"""User-local model settings. Windows credentials are encrypted with user DPAPI."""
from __future__ import annotations

import ctypes
import json
import os
import tempfile
import threading
from ctypes import wintypes
from pathlib import Path

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
_WRITE_LOCK = threading.Lock()


def config_path():
    override = os.environ.get("TRACE_CONFIG_DIR")
    directory = Path(override) if override else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TraceReview"
    return directory / "model-settings.dpapi"


class Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data, decrypt=False):
    if os.name != "nt":
        raise ValueError("本地密钥加密存储目前支持 Windows；其他系统请使用进程环境变量。")
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        function = crypt32.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        success = function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        function = crypt32.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        success = function(ctypes.byref(source), "TraceReview local model settings", None, None, None, 1, ctypes.byref(output))
    if not success:
        raise ValueError("无法读写本机加密配置，请在模型设置中重新配置。")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


def load_model_config():
    result = {"base": DEFAULT_BASE, "model": DEFAULT_MODEL, "key": ""}
    path = config_path()
    if path.exists():
        try:
            saved = json.loads(_crypt(path.read_bytes(), decrypt=True))
            result.update({k: str(saved.get(k, result[k])) for k in result})
        except (OSError, ValueError, TypeError):
            raise ValueError("本机模型配置无法读取，请重新保存模型设置。") from None
    for name, variable in (("base", "TRACE_API_BASE"), ("model", "TRACE_MODEL"), ("key", "TRACE_API_KEY")):
        if variable in os.environ:
            result[name] = os.environ[variable]
    return result


def save_model_config(config):
    payload = json.dumps({k: str(config.get(k, "")) for k in ("base", "model", "key")}).encode("utf-8")
    encrypted = _crypt(payload)
    path = config_path()
    with _WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="model-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encrypted)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
