#!/usr/bin/env python
"""
诊断：**你按的键到底有没有被采到**，以及应用有没有接到。

## 为什么写成 Python 而不是 .ps1（2026-10-08）

同样的诊断我先写成了 `.ps1`，但用户机器上 `LocalMachine = RemoteSigned`
且脚本**没有** Zone.Identifier 远程标记，却仍被拒：

    无法加载文件 …\\diagnose-ptt-now.ps1。未对文件进行数字签名。
    无法在当前系统上运行该脚本。

加 `-ExecutionPolicy Bypass` 能绕过，但那要求用户记住一长串参数。
**Python 脚本不受 PowerShell 执行策略约束** —— 用户环境本来就有 Python
（提示符是 `(base)`），所以这个入口更省事。

## 它测什么

同时做两件事，一次跑完就能分清断点在哪：

  ① **本脚本自己**用 `GetAsyncKeyState` 采键（`ctypes` 直接调 Win32）
  ② 同时看**应用自己**的日志有没有新增「按下」

## 用法

    python tools/diagnose-ptt-now.py

倒数 3 秒后进入 20 秒采样窗口，请在这段时间内依次按：
    ① 右 Alt（按住 1 秒）  ② 左 Alt（按住 1 秒）  ③ 字母 A
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

LOG = ROOT / "apps" / "desktop" / "dist" / "startup.log"
USER32 = ctypes.WinDLL("user32", use_last_error=True)

# 要看的一批键：用来区分"是右 Alt 这个键的问题"还是"采样本身坏了"
KEYS = [
    ("右 Alt (A5, 应用监听的)", 0xA5),
    ("左 Alt (A4)", 0xA4),
    ("字母 A (41)", 0x41),
    ("空格 (20)", 0x20),
]


def fg_title() -> str:
    """前台窗口标题（截断），用来看"按的时候谁在前台"。"""
    hwnd = USER32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(160)
    USER32.GetWindowTextW(hwnd, buf, 160)
    s = buf.value
    return s[:28] if len(s) > 28 else s


def down(vk: int) -> bool:
    """最高位（0x8000）表示当前按下。返回 short，所以要先转无符号再比。"""
    return (USER32.GetAsyncKeyState(vk) & 0x8000) != 0


def app_press_count() -> int:
    try:
        return LOG.read_text(encoding="utf-8", errors="replace").count("按住说话] 按下")
    except OSError:
        return -1


def main() -> int:
    seconds = 20
    print("=" * 72)
    print(" 按键诊断：本脚本采到的键  vs  应用收到的「按下」")
    print("=" * 72)
    print()
    before = app_press_count()
    print(f"  应用日志里的「按下」条数（开始前）= {before}   （{LOG}）")
    if before < 0:
        print("  ⚠️ 读不到应用日志 —— 应用可能没在跑，或路径不对")
    print()
    print(f"  倒数 3 秒，然后有 {seconds} 秒采样窗口。请按顺序按：")
    print("    ① 右 Alt（按住 1 秒）   ② 左 Alt（按住 1 秒）   ③ 字母 A")
    print()
    for i in (3, 2, 1):
        print(f"    {i}…")
        time.sleep(1)
    print("    >>> 开始！现在按 <<<")
    print()

    was = {vk: False for _, vk in KEYS}
    hits = {name: 0 for name, _ in KEYS}

    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        for name, vk in KEYS:
            now = down(vk)
            if now != was[vk]:
                was[vk] = now
                if now:
                    hits[name] += 1
                mark = "DOWN" if now else "UP  "
                print(f"  [{time.monotonic() - t0:5.1f}s] {mark}  {name:<26} 前台={fg_title()}")
        time.sleep(0.012)

    print()
    print("  === 本脚本采到的按键次数 ===")
    for name, _ in KEYS:
        print(f"    {name:<26} {hits[name]} 次")

    time.sleep(0.8)
    after = app_press_count()
    print()
    print(f"  应用日志里的「按下」条数（结束后）= {after}（新增 {after - before}）")
    print()

    right = hits["右 Alt (A5, 应用监听的)"]
    left = hits["左 Alt (A4)"]
    letter = hits["字母 A (41)"]
    space = hits["空格 (20)"]
    gained = after - before

    print("  === 判读 ===")
    if right == 0 and left > 0:
        print("  🔴 **你按的其实是左 Alt（A4）**，而应用监听的是右 Alt（A5）——")
        print("     这就是「按下没用」的原因：键不对。")
        print("     修法：设置页重新录一次按键，或把 persona.yaml 的 vk 改成 164。")
        return 1
    if right == 0 and left == 0 and (letter or space):
        print("  ⚠️ 左右 Alt 都采不到，但字母/空格采得到 ——")
        print("     说明采样机制是活的，只是这台键盘的 Alt 没被采到。")
        print("     （笔记本键盘常没有独立的右 Alt；或 Alt 被输入法/其它软件吞了）")
        return 1
    if right > 0 and gained > 0:
        print("  ✅ **完整链路通了**：右 Alt 被采到，应用也记下了「按下」")
        print("     若你仍感觉「没反应」，问题在更后面（音频帧/识别），不是按键。")
        return 0
    if right > 0 and gained == 0:
        print("  🔴 **右 Alt 被采到了，但应用没记下「按下」** ——")
        print("     断点在「监视器 -> 主进程」。这是产品 bug。")
        return 1
    print("  采样本身有问题（连字母和空格都采不到）—— 看上面有没有 DOWN 行。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
