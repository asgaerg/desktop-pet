# -*- coding: utf-8 -*-
"""重建桌面快捷方式 + 验证图标可用性。

用法：
    python shortcut-rebuild.py

做四件事：
  1. 用 ExtractIconExW 验证 pet.ico 能被系统实际加载（图标显示不出来的
     最可靠判据，比看文件头靠谱）；
  2. 删掉桌面上的旧 .lnk（旧副本在 .backup/shortcut-20261014/ 已有备份）；
  3. 用 IShellLinkW **从零重建**：目标 / 参数 / 工作目录 / 图标全部重新设，
     路径一律反斜杠 —— 上一版用正斜杠存 IconLocation（`F:/ai项目/...`），
     Explorer 解析不了，很可能就是图标空白的直接原因；
  4. SHChangeNotify 让桌面立刻重画。
"""
import ctypes
import os
import sys
from ctypes import POINTER, byref, c_void_p, c_wchar_p, c_int
from ctypes import wintypes

DESKTOP = os.path.join(os.environ["USERPROFILE"], "Desktop")
ICO = r"F:\ai项目\apps\desktop\assets\pet.ico"
WSCRIPT = r"C:\Windows\System32\wscript.exe"
VBS = r"F:\ai项目\启动宠物.vbs"
CMD = r"C:\Windows\System32\cmd.exe"
DEV = r"F:\ai项目\dev.cmd"

SHORTCUTS = [
    {
        "path": os.path.join(DESKTOP, "桌面宠物.lnk"),
        "target": WSCRIPT,
        "args": f'"{VBS}"',
        "workdir": r"F:\ai项目",
        "desc": "桌面宠物",
    },
    {
        "path": os.path.join(DESKTOP, "桌面宠物（调试）.lnk"),
        "target": CMD,
        "args": f'/k ""{DEV}""',
        "workdir": r"F:\ai项目",
        "desc": "桌面宠物（调试）",
    },
]

HRESULT = ctypes.HRESULT
PTR = ctypes.sizeof(c_void_p)
PROTO_QI = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_void_p, POINTER(c_void_p))
PROTO_SETSTR = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p)
PROTO_SETPATH = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, wintypes.DWORD)
PROTO_SETICON = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, c_int)
PROTO_SAVE = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, wintypes.BOOL)


def guid(s):
    g = (ctypes.c_ulong * 4)()
    ctypes.oledll.ole32.CLSIDFromString(c_wchar_p(s), g)
    return g


def fn(iface, slot, proto):
    vtbl = ctypes.cast(iface, POINTER(c_void_p)).contents.value
    addr = ctypes.cast(c_void_p(vtbl + slot * PTR), POINTER(c_void_p)).contents.value
    return proto(addr)


def check_icon_loadable(path):
    """ExtractIconExW：让系统**真的**去加载一次图标，返回大图标句柄数。"""
    hicon_large = wintypes.HICON()
    hicon_small = wintypes.HICON()
    n = ctypes.windll.shell32.ExtractIconExW(
        c_wchar_p(path), 0, byref(hicon_large), byref(hicon_small), 1
    )
    if n > 0 and hicon_large:
        ctypes.windll.user32.DestroyIcon(hicon_large)
    if n > 0 and hicon_small:
        ctypes.windll.user32.DestroyIcon(hicon_small)
    return n


# IShellLinkW 槽位（IUnknown 0-2 之后）：
# 3 GetPath 4 GetIDList 5 SetIDList 6 GetDescription 7 SetDescription
# 8 GetWorkingDirectory 9 SetWorkingDirectory 10 GetArguments 11 SetArguments
# ... 16 GetIconLocation 17 SetIconLocation
CLSID_ShellLink = "{00021401-0000-0000-C000-000000000046}"
IID_IShellLinkW = "{000214F9-0000-0000-C000-000000000046}"
IID_IPersistFile = "{0000010B-0000-0000-C000-000000000046}"
STGM_READWRITE = 0x0002


def build_shortcut(item):
    ole32 = ctypes.oledll.ole32
    ole32.CoInitialize(None)
    try:
        obj = c_void_p()
        ole32.CoCreateInstance(guid(CLSID_ShellLink), None, 1, guid(IID_IShellLinkW), byref(obj))
        # SetPath（槽 20：SetPath 在 GetPath(3) 的对面，序列是
        # 18 SetPath? 不对 —— 槽位必须数准：3..17 如上，
        # 18 GetRelativePath? 官方顺序：GetPath GetIDList SetIDList
        # GetDescription SetDescription GetWorkingDirectory SetWorkingDirectory
        # GetArguments SetArguments GetHotkey SetHotkey GetShowCmd SetShowCmd
        # GetIconLocation SetIconLocation SetRelativePath SetPath SetWorkingDirectory(重)
        # => SetPath = 槽 20。
        hr = fn(obj, 20, PROTO_SETPATH)(obj, item["target"], 0)
        assert hr == 0, f"SetPath 0x{hr & 0xFFFFFFFF:08X}"
        hr = fn(obj, 11, PROTO_SETSTR)(obj, item["args"])
        assert hr == 0, f"SetArguments 0x{hr & 0xFFFFFFFF:08X}"
        hr = fn(obj, 9, PROTO_SETSTR)(obj, item["workdir"])
        assert hr == 0, f"SetWorkingDirectory 0x{hr & 0xFFFFFFFF:08X}"
        hr = fn(obj, 7, PROTO_SETSTR)(obj, item["desc"])
        assert hr == 0, f"SetDescription 0x{hr & 0xFFFFFFFF:08X}"
        hr = fn(obj, 17, PROTO_SETICON)(obj, ICO, 0)
        assert hr == 0, f"SetIconLocation 0x{hr & 0xFFFFFFFF:08X}"

        pf = c_void_p()
        fn(obj, 0, PROTO_QI)(obj, guid(IID_IPersistFile), byref(pf))
        hr = fn(pf, 6, PROTO_SAVE)(pf, item["path"], True)
        assert hr == 0, f"Save 0x{hr & 0xFFFFFFFF:08X}"
        print(f"已重建 {item['path']}")
    finally:
        ole32.CoUninitialize()


def notify_shell():
    SHCNE_ASSOCCHANGED = 0x08000000
    SHCNF_IDLIST = 0x0000
    ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)


if __name__ == "__main__":
    n = check_icon_loadable(ICO)
    print(f"图标加载验证：ExtractIconExW 返回 {n}（>0 即系统真的能加载）")
    if n <= 0:
        err = ctypes.GetLastError()
        print(f"❌ 图标加载失败，LastErr={err} —— 先修 ico 再重建快捷方式")
        sys.exit(1)

    for item in SHORTCUTS:
        if os.path.exists(item["path"]):
            os.remove(item["path"])
            print(f"已删旧文件 {os.path.basename(item['path'])}（备份在 .backup/shortcut-20261014/）")
        build_shortcut(item)

    notify_shell()
    print("已通知桌面重画")
