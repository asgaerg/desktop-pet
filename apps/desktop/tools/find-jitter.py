"""逐帧测宠物的**质心**，用高频抖动判定"乱飘"。

---- 为什么用蓝色质心 ----

试过两种更简单的办法都不行（都实测过）：
  · 亮度阈值：壁纸里有亮色图标，包围盒永远是整帧；
  · 饱和度阈值：画面左边缘有高饱和像素，x0 恒为 0，测不出宠物位移。
宠物是**蓝发 + 白衣**，而壁纸（暗色书架）几乎没有蓝色 ——
所以只认"蓝色像素"（蓝通道明显高于红绿，且有一定亮度），
再算它们的质心。质心对局部噪声不敏感，正好用来测"整体在不在乱飘"。

---- 判据 ----

"乱飘"的特征不是位移大，而是**方向来回变**（一帧 +30、下一帧 -30）。
所以统计相邻帧位移的**符号翻转次数**；平滑移动时符号基本不变。
"""

from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
files = sorted(glob.glob(str(src / "f*.png")))
print(f"  共 {len(files)} 帧")
print(f"  {'帧':<10} {'质心 (x, y)':<22} {'蓝像素':>7}  位移")

centroids: list[tuple[float, float] | None] = []
for f in files:
    rgb = np.array(Image.open(f).convert("RGB")).astype(int)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    # 蓝色：b 明显高于 r 和 g，且不是接近黑的暗部
    mask = (b > r + 25) & (b > g + 15) & (b > 70)
    ys, xs = np.where(mask)
    name = os.path.basename(f)
    if len(xs) < 50:
        print(f"  {name:<10} (蓝像素太少: {len(xs)})")
        centroids.append(None)
        continue
    cx = float(xs.mean())
    cy = float(ys.mean())
    centroids.append((cx, cy))
    prev = centroids[-2] if len(centroids) >= 2 else None
    if prev:
        dx = cx - prev[0]
        dy = cy - prev[1]
        print(f"  {name:<10} ({cx:>6.1f}, {cy:>5.1f})    {len(xs):>6}  dx={dx:>+6.1f} dy={dy:>+6.1f}")
    else:
        print(f"  {name:<10} ({cx:>6.1f}, {cy:>5.1f})    {len(xs):>6}  —")

# 符号翻转统计（只统计连续有效帧）
flips = 0
pairs = 0
last_sign = 0
for i in range(1, len(centroids)):
    a, b = centroids[i - 1], centroids[i]
    if a is None or b is None:
        last_sign = 0
        continue
    dx = b[0] - a[0]
    if abs(dx) < 0.5:
        continue
    sign = 1 if dx > 0 else -1
    if last_sign != 0 and sign != last_sign:
        flips += 1
    last_sign = sign
    pairs += 1

print(f"\n  有效位移帧对：{pairs}")
print(f"  方向翻转次数：{flips}")
if flips >= 5:
    print("  ==> 判定：**存在乱飘**（方向频繁翻转，不是平滑移动）")
else:
    print("  ==> 判定：位移基本平滑，未检出乱飘")
