"""统计圆括号/方括号的平衡（跳过字符串），定位真正的语法错误。"""
import pathlib

SRC = pathlib.Path(r"F:\ai项目\apps\desktop\src\main\index.ts")
lines = SRC.read_text(encoding="utf-8").split("\n")

par = 0
brk = 0
state = "code"
info = []
for line in lines:
    j = 0
    while j < len(line):
        ch = line[j]
        nxt = line[j + 1] if j + 1 < len(line) else ""
        if state == "code":
            if ch == "/" and nxt == "/":
                break
            if ch == "/" and nxt == "*":
                state = "block"
                j += 2
                continue
            if ch == "'":
                state = "squote"
            elif ch == '"':
                state = "dquote"
            elif ch == "`":
                state = "tpl"
            elif ch == "(":
                par += 1
            elif ch == ")":
                par -= 1
            elif ch == "[":
                brk += 1
            elif ch == "]":
                brk -= 1
        elif state == "block":
            if ch == "*" and nxt == "/":
                state = "code"
                j += 2
                continue
        elif state == "tpl":
            if ch == "\\":
                j += 2
                continue
            if ch == "`":
                state = "code"
        else:
            if ch == "\\":
                j += 2
                continue
            if (state == "squote" and ch == "'") or (state == "dquote" and ch == '"'):
                state = "code"
        j += 1
    info.append((par, brk))

print(f"  文件结束：圆括号 = {par}   方括号 = {brk}   （都应为 0）")
print()
print("  圆括号出现负值的行：")
for i, (p, b) in enumerate(info):
    if p < 0:
        print(f"    L{i+1}: par={p}  {lines[i].strip()[:70]}")
print()
print("  L780..L812 的圆括号深度：")
for i in range(779, min(813, len(lines))):
    print(f"    L{i+1:<4} par={info[i][0]:<3} {lines[i].strip()[:64]}")
