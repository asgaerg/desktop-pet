r"""门禁：`self.xxx` 引用的成员必须真的存在（"线没接"的静态检查，2026-10-17）。

## 为什么需要这个

2026-10-17 真机验证抓到：

```
Gateway._maybe_fire_event 里写了 self._interest_topics(limit=5)
但 _interest_topics 定义在 **Conversation** 上
-> 每一轮事件判定都抛 AttributeError
-> 被 except 接住、只记一条 ERROR
-> 随机事件永远不触发、event_log 永远是 0
-> 而进程看起来完全正常
```

这就是项目里反复出现的「**两边都写好了，中间那根线没接**」。
它不是逻辑错误，是**名字写错了一个对象**，而且恰恰被兜底 except 掩盖了 ——
单测测不到，门禁测不到，只有真机能发现。

## 它怎么查

对每个类，收集：

```
定义侧  · 方法名（def）
        · 类属性 / property
        · `self.x = ...` 赋值（含在 __init__ 里赋的）
引用侧  · 方法体里所有 `self.<name>`
```

`引用 - 定义 - 白名单` 里剩下的就是可疑项。

## ⚠️ 白名单必须显式

Python 允许动态属性（`setattr`、`__getattr__`、混入、外层注入），
所以一定有查不出来的合法情况。白名单里每一条都写清**为什么合法** ——
否则这个门禁会慢慢变成"加一行就过"的形式主义。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

# ⚠️ 别写死绝对路径（项目搬家过一次：F:\ai项目 -> F:\ai）。
# 写死的话所有文件都"不存在"，门禁会报一堆假问题 —— 比不跑更误导人。
ROOT = Path(__file__).resolve().parent.parent
BRAIN = ROOT / "apps" / "brain" / "pet"

# ⚠️ 允许引用但不在**本类**定义的名字，附理由。
#
# 键是类名，值是 {名字: 理由}。跨类引用要写成 `别的类.名字` 的形式说明来源。
ALLOW: dict[str, dict[str, str]] = {
    "Gateway": {
        # 这些是 Gateway 在 __init__ 之后由 attach/start 注入的，见 gateway.py
        "config": "构造参数，__init__ 里赋值",
    },
    "Conversation": {
        "config": "构造参数，__init__ 里赋值",
    },
}


def collect_class_members(node: ast.ClassDef) -> tuple[set[str], set[str]]:
    """返回 (定义的名字集合, 引用的名字集合)。"""
    defined: set[str] = set()
    used: set[str] = set()

    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(item.name)
        elif isinstance(item, ast.Assign):
            for tgt in item.targets:
                if isinstance(tgt, ast.Name):
                    defined.add(tgt.id)
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            defined.add(item.target.id)

    # 在方法体里找 self.x 的**赋值**（视为定义）和**读取**（视为引用）
    for item in ast.walk(node):
        if isinstance(item, (ast.Attribute, ast.Assign)):
            pass
    for item in ast.walk(node):
        if (
            isinstance(item, ast.Attribute)
            and isinstance(item.value, ast.Name)
            and item.value.id == "self"
        ):
            if isinstance(item.ctx, ast.Store):
                defined.add(item.attr)
            else:
                used.add(item.attr)
    return defined, used


def main() -> int:
    print("=" * 80)
    print("  门禁：self.xxx 引用必须存在（防「线没接」）")
    print("=" * 80)

    targets = [BRAIN / "gateway.py", BRAIN / "memory2.py", BRAIN / "events.py",
               BRAIN / "subjectivity.py", BRAIN / "segments.py"]
    problems: list[str] = []
    total_classes = 0

    for path in targets:
        if not path.exists():
            problems.append(f"{path.name} 不存在")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            total_classes += 1
            defined, used = collect_class_members(node)
            allow = ALLOW.get(node.name, {})
            # 继承来的名字查不到定义，只能靠白名单兜 —— 有父类的类跳过
            # （本项目这几个模块都是直接继承 object）
            missing = sorted(used - defined - set(allow))
            if missing:
                print()
                print(f"  {path.name} · class {node.name}")
                for name in missing:
                    print(f"     ❌ self.{name} 在本类里没有定义")
                    problems.append(f"{path.name}:{node.name}.{name}")
            else:
                print(f"  ✅ {path.name:<16} class {node.name:<14} "
                      f"(定义 {len(defined):>3} / 引用 {len(used):>3})")

    print()
    print("=" * 80)
    if problems:
        print(f"  ❌ {len(problems)} 处可疑引用：")
        for item in problems:
            print(f"     · {item}")
        print()
        print("  ⚠️ 常见真因：方法定义在**另一个类**上，调用时写错了 `self`。")
        print("     如果确实是动态注入的，在脚本的 ALLOW 里加一条并写明理由。")
    else:
        print(f"  ✅ {total_classes} 个类，所有 self.xxx 都有定义")
    print("=" * 80)
    return 1 if problems else 0


raise SystemExit(main())
