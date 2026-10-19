"""对照：设计稿原图 vs 我抠的图标 —— 看 y 轴翻对了没有（2026-10-12）。

## 背景

用户说图标要「旋转 180 度，然后 3d 向右旋转 180 度」——
`rotateY(180°)` + `rotateZ(180°)` 合起来 = **上下镜像**。
也就是他在说：**我抠的图标上下颠倒了。**

## 为什么会颠倒（很可能的根因）

我在 `extract-menu-icons.py` 里做了 `flip_y(py) = y_max - py`，
理由是"PDF 的 y 向上、SVG 的 y 向下"。

⚠️ 但 `get_drawings()` 返回的坐标**已经是 PyMuPDF 归一化过的**
（原点在页面**左上角**、y 向下）—— 也就是说**不需要再翻**。
多翻一次就上下颠倒了。

## 怎么验

把设计稿的图标区域裁出来（放大），和我抠的 SVG 并排。
一眼就能看出哪个是正的 —— 电源符号（圆环 + 竖线朝下）最明显。
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pymupdf
from PIL import Image

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

PDF = pathlib.Path(r"F:\美术资源\桌宠右键菜单 UI.pdf")
ICONS = ROOT / "tools" / "menupreview" / "icons"
TMP = ROOT / "apps" / "desktop" / "pet-data" / "ptt-tmp"
TMP.mkdir(parents=True, exist_ok=True)

# ── ① 设计稿第 1 页的图标列（x 60..84，整列）──
doc = pymupdf.open(PDF)
page = doc[0]
zoom = 6.0
pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
full = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
design_col = full.crop((int(60 * zoom), int(60 * zoom), int(84 * zoom), int(485 * zoom)))
doc.close()

# ── ② 我的 SVG 图标竖排（用 Electron 渲染一个对照页）──
NAMES = ["moon", "chat", "eye", "paw", "pad", "mic", "spark", "memory", "sun", "power", "copy"]
rows = "\n".join(
    f'<div class="r"><img src="icons/{n}.svg" /><span>{n}</span></div>' for n in NAMES
)
html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8" />
<style>
  body {{ margin:0; padding:0; background:#fff; font-family:system-ui,sans-serif; }}
  .r {{ display:flex; align-items:center; gap:6px; height:38px; padding-left:2px; }}
  img {{ width:16px; height:16px; object-fit:contain; color:#222; }}
  span {{ font-size:9px; color:#98a0a8; }}
</style></head><body>{rows}</body></html>"""
page_path = ROOT / "tools" / "menupreview" / "_orientation.html"
page_path.write_text(html, encoding="utf-8")

ELECTRON = (
    ROOT / "node_modules" / ".pnpm" / "electron@32.3.3" / "node_modules"
    / "electron" / "dist" / "electron.exe"
)
SHOT = TMP / "orientation-mine.png"
TEMPLATE = """
const { app, BrowserWindow } = require('electron');
const fs = require('fs');
app.disableHardwareAcceleration();
app.whenReady().then(async function () {
  const win = new BrowserWindow({ width: 200, height: 460, show: false,
    webPreferences: { offscreen: true, partition: 'ori' + Date.now() } });
  await win.loadURL(%(url)s);
  await new Promise(function (r) { setTimeout(r, 1200); });
  const image = await win.webContents.capturePage();
  fs.writeFileSync(%(shot)s, image.toPNG());
  console.log('SHOT ok');
  app.exit(0);
});
"""
script_path = TMP / "_ori.cjs"
script_path.write_text(
    TEMPLATE % {
        "url": repr(page_path.as_uri() + "?t=" + str(int(__import__("time").time()))),
        "shot": repr(str(SHOT).replace("\\", "/")),
    },
    encoding="utf-8",
)
env = dict(os.environ)
env.pop("ELECTRON_RUN_AS_NODE", None)
subprocess.run(
    [str(ELECTRON), str(script_path), "--no-sandbox"],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
    timeout=120, env=env, cwd=str(ROOT / "apps" / "desktop"),
)
script_path.unlink(missing_ok=True)

mine = Image.open(SHOT).convert("RGB")
# 按 devicePixelRatio 缩放回去（这台机器 1.25）
mine = mine.resize((int(mine.width / 1.25), int(mine.height / 1.25)), Image.LANCZOS)

# ── ③ 并排（两边缩到同高）──
target_h = min(design_col.height, mine.height)
d2 = design_col.resize((int(design_col.width * target_h / design_col.height), target_h), Image.LANCZOS)
m2 = mine.resize((int(mine.width * target_h / mine.height), target_h), Image.LANCZOS)

gap = 30
sheet = Image.new("RGB", (d2.width + m2.width + gap * 3, target_h + gap * 2), (245, 246, 248))
sheet.paste(d2, (gap, gap))
sheet.paste(m2, (gap * 2 + d2.width, gap))
out = TMP / "orientation-compare.png"
sheet.save(out)

print(f"  设计稿列 {design_col.size}   我的 {mine.size}")
print(f"  -> {out}  ({sheet.width}x{sheet.height})")
print("     左 = 设计稿原图   右 = 我抠的 SVG")
page_path.unlink(missing_ok=True)
