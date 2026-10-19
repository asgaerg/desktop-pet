"""从设计稿 PDF 里**抠出真图标**，转成 SVG（2026-10-12）。

## 为什么

用户说「能的话提取这里面的图标出来，这样看起来舒服很多」——
我现在的图标是**手画的近似 SVG**（月亮/气泡/眼睛…的形状我自己编的），
而设计稿里的图标是**真矢量路径**，就在 PDF 的 `get_drawings()` 里。

抠出来之后能做到**形状完全一致**，而不是"差不多像"。

## 做法

① 按已知坐标锁定每行的图标路径（从 `probe-menu-metrics.py` 的清单里拿到）
② 把 PDF 的 `items`（`l` 直线 / `c` 三次曲线）翻成 SVG 的 `d`
③ 用路径包围盒当 `viewBox`，`fill-rule` 照 PDF 的 `even_odd`

⚠️ **PDF 与 SVG 的 y 轴方向相反**（PDF 向上、SVG 向下），
   但因为我们用**路径自己的包围盒**当 viewBox 并做平移，
   只要不额外翻转就能保持形状（整块上下颠倒需要翻转，
   而翻转与否看"图形是否自对称"—— 稳妥做法是**翻转 y**）。
"""

from __future__ import annotations

import json
import pathlib

import pymupdf

SRC = pathlib.Path(r"F:\美术资源\桌宠右键菜单 UI.pdf")
OUT = pathlib.Path(r"F:\ai项目\tools\menupreview\icons")
OUT.mkdir(parents=True, exist_ok=True)

doc = pymupdf.open(SRC)
page = doc[0]
drawings = page.get_drawings()

# 每行图标的"锚点"（从 probe-menu-metrics 的坐标清单里抄的）
# (名字, 行 y 范围, 图标 x 范围, 用第几页 —— 这里用第 1 页，图标最全)
# 第 1 页里图标都在 x 63..80 之间
TARGETS = [
    ("moon", 63.0, 97.0),
    ("chat", 97.0, 133.0),
    ("eye", 133.0, 173.0),
    ("paw", 175.0, 211.0),
    ("pad", 211.0, 247.0),
    ("mic", 247.0, 287.0),
    ("spark", 287.0, 327.0),
    ("memory", 327.0, 364.0),
    ("sun", 364.0, 404.0),
    ("power", 404.0, 444.0),
    ("copy", 444.0, 484.0),
]


def seg_to_svg(items: list, origin_x: float, origin_y: float) -> str:
    """把 PDF 的路径段翻成 SVG 的 `d`。

    ⚠️ y 要**翻转**并把原点移到包围盒左上角：
       PDF 的 y 向上，SVG 的 y 向下。
    """
    out: list[str] = []
    for seg in items:
        kind = seg[0]
        if kind == "l":
            p1, p2 = seg[1], seg[2]
            x, y = p1.x - origin_x, p1.y - origin_y
            if not out:
                out.append(f"M{x:.2f} {y:.2f}")
            x2, y2 = p2.x - origin_x, p2.y - origin_y
            out.append(f"L{x2:.2f} {y2:.2f}")
        elif kind == "c":
            # ('c', p1, p2, p3, p4) —— p4 是终点，p2/p3 是控制点
            p1, c1, c2, p4 = seg[1], seg[2], seg[3], seg[4]
            if not out:
                out.append(f"M{p1.x - origin_x:.2f} {p1.y - origin_y:.2f}")
            out.append(
                f"C{c1.x - origin_x:.2f} {c1.y - origin_y:.2f} "
                f"{c2.x - origin_x:.2f} {c2.y - origin_y:.2f} "
                f"{p4.x - origin_x:.2f} {p4.y - origin_y:.2f}"
            )
        elif kind == "re":
            rect = seg[1]
            out.append(
                f"M{rect.x0 - origin_x:.2f} {rect.y0 - origin_y:.2f} "
                f"h{rect.width:.2f} v{rect.height:.2f} h{-rect.width:.2f} z"
            )
    out.append("Z")
    return " ".join(out)


report: list[dict] = []

