"""全链路验证：侧车启动 -> 拉起 MCP -> 搜 -> 退出时收掉（2026-10-15）。

## 测什么

```
① 侧车启动时会把 MCP 服务器拉起来（main._run 里那段）
② 开关同步到了 tools 模块（gateway._sync_mcp_search_flag）
③ 侧车退出时会把服务器收掉（main._run 的 finally）
④ 崩溃后重启**不累积**进程（自愈）
```

## 🔴 怎么让侧车"优雅退出"

第一版用 `Popen.terminate()` —— **错**。Windows 上它是 `TerminateProcess`：

```
硬杀 -> Python 的 signal handler 不跑 -> try/finally 不跑 -> stop() 不执行
     -> 测试报"服务器没收掉"，看起来像产品 bug，其实是测试用错了关停方式
```

⚠️ 换成 `CTRL_C_EVENT` 也不行（实测 `loop.add_signal_handler` 在 Windows
对 SIGINT 抛 NotImplementedError，退回 `signal.signal` 后**并不总能生效**，
而且进程可能挂 40 秒不退）。

✅ **真正可靠的办法：不靠信号。** 退出路径上 `main._run` 的 `finally`
一定会跑（除非硬杀），所以这里**只验 ①②④**，
③（收掉）由 `tools/test-mcp-lifecycle.py` 直接测 `stop()` 来覆盖 ——
那个测试不依赖任何信号。
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import time

ROOT = pathlib.Path(r"F:\ai项目")
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
from tool_bootstrap import setup  # noqa: E402

setup(verbose=False)
sys.path.insert(0, str(ROOT / "apps" / "brain"))
from pet import mcp_search  # noqa: E402

PYTHON = str(ROOT / ".venv" / "Scripts" / "python.exe")
LOG = ROOT / "apps" / "desktop" / "pet-data" / "mcp-e2e.log"
PORT = 8338

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


def listening(port: int = PORT) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def server_pids() -> list[int]:
    return mcp_search._server_process_ids()


def cleanup() -> None:
    mcp_search._reap_orphans()
    time.sleep(1)


print("=" * 84)
print("① 清场")
print("=" * 84)
cleanup()
check("8338 空着", not listening(), "有东西占着")
check("没有残留的 websearch-mcp", not server_pids(), f"残留 {server_pids()}")

print()
print("=" * 84)
print("② 起侧车 —— 它应该自己拉起 MCP 服务器")
print("=" * 84)
if LOG.exists():
    LOG.unlink()
env = {**os.environ, "PYTHONPATH": str(ROOT / "apps" / "brain"),
       "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
proc = subprocess.Popen(
    [PYTHON, "-m", "pet", "--port", "0",
     "--config", str(ROOT / "config" / "persona.yaml"), "--log-file", str(LOG)],
    cwd=str(ROOT / "apps" / "brain"), env=env,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True, encoding="utf-8", errors="replace",
)

ready = ""
deadline = time.time() + 90
while time.time() < deadline:
    line = proc.stdout.readline() if proc.stdout else ""
    if not line:
        if proc.poll() is not None:
            break
        time.sleep(0.1)
        continue
    if line.startswith("PET_READY "):
        ready = line.strip()
        break

if not ready:
    print("    ❌ 侧车没就绪")
    proc.kill()
    raise SystemExit(1)
info = json.loads(ready[len("PET_READY "):])
print(f"    ✅ 侧车就绪：pid {info['pid']}，ws 端口 {info['port']}")
check("MCP 服务器被侧车拉起来了（8338 在监听）", listening(),
      "侧车启动了但没拉起 MCP —— main._run 里那段没生效")
check("有且只有一个 websearch-mcp 进程", len(server_pids()) == 1,
      f"实际 {len(server_pids())} 个：{server_pids()}")

log_text = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
check("日志里有「MCP 搜索服务器已就绪」", "MCP 搜索服务器已就绪" in log_text,
      "没看到就绪记录")

print()
print("=" * 84)
print("③ 通过 WebSocket 让它真搜一次（走 tools 那条路）")
print("=" * 84)
try:
    # 项目自带的 WebSocket 客户端（手写握手/帧，不依赖 `websockets` 包）
    from pet.testing import TestClient  # noqa: PLC0415

    import asyncio

    async def drive() -> list[str]:
        client = TestClient("127.0.0.1", info["port"])
        await client.connect()
        await client.send({"type": "user.text", "text": "帮我搜一下银松镇是什么游戏"})
        types: list[str] = []
        deadline = asyncio.get_running_loop().time() + 90
        while asyncio.get_running_loop().time() < deadline:
            try:
                message = await client.recv(timeout=5)
            except asyncio.TimeoutError:
                break
            types.append(str(message.get("type")))
            if message.get("type") in ("conversation.finished", "pet.say.finished"):
                break
        await client.close()
        return types

    seen = asyncio.run(asyncio.wait_for(drive(), timeout=150))
    print(f"    收到 {len(seen)} 条消息：{seen[:10]}")
    check("整条对话跑通了（收到了消息）", bool(seen), "一条都没收到")
except Exception as exc:  # noqa: BLE001
    print(f"    ⚠️ WebSocket 那条路没跑通：{type(exc).__name__}: {exc}")
    print("       （日志里另有判据，下面会看）")

log_text = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
check("工具清单里有 web_search", "web_search" in log_text, "工具没启用")

"""
## ⚠️ 为什么不断言"模型去调了 web_search"

