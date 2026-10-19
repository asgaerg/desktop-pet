# -*- coding: utf-8 -*-
"""从 tray.ico 重新生成一份**规范格式**的 pet.ico。

问题（2026-10-14）：tray.ico 的**全部 7 个尺寸都是 PNG 压缩**。ICO 规范里
只有 256x256 那层允许 PNG，小尺寸必须是 BMP —— 资源管理器对这种全 PNG 的
ico 经常渲染失败，表现为桌面快捷方式图标空白（用户报的就是这个）。

用 Pillow 重存：小尺寸走 BMP，256 走 PNG。输出 pet.ico（不动 tray.ico，
托盘那边 Chromium 自己能读全 PNG 格式，别把它搞坏）。
"""
from PIL import Image
import pathlib

SRC = pathlib.Path(r"F:/ai项目/apps/desktop/assets/tray.ico")
DST = pathlib.Path(r"F:/ai项目/apps/desktop/assets/pet.ico")

img = Image.open(SRC)
print("源:", img.size, img.mode)
img.save(
    DST,
    format="ICO",
    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    # ⚠️ Pillow 12 默认**全部层都写 PNG**（和 tray.ico 同病）——必须显式要 BMP
    bitmap_format="bmp",
)
print("写出:", DST, DST.stat().st_size, "bytes")

# 复核：逐层打印格式头（PNG 是 89504e47，BMP 的 ICO 层是info头 28000000 之类）
import struct

d = DST.read_bytes()
_, _, count = struct.unpack("<HHH", d[:6])
print("图层数:", count)
off = 6
for i in range(count):
    w, h, _, _, _, bpp, size, offset = struct.unpack("<BBBBHHII", d[off : off + 16])
    head = d[offset : offset + 4]
    kind = "PNG" if head == b"\x89PNG" else "BMP"
    print(f"  层{i}: {(w or 256)}x{(h or 256)} bpp={bpp} 格式={kind}")
    off += 16
