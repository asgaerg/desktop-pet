"""
给模型用的工具（function calling，2026-10-12）。

## 这是什么

用户要「联网功能：让他自己去网上搜索，就像小鲸你一样，自己调用」。
实测确认两件硬事实：

  · **模型支持函数调用**（`deepseek-flash` 一次就发了两个 `web_search` 调用）
  · **模型自己不能联网**（它的知识停在 2024-06，而现在是 2026-10）

所以"能联网"必须由我们提供工具，模型只负责**决定搜什么**。

## 🔴 安全边界：**永远不给 shell**

用户问过「shell 是什么」，答复记录在这里 —— 这是**安全边界**，别将来顺手加上：

    shell = 让 AI 执行系统命令（dir / del / powershell -Command / curl | python -）

**一旦给了它，「只写沙箱目录」就在技术上失效**：

    我们写：只允许写 pet-data/web-sandbox/
    它执行：cd ..\\..\\.. && del /f /q config\\persona.yaml

**路径检查拦不住命令里的路径。** 所以工具只有这几个，没有 shell：

    web_search(query)           搜索（Bing 主 / 360 备）
    web_fetch(url)              读网页正文
    sandbox_write(name, text)   写进沙箱目录（经路径检查）
    sandbox_read(name)          读沙箱目录里的文件

这样「不许越界」才是**真的能保证的** —— 所有写入都走同一个函数，
它 `resolve()` 后确认仍在沙箱内。

## 工具描述怎么写

`description` 是**写给模型看的**，不是写给人看的。它决定模型什么时候会调、
什么时候不调。所以：

  · 说清**什么时候该用**（"需要当前信息或你不确信时"）——
    光说"搜索"模型会在闲聊时也去搜
  · 说清**什么时候不该用**（"用户只是闲聊时不要用"）——
    用户明确要求「提示词写少搜」
  · 参数说明要具体（"搜索关键词，用中文或英文都行"）
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from . import mcp_search, web
from .protocol import msg

log = logging.getLogger("pet.tools")

"""
## MCP 搜索开关（2026-10-15）

`websearch-mcp.exe` 当联网搜索的后端。**默认开**，因为它是主路径。

⚠️ 为什么用模块级变量而不是「让 `build_tools()` 收一个参数」：

`build_tools()` 的结果**带缓存**（`gateway._tools`），只在配置变更时重建。
而我们要的是「用户在菜单里一关就立刻生效」—— 收参数的话，
菜单里改完得等下一次重建工具清单才生效，用户会以为开关坏了。

⚠️ 所以这里读的是**每次调用时**的值。代价是一个模块级全局，
换来的是开关的即时性和可测性（测试可以直接 set，不用构造整个 gateway）。
"""
_mcp_search_enabled = True


def set_mcp_search(enabled: bool) -> None:
    """设置 MCP 搜索后端是否启用（网关在配置变更时调）。"""
    global _mcp_search_enabled
    _mcp_search_enabled = bool(enabled)
    log.info("MCP 搜索后端：%s", "开" if _mcp_search_enabled else "关")


def mcp_search_is_enabled() -> bool:
    """给日志/诊断用。"""
    return _mcp_search_enabled


def _mcp_search_on() -> bool:
    """`_web_search` 内部用。

    ⚠️ 单独包一层而不是直接读 `_mcp_search_enabled`：
    将来如果要做成"读配置而不是全局变量"，只改这一处。
    另外也让测试能 monkeypatch 这一个点。
    """
    return _mcp_search_enabled


"""
## 搜索模式（2026-10-16 用户要求）：accurate / fast / off

菜单里的「联网搜索」从 checkbox 改成了**三选一**：

    accurate  精准搜索（约 10~20 秒）—— free-search-mcp 多引擎为主
    fast      快速搜索（约 2 秒）—— websearch-mcp.exe（原行为）
    off       关闭联网搜索（工具不注册，模型根本看不到）

为什么做成模式而不是两个独立开关：用户原话是"可显示选择模式的方式"——
两种搜索的**耗时差 10 倍**（1.5s vs 10~20s），用户应该按当下需求选，
而不是记住"mcp_search 开关"这种实现细节。

⚠️ 模式变量的同步纪律与 `_mcp_search_enabled` 相同：
网关（持有 config）在配置变更时调 `set_search_mode()` 推过来，
`_web_search` 每次现读 —— 菜单里改完立刻生效，不用重建工具清单。
"""
SEARCH_MODES = ("accurate", "fast", "off")
_search_mode = "off"
# free-search 的端点与启动命令（gateway 从 persona.yaml 的 tools 段推过来）
_free_search_url = ""
_free_search_cmd = ""

"""
## ⚠️ DeepSeek 也有官方联网搜索了 —— 但我们**故意不用**（2026-10-17 记）

DeepSeek 公告：API 现已支持**网页搜索与抓取**，通过服务端工具调用访问
`search` / `fetch` 接口，**按请求次数计费**，自北京时间 2026-11-01 00:00
开始按次收取调用费用。

用户明确说：「deepseek开启联网搜索功能了，不过要花钱，我不弄这个」。
所以这里保持现状 —— 走本地的 `free-search-mcp` / `websearch-mcp.exe`，不接官方那套。

### 以后真要接的话，改哪里

不是改这里：官方那套是**模型侧的工具**（服务端自己调），传的是
`tools: [{type: 'web_search'}]` 之类的声明，跟我们现在"自己实现工具 + 回填结果"
的 agent loop 是两条路。接的话要在 `brains/openai_compat.py` 的请求体里声明，
并把 `pet.say.end` 之前的工具轮次让给服务端 —— 那是一次**结构性改动**，
不是加个开关。

