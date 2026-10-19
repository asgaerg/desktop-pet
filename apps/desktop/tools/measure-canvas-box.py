"""量立绘在**画布坐标系**里的可见包围盒。

---- 为什么要这个 ----

用户要求（原话）：「窗口现在完全留给宠物」「现在问题是窗口太大导致无法完美
贴合屏幕边缘触发探头」。

立绘在画布里的位置就是立绘自己的位置（画布是原点）。所以只要在真实渲染帧里
量出不透明像素的包围盒，就能把**窗口缩到和立绘一样大** ——
窗口边 == 立绘边，贴边判定不再需要任何估算的留白。

输出同时给出"画布 CSS 尺寸"，用于把测量值折算成 canvas 坐标。
"""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
info_path = src / "info.json"

canvas_css = None
win_logical = None
if info_path.exists():
    info = json.loads(info_path.read_text(encoding="utf-8"))
    canvas_css = info.get("canvasCss")
    win_logical = info.get("win")

files = sorted(glob.glob(str(src / "f*.png")))
if not files:
    print("  没找到截图")
    raise SystemExit(1)

print(f"  样本 {len(files)} 张")
if canvas_css:
    print(f"  画布 CSS 尺寸 {canvas_css}，窗口逻辑尺寸 {win_logical}")

worst = None
for f in files:
    img = Image.open(f).convert("RGBA")
    a = np.array(img)
    # ⚠️ 阈值必须高：drop-shadow 的羽化会和本体连通，阈值低会把阴影算进立绘
    mask = a[..., 3] > 200
    ys, xs = np.where(mask)
    if len(xs) < 100:
        continue
    box = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))
    w = box[2] - box[0] + 1
    h = box[3] - box[1] + 1
    if worst is None or w * h > worst[0] * worst[1]:
        worst = (w, h, box, Path(f).name)

w, h, box, name = worst
print(f"  最坏样本 {name}")
print(f"    截图坐标包围盒 x0={box[0]} y0={box[1]} w={w} h={h}")

# 截图像素 -> 画布 CSS 像素：按 dpr 缩放（截图是物理像素）
if canvas_css and win_logical:
    # 截图宽度对应窗口逻辑宽度的 dpr 倍
    dpr = round(a.shape[1] / win_logical[0], 3) if win_logical[0] else 1.25
    print(f"    推算 dpr = {dpr}")
    cx0 = box[0] / dpr
    cy0 = box[1] / dpr
    cw = w / dpr
    ch = h / dpr
    print(f"    折算到画布 CSS 坐标: x0={cx0:.1f} y0={cy0:.1f} w={cw:.1f} h={ch:.1f}")
    print(f"    => 画布 {canvas_css[0]}x{canvas_css[1]} 中，立绘占 {cw:.0f}x{ch:.0f}")
    print(f"    => 若要窗口贴合立绘，窗口应约 {round(cw)}x{round(ch)}（并把画布设成同尺寸）")
    print(f"    => 旋转 45° 后外接约 {round((cw + ch) * 0.7071)}x{round((cw + ch) * 0.7071)}")
