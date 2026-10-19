"""实测 `pet.tools`：工具执行 + **沙箱越界检查**（2026-10-12）。

## 最重要的是越界检查

用户的原话：「可修改和存储还是要给个地址，**不让他超出地址操作**」。

所以这里把能想到的绕过手法全试一遍 —— **越界必须被拒**：

    ../etc/passwd          ..\\..\\config\\persona.yaml
    /etc/passwd            C:\\Windows\\system32\\x.txt
    ....//config/x         子目录/文件
    含 NUL 字节            Windows 保留名（CON/NUL/PRN）
    超长名字               空名字

## 为什么这个测试值得写这么细

「限定目录」是**安全承诺**。一旦漏掉一种绕过手法，
用户看到的是一切正常，而实际已经能被写到项目里去了 —— 那种 bug 不会报错。
"""

from __future__ import annotations

import asyncio
import pathlib
import shutil
import sys

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

from pet import tools  # noqa: E402

passed = 0
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed
    if ok:
        passed += 1
        print(f"  OK   {name}")
    else:
        failed.append(f"{name} —— {detail}")
        print(f"  FAIL {name}\n       {detail}")


# 用项目内的临时目录，不用 %TEMP%（这台机器上它不可写，见 docs）
STATE = ROOT / "apps" / "desktop" / "pet-data" / "_tools-test"
if STATE.exists():
    shutil.rmtree(STATE, ignore_errors=True)
STATE.mkdir(parents=True, exist_ok=True)

print("  ═══ ① 工具清单 ═══")
if True:   # 原为 C 风格花括号（Python 不支持）
    with_web = tools.build_tools(STATE, allow_web=True)
    without_web = tools.build_tools(STATE, allow_web=False)
    names_web = [t.name for t in with_web]
    names_noweb = [t.name for t in without_web]
    print(f"    开联网: {names_web}")
    print(f"    关联网: {names_noweb}")

    check("开联网时有 web_search", "web_search" in names_web)
    check("开联网时有 web_fetch", "web_fetch" in names_web)
    check("开联网时有 bilibili_search", "bilibili_search" in names_web)
    check("关联网时**没有** web_search", "web_search" not in names_noweb,
          "关掉还注册的话，模型会尝试调用，而 UI 上写着'没开' —— 隐私口径不一致")
    check("关联网时**没有** web_fetch", "web_fetch" not in names_noweb)
    check("关联网时**没有** bilibili_search", "bilibili_search" not in names_noweb,
          "B 站搜索也是联网 —— 关掉就必须一起关")
    check("沙箱工具不受联网开关影响", "sandbox_write" in names_noweb and "sandbox_read" in names_noweb)
    check("**没有 shell 工具**（安全边界）",
          not any("shell" in n or "exec" in n or "run" in n for n in names_web),
          f"出现了可疑工具：{names_web}")
    # ⚠️ 抖音读不了（要签名），**不能**做成工具 —— 否则模型会一直试
    check("**没有抖音工具**（要签名，读不了）",
          not any("douyin" in n.lower() for n in names_web),
          f"出现了：{[n for n in names_web if 'douyin' in n.lower()]}")

    schema = tools.schemas(with_web)
    check("schema 是 OpenAI 的 shape",
          all(s.get("type") == "function" and "name" in s.get("function", {}) for s in schema),
          f"实际 {schema[:1]}")
    check("web_search 的 query 是必填",
          "query" in schema[0]["function"]["parameters"].get("required", []))

    by_name = {s["function"]["name"]: s for s in schema}
    check("bilibili_search 有 schema", "bilibili_search" in by_name)
    if "bilibili_search" in by_name:
        params = by_name["bilibili_search"]["function"]["parameters"]
        check("bilibili_search 的 query 是必填", "query" in params.get("required", []))
        desc = by_name["bilibili_search"]["function"]["description"]
        # ⚠️ 这条描述是修过 bug 的：之前没写"够了就停"，
        #    模型搜到答案后继续换 4 个词挖 3 轮，撞上限被强制收尾，回复只剩 60 字
        check("描述里写了「搜一次就够、立刻回答」",
              "搜一次就够" in desc or "停下来回答" in desc,
              "少了这条，模型会反复换词重搜直到撞上轮数上限")
        check("描述里提醒了「不是官方信息」", "不是官方信息" in desc)
        check("描述里说明了只支持 B 站、抖音读不了",
              "只支持 B 站" in desc and "抖音" in desc)