⚠️ 计费点：按**请求次数**，不是按 token。长对话里模型可能每轮都想搜一次。
"""


def mode_from_config(section: dict[str, Any] | None) -> str:
    """从 `tools` 配置段读搜索模式（**唯一实现**，网关和事件都用它）。

    兼容老键（2026-10-16 之前只有布尔 `web_search`）：

        web_search_mode 有值   -> 用它（accurate / fast / off）
        web_search: true       -> fast（老行为就是 websearch-mcp 优先）
        缺省 / false           -> off（隐私默认保守）

    ⚠️ 为什么抽出来：原来这份兼容逻辑写在 `Conversation._web_search_mode()` 里，
    而随机事件跑在 `Gateway` 上、拿不到 Conversation 的方法 ——
    复制一份的话两份逻辑迟早走偏（这个项目已经因为"两份实现"栽过几次）。
    """
    section = section or {}
    raw = str(section.get("web_search_mode") or "").strip().lower()
    if raw in SEARCH_MODES:
        return raw
    return "fast" if bool(section.get("web_search", False)) else "off"


def set_search_mode(mode: str, free_url: str = "", free_cmd: str = "") -> None:
    """设置搜索模式与 free-search 的端点/启动命令（网关在配置变更时调）。"""
    global _search_mode, _free_search_url, _free_search_cmd
    if mode not in SEARCH_MODES:
        log.warning("未知搜索模式 %r（忽略，保持 %s）", mode, _search_mode)
        return
    _search_mode = mode
    if free_url.strip():
        _free_search_url = free_url.strip()
    if free_cmd.strip():
        _free_search_cmd = free_cmd.strip()
    log.info(
        "搜索模式：%s（free-search %s）",
        {"accurate": "精准（free-search 优先）", "fast": "快速（websearch-mcp）", "off": "关"}[mode],
        "已配置" if _free_search_url else "未配置端点",
    )


def search_mode() -> str:
    """给日志/诊断用。"""
    return _search_mode


"""
## 这个变量在 `_web_search` 里被引用，但函数定义在模块级
##
## ⚠️ 上面那个 `set_mcp_search` 改的是模块级变量，
## 而 `_web_search` 里如果写 `if _mcp_search_enabled:` 读到的就是当前值
## （Python 在函数里读全局变量是**运行时查找**，不是定义时快照）。
## 这正是我们要的 —— 改了立刻生效。
"""


"""
## 🔴 上一次 `web_search` 的结果集（2026-10-12）—— 用来检测「查询没起作用」

### 为什么需要

搜索的失败模式是**静默的**：HTTP 200、6 条结果、**看起来成功了**，
只有内容不对。模型看到"6 条结果"会以为搜到了，然后拿垃圾内容硬答。

### 判据：换了词，但结果**一模一样**

实测（`probe-dup-detect.py`，历史实测脚本）：

```
'幻梦猎人'       -> 8 条
'幻梦猎人 新书'   -> 重叠 8/8      <- Bing 把它拆成了「幻梦」
'幻梦猎人 作品'   -> 重叠 8/8         换了词完全无效
```

也就是搜索引擎**根本没把这个查询当查询**。

### ⚠️ 为什么模块级变量够用

一次对话里搜索是**顺序**发生的（工具调用一轮一轮来），
不存在并发写。而且这个值只用于"提示模型换个说法"，
**丢了也不影响正确性**（最坏就是少一句提示）。

⚠️ 别把它做成 `Conversation` 的字段 —— 那会把一个纯提示性的启发式
耦合进会话状态机，收益不值那个复杂度。
"""
_last_search_urls: set[str] = set()

"""
判据的**阈值**（2026-10-15 抽成常量，方便测试引用）。

```
重叠率 >= 0.8    容忍浮动的排名 / 收录差异（不是 1.0）
较小者 >= 3 条   1~2 条时"重叠 1/1"没有统计意义
```
"""
_QUERY_IGNORED_OVERLAP = 0.8
_QUERY_IGNORED_MIN_HITS = 3


def query_looks_ignored(current: set[str], previous: set[str]) -> bool:
    """这次的搜索结果和上次**是不是同一批**（= 查询可能没被搜索引擎理解）。

    ## 为什么抽成纯函数（2026-10-15）

    原来这段判据内联在 `_web_search()` 里，后果是**测试只能靠真联网**。
    而它测的其实是两件不同的事：

    ```
    ① 判据的数学对不对       <- 确定性的，应该注入测试
    ② Bing 这次返回了什么     <- 不确定的，不该硬断言
    ```

    ⚠️ 实测暴露了这个设计问题：2026-10-15 时 Bing 对中文查询返回**完全无关的结果**
    （`幻梦猎人` -> 晶圆厂 / Epic Games 文档；`幻梦猎人 新书` -> 百度知道问答页），
    两次交集为 0 —— 于是测试**因为搜索引擎变差而失败**，而代码一个字都没改。

    抽成纯函数之后判据可以用注入的集合测，永远可重复。

    ## 参数

    `current`    这次结果的 URL 集合
    `previous`   上次结果的 URL 集合（第一次搜索时是空集）

    ## 返回

    `True` = 两次高度重叠，值得提示模型换个说法。

    ⚠️ **只在两次都有结果时才可能返回 `True`** ——
    第一次没有"上一次"可比；空结果走的是"搜索没有结果"那条分支。
    """
    if not current or not previous:
        return False
    both = current & previous
    smaller = min(len(current), len(previous))
    return smaller >= _QUERY_IGNORED_MIN_HITS and len(both) / smaller >= _QUERY_IGNORED_OVERLAP

"""
浏览器兜底**允不允许**（用户开关），由 `build_tools(allow_browser=...)` 设置。

⚠️ 和 `web.browser_available()`（主进程有没有把发送能力注进来）是**两个条件**，
必须都成立才真的走浏览器：

```
_browser_allowed       用户同意了吗（设置里的开关）
web.browser_available() 技术上做得到吗（主进程在跑且注入了发送函数）
```

