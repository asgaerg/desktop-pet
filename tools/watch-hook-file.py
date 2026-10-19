"""盯**应用自己的钩子**输出文件 —— 最原始的一层。

## 为什么盯这个而不是 main.log

`main.log` 里的 `监视器输出 <- DOWN` 是**主进程读到的**（多一层）。
而这个文件是**钩子直接写的** —— 少一层就少一个可疑点。

文件里出现 `DOWN 163` = 钩子真的收到了键。
一直只有 `TICK`/`REHOOK` = 钩子活着但收不到键。

## 为什么必须让用户真按

已确认：**我的测试环境（pwsh/python 起的进程）收不到任何键盘事件** ——
轮询和钩子都试过，A/B 两个钩子同时跑也都是 0 个键事件。
而 Electron 起的时候收到过（`14:30:09 DOWN 163`）。

所以"我这儿测不到"证明不了任何事，只能靠用户按。
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

OUT = ROOT / "apps" / "desktop" / "pet-data" / "ptt-tmp" / "probe-key.out"
MLOG = ROOT / "apps" / "desktop" / "pet-data" / "main.log"
SLOG = ROOT / "apps" / "desktop" / "dist" / "startup.log"

WATCH_S = 300


def rows() -> list[str]:
    try:
        return [
            l.strip()
            for l in OUT.read_text(encoding="utf-8-sig", errors="replace").split("\n")
            if l.strip()
        ]
    except OSError:
        return []


def log_lines() -> list[str]:
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
        l
        for l in out
        if any(k in l for k in ("按住说话", "采集闸门", "主动收尾", "开始识别", "语音识别",
                                "太安静", "当作误触", "收尾没拿到", "按下（", "松开 ->"))
    ]


r0 = rows()
l0 = len(log_lines())
print(f"  钩子文件：{OUT.name}")
print(f"  现在有 {len(r0)} 行，最后 3 行：")
for l in r0[-3:]:
    print(f"     {l}")
print()
print("  ══════════════════════════════════════════════════════════")
print("  👉 请 **按住右 Ctrl**，说一句，然后**松开**（最多等 5 分钟）")
print("     也可以**多按几次** —— 这正好验证「只有第一次有反应」")
print("  ══════════════════════════════════════════════════════════")
print()

t0 = time.monotonic()
last_r = len(r0)
last_l = l0
while time.monotonic() - t0 < WATCH_S:
    time.sleep(1.2)
    r = rows()
    # 钩子文件的新行（TICK/REHOOK 不显示，只显示按键）
    if len(r) > last_r:
        for l in r[last_r:]:
            if l.startswith(("TICK", "REHOOK")):
                continue
            print(f"     [{time.monotonic() - t0:5.1f}s] 🔑 钩子: {l}", flush=True)
        last_r = len(r)

    ls = log_lines()
    if len(ls) > last_l:
        for l in ls[last_l:]:
            print(f"     [{time.monotonic() - t0:5.1f}s] {l[:108]}", flush=True)
        last_l = len(ls)
        if any("语音识别（" in x or "太安静" in x or "当作误触" in x or "收尾没拿到" in x
               for x in ls[l0:]):
            time.sleep(3)
            break
    elif int(time.monotonic() - t0) % 60 == 0:
        nr = len([x for x in rows() if x.startswith(("DOWN", "UP"))])
        print(f"     [{time.monotonic() - t0:5.1f}s] （还在等…钩子文件里按键事件 {nr} 个）", flush=True)
        time.sleep(1)

print()
print("  ═══ 判读 ═══")
r = rows()
downs = [x for x in r if x.startswith("DOWN")]
ups = [x for x in r if x.startswith("UP")]
ticks = [x for x in r if x.startswith("TICK")]
rehooks = [x for x in r if x.startswith("REHOOK")]
print(f"     钩子文件：TICK {len(ticks)}  REHOOK {len(rehooks)}  DOWN {len(downs)}  UP {len(ups)}")
print()
if downs:
    print(f"     ✅ **钩子收到了 {len(downs)} 次按下 / {len(ups)} 次松开**")
    print("        -> 钩子和键都没问题，问题在后面的链路")
    rec = [l for l in log_lines()[l0:] if "语音识别（" in l]
    if rec:
        print(f"        识别结果：「{rec[-1].split('）：')[-1][:50]}」")
elif ticks:
    print("     ❌ 钩子在跑（TICK 在流）但**一个按键都没收到**")
    print("        -> 这个 Electron 进程里的钩子收不到键")
    print()
    print("        ⚠️ 但注意：14:30 那次**是收到过的**（DOWN 163 / UP 163）")
    print("        两次的差别是这一版改了：")
    print("          · 回调改成入内存队列（原来是直接写文件）")
    print("          · 加了自动重复抑制（_isDown）")
    print("          · 加了每 30 秒重装钩子")
    print("        所以要回退验证是哪一处引入的问题")
else:
    print("     ❌ 钩子连 TICK 都没有 —— 进程可能没了")
