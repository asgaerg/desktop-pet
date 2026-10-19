"""门禁：提炼提示词在两处必须**逐字一致**（2026-10-17，2026-10-17 扩展到两段）。

## 为什么必须有这道门禁

「什么会被记住」由**提炼提示词**决定。它们存在两个地方：

```
config/persona.yaml        prompts.extract / prompts.impression   <- 真相源
apps/brain/pet/memory.py   _EXTRACT_PROMPT_FALLBACK               <- 内置兜底
                           _IMPRESSION_PROMPT_FALLBACK
```

`_extract_prompt()` / `_impression_prompt()` 的逻辑是：
YAML 读得到就用 YAML，读不到 / 用户打错花括号就退回内置兜底。

## 🔴 不同步的后果是**静默的**

改了 YAML 忘了改兜底，那么：

```
正常情况下   -> 用新规则        （看起来一切正常）
YAML 一出错  -> 悄悄退回**旧规则**
             -> 记忆又开始长出 `用户在玩游戏` 这种模糊条目
             -> 而没有任何报错
```

⚠️ 这类"降级路径用的是旧逻辑"是最难发现的，因为**主路径测不出它**。

## 🔴 2026-10-17 扩展：印象提示词也要查

新增「一段交流结束提炼印象」之后，它是一模一样的结构、一模一样的风险。
一个门禁只查一半，等于留了半个洞。

## 比对方式：按行归一化后逐行比

不能直接比字符串：YAML 的 `|` 块标量会带缩进；Python 侧是用 `"..."` 拼接的。
所以两边都做：去首尾空行 -> 每行 `strip()` -> 逐行比。
"""

from __future__ import annotations

import sys
from pathlib import Path

# ⚠️ 必须先把 stdout 切成 UTF-8。
#
# 这个脚本会被 `npm run check:extract` 调用，而 npm 起子进程时**不带**
# `PYTHONIOENCODING` —— Windows 控制台默认 GBK（cp936），
# 于是哪怕只是 `print("✅ …")` 都会炸：
#
#     UnicodeEncodeError: 'gbk' codec can't encode character '\u2705'
#
# ⚠️ 这**不是**"输出好看不好看"的问题：报错发生在 print 里，
# 会让一个**本来通过**的检查以 exit 1 结尾 —— 假红灯。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

# ⚠️ 别写死绝对路径：项目搬过一次家（F:\ai项目 -> F:\ai），
# 写死之后这个门禁在**新路径上直接抛 FileNotFoundError** —— 看起来像"检查挂了"，
# 实际是脚本自己找不到文件。按 `__file__` 推导，搬去哪儿都对。
ROOT = Path(__file__).resolve().parent.parent
YAML = ROOT / "config" / "persona.yaml"
PY = ROOT / "apps" / "brain" / "pet" / "memory.py"

# (说明, YAML 里的键, Python 里的常量名)
PAIRS: list[tuple[str, str, str]] = [
    ("事实提炼", "extract", "_EXTRACT_PROMPT_FALLBACK"),
    ("印象提炼", "impression", "_IMPRESSION_PROMPT_FALLBACK"),
    # 截图对话的画面描述（2026-10-17）。同理：yaml 与内置兜底必须一致，
    # 否则"配置读失败"会静默退回到另一套描述口径。
    ("画面描述", "screen_chat", "DESCRIBE_PROMPT"),
]


def yaml_block(key: str) -> list[str]:
    """从 persona.yaml 抠出 `key: |` 的正文行。

    ⚠️ 缩进基准要取**键那一行**的缩进，不是 0 —— 第一版拿 `base_indent = 0`
    去判块结束，于是块到底后没停，把后面的段落也吃了进来
    （报"yaml 56 行 / py 44 行"，前 44 行其实是一致的）。
    """
    lines = YAML.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    inside = False
    key_indent = 0
    for raw in lines:
        stripped = raw.strip()
        if not inside:
            if stripped == f"{key}: |":
                inside = True
                key_indent = len(raw) - len(raw.lstrip())
            continue
        indent = len(raw) - len(raw.lstrip())
        # 块结束：非空行且缩进 <= 键的缩进
        if stripped and indent <= key_indent:
            break
        out.append(raw)
    if not out:
        return []
    body_indents = [len(l) - len(l.lstrip()) for l in out if l.strip()]
    cut = min(body_indents) if body_indents else 0
    return [l[cut:].rstrip() for l in out]


def python_value(name: str) -> list[str]:
    """取 `memory.py` 里某个常量的值。

    ⚠️ 不要用"找 `\\n)` 当块结尾"这种办法 —— 第一版就这么干的，
    结果被**上面那段注释里的括号**骗了，切出来的表达式语法不完整，
    报 `'(' was never closed`。

    ✅ 正确做法：**真的 import 一次**（比正则可靠）。
    """
    import importlib

    brain = str(ROOT / "apps" / "brain")
    if brain not in sys.path:
        sys.path.insert(0, brain)
    # 常量分散在两个模块里：提炼相关在 memory，画面描述在 screen_chat。
    for module_name in ("pet.memory", "pet.screen_chat"):
        module = importlib.import_module(module_name)
        if hasattr(module, name):
            value = getattr(module, name)
            break
    else:
        value = ""
    return [line.rstrip() for line in str(value).split("\n")]


def normalize(lines: list[str]) -> list[str]:
    """去首尾空行 + 每行 strip。"""
    out = [line.strip() for line in lines]
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    return out


def check_one(label: str, key: str, const: str) -> bool:
    print()
    print(f"  ── {label} ──")
    yaml_lines = normalize(yaml_block(key))
    py_lines = normalize(python_value(const))
    print(f"     persona.yaml  prompts.{key:<11} {len(yaml_lines):>3} 行")
    print(f"     memory.py     {const:<26} {len(py_lines):>3} 行")

    if not yaml_lines:
        print(f"     ❌ 没从 persona.yaml 抠到 `{key}`（键名或块标量格式变了？）")
        return False
    if not py_lines:
        print(f"     ❌ 没从 memory.py 抠到 `{const}`")
        return False
    if yaml_lines == py_lines:
        print("     ✅ 逐行完全一致")
        return True

    limit = max(len(yaml_lines), len(py_lines))
    shown = 0
    for i in range(limit):
        a = yaml_lines[i] if i < len(yaml_lines) else "<缺>"
        b = py_lines[i] if i < len(py_lines) else "<缺>"
        if a != b:
            print(f"     ❌ 第 {i + 1} 行不同：")
            print(f"          yaml: {a!r}")
            print(f"          py  : {b!r}")
            shown += 1
            if shown >= 4:
                print("          …（更多差异已省略）")
                break
    return False


def main() -> int:
    print("=" * 80)
    print("  门禁：提炼提示词两处必须一致")
    print("=" * 80)

    results = [check_one(label, key, const) for label, key, const in PAIRS]

    print()
    print("=" * 80)
    if all(results):
        print(f"  ✅ 全部 {len(results)} 段提示词逐行一致")
        return 0
    print("  ⚠️ 两处必须**逐字一致** —— 否则 YAML 一读失败就静默退回旧规则，")
    print("     记忆会重新长出「用户在玩游戏」这种模糊条目。")
    return 1


raise SystemExit(main())
