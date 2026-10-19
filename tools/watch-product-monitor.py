"""盯**产品监视器自己的输出文件**，等你手按一次。

## 为什么盯这个文件而不是 main.log

`main.log` 里的 `监视器输出 <- DOWN` 是**主进程读到的**（多一层）。
而这个文件是**监视器直接写的** —— 少一层就少一个可疑点。

如果这里出现 `DOWN 165`，说明**监视器和键都好**，问题在后面的链路。
如果这里一直没有，说明**监视器根本读不到这个键**。

## 怎么读

每 1 秒看一次文件末尾，把新增的 DOWN/UP 行打出来（TICK 不显示）。
同时报心跳间隔 —— 间隔稳定就说明循环在正常跑。
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

OUT = ROOT / "apps" / "desktop" / "pet-data" / "ptt-tmp" / "probe-key.out"
WATCH_S = 180


def rows() -> list[str]:
    try:
        txt = OUT.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    return [l.strip() for l in txt.split("\n") if l.strip()]


start = rows()
n0 = len(start)
print(f"  文件现在 {n0} 行")
print(f"  最后一行：{start[-1] if start else '（空）'}")
print()
print("  ═══ 请**按住右 Alt 说一句短的**，然后松开（最多等 180 秒）═══")
print()
print("  ⚠️ 如果 180 秒内一直没出现 DOWN 165，那说明监视器读不到这个键，")
print("     我们再换键试（F9 之类）。")
print()

t0 = time.monotonic()
last = n0
got_any = False
while time.monotonic() - t0 < WATCH_S:
    time.sleep(1)
    r = rows()
    if len(r) > last:
        for line in r[last:]:
            if line.startswith("TICK"):
                continue
            print(f"     [{time.monotonic() - t0:5.1f}s]  ✅ {line}")
            got_any = True
        last = len(r)
    # 每 20 秒报一次心跳状态（证明循环在跑）
    if int(time.monotonic() - t0) % 20 == 0 and int(time.monotonic() - t0) > 0:
        ticks = [x for x in r if x.startswith("TICK")]
        if ticks:
            print(f"     [{time.monotonic() - t0:5.1f}s] （循环正常，心跳最新 {ticks[-1]}）")
            time.sleep(1)

print()
print("  ═══ 结果 ═══")
r = rows()
ticks = [x for x in r if x.startswith("TICK")]
downs = [x for x in r if x.startswith("DOWN")]
ups = [x for x in r if x.startswith("UP")]
print(f"     心跳 {len(ticks)} 条（循环{( '正常' if len(ticks) > len([x for x in start if x.startswith('TICK')]) else '**没在涨**')}）")
print(f"     DOWN {len(downs)}   UP {len(ups)}")
print()
if got_any:
    print("     ✅ **监视器认到你的按键了**")
    print("        那么问题在后面的链路（主进程 -> 渲染层 -> sidecar），我接着查。")
else:
    print("     ❌ 180 秒内没有 DOWN")
    print(f"        心跳在涨 => 循环在跑 => **`GetAsyncKeyState(0xA5)` 读不到右 Alt**")
    print()
    print("        下一步：换成 F9 试。设置页能改「按住说话」的键，")
    print("        或者我直接改配置（providers.push_to_talk.vk = 0x78）。")