print()
print("  ═══ ② 沙箱写入 / 读回（正常路径）═══")
if True:   # 原为 C 风格花括号（Python 不支持）
    result = asyncio.run(tools.run_tool(tools.build_tools(STATE, False), "sandbox_write",
                                        '{"name": "notes.md", "content": "第一条笔记"}'))
    print(f"    写入结果：{result}")
    check("正常写入成功", "已写入" in result, result)

    read = asyncio.run(tools.run_tool(tools.build_tools(STATE, False), "sandbox_read",
                                      '{"name": "notes.md"}'))
    check("读回来内容对", read == "第一条笔记", f"实际 {read!r}")

    # 中文文件名
    r2 = asyncio.run(tools.run_tool(tools.build_tools(STATE, False), "sandbox_write",
                                    '{"name": "笔记.md", "content": "中文名"}'))
    check("中文文件名可用", "已写入" in r2, r2)

print()
print("  ═══ ③ 🔴 越界检查（最重要的一段）═══")
if True:   # 原为 C 风格花括号（Python 不支持）
    good = tools.build_tools(STATE, False)
    escapes = [
        "../outside.txt",
        "..\\outside.txt",
        "../../config/persona.yaml",
        "..\\..\\config\\persona.yaml",
        "/etc/passwd",
        "C:\\Windows\\System32\\evil.txt",
        "C:/Windows/evil.txt",
        "sub/dir/file.txt",
        "sub\\file.txt",
        "....//config/persona.yaml",
        ".hidden",
        "CON",
        "NUL",
        "a" * 80,
        "",
        "   ",
        "file\x00.txt",
        "file|name.txt",
        "file*.txt",
        "file?.txt",
    ]
    for bad in escapes:
        payload = '{"name": ' + repr(bad).replace("'", '"') + ', "content": "x"}'
        result = asyncio.run(tools.run_tool(good, "sandbox_write", payload))
        # 成功的话结果里是「已写入」
        blocked = "已写入" not in result
        check(f"越界被拒：{bad!r}", blocked, f"竟然写成功了！结果={result!r}")

    # 确认真的什么都没写出去
    outside = STATE.parent / "outside.txt"
    check("确实没在沙箱外留下文件", not outside.exists(), f"外部出现了 {outside}")
    sandbox_files = sorted(p.name for p in (STATE / tools.SANDBOX_DIRNAME).iterdir())
    print(f"    沙箱内实际文件：{sandbox_files}")
    check("沙箱内只有那两个合法文件", sandbox_files == ["notes.md", "笔记.md"],
          f"实际 {sandbox_files}")

print()
print("  ═══ ④ 参数错误要**给出可操作的信息** ═══")
if True:   # 原为 C 风格花括号（Python 不支持）
    good = tools.build_tools(STATE, False)
    r = asyncio.run(tools.run_tool(good, "sandbox_write", '{"name": "x.md"}'))
    check("缺 content 时提示缺什么", "content" in r, r)

    r = asyncio.run(tools.run_tool(good, "sandbox_write", "这不是 JSON"))
    check("参数不是 JSON 时提示重试", "JSON" in r, r)

    r = asyncio.run(tools.run_tool(good, "不存在的工具", "{}"))
    check("未知工具被拒", "没有名为" in r, r)

    r = asyncio.run(tools.run_tool(good, "sandbox_read", '{"name": "没有这个.md"}'))
    check("读不存在的文件时说清目录里有什么", "目录里现有" in r, r)

print()
print("  ═══ ⑤ 真联网工具（走 handler 全链路）═══")
if True:   # 原为 C 风格花括号（Python 不支持）
    web_tools = tools.build_tools(STATE, True)
    r = asyncio.run(tools.run_tool(web_tools, "web_search", '{"query": "Python asyncio 教程", "limit": 3}'))
    print(f"    web_search 结果前 200 字：\n      {r[:200].replace(chr(10), chr(10) + '      ')}")
    check("web_search 返回了结果", "搜索结果" in r or "搜索「" in r, r[:200])
    check("结果里有链接", "http" in r, r[:200])
    check("结果长度被截断在合理范围", len(r) <= tools.MAX_RESULT_CHARS + 100, f"长度 {len(r)}")

    r2 = asyncio.run(tools.run_tool(web_tools, "web_fetch", '{"url": "https://example.com"}'))
    print(f"    web_fetch 结果前 150 字：{r2[:150]!r}")
    check("web_fetch 能读到内容", "Example Domain" in r2 or "example" in r2.lower(), r2[:200])

    r3 = asyncio.run(tools.run_tool(web_tools, "web_fetch", '{"url": "not-a-url"}'))
    check("坏链接给出原因", "读不了" in r3 or "不是" in r3, r3)

shutil.rmtree(STATE, ignore_errors=True)

print()
print(f"  ═══ 结果：{passed}/{passed + len(failed)} 通过 ═══")
if failed:
    for item in failed:
        print(f"    ❌ {item}")
    raise SystemExit(1)
print("  ✅ 全部通过")
