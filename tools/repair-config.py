"""修 persona.yaml 里的重复键：同一段、同一缩进下，同名键只留最后一个。

## 为什么按「段 + 缩进」分组

上一版我按「缩进 + 键名」**全局**去重，误删了 28 处合法配置 ——
因为**不同段里的同名键是完全合法的**：

    providers.brain.model  = deepseek-flash
    providers.asr.model    = large-v3-turbo      <- 都叫 model，都要留

所以分组键必须是 `(段路径, 缩进, 键名)`。

段路径 = 从文件开头累积的**缩进栈**（遇到更浅的缩进就弹栈）。

## 保留哪一个

保留**最后一个**，因为 `readDeepScalar` 读到的就是最后一个 ——
这样"修完之后读到的值"和"修之前读到的值"一致，不会悄悄改变行为。
"""

from __future__ import annotations

import pathlib
import re
import sys

YAML = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "config/persona.yaml")


def main() -> int:
    lines = YAML.read_text(encoding="utf-8").split("\n")

    # (path_tuple, indent, key) -> 最后一次出现的行号
    seen: dict[tuple[tuple[str, ...], int, str], int] = {}
    stack: list[tuple[int, str]] = []  # (indent, key)
    drop: set[int] = set()

    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        m = re.match(r"^\s*([A-Za-z_][\w-]*)\s*:(.*)$", line)
        if not m:
            continue
        key, rest = m.group(1), m.group(2)

        # 弹栈：缩进 <= 栈顶的都要出栈
        while stack and indent <= stack[-1][0]:
            stack.pop()
        path = tuple(k for _, k in stack)
        sig = (path, indent, key)

        if sig in seen:
            # 同名同段同缩进已经出现过 —— 删掉**前一个**（保留最后一个）
            prev = seen[sig]
            drop.add(prev)
            print(f"  删重复: L{prev + 1}  {lines[prev].strip()[:60]}")
        seen[sig] = i

        # 只有"纯标题行"才进栈（`key:` 后面没值）
        if rest.strip() == "":
            stack.append((indent, key))

    if not drop:
        print("  ✅ 没有重复键")
        return 0
    out = [l for i, l in enumerate(lines) if i not in drop]
    YAML.write_text("\n".join(out), encoding="utf-8")
    print(f"  ✅ 删掉 {len(drop)} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
