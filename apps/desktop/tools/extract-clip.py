"""从既有 webm 里剪出一段，并导出成带 alpha 的 webm。

用途：素材只有大段循环动画（比如 `move/左转奔跑` 是"站立→趴下→爬→跑"），
但某个功能只要其中一小段（比如"趴着探头"只要侧躺那几帧）。
这个脚本负责：抽帧 → 正确抠黑底 → 用 VP9 带 alpha 重新编码。

---- 为什么不用 ffmpeg 的 colorkey ----
 
`colorkey=0x000000` 是**单像素阈值**：只要是黑的就抠掉，
于是角色的睫毛、眼线、描边也会被抠成透明（渲染层那边用户报过同样的问题）。
所以改用与渲染层一致的**连通性抠图**：从画面四边洪泛，只穿过"近黑且无色"的
像素；被描边围住的暗部（睫毛）保留下来。

---- 为什么 VP9 的 alpha 可行 ----
 
`libvpx-vp9` + `-pix_fmt yuva420p` 能编出带 alpha 的 webm，
Chromium 能正确解码（实测角落 alpha=0、角色区域 alpha=255）。
注意 `ffmpeg -i` 显示的仍是 `yuv420p`（alpha 存在独立的 BlockAdditional 里），
**不能用它判断有没有 alpha** —— 要在浏览器里读像素才算数。

用法：
    python tools/extract-clip.py <源.webm> <输出.webm> --start 5.0 --end 5.83
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

DEFAULT_FFMPEG = Path(
    r"F:\ref-pet\_internal\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe"
)


def key_black_background(image: Image.Image, threshold: int = 25) -> Image.Image:
    """
    抠掉纯黑背景（连通性法）。

    与渲染层的 `videoPlayer.ts` 和 `analyse-pose.py` 保持同一套判据，
    否则"分析时看着对、播放时不一样"。
    """
    import numpy as np

    rgb = np.array(image)
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


def main() -> int:
    parser = argparse.ArgumentParser(description="剪出一段并导出成带 alpha 的 webm")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--start", type=float, required=True, help="起始秒")
    parser.add_argument("--end", type=float, required=True, help="结束秒")
    parser.add_argument("--ffmpeg", type=Path, default=DEFAULT_FFMPEG)
    parser.add_argument("--crf", type=int, default=30, help="VP9 质量（越小越清晰，默认 30）")
    args = parser.parse_args()

    for label, path in (("源", args.source), ("ffmpeg", args.ffmpeg)):
        if not path.exists():
            raise SystemExit(f"{label}不存在：{path}")
    if args.end <= args.start:
        raise SystemExit("--end 必须大于 --start")

    args.output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        # 1) 抽帧
        subprocess.run(
            [
                str(args.ffmpeg), "-hide_banner", "-loglevel", "error",
                "-ss", str(args.start), "-to", str(args.end),
                "-i", str(args.source),
                "-y", str(tmp_dir / "src_%04d.png"),
            ],
            check=True,
        )
        frames = sorted(tmp_dir.glob("src_*.png"))
        if not frames:
            raise SystemExit("没有抽到帧，检查 --start/--end")
        print(f"抽出 {len(frames)} 帧（{args.start}s ~ {args.end}s）")

        # 2) 抠黑底
        keyed_dir = tmp_dir / "keyed"
        keyed_dir.mkdir()
        for i, path in enumerate(frames, start=1):
            image = Image.open(path).convert("RGBA")
            key_black_background(image).save(keyed_dir / f"k{i:04d}.png")
        print(f"已抠黑底（连通性法，保留睫毛/描边）")

        # 3) 编码成带 alpha 的 webm
        subprocess.run(
            [
                str(args.ffmpeg), "-hide_banner", "-loglevel", "error",
                "-framerate", "24",
                "-i", str(keyed_dir / "k%04d.png"),
                "-c:v", "libvpx-vp9",
                "-pix_fmt", "yuva420p",
                "-auto-alt-ref", "0",   # alpha 必须关掉这个
                "-crf", str(args.crf),
                "-b:v", "0",
                "-y", str(args.output),
            ],
            check=True,
        )

    size_kb = args.output.stat().st_size / 1024
    print(f"-> {args.output}  {size_kb:.1f} KB")
    print("   注意：ffmpeg -i 仍会显示 yuv420p —— alpha 在独立的 BlockAdditional 里，")
    print("   要确认得在浏览器/Chromium 里读像素（角落 alpha 应为 0）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
