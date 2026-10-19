"""再测一次：右 Alt vs F9，等 40 秒、带倒计时。

## 上一轮两个键都没认到

两种可能：
  · 你 20 秒内没按到（脚本催得太急）
  · 监视器/环境真的收不到键

## 这一轮改进

  · 等 **40 秒**，每 5 秒提示一次
  · **两个键都用合成按键先自测一遍**（在起监视器之前）——
    合成在这个环境里时灵时不灵，但**如果合成能认到**，
    就说明监视器是好的，问题在你的物理按键或时机
  · 最后统计时把"心跳条数"一起报 —— 心跳在 = 循环在跑
"""

from __future__ import annotations

import ctypes
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

SCRIPT = ROOT / "apps" / "desktop" / "tools" / "probe-key.ps1"
TMPDIR = ROOT / "apps" / "desktop" / "pet-data" / "ptt-tmp"
TMPDIR.mkdir(parents=True, exist_ok=True)
env = {**os.environ, "TEMP": str(TMPDIR), "TMP": str(TMPDIR)}

VK_RALT, VK_F9 = 0xA5, 0x78

procs: list[tuple[str, subprocess.Popen, pathlib.Path]] = []
for label, vk, fname in (("右Alt", VK_RALT, "cmp-a5.out"), ("F9", VK_F9, "cmp-f9.out")):
    out = TMPDIR / fname
    try:
        out.unlink()
    except OSError:
        pass
    p = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(SCRIPT), "-Vk", hex(vk), "-PollMs", "15", "-OutFile", str(out)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )
    procs.append((label, p, out))

print("  监视器已就绪（右Alt + F9 各一个）")
time.sleep(2)

# ── 先用**合成按键**自测：能认到就说明监视器是好的 ────────────────────
print()
print("  ═══ 先自测：合成按键（验证监视器本身好不好）═══")
u = ctypes.windll.user32
for label, vk in (("右Alt", VK_RALT), ("F9", VK_F9)):
    u.keybd_event(vk, 0, 0, 0)
    time.sleep(0.6)
    u.keybd_event(vk, 0, 0x0002, 0)
    time.sleep(0.6)
    out = next(o for l, _, o in procs if l == label)
    try:
        txt = out.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        txt = ""
    got = [l.strip() for l in txt.split("\n") if l.strip().startswith(("DOWN", "UP"))]
    print(f"     {label}: 合成后文件里 {got if got else '（没有 DOWN/UP）'}")

print()
print("  ═══ 现在请你**真的用手按**（40 秒）═══")
print("     ① 按住**右 Alt** 一秒，松开")
print("     ② 按住 **F9** 一秒，松开")
print("     （如果上一轮的合成已经认到了，那这轮主要看你的物理按键）")
print()

seen: dict[str, list[str]] = {label: [] for label, _, _ in procs}
t0 = time.monotonic()
next_hint = 5.0
while time.monotonic() - t0 < 40:
    time.sleep(1)
    for label, _, out in procs:
        try:
            txt = out.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        lines = [l.strip() for l in txt.split("\n") if l.strip() and not l.startswith("TICK")]
        if len(lines) > len(seen[label]):
            for l in lines[len(seen[label]):]:
                print(f"     [{time.monotonic() - t0:4.1f}s] {label}: {l}")
            seen[label] = lines
    el = time.monotonic() - t0
    if el >= next_hint:
        print(f"     … 还剩 {40 - int(el)} 秒")
        next_hint += 5

print()
print("  ═══ 结果 ═══")
ok: dict[str, int] = {}
for label, _, out in procs:
    try:
        txt = out.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        txt = ""
    downs = [l for l in txt.split("\n") if l.startswith("DOWN")]
    ticks = [l for l in txt.split("\n") if l.startswith("TICK")]
    ok[label] = len(downs)
    print(f"     {label:<8} DOWN {len(downs)}  心跳 {len(ticks)}")

for _, p, _ in procs:
    p.terminate()

print()
print("  ═══ 判读 ═══")
a5, f9 = ok.get("右Alt", 0), ok.get("F9", 0)
if a5 and f9:
    print("     ✅ 两个键都认到 —— **键和监视器都没问题**")
    print("        那问题在产品的链路里（监视器 -> 主进程 -> 渲染层 -> sidecar）")
elif f9 and not a5:
    print("     🔴 **F9 认到、右 Alt 认不到** —— 就是 VK 0xA5 这个键的问题")
    print("        换成 F9 即可（设置页里能改）")
elif a5 and not f9:
    print("     ⚠️ 右 Alt 认到、F9 没认到（少见）—— 可能是合成按键的残留")
elif max(a5, f9) == 0:
    print("     ❌ 两个都没认到")
    print("        · 若上面「合成按键」那一栏也全空 -> **监视器收不到任何键**")
    print("          （比「键不对」更基础，要继续查监视器）")
    print("        · 若合成那一栏有结果 -> 是**你的物理按键没被收到**")

for _, _, out in procs:
    try:
        out.unlink()
    except OSError:
        pass
