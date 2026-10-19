"""分析 webm 里"角色姿势"的逐帧变化：找出侧躺/趴着的那一段。

思路：侧躺时角色的**非透明包围盒又宽又扁**（宽高比大、高度小），
站立时接近正方形。所以逐帧算包围盒的宽高比，就能自动定位姿势区间，
不用靠肉眼看缩略图猜帧号。

用法：
    python tools/analyse-pose.py <片源.webm> [--step 4] [--ffmpeg 路径]
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


def probe_fps(clip: Path, ffmpeg: Path) -> float:
    out = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-i", str(clip)],
        capture_output=True, text=True, errors="replace",
    ).stderr
    for line in out.splitlines():
        if " fps," in line:
            for token in line.split(","):
                token = token.strip()
                if token.endswith(" fps"):
                    try:
                        return float(token.split()[0])
                    except ValueError:
                        pass
    return 24.0


def extract_frames(clip: Path, ffmpeg: Path, fps: float, step: int, out_dir: Path) -> list[Path]:
    """按 step 抽帧（step=1 表示逐帧）。"""
    target_fps = max(fps / step, 1.0)
    subprocess.run(
        [
            str(ffmpeg), "-hide_banner", "-loglevel", "error",
            "-i", str(clip),
            "-vf", f"fps={target_fps}",
            "-y", str(out_dir / "f%04d.png"),
        ],
        check=True,
    )
    return sorted(out_dir.glob("f*.png"))


def keyed_bbox(image: Image.Image, threshold: int = 25) -> tuple[int, int, int, int] | None:
    """
    抠掉纯黑背景后再算包围盒。

    ⚠️ 不抠的话 `getbbox()` 永远返回整帧（640x360），
    因为黑色背景本身是不透明的 —— 我第一版就是这样，40 帧全是 640x360，
    完全分析不出姿势。必须先把"近黑且无色"的像素当成背景去掉。

    判据与渲染层的抠图一致（见 fullscreenWatcher/videoPlayer 的说明）：
    亮度低**且**饱和度低才算背景，这样睫毛/描边这些"角色自己的黑"能保住。
    """
    import numpy as np

    rgb = np.array(image)
    r = rgb[..., 0].astype(int)
    g = rgb[..., 1].astype(int)
    b = rgb[..., 2].astype(int)
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    dark = (r <= threshold) & (g <= threshold) & (b <= threshold)
    is_bg = dark & ((mx - mn) <= 12)

    # 从四边洪泛：只有连到边缘的黑才算背景（睫毛等被围住的暗部要保留）
    h, w = is_bg.shape
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

    # 背景为 True 的地方算"空"
    mask = ~reachable
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def main() -> int:
    parser = argparse.ArgumentParser(description="分析姿势变化，定位侧躺/趴着的帧区间")
    parser.add_argument("clip", type=Path)
    parser.add_argument("--ffmpeg", type=Path, default=DEFAULT_FFMPEG)
    parser.add_argument("--step", type=int, default=4, help="每隔几帧取一帧（默认 4）")
    args = parser.parse_args()

    if not args.clip.exists():
        raise SystemExit(f"片源不存在：{args.clip}")
    if not args.ffmpeg.exists():
        raise SystemExit(f"ffmpeg 不存在：{args.ffmpeg}")

    fps = probe_fps(args.clip, args.ffmpeg)
    print(f"片源 {args.clip.name}  fps={fps:.2f}  每 {args.step} 帧取一帧")

    with tempfile.TemporaryDirectory() as tmp:
        frames = extract_frames(args.clip, args.ffmpeg, fps, args.step, Path(tmp))
        print(f"抽出 {len(frames)} 帧")
        print()
        print("  序号   原始帧号   包围盒(宽x高)   宽高比   高度占比")
        rows = []
        for i, path in enumerate(frames):
            image = Image.open(path).convert("RGBA")
            bbox = keyed_bbox(image)
            if bbox is None:
                continue
            w = bbox[2] - bbox[0]
            h = bbox[3] - bbox[1]
            ratio = w / h if h else 0
            # 高度占比：侧躺时角色整体变"矮"
            h_ratio = h / image.height
            frame_no = (i + 1) * args.step
            rows.append((i, frame_no, w, h, ratio, h_ratio))

        for r in rows:
            flag = ""
            if r[4] >= 1.6:
                flag = "  <<< 很扁（疑似侧躺/趴着）"
            elif r[4] >= 1.2:
                flag = "  <- 偏扁"
            print(f"  {r[0]:>4}   {r[1]:>6}   {r[2]:>4}x{r[3]:<5}   {r[4]:>5.2f}   {r[5]:>6.2f}{flag}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