分开判的理由：**这是一个隐私开关**。多判一次的成本是零，
漏判的代价是"用户没同意却以他的身份浏览了网站"。
"""
_browser_allowed: bool = False

_QUERY_IGNORED_HINT = (
    "\n\n⚠️ **注意：这次的搜索结果和上一次几乎完全一样。**\n"
    "这通常说明搜索引擎**没看懂这个查询**（它把词拆开、只匹配了其中一个字），\n"
    "而不是「网上没有这个东西」。别再换同义词重搜了 —— 换成：\n"
    "  · **只搜核心名字**（书名/人名/游戏名单独一个词，不加任何修饰）\n"
    "  · 或者换个**完全不同的角度**（比如从「作者名」改搜「作品名」）\n"
    "  · 或者先 `web_fetch` 一个已经搜到的页面，从页面里找线索再搜"
)

# 工具结果喂回模型时的长度上限。
# ⚠️ 不是越长越好：一次搜索 8 条结果 + 一篇 4000 字正文，很容易挤掉对话历史，
# 而模型对超长上下文的**中间部分**本来就不敏感。
MAX_RESULT_CHARS = 6000


@dataclass(slots=True)
class Tool:
    """一个可调用工具。"""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[str]]

    def schema(self) -> dict[str, Any]:
        """OpenAI 的 function 描述（`LLMRequest.tools` 要的形状）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# --------------------------------------------------------------------------- #
# 工具实现
# --------------------------------------------------------------------------- #


# 关掉联网时，搜索**不执行**，但要说一句人话 ——
# 否则模型/事件只能"静默失败"，用户看到的是"它好像卡住了"。
SEARCH_OFF_MESSAGE = "联网搜索好像没打开"


async def search_web(query: str, count: int = 6) -> str:
    """**唯一的搜索入口**（用户 2026-10-18 要求：固定方法，下辖两档）。

    精准 / 快速由 `set_search_mode()` 定的当前模式决定；`off` 时直接拒绝。
    聊天工具和随机事件（哲学事件）都必须走这里 —— 这样"关掉联网"才是一处生效。
    """
    text = str(query or "").strip()
    if not text:
        return "错误：没有给搜索词。"
    return await _web_search({"query": text, "limit": count})


