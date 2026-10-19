"""量出角色在视频里的**真实可见范围** —— 用来定 cropRect，而不是用 body_box。

`body_box = [212,60,428,330]` 是**可拖拽区域**（覆盖躯干），不是整只角色的外接框。
`cropRect()` 拿它当基准 + 大留白，结果裁切区涨到 367x360 ≈ 整帧，
于是"整帧缩进 240x240 画布" —— 角色被一起缩小，又小又糊。

这个脚本对若干条视频抽帧、按 alpha/亮度找前景，输出跨帧的并集包围盒。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

FFMPEG = Path(r"F:\ref-pet\_internal\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe")
SRC = Path(r"F:\ai项目\apps\desktop\src\renderer\assets\avatar\default")


def probe(path: Path) -> tuple[np.ndarray, int, int]:
    """抽 8 帧，返回"前景并集"掩码与尺寸。"""
    raw = subprocess.run(
        [
            str(FFMPEG), "-v", "error", "-i", str(path),
            "-vf", "fps=1,format=rgb24", "-f", "rawvideo", "-",
        ],
        capture_output=True,
        check=False,
    ).stdout
    if not raw:
        raise RuntimeError("ffmpeg 没输出")

    # 先问尺寸
    info = subprocess.run(
        [str(FFMPEG), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, check=False,
    ).stderr
    import re

    m = re.search(r"(\d{2,5})x(\d{2,5})", info)
    if not m:
        raise RuntimeError("量不到尺寸")
    w, h = int(m.group(1)), int(m.group(2))

    frame = w * h * 3
    n = len(raw) // frame
    if n == 0:
        raise RuntimeError("没有完整帧")

    union = np.zeros((h, w), dtype=bool)
    for i in range(n):
        buf = np.frombuffer(raw[i * frame : (i + 1) * frame], dtype=np.uint8).reshape(h, w, 3)
        # 背景是纯黑（抠图 alpha 的来源）；前景 = 明显亮于黑
        lum = buf.max(axis=2)
        union |= lum > 28
    return union, w, h


def main() -> int:
    vids = sorted(SRC.rglob("*.webm"))
    if not vids:
        print(f"  没找到视频：{SRC}")
        return 1

    # 每条状态抽一条代表，避免跑 106 条
    picked: list[Path] = []
    seen: set[str] = set()
    for v in vids:
        key = v.parent.name
        if key not in seen:
            seen.add(key)
            picked.append(v)
    print(f"  共 {len(vids)} 条视频，按状态各取 1 条，测 {len(picked)} 条")
    print()

    print(f"  {'状态':<16} {'源尺寸':>10} {'真实包围盒':>26} {'占比':>8}")
    print("  " + "-" * 68)

    boxes: list[tuple[int, int, int, int]] = []
    for v in picked:
        try:
            mask, w, h = probe(v)
        except Exception as exc:  # noqa: BLE001
            print(f"  {v.parent.name:<16} 失败：{exc}")
            continue
        ys, xs = np.where(mask)
        if len(xs) == 0:
            print(f"  {v.parent.name:<16} 全黑（量不到）")
            continue
        x0, x1 = int(xs.min()), int(xs.max())
        y0, y1 = int(ys.min()), int(ys.max())
        boxes.append((x0, y0, x1, y1))
        ratio = (x1 - x0 + 1) / w
        print(
            f"  {v.parent.name:<16} {w}x{h:<7} [{x0},{y0},{x1},{y1}]{'':>4} "
            f"{ratio:>7.1%}"
        )

    if boxes:
        ux0 = min(b[0] for b in boxes)
        uy0 = min(b[1] for b in boxes)
        ux1 = max(b[2] for b in boxes)
        uy1 = max(b[3] for b in boxes)
        print()
        print("  ---- 所有状态取并集（cropRect 应该用它）----")
        print(f"  实际包围盒 [{ux0},{uy0},{ux1},{uy1}]  宽高 {ux1-ux0+1}x{uy1-uy0+1}")
        print(f"  占源 640x360 的 {((ux1-ux0+1)/640):.1%} x {((uy1-uy0+1)/360):.1%}")
        print()
        print("  对比 body_box [212,60,428,330] 宽高 216x270（只是躯干）")
        print(f"  若按整帧缩放：屏上宽度 = 240 x {(ux1-ux0+1)/640:.3f} = "
              f"{240*(ux1-ux0+1)/640:.0f} 逻辑像素")
    return 0


if __name__ == "__main__":
    sys.exit(main())
