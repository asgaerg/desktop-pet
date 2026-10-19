"""MCP 搜索后端：接入验证 + 退路验证（2026-10-15）。

## 为什么需要这个测试

`test-web-search.py` 现在可能走的是 **MCP**，而它自己不知道 ——
它只断言「搜到东西了吗」，两条后端都能让它通过。

⚠️ **"测试通过"不等于"MCP 生效了"。** 这是本项目的常客：
§101 记过「靠巧合通过」的教训。

所以这个测试要断言的是**走的是哪条后端**，而不只是有没有结果。

## 测四件事

```
① MCP 服务器能起来（ensure_running）
② 开关打开时：web_search 走 MCP（从日志/返回文本特征判断）
③ 开关关闭时：web_search 退回自抓 HTML（结果里要有「来自 xxx」的引擎名）
④ MCP 不可用时（URL 指到死端口）：**不报错**，静默退回自抓
```

🔴 ④ 是最重要的一条 —— MCP 是个外部 exe，打包后可能在也可能不在，
退路必须真的работает，否则用户的搜索会直接坏掉。

## 怎么区分两条后端

```
MCP 后端   返回「#搜索结果 / 查询: ... / **来源**: so360 / **相关性**:」
自抓后端   返回「搜索「xxx」（来自 duckduckgo）的结果：/ 1. 标题」
```

这两个特征很稳 —— 一个是给 AI 优化的 Markdown，一个是我自己拼的编号列表。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(r"F:\ai项目")
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
from tool_bootstrap import setup  # noqa: E402

setup(verbose=False)

sys.path.insert(0, str(ROOT / "apps" / "brain"))
from pet import mcp_search, tools  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  OK   {name}")
    else:
        FAILED.append(f"{name} —— {detail}")
        print(f"  FAIL {name}\n       {detail}")


def looks_like_mcp(text: str) -> bool:
    """MCP 后端返回的特征。"""
    return "#搜索结果" in text or "**相关性**" in text or "**来源**" in text


def looks_like_own(text: str) -> bool:
    """自抓后端返回的特征（`web.py` 拼的格式）。"""
    return "（来自 " in text or "的结果：" in text


QUERY = "银松镇 游戏"


async def main() -> int:
    state = ROOT / "apps" / "desktop" / "pet-data" / "_mcp-test"
    if state.exists():
        shutil.rmtree(state, ignore_errors=True)
    state.mkdir(parents=True, exist_ok=True)

    tool_list = tools.build_tools(state, allow_web=True, allow_browser=False)

    print("=" * 84)
    print("① MCP 服务器能找到、能起来")
    print("=" * 84)
    exe = mcp_search.find_exe()
    print(f"    runtime_dir = {mcp_search.runtime_dir()}")
    print(f"    exe         = {exe}")
    check("找得到 websearch-mcp.exe", exe is not None and exe.is_file(), f"实际 {exe}")
    check("运行目录在 pet-data 下面（不污染产品目录）",
          "pet-data" in str(mcp_search.runtime_dir()),
          f"运行目录是 {mcp_search.runtime_dir()} —— 它会在那里写日志/数据库/配置")

    running = await asyncio.to_thread(mcp_search.ensure_running)
    check("ensure_running() 成功（服务器在跑）", running, "起不来")
    if not running:
        print("\n    ❌ 服务器起不来，后面的测试没意义")
        return 1

    check("probe() 能完成一次 MCP 握手", await asyncio.to_thread(mcp_search.probe))
    check("服务器进程数正好 1（没有残留累积）",
          len(mcp_search._server_process_ids()) == 1,
          f"实际 {mcp_search._server_process_ids()}")

    print()
    print("=" * 84)
    print("② 开关打开 -> 走 MCP")
    print("=" * 84)
    tools.set_mcp_search(True)
    check("tools.mcp_search_is_enabled() 为真", tools.mcp_search_is_enabled())
    text_mcp = await tools.run_tool(
        tool_list, "web_search", json.dumps({"query": QUERY}, ensure_ascii=False)
    )
    print(f"    返回 {len(text_mcp)} 字，开头：{text_mcp[:90]!r}")
    check("返回的是 MCP 格式（#搜索结果 / **来源**）", looks_like_mcp(text_mcp),
          f"看起来不像 MCP：{text_mcp[:200]!r}")
    check("返回里带来源标记（so360 / bing）",
          "so360" in text_mcp or "bing" in text_mcp,
          f"没有来源标记：{text_mcp[:200]!r}")

    print()
    print("=" * 84)
    print("③ 开关关闭 -> 退回自抓 HTML")
    print("=" * 84)
    tools.set_mcp_search(False)
    check("tools.mcp_search_is_enabled() 为假", not tools.mcp_search_is_enabled())
    text_own = await tools.run_tool(
        tool_list, "web_search", json.dumps({"query": QUERY}, ensure_ascii=False)
    )
    print(f"    返回 {len(text_own)} 字，开头：{text_own[:90]!r}")
    check("返回的是自抓格式（「来自 xxx」/ 编号列表）", looks_like_own(text_own),
          f"看起来不像自抓：{text_own[:200]!r}")
    check("自抓那条路**没有**MCP 的特征", not looks_like_mcp(text_own),
          "关掉开关却还是 MCP 的输出 —— 那就是个假开关")

    print()
    print("=" * 84)
    print("④ 🔴 MCP 不可用时必须静默退回（最重要的一条）")
    print("=" * 84)
    # 把 URL 指到一个确定没人监听的端口
    import socket

    probe_sock = socket.socket()
    probe_sock.bind(("127.0.0.1", 0))
    dead_port = probe_sock.getsockname()[1]
    probe_sock.close()

    tools.set_mcp_search(True)
    original_search = mcp_search.search
    original_ensure = mcp_search.ensure_running

    def dead_search(query, limit=6, **kwargs):
        return original_search(query, limit, url=f"http://127.0.0.1:{dead_port}/mcp", **kwargs)

    mcp_search.search = dead_search  # type: ignore[assignment]
    try:
        text_dead = await tools.run_tool(
            tool_list, "web_search", json.dumps({"query": QUERY}, ensure_ascii=False)
        )
    finally:
        mcp_search.search = original_search  # type: ignore[assignment]

    print(f"    MCP 指向死端口 {dead_port} 时的返回：{text_dead[:110]!r}")
    check("MCP 不可用时**没有**抛异常（工具正常返回文本）",
          isinstance(text_dead, str) and len(text_dead) > 20,
          f"返回异常：{text_dead!r}")
    check("MCP 不可用时**退回了自抓**（不是报错文本）", looks_like_own(text_dead),
          f"没退回自抓：{text_dead[:200]!r}")
    check("没有把「错误」字样直接扔给模型",
          not text_dead.startswith("错误") and "McpError" not in text_dead,
          f"把内部错误泄露给模型了：{text_dead[:200]!r}")

    # 恢复默认（开）
    tools.set_mcp_search(True)
    _ = original_ensure

    print()
    print("=" * 84)
    print("⑤ 🔴 服务器中途挂掉 -> search() 必须自己救回来（2026-10-15 实测补）")
    print("=" * 84)
    """
    ⚠️ 为什么要测这一条 —— 真实场景：

    App 开着的时候服务器**中途自己退了**（实测：22:01:05 还在服务，
    22:03 就没了，而侧车和 Electron 都活着）。

    原来的 `search()` 只连一次，连不上就抛错、上层退回自抓 ——
    于是**本次会话永久降级**（1.5 秒 -> 18.5 秒），而且没有任何提示。
    用户只会觉得"搜索越来越慢"，重启才好，却不知道为什么。

    ✅ 现在 `search()` 失败时会 `ensure_running()` 重启并重试一次。
    这条测试就是锁住那个行为。
    """
    killed = await asyncio.to_thread(mcp_search._reap_orphans)
    check("前置：确实把服务器杀掉了", killed >= 1 and not await asyncio.to_thread(mcp_search.probe),
          f"killed={killed}")

    try:
        text_heal = await asyncio.to_thread(mcp_search.search, QUERY)
        check("🔴 服务器挂掉后 search() 自己救回来了（没抛异常）",
              looks_like_mcp(text_heal),
              f"返回：{text_heal[:200]!r}")
        check("救回来之后服务器确实在跑",
              await asyncio.to_thread(mcp_search.probe),
              "probe 还是 False")
        check("没有累积进程（<=1 个）",
              len(mcp_search._server_process_ids()) <= 1,
              f"实际 {mcp_search._server_process_ids()}")
    except Exception as exc:  # noqa: BLE001
        check("🔴 服务器挂掉后 search() 自己救回来了（没抛异常）", False,
              f"{type(exc).__name__}: {exc}")

    print()
    print("=" * 84)
    total = PASSED + len(FAILED)
    if FAILED:
        print(f"  ❌ 结果：{PASSED}/{total} 通过")
        for item in FAILED:
            print(f"      - {item}")
        return 1
    print(f"  ✅ 结果：{PASSED}/{total} 通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
