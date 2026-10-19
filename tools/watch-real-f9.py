"""重启 + 盯一次**真人**的 F9 切换（6 分钟窗口）。

## 为什么要这么久

前面几次"没反应"有一部分是**时机**：我挂 150 秒的窗口，
而用户看到消息、按键、说话往往要更久。所以这次给 6 分钟。

## 这一轮要验的是三处改动

  ① **VAD 阈值调低**（`energy_ratio` 3.0 -> 2.0、`noise_adapt` 0.05 -> 0.02，
     并**接线到配置**）—— 之前阈值被背景噪声推到 0.042，
     实测语音 0.07~0.63，余量太小；
  ② **时长门槛** `MIN_FORCED_DURATION_S = 0.3` —— 按两下没说话不送识别；
  ③ **整段 RMS 门槛** —— 静音不送识别（Whisper 对静音会造句子）。

判据：日志里出现「语音识别（…）」且文字**是你说的内容**。
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

DESKTOP = ROOT / "apps" / "desktop"
MLOG = DESKTOP / "pet-data" / "main.log"
SLOG = DESKTOP / "dist" / "startup.log"

KEYS = ("按住说话", "采集闸门", "采集电平", "主动收尾", "开始识别", "语音识别",
        "以语音输入", "太安静", "当作误触", "收尾没拿到")

WATCH_S = 360


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


print("  ═══ 重启应用（加载新 VAD 参数）═══")
subprocess.run(
    ["powershell", "-NoProfile", "-Command",
     "Get-Process electron -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue"],
    capture_output=True, text=True,
)
time.sleep(3)
for p in (MLOG, SLOG):
    try:
        p.unlink()
    except OSError:
        pass
env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
subprocess.Popen(
    [str(ROOT / "node_modules" / ".pnpm" / "electron@32.3.3" / "node_modules"
         / "electron" / "dist" / "electron.exe"),
     ".", "--dev", "--no-sandbox", "--debug-port=9222"],
    cwd=str(DESKTOP), env=env,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=0x00000008,
)
print("  等 45 秒…")
time.sleep(45)

base = lines()
n0 = len(base)
print(f"  启动后相关日志 {n0} 条：")
for l in base[-4:]:
    print(f"     {l[:110]}")
print()
print("  ══════════════════════════════════════════════════════════")
print("  👉 请现在 **按 F9 说一句**（说清楚点），说完 **再按一次 F9**")
print("     最多等 6 分钟")
print("  ══════════════════════════════════════════════════════════")
print()

t0 = time.monotonic()
last = n0
levels = 0
done = False
while time.monotonic() - t0 < WATCH_S:
    time.sleep(1.5)
    ls = lines()
    if len(ls) > last:
        for l in ls[last:]:
            if "采集电平" in l:
                levels += 1
                continue
            print(f"     [{time.monotonic() - t0:5.1f}s] {l[:112]}", flush=True)
        last = len(ls)
        if any("语音识别（" in x or "太安静" in x or "当作误触" in x or "收尾没拿到" in x
               for x in ls[n0:]):
            done = True
            time.sleep(4)
            break
    elif int(time.monotonic() - t0) % 60 == 0:
        print(f"     [{time.monotonic() - t0:5.1f}s] （还在等…按 F9）", flush=True)
        time.sleep(1)

print()
if levels:
    print(f"  （采集电平 {levels} 条）")
print()
print("  ═══ 判读 ═══")
import re  # noqa: E402

got = lines()[n0:]
j = "\n".join(got)
rec = [l for l in got if "语音识别（" in l]

if rec:
    m = re.search(r"语音识别（([\d.]+)s，耗时 (\d+)ms）：(.*)$", rec[-1])
    if m:
        print(f"     音频 {m.group(1)}s  耗时 {m.group(2)}ms")
        print(f"     识别：「{m.group(3)}」")
        print()
        bad = any(k in m.group(3) for k in ("简体中文", "字幕志愿者", "剧场", "明镜", "订阅"))
        if bad:
            print("     🔴 **还是幻觉** —— 阈值/门槛还需要再调")
        else:
            print("     🎉 **成功** —— 识别出的是真实内容")
elif "太安静" in j:
    print("     ⚠️ 被「整段太安静」门槛拦了")
    print("        说明阈值仍高于你的说话能量 —— 要么调小 energy_ratio，")
    print("        要么调大 input_gain（配置里 asr.input_gain）")
elif "当作误触" in j:
    print("     ⚠️ 被「太短」门槛拦了（音频 < 0.3s）—— 可能是按太快")
elif "按住说话" in j:
    print("     ⚠️ 走到了按住说话流程但没识别")
else:
    print("     ❌ 6 分钟内没有收到 F9 —— 应用可能没起来，或者热键没生效")
    print("        检查 `dist/startup.log` 里有没有「按住说话已启动（切换式）」")

# 把电平也打出来，方便看阈值
lv = [l for l in got if "采集电平" in l]
if lv:
    print()
    print("  ═══ 采集电平（看阈值 vs 语音）═══")
    for l in lv[:10]:
        print(f"     {l[:126]}")
