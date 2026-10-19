"""生成「上下翻」变体，和设计稿三方对照（2026-10-12）。

## 为什么要三方对照

我看了两遍并排缩略图都没把握图标到底翻没翻 —— **肉眼在 12px 的图标上不可靠**。

而设计稿的 `power` 有**数值事实**：

```
竖线（窄）  y 416.24..422.96   -> 归一化 0.00..6.72
圆环（宽）  y 418.43..428.79   -> 归一化 2.19..12.55
```

竖线的 y 范围**比圆环更靠上** => 设计稿是「竖线朝上」的标准电源符号。

## 两个候选

  A 现在这个（抠图时做了 `y_max - py`，含上下镜像）
  B 上下翻回来（`scaleY(-1)`）

哪个和设计稿一致就用哪个 —— **一次就能看清，不用再猜**。
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
TMP = ROOT / "apps" / "desktop" / "pet-data" / "ptt-tmp"
ICONS = ROOT / "tools" / "menupreview" / "icons"

# ── ① 设计稿的图标列（放大）──
doc = pymupdf.open(PDF)
page = doc[0]
zoom = 8.0
pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
full = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
design = full.crop((int(62 * zoom), int(408 * zoom), int(82 * zoom), int(440 * zoom)))
doc.close()

# ── ② 两个候选（同一页里并排，带 scaleY 变体）──
NAMES = ["moon", "chat", "eye", "paw", "pad", "mic", "spark", "memory", "sun", "power", "copy"]
rows_a = "\n".join(f'<div class="c"><img src="icons/{n}.svg" /><b>{n}</b></div>' for n in NAMES)
rows_b = "\n".join(
    f'<div class="c"><img class="flip" src="icons/{n}.svg" /><b>{n}</b></div>' for n in NAMES
)
html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8" />
<style>
  body {{ margin:0; padding:8px; background:#fff; font-family:system-ui,sans-serif;
         display:flex; gap:16px; }}
  .col {{ display:flex; flex-direction:column; gap:4px; }}
  .c {{ display:flex; align-items:center; gap:5px; height:20px; }}
  img {{ width:20px; height:20px; object-fit:contain; color:#222; }}
  .flip {{ transform: scaleY(-1); }}
  b {{ font-size:9px; color:#98a0a8; font-weight:400; }}
  h4 {{ margin:0 0 4px; font-size:10px; color:#374151; }}
</style></head><body>
  <div class="col"><h4>A 现在（抠图时翻了）</h4>{rows_a}</div>
  <div class="col"><h4>B 再翻回来</h4>{rows_b}</div>
</body></html>"""
page_path = ROOT / "tools" / "menupreview" / "_flipcmp.html"
page_path.write_text(html, encoding="utf-8")

ELECTRON = (
    ROOT / "node_modules" / ".pnpm" / "electron@32.3.3" / "node_modules"
    / "electron" / "dist" / "electron.exe"
)
SHOT = TMP / "flip-candidates.png"
TEMPLATE = """
const { app, BrowserWindow } = require('electron');
const fs = require('fs');
app.disableHardwareAcceleration();
app.whenReady().then(async function () {
  const win = new BrowserWindow({ width: 420, height: 340, show: false,
    webPreferences: { offscreen: true, partition: 'fl' + Date.now() } });
  await win.loadURL(%(url)s);
  await new Promise(function (r) { setTimeout(r, 1200); });
  const rect = JSON.parse(await win.webContents.executeJavaScript(
    'JSON.stringify(document.body.getBoundingClientRect().toJSON())'));
  const dpr = await win.webContents.executeJavaScript('window.devicePixelRatio');
  const image = await win.webContents.capturePage();
  fs.writeFileSync(%(shot)s, image.toPNG());
  fs.writeFileSync(%(meta)s, JSON.stringify({ rect: rect, dpr: dpr }));
  app.exit(0);
});
"""
meta_path = SHOT.with_suffix(".json")
script_path = TMP / "_flip.cjs"
script_path.write_text(
    TEMPLATE % {
        "url": repr(page_path.as_uri() + "?t=" + str(int(__import__("time").time()))),
        "shot": repr(str(SHOT).replace("\\", "/")),
        "meta": repr(str(meta_path).replace("\\", "/")),
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

import json  # noqa: E402

meta = json.loads(meta_path.read_text(encoding="utf-8"))
r = meta["rect"]
dpr = meta.get("dpr", 1) or 1
shot = Image.open(SHOT).convert("RGB")
cands = shot.crop((0, 0, int(r["width"] * dpr), int(r["height"] * dpr)))

# ── ③ 三方并排：设计稿(power) 单独放大 + 两个候选列 ──
target_h = 420
d2 = design.resize((int(design.width * target_h / design.height), target_h), Image.LANCZOS)
c2 = cands.resize((int(cands.width * target_h / cands.height), target_h), Image.LANCZOS)

gap = 20
sheet = Image.new("RGB", (d2.width + c2.width + gap * 3, target_h + gap * 2), (245, 246, 248))
sheet.paste(d2, (gap, gap))
sheet.paste(c2, (gap * 2 + d2.width, gap))
out = TMP / "flip-compare.png"
sheet.save(out)
print(f"  设计稿(power 放大) {d2.size}   候选 A/B {c2.size}")
print(f"  -> {out}")
print("     最左 = 设计稿的 power（竖线朝上）；右边两列 = A 现在 / B 翻回来")
page_path.unlink(missing_ok=True)