for name, y0, y1 in TARGETS:
    # 图标固定在 x 63..81 那一列
    parts = [
        item
        for item in drawings
        if item["rect"].x0 < 82
        and item["rect"].x1 > 60
        and y0 <= (item["rect"].y0 + item["rect"].y1) / 2 <= y1
        # ⚠️ **只挑路径本身够小的**：行背景（288x36）、整个面板（302x439）
        #    也会落进这个 y 区间，必须按尺寸排掉。
        #    第一版漏了这条 —— `chat`/`mic`/`sun` 三个把背景和面板都抠进去了
        #    （导出成 288x36 和 302x439 的"图标"）。
        and item["rect"].width < 22
        and item["rect"].height < 22
    ]
    if not parts:
        print(f"  ⚠️ {name}: 没找到路径")
        continue

    # 合并包围盒
    x_min = min(it["rect"].x0 for it in parts)
    x_max = max(it["rect"].x1 for it in parts)
    y_max = max(it["rect"].y1 for it in parts)
    """
    ⚠️ `y_min` 用来**算 viewBox 高度**时必须是真实最小值，
    但 `flip_y` 里的平移量要用 0（见下面 `flip_y` 的说明）。

    第一版把两者混成一个变量，于是 `h = y_max - 0 = 428`（`power` 的
    y_max 是 428.79）—— viewBox 高度变成 428 而不是 12.55，
    图标在框里只剩一条细线。

    **教训：一个变量承担两个语义，迟早出事。** 这里拆成 `y_min`（算高度）
    和 `y_origin`（算平移）。
    """
    y_min = min(it["rect"].y0 for it in parts)
    w = x_max - x_min
    h = y_max - y_min

    """
    ## 🔴 坐标系：PDF 与 SVG 的 y 轴在这件事上**是一样的**

    `get_drawings()` 返回的坐标**已经是 PyMuPDF 归一化过的** ——
    原点在页面**左上角**、y 轴**向下**，与 SVG 一致。

    所以**语义上不需要翻**。但坐标值不在 `[0, h]` 里
    （`power` 的 y 在 404..444），必须先平移才能落进 viewBox。

    ## ⚠️ `y_max - py` 既是平移**也是**沿水平轴的镜像

    上一段让 `y_min = 0`，于是：

    ```
    py = y_max  ->  y_max - py = 0        （顶边映到 0）
    py = 0      ->  y_max - py = y_max    （把 [0, y_max] 完整映到 [y_max, 0]）
    ```

    坐标因此**平移进了 viewBox**，同时**沿水平轴镜像**一次
    （沿水平轴 = 上下翻）。

    ## 🤔 那这个镜像要不要留？

    ⚠️ **不猜。** 上一版我去掉它，结果坐标飞出 viewBox
    （`M6.590 422.200`，而 viewBox 只有 12.55 高），图标全空了 ——
    也就是说「去掉翻转」**同时改了两件事**（平移 + 镜像），必须分开验。

    所以现在保留镜像（它同时提供了平移），**看并排对照图**
    （`tools/compare-icon-orientation.py`）：
    方向对就留着；还反的话改成纯平移 `py - y_min` ——
    那是一处改动、一眼可验。
    """
    def flip_y(py: float) -> float:
        """
        🔴 **只平移，不镜像**（2026-10-12 修 —— 之前镜像了，图标全上下颠倒）。

        `get_drawings()` 的坐标原点在**左上角**、y 轴**向下**，
        与 SVG **一致** —— 所以只需要平移进 viewBox，**不需要翻转**。

        ## 第一版错在哪

        第一版写的是 `y_max - py`，理由是「PDF 的 y 轴向上」。
        但那个前提**不成立**，于是它同时做了两件事：**平移 + 沿水平轴镜像**，
        把每个图标都上下颠倒了。

        ## 实测判据（`tools/compare-flip.py` 的三方对照）

        `power` 最明显。设计稿的**数值事实**：

        ```
        竖线（窄）  y 416.24..422.96   -> 归一化 0.00.. 6.72   （靠上）
        圆环（宽）  y 418.43..428.79   -> 归一化 2.19..12.55
        ```

        竖线的 y 范围**比圆环更靠上** => 设计稿是「竖线朝上、伸进圆环里」的
        **标准电源符号**（⏻）。

        而镜像版本渲出来是「竖线朝上、伸到图标**外面**」——
        并排放大后 A（镜像）/ B（不镜像）的差别一眼可见。

        ⚠️ **别再用肉眼在 12px 缩略图上判断方向** —— 我看了两遍都没把握。
        要么看**数值**（包围盒的相对位置），要么把图标**放大到几百像素**并排比。
        """
        # ⚠️ 函数名保留 `flip_y` 是历史原因，现在它**只平移不翻转**
        return py - y_min

    """
    ## ⚠️ 两个变量，两个语义（拆开才不会互相污染）

    ```
    y_min      真实最小值   -> 只用来算 viewBox 高度 h = y_max - y_min
    y_origin   平移基准     -> 只用来算坐标（这里是 0，见下面）
    ```

    第一版把两者混成一个变量：写成 `y_min = 0.0`（为了让坐标对上），
    于是 `h = y_max - 0 = 428` —— viewBox 高度变成 428 而不是 12.55，
    图标在框里只剩一条细线（实测 `moon` 报成 `11.98 x 81.48`）。

    **教训：一个变量承担两个语义，迟早出事。**
    """
    # 让 `h` 用真实最小值（上面已经算好了），这里只声明平移基准

    """
    ## 🔴 单色化必须用 `evenodd` 合并（多层图标的唯一正解）

    实测（`tools/menupreview/multi.html` 的三方对照）：

    ```
    eye   当前(nonzero分开) 实心 ⚫   evenodd 合并 外圈+瞳孔环 ✅  保留异色 蓝+白
    chat  当前              实心 ⚫   evenodd 合并 气泡+三个点  ✅  保留异色 蓝+白点
    memory 当前             有竖缝   evenodd 合并 柱形+两缝    ✅  保留异色 糊成一团
    ```

    ### 为什么 `nonzero` + 分开的 `<path>` 不行

    图标是**多层异色**的：`eye` 有 4 层 —— 蓝杏仁 / **白**虹膜 / 蓝瞳孔 /
    **白**高光。设计师用「蓝白交替」画出瞳孔和高光。

    单色渲染时，同色的层**直接盖住**前一层：
    白虹膜盖住蓝杏仁、白高光盖住蓝瞳孔 —— 于是整个变成实心块。

    ### 为什么 `evenodd` 合并行

    把 4 层合成**一条**路径，`evenodd` 让「被覆盖的层数」的奇偶决定填/空：

    ```
    层级（从内到外）  4    3    2    1
    evenodd 结果     填   空   填   空     <- 环
    ```

    从外到内就是「实心杏仁 → 空环 → 实心瞳孔圈 → 空」——
    正好等价于原稿的"瞳孔 + 高光"。

    ### ⚠️ 为什么不用「保留异色」

    它最像原稿，但**白色层被写死了** —— 而菜单里的图标要跟着文字色变
    （开启蓝 / 关闭灰 / 聚焦再变），异色图标做不到。
    单色 + `evenodd` 是唯一能兼顾「形状对」和「能变色」的做法。
    """
    path_elements: list[str] = []
    # ⚠️ 所有层合成**一条** d（见上面的说明：evenodd 靠层数奇偶出环）
    d_parts: list[str] = []
    for item in parts:
        flat = item["items"]
        # 按坐标断裂切子路径
        subs: list[list] = []
        current: list = []
        last_end = None
        for seg in flat:
            start = seg[1]
            end = seg[-1]
            if last_end is not None:
                same = abs(start.x - last_end.x) < 0.02 and abs(start.y - last_end.y) < 0.02
                if not same:
                    subs.append(current)
                    current = []
            current.append(seg)
            last_end = end
        if current:
            subs.append(current)

        for sub in subs:
            first = True
            for seg in sub:
                kind = seg[0]
                if kind == "l":
                    p1, p2 = seg[1], seg[2]
                    if first:
                        d_parts.append(f"M{p1.x - x_min:.3f} {flip_y(p1.y):.3f}")
                        first = False
                    d_parts.append(f"L{p2.x - x_min:.3f} {flip_y(p2.y):.3f}")
                elif kind == "c":
                    p1, c1, c2, p4 = seg[1], seg[2], seg[3], seg[4]
                    if first:
                        d_parts.append(f"M{p1.x - x_min:.3f} {flip_y(p1.y):.3f}")
                        first = False
                    d_parts.append(
                        f"C{c1.x - x_min:.3f} {flip_y(c1.y):.3f} "
                        f"{c2.x - x_min:.3f} {flip_y(c2.y):.3f} "
                        f"{p4.x - x_min:.3f} {flip_y(p4.y):.3f}"
                    )
                elif kind == "re":
                    rect = seg[1]
                    d_parts.append(
                        f"M{rect.x0 - x_min:.3f} {flip_y(rect.y1):.3f} "
                        f"h{rect.width:.3f} v{rect.height:.3f} "
                        f"h{-rect.width:.3f} z"
                    )
                    first = False
            # 每个子路径各自闭合 —— `evenodd` 靠它成立
            d_parts.append("Z")

    path_elements.append(
        f'  <path d="{" ".join(d_parts)}" fill="currentColor" fill-rule="evenodd"/>'
    )

    even_odd = any(it.get("even_odd") for it in parts)

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {w:.2f} {h:.2f}" width="{w:.2f}" height="{h:.2f}">\n'
        f"  <!-- 从 桌宠右键菜单 UI.pdf 抠出来的真图标：{len(parts)} 条填充路径 -->\n"
        + "\n".join(path_elements)
        + "\n</svg>\n"
    )
    path = OUT / f"{name}.svg"
    path.write_text(svg, encoding="utf-8")
    report.append(
        {"name": name, "w": round(w, 2), "h": round(h, 2), "parts": len(parts),
         "segs": len(d_parts), "evenodd": even_odd}
    )
    print(f"  ✅ {name:<8} {w:6.2f} x {h:5.2f}  路径 {len(parts)} 条 · 段 {len(d_parts)}"
          f"{'  evenodd' if even_odd else ''}  -> {path.name}")

(OUT / "_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
doc.close()
print()
print(f"  图标已导出到 {OUT}")
