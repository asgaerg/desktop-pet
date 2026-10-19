"""验证联网搜索**会通知状态条**（2026-10-12 用户要求）。

## 用户原话

> 「宠物联网搜索的时候状态栏不会变，没有显示联网搜索中」

## 修的是什么

状态条的 UI **全都是现成的**：

    · `StatusKind = 'idle' | 'searching' | 'error'`    早已定义
    · `.light[data-state='searching']` 琥珀色 + 呼吸动画  早已写好
    · `StatusWindow.setStatus()` / `pushState()`       早已实现

**缺的只是「没人调用它」**（主进程那个共用的 `setStatus` 只更新了托盘）。

现在整条链是：

    tools.run_tool  -> bus.publish('pet.tool', start/end)
                    -> 主进程 message 处理 -> statusWindow.setStatus(kind, text)
                    -> 渲染层换灯色 + 文字

## 这个脚本验哪一段

**侧车那一段**（`run_tool` 发不发事件、顺序对不对）。
主进程那一段靠 `npm test` 的断言。

### ⚠️ 最要紧的一条：**失败时也必须发 end**

只在成功路径发 end 的话，工具抛异常时状态条会**永远停在「搜索中」**——
那比不改还糟，因为用户会以为它卡住了。
"""

from __future__ import annotations

import asyncio
import shutil
import sys

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

from pet import tools  # noqa: E402
from pet.bus import Bus  # noqa: E402

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


async def collect(bus: Bus, coro) -> list[dict]:
    events: list[dict] = []

    async def collector(event) -> None:
        events.append({"name": event.name, "payload": event.payload or {}})

    unsub = bus.subscribe("*", collector)
    try:
        await coro
    finally:
        unsub()
    return [e for e in events if e["name"] == "pet.tool"]


