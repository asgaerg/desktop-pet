"""大脑层共享类型：请求、流事件、情绪状态机。"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Optional

from ..protocol import EMOTIONS, clamp01

Role = Literal["user", "assistant"]


@dataclass(slots=True)
class Turn:
    """一轮对话。role 只有 user / assistant 两种 —— system 永远由当前人格现拼。"""

    role: Role
    text: str
    ts: float = field(default_factory=time.time)


@dataclass(slots=True)
class LLMRequest:
    """一次生成请求。提供者只能看到这个结构，不知道之上是打字还是语音。"""

    system: str
    history: list[Turn]
    # None 表示**自主发言**：没有用户消息，模型要自己起话头。
    # 用一个显式的 None 而不是空字符串，是为了让"没说话"和"说了空话"不可能混淆。
    user_text: Optional[str]
    temperature: float = 0.85
    max_tokens: int = 400
    timeout_s: float = 30.0
    # 额外拼到 system 末尾的上下文（例如"你刚看到屏幕上是什么"）。
    # 用单独字段而不是直接塞进 system：让"人格"与"当下感知"在提示词里来源清晰，
    # 排查"它为什么这么说"时能一眼看出是哪一段在起作用。
    context_hint: str = ""
    """
    自主发言时那条 user 消息的**内容**（2026-10-17，截图对话引入）。

    ## 为什么不是 user_text

    `user_text is None` 是"自主发言"的判定依据（见 `is_idle`）。把描述塞进
    `user_text` 会让它被当成"用户说的话"：写进历史、计入注意力、进记忆提炼 ——
    语义全乱。

    ## 为什么不能只放 context_hint

    `context_hint` 进的是 **system**，而 system 里还堆着人设、状态、记忆召回。
    实测（截图对话）模型会抓记忆里的旧话题，**无视**画面描述：
    用户截图 Steam 商店，它回「命令行滚成那样，是又在跑构建吗」。

    所以另开一个字段，由 provider 放进 **user 那一轮** —— 那是模型最当真的位置。
    空字符串表示"用内置的通用舞台提示"（见 `openai_compat.IDLE_USER_TURN`）。
    """
    stage_direction: str = ""

    """
    思考模式（DeepSeek 的 `thinking` 参数），用户可在宠物菜单里开关。

    ## `None` 与 `True/False` 是**三种不同**的语义，别合并

      · `None`  -> **不传这个参数**，完全由服务端默认决定
      · `True`  -> 显式传 `{"type": "enabled"}`
      · `False` -> 显式传 `{"type": "disabled"}`

    为什么需要 `None` 这一档：**别的 OpenAI 兼容服务不认识 `thinking` 字段**，
    传过去可能直接 400。而本项目的 `providers.brain` 就是"任何 OpenAI 兼容接口"
    （DeepSeek / 硅基流动 / 阿里 / OpenAI 都行）。

    所以默认值是 `None`（不传 = 对任何服务商都安全），
    由 `gateway` 在**确认是 DeepSeek** 时才填 `True/False`。
    """
    thinking: Optional[bool] = None

    """
    可调用的工具（function calling，2026-10-12 加）。

    ## 为什么需要它

    用户要「联网功能：让他自己去网上搜索，就像小鲸你一样，自己调用」。
    实测确认模型**支持函数调用**（`deepseek-flash` 一次就发了两个
    `web_search` 调用），但项目原来的 `LLMRequest` 里**没有这个字段** ——
    工具递不进去，模型无从选择。

    ## 结构是 OpenAI 的 shape，一字不改

    ```python
    [{"type": "function",
      "function": {"name": "web_search",
                   "description": "联网搜索。需要当前或不确信的信息时使用。",
                   "parameters": {"type": "object",
                                  "properties": {"query": {"type": "string"}},
                                  "required": ["query"]}}}]
    ```

    ⚠️ **不为"更好看"重排这个结构**：它是发给服务商的原始 payload，
    中间任何一层翻译都会多一个出错机会，而且报错信息只会说
    "tools[0].function 缺字段"，看不出是哪层改坏的。

    ## 为空时不传

    `None` / 空列表 -> 请求体里**一个字节都不加**。
    理由与 `thinking` 完全相同：别的 OpenAI 兼容服务不一定认这个字段。
    """
    tools: Optional[list[dict[str, Any]]] = None

    """
    助手要求调用过什么 + 结果是什么（2026-10-12）。**回填给模型的第二轮**。

    ## 一轮工具调用长什么样

        ┌─ 第 1 轮 ────────────────────────────────────────┐
        │ 请求：system + 历史 + 用户的话 + tools            │
        │ 回复：delta「我查一下」 + tool_calls[web_search]  │
        │       + **reasoning_content**（思考模式的推理过程）│
        └──────────────────────────────────────────────────┘
                            ↓ 我们执行 web_search
        ┌─ 第 2 轮 ────────────────────────────────────────┐
        │ 请求：上面那些 + assistant(tool_calls + reasoning)│
        │                + tool(结果)                       │
        │ 回复：delta「查到了，5090 是……」                  │
        └──────────────────────────────────────────────────┘

    ## 🔴 `reasoning` 不带回去就是 **HTTP 400**（实测）

    第一版只回填了 `tool_calls` + 结果，DeepSeek 直接拒：

        HTTP 400：The `reasoning_content` in the thinking mode must be passed
        back to the API.

    这是**思考模式特有的要求**：带 tool_calls 的那条 assistant 消息必须
    把它当时的 `reasoning_content` 一并送回，否则服务端认为上下文不完整。

    ⚠️ 而 `thinking: None`（不传该参数）时服务商可能自己按默认开了思考 ——
    所以这条**不能只在"用户开了思考模式"时才走**，得看实际有没有收到
    `reasoning_content`。收到就必须带回，这是硬要求。

    ## ⚠️ 协议顺序也是硬要求

        {"role": "assistant", "content": "", "tool_calls": [...], "reasoning_content": "..."}
        {"role": "tool", "tool_call_id": ..., "content": "..."}

    漏了 assistant 那一条，服务端会报「tool 消息前面必须有一条带 tool_calls
    的 assistant 消息」—— 那个错看起来像我们乱发协议，实际是少了一环。

    ## 为什么不用 `history`

    `history` 的元素是 `Turn(role, text)`，而 `role` **只有 user/assistant
    两种**（见 `Turn` 的注释：「system 永远由当前人格现拼」）。
    工具结果的角色是 `tool` —— 硬塞进去会打破那个不变量。
    """
    tool_results: Optional[list[ToolExchange]] = None

    @property
    def is_idle(self) -> bool:
        return self.user_text is None

    def full_system(self) -> str:
        if not self.context_hint:
            return self.system
        return f"{self.system}\n\n{self.context_hint}"


# 流事件：提供者统一吐这几种，上层不必区分是 mock 还是真实模型
StreamEvent = dict[str, Any]
"""
{'kind': 'delta'|'done'|'error'|'tool_calls'}

