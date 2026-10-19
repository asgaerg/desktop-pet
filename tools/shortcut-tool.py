# -*- coding: utf-8 -*-
"""读/改桌面快捷方式（IShellLinkW，无第三方依赖）。

用法：
    python shortcut-tool.py read <lnk路径> [<lnk路径>...]
    python shortcut-tool.py set-icon <lnk路径> <ico路径> [索引]

只动 IconLocation，别的字段一律不碰；set-icon 前先在旁边留 .bak 备份。
"""
import ctypes
import shutil
import sys
from ctypes import POINTER, Structure, byref, c_void_p, c_wchar_p, c_int, sizeof
from ctypes import wintypes

CLSID_ShellLink = "{00021401-0000-0000-C000-000000000046}"
IID_IShellLinkW = "{000214F9-0000-0000-C000-000000000046}"
IID_IPersistFile = "{0000010B-0000-0000-C000-000000000046}"
STGM_READ = 0
SLGP_RAWPATH = 0x0004

PTR_SIZE = sizeof(c_void_p)


class WIN32_FIND_DATAW(Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("dwReserved0", wintypes.DWORD),
        ("dwReserved1", wintypes.DWORD),
        ("cFileName", c_wchar_p * 260),
        ("cAlternateFileName", c_wchar_p * 14),
    ]


def _guid(s: str):
    g = (ctypes.c_ulong * 4)()
    ctypes.oledll.ole32.CLSIDFromString(c_wchar_p(s), g)
    return g


def _vtbl_addr(iface: c_void_p) -> int:
    """接口指针 -> 虚表地址（接口指针指向的第一块内存就是虚表指针）。"""
    return ctypes.cast(iface, POINTER(c_void_p)).contents.value


def _fn(iface: c_void_p, slot: int, proto):
    """取虚表第 slot 槽并包成可调用。"""
    addr = ctypes.cast(
        c_void_p(_vtbl_addr(iface) + slot * PTR_SIZE), POINTER(c_void_p)
    ).contents.value
    return proto(addr)


HRESULT = ctypes.HRESULT
PROTO_GETPATH = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, wintypes.DWORD, POINTER(WIN32_FIND_DATAW), wintypes.DWORD)
PROTO_GETSTR = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, c_int)
PROTO_GETICON = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, c_int, POINTER(c_int))
PROTO_LOAD = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, wintypes.DWORD)
PROTO_SAVE = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, wintypes.BOOL)
PROTO_SETICON = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, c_int)
PROTO_QI = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_void_p, POINTER(c_void_p))


def open_lnk(path: str):
    """返回 (shell_link, persist_file)。调用方负责 CoUninitialize。"""
    ole32 = ctypes.oledll.ole32
    ole32.CoInitialize(None)
    try:
        obj = c_void_p()
        ole32.CoCreateInstance(_guid(CLSID_ShellLink), None, 1, _guid(IID_IShellLinkW), byref(obj))
        pf = c_void_p()
        _fn(obj, 0, PROTO_QI)(obj, _guid(IID_IPersistFile), byref(pf))
        _fn(pf, 5, PROTO_LOAD)(pf, path, STGM_READ)
        return obj, pf
    except Exception:
        ole32.CoUninitialize()
        raise


def read_lnk(path: str) -> None:
    obj, pf = open_lnk(path)
    try:
        buf = ctypes.create_unicode_buffer(2048)
        fd = WIN32_FIND_DATAW()
        _fn(obj, 3, PROTO_GETPATH)(obj, buf, 2048, byref(fd), SLGP_RAWPATH)
        target = buf.value

        _fn(obj, 10, PROTO_GETSTR)(obj, buf, 2048)
        args = buf.value

        ibuf = ctypes.create_unicode_buffer(2048)
        idx = c_int()
        _fn(obj, 16, PROTO_GETICON)(obj, ibuf, 2048, byref(idx))
        icon = ibuf.value

        print(f"文件: {path}")
        print(f"  目标:  {target}")
        print(f"  参数:  {args}")
        print(f"  图标:  {icon} (索引 {idx.value})")
    finally:
        ctypes.oledll.ole32.CoUninitialize()


def set_icon(path: str, icon: str, index: int = 0) -> None:
    backup = path + ".bak"
    shutil.copy2(path, backup)
    print(f"备份 -> {backup}")

    obj, pf = open_lnk(path)
    try:
        # SetIconLocation 是 IShellLinkW 第 17 个槽（GetIconLocation=16 的下一个）
        PROTO_SETICON_T = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, c_int)
        hr = _fn(obj, 17, PROTO_SETICON_T)(obj, icon, index)
        if hr != 0:
            raise OSError(f"SetIconLocation 失败: 0x{hr & 0xFFFFFFFF:08X}")
        hr = _fn(pf, 6, PROTO_SAVE)(pf, path, True)
        if hr != 0:
            raise OSError(f"Save 失败: 0x{hr & 0xFFFFFFFF:08X}")
        print(f"图标已设为 {icon} (索引 {index})")
    finally:
        ctypes.oledll.ole32.CoUninitialize()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "read"
    if cmd == "read":
        for p in sys.argv[2:]:
            read_lnk(p)
    elif cmd == "set-icon":
        set_icon(sys.argv[2], sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else 0)
        read_lnk(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(2)
