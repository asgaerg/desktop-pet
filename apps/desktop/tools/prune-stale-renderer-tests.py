"""删掉 test-renderer.mjs 里考"已删 UI"的那几节。

输入框/发送按钮搬去了独立窗口（chat），活泼度滑杆移进设置窗口，
语音/免打扰按钮删掉了。所以第 4~6 节（输入与发送、活泼度滑杆、免打扰）
以及第 9~12 节里针对已删元素的断言都不再成立 —— 留着就是一片红，
而红的是**测试过时**、不是代码坏了。那种红最容易让人开始忽略检查结果。
"""

from __future__ import annotations

import re
from pathlib import Path

TEST = Path(r"F:\ai项目\apps\desktop\tools\test-renderer.mjs")


def cut_section(text: str, start_marker: str, end_marker: str) -> str:
    """删掉从 start_marker 那一行到 end_marker 之前的所有内容。"""
    start = text.index(start_marker)
    # 往回退到该行行首
    start = text.rfind("\n", 0, start) + 1
    end = text.index(end_marker)
    end = text.rfind("\n", 0, end) + 1
    return text[:start] + text[end:]


def main() -> int:
    text = TEST.read_text(encoding="utf-8")

    # 第 4~6 节整体删掉（输入与发送 / 活泼度滑杆 / 免打扰）
    text = cut_section(text, "console.log('\\n4. 输入与发送')", "console.log('\\n7. 立绘状态机")
    print("  已删：第 4~6 节（输入与发送、活泼度滑杆、免打扰）")

    # 第 9 节里"状态文字被更新"之类的断言也要删（调用 setStatus 的地方还在，
    # 但断言读的是已被删掉的 #status 元素）
    for marker_start, marker_end, label in [
        ("console.log('\\n10. 语音状态灯')", "console.log('\\n11. 记忆显示与清空')", "第 10 节（语音状态灯）"),
        ("console.log('\\n11. 记忆显示与清空')", "console.log('\\n12. 自主行为状态显示')", "第 11 节（记忆显示与清空）"),
        ("console.log('\\n12. 自主行为状态显示')", "console.log('\\n13. 中断与快捷键')", "第 12 节（自主行为状态显示）"),
    ]:
        if marker_start in text and marker_end in text:
            text = cut_section(text, marker_start, marker_end)
            print(f"  已删：{label}")

    TEST.write_text(text, encoding="utf-8")
    print(f"-> {TEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
