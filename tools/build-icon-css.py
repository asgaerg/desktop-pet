"""把抠出来的 SVG 图标变成 CSS 的 mask 声明（2026-10-12）。

## 为什么用生成脚本而不是手抄

图标路径有几千个字符、还含 `#` 和引号 —— 手抄进 CSS 必然出错
（这个会话里已经因为"手拼长字符串"踩过好几次）。

生成脚本的好处：图标重抠之后**跑一次就同步**，两边不会漂移。

## 产出

`tools/menupreview/icon-masks.css` —— 只含 `.ico[data-icon='xx'] { --icon: url(...) }`，
由 `petMenu.css` 用 `@import` 引入。

⚠️ 用 `@import` 而不是把 CSS 写进 `petMenu.css`：
   一是保持生成物与手写物分离，二是 diff 时一眼能看出"哪部分是生成的"。
"""

from __future__ import annotations

import pathlib
import re
import urllib.parse

ICONS = pathlib.Path(r"F:\ai项目\tools\menupreview\icons")

"""
## ⚠️ 两个输出，**同一份内容**（2026-10-14）

  · `tools/menupreview/icon-masks.css`  —— 设计预览页用（三态对照）
  · `src/renderer/petMenu.icons.css`    —— **应用里真正生效的那份**

为什么要搬进 `renderer/`：菜单已经从 Electron 原生菜单换成了自绘窗口
（`src/renderer/petMenu.html`），它只能引渲染层目录里的资源。
但**预览页必须继续有人用** —— 那是"视觉对不对"唯一能逐像素对照设计稿的地方。

⚠️ 两份内容必须一致，否则会出现"预览页看着对、应用里图标不一样"
   —— 所以在这里**一次生成两份**，而不是让人手动拷。
"""
OUTS = [
    pathlib.Path(r"F:\ai项目\tools\menupreview\icon-masks.css"),
    pathlib.Path(r"F:\ai项目\apps\desktop\src\renderer\petMenu.icons.css"),
]
OUT = OUTS[0]  # 兼容下面的打印语句（真正的写入在文件末尾遍历 OUTS）

ORDER = [
    ("moon", "沉浸模式"),
    ("chat", "聊天模式"),
    ("speaker", "语音播报"),
    ("globe", "联网搜索"),
    ("eye", "允许看屏幕"),
    ("paw", "宠物大小"),
    ("pad", "桌宠控制"),
    ("mic", "麦克风"),
    ("spark", "活泼度"),
    ("memory", "记忆管理"),
    ("sun", "设置"),
    ("power", "退出"),
    ("copy", "退出全部实例"),
]

"""
## ⚠️ 设计稿里**没有**的图标在这里手写（2026-10-14）

「语音播报」这个开关是用户后来要求加的，设计稿 PDF 里没有对应图形。
所以给它一个手写 SVG（同样遵守抠图那三条规矩：单色、`evenodd`、
viewBox 写真实尺寸）—— 放在脚本里而不是直接改生成的 CSS，
是为了**下次重新生成时不会丢**（直接改 petMenu.icons.css 会被覆盖）。

其余 11 个仍然从 `tools/menupreview/icons/*.svg` 读（PDF 抠出来的真图形）。
"""
EXTRA_ICONS: dict[str, str] = {
    "speaker": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 14" fill-rule="evenodd">'
        '<path d="M9 1.6 L5.5 4.4 H2.4 C1.63 4.4 1 5.03 1 5.8 V8.2 C1 8.97 1.63 9.6 2.4 9.6 '
        'H5.5 L9 12.4 C9.6 12.9 10.4 12.4 10.4 11.8 V2.2 C10.4 1.6 9.6 1.1 9 1.6 Z"/>'
        '<path d="M12.6 5.2 A3.1 3.1 0 0 1 12.6 8.8" fill="none" stroke="#000" '
        'stroke-width="1.7" stroke-linecap="round"/>'
        '<path d="M15.2 3.3 A6.2 6.2 0 0 1 15.2 10.7" fill="none" stroke="#000" '
        'stroke-width="1.7" stroke-linecap="round"/>'
        "</svg>"
    ),
    # 联网搜索（2026-10-16）：地球 = 经纬圈。stroke 画圆环 + 横线 + 竖椭圆，
    # 单色 alpha mask 与其余图标一致。
    "globe": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="none">'
        '<circle cx="8" cy="8" r="6.4" stroke="#000" stroke-width="1.5"/>'
        '<ellipse cx="8" cy="8" rx="2.9" ry="6.4" stroke="#000" stroke-width="1.3"/>'
        '<path d="M1.8 8 H14.2" stroke="#000" stroke-width="1.3"/>'
        '<path d="M2.9 4.6 H13.1 M2.9 11.4 H13.1" stroke="#000" stroke-width="1.3"/>'
        "</svg>"
    ),
}


