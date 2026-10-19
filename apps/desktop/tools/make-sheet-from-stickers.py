"""
从表情包（多格贴纸图）生成序列帧立绘。

与 make-sprite-sheet.py 的区别：那个是"一张立绘 -> 几何变换"，这个是
"多格贴纸 -> 每格本身就是不同姿势"，所以**姿势来自素材**，不需要凭空生成。

用法：
    python make-sheet-from-stickers.py 贴纸图.png -o 输出目录
        --cols 5 --rows 4
        --map "idle:0,1,2,1;blink:4;talk:2;sleep:7;happy:4,14"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import deque
from pathlib import Path

from PIL import Image

FRAME = 256


# --------------------------------------------------------------------------- #
# 抠底：洪泛填充（只处理与边缘连通的背景，避免挖空人物内部的同色区域）
# --------------------------------------------------------------------------- #


def remove_background(image: Image.Image, tolerance: int = 20) -> Image.Image:
    rgba = image.convert("RGBA")
    width, height = rgba.size
    pixels = rgba.load()

    corners = [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]
    samples = [pixels[x, y][:3] for x, y in corners]
    base = tuple(sum(s[i] for s in samples) // len(samples) for i in range(3))

    visited = bytearray(width * height)
    queue: deque[tuple[int, int]] = deque()
    for x, y in corners:
        if not visited[y * width + x]:
            visited[y * width + x] = 1
            queue.append((x, y))

    while queue:
        x, y = queue.popleft()
        r, g, b, a = pixels[x, y]
        if a == 0:
            continue
        if abs(r - base[0]) > tolerance or abs(g - base[1]) > tolerance or abs(b - base[2]) > tolerance:
            continue
        pixels[x, y] = (0, 0, 0, 0)
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < width and 0 <= ny < height and not visited[ny * width + nx]:
                visited[ny * width + nx] = 1
                queue.append((nx, ny))
    return rgba


def soften_alpha(image: Image.Image, radius: float = 0.6) -> Image.Image:
    from PIL import ImageFilter

    r, g, b, a = image.split()
    return Image.merge("RGBA", (r, g, b, a.filter(ImageFilter.GaussianBlur(radius))))


# --------------------------------------------------------------------------- #
# 归一化：每格姿势大小位置都不同，必须统一，否则动画会乱跳
# --------------------------------------------------------------------------- #


def normalize(cell: Image.Image, frame: int = FRAME, margin: int = 6) -> Image.Image:
    """
    按 alpha 裁掉透明边，等比缩放到帧内并底部对齐。

    底部对齐是关键：这样不同姿势的"脚"在同一水平线上，
    切换姿势时不会整只上下跳。居中对齐会让人物看起来在飘。
    """
    bbox = cell.getbbox()
    if bbox is None:
        raise ValueError("这一格抠完是空的")

    trimmed = cell.crop(bbox)
    limit = frame - margin * 2
    scale = min(limit / trimmed.width, limit / trimmed.height)
    size = (max(1, round(trimmed.width * scale)), max(1, round(trimmed.height * scale)))
    resized = trimmed.resize(size, Image.LANCZOS)

    canvas = Image.new("RGBA", (frame, frame), (0, 0, 0, 0))
    canvas.paste(resized, ((frame - size[0]) // 2, frame - margin - size[1]), resized)
    return canvas


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def parse_map(spec: str, cell_count: int) -> dict[str, list[int]]:
    """
    解析 `--map "idle:0,1,2;talk:3"`。

    给的是**格号**（从 0 开始，按行优先）。没给的动画会用默认回退。
    """
    result: dict[str, list[int]] = {}
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise SystemExit(f"--map 里的这一段缺少冒号：{part!r}")
        name, indices = part.split(":", 1)
        numbers = [int(n) for n in re.findall(r"\d+", indices)]
        if not numbers:
            raise SystemExit(f"--map 里 {name} 没有格号")
        for n in numbers:
            if not 0 <= n < cell_count:
                raise SystemExit(f"--map 里 {name} 的格号 {n} 超出范围（共 {cell_count} 格）")
        result[name.strip()] = numbers
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="从多格贴纸图生成序列帧立绘")
    parser.add_argument("source", type=Path)
    parser.add_argument("-o", "--out", type=Path, required=True)
    parser.add_argument("--cols", type=int, default=5)
    parser.add_argument("--rows", type=int, default=4)
    parser.add_argument("--frame", type=int, default=FRAME)
    parser.add_argument("--tolerance", type=int, default=20)
    parser.add_argument("--name", default="avatar")
    parser.add_argument(
        "--image-name",
        default="",
        help="sheet.png 里 image 字段的文件名。留空则用 <name>-sheet.png",
    )
    parser.add_argument(
        "--optimize",
        action="store_true",
        help="用调色板量化压缩 PNG（体积可小一个数量级，256 色对这类插画足够）",
    )
    parser.add_argument(
        "--map",
        default="",
        help='动画到格号的映射，例如 "idle:0,1,2,1;blink:5;talk:2"',
    )
    parser.add_argument(
        "--preview", action="store_true", help="另外输出一张带序号的预览图，便于人工核对"
    )
    args = parser.parse_args()

    if not args.source.exists():
        print(f"找不到输入图：{args.source}", file=sys.stderr)
        return 1

    sheet = Image.open(args.source)
    width, height = sheet.size
    cell_w, cell_h = width // args.cols, height // args.rows
    print(f"输入 {args.source} {sheet.size} -> {args.cols}x{args.rows} 格，每格 {cell_w}x{cell_h}")

    cells: list[Image.Image] = []
    for row in range(args.rows):
        for col in range(args.cols):
            raw = sheet.crop(
                (col * cell_w, row * cell_h, (col + 1) * cell_w, (row + 1) * cell_h)
            )
            cut = soften_alpha(remove_background(raw, args.tolerance))
            try:
                cells.append(normalize(cut, args.frame))
            except ValueError as error:
                print(f"  第 {row}{col} 格：{error}", file=sys.stderr)
                return 2

    print(f"已归一化 {len(cells)} 格")

    mapping = parse_map(args.map, len(cells)) if args.map else {}
    if not mapping:
        print(
            "未提供 --map，将按顺序自动分配：\n"
            "  idle=前 4 格  blink=第 5 格  talk=第 6 格  sleep=第 7 格  happy=第 8 格  curious=第 9 格",
            file=sys.stderr,
        )
        order = list(range(len(cells)))
        mapping = {
            "idle": order[0:4] or [0],
            "blink": order[4:5] or order[0:1],
            "talk": order[5:6] or order[0:1],
            "drag": order[8:9] or order[0:1],
            "sleep": order[6:7] or order[0:1],
            "happy": order[7:8] or order[0:1],
            "curious": order[9:10] or order[0:1],
        }

    # 按动画顺序把所有用到的格号排成一张单行图，并算出每个动画的帧下标
    used: list[int] = []
    animations: dict[str, dict] = {}
    for name, indices in mapping.items():
        frames = []
        for cell_index in indices:
            if cell_index not in used:
                used.append(cell_index)
            frames.append(used.index(cell_index))
        animations[name] = {"frames": frames, "fps": 8, "loop": name not in {"blink"}}
    animations.setdefault("idle", {"frames": [0], "fps": 8, "loop": True})
    # talk 通常要快一点
    if "talk" in animations:
        animations["talk"]["fps"] = 12

    total = len(used)
    out_sheet = Image.new("RGBA", (args.frame * total, args.frame), (0, 0, 0, 0))
    for slot, cell_index in enumerate(used):
        out_sheet.paste(cells[cell_index], (slot * args.frame, 0), cells[cell_index])

    args.out.mkdir(parents=True, exist_ok=True)
    image_name = args.image_name or f"{args.name}-sheet.png"
    sheet_path = args.out / image_name

    to_save = out_sheet
    if args.optimize:
        # 256 色对这类扁平插画几乎无损，但 PNG 能小一个数量级。
        # 透明要保留，所以用 FASTOCTREE 且带 alpha。
        to_save = out_sheet.quantize(colors=256, method=Image.FASTOCTREE)
        to_save.save(sheet_path, optimize=True)
    else:
        to_save.save(sheet_path)
    size_kb = sheet_path.stat().st_size / 1024
    print(f"已写出 {sheet_path}  {out_sheet.size}   {size_kb:.0f} KB（用了 {total} 格）")

    manifest = {
        "$comment": "由 make-sheet-from-stickers.py 从表情包生成。",
        "image": image_name,
        "frameWidth": args.frame,
        "frameHeight": args.frame,
        "fps": 8,
        "palette": {},
        "animations": animations,
    }
    manifest_path = args.out / f"{args.name}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写出 {manifest_path}")
    for name, spec in animations.items():
        print(f"  {name:<8} 帧 {spec['frames']}  fps {spec['fps']}  loop={spec['loop']}")

    if args.preview:
        scale = max(1, 120 // args.frame) if args.frame < 120 else 1
        thumb_w = min(120, args.frame)
        preview = Image.new(
            "RGBA", (thumb_w * total, thumb_w), (24, 24, 32, 255)
        )
        for slot, cell_index in enumerate(used):
            thumb = cells[cell_index].copy()
            thumb.thumbnail((thumb_w, thumb_w), Image.LANCZOS)
            preview.paste(thumb, (slot * thumb_w, 0), thumb)
        preview_path = args.out / f"{args.name}-preview.png"
        preview.save(preview_path)
        print(f"预览 {preview_path}（共 {total} 格，按顺序排列）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