## ⚠️ `tool_calls` 必须是**独立事件类型**，不能混进 `delta`（2026-10-12）

`delta` 的语义是「宠物要说的话」，它的内容会被直接拼进回复文本、
送去 TTS、显示在气泡里。而工具调用是**结构化数据**（函数名 + JSON 参数），
拼进正文会变成一串没人看得懂的乱码，还会被念出来。

所以两者严格分开：

    {'kind': 'delta',      'text': '我查一下'}         <- 要说的话
    {'kind': 'tool_calls', 'calls': [ {...}, ... ]}    <- 要做的事

一次流里**两者都可能出现**：模型常常先说一句"我查一下"，再发工具调用。
"""


def new_tool_call(index: int = 0, call_id: str = "", name: str = "") -> dict[str, Any]:
    """构造一个"正在累积中"的工具调用槽位。

    流式协议里工具调用是**分片到达**的（先是名字，再是参数的片段），
    所以要有个可变结构慢慢攒。抽成函数是为了让"槽位长什么样"
    在提供者之间保持一致 —— 上层（agent loop）只认这个结构。
    """
    return {"index": index, "id": call_id, "name": name, "arguments": ""}


@dataclass(slots=True)
class ToolExchange:
    """一次工具调用 + 它的结果（agent loop 回填用）。

    用数据类而不是元组：这类结构**字段会随时间增加**
    （`reasoning` 就是后加的 —— 加它之前元组已经用到四元了，
    再加第五个位置参数就没有可读性了）。
    """

    call_id: str
    name: str
    # ⚠️ **原样回填**我们收到的那串 JSON 字符串 ——
    # 重新序列化可能因空格/键序不同而与服务端记录对不上。
    arguments: str
    # 执行结果（给模型看的文本）
    content: str
    """
    带 tool_calls 那条 assistant 消息的 `reasoning_content`。

    ⚠️ **思考模式下它是必填的** —— 不带回去服务端直接 400：
    「The reasoning_content in the thinking mode must be passed back」。
    """
    reasoning: str = ""


class Brain:
    """
    生成提供者的抽象基类。

    P0 只要求"流式吐字"这一个能力。将来要加"看图说话"时，扩展的是一份新的
    请求结构（VisionRequest），而不是把这个类改胖 —— 那正是协议分层的意义。
    """

    name: str = "brain"

    async def stream(self, req: LLMRequest) -> AsyncIterator[StreamEvent]:  # pragma: no cover - 抽象
        raise NotImplementedError
        yield {}  # 让类型检查知道这是异步生成器


class EmotionState:
    """
    情绪三维度：精力 / 情绪 / 亲近度，取值 0~1。

    作用不是装饰，而是让宠物"不是复读机"：
      · 影响台词口吻（这里传给 mock 与 system prompt 的活泼度描述）；
      · 影响立绘表情（通过 pet.emotion 事件）；
      · 将来影响自主发言意愿（P5 的 Autonomy 会读它）。

    动力学很简单，但必须"缓慢变化 + 自然回落"：一轮对话不改人格。
    """

    EMOTIONS = EMOTIONS

    def __init__(self, energy: float = 0.7, mood: float = 0.55, intimacy: float = 0.2) -> None:
        self.energy = clamp01(energy, 0.7)
        self.mood = clamp01(mood, 0.55)
        self.intimacy = clamp01(intimacy, 0.2)
        self._last_update = time.time()
        # 好奇心到期时间（截图对话戳"好奇"表情用，见 on_curious）
        self._curious_until = 0.0

    def tick(self) -> None:
        """时间流逝：精力缓慢衰减，情绪回归中性。挂机久了会犯困。"""
        now = time.time()
        elapsed = min(now - self._last_update, 600.0)
        self._last_update = now
        if elapsed <= 0:
            self._last_update = now
            return
        minutes = elapsed / 60.0
        self.energy = clamp01(self.energy - 0.012 * minutes, self.energy)
        self.mood = clamp01(
            self.mood + (0.5 - self.mood) * min(0.08 * minutes, 0.5), self.mood
        )

    def on_user_message(self, text: str) -> None:
        """被打招呼就精神一振；长消息稍微更费神。"""
        self.tick()
        self.energy = clamp01(self.energy + 0.10, self.energy)
        self.mood = clamp01(self.mood + 0.04, self.mood)
        self.intimacy = clamp01(self.intimacy + min(0.02 + len(text) / 4000.0, 0.05), self.intimacy)

    def on_curious(self, seconds: float = 90.0) -> None:
        """
        让表情**在接下来一段时间里是「好奇」**（截图对话用）。

        为什么不复用 `dominant` 里那条随机分支：那是 `random.random() < 0.3`
        的偶发好奇，而"用户刚给我看屏幕"应该是**确定地**一脸好奇。

        ⚠️ 只有 90 秒也是刻意的 —— 好奇不能变成永久表情。
        """
        self._curious_until = time.time() + max(1.0, seconds)

    def on_reply_done(self, length: int) -> None:
        """说话消耗精力。说得越多越累 —— 这是"活泼度"之外的自然制动。"""
        self.energy = clamp01(self.energy - min(0.01 + length / 3000.0, 0.05), self.energy)

    @property
    def dominant(self) -> str:
        """映射到立绘表情用的情绪标签。"""
        if self.energy < 0.28:
            return "sleepy"
        if self.mood > 0.78 and self.energy > 0.6:
            return "excited"
        if self.mood > 0.62:
            return "happy"
        if self.mood < 0.3:
            return "annoyed"
        # 被触发的好奇心优先于随机好奇（截图对话会调 on_curious）。
        # ⚠️ 它是**有时限**的：不能因为看过一次截图就永远一脸好奇。
        if self._curious_until and time.time() < self._curious_until:
            return "curious"
        if self.intimacy > 0.5 and random.random() < 0.3:
            return "curious"
        return "neutral"

    def as_dict(self) -> dict[str, object]:
        """
        情绪快照。

        ⚠️ `dominant` 是**字符串**（不是 0~1 的数值），所以返回类型是
        `dict[str, object]` 而不是 `dict[str, float]`。

        为什么要带上它（2026-10-21，「把情绪接完」）：
        离散表情的阈值只在**后端**这一处算（见 `dominant`），渲染层以前拿不到它，
        只能自己从 mood 反推，于是"prompt 里说她有点不爽、立绘却在笑"。
        现在标签跟着快照一起走，两边用的是同一个判断。
        """
        return {
            "energy": round(self.energy, 3),
            "mood": round(self.mood, 3),
            "intimacy": round(self.intimacy, 3),
            "dominant": self.dominant,
        }

    def describe(self) -> str:
        """给 system prompt 用的一句状态描述。"""
        if self.energy < 0.3:
            return "你现在很困，反应慢，说话懒洋洋的。"
        if self.mood > 0.75:
            return "你现在心情特别好，话里带笑。"
        if self.mood < 0.3:
            return "你现在有点不爽，说话冲一点但别刻薄。"
        return "你状态平稳。"