async def _web_search(args: dict[str, Any]) -> str:
    """搜索。结果格式化成模型好读的文本（不是 JSON —— 它读文本更准）。

    ## 🔴 两条后端，MCP 优先（2026-10-15）

    ```
    mcp_search  ──HTTP──>  websearch-mcp.exe  ──>  360 / Bing 融合 + 评分重排
    web.search  ──urllib──> 自己抓 HTML 解析
    ```

    MCP 那条路实测**快 7 倍**（1.5s vs 10.8s）且结果带相关性分数，
    所以优先。但它依赖一个外部 exe，**可能在打包后找不到** ——
    所以必须有退路，而且退路要**静默生效**（用户不该看到"搜索坏了"）。

    ⚠️ 失败时**一定要记日志**。这个退路是锦上添花的设计，
    但如果它天天在生效而没人知道，那 MCP 那条路坏了也发现不了。
    """
    query = str(args.get("query") or "").strip()
    if not query:
        return "错误：没有给搜索词。请用 query 参数传入要搜什么。"

    # 🔴 关掉联网 -> 一次都不许发出去（用户 2026-10-18 明确要求）。
    # 这里返回**一句用户能听懂的话**，而不是空串：空串会让模型以为"搜到了但没内容"，
    # 然后自己编一个答案 —— 那是最坏的结果。
    if _search_mode == "off":
        log.info("web_search q=%r 被拒：联网搜索是关着的", query)
        return SEARCH_OFF_MESSAGE

    limit = args.get("limit")
    try:
        count = max(1, min(10, int(limit))) if limit is not None else 6
    except (TypeError, ValueError):
        count = 6

    """
    ## ① 先试 MCP 后端

    ⚠️ `mcp_search.search()` 是**同步阻塞**的（urllib），要 `to_thread` ——
    和下面 `web.search` 同理，直接在事件循环里调会卡住整个 sidecar。

    ⚠️ 这里吞掉异常是**刻意**的：MCP 不可用时应该退到自己的抓取，
    而不是让工具报错给模型看（模型会据此说"我搜不了"，而其实还能搜）。

    ## 🔴 2026-10-16：按「搜索模式」分流

        accurate  先走 **free-search-mcp**（多引擎融合，命中质量实测明显更好
                  —— 能拿到官网公告而不是"二"字百科），失败落回 fast 链路
        fast      websearch-mcp.exe（1.5 秒快筛）
        off       不会进来（工具根本没注册）

    A/B 实测数据见 `mcp_search.search_free` 的说明。
    """
    if _search_mode == "accurate" and _free_search_url:
        try:
            text = await asyncio.to_thread(
                mcp_search.search_free, query, count,
            )
            if text.strip():
                log.info(
                    "web_search q=%r limit=%s -> free-search 后端，%d 字",
                    query, count, len(text),
                )
                return text
            log.warning("web_search q=%r free-search 返回空，退到 fast 链路", query)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "web_search q=%r free-search 失败（%s），退到 fast 链路",
                query, str(exc)[:160],
            )

    if _mcp_search_on():
        try:
            text = await asyncio.to_thread(mcp_search.search, query, count)
            if text.strip():
                log.info(
                    "web_search q=%r limit=%s -> MCP 后端，%d 字",
                    query, count, len(text),
                )
                return text
            log.warning("web_search q=%r MCP 返回空，退到自抓后端", query)
        except Exception as exc:  # noqa: BLE001
            log.warning("web_search q=%r MCP 后端失败（%s），退到自抓后端",
                        query, str(exc)[:160])

    # ⚠️ `search()` 是**同步阻塞**的（urllib），放到线程里跑 ——
    # 直接在事件循环里调会卡住整个 sidecar（连语音都停）。
    outcome = await asyncio.to_thread(web.search, query, count)

    """
    ## 🔴 搜索词必须**打进日志**（2026-10-12 补）

    用户问「你发给我它搜索的关键词是什么」—— 而我**答不出来**：
    原来这里只把结果发给模型，自己一句都不记。

    于是排查"为什么搜不到"时完全没有线索：
      · 不知道它搜的是中文还是英文
      · 不知道它有没有自己加限定词
      · 不知道查了几次、每次查什么

    而"模型自己编的查询词"恰恰是**最可能出问题的一环** ——
    比如它把「银松镇」直译成 "Silver Town"，而游戏的英文名是
    **Silver Pines**，那搜一万次也搜不到。

    所以打**一行 INFO**，把 query / 结果数 / 引擎都记下。
    这是排查联网问题时唯一可靠的信息源。
    """
    log.info(
        "web_search q=%r limit=%s -> %d 条（%s）",
        query,
        count,
        len(outcome.hits),
        outcome.engine or "无",
    )

    if not outcome.ok:
        # ⚠️ 失败要说清"为什么" —— 含糊的失败会让模型编一个答案
        return f"搜索没有结果。尝试记录：{'；'.join(outcome.attempts) or '未知原因'}"

    """
    ## 🔴 检测「查询没起作用」（2026-10-12）

    见 `_last_search_urls` 的说明。

    ## ⚠️ 判据是**重叠率**，不是"完全相同"

    第一版写的是 `current_urls == _last_search_urls`（集合完全相等），
    实测**会漏报**：Bing 每次返回的集合有轻微浮动（换词后重叠 7/8 而不是 8/8），
    于是明明"查询没起作用"却检测不到。

    ``` 
    '幻梦猎人'       -> 8 条
    '幻梦猎人 新书'   -> 重叠 7/8   <- 集合不相等，但显然没变
    ```

    所以改成：**两次结果的交集占任一边的比例 ≥ 0.8，且条数 ≥ 3**。

    ⚠️ 那两个阈值都是**防误报**用的：
      · 比例 0.8 而不是 1.0 —— 容忍浮动的排名/收录差异
      · 条数 ≥ 3 —— 只有 1~2 条结果时，"重叠 1/1" 没有统计意义，
        正常搜索也会因为结果少而"看起来相同"

    ⚠️ 只在**两次都有结果**时才算（第一次没有"上一次"可比；
    空结果走的是上面那条"搜索没有结果"的分支）。

    ## 🔴 2026-10-15：判据抽成纯函数 `query_looks_ignored()`

    ### 为什么抽

    原来这段判据是**内联在这里**的。后果是**测试只能靠真联网** ——
    而它测的其实是两件不同的事：

    ```
    ① 判据的数学对不对        （确定性的，应该注入测试）
    ② Bing 这次返回了什么      （不确定的，不该硬断言）
    ```

    ⚠️ 实测暴露了这个设计问题：2026-10-15 时 Bing 对中文查询返回**完全无关的结果**
    （`幻梦猎人` -> 晶圆厂 / Epic Games 文档；`幻梦猎人 新书` -> 百度知道问答页），
    两次交集为 0 —— 于是 `test-query-ignored.py` 的断言**因为搜索引擎变差而失败**，
    而代码一个字都没改。

    抽出来之后：判据用注入的 URL 集合测（永远可重复），
    真联网那两条降级成"仅供参考"。
    """
    global _last_search_urls
    current_urls = outcome.urls()
    ignored = query_looks_ignored(current_urls, _last_search_urls)
    _last_search_urls = current_urls
    if ignored:
        log.info("web_search q=%r 的结果与上次高度重叠 —— 查询可能没被理解", query)

    lines = [f"搜索「{query}」（来自 {outcome.engine}）的结果："]
    for idx, hit in enumerate(outcome.hits, 1):
        lines.append(f"{idx}. {hit.title}")
        lines.append(f"   {hit.url}")
        if hit.snippet:
            lines.append(f"   {hit.snippet[:160]}")
    lines.append("")
    """
    ## 🔴 每次搜索结果末尾都要给「下一步」（2026-10-12）

    实测（`probe-novel-live.py`，历史实测脚本）暴露的问题：

    ```
    模型搜了 5 次、**一次 web_fetch 都没用**
    -> 撞上轮数上限 -> 被强制收尾 -> 回复「完全没查到，抱歉呀」
    ```

    而它**第三次就搜对了**（`'天官赐邪'` 拿到了正确的书页链接），
    只是**不知道下一步该读那个页面**，于是继续换词搜。

    ⚠️ 所以光在**工具描述**里写"够了就停"不够 ——
    模型在**读结果的那一刻**才决定下一步，提示得出现在**结果文本**里。

    ## 🔴🔴 更要紧的：**答案往往就在摘要里**（同一批实测发现）

    ```
    2. 天官赐邪 (幻梦猎人)小说在线阅读-首发起点中文网
       https://www.qidian.com/book/1037780233/
       14 小时之前 · 幻梦猎人创作的悬疑灵异小说《天官赐邪》，已更新1307章，
       最新章节：第1307章 拜神结束。
    ```

    这一条**摘要里就有全部答案**（作者 / 类型 / 连载中 / 最新章），
    而模型回复「出来的全是'幻梦'这个词的百科，压根没找到」——
    **它没读摘要，只扫了标题。**

    ⚠️ 这是**最浪费**的一种失败：搜索成功了、答案在眼前、
    却因为没读而报"没找到"。

    所以最后那句提示必须说**"先读摘要"**，而不只是"去 web_fetch"。
    """
    lines.append("要看某一篇的完整内容，用 web_fetch 传它的链接。")
    lines.append(
        "⚠️ **先仔细读上面的标题和摘要** —— 答案经常直接写在里面"
        "（比如「《某书》是某某创作的…已更新N章」「由某某演唱」这类）。"
        "摘要里已经有答案就直接回答，**不要**再换词搜、也不用 web_fetch。"
    )
    lines.append(
        "⚠️ 只有**摘要里确实没有**你要的信息时，才 web_fetch 读那一篇的正文。"
    )
    if ignored:
        lines.append(_QUERY_IGNORED_HINT)
    return _clamp("\n".join(lines))


