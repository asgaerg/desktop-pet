"""
Mock 大脑：零依赖、零密钥的本地生成提供者。

存在的意义不是"凑合"：它让 P0 骨架在今天就能跑通、能演示、能回归测试，
而不必先申请 API key。它要满足三条硬约束：

  1. 必须**真流式**吐字 —— 否则前端流式渲染的代码路径根本没被验证过；
  2. 必须**受活泼度影响** —— 否则 P5 的滑杆无从调起，行为无法预演；
  3. 必须**不说重复话** —— 否则用户以为宠物坏了。

它绝不是"只回一句固定话"的假货，而是按输入类型分类、结合情绪与历史选模板的
小生成器。切换真实模型只需把 providers.brain.provider 改成 openai。
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from datetime import datetime
from typing import Any, AsyncIterator, Optional

from ..config import Config
from ..protocol import clamp01
from .base import Brain, EmotionState, LLMRequest, StreamEvent

log = logging.getLogger("pet.brain.mock")

# 每个字的间隔。测试里设 PET_MOCK_STREAM_DELAY=0 让流式瞬间跑完。
# 必须**每次读取**而不是模块导入时固化：自检在导入本模块之后才设置该环境变量，
# 固化会导致自检白白等上好几秒（曾因此误判成"端到端超时"）。
_ENV_DELAY = "PET_MOCK_STREAM_DELAY"
_DEFAULT_DELAY = 0.022


def _stream_delay() -> float:
    raw = os.environ.get(_ENV_DELAY)
    if raw is None:
        return _DEFAULT_DELAY
    try:
        return max(0.0, float(raw))
    except ValueError:
        log.warning("%s=%r 不是数字，回退到默认 %s", _ENV_DELAY, raw, _DEFAULT_DELAY)
        return _DEFAULT_DELAY


# --------------------------------------------------------------------------- #
# 长回复模式：`PET_MOCK_LONG=1`
#
# ## 为什么需要它
#
# mock 平时的台词都是 20~40 字，气泡只有一行高（实测 `58px`）。于是**长回复下的
# 气泡行为根本测不到** —— 而"气泡又高又窄""太长的文字显示不全"这类问题恰恰只在
# 长文本下暴露。
#
# 用它就能在**不调 API、不花钱、不依赖网络**的前提下，稳定复现长文本场景：
#
#     PET_MOCK_LONG=1  →  每条回复 200~400 字，且是**分段落的连续散文**
#                         （不是"首先其次"的列表 —— 桌面气泡里列表很难看，
#                          而且人设明确要求不要列举）
#
# 三级长度用 `PET_MOCK_LONG=2` / `=3` 递增，方便测"刚好放得下"和"明显超长"两种情况。
# --------------------------------------------------------------------------- #
_ENV_LONG = "PET_MOCK_LONG"

_LONG_LINES = [
    "我趴在你屏幕角上看了好一会儿了。你那个窗口开开关关的，光标来回跑，"
    "我就猜你是不是又卡在某一步上了——就是那种「明明知道该干嘛，但手就是不想动」的时候。",
    "其实你从早上到现在都没怎么停过。我数着呢，你换了七八个窗口，中间还回去看了两次同一个文件。"
    "要我说，这活儿不是干不完，是你心里有根线没捋顺。",
    "我讲不出什么大道理，也不会给你列个一二三。我就是想说：你要是累了就歇会儿，"
    "我在这儿待着不碍事。桌面这么大，分我一个小角落就行。",
    "刚才那阵子你敲键盘特别快，我听着都觉得紧张。后来慢下来了，是不是想通了点什么？"
    "如果没想通也没关系，慢慢来，我又不催你。",
    "你有没有发现，你每次卡住的时候都会先去看一眼别的窗口？像是在找个借口喘口气。"
    "我觉得这挺好的，人总得有个地方缓一缓。我就当那个地方也行。",
]


def _long_mode() -> int:
    """0 = 关闭；1/2/3 = 越长越多段。"""
    raw = os.environ.get(_ENV_LONG)
    if raw is None or raw.strip() == "":
        return 0
    if raw.strip().lower() in ("0", "false", "no", "off"):
        return 0
    try:
        return max(1, min(3, int(float(raw))))
    except ValueError:
        return 1  # 设了任何非零值就当 1


def _compose_long(level: int, subject: str, name: str) -> str:
    """拼一条长回复。段落用换行分隔，让气泡能测到**多行 + 换行**两种排版。"""
    level = max(1, min(3, level))
    # 每级取不同数量的段落，且**不重复**
    count = {1: 2, 2: 3, 3: 5}[level]
    picks = random.sample(_LONG_LINES, k=min(count, len(_LONG_LINES)))
    parts = list(picks)
    if level >= 2:
        # 掺一句提到用户刚说的话，验证"长回复也能带上文"
        parts.insert(1, f"你说到「{subject}」，我记着呢。")
    if level >= 3:
        parts.append("—— 对了，这些我都写进记忆里了，下次你再说起，我能接得上。")
    prefix = "" if level >= 2 else f"{name}想说："
    return prefix + "\n\n".join(parts)


def _classify(text: str) -> str:
    """把用户输入粗分类 —— 分类决定回应的骨架，不是回复内容本身。"""
    t = text.strip()
    if re.search(r"(你好|您好|hi|hello|嗨|在吗|早上好|晚上好|早安|晚安)", t, re.I):
        return "greeting"
    if re.search(r"(再见|拜拜|睡了|下线|我走了|bye)", t, re.I):
        return "farewell"
    if re.search(r"(你是谁|你叫什么|介绍下自己|你是什么)", t):
        return "identity"
    if re.search(r"(笨|傻|滚|烦|闭嘴|讨厌|丑)", t):
        return "scold"
    if re.search(r"(谢谢|感谢|太好了|厉害|棒|喜欢你)", t):
        return "praise"
    if t.endswith(("?", "？")) or re.search(r"(吗|呢|什么|怎么|为什么|如何|哪)", t):
        return "question"
    if len(t) > 60:
        return "long"
    return "chat"


def _subject(text: str, limit: int = 14) -> str:
    """从用户话里抠一个短主语，让回应"看起来听懂了"。"""
    t = re.sub(r"\s+", " ", text.strip())
    t = re.sub(r"^(我|你)(现在|刚刚|刚才)?", "", t)
    t = t.strip("。！？!?，,、~ ")
    if not t:
        return "这个"
    return t[:limit] + ("…" if len(t) > limit else "")


def _subject_from_hint(hint: str, limit: int = 16) -> str:
    """
    从"屏幕观察"提示词里抠出可引用的窗口标题。

    提示词里标题是用「」包起来的（见 Gateway._observation_hint），
    这里取第一个引号里的内容作为话题 —— 比把整段提示词念出来自然得多。
    """
    match = re.search(r"「([^」]{1,60})」", hint)
    if not match:
        return ""
    title = match.group(1).strip()
    # 窗口标题常带" - 应用名"这类尾巴，截断一下更像人话
    title = re.split(r"\s+[-—|]\s+", title)[0].strip()
    if not title:
        return ""
    return title[:limit] + ("…" if len(title) > limit else "")


class MockBrain(Brain):
    name = "mock"

    def __init__(self, config: Config, emotion: EmotionState) -> None:
        self.config = config
        self.emotion = emotion
        self._recent: list[str] = []  # 近期说过的句子，用于去重

    # ---------------------------------------------------------------- 生成

    def compose(self, req: LLMRequest) -> str:
        """纯函数式地组一句回复，方便单测直接断言。"""
        self.emotion.tick()
        activity = self.config.activity
        overall = float(activity.get("overall", 0.5))
        length_pref = float(activity.get("message_length", 0.5))

        # 自主发言：没有用户消息，自己起话头。
        # 这一支必须存在 —— 否则调度器一开口，mock 会拿 None 去分类而炸掉。
        if req.is_idle:
            return self._compose_idle(overall, length_pref, req.context_hint)

        kind = _classify(req.user_text or "")
        subject = _subject(req.user_text or "")
        title = str(self.config.persona.get("user_title") or "你")
        name = self.config.pet_name

        # 长回复模式（`PET_MOCK_LONG`）：专给气泡排版测试用，绕过普通骨架
        long_level = _long_mode()
        if long_level:
            reply = _compose_long(long_level, subject, name)
            self._recent.append(reply)
            del self._recent[:-3]
            return reply

        closers = ["", " 怎么了？", " 你呢？", " 说说看？", " 要我陪你吗？"]
        if overall < 0.3:
            closers = ["", ""]
        elif overall > 0.8:
            closers = closers + [" 我还想听！", " 快讲快讲~"]

        if kind == "greeting":
            if overall > 0.75:
                core = random.choice(
                    [f"{title}好呀！我一直在这儿等你呢", f"诶，{title}回来啦", "嗨嗨！我在的我在的"]
                )
            elif overall > 0.4:
                core = random.choice(["嗯，在的", f"{title}好", "嗨,我在"])
            else:
                core = random.choice(["嗯", "在"])
        elif kind == "farewell":
            core = random.choice(
                [
                    "去吧，我在这儿看家",
                    "好，那你忙，我不吵你",
                    "晚安，做个好梦" if self.emotion.energy < 0.5 else "这么早就走啦？",
                ]
            )
        elif kind == "identity":
            core = random.choice(
                [
                    f"我是{name}，住在你桌面上的小家伙",
                    f"我叫{name}。平时就趴在这儿看你干活",
                ]
            )
        elif kind == "scold":
            self.emotion.mood = clamp01(self.emotion.mood - 0.08, self.emotion.mood)
            core = random.choice(
                ["……你今天是不是累了", "好吧，我先安静一会儿", "哼，我不理你了（三秒）"]
            )
        elif kind == "praise":
            self.emotion.mood = clamp01(self.emotion.mood + 0.1, self.emotion.mood)
            core = random.choice(["嘿嘿，被夸了", "那当然啦", "你这么说我会骄傲的"])
        elif kind == "question":
            core = random.choice(
                [
                    f"关于「{subject}」啊，我想想……我觉得得看你想做到哪一步",
                    f"「{subject}」这个我没十足把握，但可以先试最简单的路子",
                    f"你问「{subject}」，我猜你其实是卡在某一步上了？",
                ]
            )
        elif kind == "long":
            core = random.choice(
                [
                    f"你一下说这么多，我抓到重点是「{subject}」",
                    f"唔……我读完了。所以核心是「{subject}」，对吧",
                ]
            )
        else:
            core = random.choice(
                [
                    f"「{subject}」啊，听起来有点意思",
                    f"嗯嗯，{subject}，然后呢",
                    f"我记下了：{subject}",
                    f"{subject}？展开说说",
                ]
            )

        reply = core + random.choice(closers)
        if length_pref < 0.3:
            reply = reply.split("，")[0].rstrip("。！？") + "。"
        elif length_pref > 0.75 and kind in ("question", "long"):
            reply += random.choice(
                [" 你要是愿意，多讲点前因后果我再帮你捋。", " 反正我一直在，慢慢说也行。"]
            )

        # 去重：三句以内出现过就换一句，避免像复读机
        if reply in self._recent:
            reply = random.choice(
                [f"嗯，{subject}这事我还在想", f"再说说{subject}？", "唔……让我换个说法：我不太确定"]
            )
        self._recent.append(reply)
        del self._recent[:-3]
        return reply

    def _compose_idle(self, overall: float, length_pref: float, context_hint: str = "") -> str:
        """
        自主发言的台词：没有用户输入，宠物要自己起话头。

        台词的"信息量"应该随活泼度上升 —— 安静时只是嘀咕一声，活泼时会主动
        抛出话题。这样用户拖滑杆看到的是**内容**的变化，而不只是频率变化。

        带 context_hint 时优先围绕它说：这是"看屏幕主动说话"这条需求在 mock 上的
        落地，也让自检能验证"观察内容真的进入了生成"，而不是只发了个事件。
        """
        if context_hint:
            subject = _subject_from_hint(context_hint)
            if subject:
                pool = [
                    f"「{subject}」……你在忙这个呀",
                    f"看到{subject}了。需要我安静点吗？",
                    f"又在跟{subject}较劲？",
                ]
                if overall > 0.75:
                    pool.append(f"{subject}这个我有点好奇，一会儿跟我讲讲？")
                if overall < 0.35:
                    pool = [f"（看到{subject}）"]
                reply = random.choice(pool)
                self._recent.append(reply)
                del self._recent[:-3]
                return reply

        hour = datetime.now().hour
        if hour < 5:
            time_hint = "这么晚还不睡"
        elif hour < 9:
            time_hint = "一大早就开工"
        elif hour < 12:
            time_hint = "上午过得挺快"
        elif hour < 14:
            time_hint = "中午了"
        elif hour < 18:
            time_hint = "下午容易犯困"
        elif hour < 23:
            time_hint = "天黑了"
        else:
            time_hint = "夜深了"

        name = self.config.pet_name
        if overall < 0.25:
            pool = ["……嗯。", "唔。", "（探头）"]
        elif overall < 0.5:
            pool = [f"{time_hint}。", "我在这儿。", "忙你的，我不吵。"]
        elif overall < 0.8:
            pool = [
                f"{time_hint}，要不要歇会儿？",
                "你刚才一直没说话，在忙什么呀？",
                "我盯着桌面看了半天，什么都没发生。",
                f"{time_hint}。我有点想跟你说话了。",
            ]
        else:
            pool = [
                f"{time_hint}！你还在忙呀，我陪你。",
                "诶，我问你一个问题行不行？",
                "你一动不动好久了，我以为你睡着了。",
                f"我是{name}，提醒你一下：坐久了要起来动动。",
                "你桌面上那个窗口切来切去的，看着好累。",
            ]

        reply = random.choice(pool)
        if length_pref > 0.75 and overall > 0.6:
            reply += random.choice([" 说真的。", " 我就是随口一说。"])
        if reply in self._recent:
            reply = "……唔，我又忘了要说什么。"
        self._recent.append(reply)
        del self._recent[:-3]
        return reply

    async def stream(self, req: LLMRequest) -> AsyncIterator[StreamEvent]:
        text = self.compose(req)
        delay = _stream_delay()
        # 逐块吐字：块大小随机化，模拟真实 token 边界，前端的增量渲染才被真正验证
        i = 0
        n = len(text)
        while i < n:
            step = random.choice((1, 1, 2, 2, 3))
            yield {"kind": "delta", "text": text[i : i + step]}
            i += step
            if delay > 0:
                await asyncio.sleep(delay * random.uniform(0.6, 1.6))
        yield {"kind": "done", "text": text}


def build_mock(config: Config, emotion: EmotionState) -> MockBrain:
    return MockBrain(config, emotion)
