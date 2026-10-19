"""量"立绘在画布里实际占多宽多高"。

---- 为什么需要这个 ----

用户反馈：「h5 窗口太大，不贴合宠物，导致还没到屏幕边缘就自动探头」。

根因是贴边判定用的是**窗口 bounds**，而窗口 348 宽、立绘只有 240 —
四周那 54px 透明留白也算进去了，于是立绘离屏幕边还有 54px 时
窗口的边就已经"碰到"屏幕了，判定提前触发。

要修就得让窗口**贴合立绘**。但立绘在画布里的实际占用未必是满的
（角色通常是中间一坨、四周透明），所以得先量出来真实的外接矩形，
否则缩了窗口反而会把立绘裁掉。
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
from PIL import Image

src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")

# 找所有渲染出来的帧（RGBA 或已合成）
files = sorted(glob.glob(str(src / "*.png")))
if not files:
    print("  没找到 png")
    raise SystemExit(1)

for f in files[:6]:
    img = Image.open(f)
    has_alpha = img.mode in ("RGBA", "LA") or "transparency" in img.info
    rgba = img.convert("RGBA")
    a = np.array(rgba)
    if has_alpha:
        mask = a[..., 3] > 8
    else:
        # 没有 alpha 通道（capturePage 会把透明合成成白底）-> 用"非纯白"当立绘
        rgb = a[..., :3].astype(int)
        mask = ~((rgb[..., 0] > 246) & (rgb[..., 1] > 246) & (rgb[..., 2] > 246))
    ys, xs = np.where(mask)
    name = Path(f).name
    if len(xs) == 0:
        print(f"  {name}: 画面全空")
        continue
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    w, h = x1 - x0 + 1, y1 - y0 + 1
    print(
        f"  {name}  图 {a.shape[1]}x{a.shape[0]}  alpha={has_alpha}\n"
        f"      立绘外接 {w}x{h}，偏移 x0={x0} y0={y0}\n"
        f"      占画面比例 宽 {w / a.shape[1]:.1%} 高 {h / a.shape[0]:.1%}"
    )