async def main() -> int:
    states = ROOT / "apps" / "desktop" / "pet-data" / "_status-test"
    if states.exists():
        shutil.rmtree(states, ignore_errors=True)
    states.mkdir(parents=True, exist_ok=True)

    web_tools = tools.build_tools(states, allow_web=True)
    local_tools = tools.build_tools(states, allow_web=False)

    print("  ═══ ① web_search 要发 start + end ═══")
    bus = Bus()
    events = await collect(
        bus,
        tools.run_tool(web_tools, "web_search", '{"query": "银松镇 游戏"}', bus),
    )
    print(f"    收到 {len(events)} 个 pet.tool 事件")
    for e in events:
        print(f"      {e['payload'].get('phase')} tool={e['payload'].get('tool')} "
              f"detail={e['payload'].get('detail')!r}")
    check("发了 2 个（start + end）", len(events) == 2, f"实际 {len(events)}")
    check("第一个是 start",
          bool(events) and events[0]["payload"].get("phase") == "start")
    check("最后一个是 end",
          bool(events) and events[-1]["payload"].get("phase") == "end",
          "**少了 end 状态条会永远卡在「搜索中」**")
    check("tool 名是 web_search",
          bool(events) and events[0]["payload"].get("tool") == "web_search")
    check("detail 是搜索词",
          bool(events) and events[0]["payload"].get("detail") == "银松镇 游戏",
          f"实际 {events[0]['payload'].get('detail')!r}" if events else "")
    check("detail 被截断到 28 字以内",
          bool(events) and len(str(events[0]["payload"].get("detail") or "")) <= 28)

    print()
    print("  ═══ ② web_fetch 也要发（detail 是网址）═══")
    bus2 = Bus()
    events2 = await collect(
        bus2,
        tools.run_tool(web_tools, "web_fetch", '{"url": "https://example.com"}', bus2),
    )
    for e in events2:
        print(f"      {e['payload'].get('phase')} detail={e['payload'].get('detail')!r}")
    check("web_fetch 发了 2 个", len(events2) == 2, f"实际 {len(events2)}")
    check("detail 去掉了 https:// 前缀",
          bool(events2) and not str(events2[0]["payload"].get("detail") or "").startswith("http"),
          f"实际 {events2[0]['payload'].get('detail')!r}" if events2 else "")

    print()
    print("  ═══ ③ 🔴 失败时**也必须**发 end（否则状态条永远卡住）═══")
    bus3 = Bus()
    # 参数是坏 JSON -> 走 early return。这条路径原来会**直接返回**，
    # 连 start 都没发 —— 但那种情况没发 start 也就不需要 end。
    events3 = await collect(
        bus3,
        tools.run_tool(web_tools, "web_fetch", "这不是 JSON", bus3),
    )
    print(f"    坏 JSON：收到 {len(events3)} 个（0 个是对的 —— 没发 start 就不需要 end）")
    check("坏 JSON 不发事件（没开始就不用结束）", len(events3) == 0,
          f"实际 {len(events3)}")

    # 真正要验的是：handler **抛异常**时有没有发 end
    bus4 = Bus()

    async def boom(args):
        raise RuntimeError("模拟工具内部崩溃")

    broken = list(web_tools)
    for i, t in enumerate(broken):
        if t.name == "web_search":
            broken[i] = tools.Tool(
                name=t.name, description=t.description, parameters=t.parameters, handler=boom
            )
    events4 = await collect(
        bus4,
        tools.run_tool(broken, "web_search", '{"query": "会崩的"}', bus4),
    )
    print(f"    工具崩溃：收到 {len(events4)} 个："
          f"{[e['payload'].get('phase') for e in events4]}")
    check("**工具崩溃时也发了 end**（try/finally）", len(events4) == 2,
          f"实际 {len(events4)} —— 少了 end 状态条会永远停在「搜索中」")
    check("最后一个仍是 end",
          bool(events4) and events4[-1]["payload"].get("phase") == "end")

    print()
    print("  ═══ ④ 沙箱工具**不上报**（毫秒级完成，闪一下像故障）═══")
    bus5 = Bus()
    events5 = await collect(
        bus5,
        tools.run_tool(local_tools, "sandbox_write",
                       '{"name": "n.md", "content": "x"}', bus5),
    )
    check("sandbox_write 不发 pet.tool", len(events5) == 0, f"实际 {len(events5)}")

    print()
    print("  ═══ ⑤ 不传 bus 时**完全不上报**（测试里不需要它）═══")
    text = await tools.run_tool(web_tools, "web_search", '{"query": "test"}')
    check("不传 bus 也能正常执行", bool(text), f"实际 {text[:60]!r}")

    """
    ## ⑥ 🔴 登记的通道 vs 主进程真的会读的（2026-10-16 补）

    这一节是**因为一个真 bug 才加的**：用户报「联网搜索时状态栏不显示」。

    ```
    tools.run_tool()  一直在发 pet.tool          ✅
    gateway.py        _BUS_TO_WIRE 里**没登记**  ❌ <- 死在这里
    index.ts          一直在收 pet.tool          ✅
    ```

    `_on_bus_event` 对没登记的事件直接 `return`（静默丢弃），
    所以整条链在中间断了 —— 而**这个测试前面五节全是通过的**。

    ⚠️ 断点正好落在「两个测试的交界处」：
      · 本测试验侧车**发不发**（发）
      · 主进程测试验收到后**切成什么状态**（切）
      · **没人验中间那根线接上了没有**

    所以这里直接把两端对起来比。
    """
    print()
    print("  ═══ ⑥ 🔴 事件名字登记了吗（发了但没登记 = 静默丢弃）═══")
    import re
    from pathlib import Path

    gateway_src = (ROOT / "apps" / "brain" / "pet" / "gateway.py").read_text(encoding="utf-8")
    registered = set(re.findall(r'^\s*"([a-z][a-z_.]+)":\s*"', gateway_src, re.M))
    print(f"    _BUS_TO_WIRE 里登记了 {len(registered)} 个：{sorted(registered)}")

    """
    ## 🔴🔴 用 AST 验一遍（正则看不出"粘起来的字符串"）

    这里有个**极隐蔽**的坑，2026-10-16 我本人踩了**两次**（一次是产品代码，
    一次是这个测试的注释本身）：

    ```python
    _BUS_TO_WIRE = {
        "pet.mouth": "pet.mouth",
        <三个引号> ...一段说明... <三个引号>
        "pet.tool": "pet.tool",      # <- 以为登记了
    }
    ```

    ⚠️ Python 的**相邻字符串字面量自动拼接**把那段三引号文档和 `"pet.tool"`
    **粘成了一个字符串**，于是真正的 key 是那一整段文档，`pet.tool` 查不到。

    ```
    实际 key = '\\n    ...说明...\\n    pet.tool'
    _BUS_TO_WIRE["pet.tool"] -> None      # 修了等于没修
    ```

    **症状**：正则搜 `"pet.tool":` 能搜到（源码里确实有），
    但运行时字典里没有。所以**正则不够，必须用 AST 看编译后的 key**。

    ⚠️ 老代码里的 `voice.tts_failed` 就是长期受害者 —— 它的 key 也是整段
    文档，所以那条"TTS 合成失败"消息**从来没被广播过**。

    ⚠️ 连这个测试文件本身都中了一次：上面那段示例里写了三引号，
    把外层文档串**提前闭合**了 -> SyntaxError。
    所以下面改成用 `<三个引号>` 这种写法描述，不写真字符。
    """
    import ast as _ast

    tree = _ast.parse(gateway_src)
    ast_keys: list[str] = []
    for node in tree.body:
        if isinstance(node, _ast.AnnAssign) and getattr(node.target, "id", "") == "_BUS_TO_WIRE":
            for key in node.value.keys:  # type: ignore[attr-defined]
                value = getattr(key, "value", None)
                if isinstance(value, str):
                    ast_keys.append(value)
            break
    print(f"    AST 解析出的 key（{len(ast_keys)} 个）：{sorted(ast_keys)}")

    weird = [k for k in ast_keys if len(k) > 30 or "\n" in k or " " in k]
    check("字典里没有『文档串粘成 key』的垃圾项",
          not weird,
          f"发现 {len(weird)} 个异常 key（都是长文本）—— "
          f"这是**相邻字符串自动拼接**吃掉了真 key。"
          f"把说明改成 `#` 注释，别用裸的三引号字符串。"
          f"第一个：{weird[0][:60]!r}…" if weird else "")

    check("AST 里的 key 和正则扫到的一致（正则没骗人）",
          set(ast_keys) == registered,
          f"正则={sorted(registered - set(ast_keys))} 只存在于正则；"
          f"AST={sorted(set(ast_keys) - registered)} 只存在于 AST")

    # `tools.py` 会发布这些 —— 每一个都必须在 _BUS_TO_WIRE 里，否则永远到不了前端
    published_by_tools = {"pet.tool"}
    for name in sorted(published_by_tools):
        check(f"{name} 已登记进 _BUS_TO_WIRE（否则静默丢弃）",
              name in registered,
              f"没登记！`_on_bus_event` 会直接 return，"
              f"于是 index.ts 里那段 `message.type === '{name}'` **永远不会执行**")

    """
    ## 两条完全不同的来路（分清楚才不会误报）

    ```
    A. 侧车广播给所有人     bus.publish -> _BUS_TO_WIRE -> broadcast -> 渲染层
                           pet.say.* / pet.emotion / pet.tool / voice.state / ...

    B. 侧车**点名请求**主进程  直接 send_to（不进渲染层，也不走 bus）
                           tts.synthesize / web.browser.fetch / ...
    ```

    ⚠️ B 类**故意**不登记进 `_BUS_TO_WIRE`：它们的终点是主进程自己
    （本地 SAPI 合成、用隐藏窗口读网页 —— 这些渲染层做不到）。
    协议那边也标了「不进 `TO_UI_TYPES` 白名单」。

    ⚠️ 所以这个测试不能简单地"没登记就报错"——
    那会把 B 类误判成 bug。下面按类分开查。
    """
    index_src = (ROOT / "apps" / "desktop" / "src" / "main" / "index.ts").read_text(
        encoding="utf-8"
    )
    consumed = sorted(set(re.findall(r"message\.type === '([a-z][a-z_.]+)'", index_src)))
    print(f"    主进程 index.ts 读这些类型：{consumed}")

    # B 类：侧车点名请求主进程（走 send_to / 直接应答），**不该**登记进 _BUS_TO_WIRE
    NOT_VIA_BUS = {
        "voice.audio",          # 二进制帧 + JSON 头单独配对
        "memory.backup.done",   # 应答主进程的备份请求
        "memory.result",        # 应答主进程的记忆查询
        "tts.synthesize",       # Python 发起：帮我用本地 SAPI 合成（渲染层做不到）
        "web.browser.fetch",    # Python 发起：帮我用隐藏窗口读网页（渲染层做不到）
    }
    # A 类里**不走 _BUS_TO_WIRE** 的特例（主进程本地产生的）
    LOCAL_ONLY = {"ready"}      # SidecarManager 的 event，不是线上消息

    known_consumed = {
        "pet.say.start", "pet.say.delta", "pet.say.end", "pet.emotion",
        "pet.thinking", "pet.observe", "pet.mouth", "pet.tool",
        "system.error", "voice.state", "voice.tts_failed",
    } | NOT_VIA_BUS | LOCAL_ONLY

    unknown = [t for t in consumed if t not in known_consumed]
    check("没有出现不认识的新消息类型（出现了就来这里分类）",
          not unknown,
          f"新类型 {unknown} —— 先判断它走 bus 还是侧车点名请求，再归到对应清单")

    for name in consumed:
        if name in NOT_VIA_BUS or name in LOCAL_ONLY:
            continue
        check(f"A 类 {name} 已登记（否则 index.ts 那段是死代码）",
              name in registered,
              f"`{name}` 没登记 —— 主进程里 `message.type === '{name}'` 那段**永远不执行**")

    """
    ⚠️ 反向还差一环 —— 而且是**最容易出事**的那一环。

    消息要真正到达渲染层，得过**两道**闸门：

    ```
    ① gateway  _BUS_TO_WIRE     没登记 -> 在 Python 侧就被丢掉
    ② protocol TO_UI_TYPES      不在白名单 -> 在 parseMessage 里被丢掉
    ```

    ⚠️ 两道闸门**都会静默丢弃**，而且都在"另一个文件"里 ——
    所以"两边各自都写好了"的时候，特别容易中间断一根。
    这里把两道闸门和真实的发布方三者对起来。
    """
    protocol_src = (ROOT / "packages" / "protocol" / "src" / "index.ts").read_text(
        encoding="utf-8"
    )
    block = re.search(r"const TO_UI_TYPES = new Set<string>\(\[(.*?)\]\)", protocol_src, re.S)
    # ⚠️ 只抓**整个字符串字面量**独占一行的那种，避免把 `'start'`/`'end'`
    # 这类**别的用途**的字面量（比如 `phase === 'start'`）混进白名单。
    # 实测正则放松了就会把 start / end / web_search / web_fetch 也算成消息类型。
    whitelist = set(
        re.findall(r"^\s*'([a-z][a-z_]*\.[a-z_.]+)',\s*$", block.group(1), re.M)
    ) if block else set()
    print(f"    protocol TO_UI_TYPES 白名单有 {len(whitelist)} 个")

    # `_BUS_TO_WIRE` 的**值**就是真正发到线上的类型名
    wire_values = set(re.findall(r'^\s*"[a-z][a-z_.]+":\s*"([a-z][a-z_.]+)"', gateway_src, re.M))

    """
    ⚠️ **故意的例外**：广播是"发给所有人"，但有些消息只有主进程该处理。

    ```
    broadcast()  ->  所有连着的客户端（主进程 + 渲染层）**都收到**
    ```

    所以有些类型**故意不进 `TO_UI_TYPES`** —— 渲染层收到后按白名单丢掉，
    正好实现"只给主进程看"。协议的注释里就是这么写的
    （"它的终点是**主进程自己**……不进白名单"）。

    ⚠️ 我第一版断言写成"广播的必须在白名单里"，把这条误报成 bug 了。
    分清"漏登记"（真 bug）和"故意不给渲染层"（设计）很重要 ——
    否则测试会逼着人把不该给的权限给出去。
    """
    MAIN_ONLY_BROADCAST = {
        "voice.tts_failed",  # 只给主进程：据此把回复降级成"显示文字"
    }

    for name in sorted(wire_values):
        if name in MAIN_ONLY_BROADCAST:
            check(f"广播的 {name} **故意**不在白名单（只给主进程）",
                  name not in whitelist,
                  f"`{name}` 进了白名单 —— 那渲染层也会处理它，"
                  f"确认这是想要的（协议注释说它只该给主进程）")
            continue
        check(f"广播的 {name} 在 TO_UI_TYPES 白名单里",
              name in whitelist,
              f"`{name}` 会被 `parseMessage` 静默丢弃 —— "
              f"Python 发了、渲染层收不到，而且**两边都不会报错**")

    """
    ⚠️ 最后一项：白名单里有没有**没人发**的过期项。

    ⚠️ 抓 `publish("x")` 会把 `publish("web_search", ...)` 这种
    **工具名**也抓进来 —— 所以下面按"是不是真的像事件名"过滤，
    宁可有几个"留观"也不能误报（假阳性会让人开始不信这个测试）。
    """
    published: set[str] = set()
    for path in (ROOT / "apps" / "brain" / "pet").glob("*.py"):
        for hit in re.findall(r'publish\(\s*"([a-z][a-z_.]+)"', path.read_text(encoding="utf-8")):
            if "." in hit:  # 事件名都带点（pet.say.start）；工具名没有
                published.add(hit)
    print(f"    侧车 publish() 的事件名 {len(published)} 个")

    stale = []
    for name in sorted(whitelist):
        if name in published or name in wire_values or name in NOT_VIA_BUS:
            continue
        if name in {"ready", "user.voice", "memory.cleared", "voice.say",
                    "web.browser.result", "web.browser.fetch", "tts.synthesize",
                    "memory.result", "memory.backup.done"}:
            continue  # 已知的"别的发法"（主进程本地产生 / 点名请求 / 回执）
        stale.append(name)
    check("白名单里没有说不清来历的项", not stale,
          f"{stale} —— 确认是谁发的；说不清就可以考虑删掉（那是过期权限）")

    shutil.rmtree(states, ignore_errors=True)
    print()
    print(f"  ═══ 结果：{passed}/{passed + len(failed)} 通过 ═══")
    if failed:
        for item in failed:
            print(f"    ❌ {item}")
        return 1
    print("  ✅ 全部通过")
    return 0


raise SystemExit(asyncio.run(main()))
