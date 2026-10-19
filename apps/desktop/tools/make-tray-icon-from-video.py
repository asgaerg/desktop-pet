"""
从 webm 立绘生成托盘图标。

---- 为什么要专门做一版 ----
 
托盘图标在 Windows 上**只有 16×16**（125% 缩放时是 20×20）。这么小的尺寸下，
把半身像塞进去必然糊成一团 —— 眼睛、发带、衣服细节全糊掉，远看只是一坨蓝。
所以正确做法是**只取脸部**：大色块、对比强，缩到 16px 仍能认出是谁。

---- 与旧脚本的关系 ----
 
`make-tray-icon.py` 是给**序列帧贴图**用的（从 sheet.png 切第 N 帧）。
现在立绘换成了 webm 视频，那份脚本的输入源不存在了，所以另写这一版。
两者裁剪逻辑相同，但本脚本直接从视频取帧。

用法：
    python tools/make-tray-icon-from-video.py [--clip 路径] [--frame-ratio 0.35]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
DEFAULT_FFMPEG = Path(
    r"F:\ref-pet\_internal\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe"
)
DEFAULT_CLIP = (
    HERE.parent / "src" / "renderer" / "assets" / "avatar" / "default" / "idle" / "待机呼吸休闲.webm"
)

# Windows 托盘实际会用到的尺寸。16 是最关键的（任务栏通知区域），
# 24/32 用于高 DPI，更大的尺寸给"拖出来固定"或别的 shell 位置用。
ICON_SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (48, 48), (64, 64)]


def extract_frame(clip: Path, ffmpeg: Path, frame_index: int) -> Image.Image:
    """用 ffmpeg 抽一帧，直接读进 PIL。"""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "frame.png"
        subprocess.run(
            [
                str(ffmpeg), "-hide_banner", "-loglevel", "error",
                "-i", str(clip),
                "-vf", f"select=eq(n\\,{frame_index})",
                "-vframes", "1", "-y", str(out),
            ],
            check=True,
        )
        if not out.exists():
            raise SystemExit(f"抽帧失败：{clip}")
        return Image.open(out).convert("RGBA").copy()


def key_black_background(frame: Image.Image, threshold: int = 25) -> Image.Image:
    """
    抠掉纯黑背景。

    ⚠️ 不做这一步的后果我实测过：图标会变成**一块黑方块**（裁脸把黑底一起裁进来了）。

    算法与竖屏视频播放器 / `import-frames.py` 一致 —— **从画面四边洪泛**，
    只穿过"近黑**且**无色"的像素。不能只按单像素阈值判，否则睫毛、眼线、
    描边这些"角色自己的黑"也会被抠掉（渲染层那边用户报过同样的问题）。
    """
    import numpy as np

    rgb = np.array(frame)
    h, w = rgb.shape[:2]
    r = rgb[..., 0].astype(int)
    g = rgb[..., 1].astype(int)
    b = rgb[..., 2].astype(int)
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    dark = (r <= threshold) & (g <= threshold) & (b <= threshold)
    is_bg = dark & ((mx - mn) <= 12)

    reachable = np.zeros((h, w), dtype=bool)
    stack: list[tuple[int, int]] = []
    for x in range(w):
        for y in (0, h - 1):
            if is_bg[y, x] and not reachable[y, x]:
                reachable[y, x] = True
                stack.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if is_bg[y, x] and not reachable[y, x]:
                reachable[y, x] = True
                stack.append((x, y))
    while stack:
        x, y = stack.pop()
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h and is_bg[ny, nx] and not reachable[ny, nx]:
                reachable[ny, nx] = True
                stack.append((nx, ny))

    alpha = np.where(reachable, 0, 255).astype(np.uint8)
    out = np.dstack([rgb[..., 0], rgb[..., 1], rgb[..., 2], alpha])
    return Image.fromarray(out, "RGBA")


def face_crop(
    image: Image.Image,
    head_box: tuple[float, float, float, float],
    vw: int = 640,
    vh: int = 360,
    pad: float = 0.12,
) -> Image.Image:
    """
    裁出脸部。

    **用素材自带的 head_box，而不是按整帧比例猜。**
    我第一版用 `getbbox()`（整幅人物范围）再按比例取纵向区间，结果只截到发带、
    脸整个没进来 —— 因为 bbox 包含身体，纵向比例算出来偏上。
    head_box 是原作者标定的人头范围（[185,60,436,195]），直接用它最准。

    再向外扩一点（pad），把发带和下巴包进来；最后以画面水平中心对齐，
    保证脸居中（16px 下偏一点就很难认）。
    """
    x0, y0, x1, y1 = head_box
    w = x1 - x0
    h = y1 - y0
    px = w * pad
    py = h * pad
    left = max(0, x0 - px)
    right = min(vw, x1 + px)
    top = max(0, y0 - py)
    bottom = min(vh, y1 + py)
    return image.crop((round(left), round(top), round(right), round(bottom)))


def to_square(image: Image.Image, pad_ratio: float = 0.04) -> Image.Image:
    """补成正方形并留一点边距 —— 非正方形缩放进 ico 会被压扁。"""
    size = max(image.width, image.height)
    pad = round(size * pad_ratio)
    canvas = Image.new("RGBA", (size + pad * 2, size + pad * 2), (0, 0, 0, 0))
    canvas.paste(image, ((canvas.width - image.width) // 2, (canvas.height - image.height) // 2), image)
    return canvas


def main() -> int:
    parser = argparse.ArgumentParser(description="从 webm 立绘生成托盘图标")
    parser.add_argument("--clip", type=Path, default=DEFAULT_CLIP, help="webm 片源")
    parser.add_argument("--ffmpeg", type=Path, default=DEFAULT_FFMPEG)
    parser.add_argument("--frame-index", type=int, default=8, help="用第几帧（默认 8，通常正脸睁眼）")
    parser.add_argument(
        "--head-box",
        default="245,60,395,210",
        help=(
            '从源帧里裁出的区域 "x0,y0,x1,y1"。默认值是对着实际画面调出来的：'
            "源视频 640x360，人物在 x213-427、头在 y60-195；"
            "取 150x150 的**正方形**区域把脸（刘海+眼睛+腮）填满。"
            "**接近正方形很重要** —— 裁出来太宽时补方形会在上下留大片空白，"
            "脸被压小，16px 下就认不出了（试过 208x93 那版，确实糊）。"
        ),
    )
    parser.add_argument("--pad", type=float, default=0.0, help="在裁剪区外扩的比例")
    parser.add_argument("--out", type=Path, default=HERE.parent / "assets" / "tray.ico")
    parser.add_argument("--scale", type=float, default=4.0, help="先放大再写 ico，减轻锯齿")
    args = parser.parse_args()

    if not args.clip.exists():
        raise SystemExit(f"片源不存在：{args.clip}")
    if not args.ffmpeg.exists():
        raise SystemExit(f"ffmpeg 不存在：{args.ffmpeg}")

    frame = extract_frame(args.clip, args.ffmpeg, args.frame_index)
    frame = key_black_background(frame)
    print(f"抽帧 {args.clip.name} 第 {args.frame_index} 帧 -> {frame.size}（已抠黑底）")

    head_box = tuple(float(v) for v in args.head_box.split(","))
    if len(head_box) != 4:
        raise SystemExit("--head-box 需要四个数字")
    face = face_crop(frame, head_box, frame.width, frame.height, args.pad)
    square = to_square(face)
    print(f"裁脸 {face.size} -> 方形 {square.size}")

    # 先放大再缩小：多一次重采样能明显减轻 16px 下的锯齿
    big = square.resize(
        (round(square.width * args.scale), round(square.height * args.scale)), Image.LANCZOS
    )

    images: list[Image.Image] = []
    for width, height in ICON_SIZES:
        # 每一档都从放大版单独缩，避免链式缩放累积误差
        images.append(big.resize((width, height), Image.LANCZOS))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    images[-1].save(
        args.out,
        format="ICO",
        sizes=[(img.width, img.height) for img in images],
        append_images=images[:-1],
    )
    print(f"-> {args.out}  {args.out.stat().st_size / 1024:.1f} KB")
    print(f"   包含尺寸：{', '.join(f'{w}x{h}' for w, h in ICON_SIZES)}")

    # 预览：把各档按实际大小并排画出来，方便判断 16px 下认不认得出
    preview = Image.new(
        "RGBA", (sum(s[0] for s in ICON_SIZES) + 12 * len(ICON_SIZES) + 12, 72), (46, 48, 58, 255)
    )
    x = 12
    for img in images:
        preview.paste(img, (x, (72 - img.height) // 2), img)
        x += img.width + 12
    preview_path = args.out.with_name("tray-preview.png")
    preview.save(preview_path)
    print(f"-> {preview_path}（按实际像素大小并排，看 16px 下能不能认出来）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
