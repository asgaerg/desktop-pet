"""把抽好的帧拼成缩略图网格，并在旁边打印 idx -> 片名映射。

给"人工找某段动画"用的：106 段逐一看缩略图，比逐个播快得多。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

SCAN_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("scan")
# 缩略图尺寸：够看清姿势即可
CW, CH = 200, 113
COLS = 8
LABEL_H = 16


def main() -> int:
    # utf-8-sig：PowerShell 的 Set-Content -Encoding UTF8 会加 BOM，
    # 用普通 utf-8 读会报 "Unexpected UTF-8 BOM"。
    manifest = json.loads((SCAN_DIR / "manifest.json").read_text(encoding="utf-8-sig"))
    rows = (len(manifest) + COLS - 1) // COLS
    canvas = Image.new("RGB", (CW * COLS, (CH + LABEL_H) * rows), (30, 32, 40))
    draw = ImageDraw.Draw(canvas)

    for i, item in enumerate(manifest):
        path = SCAN_DIR / item["file"]
        try:
            thumb = Image.open(path).convert("RGB").resize((CW, CH))
        except Exception:
            continue
        x = (i % COLS) * CW
        y = (i // COLS) * (CH + LABEL_H)
        canvas.paste(thumb, (x, y))
        draw.text((x + 3, y + CH + 2), str(item["idx"]), fill=(255, 220, 120))

    out = SCAN_DIR / "all_grid.png"
    canvas.save(out)
    print(f"-> {out}  {canvas.size}  {len(manifest)} 帧")

    # idx -> 片名，方便对着网格找
    for item in manifest:
        print(f"{item['idx']}\t{item['rel']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
