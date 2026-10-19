"""把 app.js 里已随 UI 删除而失效的代码块剥掉。

输入框/发送按钮已经搬到独立的对话条窗口（chat.html + chat.js），
活泼度滑杆移进设置窗口，语音/免打扰/设置/最小化按钮彻底删掉。
所以 app.js 里对应的元素引用、事件绑定、以及只服务于它们的函数都要清掉 ——
留着就是空引用报错（`els.input` 为 null 时 `.addEventListener` 直接抛）。

这个脚本按"函数名 + 行号区间"删，不做正则猜谜：宁可多打印出来让人核对，
也不要删错一行（app.js 里有拖拽/穿透这类容易出事的逻辑）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

APP = Path(r"F:\ai项目\apps\desktop\src\renderer\app.js")


def main() -> int:
    text = APP.read_text(encoding="utf-8")
    lines = text.split("\n")

    # 1) 收窄 els：只留宠物窗口真正拥有的元素
    els_new = """const els = {
  bubble: $('bubble'),
  bubbleText: $('bubble-text'),
  typing: $('typing'),
  canvas: $('pet'),
  toast: $('toast'),
};"""
    start = next(i for i, line in enumerate(lines) if line.startswith("const els = {"))
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "};")
    lines[start : end + 1] = els_new.split("\n")

    text = "\n".join(lines)

    # 2) 删掉只服务于已删 UI 的函数（整段函数体，按大括号配对找结尾）
    drop_functions = [
        "renderMemory",
        "toggleMic",
        "submit",
        "setActivityUI",
        "renderAutonomy",
    ]
    for name in drop_functions:
        pattern = re.compile(
            rf"^(?:async )?function {name}\(", re.MULTILINE
        )
        match = pattern.search(text)
        if not match:
            print(f"  跳过（未找到）: {name}")
            continue
        # 找函数体结束：从 `{` 开始配对大括号
        brace_start = text.index("{", match.end())
        depth = 0
        i = brace_start
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        # 连同前面的 JSDoc 注释一起删
        line_start = text.rfind("\n", 0, match.start()) + 1
        # 往上吃掉紧邻的注释块
        probe = line_start
        while True:
            prev = text.rfind("\n", 0, probe - 1)
            if prev < 0:
                break
            prev_line = text[prev + 1 : probe].strip()
            if prev_line.startswith("*") or prev_line.startswith("/**") or prev_line.startswith("//"):
                probe = prev + 1
            else:
                break
        text = text[:probe] + text[i + 1 :]
        print(f"  已删函数: {name}")

    APP.write_text(text, encoding="utf-8")
    print(f"-> {APP}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
