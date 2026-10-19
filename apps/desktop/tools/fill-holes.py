"""
后处理：把"被角色完全围住的透明区域"填回不透明。

为什么这类区域是**缺陷**而不是"发缝"：
  真发缝是**开放**的（与画面边缘的透明区连通），所以不在"内部洞"里。
  被完全围住的透明区说明是抠图时误抠的（描边细缝导致洪泛漏进角色内部），
  深色壁纸会从那里透出来，看着像残留的浅色缝隙。

填法：用洞周围一圈的不透明像素做**最近邻填充**（逐层向内推进），
这样填出来的颜色与周围头发一致，不会突兀。

用法：
    python tools/fill-holes.py 输入贴图 输出贴图 [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image
import numpy as np


def enclosed_transparent(alpha: np.ndarray) -> np.ndarray:
    """
    返回"被角色完全围住"的透明/空洞掩码。

    做法：从画面四边洪泛透明区（那是外部背景），剩下没被洪泛到的透明像素
    就是被围住的洞。
    """
    trans = alpha < 250          # 视为"不完整"的像素（全透明或半透明）
    ext = np.zeros_like(trans)
    ext[0, :] = trans[0, :]
    ext[-1, :] = trans[-1, :]
    ext[:, 0] = trans[:, 0]
    ext[:, -1] = trans[:, -1]
    while True:
        nxt = ext.copy()
        nxt[1:, :] |= ext[:-1, :]
        nxt[:-1, :] |= ext[1:, :]
        nxt[:, 1:] |= ext[:, :-1]
        nxt[:, :-1] |= ext[:, 1:]
        nxt &= trans
        if np.array_equal(nxt, ext):
            break
        ext = nxt
    return trans & ~ext


def fill_by_nearest_hair(image: np.ndarray, holes: np.ndarray) -> np.ndarray:
    """
    把洞填成**最近的"头发色"像素的颜色**。

    为什么不是"取周围邻居的平均值"（我第一版这么做的，失败了）：
    这些洞是被**深色描边**围出来的，取平均会把描边的黑色混进来，
    填出一块 RGB(106,107,105) 的中灰斑 —— 比原来的透明洞还难看。（实测。）

    正确做法：从洞向外找最近的、**非描边**的不透明像素，用它自己的颜色。
    判据用"饱和度"：描边是近黑（sat 很低），头发是有色的（sat 较高）。
    这样呆毛围出的空隙会填成头发的蓝色，衔接自然。
    """
    out = image.copy()
    h, w = holes.shape

    rgb = out[..., :3].astype(int)
    sat = rgb.max(axis=2) - rgb.min(axis=2)
    opacity = out[..., 3]
    # 可作为"取色源"的像素：不透明、且不是近黑描边
    source = (opacity >= 250) & (~holes) & (sat >= 18)

    # 多源 BFS：所有源同时向外扩散，洞像素取最先到达它的那个源的颜色
    from collections import deque

    color = np.zeros((h, w, 3), dtype=np.uint8)
    seen = np.zeros((h, w), dtype=bool)
    queue: deque[tuple[int, int]] = deque()
    ys, xs = np.where(source)
    for y, x in zip(ys.tolist(), xs.tolist()):
        seen[y, x] = True
        color[y, x] = out[y, x, :3]
        queue.append((y, x))

    if not queue:
        return out

    while queue:
        y, x = queue.popleft()
        for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if 0 <= ny < h and 0 <= nx < w and not seen[ny, nx]:
                seen[ny, nx] = True
                color[ny, nx] = color[y, x]
                queue.append((ny, nx))

    filled_holes = holes & seen
    out[filled_holes, 0] = color[filled_holes, 0]
    out[filled_holes, 1] = color[filled_holes, 1]
    out[filled_holes, 2] = color[filled_holes, 2]
    out[filled_holes, 3] = 255
    return out


def fill_by_grow(image: np.ndarray, holes: np.ndarray) -> np.ndarray:
    """
    用"逐层向内扩散"把洞填上：每轮把已定色的像素颜色传给相邻的洞像素。
    比"整块用平均值"更自然（渐变会保留周围头发的层次）。
    """
    out = image.copy()
    remaining = holes.copy()
    # 已定色区域：不透明且不属于洞
    known = ~remaining

    guard = 0
    while remaining.any() and guard < 400:
        guard += 1
        acc = np.zeros_like(out[..., :3], dtype=np.float64)
        cnt = np.zeros(out.shape[:2], dtype=np.float64)
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            # ⚠️ 颜色与"已知掩码"必须用**同一个** shift。
            # 早先我让颜色 shift(dy,dx)，却让已知掩码 shift(-dy,-dx)，
            # 于是取到的是对面那一侧的邻居 —— 结果填充值全错，
            # 而且恰好与原值接近，肉眼看不出区别（白折腾一轮）。
            shifted_color = np.roll(out[..., :3], shift=(dy, dx), axis=(0, 1)).astype(np.float64)
            shifted_known = np.roll(known, shift=(dy, dx), axis=(0, 1))
            # roll 是环绕的，边界处要清掉，否则会把画面另一端的像素当邻居
            if dy == -1:
                shifted_known[-1, :] = False
            elif dy == 1:
                shifted_known[0, :] = False
            if dx == -1:
                shifted_known[:, -1] = False
            elif dx == 1:
                shifted_known[:, 0] = False
            take = remaining & shifted_known
            acc[take] += shifted_color[take]
            cnt[take] += 1

        fillable = remaining & (cnt > 0)
        if not fillable.any():
            break
        out[fillable, 0] = (acc[fillable, 0] / cnt[fillable]).round().astype(np.uint8)
        out[fillable, 1] = (acc[fillable, 1] / cnt[fillable]).round().astype(np.uint8)
        out[fillable, 2] = (acc[fillable, 2] / cnt[fillable]).round().astype(np.uint8)
        out[fillable, 3] = 255
        known |= fillable
        remaining &= ~fillable

    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="填回贴图里被围住的透明洞")
    parser.add_argument("source")
    parser.add_argument("-o", "--out", required=True)
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写文件")
    args = parser.parse_args()

    img = Image.open(args.source).convert("RGBA")
    arr = np.array(img)
    alpha = arr[..., 3]

    holes = enclosed_transparent(alpha)
    print(f"贴图 {img.size}")
    print(f"被围住的透明/半透明像素：{int(holes.sum())}（占 {holes.sum()/holes.size:.3%}）")

    if args.dry_run:
        return 0

    filled = fill_by_nearest_hair(arr, holes)
    left = (holes & (filled[..., 3] < 250)).sum()
    if left:
        print(f"  警告：有 {int(left)} 个像素找不到取色源（四周全透明），保持原样")

    out_img = Image.fromarray(filled, "RGBA")
    out_img.save(args.out)
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
