"""
从角色立绘生成托盘/快捷方式用的 .ico。

为什么需要它：托盘图标是用户唯一能"找到并关掉宠物"的入口，而之前那个图标
是代码画的 16×16 橙色笑脸 —— 跟角色毫无关系，在一堆托盘图标里根本认不出来，
用户会点错到别的程序上（实际发生过）。

这个脚本从 sheet.png 的第一帧（正面 idle）裁出头部，生成多尺寸 .ico：
Windows 会在 16/24/32/48/256 之间挑最合适的，所以要都放进去，
否则小尺寸下会被粗暴缩小、糊成一团。

用法：
    python make-tray-icon.py                      # 用默认素材
    python make-tray-icon.py --src 某图.png --out 输出.ico
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("需要 Pillow：pip install Pillow", file=sys.stderr)
    raise SystemExit(2)

# .ico 里要包含的尺寸。16 是托盘实际显示的大小；48/256 给快捷方式和高 DPI 用。
ICON_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def crop_head(frame: Image.Image, top_ratio: float = 0.26, height_ratio: float = 0.68) -> Image.Image:
    """
    从一帧里裁出**带头部的脸**。

    两个取值都是量出来的，不是拍的：
      · 4 帧高度太小（0.06~0.46）会把眼睛和脸切掉，只剩头饰和头顶 ——
        在 16px 下就是一块看不出是什么的蓝色。脸中心大约在整帧高度的 47%，
        所以范围要覆盖 0.26~0.68。
      · 只留头部而不是整只：托盘图标只有 16px，整只小人缩到 16px 后脸部
        只剩两三个像素，完全认不出是谁。
    """
    bbox = frame.getbbox()
    if bbox is None:
        raise ValueError("这一帧是空的")

    left, top, right, bottom = bbox
    height = bottom - top
    head_top = top + round(height * top_ratio)
    head_bottom = top + round(height * height_ratio)

    # 横向取中间：头部通常居中；留出脸颊余量，避免只截到鼻子
    width = right - left
    center = (left + right) / 2
    half = max(width * 0.40, (head_bottom - head_top) * 0.52)
    head_left = max(left, round(center - half))
    head_right = min(right, round(center + half))

    return frame.crop((head_left, head_top, head_right, head_bottom))


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
    canvas.paste(
        resized,
        ((size - resized.width) // 2, (size - resized.height) // 2),
        resized,
    )
    return canvas


def main() -> int:
    here = Path(__file__).resolve().parent
    default_src = here.parent / "src" / "renderer" / "assets" / "avatar" / "default" / "sheet.png"

    parser = argparse.ArgumentParser(description="从角色立绘生成托盘 .ico")
    parser.add_argument("--src", type=Path, default=default_src, help="序列帧图（默认取 default 素材）")
    parser.add_argument("--out", type=Path, default=here.parent / "assets" / "tray.ico")
    parser.add_argument("--frame", type=int, default=256, help="每帧边长")
    parser.add_argument("--frame-index", type=int, default=0, help="用第几帧（默认 0 = idle 正面）")
    parser.add_argument("--scale", type=float, default=3.0, help="先放大再写 ico，减轻缩小后的锯齿")
    args = parser.parse_args()

    if not args.src.exists():
        print(f"找不到素材：{args.src}", file=sys.stderr)
        return 1

    sheet = Image.open(args.src).convert("RGBA")
    print(f"素材 {args.src.name}  {sheet.size}  帧宽 {args.frame}")

    index = args.frame_index
    frame = sheet.crop(
        (index * args.frame, 0, (index + 1) * args.frame, args.frame)
    )

    head = crop_head(frame)
    print(f"头部裁切：{head.size}（占整帧 {head.width / args.frame:.0%} 宽）")

    # 先生成一张放大的主图，再缩到各尺寸 —— 直接从小图缩会更糊
    big = args.scale
    images = []
    for width, height in ICON_SIZES:
        canvas = square(head, max(width, height) * int(big) if big > 1 else max(width, height))
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

    # 顺手存一张预览，方便核对图标长什么样
    preview = Image.new("RGBA", (sum(s[0] for s in ICON_SIZES) + 10 * len(ICON_SIZES), 256), (40, 40, 52, 255))
    x = 5
    for img in images:
        preview.paste(img, (x, (256 - img.height) // 2), img)
        x += img.width + 10
    preview_path = args.out.with_name("tray-preview.png")
    preview.save(preview_path)
    print(f"预览 -> {preview_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
