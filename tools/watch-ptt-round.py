"""盯日志，等下一次「按住说话 -> 识别」的完整链路。

## 用法

后台跑起来，然后**按住右 Alt 说一句**。
脚本会把这一轮的每一步和耗时打出来。

## 判据（切到本地 GPU 之后应当是）

```
按住说话：收到 phase='start'          <- 按键生效
采集闸门：开
（说话中，采集电平打点）
按住说话：收到 phase='end'
松开 -> 主动收尾（N.NNs，forced）      <- 音频收齐
开始识别：音频 N.NNs，超时上限 XXs
语音识别（N.NNs，耗时 XXXms）：<文字>   <- **重点看这个耗时**
```

期望：`耗时` **300~500ms**（本地 GPU）。
若仍是几千毫秒 —— 说明没走本地，或者首次加载还没完成。
"""

from __future__ import annotations

import pathlib
import re
import sys
import time

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

LOG = ROOT / "apps" / "desktop" / "pet-data" / "main.log"
WAIT_S = 180

KEYS = ("phase=", "采集闸门", "主动收尾", "开始识别", "语音识别", "以语音输入", "采集电平", "收尾没拿到")


def snapshot() -> tuple[int, list[str]]:
    try:
        lines = LOG.read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        return 0, []
    keep = [
        l.strip()
        for l in lines
        if "[brain]" not in l and "帧头" not in l and any(k in l for k in KEYS)
    ]
    return len(lines), keep


print("  ═══ 等着你按键（最多 3 分钟）═══")
print("     请**按住右 Alt，说一句短话，松开**")
print()

_, before = snapshot()
n0 = len(before)
t0 = time.monotonic()
done = False
while time.monotonic() - t0 < WAIT_S:
    time.sleep(2)
    _, now = snapshot()
    new = now[n0:]
    if not new:
        continue
    # 出现「语音识别（...）」或者错误，就算一轮结束
    if any("语音识别（" in l or "收尾没拿到" in l or "识别超时" in l or "识别失败" in l for l in new):
        done = True
        break
    if any("phase='start'" in l for l in new):
        print("     👂 检测到按键了，正在录…")

print()
print("  ═══ 这一轮的完整链路 ═══")
_, now = snapshot()
new = now[n0:]
if not new:
    print("     （没等到任何按键事件）")
    print("     可能：你没按 / 按键没被监视器认到 / 应用没在跑")
else:
    for l in new:
        # 采集电平太密，压缩显示
        if "采集电平" in l:
            continue
        print(f"     {l[:118]}")
    levels = [l for l in new if "采集电平" in l]
    if levels:
        print(f"     （另有 {len(levels)} 条采集电平，已省略）")

print()
print("  ═══ 判读 ═══")
rec = [l for l in new if "语音识别（" in l]
if rec:
    m = re.search(r"耗时 (\d+)ms", rec[0])
    ms = int(m.group(1)) if m else -1
    print(f"     识别耗时 = {ms}ms")
    if 0 < ms < 800:
        print("     ✅ **本地 GPU 生效** —— 300~500ms 是它的正常水平")
    elif ms < 2000:
        print("     ✅ 可接受（可能含首次加载）")
    else:
        print("     ⚠️ 偏慢 —— 可能没走本地，或首次加载未完成")
else:
    print("     没有识别结果")
    if any("主动收尾" in l for l in new):
        print("     （音频收齐了但没出结果 —— 看上面有没有「开始识别」和超时日志）")
    else:
        print("     （连「主动收尾」都没有 —— 音频没收齐）")
print()
print(f"     日志：{LOG}")
