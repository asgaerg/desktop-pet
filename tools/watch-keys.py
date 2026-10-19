#!/usr/bin/env python
"""
**后台持续记录**按键 60 秒 —— 你按到看见为止，不用赶时间窗口。

## 为什么这么做（2026-10-08）

前面我一直在用"脚本起来后有 10 秒窗口，请在这期间按"这种方式收集按键，
结果反复得到"0 次"：**要么你没看到提示，要么没赶上那 10 秒**。
而我据此得出的"键收不到"结论，其实可能是"你没按"。

这个脚本把采样窗口拉到 60 秒，并且**每次看到按下就立刻打印并追加写文件**，
所以：你按一下 -> 屏幕上立刻出现一行 -> 你能确认"我按到了"。
看到 `DOWN` 出现就说明采集正常，可以 Ctrl+C 停掉。

## 用法

    & "F:\\ai项目\\.venv\\Scripts\\python.exe" tools\\watch-keys.py

它会同时盯这几个键，看清楚到底是哪一个被采到：
    右 Alt (A5) / 左 Alt (A4) / 右 Ctrl (A3) / 字母 A (41) / 空格 (20)
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

USER32 = ctypes.WinDLL("user32", use_last_error=True)

KEYS = [
    ("右 Alt (A5, 应用监听的)", 0xA5),
    ("左 Alt (A4)", 0xA4),
    ("右 Ctrl (A3)", 0xA3),
    ("字母 A (41)", 0x41),
    ("空格 (20)", 0x20),
]

OUT = ROOT / "apps" / "desktop" / "pet-data" / "ptt-tmp" / "watch-keys.log"
SECONDS = 60


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    try:
        OUT.write_text("", encoding="utf-8")
    except OSError:
        pass

    print("=" * 70)
    print(f" 持续记录按键 {SECONDS} 秒 —— 请**按住右 Alt 2 秒**")
    print(" 看到 DOWN 出现就说明采集正常，可以 Ctrl+C 停")
    print("=" * 70)
    print()

    was = {vk: False for _, vk in KEYS}
    hits = {name: 0 for name, _ in KEYS}
    lines: list[str] = []

    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < SECONDS:
            for name, vk in KEYS:
                now = (USER32.GetAsyncKeyState(vk) & 0x8000) != 0
                if now != was[vk]:
                    was[vk] = now
                    mark = "DOWN" if now else "UP  "
                    line = f"[{time.monotonic() - t0:5.1f}s] {mark}  {name}"
                    if now:
                        hits[name] += 1
                    print("  " + line, flush=True)
                    lines.append(line)
            time.sleep(0.012)
    except KeyboardInterrupt:
        print("\n  （手动停止）")

    print()
    print("  === 统计 ===")
    for name, _ in KEYS:
        print(f"    {name:<26} {hits[name]} 次")

    try:
        OUT.write_text("\n".join(lines), encoding="utf-8")
        print(f"\n  已写入 {OUT}")
    except OSError:
        pass

    right = hits["右 Alt (A5, 应用监听的)"]
    left = hits["左 Alt (A4)"]
    print()
    if right > 0:
        print("  ✅ **右 Alt 被采到了** —— 采集链路正常")
        print("     如果应用那边仍没反应，问题在「监视器 -> 主进程」这一段")
        return 0
    if left > 0:
        print("  🔴 **采到的是左 Alt，不是右 Alt** ——")
        print("     你可能按的是左 Alt（笔记本键盘常没有独立右 Alt）")
        return 1
    if hits["字母 A (41)"] or hits["空格 (20)"]:
        print("  ⚠️ 别的键采得到、Alt 采不到 —— Alt 可能被输入法/其它软件占用")
        return 1
    print("  ❌ 60 秒内**一个键都没采到** —— 采集环境有问题，或者你没按")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
