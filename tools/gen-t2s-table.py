#!/usr/bin/env python
"""
从 Unicode Unihan 数据生成**繁->简**映射表，写成 Python 模块。

## 为什么要自己生成（而不是装个库）

`opencc` / `zhconv` 这类库都能做简繁转换，但：
  · 多一个运行时依赖，而本项目一直尽量少依赖（尤其避免需要编译的）
  · 它们体积不小，而我们只需要"繁 -> 简"这一个方向

Unihan 是 **Unicode 官方**的变体数据，权威且稳定。用它生成一张表，
既没有运行时依赖，也可以随时重新生成（换 Unicode 版本时）。

## 数据来源

`Unihan_Variants.txt`（在 `Unihan.zip` 里），我们只用这两个字段：

  · `kTraditionalVariant` —— 某个字**是**哪些繁体字的简体形式
  · `kSimplifiedVariant`  —— 某个字**是**哪些简体字的繁体形式

反过来推一遍就能得到"繁体 -> 简体"：如果 `A` 的 `kTraditionalVariant`
是 `B`，那么 `B` 的简体就是 `A`。

## ⚠️ 一对多怎么办

简繁不是一一对应的（一个简体可能对应多个繁体，反之亦然）。
遇到多值时取**第一个** —— 这是 opencc 的 `t2s` 也在用的近似做法，
对"把识别出的繁体转成简体"这个用途足够（个别字会不完美，
但比整句都是繁体好得多）。

## 用法

    python tools/gen-t2s-table.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

SRC = ROOT / "apps" / "desktop" / "pet-data" / "unihan" / "Unihan_Variants.txt"
OUT = ROOT / "apps" / "brain" / "pet" / "t2s_table.py"

# `U+4E00\tkTraditionalVariant\tU+4E01 U+4E02`
LINE = re.compile(r"^U\+([0-9A-F]+)\tk(Simplified|Traditional)Variant\t(.+)$")

"""
## 手工补充表：Unihan **不收**的常见繁体

Unihan 的 `kTraditionalVariant` / `kSimplifiedVariant` 只描述**正式的繁简对应**，
不收"异体字 / 地区变体"。实测缺了这些**日常极常见**的字：

    U+88E1 裡  —— 台湾写法，大陆是「里」。Unihan 把它当 U+88CF 裏 的异体，不收
    U+9F8D 龍  —— 有收（对照用）

所以补一张小表。**只补"确实高频且 Unihan 漏掉"的**，不要凭感觉往里塞 ——
塞错了会把本来正确的字改坏（比如「著」在「著作」里读 zhù，
Unihan 给的映射是「着」，直接换会变成「着作」）。
"""
MANUAL: dict[str, str] = {
    "裡": "里",  # 台湾常见，大陆一律用「里」
    "裏": "里",
}


def parse_codepoints(field: str) -> list[str]:
    out = []
    for token in field.split():
        if token.startswith("U+"):
            try:
                out.append(chr(int(token[2:], 16)))
            except ValueError:
                continue
    return out


def main() -> int:
    if not SRC.is_file():
        print(f"  ❌ 找不到 {SRC}")
        print("     先解压 Unihan.zip（见 docs 里的说明）")
        return 1

    trad_to_simp: dict[str, str] = {}
    simp_to_trad: dict[str, str] = {}

    for raw in SRC.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw or raw.startswith("#"):
            continue
        m = LINE.match(raw)
        if not m:
            continue
        code, field, value = m.group(1), m.group(2), m.group(3)
        try:
            char = chr(int(code, 16))
        except ValueError:
            continue
        targets = parse_codepoints(value)
        if not targets:
            continue
        if field == "Traditional":
            # `char` 是简体，targets 是它的繁体形式 -> 反推：繁体 -> char
            for t in targets:
                trad_to_simp.setdefault(t, char)
            simp_to_trad.setdefault(char, targets[0])
        else:
            # `char` 是繁体，targets 是它的简体形式 -> 直接用
            trad_to_simp.setdefault(char, targets[0])

    # 去掉"转成自己"的项（有些字简繁同形）
    trad_to_simp = {k: v for k, v in trad_to_simp.items() if k != v}
    # 手工补充（Unihan 不收的异体/地区变体）
    for k, v in MANUAL.items():
        trad_to_simp.setdefault(k, v)

    if len(trad_to_simp) < 1000:
        print(f"  ⚠️ 只生成 {len(trad_to_simp)} 条，数据可能不对，先别覆盖")
        return 1

    # 排成稳定顺序，便于 diff
    items = "".join(f"{k}{v}" for k, v in sorted(trad_to_simp.items()))

    OUT.write_text(
        f'''"""
繁 -> 简 映射表（**自动生成，不要手改**）。

生成工具：`tools/gen-t2s-table.py`
数据来源：Unicode Unihan `Unihan_Variants.txt`
（`kTraditionalVariant` / `kSimplifiedVariant` 两个字段）

## 为什么需要它

Whisper 系模型的**中文输出会混繁体**（训练语料里繁体占比高）。实测：

    large-v3-turbo / medium / small 都会出现「今天天氣不錯」这种

而用户是简体用户，看到繁体很别扭。`initial_prompt` 引导（"请用简体中文
转写"）**有效但不保险** —— 模型可能不听。所以加一道**确定性的后处理**兜底。

## 规模

共 {len(trad_to_simp)} 条。用**扁平字符串**（两字一组）而不是 dict 字面量：
同样的数据，字符串形式的源码体积小得多、导入也快得多
（Python 解析 `{{'x': 'y', ...}}` 的字典字面量比解析一个大字符串慢很多）。

## 怎么用

    from .t2s_table import to_simplified
    text = to_simplified(text)

它的实现刻意**逐字查表**（而不是用 `str.translate`）：`translate` 需要先建
一张完整码点映射表，而这里只需要一个 dict 查找，对"一句话几十个字"的
场景足够快（实测 < 0.1ms），也不必额外占内存。
"""

from __future__ import annotations

# 两字一组：繁体在前、简体在后
_PAIRS = (
{_wrap(items)}
)

# 构造时只做一次
_T2S = dict(zip(_PAIRS[0::2], _PAIRS[1::2]))


def to_simplified(text: str) -> str:
    """把文本里的繁体字换成简体；没有繁体的句子原样返回。"""
    if not text:
        return text
    # 先快速判断有没有需要转的字 —— 大多数句子一次都不用进循环体
    if not any(ch in _T2S for ch in text):
        return text
    return "".join(_T2S.get(ch, ch) for ch in text)
''',
        encoding="utf-8",
    )
    print(f"  ✅ 生成 {OUT.relative_to(ROOT)}：{len(trad_to_simp)} 条繁->简")
    print(f"     源码 {OUT.stat().st_size / 1024:.1f} KB")
    return 0


def _wrap(s: str, width: int = 64) -> str:
    """把长字符串按宽度折行，写成多行字符串字面量（Python 会自动拼起来）。"""
    lines = [s[i : i + width] for i in range(0, len(s), width)]
    return "\n".join(f'    "{line}"' for line in lines)


if __name__ == "__main__":
    raise SystemExit(main())
