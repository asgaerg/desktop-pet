"""往**产品监视器**里注入按键，看它对哪些 VK 有反应。

## 为什么这样测

产品监视器**健康**（心跳在流、主进程在读：148 条 TICK），
但 `DOWN/UP` 一条都没有。而我自己的测试脚本里**两个键都不认到** ——
那是 `pwsh→python→powershell` 这条链路被限制了，测不出真东西。

所以这次**不另起监视器**，直接给**产品那个**（Electron 起的、已被证明能收键的）
注入按键，看它的输出文件怎么变。

## 注入哪些键

```
0xA5  右 Alt      <- 当前配置的，用户说没用
0x78  F9          <- 对照（普通键）
0x41  A           <- 更基础的对照
0x20  空格        <- 最基础
```

## 判据

  · 全都认到        -> 监视器好、键也好 -> 问题在**用户的物理按键**
  · 只认得普通键     -> **VK 0xA5 这个键被系统/输入法吃掉了** -> 换键
  · 全都不认到      -> `GetAsyncKeyState` 在这个进程里读不到任何键

⚠️ 注入用 `keybd_event`。它在**产品监视器这种环境**里是否有效，
   正是这次要测的 —— 所以"注入没认到"不能直接推出"键有问题"，
   要结合"别的键认不认到"一起看。
"""

from __future__ import annotations

import ctypes
import pathlib
import sys
import time

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

OUT = ROOT / "apps" / "desktop" / "pet-data" / "ptt-tmp" / "probe-key.out"
MLOG = ROOT / "apps" / "desktop" / "pet-data" / "main.log"

KEYS = [
    (0xA5, "右 Alt（当前配置）"),
    (0x78, "F9"),
    (0x41, "A"),
    (0x20, "空格"),
]

KEYEVENTF_KEYUP = 0x0002


def snap() -> tuple[int, list[str]]:
    try:
        txt = OUT.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return 0, []
    rows = [l.strip() for l in txt.split("\n") if l.strip()]
    return len(rows), rows


def log_events() -> list[str]:
    try:
        return [
            l.strip()
            for l in MLOG.read_text(encoding="utf-8", errors="replace").split("\n")
            if "[brain]" not in l and ("监视器输出 <- DOWN" in l or "监视器输出 <- UP" in l)
        ]
    except OSError:
        return []


n0, rows0 = snap()
e0 = len(log_events())
print(f"  产品监视器输出：{n0} 行（最后一行 {rows0[-1] if rows0 else '（空）'}）")
print(f"  已记录的 DOWN/UP：{e0} 条")
print()
print("  ═══ 逐个注入（每个按键按住 0.4 秒）═══")
print()

u = ctypes.windll.user32
for vk, label in KEYS:
    n_before, _ = snap()
    u.keybd_event(vk, 0, 0, 0)
    time.sleep(0.4)
    u.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.9)  # 等 Node 侧 80ms 轮询 + 文件落盘

    _, rows = snap()
    new = [r for r in rows[n_before:] if r.startswith(("DOWN", "UP"))]
    flag = "✅ 认到" if new else "❌ 没认到"
    print(f"     {label:<18} VK 0x{vk:02X}   {flag}   {new if new else ''}")

print()
print("  ═══ 主进程侧收到了哪些 ═══")
events = log_events()
new_events = events[e0:]
if new_events:
    for l in new_events[-12:]:
        print(f"     {l[:100]}")
else:
    print("     （一条都没有）")

print()
print("  ═══ 判读 ═══")
_, rows = snap()
all_new = [r for r in rows[n0:] if r.startswith(("DOWN", "UP"))]
got_vks = set()
for r in all_new:
    parts = r.split()
    if len(parts) >= 2 and parts[1].isdigit():
        got_vks.add(int(parts[1]))

a5_ok = 0xA5 in got_vks
other_ok = bool(got_vks - {0xA5})

print(f"     认到的 VK：{sorted(hex(v) for v in got_vks) if got_vks else '（无）'}")
print()
if a5_ok and other_ok:
    print("     ✅ 右 Alt 和别的键都认到 —— **注入有效、键也好**")
    print("        那问题在**你的物理按键**：可能没按到、或者被别的程序吃了")
elif other_ok and not a5_ok:
    print("     🔴 **别的键认到、右 Alt 认不到** —— 就是 VK 0xA5 的问题")
    print("        右 Alt 在 Windows 里同时是 **AltGr**，会被输入法/某些程序拦截。")
    print("        -> **换键**（设置页里能改「按住说话」的键）")
elif a5_ok and not other_ok:
    print("     ⚠️ 只有右 Alt 认到（少见）—— 可能是注入的时序巧合")
else:
    print("     ❌ 一个都没认到 —— 注入在这个环境里**对产品监视器也无效**")
    print()
    print("        这说明我**没法用注入测**，必须靠你手按。")
    print("        请直接用手按右 Alt，然后看：")
    print(f"          {OUT}")
    print("        文件里出现 DOWN 165 就说明键和监视器都好。")
