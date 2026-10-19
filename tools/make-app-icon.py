"""从立绘帧生成应用图标（2026-10-18）。

## 为什么要生成新的

`apps/desktop/assets/pet.ico` 是早先手工截的，问题有两个：

1. 底部是**贴边探头的渐隐**（立绘"从屏幕边缘探头"那套动画的产物），
   缩到 16×16 之后下半截直接糊成一团灰。
2. 它是从 256 像素的 ico 里再导出来的，小尺寸没有按目标尺寸单独重采样。

这里换成"从原始 sheet 重新裁 + 每个尺寸单独 LANCZOS 缩放"。

## 构图

立绘是**全身 chibi**（177×240），整只塞进 16×16 就是一坨。
所以只取头部：从 bbox 顶端往下 ~52% 高度，水平按头部中线居中，
再留 6% 透明内边距 —— 这是 Windows 图标在任务栏/开始菜单里最好认的构图。

用法：

    python tools/make-app-icon.py                # 生成 build/icon.ico + build/icon.png
    python tools/make-app-icon.py --frame 24     # 换一帧（0/24/40 都是正脸）
"""

from __future__ import annotations

import argparse
import pathlib

from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHEET = ROOT / "apps" / "desktop" / "src" / "renderer" / "assets" / "avatar" / "default-sprite" / "sheet.png"
OUT_DIR = ROOT / "apps" / "desktop" / "build"  # electron-builder 的 buildResources（win.icon 从这里找）
FW = FH = 256
COLS = 12

# Windows 图标实际会用到的尺寸。256 给"大图标"视图，16 给任务栏。
SIZES = [16, 24, 32, 48, 64, 128, 256]


def build(frame: int) -> tuple[pathlib.Path, pathlib.Path]:
    sheet = Image.open(SHEET).convert("RGBA")
    x, y = (frame % COLS) * FW, (frame // COLS) * FH
    pic = sheet.crop((x, y, x + FW, y + FH))

    bbox = pic.getchannel("A").getbbox()
    if bbox is None:
        raise SystemExit("这一帧是全透明的，换一帧")
    left, top, right, bottom = bbox
    height = bottom - top

    # 取景：从 bbox 顶端 6% 处往下到 56%，水平半宽 = 身高 40%（2026-10-18 试了三组）
    #
    #   v0 顶 0% -> 52%、半宽 50%   留了太多头发，16px 看不清脸
    #   v2 顶 10% -> 60%、半宽 34%  256px 把耳朵和头发裁掉了，太紧
    #   v1 顶 6% -> 56%、半宽 40%   既有五官又留了发型轮廓 <- 选它
    head_top = top + int(height * 0.06)
    head_bottom = top + int(height * 0.56)
    head_center_x = (left + right) // 2
    half = max(int(height * 0.40), 1)
    crop = (head_center_x - half, head_top, head_center_x + half, head_bottom)

    head = pic.crop(crop)
    side = head.width
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.alpha_composite(head, (0, 0))

    # 6% 内边距：贴边会让小尺寸显得拥挤
    pad = int(side * 0.06)
    padded = Image.new("RGBA", (side + pad * 2, side + pad * 2), (0, 0, 0, 0))
    padded.alpha_composite(canvas, (pad, pad))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    png_path = OUT_DIR / "icon.png"
    padded.resize((512, 512), Image.LANCZOS).save(png_path)

    ico_path = OUT_DIR / "icon.ico"
    # Pillow 的 save(..., sizes=...) 内部会**逐尺寸**重采样（不是缩放同一张），
    # 这正是小尺寸清晰的关键。
    padded.resize((256, 256), Image.LANCZOS).save(ico_path, format="ICO", sizes=[(s, s) for s in SIZES])
    return ico_path, png_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frame", type=int, default=24, help="立绘帧号（默认 24：正脸、眼睛睁开）")
    args = ap.parse_args()
    ico, png = build(args.frame)
    for p in (ico, png):
        print(f"  ✅ {p.relative_to(ROOT)}  {p.stat().st_size / 1024:.0f} KB")
    im = Image.open(ico)
    print("  内含尺寸:", sorted(im.ico.sizes()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
