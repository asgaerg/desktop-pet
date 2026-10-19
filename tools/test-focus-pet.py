"""验证「右 Alt 没触发是因为宠物窗口没焦点」。

## 假设

宠物窗口是**鼠标穿透**的（`setIgnoreMouseEvents(true, {forward:true})`）——
点击会**穿到下面的窗口**。所以用户"点宠物"那一下，焦点其实给了底下的程序，
**Electron 窗口从来没拿到焦点**。

而实测已证明：`before-input-event` **失焦时 0 个事件**。

## 这个脚本做什么

  ① 打印当前焦点窗口的标题（**不是**宠物窗口就说明假设成立）
  ② 用 Win32 `SetForegroundWindow` 把宠物窗口**强行设成前台**
  ③ 再打印一次，确认焦点换了
  ④ 然后请你按住右 Alt —— 如果这次触发了，假设就证实了

## ⚠️ 这个脚本不碰产品代码

只是把窗口提到前台，纯诊断。不动配置、不改文件。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import pathlib
import sys
import time

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def window_title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ''
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def pid_of(hwnd: int) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def find_electron_pids() -> set[int]:
    """用 tasklist 找 electron.exe 的 PID（避免依赖 psutil）"""
    import subprocess

    r = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq electron.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    pids: set[int] = set()
    for line in (r.stdout or "").split("\n"):
        parts = [p.strip('"') for p in line.strip().split('","')]
        if len(parts) >= 2 and parts[0].lower().startswith("electron"):
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    return pids


el_pids = find_electron_pids()
print(f"  electron.exe 的 PID: {sorted(el_pids) or '（没找到）'}")

fg = user32.GetForegroundWindow()
print()
print("  ═══ ① 当前焦点窗口 ═══")
print(f"     HWND   = {fg}")
print(f"     标题   = {window_title(fg)!r}")
print(f"     属于   = PID {pid_of(fg)}"
      f"{'  ← **是 Electron！**' if pid_of(fg) in el_pids else '  ← 不是 Electron（假设成立）'}")

# 枚举所有顶层窗口，找 Electron 的
print()
print("  ═══ ② Electron 的顶层窗口 ═══")
EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
found: list[tuple[int, str, bool]] = []


def cb(hwnd, _lparam):
    pid = pid_of(hwnd)
    if pid in el_pids:
        title = window_title(hwnd)
        visible = bool(user32.IsWindowVisible(hwnd))
        found.append((hwnd, title, visible))
    return True


user32.EnumWindows(EnumWindowsProc(cb), 0)
for hwnd, title, visible in found:
    print(f"     HWND {hwnd:<10} 可见={visible}  标题={title!r}")

if not found:
    print("     ❌ 没找到 Electron 的顶层窗口")
    raise SystemExit(1)

# 挑一个可见的（宠物窗口标题通常是"桌面宠物"之类）
target = next((h for h in found if h[2]), found[0])
hwnd = target[0]
print()
print(f"  ═══ ③ 把 HWND {hwnd}（{target[1]!r}）设为前台 ═══")

# SetForegroundWindow 有前台锁限制；先用 AttachThreadInput 绕过
fg_thread = user32.GetWindowThreadProcessId(fg, None)
cur_thread = kernel32.GetCurrentThreadId()
tgt_thread = user32.GetWindowThreadProcessId(hwnd, None)
attached = False
try:
    if fg_thread and fg_thread != cur_thread:
        attached = bool(user32.AttachThreadInput(fg_thread, cur_thread, True))
    user32.ShowWindow(hwnd, 5)  # SW_SHOW
    ok = bool(user32.SetForegroundWindow(hwnd))
    print(f"     AttachThreadInput={attached}  SetForegroundWindow={ok}")
finally:
    if attached:
        user32.AttachThreadInput(fg_thread, cur_thread, False)

time.sleep(1)
fg2 = user32.GetForegroundWindow()
print()
print("  ═══ ④ 现在焦点是 ═══")
print(f"     HWND   = {fg2}")
print(f"     标题   = {window_title(fg2)!r}")
print(f"     属于   = PID {pid_of(fg2)}"
      f"{'  ✅ **是 Electron —— 焦点拿到了！**' if pid_of(fg2) in el_pids else '  ❌ 还是不是 Electron'}")

print()
print("  ══════════════════════════════════════════════════════════")
print("  👉 现在宠物窗口应该是聚焦的。请**按住右 Alt 一秒再松开**")
print("     等 20 秒")
print("  ══════════════════════════════════════════════════════════")
print()

MLOG = ROOT / "apps" / "desktop" / "pet-data" / "main.log"
SLOG = ROOT / "apps" / "desktop" / "dist" / "startup.log"


def lines() -> list[str]:
    out: list[str] = []
    for p in (MLOG, SLOG):
        try:
            out += [
                l.strip()
                for l in p.read_text(encoding="utf-8", errors="replace").split("\n")
                if "[brain]" not in l and "帧头" not in l
            ]
        except OSError:
            pass
    return [
        l for l in out
        if any(k in l for k in ("按住说话", "采集闸门", "主动收尾", "开始识别", "语音识别",
                                "太安静", "focused", "聚焦"))
    ]


n0 = len(lines())
seen = n0
for i in range(20):
    time.sleep(1)
    ls = lines()
    if len(ls) > seen:
        for l in ls[seen:]:
            print(f"     [{i + 1:2d}s] {l[:112]}")
        seen = len(ls)

got = lines()[n0:]
j = "\n".join(got)
print()
print("  ═══ 判读 ═══")
if "按住 右 Alt" in j or "开始采集（按住" in j or "松开 右 Alt" in j:
    print("     🎉🎉 **假设证实：聚焦之后右 Alt 就生效了！**")
    print("        -> 问题不是键不对，而是**宠物窗口拿不到焦点**")
    print("        -> 修法：给宠物窗口一个「获得焦点」的入口（全局热键 / 点击宠物本体）")
elif got:
    print("     ⚠️ 有新日志但没看到右 Alt 触发：")
    for l in got[:6]:
        print(f"        {l[:100]}")
else:
    print("     ❌ 什么新日志都没有 —— 聚焦了也收不到")
