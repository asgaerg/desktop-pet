"""从用户新给的立绘里裁出**头部**，并生成多尺寸预览让人挑。

## 为什么要先出预览

托盘图标只有 16px。整只小人缩到 16px 后脸部只剩两三个像素，完全认不出是谁 ——
所以必须只留头。但"头"到哪为止（要不要留辫子、留多少脖子）是审美问题，
**不该由我拍**，所以一次出 6 个候选，横排对比。

## 与现有 `make-tray-icon.py` 的关系

那个脚本的 `crop_head(frame)` 是给 **256×256 单帧序列图**写的：
它按"整帧"的比例算（top 0.26 / height 0.68），而整帧里角色只占一部分，
所以比例是"相对帧"而不是"相对角色"。

用户这次给的是**一张整图**（1470×630，角色只占中间 375×513）。
直接套 0.26/0.68 会把比例算歪 —— 45 + 513*0.26 = 182，那是肩膀不是头顶。

所以这里按**角色自己的包围盒**算比例，这才是可迁移的做法。
"""

from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw

ROOT = pathlib.Path(__file__).resolve().parents[1]
# 上一步量出来的角色包围盒（已排除左上角的小碎片）
LEFT, TOP, RIGHT, BOTTOM = 551, 49, 926, 562

SRC = ROOT / "assets" / "src" / "avatar-full.webp"
OUT = ROOT / "assets" / "src" / "_head-candidates.png"

# (名字, 从头顶往下的起点比例, 到哪结束的比例, 横向半宽系数)
# 比例都是**相对角色自身高度 513** 算的
CANDIDATES = [
    ("全头+辫子", 0.00, 0.62, 0.50),
    ("全头", 0.00, 0.55, 0.50),
    ("头略紧", 0.04, 0.52, 0.50),
    ("只要脸", 0.10, 0.50, 0.46),
    ("宽一点", 0.00, 0.58, 0.62),
    ("很紧(脸+眼)", 0.14, 0.48, 0.44),
]


def main() -> int:
    if not SRC.exists():
        print(f"找不到 {SRC}")
        return 1
    sheet = Image.open(SRC).convert("RGBA")
    char = sheet.crop((LEFT, TOP, RIGHT, BOTTOM))
    cw, ch = char.size
    print(f"  角色 {cw}x{ch}")

    previews = []
    for name, top_ratio, bot_ratio, half_ratio in CANDIDATES:
        y0 = round(ch * top_ratio)
        y1 = round(ch * bot_ratio)
        cx = cw // 2
        half = round(cw * half_ratio)
        x0 = max(0, cx - half)
        x1 = min(cw, cx + half)
        head = char.crop((x0, y0, x1, y1))
        print(f"  {name:<12} 裁 {head.size}  (y {y0}-{y1}, x {x0}-{x1})")
        # 缩到 256 高的方块，模拟最终图标
        head.thumbnail((256, 256), Image.LANCZOS)
        canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        canvas.paste(head, ((256 - head.width) // 2, (256 - head.height) // 2), head)
        previews.append((name, canvas))

    # 拼一张对比图：每个候选 256 宽 + 40 空隙，底部留 34px 写名字
    gap = 20
    W = len(previews) * 256 + (len(previews) + 1) * gap
    H = 256 + 88
    board = Image.new("RGBA", (W, H), (34, 36, 44, 255))
    draw = ImageDraw.Draw(board)
    x = gap
    for name, img in previews:
        board.paste(img, (x, 34), img)
        # 名字用英文/数字避免字体缺失时的方块（PIL 默认字体不支持中文）
        draw.text((x + 4, 8), f"{previews.index((name, img)) + 1}", fill=(230, 230, 240, 255))
        draw.text((x + 4, 256 + 44), f"#{previews.index((name, img)) + 1}", fill=(150, 150, 165, 255))
        x += 256 + gap
    board.save(OUT)
    print(f"\n  对比图 -> {OUT}")
    print("  编号对应：")
    for i, (name, _) in enumerate(previews, 1):
        print(f"    {i}. {name}")

    # 另外单独把 16px 的效果也拼一条 —— 那才是托盘里的真实大小
    strip = Image.new("RGBA", (len(previews) * 34 + 10, 40), (34, 36, 44, 255))
    sx = 5
    for _name, img in previews:
        small = img.resize((16, 16), Image.LANCZOS)
        strip.paste(small, (sx, 12), small)
        sx += 34
    strip.save(ROOT / "assets" / "src" / "_head-16px.png")
    print(f"  16px 真实效果 -> assets/src/_head-16px.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
