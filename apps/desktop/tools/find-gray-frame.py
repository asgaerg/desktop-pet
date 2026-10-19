"""在录制里找出"宠物四周出现灰色矩形"的帧。

用户报告：点击宠物、或把它从探头状态拖出来时，四周的窗口会显出灰色。

做法：先量出立绘的不透明包围盒，再看包围盒**外面那圈**像素的颜色。
正常情况下那圈应该是桌面/浏览器（颜色杂乱、亮度高）；
出问题时它会是**均匀的灰色**（R≈G≈B，中间亮度），且覆盖整圈。
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
if not files:
    print("  没找到帧")
    raise SystemExit(1)

print(f"  共 {len(files)} 帧")
print(f"  {'帧':<10} {'立绘外接':<16} {'立绘外一圈平均色':<22} {'灰?'}")

suspects: list[str] = []
for f in files:
    a = np.array(Image.open(f).convert("RGB")).astype(int)
    h, w, _ = a.shape
    mx = a.max(axis=2)
    mn = a.min(axis=2)
    sat = mx - mn
    # 立绘：高饱和（蓝发白衣）；页面背景是白/灰（低饱和）
    mask = (sat > 45) & (mx > 60)
    ys, xs = np.where(mask)
    name = os.path.basename(f)
    if len(xs) < 300:
        continue
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    # 取立绘外一圈（上下左右各外扩 4px 的一条环）
    pad = 4
    ring = np.zeros((h, w), dtype=bool)
    ring[max(0, y0 - pad) : y0, max(0, x0 - pad) : min(w, x1 + pad)] = True
    ring[min(h, y1 + 1) : min(h, y1 + 1 + pad), max(0, x0 - pad) : min(w, x1 + pad)] = True
    ring[max(0, y0 - pad) : min(h, y1 + 1 + pad), max(0, x0 - pad) : x0] = True
    ring[max(0, y0 - pad) : min(h, y1 + 1 + pad), min(w, x1 + 1) : min(w, x1 + 1 + pad)] = True
    if ring.sum() < 50:
        continue
    px = a[ring]
    mean = px.mean(axis=0).round().astype(int)
    spread = int(px.std(axis=0).mean())
    is_gray = (abs(mean[0] - mean[1]) < 12 and abs(mean[1] - mean[2]) < 12
               and 90 < mean.mean() < 240 and spread < 22)
    if is_gray:
        suspects.append(name)
    if len(suspects) <= 40 or is_gray:
        print(f"  {name:<10} {f'{x1-x0+1}x{y1-y0+1}':<16} RGB={tuple(mean)} σ={spread:<4} {'★灰' if is_gray else ''}")

print(f"\n  疑似灰框帧 {len(suspects)} 个：{', '.join(suspects[:20])}")
