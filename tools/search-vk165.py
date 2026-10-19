"""查历史：VK 0xA5（右 Alt）那个问题最后怎么解决的？

## 线索（`test-vk165.ps1` 的文件头）

```
# 应用日志里**从来没有「按下」**（连用户物理按键也没有）—— 说明
# 监视器 -> 主进程 这一段断了。而前面已证明监视器**对 A 键是好的**
# （tools/test-ptt-unfocused.ps1 里收到了 DOWN 65 / UP 65）。
# 所以怀疑点收窄到：**VK 0xA5（右 Alt）这个键本身**。
```

**和现在的现象一模一样** —— 监视器一个 DOWN 都不认。
而 20:01:25 那条 `phase='end'`（无 start）说明**偶尔能过**。

## 查什么

在 `docs/进度交接.md` 里找 §三十六~§五十 那一段（PTT 相关），
关键词：`0xA5` / `VK 165` / `右 Alt` / `AltGr` / `test-vk165`。
"""

import pathlib
import re
import sys

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

DOC = ROOT / "docs" / "进度交接.md"
lines = DOC.read_text(encoding="utf-8").split("\n")

PATTERNS = {
    "VK 0xA5 / 165": r"0xA5|VK 165|v?k=165|右键|右 ?Alt|AltGr",
    "test-vk165": r"test-vk165",
    "A 键对照": r"DOWN 65|UP 65|A 键|VK 65",
    "换键/改键": r"换(个|一个)?键|改成 ?F\d|建议.*键|recommend",
    "GetAsyncKeyState 局限": r"GetAsyncKeyState.{0,60}(局限|不适用|缺陷|问题|限制)",
}

for label, pat in PATTERNS.items():
    hits = [(i + 1, l.strip()) for i, l in enumerate(lines) if re.search(pat, l)]
    print(f"  ═══ {label}（{len(hits)} 处）═══")
    for n, l in hits[:12]:
        head = ""
        for k in range(n - 1, max(0, n - 300), -1):
            if lines[k].startswith("## "):
                head = lines[k].strip()[:46]
                break
        print(f"    L{n:<6} {l[:88]}")
        if head:
            print(f"           ^ {head}")
    print()
