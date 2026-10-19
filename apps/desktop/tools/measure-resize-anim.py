"""逐帧测"宠物可见区域的包围盒尺寸"，判断参考应用是不是在**动画窗口尺寸**。

---- 为什么关心这个 ----

我这边的实现里：
  · 画布旋转有 CSS transition（220ms）-> 丝滑；
  · 窗口尺寸是 `setBounds` 一帧到位 -> **瞬间跳**。
如果参考应用的尺寸是**逐帧渐变**的，说明它把"变大"也做成了动画，
那我就该补上这一步（否则变身那一瞬间会显得生硬）。

判据：连续帧之间尺寸若只变化几像素、连续十几帧完成过渡 -> 是动画；
      若一帧之内从 A 跳到 B -> 是瞬变。
"""

from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
start = int(sys.argv[2]) if len(sys.argv) > 2 else 0
end = int(sys.argv[3]) if len(sys.argv) > 3 else 60

files = sorted(glob.glob(str(src / "f*.png")))[start:end]
print(f"  分析 {len(files)} 帧：{os.path.basename(files[0])} .. {os.path.basename(files[-1])}")
print(f"  {'帧':<10} {'包围盒':<26} {'宽x高':<12} 尺寸变化")

prev: tuple[int, int] | None = None
jumps = 0
for f in files:
    rgb = np.array(Image.open(f).convert("RGB")).astype(int)
    mx = rgb.max(axis=2)
    mn = rgb.min(axis=2)
    sat = mx - mn
    # 参考应用的窗口底色是浅灰，宠物是深色高饱和的 -> 用"饱和 + 不暗"
    mask = (sat > 40) & (mx > 60)
    ys, xs = np.where(mask)
    name = os.path.basename(f)
    if len(xs) < 200:
        print(f"  {name:<10} (无匹配像素)")
        prev = None
        continue
    box = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))
    w, h = box[2] - box[0], box[3] - box[1]
    if prev is None:
        delta = ""
    else:
        dw, dh = w - prev[0], h - prev[1]
        delta = f"  dw={dw:+4d} dh={dh:+4d}"
        if abs(dw) > 40 or abs(dh) > 40:
            delta += "   <<< 一帧内大跳"
            jumps += 1
    print(f"  {name:<10} {str(box):<26} {w:>4}x{h:<5}{delta}")
    prev = (w, h)

print(f"\n  一帧内尺寸大跳的次数：{jumps}")
print("  判读：次数多 = 瞬变；为零且尺寸逐帧小幅变化 = 有动画过渡")
