# -*- coding: utf-8 -*-
"""给渲染层样式接上「界面主题」（浅色/深色）—— 2026-10-14。

## 做法

1. **字面量 → 角色变量**：把 `background: #f3f5f9` 这类写死的颜色换成
   `background: var(--hover)`，只在 `:root` / `html[data-theme]` 块**之外**替换
   （否则会把变量定义自己也换掉，变成自引用）。
   替换是**按属性分类**的：`color` 用文字色表、`background*` 用面色表、
   `border*` 用边框色表 —— 同一个 `#ffffff` 在不同属性上语义不同
   （蓝按钮上的白字 vs 白卡片），分不开就会把白字换成卡片色。

2. **注入角色变量**：写进各文件**已有的** `:root` 里（不新建第二个 `:root`，
   那会让门禁的"顶层重复声明"报警，也会让两份定义谁生效变得难猜）。

3. **追加深色覆盖**：文件末尾加一个 `html[data-theme='dark']` 块。
   主进程把属性挂到 `<html>` 上（见 `index.ts` 的 `themeScript`），
   这里只认这一个属性，渲染层不需要任何 JS。
"""
from __future__ import annotations

import pathlib
import re

BASE = pathlib.Path(r"F:\ai项目\apps\desktop\src\renderer")

# 角色 → (浅色, 深色)
ROLES: dict[str, tuple[str, str]] = {
    "--bg": ("#f5f6f8", "#1c1d24"),
    "--bg-soft": ("#fafbfc", "#17181e"),
    "--card": ("#ffffff", "#24262f"),
    "--card-soft": ("#fbfcfd", "#2a2d38"),
    "--hover": ("#f3f5f9", "#2a2d38"),
    "--sunken": ("#eef0f4", "#16171d"),
    "--border": ("#e4e7ec", "#343744"),
    "--border-strong": ("#d3d7de", "#3a3d4a"),
    "--text": ("#3c4149", "#e8e9ee"),
    "--text-dim": ("#98a0a8", "#9a9db0"),
    "--text-soft": ("#5b616b", "#cfd2dd"),
    "--accent": ("#2f6fed", "#6aa9ff"),
    "--ok": ("#22a06b", "#6ee7a8"),
    "--bad": ("#d93843", "#ff7b7b"),
    "--warn": ("#f0a500", "#e0b45c"),
    "--tag-blue-bg": ("#e8effd", "#2b3550"),
    "--tag-blue-bd": ("#d5e2fb", "#33405f"),
    "--tag-green-bg": ("#e6f4ec", "#2a3a2e"),
    "--tag-green-fg": ("#1f7a4d", "#95ec69"),
    "--tag-red-bg": ("#fbeaec", "#3a2226"),
    "--tag-red-bd": ("#f5d5d8", "#4a2a2f"),
    "--tag-red-fg": ("#b32833", "#ffb4b4"),
}

# 属性分类：同一个字面量在不同属性上映射到不同角色
SURFACE = {
    "#ffffff": "--card",
    "#fff": "--card",
    "#f5f6f8": "--bg",
    "#fafbfc": "--bg-soft",
    "#fbfcfd": "--card-soft",
    "#f3f5f9": "--hover",
    "#f0f2f5": "--sunken",
    "#eef0f4": "--sunken",
    "#e4e7ec": "--border",
    "#d3d7de": "--border-strong",
    "#e8effd": "--tag-blue-bg",
    "#d5e2fb": "--tag-blue-bd",
    "#e6f4ec": "--tag-green-bg",
    "#fbeaec": "--tag-red-bg",
    "#f5d5d8": "--tag-red-bd",
}
TEXT = {
    "#3c4149": "--text",
    "#98a0a8": "--text-dim",
    "#5b616b": "--text-soft",
    "#2f6fed": "--accent",
    "#22a06b": "--ok",
    "#d93843": "--bad",
    "#f0a500": "--warn",
    "#f5a623": "--warn",
    "#1f7a4d": "--tag-green-fg",
    "#b32833": "--tag-red-fg",
    "#c2373f": "--tag-red-fg",
}
# #ffffff 在 color 属性上是**蓝按钮上的白字**，两套主题都保持纯白 —— 不替换

