"""
把这套帧导入成宠物立绘。

输入：一批尺寸一致的 PNG（每帧整幅画面，近白渐变背景 + 左上角水印）。
输出：`sheet.png`（单行序列帧）+ `avatar.json`，直接可被 sprite.ts 使用。

处理步骤：
  1. **先削掉水印区**：左上角那块灰黑文字会污染"人物在哪"的检测，
     也会在缩放后糊成一小块脏东西。宁可切掉一角（那里本来也没有人物）。
  2. **逐帧检测人物范围**，再取**所有帧的并集** —— 用并集才能保证动画过程中
     人物不会被裁到（单帧检测会让不同帧的裁切框不一样，人物看起来会抖）。
  3. 抠掉近白背景（洪泛 + 饱和度判据），得到透明人物。
  4. 等比缩放进统一帧、**底部对齐**，保证脚不动。
  5. 按行优先排成网格贴图（单行 59 帧会宽到 87000px，浏览器/GPU 都吃不住）。

用法：
    python import-frames.py 帧目录 -o 输出目录 [--frame 256] [--cols 10]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import deque

from PIL import Image

# 默认削掉的水印区（源图坐标）。写死是因为它在这套素材里位置固定；
# 换素材时用 --watermark 覆盖，或传 `--watermark 0,0,0,0` 关掉。
DEFAULT_WATERMARK = (0, 0, 268, 86)


def strip_watermark(image: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    """
    把水印区域涂成背景色（而不是透明）。

    为什么涂成背景色而不是透明：水印就在背景上，涂成透明等于在画面左上角
    留一个透明洞；而它周围是背景色，填成同色看起来是连续的，后续抠底会一并
    处理掉。取该区域外的背景色做填充，比硬编码白色更稳（这套素材背景是渐变）。
    """
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return image
    canvas = image.copy()
    # 取水印右侧一点的位置作为背景色参考
    ref_x = min(image.width - 1, x1 + 10)
    ref_y = min(image.height - 1, (y0 + y1) // 2)
    fill = canvas.getpixel((ref_x, ref_y))
    region = Image.new("RGBA", (x1 - x0, y1 - y0), fill)
    canvas.paste(region, (x0, y0))
    return canvas


def character_box(image: Image.Image):
    """
    估人物范围。优先用 alpha（若上游已抠过底），否则按"非背景"判定。

    返回 (x0, y0, x1, y1) 或 None。
    """
    import numpy as np

    a = np.array(image.convert("RGBA"))
    alpha = a[..., 3]
    if (alpha < 250).any():
        # 已经有透明区域，直接用它
        mask = alpha > 16
    else:
        # 未抠底：按"与边缘色不同"粗略判定
        rgb = a[..., :3].astype(int)
        sat = rgb.max(axis=2) - rgb.min(axis=2)
        lum = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
        mask = (sat > 40) | (lum < 195)

    ys, xs = np.where(mask)
    if not len(xs):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def remove_background_by_color(
    image: Image.Image,
    background: tuple[int, int, int],
    tolerance: int = 14,
    soft_edge: int = 10,
    erode_px: int = 1,
    fill_holes: bool = True,
    hole_max_area: int = 0,
    close_gap_radius: int = 0,
) -> Image.Image:
    """
    纯色（绿幕）抠图。

    ...（算法说明见下）

    ---- close_gap_radius：封住描边细缝，别让洪泛漏进角色内部 ----

    实测踩过的坑：发丝与尾巴交界的描边很细，扫描/抗锯齿会让它在掩码上断成
    一两像素的缺口。洪泛顺着缺口漫进角色内部，把那里的头发也抠成透明 ——
    表现是**轮廓内部出现透明破洞**，深色壁纸透进来，看着像残留的浅色缝隙。

    对背景掩码做闭运算（先膨胀 R 像素封缝、再腐蚀 R 像素还原尺寸）即可解决。
    R=2 能封住两像素以内的缝，而对大块背景没有影响。
    """
    import numpy as np

    rgba = image.convert("RGBA")
    width, height = rgba.size
    rgb = np.array(rgba)

    diff = np.abs(rgb[..., :3].astype(int) - np.array(background, dtype=int)).max(axis=2)
    is_bg_raw = diff <= tolerance

    # ---- 关键一步：把"背景掩码"做闭运算，再拿它洪泛 ----
    #
    # 为什么必须这样，以及我一开始为什么搞错了：
    #
    #   角色的描边在发丝交界、尾巴根部等处只有一两像素宽，扫描/抗锯齿会让它
    #   在掩码上断成一两个像素的缺口。洪泛顺着缺口漫进角色内部，把那里的
    #   头发也抠成透明 —— 表现是**轮廓内部出现透明破洞**，深色壁纸透进来，
    #   看着像残留的浅色缝隙。（用户实际报过，88 帧**每帧都有** 290~400 个这样的像素。）
    #
    #   我先前的错误做法是"把闭运算结果当**屏障**"（只在更小的区域里洪泛）：
    #   那等于让洪泛更容易穿透，方向反了。正确做法是**用闭运算的结果当洪泛的域**——
    #   闭运算把缺口两侧的背景连成一片，于是洪泛能"跨过"缺口，而缺口本身
    #   因为被填实，不再被当作背景。
    #
    # 验证过的受控实验：深色圆盘 + 一条 3px 宽的绿色通道。
    #   close_gap=0 时洪泛穿过通道，把圆盘中心抠成透明；
    #   close_gap=2 时闭运算把通道两侧连上，中心保住。
    if close_gap_radius > 0:
        from PIL import ImageFilter

        mask_img = Image.fromarray((is_bg_raw * 255).astype(np.uint8), "L")
        size = close_gap_radius * 2 + 1
        # 闭运算 = 先膨胀后腐蚀：填掉细缝，尺寸不变
        closed = mask_img.filter(ImageFilter.MaxFilter(size)).filter(ImageFilter.MinFilter(size))
        is_bg_closed = np.array(closed) > 127
    else:
        is_bg_closed = is_bg_raw

    reachable = _flood_from_edges(is_bg_closed, width, height)

    # 1b) 补上被轮廓围住的封闭背景块（例如呆毛弯钩与头顶之间的那块）。
    #     不做的话它会保留原色，看起来像一坨背景色斑块。
    if fill_holes:
        # 填封闭洞时用**原始**背景判据：闭运算后的掩码已经把细缝填实，
        # 拿它去判"哪些封闭区算背景"会把细缝也当成背景色区域。
        reachable = fill_enclosed_holes(reachable, is_bg_raw, max_area=hole_max_area)

    # 2) 软 alpha：只在与背景连通的区域里按距离渐变
    outer = tolerance + max(0, soft_edge)
    if outer > tolerance:
        ramp = (diff - tolerance) / float(outer - tolerance)
        ramp = np.clip(ramp, 0.0, 1.0)
        soft_alpha = (ramp * 255).astype(np.uint8)
    else:
        soft_alpha = np.where(diff <= tolerance, 0, 255).astype(np.uint8)

    alpha = np.where(reachable, soft_alpha, 255).astype(np.uint8)

    # 3) 往内收 erode_px 像素，削掉最外层带背景色的那一圈
    if erode_px > 0:
        from PIL import ImageFilter

        a_img = Image.fromarray(alpha, "L")
        a_img = a_img.filter(ImageFilter.MinFilter(erode_px * 2 + 1))
        alpha = np.array(a_img)

    # 4) 已被判为背景的地方强制全透明（MinFilter 可能把 0 抬起来）
    alpha = np.where(reachable & (diff <= tolerance), 0, alpha)
    alpha = np.where(rgb[..., 3] < 8, 0, alpha)

    result = Image.fromarray(
        np.dstack([rgb[..., 0], rgb[..., 1], rgb[..., 2], alpha]), "RGBA"
    )
    from PIL import ImageFilter

    r, g, b, a = result.split()
    return Image.merge("RGBA", (r, g, b, a.filter(ImageFilter.GaussianBlur(0.4))))


def detect_background_color(image: Image.Image, sample: int = 6) -> tuple[int, int, int]:
    """
    从图像四边推断背景主色。

    取边缘像素的**中位数**而不是均值：边缘上可能压着角色的头发或水印，
    均值会被它们拉偏，中位数对少量离群值稳健。
    """
    import numpy as np

    rgb = np.array(image.convert("RGB")).astype(int)
    edge = np.concatenate([
        rgb[0:sample, :].reshape(-1, 3),
        rgb[-sample:, :].reshape(-1, 3),
        rgb[:, 0:sample].reshape(-1, 3),
        rgb[:, -sample:].reshape(-1, 3),
    ])
    return tuple(int(v) for v in np.median(edge, axis=0))


def remove_background(
    image: Image.Image,
    sat_limit: int = 40,
    bright_min: int = 195,
    close_radius: int = 0,
    hole_sat: int = 0,
    hole_lum: int = 0,
) -> Image.Image:
    """
    近白背景抠图（适用于"白底 + 浅色角色"的素材）。

    ⚠️ 这套算法很脆弱，只在前一套素材上验证过。**如果素材是纯色绿幕，
    用 `remove_background_by_color`** —— 那条路简单可靠得多。

    难点（实测数据）：背景 lum≈243/sat≈0，而蕾丝发带 lum≈217/sat≈10、
    蕾丝高光 lum≈246/sat≈3、头发光泽 lum≈225/sat≈25。
    **背景与蕾丝在颜色上不可分**（试过 10 组阈值，蕾丝误判率始终 ≥25%）。

    所以只能靠几何。而最反直觉的一点是：

      **被描边围住的白色区域，洪泛到不了，于是被保留 —— 大多数情况下这是对的。**

    因为蕾丝的白色凹陷、头发的浅色光泽全都是"封闭区域"。按面积把这些封闭块
    一起抠掉会误伤（实测：面积 5542 的块是**头发光泽**、3083 的才是背景）。

    也**不要**对背景掩码做闭运算封缝：蕾丝与背景在掩码上本来就连成一片，
    `close_radius=1` 就会把蕾丝侵蚀掉大半，`=2` 全没。（试过。）

    ---- 关于 hole_sat / hole_lum（默认关闭）----

    这套素材有个**无法两全**的问题：背景是白的，而蕾丝和头发高光也是白的。
    头发绺之间被围住的背景（表现为发丝缝隙里的白块）从边缘到不了，于是留着。

    打开这两个参数（例如 hole_sat=20, hole_lum=235）会把"非常白且被围住"的
    区域也抠掉，能消掉大部分发缝白块。代价是**蕾丝发带会被打碎成噪点**，
    因为它内部的灰白细节同样落在判据里。

    实测三档的取舍（可见的近白像素占比 / 蕾丝状态）：

        不填                4.21%   蕾丝完好
        sat<=20 lum>=235    0.79%   蕾丝开始有碎点
        sat<=25 lum>=230    0.00%   蕾丝被打碎成噪点
        sat<=15 lum>=240    2.36%   蕾丝完好，但只抠掉一半白块

    **结论：白底素材上没有好答案。** 需要发缝透明的话，应该改用绿幕素材
    （`remove_background_by_color`）—— 绿与白在颜色上可分，不靠几何硬凑。
    """
    import numpy as np

    rgba = image.convert("RGBA")
    width, height = rgba.size
    rgb = np.array(rgba)

    high = rgb[..., :3].max(axis=2).astype(int)
    low = rgb[..., :3].min(axis=2).astype(int)
    looks_bg = ((high - low) <= sat_limit) & (high >= bright_min)

    if close_radius > 0:
        from PIL import ImageFilter

        mask_img = Image.fromarray((looks_bg * 255).astype(np.uint8), "L")
        size = close_radius * 2 + 1
        closed = mask_img.filter(ImageFilter.MaxFilter(size)).filter(ImageFilter.MinFilter(size))
        looks_bg = np.array(closed) > 127

    reachable = _flood_from_edges(looks_bg, width, height)

    if hole_lum > 0:
        hole_bg = ((high - low) <= hole_sat) & (high >= hole_lum)
        reachable = fill_enclosed_holes(reachable, hole_bg)

    out_alpha = np.where(reachable, 0, 255).astype(np.uint8)
    out_alpha = np.where(rgb[..., 3] < 8, 0, out_alpha)

    result = Image.fromarray(
        np.dstack([rgb[..., 0], rgb[..., 1], rgb[..., 2], out_alpha]), "RGBA"
    )
    from PIL import ImageFilter

    r, g, b, a = result.split()
    return Image.merge("RGBA", (r, g, b, a.filter(ImageFilter.GaussianBlur(0.5))))


def _flood_from_edges(mask, width: int, height: int):
    """
    从画面四边洪泛 mask 中为 True 的像素，返回可达的布尔数组。

    用 numpy 的邻居扩散（每轮整体推进），而不是逐像素 Python 循环 ——
    1280x720 的图逐像素循环要几秒，这里几十毫秒。
    """
    import numpy as np

    reachable = np.zeros((height, width), dtype=bool)
    # 种子：四条边上的 mask 像素
    reachable[0, :] = mask[0, :]
    reachable[-1, :] = mask[-1, :]
    reachable[:, 0] |= mask[:, 0]
    reachable[:, -1] |= mask[:, -1]

    while True:
        grown = reachable.copy()
        grown[1:, :] |= reachable[:-1, :]
        grown[:-1, :] |= reachable[1:, :]
        grown[:, 1:] |= reachable[:, :-1]
        grown[:, :-1] |= reachable[:, 1:]
        grown &= mask
        if np.array_equal(grown, reachable):
            return reachable
        reachable = grown


def fill_enclosed_holes(reachable, looks_bg, max_area: int = 0):
    """
    抠掉"被描边完全围住、洪泛到不了"的背景块。

    **为什么必须做这一步**：洪泛只能处理与画面边缘连通的背景。被角色轮廓
    完全围住的背景区（典型例子：头顶那根呆毛弯成一个钩，钩与头顶之间围出的
    那块背景）从边缘到不了，于是**保留原色**留在画面上 —— 表现是一坨背景色的
    色块，看起来像呆毛断了或者多了个绿斑。

    **实现方式**：对"从边缘到不了的背景"反复做膨胀，直到不再变化。
    `looks_bg` 是屏障，所以膨胀只会在该区域内蔓延，把它填满。

    `max_area` 大于 0 时**只填面积不超过它的块**。这是给"想清掉发缝里的小碎块、
    但保住大块蕾丝"的场合用的：

        蕾丝发带是一个**大而连通**的封闭区（几百到几千像素）；
        头发绺之间、尾巴周围的小缝隙通常是**小碎块**（几十到几百像素）。

    所以按面积筛能分开这两类。传 0 = 全填（靠 `looks_bg` 的严格程度来控制风险）。

    这条正是前一套近白素材**不能照搬**的原因 —— 那套的蕾丝凹陷和头发
    光泽全是"背景色的封闭区域"，填了会把蕾丝抠成空洞。所以两套算法分开。
    """
    import numpy as np

    enclosed = looks_bg & ~reachable
    if not enclosed.any():
        return reachable

    if max_area <= 0:
        # 全填：迭代膨胀直到不再变化
        grown = enclosed.copy()
        while True:
            nxt = grown.copy()
            nxt[1:, :] |= grown[:-1, :]
            nxt[:-1, :] |= grown[1:, :]
            nxt[:, 1:] |= grown[:, :-1]
            nxt[:, :-1] |= grown[:, 1:]
            nxt &= enclosed
            if np.array_equal(nxt, grown):
                break
            grown = nxt
        return reachable | grown

    # 按块筛：逐块收集，只并回面积达标的小块。
    # 用"从种子迭代膨胀"逐块提取（没有 scipy 可用，index 标签要自己写）。
    height, width = enclosed.shape
    remaining = enclosed.copy()
    filled = np.zeros_like(enclosed)
    while remaining.any():
        seed_y, seed_x = np.argwhere(remaining)[0]
        grown = np.zeros_like(enclosed)
        grown[seed_y, seed_x] = True
        while True:
            nxt = grown.copy()
            nxt[1:, :] |= grown[:-1, :]
            nxt[:-1, :] |= grown[1:, :]
            nxt[:, 1:] |= grown[:, :-1]
            nxt[:, :-1] |= grown[:, 1:]
            nxt &= enclosed
            if np.array_equal(nxt, grown):
                break
            grown = nxt
        remaining &= ~grown
        if int(grown.sum()) <= max_area:
            filled |= grown
    return reachable | filled


def main() -> int:
    parser = argparse.ArgumentParser(description="把一批帧导入成宠物立绘")
    parser.add_argument("source", help="帧目录（内含按名字排序的 PNG）")
    parser.add_argument("-o", "--out", required=True, help="输出目录")
    parser.add_argument("--frame", type=int, default=256, help="每帧边长，默认 256")
    parser.add_argument(
        "--cols",
        type=int,
        default=10,
        help="贴图每行排几帧。单行 59 帧会宽到 87000px，浏览器与 GPU 都吃不住",
    )
    parser.add_argument(
        "--watermark",
        default=",".join(str(v) for v in DEFAULT_WATERMARK),
        help='要削掉的水印区 "x0,y0,x1,y1"；传 0,0,0,0 关闭',
    )
    parser.add_argument("--margin", type=int, default=6, help="人物四周留白")
    parser.add_argument(
        "--crop-mode",
        choices=("stable", "union"),
        default="stable",
        help=(
            "裁切框怎么定。stable（默认）= 取出现次数最多的那个框，忽略少数异常帧；"
            "union = 所有帧的并集。别轻易用 union：只要有一帧手臂甩出去，"
            "并集就会被撑到整幅宽，人物被缩小到只占帧的四成。"
        ),
    )
    parser.add_argument("--image-name", default="sheet.png")
    parser.add_argument("--name", default="avatar")
    parser.add_argument("--fps", type=float, default=15.0, help="源动画约 15fps（59 帧 / 3.93 秒）")
    parser.add_argument(
        "--blink-range",
        default="0,0",
        help=(
            '哪一段帧构成一次独立眨眼 "起始,结束"（含起始、不含结束）。'
            "**默认 0,0 = 不输出独立眨眼段**，因为素材的待机循环里通常已经含一次闭眼，"
            "再单独声明会与循环叠加、显得眨个不停。"
            "只有当素材另有一套眨眼帧时才需要填，并配合 --emit-blink。"
        ),
    )
    parser.add_argument(
        "--emit-blink",
        action="store_true",
        help="真的把 blink 段写进清单（需要同时给出 --blink-range）",
    )
    parser.add_argument(
        "--blink-fps",
        default=14.0,
        help=(
            "眨眼段播放速度。9 帧 @14fps ≈ 0.64 秒。"
            "别调太低：早先用整段 13→34（21 帧）@18fps 是 1.17 秒，"
            "闭眼持续一秒多看起来像卡住，而不是眨眼。"
        ),
    )
    parser.add_argument("--pattern", default="frame_*.png", help="帧文件名通配（默认 frame_*.png）")
    parser.add_argument(
        "--bg-mode",
        choices=("auto", "color", "nearwhite", "none"),
        default="auto",
        help=(
            "怎么抠背景。auto（默认）= 采样四边判断是纯色幕布还是近白底；"
            "color = 纯色幕布（绿幕等）；nearwhite = 近白底（前一套素材用过的脆弱算法）；"
            "none = 不抠底（素材自带 alpha）。"
        ),
    )
    parser.add_argument(
        "--bg-color",
        default="",
        help='手动指定背景色 "R,G,B"。留空则自动从四边取中位数。',
    )
    parser.add_argument(
        "--bg-tolerance",
        type=int,
        default=14,
        help="纯色幕布的容差（最大通道差）。实测背景抖动 ±4，取 14 有余量",
    )
    parser.add_argument(
        "--soft-edge",
        type=int,
        default=10,
        help=(
            "软边宽度（颜色距离的过渡带）。**别设成 0**：抗锯齿的边缘像素是"
            "角色色+背景色的混合，二值判定会把它们整块保留，在深色桌面上显出"
            "一圈背景色描边（绿幕素材上是明显的绿晕）。"
        ),
    )
    parser.add_argument(
        "--erode",
        type=int,
        default=1,
        help="把 alpha 往内收几个像素，削掉最外层仍带背景色的那一圈",
    )
    parser.add_argument(
        "--close-gap",
        type=int,
        default=2,
        help=(
            "封住描边上的细缝（对背景掩码做闭运算的半径），防止洪泛漏进角色内部。"
            "**别设 0**：发丝与尾巴交界的描边只有一两像素宽，掩码上会断，"
            "洪泛顺着缺口漫进去，把角色内部的头发也抠成透明 ——"
            "表现是轮廓内部出现透明破洞，深色壁纸透进来像残留的浅色缝隙。"
        ),
    )
    parser.add_argument(
        "--hole-max-area",
        type=int,
        default=0,
        help=(
            "只填补面积不超过此值的封闭块（0 = 全填）。"
            "蕾丝发带是**大而连通**的封闭区（几百~几千像素），"
            "头发绺之间的小缝隙通常是**小碎块**（几十~几百像素），"
            "所以按面积筛能把它们分开：设 200 左右可清掉发缝碎块而保住蕾丝。"
        ),
    )
    parser.add_argument(
        "--hole-lum",
        type=int,
        default=0,
        help=(
            "近白底素材专用：把'非常白且被轮廓围住'的区域也抠掉（消掉发丝缝隙里的白块）。"
            "0 = 关闭（默认，保住蕾丝）。设 235 左右会明显减少白块，"
            "**但蕾丝发带会被打碎成噪点** —— 白底素材上无法两全，详见 remove_background 的注释。"
        ),
    )
    parser.add_argument(
        "--hole-sat",
        type=int,
        default=20,
        help="配合 --hole-lum 使用：封闭区的饱和度上限",
    )
    parser.add_argument("--preview", action="store_true", help="另外输出接触印相（缩略图总览）")
    args = parser.parse_args()

    # 默认只取 frame_*.png。
    #
    # 为什么不取目录里的全部 PNG：这个目录里通常还放着分析产物（预览图、对比条）。
    # 早先用 `*.png` 时，preview.png 被当成一帧混了进去 —— 帧数从 59 变成 62，
    # 而且它的尺寸与帧不同，直接把"众数裁切框"算歪成 381x136，人物被压扁。
    # 导入素材时**必须**用严格的文件名模式，别让它去猜。
    files = sorted(glob.glob(os.path.join(args.source, args.pattern)))
    if not files:
        print(f"目录里没有匹配 {args.pattern} 的文件：{args.source}", file=sys.stderr)
        return 1

    wm = tuple(int(v) for v in args.watermark.split(","))
    if len(wm) != 4:
        print("--watermark 需要四个数字", file=sys.stderr)
        return 1

    print(f"共 {len(files)} 帧，输出帧尺寸 {args.frame}，贴图每行 {args.cols} 帧")
    print(f"水印区（将涂掉）：{wm if wm[2] > wm[0] else '不处理'}")

    # ---- 决定抠底方式与背景色 ----
    probe = strip_watermark(Image.open(files[0]).convert("RGBA"), wm)
    bg_mode = args.bg_mode
    bg_color = tuple(int(v) for v in args.bg_color.split(",")) if args.bg_color else None

    if bg_mode == "auto":
        detected = detect_background_color(probe)
        r, g, b = detected
        # 纯色幕布的特征是"颜色明显偏离中性灰"（绿幕/蓝幕饱和度高）
        if max(detected) - min(detected) > 30:
            bg_mode = "color"
            print(f"自动判断：纯色幕布，背景色 RGB{detected}")
        else:
            bg_mode = "nearwhite"
            print(f"自动判断：近白底，背景色 RGB{detected}（用近白算法）")
    elif bg_mode == "color":
        detected = bg_color or detect_background_color(probe)
        print(f"纯色幕布抠底，背景色 RGB{detected}，容差 {args.bg_tolerance}")
    else:
        detected = bg_color
        print(f"抠底方式：{bg_mode}")

    def cut_background(path: str) -> Image.Image:
        """读一帧 -> 涂掉水印 -> 抠底。所有后续步骤都在它之上。"""
        image = strip_watermark(Image.open(path).convert("RGBA"), wm)
        if bg_mode == "color":
            return remove_background_by_color(
                image,
                detected,  # type: ignore[arg-type]
                tolerance=args.bg_tolerance,
                soft_edge=args.soft_edge,
                erode_px=args.erode,
                close_gap_radius=args.close_gap,
            )
        if bg_mode == "nearwhite":
            return remove_background(
                image, hole_sat=args.hole_sat, hole_lum=args.hole_lum
            )
        return image

    # 先扫一遍所有帧的人物范围，再决定统一裁切框
    boxes = []
    for path in files:
        box = character_box(cut_background(path))
        if box is not None:
            boxes.append(box)
    if not boxes:
        print("所有帧都没检测到人物（抠底把人物也抠没了？检查 --bg-tolerance）", file=sys.stderr)
        return 1

    if args.crop_mode == "union":
        union = (
            min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes),
        )
    else:
        # stable：取"出现次数最多的那个框"。
        #
        # 为什么不用并集：这套素材里 47 帧几乎静止、14 帧手臂甩出去一次。
        # 并集会被那 14 帧撑成 906px 宽（整幅的 62%），于是统一缩放比只有 0.27，
        # 人物缩到只占帧宽的 40%、还偏在左边。用众数框则拿到稳定姿态的真实尺寸，
        # 缩放比能到 0.55 —— 人物大小差一倍。
        from collections import Counter

        # 量化到 4px 一格再数众数，避免亚像素抖动把本该相同的框拆成很多个
        quantized = Counter(
            (round(b[0] / 4), round(b[1] / 4), round(b[2] / 4), round(b[3] / 4)) for b in boxes
        )
        best, count = quantized.most_common(1)[0]
        union = tuple(v * 4 for v in best)
        print(
            f"裁切框取了出现最多的那个（{count}/{len(boxes)} 帧；"
            f"其余帧的肢体可能越出一点，属正常）"
        )

    pad = 4
    first_size = Image.open(files[0]).size
    crop = (
        max(0, union[0] - pad),
        max(0, union[1] - pad),
        min(first_size[0], union[2] + pad + 1),
        min(first_size[1], union[3] + pad + 1),
    )
    print(f"统一裁切 {crop}（{crop[2]-crop[0]} x {crop[3]-crop[1]}）")

    # 统一缩放比：所有帧用**同一个**缩放，否则人物会一帧大一点一帧小一点
    limit = args.frame - args.margin * 2
    scale = min(limit / (crop[2] - crop[0]), limit / (crop[3] - crop[1]))
    print(f"统一缩放比 {scale:.4f}")

    frames = []
    for index, path in enumerate(files):
        image = cut_background(path)
        piece = image.crop(crop)
        size = (max(1, round(piece.width * scale)), max(1, round(piece.height * scale)))
        piece = piece.resize(size, Image.LANCZOS)
        canvas = Image.new("RGBA", (args.frame, args.frame), (0, 0, 0, 0))
        # 底部对齐：脚不动，动画不会上下漂
        canvas.paste(piece, ((args.frame - size[0]) // 2, args.frame - args.margin - size[1]), piece)
        frames.append(canvas)

    print(f"已处理 {len(frames)} 帧")

    blink_parts = [int(v) for v in args.blink_range.split(",")]
    blink_from, blink_to = (blink_parts + [0, 0])[:2] if len(blink_parts) >= 2 else (0, 0)
    if blink_to <= blink_from:
        blink_from, blink_to = 0, 0
        print("未指定眨眼段（--blink-range 0,0）")
    else:
        print(f"眨眼段：帧 {blink_from} → {blink_to - 1}（{blink_to - blink_from} 帧）")

    total = len(frames)
    rows = (total + args.cols - 1) // args.cols
    sheet = Image.new("RGBA", (args.cols * args.frame, rows * args.frame), (0, 0, 0, 0))
    for i, frame in enumerate(frames):
        sheet.paste(frame, ((i % args.cols) * args.frame, (i // args.cols) * args.frame), frame)

    os.makedirs(args.out, exist_ok=True)
    sheet_path = os.path.join(args.out, args.image_name)
    # 调色板量化：256 色对这类扁平插画几乎无损，体积小一个数量级
    sheet.quantize(colors=256, method=Image.FASTOCTREE).save(sheet_path, optimize=True)
    print(f"贴图 -> {sheet_path}  {sheet.size}  {os.path.getsize(sheet_path)/1024:.0f} KB（{rows} 行 x {args.cols} 列）")

    animations: dict[str, dict] = {
        # 待机：源动画本身就是一段连续待机（里面通常已经包含一次闭眼）
        "idle": {"frames": list(range(total)), "fps": args.fps, "loop": True},
    }
    # 独立的 blink 段：**默认不输出**。
    #
    # 理由：这类素材的待机循环本来就含一段闭眼（实测这套是帧 32-43），
    # 再额外声明一个 blink 段，播放器就会"循环自己眨 + 计时器再眨"两套叠加，
    # 看起来眨个不停。（用户报过这个现象。）
    #
    # 只有素材**单独有一套眨眼帧**（不在 idle 循环里）时才应该打开 --emit-blink，
    # 否则会重复。
    if args.emit_blink and blink_to > blink_from:
        blink_frames = list(range(blink_from, min(blink_to, total)))
        if blink_frames:
            animations["blink"] = {
                "frames": blink_frames,
                "fps": args.blink_fps,
                "loop": False,
            }

    manifest = {
        "$comment": (
            "由 tools/import-frames.py 从一批 PNG 帧导入。"
            "只声明真正有素材的动画；其余状态由播放器回退到 idle"
            "（见 sprite.ts 的 resolveSpec）。"
            "**不要**把 idle 的帧号硬套给 talk/drag 等状态 —— 那会让拖动时也在呼吸，"
            "看起来像坏了。要做真正的差异化动作，需要另外的素材。"
        ),
        "image": args.image_name,
        "frameWidth": args.frame,
        "frameHeight": args.frame,
        "fps": args.fps,
        "palette": {},
        "animations": animations,
        "grid": {"columns": args.cols, "rows": rows, "count": total},
    }
    manifest_path = os.path.join(args.out, f"{args.name}.json")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    print(f"清单 -> {manifest_path}")

    if args.preview:
        cell = min(160, args.frame)
        cols = min(10, total)
        rows_p = (total + cols - 1) // cols
        preview = Image.new("RGBA", (cols * cell, rows_p * cell), (26, 26, 34, 255))
        for i, frame in enumerate(frames):
            thumb = frame.copy()
            thumb.thumbnail((cell, cell), Image.LANCZOS)
            preview.paste(
                thumb,
                ((i % cols) * cell + (cell - thumb.width) // 2,
                 (i // cols) * cell + (cell - thumb.height) // 2),
                thumb,
            )
        preview_path = os.path.join(args.out, f"{args.name}-preview.png")
        preview.save(preview_path)
        print(f"预览 -> {preview_path}  {preview.size}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
