#!/usr/bin/env python
"""
从**当前进程**发一个合成的右 Alt（`keybd_event`），验证应用的监视器收不收得到。

## 为什么用它而不是让用户反复手按（2026-10-08）

用户已经手按验证过：右 Alt 确实被 `GetAsyncKeyState` 采到（见
`tools/diagnose-ptt-now.py` 的输出 `DOWN 右 Alt (A5)`），但应用日志里没有「按下」。

为了快速迭代，这里用 **Python 的 `ctypes` 直接调 `keybd_event`** 合成按键 ——
比让用户每次手按快得多，而且能连按多次做统计。

## ⚠️ 合成键与真实键的差别（已知，别误判）

- `keybd_event` **不带扫描码**。绝大多数程序只看 VK，所以够用。
- 之前我从 PowerShell 子进程发时，**应用收不到**；但独立监视器收得到。
  这正是要用它来测的原因 —— 如果这次应用能收到，说明差别在"谁发的"；
  如果仍收不到，说明差别在"应用那个监视器进程"。

## 用法

    python tools/send-right-alt.py            # 发 5 次，每次按住 600ms
    python tools/send-right-alt.py --times 3 --hold 800
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

LOG = ROOT / "apps" / "desktop" / "dist" / "startup.log"
USER32 = ctypes.WinDLL("user32", use_last_error=True)

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
VK_RIGHT_ALT = 0xA5


def press(vk: int, hold_ms: int) -> None:
    """按住 hold_ms 再松开。右 Alt 是扩展键，所以带 EXTENDEDKEY。"""
    USER32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
    time.sleep(hold_ms / 1000.0)
    USER32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)


def count(pattern: str) -> int:
    try:
        return LOG.read_text(encoding="utf-8", errors="replace").count(pattern)
    except OSError:
        return -1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--times", type=int, default=5)
    ap.add_argument("--hold", type=int, default=600, help="每次按住多少毫秒")
    ap.add_argument("--gap", type=int, default=900, help="两次之间间隔多少毫秒")
    args = ap.parse_args()

    print("=" * 72)
    print(" 从本进程合成「右 Alt」，看应用收不收得到")
    print("=" * 72)
    print()
    print(f"  日志：{LOG}")

    press_before = count("按住说话] 按下")
    raw_before = count("监视器输出 <- DOWN")
    print(f"  开始前：「按下」{press_before} 条，「监视器输出 <- DOWN」{raw_before} 条")
    print()

    # 先确认监视器在跑
    import subprocess

    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_Process -Filter \"Name = 'powershell.exe'\" | "
                "Where-Object { $_.CommandLine -like '*probe-key.ps1*' } | Measure-Object).Count",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        print(f"  probe-key 监视器进程数：{(out.stdout or '').strip() or '(读不到)'}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 查监视器进程失败：{str(exc)[:70]}")
    print()

    print(f"  === 发 {args.times} 次右 Alt（按住 {args.hold}ms，间隔 {args.gap}ms）===")
    for i in range(1, args.times + 1):
        press(VK_RIGHT_ALT, args.hold)
        print(f"    第 {i} 次已发")
        time.sleep(args.gap / 1000.0)

    time.sleep(1.2)
    press_after = count("按住说话] 按下")
    raw_after = count("监视器输出 <- DOWN")
    print()
    print(f"  结束后：「按下」{press_after} 条（+{press_after - press_before}）")
    print(f"          「监视器输出 <- DOWN」{raw_after} 条（+{raw_after - raw_before}）")
    print()

    print("  === 判读 ===")
    if press_after > press_before:
        print("  ✅ 应用收到了合成键并触发了「按下」—— 链路是通的")
        return 0
    if raw_after > raw_before:
        print("  🔴 **监视器输出到了 Node，但没触发「按下」** ——")
        print("     断点在 handleLine（最可能是 `this.down` 卡在 true，去重把事件吃掉了）")
        return 1
    print("  🔴 **Node 一行都没收到** —— 断点在「监视器 stdout -> Node」这一段")
    print("     （管道/编码/子进程问题）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
