"""设计稿 vs 我的实现 —— 同比例并排对照图（2026-10-12）。

## 为什么

用户说「ui 都搞反了」，而我把能量到的几何全对了一遍（面板/行高/图标位置/
文字位置/聚焦底内缩/开关两态）—— **全对得上**。

那说明**我看漏了某个维度**。肉眼并排是找出"看漏了什么"最快的办法：
把设计稿第 1 页和我的渲染放在同一张图里、按同一比例，
**任何差异都会跳出来**。

## 做法

① 把设计稿第 1 页面板区域按 3 倍渲染
② 把我的 `one.html`（单栏 1:1）也按同样比例渲染
③ 横向拼起来，中间留一条分隔
"""

from __future__ import annotations

import json
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
PREVIEW = ROOT / "tools" / "menupreview" / "one.html"
TMP = ROOT / "apps" / "desktop" / "pet-data" / "ptt-tmp"
TMP.mkdir(parents=True, exist_ok=True)

SCALE = 2  # 最终图的缩放

# ── ① 设计稿：裁面板区域（x48..348, y48..485）──
doc = pymupdf.open(PDF)
page = doc[0]
zoom = 3.0
pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
full = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
design = full.crop(
    (int(44 * zoom), int(44 * zoom), int(352 * zoom), int(489 * zoom))
)
doc.close()

# ── ② 我的实现：离屏渲染 one.html ──
ELECTRON = (
    ROOT
    / "node_modules"
    / ".pnpm"
    / "electron@32.3.3"
    / "node_modules"
    / "electron"
    / "dist"
    / "electron.exe"
)
SHOT = TMP / "one-3x.png"
TEMPLATE = """
const { app, BrowserWindow } = require('electron');
const fs = require('fs');
app.disableHardwareAcceleration();

const MEASURE = 'JSON.stringify(document.querySelector(".panel").getBoundingClientRect().toJSON())';

app.whenReady().then(async function () {
  /*
   * ⚠️ 窗口尺寸**不能当作裁图依据** —— Electron 有最小窗口尺寸，
   *    `setContentSize(306, 434)` 实际渲染出来是 385x545（我踩过）。
   *    所以先量 `.panel` 的 `getBoundingClientRect()`，再按它裁。
   */
  const win = new BrowserWindow({
    width: 900, height: 700, show: false,
    webPreferences: { offscreen: true, partition: 'cmp' + Date.now() },
  });
  await win.loadURL(%(url)s);
  await new Promise(function (r) { setTimeout(r, 1500); });

  const rect = JSON.parse(await win.webContents.executeJavaScript(MEASURE));
  /*
   * ⚠️ `capturePage()` 给的是**物理像素**，而 `getBoundingClientRect()`
   *    是 **CSS 像素** —— 这台机器 DPR=1.25，直接用 rect 裁会**裁断**（我踩过）。
   *    所以把 DPR 一并带出去，由 Python 那边乘上去。
   */
  const dpr = await win.webContents.executeJavaScript('window.devicePixelRatio');
  const image = await win.webContents.capturePage();
  fs.writeFileSync(%(shot)s, image.toPNG());
  fs.writeFileSync(%(rect)s, JSON.stringify({ rect: rect, dpr: dpr }));
  console.log('RECT dpr=' + dpr + ' ' + Math.round(rect.width) + 'x' + Math.round(rect.height));
  app.exit(0);
});
"""
script = TEMPLATE % {
    "url": repr(PREVIEW.as_uri() + "?t=" + str(int(__import__("time").time()))),
    "shot": repr(str(SHOT).replace("\\", "/")),
    "rect": repr(str(SHOT.with_suffix(".json")).replace("\\", "/")),
}
script_path = TMP / "_cmp.cjs"
script_path.write_text(script, encoding="utf-8")

env = dict(os.environ)
env.pop("ELECTRON_RUN_AS_NODE", None)
subprocess.run(
    [str(ELECTRON), str(script_path), "--no-sandbox"],
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    timeout=180,
    env=env,
    cwd=str(ROOT / "apps" / "desktop"),
)
script_path.unlink(missing_ok=True)

if not SHOT.exists():
    print("  ❌ 我的渲染失败")
    raise SystemExit(1)

# 按 `.panel` 的实际矩形裁（窗口尺寸不可靠，见上面脚本里的说明）
RECT_FILE = SHOT.with_suffix(".json")
meta = json.loads(RECT_FILE.read_text(encoding="utf-8")) if RECT_FILE.exists() else None
shot = Image.open(SHOT).convert("RGB")
if meta:
    rect = meta["rect"]
    dpr = meta.get("dpr", 1) or 1
    box = (
        int(rect["x"] * dpr),
        int(rect["y"] * dpr),
        int((rect["x"] + rect["width"]) * dpr),
        int((rect["y"] + rect["height"]) * dpr),
    )
    mine = shot.crop(box)
    print(f"  DPR={dpr}  截图 {shot.size}  按面板矩形裁 {box} -> {mine.size}")
else:
    mine = shot
print(f"  设计稿裁图 {design.size}   我的渲染 {mine.size}")

# ── ③ 等高缩放后并排 ──
target_h = min(design.height, mine.height)
design2 = design.resize(
    (int(design.width * target_h / design.height), target_h), Image.LANCZOS
)
mine2 = mine.resize((int(mine.width * target_h / mine.height), target_h), Image.LANCZOS)

gap = 24
sheet = Image.new("RGB", (design2.width + mine2.width + gap * 3, target_h + gap * 2), (235, 236, 240))
sheet.paste(design2, (gap, gap))
sheet.paste(mine2, (gap * 2 + design2.width, gap))

sheet = sheet.resize((sheet.width // SCALE, sheet.height // SCALE), Image.LANCZOS)
out = TMP / "compare-design-vs-mine.png"
sheet.save(out)
print(f"  -> {out}  ({sheet.width}x{sheet.height})")
print("     左 = 设计稿第 1 页   右 = 我的实现")
