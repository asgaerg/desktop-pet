"""从用户新给的立绘裁出头部，生成 `assets/tray.ico`（配一个可复用入口）。

## 为什么单独一个脚本而不是改 `make-tray-icon.py`

那个脚本的输入约定是 **256×256 单帧序列图**（`--frame 256 --frame-index N`），
因为它当初是给 `sheet.png` 写的。

用户这次给的是**一张整图**（1470×630，角色只占中间 375×513）。
套那个约定要伪造一堆参数（frame 宽度、帧索引），而且比例会算歪 ——
它的 `crop_head` 按"整帧"算比例（top 0.26），而整图里角色只占一部分，
45 + 513×0.26 = 182，那是**肩膀**不是头顶。

所以这里按**角色自己的包围盒**算比例 —— 这才是可迁移的做法：
先自动量出角色在哪，再按角色的比例裁头。

用法：
    python make-tray-icon-full.py                 # 用默认（新立绘 + 全头）
    python make-tray-icon-full.py --candidate 3   # 换一个裁法
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("需要 Pillow", file=sys.stderr)
    raise SystemExit(2)

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_SRC = ROOT / "assets" / "src" / "avatar-full.webp"
DEFAULT_OUT = ROOT / "assets" / "tray.ico"

# .ico 里要包含的尺寸。16 是托盘实际大小；48/256 给快捷方式和高 DPI。
ICON_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]

# 与 `crop-head-candidates.py` 里那张对比图**同一套定义**（编号一致）
# (名字, 从头顶往下的起点比, 到哪结束的比, 横向半宽系数)
CANDIDATES = [
    ("全头+辫子", 0.00, 0.62, 0.50),
    ("全头", 0.00, 0.55, 0.50),
    ("头略紧", 0.04, 0.52, 0.50),
    ("只要脸", 0.10, 0.50, 0.46),
    ("宽一点", 0.00, 0.58, 0.62),
    ("很紧(脸+眼)", 0.14, 0.48, 0.44),
]

# 默认用 #2「全头」：呆毛 + 头饰 + 下巴都在，16px 下也认得出
DEFAULT_CANDIDATE = 2


def character_bbox(image: Image.Image, skip_top_rows: int = 60) -> tuple[int, int, int, int]:
    """量出**角色**的包围盒，顺带排除角落碎片。

    ## ⚠️ 为什么要排除顶部若干行

    用户给的图左上角有一小块孤立碎片（约在 x<180 / y<60）。
    直接 `getbbox()` 会把它算进来，包围盒就往左上撑开了 ——
    于是"角色中心"偏移，裁出来的头会偏掉。

    做法：先忽略顶部 60 行扫一遍**列**，取最宽的连续内容块当角色所在列区间；
    再在该区间内扫**行**得到上下边界。碎片只有几十像素宽，一定输给角色（375 宽）。
    """
    px = image.load()
    width, height = image.size

    def solid(x: int, y: int) -> bool:
        r, g, b, a = px[x, y]
        # 透明的不算；接近纯白且不透明的也不算（那是 JPEG 压出来的白边）
        return a > 20 and not (r > 245 and g > 245 and b > 245)

    columns = []
    for x in range(width):
        columns.append(sum(1 for y in range(skip_top_rows, height, 2) if solid(x, y)))

    runs: list[tuple[int, int]] = []
    start: int | None = None
    for x, hits in enumerate(columns):
        if hits > 1 and start is None:
            start = x
        elif hits <= 1 and start is not None:
            if x - start > 12:
                runs.append((start, x))
            start = None
    if start is not None and width - start > 12:
        runs.append((start, width))
    if not runs:
        raise ValueError("整张图找不到角色内容")

    left, right = max(runs, key=lambda r: r[1] - r[0])

    rows = []
    for y in range(height):
        rows.append(sum(1 for x in range(left, right, 2) if solid(x, y)))
    filled = [y for y, hits in enumerate(rows) if hits > 1]
    if not filled:
        raise ValueError("角色列区间内找不到内容")
    return left, min(filled), right, max(filled)


def crop_head(char: Image.Image, index: int) -> Image.Image:
    """按候选编号裁头（比例都是**相对角色自身**，不是相对整图）"""
    _name, top_ratio, bot_ratio, half_ratio = CANDIDATES[index - 1]
    cw, ch = char.size
    y0 = round(ch * top_ratio)
    y1 = round(ch * bot_ratio)
    cx = cw // 2
    half = round(cw * half_ratio)
    return char.crop((max(0, cx - half), y0, min(cw, cx + half), y1))


def fade_bottom(image: Image.Image, ratio: float = 0.07) -> Image.Image:
    """把底边渐变到透明，去掉"从脖子处一刀切"的硬边。

    ## 为什么需要

    裁头的下边界落在**脖子/胸口**，在角色身上是连续的画面，切一刀会留下一条
    水平硬边。16px 下看不出来，但 256px 那个（快捷方式、高 DPI）会很明显，
    看起来像图片被截断了。

    ## 怎么做的

    逐行乘一个 alpha 系数：`ratio` 那一段从 1 线性降到 0。
    ⚠️ 用 `Image.putalpha` 而不是在 RGB 上刷白 —— 图标背景是透明的，
    刷白会出现一圈白边（和"图是白的"那个坑同源）。

    只作用于**已有内容**：本来 alpha=0 的像素乘完还是 0，不会凭空造出半透明块。
    """
    if ratio <= 0:
        return image
    out = image.copy()
    alpha = out.getchannel("A")
    width, height = out.size
    fade_rows = max(1, round(height * ratio))
    start = height - fade_rows
    pixels = alpha.load()
    for y in range(start, height):
        # 1.0 -> 0.0（最后一行完全透明）
        factor = 1.0 - (y - start + 1) / fade_rows
        for x in range(width):
            pixels[x, y] = round(pixels[x, y] * factor)
    out.putalpha(alpha)
    return out


def square(image: Image.Image, size: int, pad_ratio: float = 0.06) -> Image.Image:
    """等比缩放进正方形画布，留一点内边距，避免贴边被裁。"""
    pad = max(1, round(size * pad_ratio))
    limit = size - pad * 2
    scale = min(limit / image.width, limit / image.height)
    resized = image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.LANCZOS,
    )
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(resized, ((size - resized.width) // 2, (size - resized.height) // 2), resized)
    return canvas


def main() -> int:
    parser = argparse.ArgumentParser(description="从整图立绘生成托盘 .ico")
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--candidate", type=int, default=DEFAULT_CANDIDATE, help="用第几个裁法（见对比图）")
    parser.add_argument("--scale", type=float, default=3.0, help="先放大再缩，减轻锯齿")
    args = parser.parse_args()

    if not args.src.exists():
        print(f"找不到素材：{args.src}", file=sys.stderr)
        return 1
    if not 1 <= args.candidate <= len(CANDIDATES):
        print(f"--candidate 要在 1..{len(CANDIDATES)} 之间", file=sys.stderr)
        return 1

    sheet = Image.open(args.src).convert("RGBA")
    bbox = character_bbox(sheet)
    print(f"素材 {args.src.name}  {sheet.size}")
    print(f"角色包围盒 {bbox}  =  {bbox[2] - bbox[0]}x{bbox[3] - bbox[1]}")

    char = sheet.crop(bbox)
    name, *_ = CANDIDATES[args.candidate - 1]
    head = fade_bottom(crop_head(char, args.candidate))
    print(f"裁法 #{args.candidate}「{name}」-> 头部 {head.size}（底边已渐隐）")

    big = max(1, int(args.scale))
    images = []
    for width, height in ICON_SIZES:
        canvas = square(head, max(width, height) * big)
        images.append(canvas.resize((width, height), Image.LANCZOS))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    images[-1].save(
        args.out,
        format="ICO",
        sizes=[(img.width, img.height) for img in images],
        append_images=images[:-1],
    )
    print(f"已写出 {args.out}  {args.out.stat().st_size / 1024:.1f} KB")
    print(f"包含尺寸：{', '.join(f'{w}x{h}' for w, h in ICON_SIZES)}")

    # 预览（托盘图标改完看不到效果，这个文件是唯一的核对手段）
    preview = Image.new(
        "RGBA",
        (sum(s[0] for s in ICON_SIZES) + 10 * len(ICON_SIZES), 256),
        (40, 40, 52, 255),
    )
    x = 5
    for img in images:
        preview.paste(img, (x, (256 - img.height) // 2), img)
        x += img.width + 10
    preview.save(args.out.with_name("tray-preview.png"))
    print(f"预览 -> {args.out.with_name('tray-preview.png').name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