def _format_metas(metas: dict[str, Any], limit: int = 8) -> str:
    """把页面 `<meta>` 拼成一小段文字。**没内容就返回空串**。

    ## 为什么这个比正文有用

    浏览器的 `metas` 里含 `og:*` —— 那是**站点自己生成的结构化数据**。
    实测起点书页：

    ```
    og:novel:book_name           天官赐邪
    og:novel:author              幻梦猎人
    og:novel:category            诡异神秘
    og:novel:status              连载
    og:novel:latest_chapter_name 第1308章 黑色冰莲
    og:novel:update_time         2026-10-13 00:57:30
    ```

    **这一小段就回答了「作者是谁 / 在写什么 / 更新到哪」** ——
    比从 22,000 字正文里抠准确得多，而且短得多（省 token）。

    ## ⚠️ 排序：`og:novel:*` 在前

    因为这些是**语义明确的**，而 `description` / `keywords` 是通用字段
    （可能只是一句宣传语）。放在前面能让模型先看到最有用的。
    """
    if not metas:
        return ""
    ordered = sorted(
        metas.items(),
        key=lambda item: (
            # og:novel:* 最优先，其余 og:* 次之，description/keywords 最后
            0 if str(item[0]).startswith("og:novel:") else (1 if str(item[0]).startswith("og:") else 2),
            str(item[0]),
        ),
    )
    lines = []
    for key, value in ordered[:limit]:
        text = str(value or "").strip()
        if text:
            lines.append(f"  {key}: {text[:200]}")
    if not lines:
        return ""
    return "页面元信息：\n" + "\n".join(lines) + "\n\n"


async def _bilibili_search(args: dict[str, Any]) -> str:
    """搜 B 站视频（走站内接口，不用签名）。

    ## 为什么值得单独一个工具

    `web_search` 也能搜到 B 站视频，但那是**搜索引擎的索引** ——
    只有被 Bing 收录的才搜得到，而且给的是网页链接。

    B 站站内搜索给的是**站内热度排序 + 播放量 + 时长**，
    对"大家最近在聊什么"这类问题明显更好用（实测「终末地 新角色」
    在 `web_search` 里能搜到，但站内搜索能直接给出一串相关视频）。

    ⚠️ **B 站内容主要是中文社区的讨论**，不代表官方信息 ——
    所以拼出来的文本里明确写了这一点（见 `video.format_hits`）。
    """
    from . import video as video_module

    query = str(args.get("query") or "").strip()
    if not query:
        return "错误：没有给搜索词。请用 query 参数传入要搜什么。"

    limit = args.get("limit")
    try:
        count = max(1, min(12, int(limit))) if limit is not None else 6
    except (TypeError, ValueError):
        count = 6

    hits, reason = await asyncio.to_thread(video_module.search_bilibili, query, count)
    log.info("bilibili_search q=%r -> %d 条%s", query, len(hits), f"（{reason}）" if reason else "")
    if not hits:
        return f"B 站没搜到。原因：{reason}"
    return video_module.format_hits(query, hits)


async def _web_fetch(args: dict[str, Any]) -> str:
    """读网页正文。**视频站链接会走结构化接口**（2026-10-12）。"""
    url = str(args.get("url") or "").strip()
    if not url:
        return "错误：没有给链接。请用 url 参数传入要读的网址。"

    """
    ## 🔴 先认视频站（2026-10-12 用户要求）

    用户要「**让他读懂 b 站上的信息**」。

    而视频页面的 HTML 里只有导航和推荐列表（实测 1185 字，标题/标签之外
    几乎没内容），真正的信息在**接口**里：

    ```
    web.fetch_page()  -> 「首页/番剧/直播/…」+ 推荐列表
    bilibili API      -> 标题、标签、UP 主、发布时间、播放/点赞/评论数、分P
    ```

    所以这里**先试 `video.parse()`** —— 它认不出站点就返回 `None`，
    自然落到通用网页抓取，两条路不会混。
    """
    from . import video as video_module

    parsed = await asyncio.to_thread(video_module.parse, url)
    if parsed is not None:
        log.info(
            "web_fetch url=%r -> 视频站 %s ok=%s%s",
            url,
            parsed.site,
            parsed.ok,
            "" if parsed.ok else " 原因：" + parsed.reason,
        )
        return video_module.format_video(parsed)

    result = await asyncio.to_thread(web.fetch_page, url)

    """
    ## 🔴 浏览器兜底（2026-10-13 用户要求）

    用户问「起点和番茄你也帮我测测能不能行」时发现：起点**全站**返回
    WAF 挑战（`x-waf-captcha-referer` + `probe.js`），纯 HTTP 抓不到。
    而真浏览器能过 —— 主进程开个隐藏 Chromium 窗口就行。

    ## ⚠️ 触发条件：**只在那两件"机器挡的"情况**才走浏览器

    ```
    ① 失败原因是 WAF/验证码        <- 明确是"机器挡的"
    ② 失败原因是 HTTP 202          <- 起点那种挑战，302 之外最典型的
    ```

    **刻意不兜底的情况**（那些走浏览器也没用，只是白等 15 秒）：

    ```
    HTTP 404 / 403      页面不存在 / 明确拒绝 -> 换浏览器也一样
    PDF                 不是 HTML
    JS 站正文太短        浏览器**能**渲染，但那类站（小红书）
                        多半还要登录，兜底成功率低而代价高
    ```

    ⚠️ **纯 JS 站其实是个边界情况** —— 浏览器确实能渲染出正文，
    所以理论上该兜底。但它会**每次多等十几秒**，而成功率不确定。
    暂时不兜（记在这里，将来有实测数据再改）。

    ## ⚠️ 兜底失败时**保留原始失败原因**

    不能说"浏览器也读不了"就完事 —— 原始原因（HTTP 404 / WAF / 超时）
    才是模型决定"换链接还是放弃"的依据。
    """
    if not result.ok and _browser_allowed and web.browser_available():
        reason = result.reason or ""
        looks_blocked = ("反爬验证" in reason) or ("HTTP 202" in reason)
        if looks_blocked:
            log.info("web_fetch 遇到 %s，改用浏览器兜底：%s", reason[:40], url[:80])
            fallback = await web.browser_fetch(url)
            if fallback.get("ok"):
                metas = fallback.get("metas") or {}
                text = str(fallback.get("text") or "")
                title = str(fallback.get("title") or "")
                log.info(
                    "浏览器兜底成功：%s（%s 字，%s 毫秒）",
                    url[:70],
                    len(text),
                    fallback.get("elapsedMs"),
                )
                head = f"《{title}》\n{url}\n\n" if title else f"{url}\n\n"
                extra = _format_metas(metas)
                return _clamp(head + extra + text)
            log.info("浏览器兜底也没成：%s", str(fallback.get("reason"))[:80])
            return (
                f"读不了这个页面：{reason}"
                f"（用浏览器兜底也没成功：{fallback.get('reason')}）"
                f"（{url}）"
            )

    """
    🔴 `web_fetch` 也要打日志（2026-10-12 补）。

    与 `web_search` 同理：读网页失败时，「**读了哪个链接**」是唯一的线索。
    不记的话用户问「它到底看了什么」，我答不出来。
    """
    log.info(
        "web_fetch url=%r -> ok=%s%s",
        url,
        result.ok,
        "" if result.ok else " 失败原因：" + result.reason,
    )
    if not result.ok:
        # 原因必须带出去（模型据此决定换个链接还是放弃）
        return f"读不了这个页面：{result.reason}（{url}）"

    head = f"《{result.title}》\n{result.url}\n\n" if result.title else f"{result.url}\n\n"
    return _clamp(head + result.text)