def emit(lines: list[str], name: str, label: str, svg: str) -> None:
    """把一个 SVG 变成一段 `.ico[data-icon=...]` 的 mask 声明。"""
    # 压掉换行与缩进（data URI 里没必要），但保留语义
    compact = " ".join(line.strip() for line in svg.splitlines() if line.strip())
    # 去掉注释（data URI 里越短越好，注释也没意义）
    while "<!--" in compact:
        start = compact.index("<!--")
        end = compact.index("-->", start) + 3
        compact = (compact[:start] + compact[end:]).strip()
    encoded = urllib.parse.quote(compact, safe="")
    lines.append(f"/* {label} */")
    lines.append(f".ico[data-icon='{name}'] {{")
    lines.append(f"  --icon: url(\"data:image/svg+xml,{encoded}\");")
    """
    ⚠️ **尺寸也要写进去** —— 设计稿里各图标**本来就不一样大**：

        moon  11.98 x 11.98      eye   12.40 x  7.60
        pad   15.51 x  9.09      mic   10.51 x 13.91

    统一塞进 14x14 会把比例压变形（`pad` 会变窄、`eye` 会变高）。
    """
    match = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', compact)
    iw, ih = (match.group(1), match.group(2)) if match else ("14", "14")
    lines.append(f"  --iw: {iw}px;")
    lines.append(f"  --ih: {ih}px;")
    lines.append("}")
    lines.append("")


lines: list[str] = [
    "/*",
    " * 图标 mask —— **本文件由脚本生成，不要手改**。",
    " *",
    " * 生成：`python tools/build-icon-css.py`",
    " * 来源：`F:\\美术资源\\桌宠右键菜单 UI.pdf` 的矢量路径",
    " *       （`tools/extract-menu-icons.py` 抠成 SVG）",
    " *",
    " * ## ⚠️ 为什么内联成 data URI 而不是引 `.svg` 文件",
    " *",
    " * ① `mask-image` 引外部文件在某些 Chromium 版本下受同源/协议限制；",
    " *    data URI 没有这个问题。",
    " * ② 少 11 个 HTTP/文件请求。",
    " * ③ **单色**：`background-color: currentColor` 让图标跟着文字色变",
    " *    （开启蓝 / 关闭灰 / 聚焦再变）—— 这是抠图时必须用 `evenodd`",
    " *    单色化的原因，见 `extract-menu-icons.py` 的说明。",
    " */",
    "",
    "/* ⚠️ 这个文件由 `tools/build-icon-css.py` 生成 —— 手改会在下次生成时被覆盖 */",
    "",
]

missing: list[str] = []
for name, label in ORDER:
    if name in EXTRA_ICONS:
        emit(lines, name, label, EXTRA_ICONS[name])
        continue
    svg_path = ICONS / f"{name}.svg"
    if not svg_path.exists():
        missing.append(name)
        continue
    emit(lines, name, label, svg_path.read_text(encoding="utf-8"))

text = "\n".join(lines)
for out in OUTS:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"  ✅ -> {out}")
print(f"     写了 {len(ORDER) - len(missing)} 个图标，{len(text)} 字符")
if missing:
    print(f"  ⚠️ 缺：{missing}")

# 顺便报告每个图标的原始尺寸（菜单 CSS 里要用它决定 mask-size）
print()
print("  ═══ 各图标的 viewBox 尺寸 ═══")
import re  # noqa: E402

for name, _label in ORDER:
    svg_path = ICONS / f"{name}.svg"
    if not svg_path.exists():
        continue
    head = svg_path.read_text(encoding="utf-8").split("\n")[0]
    match = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', head)
    if match:
        print(f"    {name:<8} {match.group(1):>6} x {match.group(2)}")
