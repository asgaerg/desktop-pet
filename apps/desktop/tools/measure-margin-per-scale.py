"""量各缩放下"立绘可见边到窗口边"的实际留白。

用户报告：「在设置中调节宠物大小之后窗口的留白变化了，改成自适应」。

`clampMargin` / `artMargin` 都是按 `ART_BOX_RATIO × petScale` 线性推算的。
如果实际渲染的立绘不随缩放线性变化（视频裁剪、取整、dpr 都可能引入偏差），
那推算值就会和真实留白对不上 —— 表现就是"调大小之后贴边不准"。

所以这里逐档缩放实测：抓窗口截图，量不透明像素到四边的距离。
"""

from __future__ import annotations

import glob
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
info = json.loads((src / "info.json").read_text(encoding="utf-8")) if (src / "info.json").exists() else {}

ELECTRON = r"F:\ai项目\node_modules\.pnpm\electron@32.3.3\node_modules\electron\dist\electron.exe"


def main() -> int:
    shots = sorted(glob.glob(str(src / "s*.png")))
    if not shots:
        print("  没有截图")
        return 1
    print(f"  {'缩放':>5} {'窗口':>10} {'立绘':>12}  左   右   上   下   对称?")
    for f in shots:
        name = Path(f).stem  # s100 = scale 1.00
        scale = int(name[1:]) / 100
        win = info.get(str(scale), {})
        img = Image.open(f).convert("RGBA")
        a = np.array(img)
        mask = a[..., 3] > 200
        ys, xs = np.where(mask)
        if len(xs) == 0:
            print(f"  {scale:>5.2f}  (画面全空)")
            continue
        W, H = a.shape[1], a.shape[0]
        dpr = 1.25
        left = round(xs.min() / dpr)
        right = round((W - 1 - xs.max()) / dpr)
        top = round(ys.min() / dpr)
        bottom = round((H - 1 - ys.max()) / dpr)
        art_w = round((xs.max() - xs.min() + 1) / dpr)
        art_h = round((ys.max() - ys.min() + 1) / dpr)
        sym = "是" if abs(left - right) <= 6 else f"否(差{abs(left - right)})"
        print(
            f"  {scale:>5.2f} {win.get('win', '?')!s:>10} {art_w:>5}x{art_h:<6}"
            f" {left:>4} {right:>4} {top:>4} {bottom:>4}   {sym}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
