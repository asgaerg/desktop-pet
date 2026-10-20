"""
OpenAI 兼容的流式对话提供者。

写这一份就能同时支持 DeepSeek / 通义 / Kimi / 本地 llama.cpp / Ollama / vLLM ——
它们都吃 OpenAI 的 `/chat/completions` + SSE 协议。换模型只改 config，
不动代码，这是"混合大脑"策略能落地的前提。

实现取舍：
  · HTTP 用标准库 urllib，不引 httpx/aiohttp（延续零依赖原则）；
  · 阻塞读取丢进 `asyncio.to_thread`，事件循环不会被网络卡住；
  · 线程通过 `call_soon_threadsafe` 把增量塞回异步队列 —— 不跨线程碰事件循环；
  · 支持取消：用户打断时置位 Event，线程下一轮检查就退出并断开连接；
  · 错误必须**可读**：401/超时/余额不足要翻译成人能看懂的一句话，而不是
    traceback 丢进日志了事 —— 宠物哑巴时用户得知道为什么。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, AsyncIterator, Iterator, Optional

from .. import netutil
from ..config import Config
from .base import Brain, EmotionState, LLMRequest, StreamEvent, new_tool_call

log = logging.getLogger("pet.brain.openai")

_SENTINEL = object()
_READ_TIMEOUT_S = 60.0

IDLE_USER_TURN = "（现在没有人对你说话。按上面的设定，你自己开口说一句。）"
"""
自主发言时那一条 user 消息的**内容**（2026-10-17）。

## 为什么不能是空串

`None` 会被接口拒收（2026-10-07 修过一次），空串虽然合法，但模型会把它
当成"用户发了一条空消息"去**回应**而不是"该我说话了"。实测它说：

    「诶，又是白茫茫一片。咱俩这是比谁憋得久吗？」
    「诶，还是空的，第七回了吧。我怀疑你是不是按着删除键不撒手了」

于是"截图对话"完全失效 —— 画面描述写在 system 里也没用，
它专心吐槽空消息。换成这句舞台提示之后它才真的开始说画面。

