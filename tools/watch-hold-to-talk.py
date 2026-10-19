"""盯一次**真正的按住说话**（右 Ctrl 按下/松开）。

## 与上一版的区别

| | 上一版（切换式） | 这一版（钩子） |
|---|---|---|
| 拿到键的机制 | `globalShortcut` | `WH_KEYBOARD_LL` 钩子 |
| 按下 | ✅ | ✅ |
| **松开** | ❌ 拿不到 | ✅ **拿得到** |
| 交互 | 按一次开始、再按一次结束 | **按住说、松开结束** |

用户明确要求「监听按下和松开的触发事件」—— 这一版就是。

## 预期日志

```
按住说话：按下（右 Ctrl）-> 开始采集          <- keydown
                              采集闸门：开
（采集电平打点 → 语音）
按住说话：松开 -> 结束采集，送去识别          <- keyup
                              松开 -> 主动收尾（N.NNs，forced）
                              开始识别：音频 N.NNs
                              语音识别（N.NNs，耗时 XXXms）：<文字>
```

## 判据

  · 有「按下（右 Ctrl）」-> 钩子收到 keydown ✅
  · 有「松开」           -> **钩子收到 keyup**（这是这一版的关键）
  · 有识别结果           -> 整条链路通
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
        "以语音输入", "太安静", "当作误触", "收尾没拿到", "监视器输出 <- DOWN",
        "监视器输出 <- UP")


def lines() -> list[str]:
    out: list[str] = []
    for p in (MLOG, SLOG):
        try:
            out += [
                l.strip()
                for l in p.read_text(encoding="utf-8", errors="replace").split("\n")
                if "[brain]" not in l and "帧头" not in l and "TICK" not in l
            ]
        except OSError:
            pass
    return [l for l in out if any(k in l for k in KEYS)]


n0 = len(lines())
print(f"  起始 {n0} 条相关日志")
print()
print("  ══════════════════════════════════════════════════════════")
print("  👉 请 **按住右 Ctrl**，说一句，然后**松开**（最多等 5 分钟）")
print("     按下时会有一声「嘀」= 生效了")
print("  ══════════════════════════════════════════════════════════")
print()

t0 = time.monotonic()
last = n0
levels = 0
while time.monotonic() - t0 < WATCH_S:
    time.sleep(1.2)
    ls = lines()
    if len(ls) > last:
        for l in ls[last:]:
            if "采集电平" in l:
                levels += 1
                continue
            print(f"     [{time.monotonic() - t0:5.1f}s] {l[:114]}", flush=True)
        last = len(ls)
        if any("语音识别（" in x or "太安静" in x or "收尾没拿到" in x for x in ls[n0:]):
            time.sleep(3)
            break
    elif int(time.monotonic() - t0) % 45 == 0:
        print(f"     [{time.monotonic() - t0:5.1f}s] （还在等…按住右 Ctrl 说话）", flush=True)
        time.sleep(1)

print()
if levels:
    print(f"     （采集电平 {levels} 条）")
print()
print("  ═══ 判读 ═══")
got = lines()[n0:]
j = "\n".join(got)
checks = [
    ("钩子收到 keydown（按下）", "按下（"),
    ("钩子收到 keyup（松开）", "松开 ->"),
    ("有采集电平（帧在喂端点）", "采集电平"),
    ("走到收尾并送识别", "主动收尾"),
    ("识别出结果", "语音识别（"),
]
for label, needle in checks:
    print(f"     {'OK  ' if needle in j else '----'} {label}")

rec = [l for l in got if "语音识别（" in l]
if rec:
    m = re.search(r"语音识别（([\d.]+)s，耗时 (\d+)ms）：(.*)$", rec[-1])
    if m:
        print()
        print(f"     音频 {m.group(1)}s  耗时 {m.group(2)}ms")
        print(f"     识别：「{m.group(3)}」")
        bad = any(k in m.group(3) for k in ("简体中文", "字幕志愿者", "剧场", "明镜", "订阅"))
        print()
        if bad:
            print("     🔴 还是幻觉")
        else:
            print("     🎉 **成功**")

print()
if "按下（" in j and "松开 ->" in j:
    print("     ⭐ **keydown 和 keyup 都拿到了** —— 真正的「按住说话」成立")
elif "按下（" in j:
    print("     ⚠️ 只拿到 keydown，没拿到 keyup（松开没报到）")
else:
    print("     ❌ 连 keydown 都没有 —— 钩子没收到你的按键")
    print("        （钩子心跳是正常的，所以是「收不到键」而不是「钩子死了」）")
