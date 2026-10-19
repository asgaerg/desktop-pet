"""量"立绘不透明像素到自身中心的距离"，算出旋转所需的最小窗口边长。

---- 为什么需要 ----

用户反馈：「h5 窗口太大，不贴合宠物，导致还没到屏幕边缘就自动探头」。
原来的窗口是按"立绘外接矩形旋转 45°"算的，但立绘不是实心矩形，
角上大量透明，所以那个尺寸偏大。

---- ⚠️ 两个必须避开的坑（都实际踩过）----

① **只看最大的连通块**。
   第一版直接用"所有 alpha>8 的像素"，结果量出半宽 198px —— 而画布只有 240，
   一半才 120，**物理上不可能**。原因是截图里混进了噪声：`filter: drop-shadow`
   的羽化边缘、以及窗口自身的残留像素。这些散点离中心很远，把最大值拉爆了。
   加连通域筛选（4 邻接）后才是真正的立绘。

② **用内容包围盒的中心，不用质心**。
   质心会被头发/裙摆的分布带偏；旋转是绕 `transform-origin: center`
   （即元素中心）做的，所以基准点必须是包围盒中心才对应实际渲染。
"""

from __future__ import annotations

import glob
import sys
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image

src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
scale = float(sys.argv[2]) if len(sys.argv) > 2 else 0.8


def largest_component(mask: np.ndarray) -> np.ndarray:
    """返回 mask 里最大的 4-邻接连通块。纯 numpy + 队列，避免依赖 scipy。"""
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    best = np.zeros_like(mask, dtype=bool)
    best_size = 0
    ys, xs = np.where(mask)
    for sy, sx in zip(ys.tolist(), xs.tolist()):
        if seen[sy, sx]:
            continue
        q = deque([(sy, sx)])
        seen[sy, sx] = True
        comp = []
        while q:
            y, x = q.popleft()
            comp.append((y, x))
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    q.append((ny, nx))
        if len(comp) > best_size:
            best_size = len(comp)
            best = np.zeros_like(mask, dtype=bool)
            cy = np.array([p[0] for p in comp])
            cx = np.array([p[1] for p in comp])
            best[cy, cx] = True
    return best


files = sorted(glob.glob(str(src / "f*.png")))
if not files:
    print("  没找到图片")
    raise SystemExit(1)

print(f"  样本 {len(files)} 张，scale={scale}")
best = None
for f in files:
    img = Image.open(f).convert("RGBA")
    a = np.array(img)
    mask = a[..., 3] > 200  # 阈值必须够高：drop-shadow 的羽化会与本体连通，阈值太低会把阴影算进立绘
    if mask.sum() < 100:
        continue
    comp = largest_component(mask)
    ys, xs = np.where(comp)
    if len(xs) < 100:
        continue
    # 用包围盒中心（对应 transform-origin: center），不用质心
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    half_w = float(max(abs(xs - cx).max(), 1))
    half_h = float(max(abs(ys - cy).max(), 1))
    if best is None or (half_w + half_h) > (best[0] + best[1]):
        best = (half_w, half_h, Path(f).name, (x1 - x0 + 1, y1 - y0 + 1))

half_w, half_h, name, box = best
print(f"  最坏样本 {name}（连通块外接 {box[0]}x{box[1]} 截图像素）")
print(f"    到中心的半宽 {half_w:.1f}  半高 {half_h:.1f}（截图像素）")
if scale != 1.0:
    half_w /= scale
    half_h /= scale
    print(f"    折算逻辑像素: 半宽 {half_w:.1f}  半高 {half_h:.1f}")
rot = (half_w + half_h) * (2**0.5) / 2
print(f"    旋转 45° 所需半径 ≈ {rot:.1f}  =>  窗口边长 ≈ {2 * rot:.0f}")
