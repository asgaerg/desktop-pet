"""把设计稿里的图标区域放大裁出来，看清"本来长什么样"。

## 为什么要

抠出来的图标里 `eye` / `sun` / `memory` 是实心块，我怀疑**方向不对**：
`get_drawings()` 把「外圈 + 内圈」拆成两条路径（`nonzero` 下内圈要反向绕才有洞），
而 SVG 里两条独立 `<path>` 的绕向各自独立 —— 洞就丢了。

对照 fill-rule 的三种组合**结果完全一样**，说明问题比 fill-rule 更靠前。

所以要回到源头：**看设计稿本身长什么样**。

⚠️ 白底渲染再看，因为图标是深色的；再裁到图标那一列并放大 8 倍。
"""

from __future__ import annotations

import pathlib

import pymupdf
from PIL import Image

SRC = pathlib.Path(r"F:\美术资源\桌宠右键菜单 UI.pdf")
OUT = pathlib.Path(r"F:\ai项目\apps\desktop\pet-data\ptt-tmp")
OUT.mkdir(parents=True, exist_ok=True)

doc = pymupdf.open(SRC)
page = doc[0]

# 图标在 x 63..81 那一列；每行 36 高（第 1 页）
ROWS = [
    ("moon", 63, 97),
    ("chat", 97, 133),
    ("eye", 133, 173),
    ("paw", 175, 211),
    ("pad", 211, 247),
    ("mic", 247, 287),
    ("spark", 287, 327),
    ("memory", 327, 364),
    ("sun", 364, 404),
    ("power", 404, 444),
    ("copy", 444, 484),
]

# 整页按 8 倍渲染一次，然后逐行裁
zoom = 8.0
pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
full = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

tiles: list[Image.Image] = []
for name, y0, y1 in ROWS:
    box = (
        int(60 * zoom),
        int(y0 * zoom),
        int(84 * zoom),
        int((y0 + 36) * zoom),
    )
    tile = full.crop(box)
    tiles.append(tile)

gap = 8
w = sum(t.width for t in tiles) + gap * (len(tiles) + 1)
h = tiles[0].height + gap * 2
sheet = Image.new("RGB", (w, h), (255, 255, 255))
x = gap
for tile in tiles:
    sheet.paste(tile, (x, gap))
    x += tile.width + gap

# 缩小一点便于查看
scale = 0.5
sheet = sheet.resize((int(sheet.width * scale), int(sheet.height * scale)), Image.LANCZOS)
out = OUT / "design-icons-zoom.png"
sheet.save(out)
print(f"  行序：{' / '.join(n for n, _, _ in ROWS)}")
print(f"  -> {out}  ({sheet.width}x{sheet.height})")

doc.close()
