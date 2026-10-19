"""全程盯一轮按键，把**每一环**都记下来（含幽灵 end 的来源）。

## 为什么要盯这么细

幽灵 `end`（没有对应 `start`）会把闸门关掉、丢掉用户正在说的话。
我加了 `[按住说话] 渲染层发来 <phase>` 日志，现在要抓它。

## 监控四层（时间对齐）

  ① `pet-data/ptt-tmp/probe-key.out` —— PowerShell 监视器认到的 DOWN/UP
  ② `main.log` 的 `监视器输出 <-` —— Node 读到那些行了吗
  ③ `main.log` 的 `[按住说话]` / `渲染层发来` —— 主进程发了什么
  ④ `main.log` 的 `按住说话：收到 phase=` / `采集闸门` —— sidecar 收到什么

## 判读（对照表）

| ①有DOWN | ②有"监视器输出" | ③有"渲染层发来 start" | ④有"phase='start'" | 结论 |
|---|---|---|---|---|
| ✅ | ✅ | ✅ | ✅ | 链路全通 |
| ✅ | ✅ | ❌ | ❌ | 主进程没读/没转发 |
| ✅ | ❌ | ❌ | ❌ | Node 侧读文件断了 |
| ❌ | ❌ | ❌ | ❌ | 监视器没认到按键 |

幽灵 `end` 就看 ③：**如果 ③ 没有对应的「渲染层发来 end」而 ④ 有**，
说明它**不是通过 `pet:voice-ptt` 进来的**（那就要查别处）。
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

PROBE = ROOT / "apps" / "desktop" / "pet-data" / "ptt-tmp" / "probe-key.out"
MLOG = ROOT / "apps" / "desktop" / "pet-data" / "main.log"

WATCH_S = 150

KEYS = ("按住说话", "渲染层发来", "采集闸门", "监视器输出", "主动收尾", "开始识别",
        "语音识别", "收尾没拿到", "以语音输入")


def probe_lines() -> list[str]:
    try:
        return [l.strip() for l in PROBE.read_text(encoding="utf-8", errors="replace").split("\n") if l.strip()]
    except OSError:
        return []


def log_lines() -> list[str]:
    try:
        return [
            l.strip()
            for l in MLOG.read_text(encoding="utf-8", errors="replace").split("\n")
            if "[brain]" not in l and "帧头" not in l and any(k in l for k in KEYS)
        ]
    except OSError:
        return []


p0 = len(probe_lines())
l0 = len(log_lines())
print(f"  起始：probe-key.out {p0} 行，main.log（相关）{l0} 条")
print()
print("  ═══ 请按住右 Alt 说一句，然后松开（最多等 150 秒）═══")
print()

t0 = time.monotonic()
last_p, last_l = p0, l0
while time.monotonic() - t0 < WATCH_S:
    time.sleep(1.5)
    p, l = len(probe_lines()), len(log_lines())
    if p != last_p:
        for line in probe_lines()[last_p:p]:
            print(f"     [{time.monotonic() - t0:5.1f}s] ①监视器  {line}")
        last_p = p
    if l != last_l:
        for line in log_lines()[last_l:l]:
            tag = "③主进程"
            if "监视器输出" in line:
                tag = "②Node读"
            elif "phase=" in line or "采集闸门" in line or "收尾" in line:
                tag = "④sidecar"
            print(f"     [{time.monotonic() - t0:5.1f}s] {tag}  {line[:100]}")
        last_l = l
    # 出现识别结果就算一轮结束
    if any("语音识别（" in x or "收尾没拿到" in x for x in log_lines()[l0:]):
        time.sleep(2)
        break

print()
print("  ═══ 这一轮汇总 ═══")
probe = probe_lines()[p0:]
logs = log_lines()[l0:]

print(f"     ① 监视器认到：{probe if probe else '（无）'}")
print(f"     ② Node 读到  ：{[x for x in logs if '监视器输出' in x] or '（无）'}")
print(f"     ③ 主进程发出：{[x.split('] ')[-1][:40] for x in logs if '渲染层发来' in x] or '（无）'}")
print(f"     ④ sidecar 收：{[x.split('：')[-1][:30] for x in logs if 'phase=' in x] or '（无）'}")

print()
print("  ═══ 对照表判读 ═══")
had_down = any("DOWN" in x for x in probe)
had_read = any("监视器输出" in x for x in logs)
had_fwd = any("渲染层发来" in x for x in logs)
had_side = any("phase=" in x for x in logs)

print(f"     ① 监视器认到 DOWN : {'✅' if had_down else '❌'}")
print(f"     ② Node 读到       : {'✅' if had_read else '❌'}")
print(f"     ③ 主进程转发      : {'✅' if had_fwd else '❌'}")
print(f"     ④ sidecar 收到    : {'✅' if had_side else '❌'}")
print()
if had_down and had_read and had_fwd and had_side:
    print("     链路全通 ✅")
elif had_down and had_read and not had_fwd:
    print("     🔴 断在 **主进程没读/没转发** —— 看 ② 有但 ③ 没有")
elif had_down and not had_read:
    print("     🔴 断在 **Node 侧读文件** —— 监视器写了但没读到")
elif not had_down:
    print("     🔴 断在 **监视器** —— 一个 DOWN 都没认到")

ends = [x for x in logs if "phase='end'" in x]
starts = [x for x in logs if "phase='start'" in x]
if len(ends) > len(starts):
    print()
    print(f"     ⚠️ **end({len(ends)}) 比 start({len(starts)}) 多** —— 有幽灵 end！")
    print("        对照 ③：如果没有同样多的「渲染层发来 end」，")
    print("        说明它不是走 pet:voice-ptt 来的。")
