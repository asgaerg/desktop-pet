"""重新生成状态条五档对比图（2026-10-17）。

## 为什么旧的必须重做

`apps/desktop/pet-data/status-shot/状态条-四档对比.png` 是 10-16 生成的，
而 10-17 状态条**整个重做了**：

    旧（10-16，六档）  idle / thinking / searching / voice / speaking(琥珀) / error
    新（10-17，五档）  idle / thinking / searching / speaking(**青**) / error
                        'voice'（语音准备中）已删，合成期并入 thinking

所以那张「四档对比」既**数量不对**又**颜色不对**（说话中从琥珀改成青色）。
⚠️ 过时的验收证据比没有更糟 —— 它会让人以为现状是图里那样。

## 做法

用 `tools/shot-page.py` 逐个档位离屏渲染 `status.html`，再拼成一张对比图。
拼图用 Pillow（项目已依赖）。

⚠️ 不从真机上截：真机的胶囊大小受 DPI/窗口状态影响，而对比图要的是
"页面自己画的颜色和文字"，离屏渲染才是可复现的。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(r"F:\ai项目")
SHOT = ROOT / "tools" / "shot-page.py"
OUT_DIR = ROOT / "apps" / "desktop" / "pet-data" / "status-shot"
PY = ROOT / ".venv" / "Scripts" / "python.exe"

# (data-state, 文字, 说明) —— 与 status.css 的五个规则一一对应
TIERS = [
    ("idle", "空闲", "#34c759 绿"),
    ("thinking", "思考", "#bf7af0 紫（呼吸）"),
    ("searching", "联网搜索中", "#2f86ed 蓝（呼吸）"),
    ("speaking", "说话中", "#32ade6 青（呼吸）"),
    ("error", "异常", "#e5484d 红"),
]


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 高度随字数：statusWindow 的 heightForLabel = 78 + (字数-2)*15.3
    made: list[tuple[str, Path]] = []
    for state, label, _note in TIERS:
        extra = max(0, len(label) - 2)
        height = int(round(78 + extra * 15.3))
        out = OUT_DIR / f"_tier-{state}.png"
        js = (
            f"document.querySelector('.light').dataset.state = {state!r};\n"
            f"document.querySelector('.text').textContent = {label!r};\n"
        )
        cmd = [
            str(PY), str(SHOT),
            "apps/desktop/src/renderer/status.html", str(out),
            "40", str(height), "dark",
            f"js={js}",
            "settle=700",
        ]
        print(f"  ▶ {state:10s} {label:<6} {height}px")
        # ⚠️ 必须显式指定编码：Windows 上 `text=True` 默认用 GBK 解码，
        #    而 shot-page.py 的输出里有中文 —— 会抛 UnicodeDecodeError
        #    （不影响截图本身，但会把日志打成一片红，看着像失败了）。
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=ROOT,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0 or not out.exists():
            print(f"    ❌ 失败：{result.stderr.strip()[:200]}")
            continue
        made.append((state, out))

    if not made:
        print("  ❌ 一张都没截到")
        return 1

    # ── 拼成一张横向对比图 ──
    from PIL import Image, ImageDraw

    images = [(state, Image.open(path).convert("RGBA")) for state, path in made]
    gap, pad, caption_h = 18, 16, 46
    total_w = sum(im.width for _s, im in images) + gap * (len(images) - 1) + pad * 2
    max_h = max(im.height for _s, im in images)
    total_h = max_h + caption_h + pad * 2
    canvas = Image.new("RGBA", (total_w, total_h), (24, 27, 33, 255))
    draw = ImageDraw.Draw(canvas)

    x = pad
    for (state, im), (_s, _label, note) in zip(images, TIERS):
        # 档位色板背景，让"哪个是哪个"一眼能对上
        canvas.alpha_composite(im, (x, caption_h))
        draw.text((x, 8), state, fill=(200, 205, 215, 255))
        draw.text((x, 24), note.split()[0], fill=(150, 155, 165, 255))
        x += im.width + gap

    out = OUT_DIR / "状态条-五档对比.png"
    canvas.convert("RGB").save(out)
    print()
    print(f"  ✅ 已生成 {out.relative_to(ROOT)}  ({out.stat().st_size // 1024} KB)")

    # 删掉中间产物
    for _state, path in made:
        path.unlink(missing_ok=True)
    # 旧的四档图已过时，删掉避免误用
    old = OUT_DIR / "状态条-四档对比.png"
    if old.exists():
        old.unlink()
        print(f"  🗑 删掉过时的 状态条-四档对比.png（数量+颜色都变了）")
    return 0


raise SystemExit(main())
