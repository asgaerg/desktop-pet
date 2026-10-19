#!/usr/bin/env python
"""
从"角色三视图/立绘"生成序列帧立绘。

适用素材：**纯色或浅灰底**的单张人物立绘（常见于角色设定图、三视图）。
它做三件事：
  1. 洪泛抠底（从四周连通区域，不用颜色阈值硬切，避免挖空人物内部的浅色部分）；
  2. 自动从多视图里**切出正视图**（找横向连通块，取最靠左且够大的那个）；
  3. 用几何变换 + 局部绘制生成整套动画。

生成哪些动画：
  idle    呼吸（极轻微的纵向起伏 + 缩放，幅度按帧高比例算）
  blink   真·闭眼（在测出的眼睛位置画眼睑）
  talk    嘴部开合（在测出的嘴位置画椭圆）
  drag    整体倾斜
  sleep   下沉 + 闭眼 + 更浅的呼吸
  happy   上浮 + 左右轻摆
  curious 歪头

**关键**：闭眼与说话需要知道眼睛/嘴的位置。本脚本支持两种方式：
  · `--eyes "x1,y1,x2,y2"` / `--mouth "x,y,w"` 显式给定（**推荐**：先用
    `--ruler` 输出标尺图，自己读一遍坐标，最可靠）；
  · 不给定则尝试自动检测（按肤色找脸 -> 脸内找亮蓝虹膜），**可能不准**，
    因为卡通角色的虹膜与头发常是同色系。自动检测失败时只会跳过闭眼绘制，
    不会弄坏素材。

用法：
    # 先出标尺图，读出眼睛/嘴的坐标
    python make-sprite-sheet.py 立绘.png -o out --ruler
    # 再按读出的坐标生成
    python make-sprite-sheet.py 立绘.png -o out --eyes "187,287,285,285" --mouth "252,322,20"
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import deque
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

FRAME = 256
MARGIN = 8


# --------------------------------------------------------------------------- #
# 抠底
# --------------------------------------------------------------------------- #


def flood_background(image: Image.Image, tolerance: int = 30) -> Image.Image:
    """
    从四边洪泛填充，抠掉背景。

    **判据是"低饱和 + 高亮度"而不是"接近某个基准色"** —— 这一点是踩出来的：
    很多立绘背景不是纯色，而是从上到下 225→255 的灰白渐变（三视图尤其常见）。
    用单一基准色加容差去比，总有一端落在容差外，结果是抠不干净、
    三个视图连成一片。改成看饱和度就同时覆盖了 225 和 255：
    它们都是"没有颜色倾向的灰白"，而人物线稿和上色都有明显饱和度。

    `tolerance` 现在表示允许的饱和度上限（越大越激进）。
    """
    rgba = image.convert("RGBA")
    width, height = rgba.size
    pixels = rgba.load()

    sat_limit = max(6, tolerance)

    def is_background(r: int, g: int, b: int) -> bool:
        high = max(r, g, b)
        low = min(r, g, b)
        return (high - low) <= sat_limit and high >= 190

    visited = bytearray(width * height)
    queue: deque[tuple[int, int]] = deque()
    for x in range(width):
        for y in (0, height - 1):
            if not visited[y * width + x]:
                visited[y * width + x] = 1
                queue.append((x, y))
    for y in range(height):
        for x in (0, width - 1):
            if not visited[y * width + x]:
                visited[y * width + x] = 1
                queue.append((x, y))

    while queue:
        x, y = queue.popleft()
        r, g, b, alpha = pixels[x, y]
        if alpha == 0:
            continue
        if not is_background(r, g, b):
            continue
        pixels[x, y] = (0, 0, 0, 0)
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < width and 0 <= ny < height and not visited[ny * width + nx]:
                visited[ny * width + nx] = 1
                queue.append((nx, ny))

    r, g, b, a = rgba.split()
    return Image.merge("RGBA", (r, g, b, a.filter(ImageFilter.GaussianBlur(0.6))))


def split_views(image: Image.Image, min_width: int = 60) -> list[tuple[int, int, int, int]]:
    """找出图里横向排开的几个角色（三视图那种）。返回各自的 bbox。"""
    alpha = image.getchannel("A")
    width, height = image.size
    opaque = alpha.load()
    columns = [sum(1 for y in range(height) if opaque[x, y] > 40) for x in range(width)]

    spans: list[tuple[int, int]] = []
    start = None
    for x in range(width):
        if columns[x] > 2:
            if start is None:
                start = x
        else:
            if start is not None and x - start >= min_width:
                spans.append((start, x - 1))
            start = None
    if start is not None and width - start >= min_width:
        spans.append((start, width - 1))

    boxes = []
    for x0, x1 in spans:
        rows = [
            y
            for y in range(height)
            if any(opaque[x, y] > 40 for x in range(x0, x1 + 1, max(1, (x1 - x0) // 30)))
        ]
        if rows:
            boxes.append((x0, min(rows), x1, max(rows)))
    return boxes


# --------------------------------------------------------------------------- #
# 归一化
# --------------------------------------------------------------------------- #


def normalize(character: Image.Image, frame: int = FRAME) -> tuple[Image.Image, float, int, int]:
    """
    裁掉透明边，等比缩放进帧内，底部对齐。

    返回 (帧, 缩放比, 横向偏移, 纵向偏移) —— 后面画眼睛/嘴要按同样的
    变换换算坐标，否则闭眼会画歪。
    """
    bbox = character.getbbox()
    if bbox is None:
        raise ValueError("抠完之后是空的")
    trimmed = character.crop(bbox)
    limit = frame - MARGIN * 2
    scale = min(limit / trimmed.width, limit / trimmed.height)
    size = (max(1, round(trimmed.width * scale)), max(1, round(trimmed.height * scale)))
    resized = trimmed.resize(size, Image.LANCZOS)
    dx = (frame - size[0]) // 2
    dy = frame - MARGIN - size[1]
    canvas = Image.new("RGBA", (frame, frame), (0, 0, 0, 0))
    canvas.paste(resized, (dx, dy), resized)
    return canvas, scale, dx - bbox[0] * scale, dy - bbox[1] * scale


# --------------------------------------------------------------------------- #
# 局部绘制
# --------------------------------------------------------------------------- #

EYE_COLOR = (58, 42, 44, 255)
MOUTH_COLOR = (150, 78, 92, 255)


def draw_closed_eye(frame: Image.Image, cx: int, cy: int, width: int, height: int) -> None:
    """
    画一只闭着的眼。

    三条经验都是在这张素材上反复调出来的：
      · 用**向下弯的弧**（开口朝上）；直线看起来像眉毛。
      · 线要**细**。粗弧在 256px 的帧上会变成两块黑斑，很脏。
        取眼睛高度的 ~13%，实测 3~4px 最像闭眼。
      · 弧的**宽度贴着眼球**，不能按眼距推算 —— 卡通角色眼距通常远大于
        眼球本身（这只角色眼距 ~52px，眼球只有 ~24px）。早先按 0.62 倍算，
        画出来比眼睛宽一半，横在脸上很怪。
      · 弧盒高度只取眼睛高度的 ~1/3。太高就变成一个圆圈，
        看起来像戴眼镜而不是闭眼。
    """
    half = max(3, width // 2)
    thickness = max(2, round(height * 0.13))
    box_h = max(3, round(height * 0.34))
    draw = ImageDraw.Draw(frame)
    draw.arc(
        [cx - half, cy - box_h // 2, cx + half, cy + box_h],
        start=195,
        end=345,
        fill=EYE_COLOR,
        width=thickness,
    )


def draw_mouth_open(frame: Image.Image, cx: int, cy: int, width: int, openness: float) -> None:
    """说话时的嘴：高度随 openness 变化的小椭圆，位置就落在嘴中心。"""
    half_w = max(2, width // 2)
    half_h = max(1, round(width * 0.5 * openness))
    if half_h < 1:
        return
    draw = ImageDraw.Draw(frame)
    draw.ellipse([cx - half_w, cy - half_h, cx + half_w, cy + half_h], fill=MOUTH_COLOR)


def transformed(base: Image.Image, *, scale: float = 1.0, tilt: float = 0.0, dy: int = 0, dx: int = 0) -> Image.Image:
    """缩放/旋转/平移；缩放以**底边中心**为锚点，脚不动、头顶起伏。"""
    frame = base.size[0]
    image = base
    if abs(scale - 1.0) > 1e-4:
        size = (max(1, round(frame * scale)), max(1, round(frame * scale)))
        resized = image.resize(size, Image.LANCZOS)
        canvas = Image.new("RGBA", (frame, frame), (0, 0, 0, 0))
        canvas.paste(resized, ((frame - size[0]) // 2, frame - size[1]), resized)
        image = canvas
    if abs(tilt) > 0.01:
        image = image.rotate(tilt, resample=Image.BICUBIC, center=(frame // 2, frame))
    if dx or dy:
        canvas = Image.new("RGBA", (frame, frame), (0, 0, 0, 0))
        canvas.paste(image, (dx, dy), image)
        image = canvas
    return image


def parse_pair(spec: str) -> list[tuple[int, int]]:
    numbers = [int(n) for n in re.findall(r"-?\d+", spec)]
    if len(numbers) % 2:
        raise SystemExit(f"--eyes 需要成对的坐标，收到：{spec!r}")
    return [(numbers[i], numbers[i + 1]) for i in range(0, len(numbers), 2)]


def parse_triple(spec: str) -> tuple[int, int, int]:
    numbers = [int(n) for n in re.findall(r"-?\d+", spec)]
    if len(numbers) < 2:
        raise SystemExit(f"--mouth 至少要有 x,y，收到：{spec!r}")
    return (numbers[0], numbers[1], numbers[2] if len(numbers) > 2 else 18)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description="从单张立绘生成序列帧素材")
    parser.add_argument("source", type=Path)
    parser.add_argument("-o", "--out", type=Path, required=True)
    parser.add_argument("--frame", type=int, default=FRAME)
    parser.add_argument("--tolerance", type=int, default=30)
    parser.add_argument("--view", type=int, default=0, help="用第几个视图（默认 0=最左）")
    parser.add_argument("--eyes", default="", help='眼睛中心，源图坐标："x1,y1,x2,y2"')
    parser.add_argument("--mouth", default="", help='嘴部 "x,y,w"，源图坐标')
    parser.add_argument("--image-name", default="sheet.png")
    parser.add_argument("--name", default="avatar")
    parser.add_argument("--optimize", action="store_true")
    parser.add_argument("--ruler", action="store_true", help="只输出带坐标标尺的图，方便读坐标")
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()

    if not args.source.exists():
        print(f"找不到：{args.source}", file=sys.stderr)
        return 1

    raw = Image.open(args.source)
    print(f"输入 {args.source}  {raw.size}  {raw.mode}")

    if args.ruler:
        cut = flood_background(raw, args.tolerance)
        boxes = split_views(cut)
        print(f"检测到 {len(boxes)} 个视图：{boxes}")
        target = boxes[min(args.view, len(boxes) - 1)] if boxes else (0, 0, raw.width - 1, raw.height - 1)
        view = cut.crop(target)
        scale = 2
        big = view.resize((view.width * scale, view.height * scale), Image.LANCZOS).convert("RGB")
        draw = ImageDraw.Draw(big)
        for sx in range(0, view.width, 20):
            x = sx * scale
            major = sx % 100 == 0
            draw.line([(x, 0), (x, big.height)], fill=(255, 0, 0) if major else (255, 180, 180), width=2 if major else 1)
            if major:
                draw.text((x + 3, 3), str(sx), fill=(255, 0, 0))
        for sy in range(0, view.height, 20):
            y = sy * scale
            major = sy % 100 == 0
            draw.line([(0, y), (big.width, y)], fill=(0, 110, 255) if major else (180, 205, 255), width=2 if major else 1)
            if major:
                draw.text((3, y + 3), str(sy), fill=(0, 110, 255))
        args.out.mkdir(parents=True, exist_ok=True)
        out = args.out / "ruler.png"
        big.save(out)
        print(f"标尺图 -> {out}")
        print(f"视图尺寸 {view.width}x{view.height}（标尺坐标除以 {scale} 就是源图坐标）")
        return 0

    cut = flood_background(raw, args.tolerance)
    boxes = split_views(cut)
    if len(boxes) > 1:
        print(f"检测到 {len(boxes)} 个视图，使用第 {args.view} 个：{boxes[min(args.view, len(boxes) - 1)]}")
    target = boxes[min(args.view, len(boxes) - 1)] if boxes else (0, 0, raw.width - 1, raw.height - 1)
    view_raw = cut.crop(target)

    frame_img, scale, off_x, off_y = normalize(view_raw, args.frame)
    print(f"归一化：缩放 {scale:.3f}，偏移 ({off_x:.0f},{off_y:.0f})")

    # 源图坐标 -> 帧内坐标
    def to_frame(x: float, y: float) -> tuple[int, int]:
        return round(x * scale + off_x), round(y * scale + off_y)

    eyes: list[tuple[int, int, int, int]] = []
    if args.eyes:
        numbers = [int(n) for n in re.findall(r"-?\d+", args.eyes)]
        # 支持两种写法：每只眼 2 个或 4 个数字（4 个时最后一个当高度）
        per_eye = 2 if len(numbers) % 4 else 4
        if len(numbers) % per_eye:
            raise SystemExit(f"--eyes 的数字个数不对：{args.eyes!r}")
        for i in range(0, len(numbers), per_eye):
            if per_eye == 4:
                x, y, w, h = numbers[i : i + 4]
            else:
                x, y = numbers[i : i + 2]
                w = h = 0
            eyes.append((x, y, w, h))
        # 没给宽高的：按眼距的 0.5 估（实测这只角色眼距 42、眼球 22，约 0.52）
        if len(eyes) == 2 and eyes[0][2] == 0:
            span = abs(eyes[1][0] - eyes[0][0])
            w = max(6, round(span * 0.52))
            h = round(w * 1.18)
            eyes = [(eyes[0][0], eyes[0][1], w, h), (eyes[1][0], eyes[1][1], w, h)]

    mouth = parse_triple(args.mouth) if args.mouth else None
    if mouth is None and eyes:
        # 没给嘴坐标时，由眼睛位置推算：嘴在两眼连线中点**下方**。
        # 距离取眼睛高度的 1.1 倍 —— 卡通脸五官集中，这个比例比"脸的固定百分比"
        # 稳：它自动适配不同头身比，也不依赖脸在哪。
        mid_x = (eyes[0][0] + eyes[1][0]) // 2
        eye_y = (eyes[0][1] + eyes[1][1]) // 2
        eye_h = eyes[0][3] or 60
        mouth = (mid_x, eye_y + round(eye_h * 1.1), max(10, round(eye_h * 0.22)))
        print(f"未给 --mouth，按眼睛位置推算：{mouth}")

    if not eyes:
        print("未提供 --eyes，跳过闭眼绘制（先用 --ruler 读坐标）", file=sys.stderr)
    frames: list[Image.Image] = []

    # 每秒的闭眼绘制（帧内坐标）
    def frame_eyes(target: Image.Image) -> None:
        for x, y, w, h in eyes:
            fx, fy = to_frame(x, y)
            draw_closed_eye(target, fx, fy, max(6, round(w * scale)), max(6, round(h * scale)))

    # idle：呼吸。幅度按帧高比例，太小看不见、太大会像在跳
    amp = args.frame * 0.008
    for i in range(4):
        phase = math.sin(i / 4 * 2 * math.pi)
        frames.append(transformed(frame_img, scale=1.0 + phase * 0.01, dy=-round(phase * amp)))

    # blink：睁眼 / 闭眼
    frames.append(frame_img.copy())
    closed = frame_img.copy()
    frame_eyes(closed)
    frames.append(closed)

    # talk：嘴部开合
    for openness in (0.3, 0.9, 0.5):
        frame = frame_img.copy()
        if mouth:
            mx, my, mw = mouth
            fx, fy = to_frame(mx, my)
            draw_mouth_open(frame, fx, fy, max(4, round(mw * scale)), openness)
        frames.append(frame)

    # drag：倾斜
    frames.append(transformed(frame_img, tilt=7, dx=3))

    # sleep：下沉 + 闭眼
    for i in range(2):
        frame = transformed(frame_img, scale=1.0 + (0.006 if i else 0.0), dy=4 + i)
        frame_eyes(frame)
        frames.append(frame)

    # happy：上浮 + 摆动
    for i in range(2):
        frames.append(transformed(frame_img, dy=-5, tilt=-4 if i else 4))
    # curious：歪头
    for i in range(2):
        frames.append(transformed(frame_img, tilt=10 if i else 8, dx=-2))

    total = len(frames)
    sheet = Image.new("RGBA", (args.frame * total, args.frame), (0, 0, 0, 0))
    for index, frame in enumerate(frames):
        sheet.paste(frame, (index * args.frame, 0), frame)

    args.out.mkdir(parents=True, exist_ok=True)
    sheet_path = args.out / args.image_name
    if args.optimize:
        sheet.quantize(colors=256, method=Image.FASTOCTREE).save(sheet_path, optimize=True)
    else:
        sheet.save(sheet_path)
    print(f"已写出 {sheet_path}  {sheet.size}  {sheet_path.stat().st_size / 1024:.0f} KB（{total} 帧）")

    animations = {
        "idle": {"frames": [0, 1, 2, 3], "fps": 8, "loop": True},
        "blink": {"frames": [4, 5], "fps": 9, "loop": False},
        "talk": {"frames": [6, 7, 8], "fps": 12, "loop": True},
        "drag": {"frames": [9], "fps": 8, "loop": True},
        "sleep": {"frames": [10, 11], "fps": 2.2, "loop": True},
        "happy": {"frames": [12, 13], "fps": 8, "loop": True},
        "curious": {"frames": [14, 15], "fps": 8, "loop": True},
    }
    manifest = {
        "$comment": "由 make-sprite-sheet.py 从单张立绘生成。",
        "image": args.image_name,
        "frameWidth": args.frame,
        "frameHeight": args.frame,
        "fps": 8,
        "palette": {"eye": "#3a2a2c", "mouth": "#964e5c"},
        "animations": animations,
    }
    manifest_path = args.out / f"{args.name}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写出 {manifest_path}")

    if args.preview:
        cell = min(140, args.frame)
        preview = Image.new("RGBA", (cell * total, cell), (26, 26, 34, 255))
        for index, frame in enumerate(frames):
            thumb = frame.copy()
            thumb.thumbnail((cell, cell), Image.LANCZOS)
            preview.paste(thumb, (index * cell, (cell - thumb.height) // 2), thumb)
        preview_path = args.out / f"{args.name}-preview.png"
        preview.save(preview_path)
        print(f"预览 -> {preview_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
