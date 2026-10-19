"""查历史：这个项目怎么处理「不聚焦时按键失效」的？

## 找什么

用户说「现在还是要聚焦宠物窗口按键才能有用，你看看历史怎么搞的」
—— 说明**以前有过能用的版本**，或者至少讨论过。

在 `docs/进度交接.md` 里按关键词找：
  · 焦点 / 前台 / foreground / focus 相关的按键问题
  · GetAsyncKeyState 的坑
  · probe-key-heartbeat.ps1（这个是干什么的？名字像"心跳"）
  · 低级键盘钩子 / SetWindowsHookEx / RegisterRawInputDevices
"""

import pathlib
import re
import sys

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

DOC = ROOT / "docs" / "进度交接.md"
doc = DOC.read_text(encoding="utf-8")
lines = doc.split("\n")

PATTERNS = {
    "焦点/前台 + 按键": r"(焦点|前台|foreground|focus).{0,40}(按键|键|钩子|hotkey|shortcut)|(按键|钩子).{0,40}(焦点|前台|foreground)",
    "GetAsyncKeyState": r"GetAsyncKeyState",
    "心跳脚本": r"probe-key-heartbeat|心跳",
    "低级钩子": r"SetWindowsHookEx|低级键盘钩子|WH_KEYBOARD_LL",
    "RawInput": r"RegisterRawInputDevices|RawInput|原始输入",
    "全局快捷键失效": r"globalShortcut.{0,60}(失效|没用|无效|不生效)",
    "管理员权限": r"管理员|elevat|UAC|以管理员身份",
}

for label, pat in PATTERNS.items():
    hits = [(i + 1, l.strip()) for i, l in enumerate(lines) if re.search(pat, l, re.I)]
    print(f"  ═══ {label}（{len(hits)} 处）═══")
    for n, l in hits[:8]:
        # 找最近的标题
        head = ""
        for k in range(n - 1, max(0, n - 260), -1):
            if lines[k].startswith("## "):
                head = lines[k].strip()[:52]
                break
        print(f"    L{n:<6} {l[:86]}")
        if head:
            print(f"           ^ {head}")
    print()


print("  ═══ probe-key-heartbeat.ps1 是干什么的 ═══")
hb = ROOT / "apps" / "desktop" / "tools" / "probe-key-heartbeat.ps1"
if hb.is_file():
    txt = hb.read_text(encoding="utf-8", errors="replace")
    for i, l in enumerate(txt.split("\n")[:40]):
        if l.strip().startswith("#"):
            print(f"    {l.strip()[:100]}")
else:
    print("    （文件不在）")