DECL_RE = re.compile(r"(?P<prop>[a-zA-Z-]+)(?P<sep>\s*:\s*)(?P<value>[^;{}]+)")
SKIP_RE = re.compile(r":root\s*\{[^}]*\}|html\[data-theme='dark'\]\s*\{[^}]*\}")


def pick(prop: str, literal: str) -> str | None:
    p = prop.lower()
    if p == "color" or p in {"fill", "stroke"}:
        return TEXT.get(literal)
    if p.startswith("background"):
        return SURFACE.get(literal)
    if p.startswith("border") or p.startswith("outline"):
        return SURFACE.get(literal) or TEXT.get(literal)
    if p == "box-shadow":
        return None  # 阴影里的颜色单独处理，见 replace_all_shadows
    return SURFACE.get(literal) or TEXT.get(literal)


def convert(text: str) -> str:
    """只在非 :root / 非深色块里做字面量→变量替换"""
    parts: list[str] = []
    pos = 0
    for m in SKIP_RE.finditer(text):
        parts.append(_convert_chunk(text[pos:m.start()]))
        parts.append(m.group(0))
        pos = m.end()
    parts.append(_convert_chunk(text[pos:]))
    return "".join(parts)


def _convert_chunk(chunk: str) -> str:
    def fix(m: re.Match[str]) -> str:
        prop, sep, value = m.group("prop"), m.group("sep"), m.group("value")
        found = re.findall(r"#[0-9a-fA-F]{3,8}\b", value)
        new = value
        for lit in found:
            role = pick(prop, lit.lower())
            if role:
                new = re.sub(rf"(?<![0-9a-fA-F]){re.escape(lit)}(?![0-9a-fA-F])", f"var({role})", new)
        return f"{prop}{sep}{new}"

    return DECL_RE.sub(fix, chunk)


def inject_roles(path: pathlib.Path, text: str) -> str:
    """把角色变量写进已有的 :root（没有就在文件开头建一个）"""
    light = "\n".join(f"  {name}: {v[0]};" for name, v in ROLES.items())
    block = (
        "  /* ── 界面主题用的角色变量（浅色值）────────────────────────────\n"
        "   * 由 `tools/theme-light-dark.py` 生成；深色值在同文件末尾的\n"
        "   * `html[data-theme='dark']` 里覆盖。属性由主进程注入到 `<html>`。\n"
        "   */\n" + light + "\n"
    )
    m = re.search(r":root\s*\{", text)
    if m:
        return text[: m.end()] + "\n" + block + text[m.end():]
    return ":root {\n" + block + "}\n\n" + text


def append_dark(path: pathlib.Path, text: str) -> str:
    if "2026-10-14" in text:
        return text
    dark = "\n".join(f"  {name}: {v[1]};" for name, v in ROLES.items())
    block = (
        "\n/* ══════════════════════════════════════════════════════════════════\n"
        " * 深色主题（2026-10-14 加，设置页「界面主题」里切换）\n"
        " * ══════════════════════════════════════════════════════════════════\n"
        " * 只认主进程注入的 `<html data-theme=\"dark\">`。\n"
        " * ⚠️ 想改深色配色**只改这里**，不要去改上面的浅色值 ——\n"
        " *    两份定义分居 `:root` 与这里，改错一边的表现是\n"
        " *    \"切到深色之后有个别地方还是浅的\"，很难归因。\n"
        " */\n"
        "html[data-theme='dark'] {\n" + dark + "\n}\n"
    )
    return text + block


for name in ("petMenu.css", "settings.css", "memory.css"):
    path = BASE / name
    src = path.read_text(encoding="utf-8")
    out = append_dark(path, inject_roles(path, convert(src)))
    path.write_text(out, encoding="utf-8")

    leftover = sorted(set(re.findall(r"#[0-9a-fA-F]{6}\b", out)))
    mapped = {v[0].lower() for v in ROLES.values()} | {v[1].lower() for v in ROLES.values()}
    unmapped = [c for c in leftover if c.lower() not in mapped]
    print(f"  {name}: 完成；未纳入变量的深色系颜色 {len(unmapped)} 个：{unmapped[:12]}")
print("OK")