# --------------------------------------------------------------------------- #
# 沙箱（可读网上、只写沙箱目录）
# --------------------------------------------------------------------------- #

# 沙箱目录名。放在 pet-data 下 —— 那里本来就是"应用的运行时数据"，
# 而且 `pet-data/` 已经在 .gitignore 之类的地方被当成不可版本化的目录。
SANDBOX_DIRNAME = "web-sandbox"

# ⚠️ 文件名白名单。**不只是防路径穿越，也防奇怪字符**
# （Windows 上 `CON` / `NUL` / `PRN` 是保留名，`: ` `*` `?` 在文件名里非法）。
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_.\-\u4e00-\u9fff]{0,60}$")


def sandbox_root(state_dir: pathlib.Path) -> pathlib.Path:
    """沙箱目录（不存在就建）。

    `state_dir` 由调用方注入（`pet-data/`）—— 与 `settingsStore` 同一个理由：
    硬编码路径在换部署位置时会静默失效。
    """
    root = pathlib.Path(state_dir) / SANDBOX_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve_in_sandbox(state_dir: pathlib.Path, name: str) -> Optional[pathlib.Path]:
    """把文件名解析成沙箱内的绝对路径；**越界就返回 None**。

    ## 🔴 这是"不许越界"的唯一落点

    所有写入都经过这里。检查分三层：

      ① **文件名白名单**（`_SAFE_NAME`）—— 直接挡掉 `../`、`..\\`、绝对路径、
         盘符、非法字符。比"先拼接再检查"更早、更难绕过。
      ② `resolve()` 后确认父目录**就是**沙箱根 ——
         挡掉符号链接之类"拼出来看着在里、实际指到外面"的情况。
      ③ 长度与保留名（Windows 的 `CON`/`NUL`/…）。

    ⚠️ **三层都要有**。只靠 ① 的话，某些 Unicode 规范化差异可能绕过正则；
    只靠 ② 的话，`..` 已经在拼接阶段生效了（`resolve()` 会把它解到外面，
    于是 ② 确实能挡住 —— 但错误信息会变成"路径不在沙箱内"，
    而不是更有用的"文件名不合法"）。
    """
    cleaned = str(name or "").strip()
    if not cleaned:
        return None
    if not _SAFE_NAME.match(cleaned):
        return None
    if cleaned.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL"}:
        return None

    root = sandbox_root(state_dir)
    candidate = (root / cleaned).resolve()
    if candidate.parent != root.resolve():
        return None
    return candidate


async def _sandbox_write(args: dict[str, Any], state_dir: pathlib.Path) -> str:
    name = str(args.get("name") or "").strip()
    text = str(args.get("content") or "")
    if not name:
        return "错误：没有给文件名。"
    if not text:
        return "错误：没有给内容（content 是空的）。"

    path = _resolve_in_sandbox(state_dir, name)
    if path is None:
        # ⚠️ 这里**不回显用户给的名字**在错误里加重试 —— 说清规则就够了
        return (
            "文件名不合法。只能用小写字母/数字/下划线/中文/点和横线，"
            "不能带路径分隔符，长度不超过 60 字。"
        )
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        return f"写文件失败：{exc}"
    log.info("沙箱写入：%s（%d 字）", path.name, len(text))
    return f"已写入 {path.name}（{len(text)} 字）。"


