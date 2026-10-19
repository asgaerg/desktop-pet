"""盯 uiohook 版的按住说话。

## 与之前几版的根本差别

| 版本 | 机制 | 谁起的 | 结果 |
|---|---|---|---|
| globalshortcut | 系统注册热键 | Electron | 只有按下，没松开 |
| probe-key.ps1 | `GetAsyncKeyState` 轮询 | Electron | ✅ 收到过；pwsh 起 ❌ |
| keyhook.ps1 | `WH_KEYBOARD_LL` | Electron | ✅ 收到过；pwsh 起 ❌ |
| **uiohook-napi** | **进程内 libuiohook** | **主进程内** | 待验证 |

关键：uiohook 在**主进程内**、由库自己在独立线程装钩子，
不经 PowerShell、不依赖窗口激活 —— 这正是用户指出的那两个限制。

## 预期日志

```
按住说话：按下（右 Alt）-> 开始采集
                          采集闸门：开
（采集电平 → 语音）
按住说话：松开 -> 结束采集，送去识别
                          松开 -> 主动收尾（N.NNs，forced）
                          语音识别（N.NNs，耗时 XXXms）：<你说的内容>
```

## 判据

  · 有「按下」-> uiohook 收到 keydown ✅
  · 有「松开」-> uiohook 收到 keyup ✅（这是"按住说话"的关键）
  · 有识别结果 -> 整条链路通
"""

from __future__ import annotations

import pathlib
import re
import sys
import time

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

MLOG = ROOT / "apps" / "desktop" / "pet-data" / "main.log"
SLOG = ROOT / "apps" / "desktop" / "dist" / "startup.log"
WATCH_S = 300

KEYS = ("按住说话", "采集闸门", "采集电平", "主动收尾", "开始识别", "语音识别",
        "以语音输入", "太安静", "当作误触", "收尾没拿到", "uiohook", "不受支持")


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
    return [l for l in out if any(k in l for k in KEYS)]


n0 = len(lines())
print(f"  起始 {n0} 条相关日志")
print()
print("  ══════════════════════════════════════════════════════════")
print("  👉 请 **按住右 Alt**，说一句，然后**松开**（最多等 5 分钟）")
print("     可以多按几次 —— 一起验证「只有第一次有反应」修没修好")
print("  ══════════════════════════════════════════════════════════")
print()

t0 = time.monotonic()
last = n0
levels = 0
presses = 0
while time.monotonic() - t0 < WATCH_S:
    time.sleep(1.2)
    ls = lines()
    if len(ls) > last:
        for l in ls[last:]:
            if "采集电平" in l:
                levels += 1
                continue
            if "按下（" in l:
                presses += 1
            print(f"     [{time.monotonic() - t0:5.1f}s] {l[:112]}", flush=True)
        last = len(ls)
        if any("语音识别（" in x or "太安静" in x or "收尾没拿到" in x for x in ls[n0:]):
            time.sleep(3)
            # 继续等，看有没有第二次（"只有第一次"那个 bug）
            if presses >= 1:
                continue
    elif int(time.monotonic() - t0) % 60 == 0:
        print(f"     [{time.monotonic() - t0:5.1f}s] （还在等…已收到 {presses} 次按下）", flush=True)
        time.sleep(1)

print()
if levels:
    print(f"     （采集电平 {levels} 条）")
print()
print("  ═══ 判读 ═══")
got = lines()[n0:]
j = "\n".join(got)
downs = [l for l in got if "按下（" in l]
ups = [l for l in got if "松开 ->" in l]
rec = [l for l in got if "语音识别（" in l]

print(f"     按下 {len(downs)} 次 / 松开 {len(ups)} 次 / 识别 {len(rec)} 次")
print()
for l in downs[:6]:
    print(f"     🔑 {l[:104]}")
print()
checks = [
    ("uiohook 收到 keydown（按下）", bool(downs)),
    ("uiohook 收到 keyup（松开）", bool(ups)),
    ("有采集电平（帧在喂端点）", "采集电平" in j),
    ("走到收尾并送识别", "主动收尾" in j),
    ("识别出结果", bool(rec)),
    ("**多次按下都收到了**（不只第一次）", len(downs) >= 2),
]
for label, ok in checks:
    print(f"     {'OK  ' if ok else '----'} {label}")

if rec:
    m = re.search(r"语音识别（([\d.]+)s，耗时 (\d+)ms）：(.*)$", rec[-1])
    if m:
        print()
        print(f"     音频 {m.group(1)}s  耗时 {m.group(2)}ms")
        print(f"     识别：「{m.group(3)}」")
        bad = any(k in m.group(3) for k in ("简体中文", "字幕志愿者", "剧场", "明镜", "订阅"))
        print()
        print("     🔴 还是幻觉" if bad else "     🎉 识别出的是真实内容")

print()
if len(downs) >= 2 and ups:
    print("     ⭐⭐ **完全成功** —— keydown/keyup 都稳定收到，且不止第一次")
elif downs and ups:
    print("     ⭐ **keydown + keyup 都收到了**")
elif downs:
    print("     ⚠️ 只有 keydown，没收到 keyup")
elif "不受支持" in j:
    print("     ❌ 键不受支持 —— 换一个键（配置 asr.push_to_talk.vk）")
elif "加载失败" in j or "不可用" in j:
    print("     ❌ uiohook 加载/启动失败 —— 看日志里的具体原因")
else:
    print("     ❌ 5 分钟内没有收到按键")
