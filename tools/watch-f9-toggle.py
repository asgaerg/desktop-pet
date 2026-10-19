"""盯 F9 切换式 PTT 的完整链路。

## 预期（按一次 -> 说 -> 再按一次）

```
[按住说话] 按下（F9）-> 开始采集（再按一次结束）
                              采集闸门：开
（说话中，采集电平打点）
[按住说话] 再按一次 -> 结束采集，送去识别
                              松开 -> 主动收尾（N.NNs，forced）
                              开始识别：音频 N.NNs，超时上限 XXs
                              语音识别（N.NNs，耗时 XXXms）：<文字>
```

## 判据

  · 有「按下（F9）」        -> **globalShortcut 生效**（这是关键）
  · 有「再按一次」           -> 切换语义生效
  · 有「主动收尾」+ 识别     -> 整条链路通
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

LOG = ROOT / "apps" / "desktop" / "pet-data" / "main.log"
WATCH_S = 150

KEYS = ("按住说话", "采集闸门", "采集电平", "主动收尾", "开始识别", "语音识别",
        "以语音输入", "收尾没拿到", "僵住")


def lines() -> list[str]:
    try:
        return [
            l.strip()
            for l in LOG.read_text(encoding="utf-8", errors="replace").split("\n")
            if "[brain]" not in l and "帧头" not in l and any(k in l for k in KEYS)
        ]
    except OSError:
        return []


n0 = len(lines())
print(f"  起始 {n0} 条相关日志")
print()
print("  ═══ 请按 **F9** 开始说，说完再按一次 **F9**（最多等 150 秒）═══")
print()
print("  ⚠️ 注意是 **F9**，不再是右 Alt（右 Alt 在 accelerator 体系里表达不了）")
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
                continue  # 太密，只计数
            print(f"     [{time.monotonic() - t0:5.1f}s] {l[:112]}")
        last = len(ls)
        if any("语音识别（" in x or "收尾没拿到" in x for x in ls[n0:]):
            time.sleep(2)
            break

print()
if levels:
    print(f"     （另有 {levels} 条采集电平，已省略）")
print()
print("  ═══ 判读 ═══")
got = lines()[n0:]
joined = "\n".join(got)

if not got:
    print("     ❌ 什么日志都没有")
    print("        -> **F9 没被 globalShortcut 收到**（或者你没按到）")
    print("        检查：应用还在跑吗？日志里有「按住说话已启动（切换式）」吗？")
else:
    checks = [
        ("按下（F9）开始采集", "按下（F9）"),
        ("再按一次结束", "再按一次"),
        ("闸门打开", "采集闸门：开"),
        ("收尾并送识别", "主动收尾"),
        ("识别出结果", "语音识别（"),
    ]
    for label, needle in checks:
        mark = "✅" if needle in joined else "❌"
        print(f"     {mark} {label}")
    print()
    if "语音识别（" in joined:
        print("     🎉 **完整成功** —— 切换式 PTT 端到端可用")
    elif "按下（F9）" in joined:
        print("     ✅ globalShortcut 生效了（这是最关键的一步）")
        print("        后面哪一步断了看上面的勾选表")
    else:
        print("     ❌ 连「按下（F9）」都没有 —— 热键没收到")