async def _sandbox_read(args: dict[str, Any], state_dir: pathlib.Path) -> str:
    name = str(args.get("name") or "").strip()
    path = _resolve_in_sandbox(state_dir, name)
    if path is None:
        return "文件名不合法（同 sandbox_write 的规则）。"
    if not path.exists():
        # 告诉它目录里有什么，省一轮往返
        try:
            existing = sorted(p.name for p in sandbox_root(state_dir).iterdir() if p.is_file())
        except OSError:
            existing = []
        return f"没有这个文件。目录里现有：{'、'.join(existing) if existing else '（空）'}"
    try:
        return _clamp(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return f"读文件失败：{exc}"


def _clamp(text: str) -> str:
    """截断过长结果。"""
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + "\n…（内容过长，已截断）"


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #


def build_tools(
    state_dir: pathlib.Path,
    allow_web: bool,
    allow_browser: bool = False,
) -> list[Tool]:
    """按开关组装工具清单。

    ## `allow_web` 控制联网

    用户要求「**开启之后**可以自由的搜索相关信息」—— 所以联网是个开关。
    关掉时**工具根本不注册**（而不是注册了再拒绝）：

      · 模型看不到 `web_search`，自然也不会尝试调用 —— 少一轮往返
      · 提示词里也不必解释"你不能联网"，省 token
      · 更重要的是：**隐私口径一致** —— 界面上写着"没开联网"，
        模型就真的拿不到联网能力，不会出现"UI 说关了、实际还能搜"

    沙箱工具**不受这个开关影响** —— 它读写的是本地目录，不涉及网络。

    ## `allow_browser`：浏览器兜底（2026-10-13 用户要求）

    ⚠️ **它和 `allow_web` 不一样：不控制"注册哪个工具"，只控制"行为"**。

    | | `allow_web` | `allow_browser` |
    |---|---|---|
    | 关掉时 | `web_search` / `web_fetch` **不注册** | 工具照常注册 |
    | 影响 | 模型看不到联网能力 | `web_fetch` **读不动时不用浏览器重试** |

    为什么不把 `web_fetch` 也摘掉：**没有浏览器兜底时它仍然有用**
    （普通网页读得了）。摘掉的话用户会"关掉一个隐私选项，结果连普通网页都读不了"。

    ⚠️ 所以这个开关的作用点只有一个：`_web_fetch` 里那个
    `looks_blocked` 分支走不走。

    ⚠️ **"能不能用"还有第二个条件**：主进程得把发送能力注进来
    （`web.attach_browser`）。两个条件都不满足时才真的不用兜底 ——
    这也是"隐私开关多判一次"的体现。
    """
    global _browser_allowed
    _browser_allowed = bool(allow_browser)
    tools: list[Tool] = []

    if allow_web:
        tools.append(
            Tool(
                name="web_search",
                description=(
                    "联网搜索。**只在这些情况用**：用户明确让你查；"
                    "或者你需要当前/最新的信息（新闻、价格、版本号、天气、"
                    "最近发生的事）；或者你对某个事实不确定。"
                    "**不要为了显得知道而搜**，也不要每次回答都搜 —— "
                    "用户闲聊、问你的看法、回忆以前聊过的事时都不该搜。\n"
                    "\n"
                    "## 怎么用（实测总结，照做能少走很多弯路）\n"
                    "\n"
                    "· **词要短**：2~4 个词最好。第一次就用最可能的那个名字，"
                    "不要一上来堆一串修饰词。\n"
                    "· **绝对不要用引号**：`「银松镇」` / `\"银松镇\"` 里的引号会被"
                    "搜索引擎**当成关键词本身**，实测返回的是"
                    "「『』是什么符号」这种页面，跟你要找的东西毫无关系。\n"
                    "· **一旦有结果就停下来读**，不要继续换词试探。\n"
                    "  实测的失败过程：`银松镇 游戏` 返回了 Steam 商店页、"
                    "百度百科、知乎评测（**完全够回答**），但它没读，改成"
                    "`银松镇 游戏 什么类型 玩法` 再搜 —— 结果全是白银价格。"
                    "**加的词越多，越容易把结果推向无关内容。**\n"
                    "· **搜到的标题和摘要通常已经够了**，直接据此回答；"
                    "只有确实需要细节时才用 `web_fetch` 读全文。\n"
                    "· **搜不到就说清「你搜了什么词、看到了什么」**，"
                    "然后换个**更短的名字**再试一次——不要声称「什么都没搜到」"
                    "却在同一轮里换四五个词（那说明前几次其实有结果）。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "搜索关键词。2~4 个词最好。"
                                "中文或英文都行，但**不要加引号或括号**。"
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "要几条结果，默认 6，最多 10。",
                        },
                    },
                    "required": ["query"],
                },
                handler=_web_search,
            )
        )
        tools.append(
            Tool(
                name="bilibili_search",
                description=(
                    "在 **B 站**站内搜视频。\n"
                    "\n"
                    "**什么时候用它而不是 `web_search`**：\n"
                    "· 用户提到「b站上都在说」「视频里看到的」这类**社区讨论**\n"
                    "· 游戏/动画/数码的新内容（**新角色、新版本、新机**）——"
                    "这类信息主要先在 B 站被讨论，网页往往滞后\n"
                    "· `web_search` 结果里出现很多 B 站链接时，可以用它再挖一层\n"
                    "\n"
                    "🔴 **搜一次就够 —— 结果一到手就停下来回答**\n"
                    "站内搜索的结果列表**本身就是答案**：视频标题里通常直接写着\n"
                    "角色名、版本号、事件名。看到标题里已经有你要的东西，\n"
                    "**就直接回答**，不要换个词再搜一遍。\n"
                    "（实测过：模型搜到答案后继续换 4 个词挖了 3 轮，"
                    "最后撞上轮数上限被强制收尾，回复反而只剩 60 字。）\n"
                    "\n"
                    "同一轮里要搜**两个方向**时才调两次；"
                    "**同一个方向不要换词重搜**。\n"
                    "\n"
                    "⚠️ 它给的是**视频标题**，只能说明社区在聊什么，"
                    "**不是官方信息** —— 别把标题里的说法当确定的事实。\n"
                    "想看某个视频的详细页面信息（标签/UP 主/数据），"
                    "用 `web_fetch` 传它的链接。\n"
                    "\n"
                    "⚠️ 只支持 B 站。抖音读不了（要签名），别试。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "搜索关键词。2~4 个词最好，"
                                "**不要加引号或括号**（会被当成关键词本身）。"
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "要几条结果，默认 6，最多 12。",
                        },
                    },
                    "required": ["query"],
                },
                handler=_bilibili_search,
            )
        )
        tools.append(
            Tool(
                name="web_fetch",
                description=(
                    "读一个网页的正文。通常在 web_search 之后用 —— "
                    "光看搜索结果的标题和摘要不够时，读全文再回答。"
                    "也可以直接读用户给你的链接。\n"
                    "\n"
                    "**传 B 站视频链接时**会自动走站内接口，"
                    "给出标题/标签/UP 主/发布时间/播放数 —— 比读网页 HTML 准得多。\n"
                    "（抖音链接读不了，会明确告诉你。）"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "要读的网址，必须以 http:// 或 https:// 开头。",
                        }
                    },
                    "required": ["url"],
                },
                handler=_web_fetch,
            )
        )

    tools.append(
        Tool(
            name="sandbox_write",
            description=(
                "把一个文件写进你的**沙箱目录**（只有那个目录，别处写不了）。"
                "适合存你自己整理的东西：查到的资料摘要、待办、想记住的清单。"
                "**不是用来记用户的事的** —— 那些用记忆库。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "文件名，如 notes.md。不能带路径分隔符。",
                    },
                    "content": {"type": "string", "description": "要写入的完整文本。"},
                },
                "required": ["name", "content"],
            },
            handler=lambda args: _sandbox_write(args, state_dir),
        )
    )
    tools.append(
        Tool(
            name="sandbox_read",
            description="读回沙箱目录里自己写过的文件。",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "要读的文件名。"}
                },
                "required": ["name"],
            },
            handler=lambda args: _sandbox_read(args, state_dir),
        )
    )
    return tools