第一版让侧车收一句「帮我搜一下银松镇是什么游戏」，然后断言日志里有
`web_search q=...`。**失败了 —— 但那不是产品 bug。**

大模型**不一定会调工具**：它对"银松镇是什么游戏"完全可以直接凭训练数据答
（实测它就是这么做的 —— 收到了 `pet.say.start` 说明它在正常回话）。

⚠️ 这踩的是本项目记过好几次的坑：**"驱动功能"要通过"让模型做某事"来实现，
那是不可靠的**。测试要打在确定的东西上。

✅ 所以这里改成：**直接调工具**（`tools.run_tool`），那条路是确定的。
开关同步（gateway -> tools 模块）用 `set_mcp_search` 的可见状态来验。
"""
import asyncio  # noqa: E402
import pathlib  # noqa: E402

from pet import tools as pet_tools  # noqa: E402

state_dir = ROOT / "apps" / "desktop" / "pet-data" / "_mcp-e2e-state"
state_dir.mkdir(parents=True, exist_ok=True)
tool_list = pet_tools.build_tools(state_dir, allow_web=True, allow_browser=False)
pet_tools.set_mcp_search(True)
output = asyncio.run(pet_tools.run_tool(
    tool_list, "web_search", json.dumps({"query": "银松镇 游戏"}, ensure_ascii=False)
))
print(f"    直接调 web_search 返回 {len(output)} 字：{output[:100]!r}")
check("直接调 web_search 走通了 MCP（返回 #搜索结果 / **来源**）",
      "#搜索结果" in output or "**来源**" in output,
      f"看起来不像 MCP 的输出：{output[:200]!r}")

print()
print("=" * 84)
print("④ 崩溃后重启：不该累积进程（自愈）")
print("=" * 84)
print("    硬杀侧车（模拟崩溃）…")
proc.kill()
proc.wait(timeout=10)
orphans = server_pids()
print(f"    侧车硬杀后，MCP 服务器剩 {len(orphans)} 个（预期 1 —— 硬杀不会执行 stop）：{orphans}")

# 再起一个侧车，它应该**复用**或**清掉再拉**，总之不累积
#
# ⚠️ 用**另一个日志文件**：第一个侧车刚被硬杀，它的文件句柄可能还没释放，
#    往同一个文件上 unlink 会 `PermissionError: WinError 32`。
LOG2 = LOG.with_name("mcp-e2e-2.log")
if LOG2.exists():
    try:
        LOG2.unlink()
    except OSError:
        pass
proc2 = subprocess.Popen(
    [PYTHON, "-m", "pet", "--port", "0",
     "--config", str(ROOT / "config" / "persona.yaml"), "--log-file", str(LOG2)],
    cwd=str(ROOT / "apps" / "brain"), env=env,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True, encoding="utf-8", errors="replace",
)
ready2 = ""
deadline = time.time() + 90
while time.time() < deadline:
    line = proc2.stdout.readline() if proc2.stdout else ""
    if not line:
        if proc2.poll() is not None:
            break
        time.sleep(0.1)
        continue
    if line.startswith("PET_READY "):
        ready2 = line.strip()
        break
check("第二个侧车也起来了", bool(ready2), "没就绪")
after = server_pids()
print(f"    重启后 websearch-mcp 进程数：{len(after)}  {after}")
check("🔴 重启后没有累积（<=1 个）", len(after) <= 1,
      f"累积成 {len(after)} 个：{after} —— 这就是「关掉 App 后删不掉文件夹」的成因")
check("重启后服务器可用", listening(), "8338 没监听")

print()
print("=" * 84)
print("⑤ 收尾")
print("=" * 84)
proc2.kill()
proc2.wait(timeout=10)
cleanup()
print(f"    收尾后 pids={server_pids()}  8338={listening()}")
check("收尾清干净", not server_pids() and not listening(), "还有残留")

print()
print("=" * 84)
total = PASSED + len(FAILED)
if FAILED:
    print(f"  ❌ 结果：{PASSED}/{total} 通过")
    for item in FAILED:
        print(f"      - {item}")
    raise SystemExit(1)
print(f"  ✅ 结果：{PASSED}/{total} 通过")
