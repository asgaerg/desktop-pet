"""测 Python 侧的浏览器兜底桥（2026-10-13）。

## 为什么用**假 sender**

真链路要 Electron 在跑（Python 做不到执行 JS）。但 Python 侧这一半
（构造请求 / 等回执 / 超时 / 清理）**可以独立测**，而且必须独立测 ——
否则真链路出问题时，分不清是"消息格式错"还是"浏览器读不到"。

假 sender 能验的：

```
✅ 请求构造对不对（type / requestId / url / timeoutMs）
✅ 回执到了能不能唤醒等待者
✅ 超时会不会挂死（必须有超时，否则工具调用会卡到 30 秒上限）
✅ 超时后那条 requestId 有没有被清掉（不清会内存泄漏）
✅ 没人认领的回执不会抛异常
✅ 没注入 sender 时返回"读不了"而不是抛异常
```

⚠️ **字段名必须是 `requestId`（驼峰）** —— 这是本项目的惯例，
主进程的 `requestFromBrain` 从这个键取。写成 `reqId` 的话两边对不上，
表现是**每次都超时**（`memory.backup` 上踩过一次）。这个测试专门盯着它。
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")
from tool_bootstrap import setup  # noqa: E402

setup(verbose=False)

from pet import web  # noqa: E402

passed = 0
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed
    if ok:
        passed += 1
        print(f"  OK   {name}")
    else:
        failed.append(f"{name} —— {detail}")
        print(f"  FAIL {name}")
        print(f"       {detail}")


print("  ═══ ① 没注入 sender 时要给原因，不能抛异常 ═══")
if True:
    web.attach_browser(None)  # type: ignore[arg-type]
    check("没有 sender 时 available() 为 False", web.browser_available() is False)
    result = asyncio.run(web.browser_fetch("https://example.com"))
    check("返回 ok=False", result.get("ok") is False, f"实际 {result}")
    check("给得出原因", bool(result.get("reason")), f"实际 {result.get('reason')!r}")


print()
print("  ═══ ② 请求构造 + 回执唤醒 ═══")
if True:
    captured: list[dict] = []

    def fake_send(message: dict):
        captured.append(message)

    async def run() -> dict:
        web.attach_browser(fake_send)
        task = asyncio.create_task(web.browser_fetch("https://www.qidian.com/book/1/", timeout_s=5))
        # 让任务跑到"已发出请求、正在等"
        await asyncio.sleep(0.05)
        check("请求已发出", len(captured) == 1, f"实际发了 {len(captured)} 条")
        request = captured[0]
        check("type 正确", request.get("type") == "web.browser.fetch", f"实际 {request.get('type')!r}")
        check(
            "**字段名是 requestId（驼峰）**",
            "requestId" in request,
            f"实际键：{sorted(request)}  —— 写成 reqId 会每次都超时",
        )
        check("没有用错名字 reqId", "reqId" not in request)
        check("url 传对了", request.get("url") == "https://www.qidian.com/book/1/")
        check("带了 timeoutMs", isinstance(request.get("timeoutMs"), int), f"实际 {request.get('timeoutMs')!r}")
        check(
            "timeoutMs 比整体超时小（留给建窗口）",
            int(request.get("timeoutMs") or 0) < 5000,
            f"实际 {request.get('timeoutMs')}，整体 5000ms",
        )

        # 模拟主进程回执
        handled = web.resolve_browser_result(
            str(request["requestId"]),
            {"ok": True, "text": "正文内容", "title": "书名", "metas": {"og:novel:author": "作者"}},
        )
        check("回执被认领", handled is True)
        return await task

    result = asyncio.run(run())
    check("拿到了回执内容", result.get("ok") is True and result.get("text") == "正文内容", f"实际 {result}")
    check("metas 带回来了", (result.get("metas") or {}).get("og:novel:author") == "作者")


print()
print("  ═══ ③ 超时必须能结束，而且清干净 ═══")
if True:

    def silent_send(message: dict):
        """发了不回 —— 模拟主进程卡住/崩了。"""
        return None

    async def run_timeout() -> dict:
        web.attach_browser(silent_send)
        web._browser_pending.clear()
        return await web.browser_fetch("https://example.com", timeout_s=0.4)

    result = asyncio.run(run_timeout())
    check("超时返回 ok=False", result.get("ok") is False, f"实际 {result}")
    check("超时给了原因", "超时" in str(result.get("reason")), f"实际 {result.get('reason')!r}")
    check(
        "**超时后 requestId 被清掉了**（不清会泄漏）",
        len(web._browser_pending) == 0,
        f"还剩 {list(web._browser_pending)}",
    )


print()
print("  ═══ ④ 没人认领的回执不该抛异常 ═══")
if True:
    check(
        "从未发过的 requestId 返回 False",
        web.resolve_browser_result("wb999", {"ok": True}) is False,
    )
    check("空 requestId 也不抛", web.resolve_browser_result("", {"ok": True}) is False)
    # 同一个回执回两次：第二次应该返回 False（Future 已经 done）
    web.attach_browser(lambda m: None)

    async def run_twice() -> bool:
        task = asyncio.create_task(web.browser_fetch("https://example.com", timeout_s=3))
        await asyncio.sleep(0.05)
        request_id = next(iter(web._browser_pending))
        first = web.resolve_browser_result(request_id, {"ok": True})
        second = web.resolve_browser_result(request_id, {"ok": True})
        await task
        return first and not second

    check("重复回执第二次被拒", asyncio.run(run_twice()) is True)


print()
print("  ═══ ⑤ `attach_browser` 注入后 available() 为真 ═══")
if True:
    web.attach_browser(lambda m: None)
    check("available() 为 True", web.browser_available() is True)


print()
print(f"  ═══ 结果：{passed}/{passed + len(failed)} 通过 ═══")
if failed:
    for item in failed:
        print(f"    ❌ {item}")
    raise SystemExit(1)
print("  ✅ 全部通过")