⚠️ 改这句话要一起看 `selftest.py` 里那条
「自主发言不能给模型一条空 user 消息」的检查。
"""


def _is_local_url(url: str) -> bool:
    """base_url 是否指向本机（用来判断"需不需要 API 密钥"）。

    覆盖 Ollama（127.0.0.1:11434 / localhost:11434）、llama.cpp、vLLM、
    LM Studio 这些本地 OpenAI 兼容服务 —— 它们都不校验密钥。
    """
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


class OpenAIBrain(Brain):
    name = "openai"

    def __init__(self, config: Config) -> None:
        self.config = config
        # ⚠️ 用 `brain_profile()` 而不是直接读 `providers.brain` ——
        # 它会把 `named.<当前厂商>` 与平铺字段合并好（见 config.py 的说明）。
        # 直接用平铺字段的话，切到硅基流动后仍然连 DeepSeek 的地址。
        profile = config.brain_profile()
        self.section = profile
        self.base_url = str(profile.get("base_url") or "").rstrip("/")
        self.model = str(profile.get("model") or "")
        # 本地服务（Ollama / llama.cpp / vLLM）**不需要密钥**：
        # 用户选本地大脑时，`config.api_key()` 自然是空的，不能因此起不来。
        local = _is_local_url(self.base_url)
        self.api_key = config.api_key() or ("local" if local else "")
        self.is_local = local
        self.timeout_s = float(profile.get("timeout_s") or 30.0)
        self._validate()

    def _validate(self) -> None:
        if not self.base_url:
            raise ValueError("providers.brain.base_url 未配置")
        if not self.model:
            raise ValueError("providers.brain.model 未配置")
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"base_url 协议不支持：{parsed.scheme!r}（需 http/https）")
        # ⚠️ 本地端点没有密钥是**正常**的（用户要求：三个能力都能用本地模型替代）：
        #    Ollama 默认 http://127.0.0.1:11434/v1 就不校验 Authorization。
        if not self.api_key and not getattr(self, "is_local", False):
            env_name = self.section.get("api_key_env") or self.config.brain_active()
            vendor = self.config.brain_active()
            raise ValueError(
                f"未找到「{vendor}」的 API 密钥。设置页填一个，"
                f"或设环境变量 {env_name} 后重启（密钥不写进配置文件）"
            )

    # ---------------------------------------------------------------- 请求构造

    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _body(self, req: LLMRequest) -> dict[str, Any]:
        """
        把内部请求结构翻成 OpenAI 兼容的 messages。
        ## ⚠️ `user_text` 可能是 `None`，绝不能直接当 content 发（2026-10-07 修）

        用户截图里那条报错就是这里来的：

            the target type: messages[3]: content should be a string or a list
            at line 1 column 23914 (request_id: 0ae1975d-...)

        `LLMRequest.user_text` 的类型是 `Optional[str]` —— **`None` 表示"自主发言"**
        （宠物自己起话头，没有用户消息，见 `base.py` 的说明）。
        而 OpenAI 兼容接口的 `content` 必须是字符串或分段列表，**`None` 会被拒**。

        实测定位：`messages[3]` 正好是「system + 2 轮历史 + user」里那个 user ——
        也就是在**自主发言**（`PET_TEST_SAY` / 定时主动说话）时必然触发。

        修法：`req.user_text or ""`。空串是合法 content，语义也正好是
        "用户什么都没说，你自己起话头"。

        ## 🔴 但**空串本身会把模型带偏**（2026-10-17 实测，改为舞台提示）

        空串确实合法，可模型会把它当成"用户发了一条空消息"来**回应**。
        实测（`deepseek-flash`；截图对话，画面描述明明已经在 system 里）：

            它说：「诶，又是白茫茫一片。咱俩这是比谁憋得久吗？我先认输
                   —— 你倒是说句人话呀。」
            它说：「诶，还是空的，第七回了吧。我怀疑你是不是按着删除键不撒手了」

        也就是说：它不看我们给了什么，**专心吐槽"你发给我的是空的"**。
        截图对话因此完全失效 —— 提示词里 198 字的画面描述等于没写。

        改成一句**舞台提示**之后，模型把它读成"该我说话了"而不是"用户没说话"。
        这不是替它编内容，只是把"轮到你了"这件事讲清楚。
        """
        messages: list[dict[str, Any]] = [{"role": "system", "content": req.full_system()}]
        for turn in req.history:
            # `turn.text` 理论上恒为 str，但历史里混进过 None 就会以同样的方式炸掉，
            # 所以这里也一并兜住 —— 报错信息（"messages[N]"）根本看不出是哪一轮。
            messages.append({"role": turn.role, "content": turn.text or ""})
        # ⚠️ 必须**非空字符串**：`None` 会被接口拒收（上面那次修复），
        #    空串会被模型当成"用户发了空消息"去吐槽（下面那段实测）。
        # 自主发言：优先用调用方给的**舞台提示**（截图对话会把画面描述放进来），
        # 没有就用通用那句。两者都**非空** —— 空串会被模型当成"用户发了空消息"。
        idle_turn = (req.stage_direction or "").strip() or IDLE_USER_TURN
        messages.append({"role": "user", "content": req.user_text or idle_turn})
        """
        工具结果回填（2026-10-12，agent loop 的第二轮）。

        ## ⚠️ 协议顺序是**硬要求**：`assistant(tool_calls)` 必须紧跟在
        ## `tool` 消息之前

        OpenAI 兼容接口对这条有校验。漏了 `assistant` 那一条的话，
        服务端报的是「tool 消息前面必须有一条带 tool_calls 的 assistant 消息」——
        而那个错**看起来像我们乱发协议**，实际是少了一环。

        所以这里一次补两条：

            {"role": "assistant", "content": "", "tool_calls": [...]}   <- 它要求调什么
            {"role": "tool", "tool_call_id": ..., "content": "..."}     <- 结果是什么

        ## 为什么 `content` 是空串而不是 None

        同 `user_text` 那条注释：OpenAI 兼容接口拒收 `None` 的 content。
        带 tool_calls 的 assistant 消息**本来就没有正文**（模型只说"我要调用"），
        空串是合法且语义正确的写法。
        """
        if req.tool_results:
            """
            ⚠️ `reasoning_content` **必须一起回填**（2026-10-12 修，实测 HTTP 400）。

            第一版只回了 `tool_calls` + 结果，DeepSeek 直接拒：

                HTTP 400：The `reasoning_content` in the thinking mode must be
                passed back to the API.

            取**第一条**的 reasoning 就够了 —— 同一条 assistant 消息只对应
            一份推理过程（哪怕它同时要求调用多个工具）。

            ## 🔴 `req.tools` 为空时要**换一种写法**（2026-10-12）

            「轮数用尽 -> 拿掉工具逼它回答」那条路径（见 `gateway._force_answer`）
            会保留 `tool_results`（那是它唯一的上下文）但把 `tools` 清空。

            而**协议不允许「有 tool 消息但没有 tools 字段」**：
            服务端会报「tool 消息必须是对某次 tool_calls 的响应，
            但这次请求没有声明任何工具」。

            所以这里必须跟着 `req.tools` 一起判断 —— 光看 `tool_results`
            会 400，而那个错看起来像「我们乱发协议」，实际是两处状态没对齐。
            """
            if req.tools:
                reasoning = next(
                    (item.reasoning for item in req.tool_results if item.reasoning), ""
                )
                assistant_message: dict[str, Any] = {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": item.call_id,
                            "type": "function",
                            "function": {"name": item.name, "arguments": item.arguments},
                        }
                        for item in req.tool_results
                    ],
                }
                # ⚠️ 只在**确实有**的时候加这个字段。空串在某些服务商那里
                # 可能被判为"提供了空推理"，反而出错。
                if reasoning:
                    assistant_message["reasoning_content"] = reasoning
                messages.append(assistant_message)
                for item in req.tool_results:
                    messages.append(
                        {"role": "tool", "tool_call_id": item.call_id, "content": item.content}
                    )
            else:
                """
                没有工具了，但结果还得让模型看见 —— 改成一段普通的
                **系统提示**塞进对话。

                为什么不用 `role: 'tool'`：那要求同时声明 `tools`（见上）。
                为什么不用 `role: 'user'`：会与用户真说的那句话混在一起，
                模型可能当成"用户又说了一遍"（这个坑见 `_generate` 里
                「历史快照必须在 append 之前取」那段长说明）。
                """
                gathered = "\n\n".join(item.content for item in req.tool_results)
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "以下是你刚才联网查到的资料（已经查完了，不要再查）：\n\n"
                            f"{gathered}"
                        ),
                    }
                )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
        }
        """
        工具清单（2026-10-12）。

        ⚠️ **为空时一个字节都不加** —— 理由与下面的 `thinking` 完全相同：
        别的 OpenAI 兼容服务不一定认 `tools` 字段，传过去可能直接 400。

        `tool_choice` 也**不传**：默认 `auto` 就是我们要的语义
        （模型自己决定要不要调用），显式传反而可能被某些服务商拒。
        """
        if req.tools:
            body["tools"] = req.tools
        """
        思考模式（用户要求做成开关，默认开启）。

        ## ⚠️ 为什么必须是"只有明确值时"才传

        `thinking` 是 **DeepSeek 专有参数**，而本项目的 `providers.brain`
        支持**任何 OpenAI 兼容接口**（DeepSeek / 硅基流动 / 阿里 / OpenAI…）。
        对不认识这个字段的服务商，多传一个未知键**可能导致 400**。

        所以 `req.thinking is None` 时**一个字节都不加** —— 由服务端默认决定。
        判断逻辑放在这里而不是 gateway：**"怎么发"是提供者的事**，
        gateway 只负责"发什么语义"。

        ## ⚠️ 实测过的两个坑

          · 只写 `{"reasoning_effort": "none"}` -> **HTTP 422**
            （`missing field 'type'`）—— `type` 是必填，不能只给 effort
          · 思考模式下 `temperature` **被服务端忽略**（官方原文
            "Has no effect in thinking mode"）—— 所以关掉思考之后
            回复风格会变，那是预期的，不是"关掉后变差了"
        """
        if req.thinking is not None:
            body["thinking"] = {"type": "enabled" if req.thinking else "disabled"}
        return body

    # ---------------------------------------------------------------- 流

    async def stream(self, req: LLMRequest) -> AsyncIterator[StreamEvent]:
        queue: asyncio.Queue[Any] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        cancel = threading.Event()
        state: dict[str, Any] = {"text": "", "usage": None}

        def emit(item: Any) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, item)

        def worker() -> None:
            try:
                for event in self._iter_sse(req, cancel, state):
                    if cancel.is_set():
                        break
                    emit(event)
            except Exception as exc:  # noqa: BLE001 —— 线程内异常必须转成事件
                log.warning("LLM 流式请求失败：%s", exc)
                emit({"kind": "error", "message": _humanize(exc)})
            finally:
                emit(_SENTINEL)

        thread = threading.Thread(target=worker, name="llm-stream", daemon=True)
        thread.start()

        full_text: list[str] = []
        try:
            async for item in self._forward(queue):
                if item.get("kind") == "delta":
                    full_text.append(item["text"])
                elif item.get("kind") == "error":
                    # 出错就收尾：已经吐出的半句话照样告诉前端，不要凭空吞掉
                    yield item
                    yield {"kind": "done", "text": "".join(full_text)}
                    return
                yield item
        finally:
            # 上层提前退出（用户打断 / 前端断开）时也主动断开 HTTP 连接
            cancel.set()
        yield {"kind": "done", "text": "".join(full_text)}

    async def _forward(self, queue: "asyncio.Queue[Any]") -> AsyncIterator[dict[str, Any]]:
        """把线程投递的事件按顺序泵出来，直到哨兵。"""
        while True:
            item = await queue.get()
            if item is _SENTINEL:
                return
            yield item

    # ---------------------------------------------------------------- SSE 解析

    def _iter_sse(
        self, req: LLMRequest, cancel: threading.Event, state: dict[str, Any]
    ) -> Iterator[StreamEvent]:
        """在工作线程里阻塞地读 SSE，逐条 yield 增量。

        ## 工具调用是**分片到达**的（2026-10-12）

        实测 `deepseek-flash` 的流式响应里，一个工具调用会拆成好几条 chunk：

            delta.tool_calls[0] = {index:0, id:"call_abc", function:{name:"web_search"}}
            delta.tool_calls[0] = {index:0, function:{arguments:"{\\"query\\":"}}
            delta.tool_calls[0] = {index:0, function:{arguments:"\\"5090\\"}"}}

        所以必须**按 `index` 累积**，等流结束再当成一条完整的调用发出去。
        ⚠️ 一条 chunk 里可能有**多个** index（模型并行发两个搜索就是这样，
        实测见过），所以内层也要循环。

        ## ⚠️ 为什么最后才 emit，而不是边收边发

        `arguments` 是 JSON 字符串，收到一半时**它不是合法 JSON**。
        边收边发的话上层要么解析失败、要么得自己拼 —— 那是把复杂度推给调用方。
        流结束（`[DONE]` / EOF）时一次性给出完整结构最干净。
        """
        payload = json.dumps(self._body(req), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint(),
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "text/event-stream",
                "User-Agent": "desktop-pet/0.1",
            },
        )
        # 每个工具调用一个槽位，键是 index（模型给的）
        slots: dict[int, dict[str, Any]] = {}
        # 不用 urlopen 全局超时：流式连接会持续很久，只设读超时
        with netutil.urlopen(request, timeout=self.timeout_s) as response:
            if response.status != 200:
                raise urllib.error.HTTPError(
                    self._endpoint(), response.status, "非 200 响应", response.headers, None
                )
            while not cancel.is_set():
                raw = response.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    log.debug("跳过无法解析的 SSE 数据块：%s", data[:120])
                    continue
                if isinstance(chunk.get("usage"), dict):
                    state["usage"] = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield {"kind": "delta", "text": piece}
                    """
                    ⚠️ **思考模式的推理过程要收集起来**（2026-10-12，实测踩到）。

                    它**不是**要说的话（不能拼进正文、不能进 TTS），
                    但如果这一轮要求了工具调用，下一轮**必须把它原样送回**：

                        HTTP 400：The `reasoning_content` in the thinking mode
                        must be passed back to the API.

                    所以攒在 `state` 里，流结束时随 `tool_calls` 一起交给上层。
                    """
                    reasoning = delta.get("reasoning_content")
                    if reasoning:
                        state["reasoning"] = str(state.get("reasoning") or "") + str(reasoning)
                    _accumulate_tool_calls(delta.get("tool_calls"), slots)
                    """
                    ## 🔴 `finish_reason` 要**收集**起来（2026-10-12）

                    原注释写的是「不做处理：交由 [DONE] 或 EOF 收尾，避免提前截断」——
                    "不要因为收到 finish_reason 就立刻停"这个判断是对的，
                    但**连记都不记**就丢掉了唯一能说明"为什么回复是空的"的线索。

                    实测踩到的坑（用户报「我说了话，但是它没有回答」）：

                        finish_reason='length'
                        completion_tokens=400
                        completion_tokens_details={'reasoning_tokens': 400}   <- 全花在推理上
                        正文 delta 共 0 字

                    思考模式下**推理和正文共享 `max_tokens` 预算**。
                    推理把预算吃光时，正文一个 token 都不剩 ——
                    而流本身是正常结束的（`[DONE]` 照来），所以谁都不报错。

                    现在把它放进 `state`，由上层决定要不要补救（见 gateway 的重试）。
                    """
                    if choice.get("finish_reason"):
                        state["finish_reason"] = str(choice["finish_reason"])
        if state.get("usage"):
            log.debug("本次用量：%s", state["usage"])
        """
        每轮结束时把 `finish_reason` 报给上层（2026-10-12）。

        ⚠️ **无条件发**，即使没有工具调用 —— 它是"这一轮有没有被截断"的
        唯一信号，而截断的表现可能是"回复空白"（见上面收集处的长说明）。

        放在 `tool_calls` 事件**之前**：上层拿到 tool_calls 就会去执行工具，
        而它需要先知道"这一轮是不是已经被截断了"。
        """
        if state.get("finish_reason"):
            yield {"kind": "finish", "finish_reason": state["finish_reason"]}
        if slots:
            # 按 index 排序 —— 并发调用时到达顺序不保证与 index 一致
            calls = [slots[key] for key in sorted(slots)]
            log.info(
                "模型要求调用 %d 个工具：%s",
                len(calls),
                "、".join(str(call.get("name")) for call in calls),
            )
            yield {
                "kind": "tool_calls",
                "calls": calls,
                # 上层回填时要带回去（见 `ToolExchange.reasoning` 的说明）
                "reasoning": str(state.get("reasoning") or ""),
            }


def _accumulate_tool_calls(raw_calls: Any, slots: dict[int, dict[str, Any]]) -> None:
    """把一条 chunk 里的 `delta.tool_calls` 累积进槽位表（2026-10-12）。

    ## 分片规则（实测 deepseek-flash）

        {"index": 0, "id": "call_abc", "function": {"name": "web_search"}}
        {"index": 0, "function": {"arguments": "{\\"query\\":"}}
        {"index": 0, "function": {"arguments": "\\"5090\\"}"}}

    只有**第一片**带 `id` 和 `name`，后面全是 `arguments` 的碎片。
    所以三个字段都要"有就更新、没有就保留"。

    ## ⚠️ 三个容易写错的地方

    · **`index` 缺省时当 0** —— 有的服务商在单调用时不发 index。
      用 `or 0` 而不是 `get(..., 0)`：显式的 `null` 也要落到 0。
    · **`arguments` 是拼接，不是覆盖** —— 覆盖的话只剩最后一片，
      JSON 必然不完整。
    · **`name` 可能出现在后面的片里**（少见但合法），所以不能只在第一片取一次。
    """
    if not isinstance(raw_calls, list):
        return
    for raw in raw_calls:
        if not isinstance(raw, dict):
            continue
        index = raw.get("index")
        index = 0 if index is None else int(index)
        slot = slots.setdefault(index, new_tool_call(index))
        if raw.get("id"):
            slot["id"] = str(raw["id"])
        function = raw.get("function")
        if not isinstance(function, dict):
            continue
        if function.get("name"):
            slot["name"] = str(function["name"])
        piece = function.get("arguments")
        if piece:
            slot["arguments"] = str(slot.get("arguments") or "") + str(piece)


def _humanize(exc: BaseException) -> str:
    """把底层异常翻译成用户能看懂的一句话。"""
    if isinstance(exc, urllib.error.HTTPError):
        detail = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
            detail = (
                parsed.get("error", {}).get("message")
                if isinstance(parsed.get("error"), dict)
                else parsed.get("message", "")
            ) or body[:200]
        except Exception:  # noqa: BLE001
            detail = ""
        table = {
            401: "API 密钥无效或已过期",
            402: "账户余额不足",
            403: "该密钥没有访问此模型的权限",
            404: "模型名或 base_url 不对",
            429: "请求过于频繁或已达配额上限",
            500: "模型服务端错误",
            502: "模型服务网关错误",
            503: "模型服务暂时不可用",
        }
        hint = table.get(exc.code, f"HTTP {exc.code}")
        return f"{hint}{('：' + detail) if detail else ''}"
    if isinstance(exc, urllib.error.URLError):
        return f"连不上模型服务（{exc.reason}）—— 检查网络或 base_url"
    if isinstance(exc, TimeoutError):
        return "模型响应超时"
    return f"{type(exc).__name__}: {exc}"


def build_openai(config: Config, emotion: EmotionState | None = None) -> Brain:
    """
    提供者工厂。

    ⚠️ **签名必须与 `brains/__init__.py` 里注册表声明的
    `Callable[[Config, EmotionState], Brain]` 一致**（即两个参数）。

    这里踩过一个很隐蔽的坑：早先只写了 `config` 一个参数，而注册表是按
    `factory(config, emotion)` 调用的。于是只要把 `providers.brain.provider`
    改成 `openai`，就会抛 `TypeError`，然后被 `build_brain` 的兜底逻辑
    **静默降级成 mock** —— 界面上一切正常，只是宠物开始说假话。
    而单元测试当时是直接 `OpenAIBrain(config)` 绕过工厂的，所以测试全绿。

    `emotion` 目前用不到（情绪影响的是提示词，由 LLMRequest 带进去），
    保留它是为了统一工厂签名。
    """
    del emotion  # 显式说明：有意不使用
    return OpenAIBrain(config)