def schemas(tools: list[Tool]) -> list[dict[str, Any]]:
    """把工具列表翻成 `LLMRequest.tools` 要的形状。"""
    return [tool.schema() for tool in tools]


"""
哪些工具会**在状态条上报进度**（2026-10-12 用户要求）。

用户原话：「宠物联网搜索的时候状态栏不会变，没有显示联网搜索中」。

## ⚠️ 只报联网工具，沙箱工具**不报**

沙箱工具是本地文件读写，**微秒级完成** —— 上报的话状态条会闪一下，
看起来像故障而不是进度。而联网那些要几百毫秒到几秒，
用户确实需要知道"它在忙"。

这也和"只报外部可见的操作"是同一条原则：状态条该反映
**用户能感知到的等待**，而不是内部实现细节。
"""
REMOTE_TOOLS = frozenset({"web_search", "web_fetch", "bilibili_search"})


def _tool_detail(name: str, args: dict[str, Any]) -> str:
    """给状态条看的一行说明（搜索词 / 网址）。

    ⚠️ **要截断**：状态条只有 40px 宽、竖排显示，长了会溢出。
    而且它只是"让人一眼看出在查什么"，不需要完整。
    """
    if name in ("web_search", "bilibili_search"):
        return str(args.get("query") or "")[:28]
    if name == "web_fetch":
        url = str(args.get("url") or "")
        # 去掉协议头，省地方
        return url.replace("https://", "").replace("http://", "")[:28]
    return ""


async def run_tool(
    tools: list[Tool],
    name: str,
    raw_args: str,
    bus: Any = None,
) -> str:
    """执行一个工具调用，**任何异常都变成给模型看的文本**。

    ## ⚠️ 绝不能把异常抛出去

    抛出去的话整个 agent loop 断掉，用户看到的是"宠物突然不说话"。
    而工具失败（网络不通、参数错、文件名非法）**是正常情况** ——
    模型应该知道"这次没成功"，然后自己决定重试还是告诉用户。

    这也和 `pet/web.py` 里"失败一定给得出原因"是同一条原则：
    含糊的失败会让模型**编一个答案**。

    ## `bus`：状态条进度上报（2026-10-12 加，可选）

    传了就发 `pet.tool` 事件（start / end），主进程据此把状态条切成
    「搜索中」。**不传就完全不上报** —— 测试里不需要它。
    """
    tool = next((item for item in tools if item.name == name), None)
    if tool is None:
        return f"没有名为 {name} 的工具。"

    # `arguments` 是模型给的 JSON 字符串。它偶尔会给出不合法 JSON
    # （少个引号、多个逗号），那种情况要明确告诉它 —— 而不是静默当空参数。
    try:
        args = json.loads(raw_args) if raw_args.strip() else {}
    except json.JSONDecodeError as exc:
        log.info("工具 %s 的参数不是合法 JSON：%s", name, raw_args[:120])
        return f"参数不是合法的 JSON（{exc}）。请重新调用，注意引号和逗号。"
    if not isinstance(args, dict):
        return "参数必须是一个 JSON 对象。"

    """
    ## 🔴 `try/finally` 保证**一定发 end**（2026-10-12）

    如果只在成功路径发 end，那么：
      · 工具抛异常 -> 状态条**永远停在「搜索中」**
      · 用户看到的是一个卡住的状态条，而且没有任何提示

    这与本项目反复出现的那类"看着对、实际没接上"是同一族 ——
    所以 end 必须放在 `finally` 里，**无论成败都发**。
    """
    if bus is not None and name in REMOTE_TOOLS:
        await bus.publish(
            "pet.tool",
            msg("pet.tool", phase="start", tool=name, detail=_tool_detail(name, args)),
        )
    try:
        return await tool.handler(args)
    except Exception as exc:  # noqa: BLE001 —— 工具失败不该中断对话
        log.warning("工具 %s 执行失败：%s", name, exc)
        return f"工具执行出错：{type(exc).__name__}: {exc}"
    finally:
        if bus is not None and name in REMOTE_TOOLS:
            await bus.publish(
                "pet.tool",
                msg("pet.tool", phase="end", tool=name, detail=_tool_detail(name, args)),
            )
