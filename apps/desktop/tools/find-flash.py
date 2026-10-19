"""找出"拖拽出探头模式时的闪现"发生在哪几帧。

用户报告：退出探头（拖离边缘）时会"闪现"一下。

"闪现"的特征通常是：某几帧画面内容**突变**（立绘位置/形状跳变），
或者出现一帧异常（比如整帧空、或立绘出现在错误位置）。

判据：逐帧量立绘的不透明包围盒，看相邻帧之间是否出现**大跳**。
和"平滑动画"的区别是：平滑动画每帧只变几个像素，闪现会一次跳几十甚至上百。
"""

from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
jump_px = int(sys.argv[2]) if len(sys.argv) > 2 else 40

files = sorted(glob.glob(str(src / "f*.png")))
if not files:
    print("  没找到帧")
    raise SystemExit(1)
print(f"  共 {len(files)} 帧；跳变判定 > {jump_px}px")
print(f"  {'帧':<10} {'包围盒 (x0,y0,x1,y1)':<26} {'尺寸':<12} 位移")

prev = None
jumps: list[tuple[str, int, int]] = []
for f in files:
    img = Image.open(f).convert("RGB")
    a = np.array(img).astype(int)
    mx = a.max(axis=2)
    mn = a.min(axis=2)
    sat = mx - mn
    # 立绘是高饱和的（蓝发白衣）；壁纸饱和度低
    mask = (sat > 60) & (mx > 80)
    ys, xs = np.where(mask)
    name = os.path.basename(f)
    if len(xs) < 200:
        print(f"  {name:<10} (几乎无立绘: {len(xs)} px)  <<< 可能是闪帧")
        prev = None
        continue
    box = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))
    w, h = box[2] - box[0] + 1, box[3] - box[1] + 1
    if prev is None:
        delta = ""
    else:
        dx = box[0] - prev[0]
        dy = box[1] - prev[1]
        delta = f" dx={dx:+4d} dy={dy:+4d}"
        if abs(dx) > jump_px or abs(dy) > jump_px:
            delta += "   <<< 跳变"
            jumps.append((name, dx, dy))
    print(f"  {name:<10} {str(box):<26} {w:>4}x{h:<6}{delta}")
    prev = box

print(f"\n  跳变帧数：{len(jumps)}")
for name, dx, dy in jumps:
    print(f"    {name}: dx={dx:+d} dy={dy:+d}")
