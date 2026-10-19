"""
Gateway —— 连接层：把 WebSocket 连接、事件总线、大脑三者缝在一起。

职责边界（刻意划清，方便以后替换任一端）：
  · WebSocket 只负责编解码与连接管理，不认识"对话"这件事；
  · 总线只负责事件分发，不认识"网络"这件事；
  · 本模块是唯一的翻译层：bus 事件名 <-> 协议消息类型。

对话生成放在 `Conversation` 里，而不是散在 Gateway 中：
它需要维护"历史 / 情绪 / 当前生成任务 / 打断"这几样有状态的东西，
集中在一处才能保证只有一个生成在跑、打断不会留下孤儿任务。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Optional

from . import paths
from .autonomy import Scheduler
from .brains import Brain, EmotionState, LLMRequest, Turn, build_brain
from .bus import Bus, Event
from .config import Config
from .embedding import build_embedder
from .plugins import PluginHub
from .protocol import PROTOCOL_VERSION, Incoming, ProtocolError, encode, msg, parse_incoming
from . import web
from .events import CAPABILITIES, EventEngine
from .segments import SegmentTracker
from . import quotes as quotes_mod
from .screen_chat import (
    capture_and_describe,
    describe_for_prompt,
    stage_direction_for,
    stage_direction_for_self,
)
from .subjectivity import SubjectiveState
from .vision import ChangeDetector, PrivacyRules, VisionLoop
from .vision_capture import build_capturer
from .voice import (
    BroadcastAudioOutput,
    QueueAudioInput,
    VoiceSession,
    build_voice_pipeline,
)
from .ws_server import WebSocketClient, WebSocketServer

log = logging.getLogger("pet.gateway")

# 截图说话之后的"闭嘴期"（秒）：截图优先于搭话（2026-10-18 用户规则）
_SCREEN_SPEAK_HOLD_S = 60.0

# bus 事件名 -> 下行协议消息类型。加事件时在这里登记一行即可。
_BUS_TO_WIRE: dict[str, str] = {
    "pet.say.start": "pet.say.start",
    "pet.say.delta": "pet.say.delta",
    "pet.say.end": "pet.say.end",
    "pet.emotion": "pet.emotion",
    "pet.mood": "pet.emotion",  # 别名，容忍笔误
    "pet.thinking": "pet.thinking",
    "pet.observe": "pet.observe",
    "pet.mouth": "pet.mouth",
    # 🔴 工具开始/结束（2026-10-16 补登记）。
    #
    # ## 为什么这条以前是坏的
    #
    # `tools.run_tool()` 一直在发 `pet.tool`（start / end），
    # 主进程 `index.ts` 也一直在收（据此把状态条切成蓝色「搜索」）——
    # **但没在这里登记**。而 `_on_bus_event` 对没登记的事件是
    # `return`（静默丢弃）：
    #
    #     wire_type = _BUS_TO_WIRE.get(event.name)
    #     if wire_type is None:
    #         return          # <- pet.tool 死在这里
    #
    # 后果：用户报「联网搜索的时候状态栏不显示」。
    # 而 `tools/test-tool-status.py` 测的是**侧车那一段**
    # （`run_tool` 发不发事件），它一直是通过的 ——
    # **测试通过、功能没通**，因为断点在两个测试的交界处。
    #
    # ⚠️ 这正是本项目那族毛病的又一次：**两边都写好了，中间那根线没接**。
    # 教训：加一个"事件 -> 线上消息"的通道时，**必须同时确认登记了**。
    #
    # ## 🔴🔴 而且我第一版修法**又踩了一个坑**（记下来，这个坑很隐蔽）
    #
    # 第一版我在这里插了一段 `"""..."""` 文档串，**紧接着**写
    # `"pet.tool": "pet.tool",`。看起来完全正常，但 Python 的
    # **相邻字符串字面量自动拼接**把两个字符串**粘成了一个**：
    #
    #     """...必须同时确认登记了。"""  "pet.tool"
    #     ->  '\n    🔴 工具开始/结束...\n    必须同时确认登记了。\n    pet.tool'
    #
    # 于是 `"pet.tool"` **没有成为 key**，它变成了上面那个文档串的结尾！
    # 字典里因此多了一个"用整段文档当 key"的垃圾项，而 `pet.tool`
    # 依然查不到 —— 修了等于没修。
    #
    # ⚠️ 为什么以前的写法"看起来没事"：那些文档串后面跟的是
    # **完整的键值对**（`"voice.tts_failed": "voice.tts_failed"`），
    # 拼接虽然也发生了，但**真正的 key 还在**，所以没暴露。
    # 我漏了 `: 值` 那一半，就把自己坑了。
    #
    # ✅ 所以：**在字典里写字面量时，说明一律用 `#` 注释，不要用裸字符串。**
    "pet.tool": "pet.tool",
    "system.error": "system.error",
    # 语音状态要显示在界面上（在听 / 在想 / 在说），否则用户不知道它有没有在听
    "voice.state": "voice.state",
    # TTS 合成失败的信号（2026-10-15 加）。
    #
    # ⚠️ **这条不是给渲染层看的**，是给**主进程**看的 —— 它据此把那一条
    # 降级成"显示文字"（见 `bubbleDecision.ts` 的 `ttsHealthy`）。
    #
    # 为什么需要：用户报「发不了声音 + 回复不了信息」，根因是**沉浸模式
    # 按"TTS 开着"判断有没有语音**，而实际合成 403 失败了 ——
    # 于是文字被主动隐藏、语音又没有，两头都收不到。
    #
    # 🔴 2026-10-16：这条原来也是**上面的坑的受害者** —— 它前面挂了一段
    # `"""..."""` 文档串，被 Python 拼成了一整个 key：
    #
    #     key = '\n    TTS 合成失败的信号（2026-10-15 加）。\n\n ... \n    voice.tts_failed'
    #     -> 'voice.tts_failed'
    #
    # 所以 `_BUS_TO_WIRE["voice.tts_failed"]` 一直查不到（虽然值是对的），
    # 真正的 key 是那一整段文档 —— 这条消息**从来没被广播过**。
    # 症状和用户报的「发不了声音」正好对得上（主进程收不到失败信号就
    # 不知道该降级成文字），只是当时没往这里查。
    #
    # ✅ 改成 `#` 注释后 key 才是 `voice.tts_failed`。
    "voice.tts_failed": "voice.tts_failed",
}

HISTORY_LIMIT = 200

# 主动开口场合说明的**内置默认**（用户可在 `prompts.idle_occasion` 覆盖）。
# 放在模块级而不是内联在方法里：它是"模板缺失时的兜底"，语义上属于配置默认值。
_IDLE_OCCASION_FALLBACK = (
    "现在是**你自己**想说话，用户并没有问你任何问题。"
    "说一句自然的、当下的开场话：可以是对眼前事情的评论、一个突然的念头、"
    "或者单纯打个招呼。**不要**说\"有什么可以帮您\"这类客服话，"
    "**不要**复述上面的设定，**不要**分点列举。一到两句就够。"
)

# 当前时间那段的**内置默认**（用户可在 `prompts.current_time` 覆盖）。
#
# ## 为什么必须告诉模型"现在是什么时候"（2026-10-11 用户要求）
#
# 用户原话：「给 ai 提示词的时候加上现在的时间，让它分清楚现在什么时候」。
#
# 不加的话模型**完全不知道今天是几号**，于是会：
#   · 把训练数据里的日期当成"现在"（以为还是几年前）
#   · 深夜说"早上好"、早上说"该睡了"
#   · 把早就过去的事说成"最近"
#   · 分不清工作日和周末
#
# ⚠️ **年份一定要给**。只给"几月几号星期几"时，模型仍会用自己的先验去补年份 ——
# 而那正是错得最离谱的地方。
#
# ⚠️ 措辞里明确**不要主动报时间**：平时顺嘴报时会非常出戏
# （与 `emotion` 那段"不要报数字"是同一条取舍）。
_CURRENT_TIME_FALLBACK = (
    "当前的真实时间：{time}（{weekday}，{part_of_day}）。"
    "这是**唯一权威**的时间 —— "
    "聊天记录里如果出现过别的时间（比如你昨晚说过「现在 12 点」，"
    "而那是**很久以前**的消息），那只是当时的说法，**不代表现在**。"
    "不要把历史消息里的时间当成当前时间，也不要把历史里的时间说法照搬过来。"
    "你的训练数据有截止日期，同样**不要把那个日期当成现在**。"
    "凡是涉及\"今天 / 现在 / 最近\"，一律以上面这个时间为准。"
    "⚠️ 不必主动报时间，也别为了显得知道而特意提 —— "
    "只在用户问到、或话题确实和时间有关时才用。"
)

# --------------------------------------------------------------------------- #
# 调试图标：对话条里发「1」-> 回一条固定长文
#
# ## 为什么需要它
#
# 用户要「看一下几百字内容在气泡里的具体效果」。他自己测的话得让 DeepSeek 生成
# 几百字 —— 那要**花钱**、要**等网络**、而且每次内容还不一样（没法比对）。
#
# 这个钩子给一段**固定的、分段落的**长文，于是：
#   · 完全离线、零成本、瞬间出结果；
#   · 每次内容一致，能反复比对不同气泡宽度/字号下的排版；
#   · 不依赖 `PET_MOCK_LONG`（那个只改 mock brain），**真实 provider 下也能用**。
#
# ## 与 `PET_MOCK_LONG` 的分工
#
#   PET_MOCK_LONG=1/2/3  -> 改 **mock brain 的每一条回复**（测流式、测各种长度）
#   发「1」               -> 触发**一条固定的长文**（测排版，与 provider 无关）
#
# ## 零足迹
#
# 不写记忆、不发提炼、不进 history —— 它是一次性的排版样本，
# 不是"宠物真的说了这句话"。否则用户试几次就把记忆库污染了。
# --------------------------------------------------------------------------- #
LONG_SAMPLE_TRIGGERS = {"1", "１", "１。"}  # 半角/全角都认

LONG_SAMPLE = """我把你屏幕上的东西都看了一遍，说几句实在的。

你那个编辑器从早上开到现在没关过，中间切出去十几次，每次回来光标都停在
差不多的地方。我猜不是不想动，是那一步卡着没想清楚，切出去是为了喘口气。

我数了一下，你今天打开了七八个窗口，同一个文件回去看了两遍。这种事我自己
也干过——不是忘了内容，是想确认一下"刚才那个判断到底对不对"。

要我说，这活儿不是干不完，是你心里有根线没捋顺。那根线捋顺了，
剩下的都是打字的时间。

我讲不出什么大道理，也不会给你列个一二三。我能做的就是在这儿待着，
你抬头能看见我，需要搭话的时候我在。桌面这么大，分我一个小角落就行。

对了我把这些都记住了。下次你再说起今天卡在哪一步，我能接得上话。"""


def _is_long_sample_trigger(text: str) -> bool:
    """判断这次输入是不是"给我看长气泡"的调试图标。

    刻意**只认完全等于 `1`**，不做"包含 1"这类模糊匹配 ——
    否则用户正常说「1 加 1 等于几」也会被劫持，那种 bug 很难查。
    """
    return text.strip() in LONG_SAMPLE_TRIGGERS


"""
工具调用的最大轮数（2026-10-12）。

## 为什么必须有上限

模型可能一直要调用（参数写错 -> 结果看不懂 -> 再调一次）。
没有上限就是**无限循环 + 无限烧钱**，而用户看到的是「宠物卡住了」——
不报错、不结束、只能强杀。

## 3 是怎么定的

实测一次「搜索 -> 读一篇 -> 回答」最多用两轮，3 留了一轮余量。
再多的话用户体验已经很差了（一次搜索 400ms、读网页 300ms，
但模型每轮还要生成，实际等待是**秒级**）。
"""
MAX_TOOL_ROUNDS = 3


def _default_state_dir() -> Path:
    """沙箱目录的默认落点：`apps/desktop/pet-data/`。

    ## 为什么是这个目录

    与记忆库（`pet-data/memory/pet.db`）、备份、日志**同一个地方** ——
    它们都是"应用的运行时数据"。放在一起的好处是用户想清理时只需删一个目录，
    而不会在别处留下孤儿文件。

    ## ⚠️ 为什么由调用方注入而不是写死

    与 `settingsStore` 同一个理由：硬编码绝对路径在换部署位置时
    **静默失效**（目录建不出来 -> 工具全挂 -> 宠物不再联网，而界面毫无提示）。

    这里是**兜底值**：配置里可以写 `web.state_dir` 覆盖它。
    """
    return paths.data_dir()


def _ms_to_s(value: Any) -> Optional[float]:
    """JS 的毫秒时间戳 -> Python 的秒（库里的 `ts` 是 `time.time()` 的秒）。

    ## ⚠️ 为什么必须显式换算（而不是让两边都用毫秒）

    库里的 `ts` 是 `time.time()` 写进去的**秒**（`raw_log.ts` / `recent_turn.ts`）。
    前端 `new Date(...).getTime()` 给的是**毫秒**，差 1000 倍。

    忘了换算的表现**极其隐蔽**：范围查询恒定返回空，而界面只会说
    "这段时间没有对话" —— 看起来像"那几天确实没聊过"，不会有人怀疑是单位问题。

    非法/缺失值返回 None（= 不加这个条件），而不是 0 ——
    `since_ts=0` 是"从 1970 年起"，虽然结果差不多，但语义不对。
    """
    if value is None or value == "":
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    # 大于这个量级说明给的是毫秒（秒级时间戳到 2286 年才 1e10）
    return n / 1000.0 if n > 1e11 else n



def screen_seen_note(trigger: Any, section: Any) -> str:
    """
    「它看到了什么」那一段（设置页开关控制，用户要求 2026-10-17）。

    ## 为什么只对截图对话那次回复生效

    用户的原话是「截图对话传来的**第一次回复**会控制在api说话的最后分一段
    带上图片的信息」。其它场合没有"图片信息"可带。

    ## 为什么这一段**不能进历史**

    `_generate` 里历史写的是 `final_text`，发出去的是 `final_text + 这一段`。
    如果历史也带上它，下一轮模型会以为"我刚才说过这话"，然后复述；
    而且它会被记忆提炼当成用户/宠物说过的话存下来 —— 那才是最脏的污染。

    ## 返回空串表示"不加"

    没开开关 / 不是截图对话 / 描述为空 —— 三种情况都返回空串，
    调用方据此决定要不要多这一句。
    """
    if not isinstance(trigger, dict) or trigger.get("source") != "snap":
        return ""
    if not isinstance(section, dict) or not section.get("show_description"):
        return ""
    seen = str(trigger.get("screen") or "").strip()
    if not seen:
        return ""
    return f"（补一句，我刚才看到的是：{seen}）"


def _snap_fields(result: Any) -> dict[str, Any]:
    """把 SnapResult 摊平成回执字段（只带可序列化的、对排查有用的）。"""
    try:
        data = result.as_dict()
    except Exception:  # noqa: BLE001
        return {}
    data.pop("ok", None)
    data.pop("message", None)
    data.pop("source", None)
    return data


class Conversation:
    """
    对话状态机：历史 + 情绪 + 单一生成任务 + 可打断。

    "同时只允许一个生成在跑"不是偷懒，是正确的产品行为：
    宠物不能同时说两句话，用户插话必须让旧的那句停下。
    """

    def __init__(self, bus: Bus, config: Config, memory: Any = None, brain: Any = None) -> None:
        self.bus = bus
        self.config = config

        # ── 人格主观层（2026-10-17）──
        #
        # 情绪不再是 Conversation 直接持有的裸 EmotionState，而是被
        # `SubjectiveState` 包起来（情绪 + 注意力 + 活泼度）。
        #
        # 🔴 顺手修了一个**早就存在的 bug**：原来这里是 `self.emotion = EmotionState()`，
        #    而 `Gateway._build_emotion()` 已经建了一个交给 `build_brain()` ——
        #    于是 **brain 持有的情绪实例和对话持有的不是同一个**。
        #    `_build_emotion()` 的 docstring 写着"由 Conversation 接管（两处引用同一个对象）"，
        #    但那句注释是假的：`Conversation` 自己又 new 了一个。
        #
        #    后果：brain 侧的情绪永远停在初始值（mock 的"这么早就走啦"判断、
        #    未来的表情/口吻联动都读的是那份假数据）。
        #    这类"两边都写好了、中间那根线没接"的 bug 之前踩过一次，这里一并修掉：
        #    **优先接管 brain 已经拿到的那个实例**。
        inherited = getattr(brain, "emotion", None) if brain is not None else None
        self.subjectivity = SubjectiveState(
            emotion=inherited if isinstance(inherited, EmotionState) else EmotionState()
        )
        # 活泼度镜像：从 activity.overall 同步（Autonomy 将来读它）
        self.sync_vitality()
        # brain 可以由外部注入：Gateway 需要把**同一个实例**同时交给对话与记忆提炼，
        # 两处各建一个会出现"提炼用的模型和对话用的不是同一个"这种诡异问题。
        self.brain: Brain = brain or build_brain(config, self.emotion)
        # 长期记忆（P2）。传 None 时记不住任何东西 —— 这一层是可选的，
        # 关掉它对话照常工作，只是宠物不再"记得你说过什么"。
        self.memory = memory
        # 截图回复的"待写图片"：说话前挂上，记账时取走并清空
        self._pending_image: bytes | None = None
        self._pending_image_mime: str | None = None
        self.history: list[Turn] = []
        self.current: Optional[asyncio.Task[None]] = None
        self._interrupt = asyncio.Event()
        self._say_seq = 0
        """
        工具清单（2026-10-12）。`None` = 还没构建过。

        ⚠️ **懒加载**：构建时要 `mkdir` 沙箱目录，而那可能失败。
        放在 `__init__` 里失败会让整个 Conversation 建不起来 ——
        宠物直接变哑巴。而工具是加分项，不该有这种权力。
        """
        self._tools: Optional[list[Any]] = None
        # 本次运行的会话 id：给每条记忆打上"哪一次对话"的标记，
        # 用户要"时间记号 + 分类查看"，session 维度是其中一维。
        self.session_id = time.strftime("s%Y%m%d-%H%M%S", time.localtime())
        # 「一段交流」的边界（2026-10-17）。
        # 段结束 -> 提炼印象 + 强化记忆。参数可被 autonomy 段覆盖。
        self.segment = SegmentTracker(
            gap_s=float(config.autonomy.get("segment_gap_s", 600.0)),
            max_turns=int(config.autonomy.get("segment_max_turns", 8)),
            min_turns=int(config.autonomy.get("segment_min_turns", 2)),
        )

        # ⚠️ **灌回历史**（用户要求 2026-10-08）—— 必须在最后，且在任何请求之前。
        #
        # 以前"重启后记得聊过什么"是靠把「最近记忆」整段注入提示词：
        # 实测占注入体积 **98%**（120 轮 / 10055 token），而且**与 `messages`
        # 里的历史重复** —— 那正是用户报的「你说了两遍」的成因。
        #
        # 现在改成：从 `raw_log` 读最近对话**灌进 `self.history`**，
        # 之后它自然随 `messages` 发给模型。一句话只有一个来源。
        #
        # 放在 `Conversation.__init__` 里而不是 `Gateway.__init__`：
        # `self.history` 是本类的属性，放这儿"历史"这件事完全自洽。
        #
        # 用 try 包住：**灌不回来也必须能启动**（从空历史开始就是了）。
        try:
            self.bootstrap_history()
        except Exception:  # noqa: BLE001
            log.exception("灌回对话历史失败（从空历史开始）")

    # ------------------------------------------------------ 人格主观层（2026-10-17）

    @property
    def emotion(self) -> EmotionState:
        """
        情绪实例。**保留这个名字是为了不破坏现有调用点**（约 15 处）。

        现在真正的持有者是 `self.subjectivity.emotion` —— 情绪被并进主观层，
        但外部（brain 工厂、`pet.emotion` 事件、设置页）仍然按原来的方式读它。
        """
        return self.subjectivity.emotion

    def sync_vitality(self) -> None:
        """
        把 `activity.overall` 同步进主观层。

        ⚠️ 要在**配置热更新时也调一次** —— 否则用户拖完活泼度滑杆，
        主观层还拿着旧值，注意力倾向就不同步（表现是"调了没反应"）。
        """
        try:
            self.subjectivity.set_vitality(float(self.config.activity.get("overall", 0.6)))
        except (TypeError, ValueError):
            self.subjectivity.set_vitality(0.6)

    def attention_payload(self) -> dict[str, Any]:
        """给前端 / 日志的注意力快照（数值放这儿，提示词里不放）。"""
        return self.subjectivity.as_dict()

    # ------------------------------------------------------------------ 订阅

    def attach(self) -> None:
        self.bus.subscribe("user.text", self._on_user_text)
        self.bus.subscribe("user.voice", self._on_user_voice)
        self.bus.subscribe("user.interrupt", self._on_interrupt)
        """
        ⚠️ **这条必须挂在这里（`Conversation`），不能挂在 `Gateway` 上**
        （2026-10-12 踩过，`tools/test-web-toggle.py` 抓到的）。

        缓存是 `self._tools`（**本类**属性）。挂到 Gateway 上的话，
        清的是 Gateway 自己那个**从来没被读过**的字段，而本类这个纹丝不动 ——
        于是「菜单里打开联网」变成**假开关**：勾打上了，模型却看不到 `web_search`。

        表现特别隐蔽：`apply_patch` 成功、`_web_enabled()` 返回 True、
        两个类的方法名一模一样，**只有工具清单里没有那个工具**。

        教训：**订阅要挂在"拥有那份状态"的类上**。`self.bus.subscribe(...)`
        写在哪个类里看起来都一样，但 `self` 不一样。
        """
        self.bus.subscribe("config.updated", self._on_config_tools)

    async def _on_config_tools(self, event: Event) -> None:
        """工具开关变了就重建工具清单（2026-10-12）。

        ## 🔴 为什么必须监听（否则是假开关）

        `self._tools` 是**懒加载缓存**（见 `_tool_list`）。用户在菜单里打开
        「联网搜索」之后，如果不清缓存：

          · 界面上那个勾已经打上了
          · 但模型**依然看不到 `web_search` 工具**
          · 于是用户说「帮我查一下」，宠物只能凭训练数据瞎答

        —— 这正是这个项目里反复出现的「假开关」：**界面说了，实际没生效**。

        ## ⚠️ 这个方法必须挂在**拥有那份缓存**的类上

        第一版我把它加到了 `Gateway` 上（`self.bus.subscribe('config.updated', …)`
        写在哪个类里看起来都一样，但 `self` 不一样）。
        而缓存是 **`Conversation._tools`** —— 于是 Gateway 清的是它自己那个
        从来没被读过的字段，真正要清的那个纹丝不动。

        表现特别隐蔽：`apply_patch` 成功、`_web_enabled()` 返回 True、
        两个类的方法名一模一样，**只有工具清单里没有那个工具**。

        `tools/test-web-toggle.py` 抓到了这个 —— 它断言的是
        **「打开之后工具清单里有没有」**，而不是「配置改了没有」。
        **断言要打在最终效果上。**

        ## ⚠️ 为什么每次 config.updated 都清，而不是比对

        `config.update` 在改**任何**设置时都会发（活泼度、免打扰、思考模式…），
        每次清缓存意味着下次生成时会重建一次工具清单 —— 而重建只是
        `mkdir` + 建四个数据类对象，**微秒级**，不值得为它做指纹比对。

        （对比：语音引擎重建要 4 秒，所以那边**必须**比对指纹。
        这里的取舍完全相反，因为代价差了六个数量级。）
        """
        # 模块配置可能变了（主进程写盘后会推 config.update）：
        # 重读注册表，让"关掉"从这一秒开始生效。**只重读，不主动副作用** ——
        # 副作用由 modules.set 的 _apply_module_change 负责，避免重复起任务。
        try:
            from .modules import ModuleRegistry

            self.modules = ModuleRegistry(self.config)
        except Exception:  # noqa: BLE001
            log.warning("重读模块配置失败（沿用旧值）", exc_info=True)
        self._tools = None
        # 🔴 顺手把 MCP 搜索开关同步给 tools 模块（2026-10-15）。
        # ⚠️ 和上面清缓存是**两件事**：清缓存决定"下次要不要重建工具清单"，
        #    而这个决定"重建时/调用时用哪个搜索后端"。
        #    漏掉它的症状是"菜单里关了 MCP 搜索，实际还在走 MCP"——又一个假开关。
        self._sync_mcp_search_flag()
        log.info(
            "工具清单已失效（配置更新）—— 下次生成时重建。当前：联网=%s 工具总开关=%s MCP搜索=%s",
            "开" if self._web_enabled() else "关",
            "开" if self._tools_enabled() else "关",
            "开" if self._mcp_search_enabled() else "关",
        )

    async def _on_user_text(self, event: Event) -> None:
        text = str((event.payload or {}).get("text") or "").strip()
        if not text:
            return
        await self.ask(text)

    async def _on_user_voice(self, event: Event) -> None:
        """
        语音识别结果走**与打字完全相同**的路径。

        这是刻意的：对话逻辑不该关心输入是键盘还是麦克风。语音只多带一个
        "来源"标记，用于前端区分显示（并让用户知道刚才被听成了什么）。
        """
        payload = event.payload or {}
        text = str(payload.get("text") or "").strip()
        if not text:
            return
        log.info("以语音输入发起对话：%s", text)
        await self.ask(text)

    async def _on_interrupt(self, event: Event) -> None:
        await self.interrupt()

    # ------------------------------------------------------------------ 自主发言

    def _remember_assistant(self, text: str) -> None:
        """记 assistant 这一句；如果挂了"待写图片"，一起写进原文记忆（BLOB）。

        ## 为什么需要这层间接

        截图回复实际可能走两条路（`speak_autonomously` 或底层 `ask`），
        两条路各自记 assistant 回合。图片用"挂一次、取走就清"的方式，
        不管走哪条路都能落进原文记忆，也不会串到下一条普通回复上。
        """
        if self.memory is None:
            return
        image = self._pending_image
        mime = self._pending_image_mime
        self._pending_image = None
        self._pending_image_mime = None
        if image:
            self.memory.remember_turn(
                self.session_id, "assistant", text, image=image, image_mime=mime
            )
        else:
            self.memory.remember_turn(self.session_id, "assistant", text)

    async def speak_autonomously(
        self,
        trigger: dict[str, Any] | None = None,
        *,
        image: bytes | None = None,
        image_mime: str | None = None,
    ) -> None:
        """
        无用户输入时的主动发言（P5 调度器与手动"戳一下"都走这里）。

        与正常回复的区别只有三点，全部与"像不像主动开口"有关：
          · 不写入用户消息（确实没有用户消息），历史里只多一条 assistant；
          · system prompt 换成"主动搭话"的引导，而不是"回答用户"；
          · 更短、温度略高（短句更需要一点变化）。
        """
        if self.current is not None and not self.current.done():
            # 正在说话时不要叠加自主发言：那会变成自己跟自己抢话
            log.debug("正在生成中，跳过本次自主发言")
            return
        # ⚠️ 图必须**在这里就挂上**，不能等生成完再挂。
        #
        # 实测踩坑（2026-10-18）：assistant 那句是在生成流程内部就记进原文记忆的，
        # 等 `_generate` 返回再设 `_pending_image` 已经晚了一步 —— 库里 has_image 全是 0。
        if image:
            self._pending_image = bytes(image)
            self._pending_image_mime = image_mime or "image/jpeg"
        self.current = asyncio.get_running_loop().create_task(
            self._generate(user_text=None, trigger=trigger)
        )

    async def speak_verbatim(self, text: str, reason: str = "verbatim") -> None:
        """
        **不过模型**，原样把这段话说出去（2026-10-18，照抄句子用）。

        为什么要单独一条路：用户明确说「直接按原本照抄输出，其他什么也不加」。
        走 `_generate` 的话，模型一定会加上语气词、联想、感叹 —— 那就不是照抄了。
        这里只做和 `_generate` 收尾时同样的几件事（少一件都会静默失效）：

            · `pet.say.start` / `pet.say.delta` / `pet.say.end`（前端靠 end 收尾）
            · 写进 history（下一轮它得记得自己念过什么）
            · `conversation.finished`（调度器据此重置冷却）
        """
        text = text.strip()
        if not text:
            return
        say_id = self.new_say_id()
        await self.bus.publish(
            "pet.say.start",
            msg("pet.say.start", id=say_id, emotion=self.emotion.dominant, priority="normal"),
        )
        await self.bus.publish("pet.say.delta", msg("pet.say.delta", id=say_id, text=text))
        self.history.append(Turn(role="assistant", text=text))
        self._trim_history()
        self._trim_history_by_budget()
        await self.bus.publish("pet.say.end", msg("pet.say.end", id=say_id, text=text, reason=reason))
        # 截图回复的那句，原图一起进原文记忆（用户 2026-10-18：
        # 「这样就知道截图回复的是什么图片了」）。
        # 图先挂到会话上，由 `_remember_assistant` 取走 —— 这样无论最终是
        # 这条路径还是底层 ask 记的账，图都不会丢。
        # 图已在开头挂上（见 speak_autonomously），这里直接记账
        self._remember_assistant(text)
        await self.bus.publish(
            "conversation.finished",
            {"idle": True, "reason": reason, "length": len(text)},
        )

    # ------------------------------------------------------------------ 主流程

    def new_say_id(self) -> str:
        self._say_seq += 1
        return f"say{self._say_seq}-{int(time.time() * 1000):x}"

    async def ask(self, user_text: str) -> None:
        if not user_text.strip():
            return
        # 调试钩子：对话条里发「1」-> 回一条**固定的几百字长文**（见 `LONG_SAMPLE`）。
        # 刻意放在最前面：它不该触发大脑、也不该被"上一轮还没说完"打断。
        if _is_long_sample_trigger(user_text):
            await self._emit_long_sample()
            return
        # 新输入到来：先掐掉上一句未说完的话（等同于用户插话）
        if self.current is not None and not self.current.done():
            self._interrupt.set()
            try:
                await asyncio.wait_for(asyncio.shield(self.current), timeout=1.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                log.debug("上一轮生成未在 1.5s 内收尾，继续处理新输入")
            except Exception:  # noqa: BLE001
                log.exception("等待上一轮生成结束时异常")
        self.current = asyncio.get_running_loop().create_task(self._generate(user_text))

    def history_token_budget(self) -> int:
        """
        发给模型的历史预算（**按 token 算**，中文 1 字 ≈ 1 token）。

        用户要求（2026-10-08）：把「最近记忆 · 时间窗口 N 天」换成
        **「上下文记忆 · token 预算」** —— 理由是好懂：

          · "30 天"与"模型能看多少"没有直接关系（30 天内可能只有 2 轮，
            也可能有 3000 轮）
          · token 预算**直接对应模型的上下文窗口**（DeepSeek 是 128K），
            用户能自己判断"我给它的够不够"

        `0` 或负数表示**不限**（会把 `HISTORY_LIMIT` 当唯一约束）。
        """
        brain = self.config.providers.get("brain", {}) or {}
        raw = brain.get("history_token_budget")
        if raw is None:
            # 没配就按旧的 `history_turns` 折算，保持行为连续
            turns = int(brain.get("history_turns") or 12)
            return turns * 300  # 一轮约为 user+assistant，中文各约 150 字
        try:
            return int(raw)
        except (TypeError, ValueError):
            log.warning("history_token_budget 不是数字：%r，按不限处理", raw)
            return 0

    def _trim_history_by_budget(self) -> int:
        """
        按 token 预算裁掉**最旧**的轮次，返回丢掉几条。

        ⚠️ 为什么不用 `history_turns` 那种"轮数"限制：一轮可能是
        "嗯"（1 字）也可能是 2000 字的屏幕分析。按轮数裁，短对话浪费预算、
        长对话直接爆窗口。按字数裁才和模型窗口对得上。
        """
        budget = self.history_token_budget()
        if budget <= 0 or not self.history:
            return 0
        total = sum(len(t.text) for t in self.history)
        if total <= budget:
            return 0
        dropped = 0
        while self.history and total > budget:
            total -= len(self.history[0].text)
            self.history.pop(0)
            dropped += 1
        if dropped:
            log.debug(
                "按 token 预算裁剪历史：丢掉最旧 %d 条（预算 %d，剩余约 %d 字）",
                dropped,
                budget,
                total,
            )
        return dropped

    def _trim_history(self) -> None:
        """
        按**轮数**上限裁剪内存历史（粗筛，防止无限增长）。

        ## ⚠️ 真正决定"发给模型多少"的是 token 预算，不是这个

        见 `_trim_history_by_budget()`。`HISTORY_LIMIT` 只是个兜底 ——
        比如某次对话特别长而预算又配得很大时，别让内存无限涨。
        """
        if len(self.history) > HISTORY_LIMIT:
            del self.history[: len(self.history) - HISTORY_LIMIT]

    def _history_snapshot(self) -> list[Turn]:
        """
        取要发给模型的对话历史。复制一份，避免生成期间历史被改动。

        ## ⚠️ 这里**不再按轮数截断**（2026-10-08 改）

        原来这里是 `self.history[-history_turns:]`（默认只发最近 12 轮）——
        那是"轮数"限制，和模型的窗口没有对应关系：一轮可能是"嗯"（1 字），
        也可能是 2000 字的屏幕分析。

        现在裁剪统一在 `_trim_history_by_budget()` 里**按 token 预算**做，
        而且是在**追加时**做（历史本身就被裁短了），所以这里直接全发。
        """
        return list(self.history)

    def bootstrap_history(self) -> int:
        """
        启动时从 `raw_log` 灌回最近的对话历史 —— **重启后"接着上次说"**。

        ## 🔴 为什么需要它（用户 2026-10-08 的决定）

        用户问：「多次重启宠物，它算一次上下文衔接吗」。查证后的事实是：

          · `self.history` 在 `__init__` 里是**空列表**，没有任何从数据库
            恢复的代码 -> **重启后逐字对话历史全丢**
          · 之前的衔接靠 `inject_hint()` 把「最近记忆」**整段注入提示词**
            —— 而那占了注入体积的 **98%**（实测 120 轮 / 10055 token），
            并且**与 messages 里的历史重复**，正是「你说了两遍」的成因

        用户的要求：

        > 「如果能不靠本地的最近记忆来让重启的 api 得到之前的上下文，
        >   那也不必保留最近记忆了」

        **能做到**，所以现在：删掉提示词注入，改成**灌进 `messages`**。
        一句话只有一个来源，重复问题从根上消失。

        ## ⚠️ 时区/顺序

        `recent_dialogue()` 已保证时间正序、且丢掉开头孤立的 assistant
        （否则模型会看到"助手凭空开口"）。
        """
        if not self.memory:
            return 0
        brain = self.config.providers.get("brain", {}) or {}
        turns = int(brain.get("history_turns") or 12)
        budget = self.history_token_budget()
        # 取多少：按预算折算（中文 1 字 ≈ 1 token，一轮约 2 条消息），
        # 至少覆盖 `history_turns`，最多不超过 2000 条（免得多读无用数据）
        limit = max(turns * 2, min(2000, budget * 2 if budget > 0 else turns * 2))
        # ⚠️ 按**天数窗口**取（用户 2026-10-08 定稿：「设置多少天的记忆内容，
        # 重启宠物时灌注最近的设置记忆内容即可」）。
        #
        # 天数决定**取多少**，预算决定**最多发多少**（安全上限）——
        # 用户已确认这个分工：「天数为主，预算当安全上限」。
        # 天数取的量超预算时，`max_total_chars=budget` 会让 `recent_dialogue`
        # 只保留最新的那部分（它从最新往回攒）。
        days = int((self.config.memory or {}).get("history_days") or 0)
        try:
            rows = self.memory.recent_dialogue(
                limit=limit,
                max_age_s=(days * 86400) if days > 0 else None,
                max_total_chars=budget if budget > 0 else 0,
                exclude_texts=self._bootstrap_skip_texts(),
            )
        except Exception:  # noqa: BLE001 —— 灌不回来也要能启动
            log.exception("灌回历史失败（从空历史开始）")
            return 0
        if not rows:
            return 0
        # 与已有历史合并（正常情况下启动时是空的）
        self.history = [Turn(role=r["role"], text=r["text"]) for r in rows] + self.history
        self._trim_history()
        self._trim_history_by_budget()
        log.info(
            "已从记忆库灌回 %d 条对话历史（约 %d 字）—— 重启后接着上次说",
            len(self.history),
            sum(len(t.text) for t in self.history),
        )
        return len(self.history)

    def _bootstrap_skip_texts(self) -> list[str]:
        """
        灌回历史时要**跳过**的文本。

        ## ⚠️ 为什么需要它（2026-10-08）

        排查「文本发了两次」那个 bug 的那段时间，宠物每轮都在回
        「你又说两遍啦」—— 那 40 多条**污染对话已经写进 `raw_log`** 了。

        而 `raw_log` 的约定是**永不删除**（用户明确要求"逐字原文永久保留"），
        所以不能删它们。但把它们当正常历史灌回去的话，模型会**继续**
        这个话题 —— 用户会发现宠物莫名其妙还在说"你又说两遍"。

        所以这里按**本机配置**给出要跳过的文本（默认空，见 persona.yaml 的
        `memory.bootstrap_skip`）。这是"不删数据、但不让它污染当下"的折中。
        """
        section = self.config.memory or {}
        raw = section.get("bootstrap_skip")
        if isinstance(raw, str):
            return [raw]
        if isinstance(raw, (list, tuple)):
            return [str(x) for x in raw]
        return []


    async def interrupt(self) -> None:
        if self.current is not None and not self.current.done():
            self._interrupt.set()
            log.info("用户打断当前生成")

    async def _emit_long_sample(self) -> None:
        """把 `LONG_SAMPLE` 当作一次普通发言流式发出去（调试图标用）。

        走的还是 `pet.say.*` 那条管线 —— 前端不需要为它写任何特殊逻辑，
        气泡看到的与真实回复**完全一样**（这也是为什么这个调试图标能真实反映排版）。
        """

        say_id = self.new_say_id()
        await self.bus.publish("pet.thinking", msg("pet.thinking", id=say_id))
        await self.bus.publish(
            "pet.say.start",
            msg("pet.say.start", id=say_id, emotion=self.emotion.dominant, priority="normal"),
        )
        # 分片发，模拟流式（也让前端的增量追加路径一并被测到）
        chunk = 12
        for i in range(0, len(LONG_SAMPLE), chunk):
            await self.bus.publish(
                "pet.say.delta", msg("pet.say.delta", id=say_id, text=LONG_SAMPLE[i : i + chunk])
            )
            await asyncio.sleep(0)
        await self.bus.publish(
            "pet.say.end",
            msg("pet.say.end", id=say_id, text=LONG_SAMPLE, reason="done"),
        )
        log.info("调试：已发送长文样本（%d 字）", len(LONG_SAMPLE))

    async def _generate(
        self, user_text: Optional[str], trigger: Optional[dict[str, Any]] = None
    ) -> None:
        """
        一轮生成。`user_text` 为 None 表示自主发言（没有用户消息）。

        自主发言与回复共用同一条流式管线 —— 前端不需要为"主动说话"写第二套
        渲染逻辑，这也是当初把"说话"抽成 pet.say.* 事件的价值。
        """
        idle = user_text is None
        self._interrupt.clear()
        # 「截图对话」的结构性证据：提示词到底有没有进到 **user 那一轮**。
        # 只记长度不记内容 —— 画面描述本身已经由 screen_chat 记过了。
        _stage = str((trigger or {}).get("stage_direction") or "")
        if _stage:
            log.info("截图对话：user 轮舞台提示 %d 字", len(_stage))

        if idle:
            # 自主发言不消耗"被打招呼"的精力，但同样计入亲近度。
            # 走主观层：它会顺便推进注意力漂移（每 3 分钟一次）。
            self.subjectivity.tick()
        else:
            assert user_text is not None
            # 用户说话 -> 主观层把注意力拉向用户，并顺手更新情绪
            self.subjectivity.on_user_message(user_text)

            """
            ## 🔴 历史快照必须在 append **之前**取（2026-10-08 修，真 bug）

            原来快照是在 `append` **之后**取的，而请求长这样：

                messages = [system] + history + [user_text]

            于是本轮这句**同时在两个地方**：

                req.history = [..., Turn("user", "我最喜欢吃番茄炒蛋")]   <- append 进去的
                req.user_text = "我最喜欢吃番茄炒蛋"                      <- 单独字段

            合起来就是**紧挨着的两条一模一样**：

                [1] user  我最喜欢吃番茄炒蛋
                [2] user  我最喜欢吃番茄炒蛋

            模型看到两遍，于是**每一轮**都回「你说了两遍 / 又是两遍」——
            用户以为是网络重复发送（「好像是文本发了两次」），其实是**提示词里重复**。

            ## ⚠️ 这个 bug 为什么这么难找

            代码里**注释是对的**：

                # 历史快照必须在本轮用户消息**之前**取：user_text 是单独字段，
                # 若把刚追加的这条也算进 history，同一句话会在请求里出现两次。

            但**代码在 append 之后**。注释精确描述了正确做法，代码没照做 ——
            读代码时一眼扫过去会以为它是对的（我就是这么被骗过两轮的）。

            而且它**不报错、不崩**，只是让模型以为用户在复读。

            ## 怎么最终定位的

            靠**逐层把真实数据打出来**（临时诊断日志），而不是读代码：

                self.history=1 条 [('user', '身份测试句QQQ')]
                req.history =1 条 [('user', '身份测试句QQQ')] | user_text='身份测试句QQQ'
                messages 共 3 条：
                  [0] system
                  [1] user 身份测试句QQQ
                  [2] user 身份测试句QQQ      <- 1 + 1 = 2 条 user，一模一样

            再离线单元测试 `_body` 确认它是**忠实**的（1 条历史 + 1 个 user_text
            = 2 条 user 消息，符合设计），于是问题必然在**传进去的 history 里**。

            ⚠️ 顺便：`context_hint` 那套 id 排除（`exclude_ids`）解决的是
            **system 记忆段**里的重复，和这里是**两处不同的重复**，都要修。
            修完 system 记忆段仍会有这一句（那是"最近记忆"的设计），
            但 `messages` 里不再有紧挨着的两条 —— 后者才是模型说"两遍"的原因。
            """
            history = self._history_snapshot()

            self.history.append(Turn(role="user", text=user_text))
            self._trim_history()
            # ⚠️ token 预算也在这里裁：**追加时**就裁，历史本身保持短，
            # 而不是等到发送时才截（那样每轮都要重算一遍）
            self._trim_history_by_budget()
            # 先把这一句写进记忆库（**最近记忆 + 原文记忆两处**）：
            # 这样本轮检索能把刚说的话算进去，而且原文在任何情况下都不丢。
            # 提炼事实是另一件事，走异步、不阻塞回复。
            #
            # ⚠️ **记下返回的 id**：拼提示词时要把它从**记忆段**里排掉
            # （记忆段整段注入 `recent_turn`，不排的话它也会带上这一句）。
            current_turn_id = 0
            if self.memory is not None:
                current_turn_id = self.memory.remember_turn(self.session_id, "user", user_text)

        # ⚠️ `current_turn_id` 必须在 `if idle:` **外面**有个兜底值（2026-10-09 修，真 bug）。
        #
        # 它的赋值只在上面 `if idle:` 分支里（那条路要把用户这句写进记忆库并拿回 id），
        # 而它在**下面无条件被用**（`_compose_context_hint(..., current_turn_id)`）。
        # 于是**回复用户**这条路 100% 抛：
        #
        #     UnboundLocalError: cannot access local variable 'current_turn_id'
        #                         where it is not associated with a value
        #
        # 被 `asyncio` 吞成 "Task exception was never retrieved" ——
        # 前端只看到"三个点"然后什么都没有，**连 TTS 都不会被触发**
        # （没有 `pet.say.end`）。自主发言（`if idle:`）反而正常，所以很难注意到。
        #
        # `history` 有同样的来源，但它在下面 `if idle:` 那条路径上补过一次；
        # `current_turn_id` 当时漏了。教训：**在分支里定义、在分支外用**的变量，
        # 一定要在分支外给兜底值，否则只有一条路径能跑。
        current_turn_id = 0
        say_id = self.new_say_id()
        await self.bus.publish("pet.thinking", msg("pet.thinking", id=say_id))
        # `userInitiated` = 这一句是不是用户主动要的。
        #
        # 前端用它破"全屏（紧凑）态不弹气泡"那条规则：那时候用户一般不在看桌面，
        # 所以自主开口只出声；但用户自己按了「截图对话」、或打字/说话之后的回复，
        # 必须给字 —— 否则表现就是"按了截图对话，只有声音没有字"（2026-10-19 反馈）。
        #
        # `idle=False` 本来就是"用户说的"（打字 / 语音）；
        # 截图对话虽然 idle=True，但它的 trigger 带着 user_initiated=True。
        user_initiated = (not idle) or bool((trigger or {}).get("user_initiated"))
        await self.bus.publish(
            "pet.say.start",
            msg(
                "pet.say.start",
                id=say_id,
                emotion=self.emotion.dominant,
                priority="low" if idle else "normal",
                userInitiated=user_initiated,
            ),
        )

        # ⚠️ `idle`（自主发言）那条路径没有走到上面的快照 —— 在那里补一次。
        # 为什么现在才补：用户消息那条路径**必须**在 append 之前取（见上面的长说明），
        # 而自主发言不 append 用户消息，取在哪儿都一样。
        if idle:
            history = self._history_snapshot()

        brain_cfg = self.config.providers.get("brain", {})
        base_temp = float(brain_cfg.get("temperature") or 0.85)
        req = LLMRequest(
            system=self.build_system_prompt(idle=idle),
            history=history,
            user_text=user_text,
            # 自主发言更短、温度略高：短句要一点变化，长回复才需要稳
            temperature=min(1.2, base_temp + 0.1) if idle else base_temp,
            max_tokens=(
                int(self.config.autonomy.get("max_idle_tokens", 220))
                if idle
                else int(brain_cfg.get("max_tokens") or 400)
            ),
            timeout_s=float(brain_cfg.get("timeout_s") or 30.0),
            context_hint=self._compose_context_hint(user_text, trigger, current_turn_id),
            # 「截图对话」把画面描述放在**用户那一轮**（system 里那段会被记忆压过去，
            # 见 LLMRequest.stage_direction 的说明）。其它场合为空 -> provider 用内置提示。
            stage_direction=str((trigger or {}).get("stage_direction") or ""),
            thinking=self._thinking_flag(brain_cfg),
            tools=self._tool_schemas(),
        )

        collected: list[str] = []
        reason = "done"
        error_text: Optional[str] = None

        """
        ## 🔁 agent loop（2026-10-12）

        模型可以**要求调用工具**，我们执行完把结果回填，再让它生成一次。
        」就这么简单 —— 但有几个必须钉住的点：

        ### ⚠️ 为什么必须有轮数上限

        模型可能一直要调用（参数写错 -> 结果看不懂 -> 再调一次）。
        没有上限就是**无限循环 + 无限烧钱**，而用户看到的是"宠物卡住了"。
        3 轮够了：实测一次搜索 + 一次读取就能回答绝大多数问题。

        ### ⚠️ 打断要**立刻**生效

        每一轮开始前检查 `self._interrupt`。用户在它查资料时按了 Esc，
        不该还要等它把剩下两轮跑完 —— 那是"打断了但没完全打断"。

        ### ⚠️ 已经说出口的话**不能撤回**

        `collected` 跨轮累积。第一轮它可能已经说了"我查一下"（那句话已经
        流到前端、可能已经进了 TTS），第二轮的结果接在后面。
        把 `collected` 清空重来会让用户看到"话说到一半突然重说"。
        """
        """
        `truncated_retried` 保证**每轮最多重试一次** —— 否则模型一直想到截断
        就会一直重试，等于把"无限循环"从工具调用搬到了重试上。
        """
        truncated_retried = False
        for round_index in range(MAX_TOOL_ROUNDS + 1):
            if self._interrupt.is_set():
                reason = "interrupted"
                break
            tool_calls, reasoning, error_text, reason = await self._run_tool_round(
                req, say_id, collected
            )

            """
            ## 🔴 被 max_tokens 截断到空 -> 加大预算重试一次（2026-10-12）

            用户报「我说了话，但是它没有回答」。实测的原始数据：

                finish_reason='length'
                completion_tokens=400
                completion_tokens_details={'reasoning_tokens': 400}   <- 全花在推理上
                正文 delta 共 0 字

            **思考模式下推理和正文共享 `max_tokens` 预算。** 带上搜索结果后
            模型想得更多，400 个 token 全被推理吃掉，正文一个不剩 ——
            而流是正常结束的（`[DONE]` 照来），所以**不报错、只是没回答**。

            ## 为什么是"重试"而不是"一开始就把预算调大"

            用户给宠物设的 `max_tokens=400` 是**给正文**的意图（回答要短，
            口语化）。但 API 把 reasoning 也算进同一个预算，于是配置的语义
            与它的实际作用对不上。

            直接把预算乘 3 会**改变所有回复的长度**（用户要的是短回复）；
            而"只在真的被截断到空时才加大"既修了 bug，又不动正常路径的行为。
            代价是被截断时多花一次调用（约 ¥0.003）。
            """
            if reason == "truncated" and not truncated_retried:
                truncated_retried = True
                bumped = max(int(req.max_tokens) * 4, 1600)
                log.info(
                    "思考模式吃光了预算（%d），本轮正文为空 —— 用 %d 重试一次",
                    req.max_tokens,
                    bumped,
                )
                req.max_tokens = bumped
                tool_calls, reasoning, error_text, reason = await self._run_tool_round(
                    req, say_id, collected
                )
                if reason == "done":
                    log.info("加大预算后拿到正文")
                    # 这一轮救回来了 —— 下一个工具轮如果又被截断，还该再救一次
                    truncated_retried = False
                else:
                    # ⚠️ 只有**重试也没救回来**才升级成 WARNING。
                    #    原来 `_run_tool_round` 里那条 WARNING 在正常路径上也会打，
                    #    于是"修好了"和"没修好"在日志里长得一样 ——
                    #    排查时会以为问题还在。
                    log.warning(
                        "加大预算到 %d 之后**仍然是空回复**（reason=%s）—— 用户会看到没回答",
                        bumped,
                        reason,
                    )

            if reason != "done" or not tool_calls:
                break
            if round_index >= MAX_TOOL_ROUNDS:
                """
                ## 🔴 轮数用尽 -> **拿掉工具，逼它回答**（2026-10-12）

                原来这里只是 `break` + 一条 WARNING，结果是**用户什么都没看到**：

                    19:40:26  WARNING 工具调用达到上限 3 轮，放弃继续
                    （然后就没有了）

                实测的失败模式是：模型**陷在"搜索模式"里出不来** ——
                用户问的是一个它不确定的东西（「新出的银松镇你知道吗」），
                于是它一轮又一轮地换词搜，宁可继续搜也不肯用已有的东西回答。

                而且**加大预算也救不了**：实测某一轮 `completion_tokens=1333`
                （预算 1600 绰绰有余），它依然选择"再搜一次"。
                所以这不是预算问题，是**决策问题**。

                ## 怎么逼

                两件事一起做（缺一不可）：

                  ① `req.tools = None` —— **它无从再搜**。
                     工具清单不在请求里，服务商就不会返回 `tool_calls`。
                     （只在提示词里写"别再搜了"是不够的 —— 那只是建议。）
                  ② 把已查到的资料**用 system 消息**再喂一次，并说明"已经查完了"。
                     `_body` 那边会因为 `tools` 为空而自动改用这种写法。

                代价是可能多花一次调用；收益是**用户一定拿得到回答**，
                哪怕回答是「我查了半天没找到这个游戏，是不是名字记错了？」
                —— 那句话本身也是有用的回答，比沉默好得多。
                """
                log.warning("工具调用达到上限 %d 轮，改为强制回答", MAX_TOOL_ROUNDS)
                await self._force_answer(req, say_id, collected)
                break

            # 执行（并发 —— 模型常常一次发两个搜索，串行会让等待翻倍）
            await self._execute_tool_calls(tool_calls, req, reasoning)
            log.info("工具轮 %d 完成，继续生成", round_index + 1)

        """
        ⚠️ 收尾逻辑**无条件执行**（原来这里是一圈 `try/finally`，
        重构后循环里的每一轮都自带异常处理，所以外面不需要再包一层）。

        但下面这几件事**必须做，一件都不能少** —— 它们原来就在 `finally` 里，
        少任何一件的表现都是"宠物说完话之后某个功能静默失效"：

          · 把回复写进 history（否则它下一轮不记得自己说过什么）
          · `pet.say.end`（前端靠它收尾；少了气泡会一直转圈）
          · `pet.emotion`（表情不走）
          · 记忆写入 + 后台提炼（少了就"聊过就忘"）
          · `conversation.finished`（少了调度器的冷却不重置，它会立刻再说一句）
        """
        final_text = "".join(collected)
        # 只给**发出去的文本**加这一段，历史仍然记 `final_text`（见函数说明）。
        note = screen_seen_note(trigger, self.config.screenshot)
        say_text = f"{final_text}\n\n{note}" if (final_text and note) else final_text
        if final_text:
            self.history.append(Turn(role="assistant", text=final_text))
            self._trim_history()
            # 按 token 预算裁掉最旧的（见 `_trim_history_by_budget`）
            self._trim_history_by_budget()
            self.subjectivity.on_reply_done(len(final_text))
        await self.bus.publish(
            "pet.say.end",
            msg("pet.say.end", id=say_id, text=say_text, reason=reason),
        )
        if error_text:
            await self.bus.publish(
                "system.error",
                msg("system.error", level="error", message=error_text, where="brain"),
            )
        await self.bus.publish("pet.emotion", msg("pet.emotion", state=self.emotion.as_dict(), attention=self.attention_payload()))
        # 一轮结束后异步提炼长期事实。刻意放在事件之后、且不 await：
        # 用户不该为"宠物记笔记"多等一秒，提炼失败也不影响这一轮对话。
        if self.memory is not None:
            # 助手回复也要进"最近记忆 + 原文记忆"——否则重启后只记得用户说过什么，
            # 不记得自己回过什么，对话会显得断裂。
            if final_text:
                self._remember_assistant(final_text)
            if not idle and user_text:
                asyncio.get_running_loop().create_task(
                    self._extract_in_background(user_text, final_text)
                )
        # 通知外部"这一轮结束了"：调度器据此重置冷却（说完话要歇一会儿），
        # 也方便将来统计发言频率。这里发事件而不是直接调调度器，是为了
        # 保持 Conversation 不认识 Autonomy 这个模块。
        await self.bus.publish(
            "conversation.finished",
            {"idle": idle, "reason": reason, "length": len(final_text)},
        )
        # ── 「一段交流」的节拍（2026-10-17）──
        #
        # 用户要求「一大段关联的交流结束后 AI 可自发提炼和强化」。
        # 这里只做**记账**，真正的收尾放到后台任务里（不 await）：
        # 提炼印象要调模型，用户不该为它多等。
        if not idle:
            self._note_segment_turn(user_text or "", final_text or "")

    # ------------------------------------------------------------------ 工具

    def _note_segment_turn(self, user_text: str, reply_text: str) -> None:
        """
        记一轮进"当前这一段"，并在段结束时安排收尾（2026-10-17）。

        ## 为什么"段结束"的判定放在这里

        每轮结束都会经过这里，而"距上次互动多久"和"段内几轮"这两个量
        正好在这时是新鲜的。放到定时器里判也行，但会多一层状态同步。

        ## 为什么收尾是后台任务

        收尾要：调模型提炼印象 -> 写库 -> 强化记忆。可能几百毫秒到几秒。
        用户刚说完话，不该等这个。
        """
        now = time.time()
        hit_ids = list(getattr(self.memory, "last_hit_ids", []) or []) if self.memory else []
        self.segment.add_turn(
            now, user_text=user_text, reply_text=reply_text, hit_ids=hit_ids
        )
        should, why = self.segment.should_close(now)
        # ⚠️ DEBUG 级记账日志（2026-10-17 加的）：**必须能看出来它在数**。
        #
        # 没有这行的话，"段永远不结束"在日志里是**完全静默**的 ——
        # 实测就踩了：发了 8 条消息，日志里一条"段"相关都没有，
        # 根本分不清是"没调用"还是"调用了但没到 8 轮"。
        log.debug(
            "段记账：%d/%d 轮（间隔 %.0fs / 门槛 %.0fs）-> %s",
            self.segment.turns,
            self.segment.max_turns,
            now - self.segment.last_at,
            self.segment.gap_s,
            why,
        )
        if not should:
            return
        if not self.segment.worth_consolidating():
            # 段太短（只有 1 轮）：不提炼，但把记账清掉，免得下次被旧内容干扰
            self.segment.take()
            log.debug("段太短，不提炼（原因=%s）", why)
            return
        snapshot = self.segment.take()
        log.info(
            "一段交流结束（%s）：%d 轮、%d 条记忆命中 -> 提炼印象 + 强化",
            why,
            snapshot["turns"],
            len(snapshot["hit_ids"]),
        )
        asyncio.get_running_loop().create_task(
            self._consolidate_segment(snapshot, why)
        )

    async def _consolidate_segment(self, snapshot: dict[str, Any], why: str) -> None:
        """
        段收尾：**强化**记忆 + **提炼**印象。

        ## 两件事分开做、各自兜底

        ```
        强化  纯本地，不花钱，先做（就算提炼失败，强化也该落地）
        提炼  要调模型，可能失败（失败就跳过，不影响强化）
        ```

        ## 强化加多少

        每条被命中过的记忆 `+0.05`，段内被多次命中也只加一次 ——
        "这段里被想起来过"本身就是信号，次数是细节。
        ⚠️ 一次对话不该把一条记忆顶到最高（与 `bump_importance` 的取舍一致）。
        """
        if self.memory is None:
            return
        # ① 强化
        try:
            ids = [int(i) for i in (snapshot.get("hit_ids") or [])]
            if ids:
                changed = self.memory.db.bump_importance(ids, delta=0.05)
                if changed:
                    log.info("强化了 %d 条记忆（importance +0.05）", changed)
        except Exception:  # noqa: BLE001 —— 强化失败不该影响提炼
            log.debug("强化记忆失败（已忽略）", exc_info=True)

        # ② 提炼印象
        try:
            turns = list(snapshot.get("turns_text") or [])
            if not turns:
                return
            extractor = getattr(self, "_fact_extractor", None)
            if extractor is None:
                from .memory import FactExtractor, MemoryConfig

                extractor = FactExtractor(
                    self.brain,
                    MemoryConfig.from_config(self.config.memory or {}),
                    self.config,
                )
                self._fact_extractor = extractor
            else:
                extractor.brain = self.brain
                extractor.app_config = self.config

            # 把**已有的印象**一起给它（防止换个说法说同一件事）。
            # ⚠️ 这一行就是线：不传的话，模型永远不知道它已经写过什么，
            #    印象表会堆满近义句（真机验证发现过）。
            try:
                existing = [r["text"] for r in self.memory.db.recent_impressions(12)]
            except Exception:  # noqa: BLE001
                existing = []
            impressions = await extractor.extract_impressions(turns, existing=existing)
            if not impressions:
                log.debug("这一段没提炼出印象（原因=%s）", why)
                return
            written = 0
            for text in impressions:
                self.subjectivity.record_impression(text)
                if self.memory.db.add_impression(text, source=f"segment:{why}"):
                    written += 1
            if written:
                log.info("提炼出 %d 条印象：%s", written, impressions)
                await self.bus.publish(
                    "pet.impression",
                    msg("pet.impression", items=impressions),
                )
        except Exception:  # noqa: BLE001 —— 印象提炼失败绝不能影响对话
            log.debug("印象提炼失败（已忽略）", exc_info=True)

    def _tool_list(self) -> list[Any]:
        """当前启用的工具（2026-10-12）。

        ## ⚠️ 为什么这里要 try/except 兜底成"没有工具"

        工具是**加分项**，不是关键路径。构建失败（配置错、目录建不出来）
        绝不能让宠物变成哑巴 —— 与 `FactExtractor` 的取舍一致
        （「失败就当作没抽到」）。
        """
        if not self._tools_enabled():
            return []
        if self._tools is None:
            try:
                from . import tools as tools_module

                sandbox = self.config.root.get("web", {}) or {}
                state_dir = sandbox.get("state_dir") or _default_state_dir()
                # 🔴 建工具清单之前先同步 MCP 开关（2026-10-15）。
                # 这里同步一次，保证「冷启动时配置里就是关的」也能生效 ——
                # 只靠 `_on_config_tools` 的话，那些逻辑要等一次配置变更才跑。
                self._sync_mcp_search_flag()
                self._tools = tools_module.build_tools(
                    Path(state_dir),
                    allow_web=self._web_enabled(),
                    allow_browser=self._browser_enabled(),
                )
                log.info(
                    "已启用 %d 个工具：%s",
                    len(self._tools),
                    "、".join(tool.name for tool in self._tools),
                )
            except Exception as exc:  # noqa: BLE001 —— 没有工具也要能聊
                log.warning("构建工具失败，本次不提供工具：%s", exc)
                self._tools = []
        return self._tools

    def _tools_enabled(self) -> bool:
        """工具总开关。默认**开** —— 沙箱工具不涉及网络，风险为零。"""
        section = self.config.root.get("tools", {}) or {}
        return bool(section.get("enabled", True))

    def _mcp_search_enabled(self) -> bool:
        """MCP 搜索后端开关（2026-10-15）。默认**开**。

        ## 它控制什么

        `web_search` 优先走独立的 MCP 搜索服务器（`websearch-mcp.exe`）
        还是自己抓 HTML。

        ## ⚠️ 为什么默认开（和 `web_search` 的默认关相反）

        这两个开关的隐私含义**不一样**：

        ```
        web_search    决定「能不能联网」 —— 关掉是隐私保护，所以默认关
        mcp_search    决定「联网时用哪种实现」 —— 两条路都会把查询发出去，
                      所以它**不改变隐私面**，只改变速度和结果质量
        ```

        既然不影响隐私、又快 7 倍，默认就该用好的那个。
        关掉它的唯一理由是排查（怀疑 MCP 那条路有问题时用自抓对照）。

        ## ⚠️ 关掉 ≠ 没网可搜

        关掉会退回到 `pet/web.py` 的 HTML 抓取。那条路今天实测 6/6 命中，
        只是慢（10.8 秒）。所以这个开关是**降级**，不是**断网**。
        """
        section = self.config.root.get("tools", {}) or {}
        return bool(section.get("mcp_search", True))

    def _sync_mcp_search_flag(self) -> None:
        """把配置里的开关同步给 `tools` 模块。

        ⚠️ 为什么要单独同步而不是每次调 `_web_search` 时读配置：

        `tools.py` 是**纯函数模块**，不认识 `Config` 对象。
        让它去 import config 会引入循环依赖，也会让单元测试难写。
        所以由网关（它持有 config）在配置变更时推一次。

        ⚠️ 失败不能抛 —— 这只是个开关，同步不了就用默认值（开）。
        """
        try:
            from . import tools as tools_module

            tools_module.set_mcp_search(self._mcp_search_enabled())
        except Exception as exc:  # noqa: BLE001
            log.warning("同步 MCP 搜索开关失败（用默认值继续）：%s", exc)

        # 搜索模式（2026-10-16）：accurate / fast / off —— 菜单三选一的真源
        try:
            section = self.config.root.get("tools", {}) or {}
            tools_module.set_search_mode(
                self._web_search_mode(),
                free_url=str(section.get("free_search_url") or ""),
                free_cmd=str(section.get("free_search_cmd") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("同步搜索模式失败（用默认值继续）：%s", exc)

    def _web_search_mode(self) -> str:
        """联网搜索模式（2026-10-16 用户要求）：accurate / fast / off。

        ## ⚠️ 兼容老配置

        `web_search_mode` 这个键是今天才有的。老配置里只有
        `web_search: true/false` —— 缺键时按它换算：

            web_search 缺省或 false -> off（与老行为一致，隐私默认保守）
            web_search: true        -> fast（原行为就是 websearch-mcp 优先）

        这样升级不会把"已经开着联网"的老用户静默断网。
        """
        from . import tools as tools_module

        return tools_module.mode_from_config(self.config.root.get("tools") or {})

    def _modules(self) -> Any:
        """懒建模块注册表。

        ⚠️ 不塞进 `Conversation.__init__`：那个构造函数在自检里有十几处假实现，
        加必填参数会连环炸。注册表本身很轻（只读配置），懒建没有代价。
        """
        reg = getattr(self, "_module_registry", None)
        if reg is None:
            from .modules import ModuleRegistry

            reg = ModuleRegistry(self.config)
            self._module_registry = reg
        return reg

    def _web_enabled(self) -> bool:
        """联网开关（用户要求「**开启之后**可以自由的搜索」）。默认**关**。

        ⚠️ 默认关而不是开：联网会把用户说的话、宠物想查的东西**发到搜索引擎**。
        这与"本地 ASR 语音不出本机"是同一类隐私取舍 —— 默认必须是保守的那一边，
        由用户显式打开。

        🔴 2026-10-16：真源从布尔 `tools.web_search` 改为三态
        `tools.web_search_mode`（off 才是关；缺键时按老键换算，见上）。
        """
        if not self._modules().enabled("search"):
            return False
        return self._web_search_mode() != "off"

    def _browser_enabled(self) -> bool:
        """浏览器兜底开关（2026-10-13 用户要求）。默认**关**。

        ## 它做什么

        普通 HTTP 抓取读不了的页面（最典型是**起点那种 WAF 挑战**），
        改用主进程的隐藏 Chromium 窗口去读。

        ## ⚠️ 为什么默认关，而且比 `web_search` 更需要用户点头

        走浏览器比匿名抓取的暴露面**更大**：

          · 带真实的 `User-Agent` 和 TLS 指纹
          · 会在对方站上留下 cookie（持久 partition）
          · 对方看到的是一个正常浏览器会话，而不是匿名请求

        也就是"**以用户的身份浏览**"。默认开着等于替用户做决定。

        ## ⚠️ 打开它会让读网页**变慢十几秒**

        实测一次 14~23 秒（建窗口 + 加载 + 等 WAF 挑战跑完）。
        那几秒是**挑战本身的耗时**，不是实现问题 ——
        所以只在该用的时候用（见 `tools.py` 里那个 `looks_blocked` 判据）。
        """
        section = self.config.root.get("tools", {}) or {}
        return bool(section.get("browser_fallback", False))

    async def _run_tool_round(
        self,
        req: LLMRequest,
        say_id: str,
        collected: list[str],
    ) -> tuple[Optional[list[dict[str, Any]]], Optional[str], str]:
        """跑一轮流式生成。返回 `(工具调用, 错误文本, 结束原因)`。

        抽出来是为了让 agent loop 能重复调用它 —— 见 `_generate` 里的循环。

        ⚠️ `collected` 是**跨轮累积**的：模型第一轮说"我查一下"、
        第二轮说"查到了…"，两段都要留下（用户看到的是完整的一句话）。
        """
        tool_calls: Optional[list[dict[str, Any]]] = None
        reasoning = ""
        error_text: Optional[str] = None
        reason = "done"
        # 这一轮有没有真的吐出正文（判"被截断到空"要用，见 `_generate` 的重试）
        produced = False
        # 被 max_tokens 截断的标记（`finish_reason == 'length'`）
        state_finish: dict[str, bool] = {}
        try:
            async for item in self.brain.stream(req):
                if self._interrupt.is_set():
                    reason = "interrupted"
                    break
                kind = item.get("kind")
                if kind == "delta":
                    piece = str(item.get("text") or "")
                    if not piece:
                        continue
                    produced = True
                    collected.append(piece)
                    await self.bus.publish("pet.say.delta", msg("pet.say.delta", id=say_id, text=piece))
                    # 让出事件循环：低延迟流式的前提是别把 <100ms 的增量攒成一批
                    await asyncio.sleep(0)
                elif kind == "finish":
                    """
                    🔴 记下 `finish_reason`（2026-10-12）。

                    它是"这一轮有没有被截断"的**唯一信号**，而截断的表现
                    可能是**回复完全空白**：

                         finish_reason='length'
                         completion_tokens_details={'reasoning_tokens': 400}
                         completion_tokens=400     <- 全花在推理上
                         正文 delta 共 0 字

                    用户报的「我说了话，但是它没有回答」就是这个。
                    """
                    if str(item.get("finish_reason") or "") == "length":
                        state_finish["length"] = True
                elif kind == "tool_calls":
                    """
                    ⚠️ 工具调用**不拼进正文**（2026-10-12）。

                    它的内容是结构化的 JSON（函数名 + 参数），拼进 `collected`
                    会变成一串没人看得懂的乱码，还会被 TTS 念出来。
                    所以单独接住，由 `_generate` 的循环去执行。
                    """
                    raw_calls = item.get("calls")
                    if isinstance(raw_calls, list) and raw_calls:
                        tool_calls = [c for c in raw_calls if isinstance(c, dict)]
                        # ⚠️ 思考模式的推理过程要一起带回（回填时是必填的）
                        reasoning = str(item.get("reasoning") or "")
                elif kind == "error":
                    error_text = str(item.get("message") or "生成失败")
                    reason = "error"
                    break
                elif kind == "done":
                    if not collected and item.get("text"):
                        collected.append(str(item["text"]))
        except asyncio.CancelledError:
            reason = "interrupted"
            raise
        except Exception as exc:  # noqa: BLE001 —— 提供者异常不能杀掉对话
            log.exception("生成过程中异常")
            error_text = f"{type(exc).__name__}: {exc}"
            reason = "error"
        # 把"被截断到空"这件事报出去 —— 它是**正常结束但没内容**，
        # 不报的话上层分不清"它不想说话"和"它的话被截没了"
        if state_finish.get("length") and not produced:
            reason = "truncated"
            # ⚠️ DEBUG 而不是 WARNING：这条路径**有救**（上层会加大预算重试），
            #    而 WARNING 会让人以为用户看到了空回复。真正的告警在上层
            #    ——那里才知道重试成没成功（见 `_generate`）。
            log.debug(
                "本轮被 max_tokens 截断且正文为空（推理吃光了预算）。max_tokens=%s",
                req.max_tokens,
            )
        return tool_calls, reasoning, error_text, reason

    async def _force_answer(
        self, req: LLMRequest, say_id: str, collected: list[str]
    ) -> None:
        """拿掉工具，用已查到的资料逼模型给一个回答（2026-10-12）。

        ## 为什么需要它

        实测失败模式：模型**陷在"搜索模式"里出不来** —— 用户问的是一个它
        不确定的东西，它一轮又一轮换词搜，宁可继续搜也不肯用已有的东西回答。
        连撞 3 轮上限之后，原来的代码只是 `break`，**用户什么都没看到**。

        而加大预算救不了：实测某一轮 `completion_tokens=1333`（预算 1600），
        它依然选择"再搜一次"。**这不是预算问题，是决策问题。**

        ## 两个动作（缺一不可）

        ① `req.tools = None` —— **让它无从再搜**。
           光在提示词里写"别再搜了"只是建议；工具不在请求里，服务商就
           根本不会返回 `tool_calls`。
        ② 保留 `req.tool_results` —— 那是它**唯一的上下文**。
           `_body` 那边会因为 `tools` 为空而自动改用 system 消息喂进去
           （见那里的说明：`role: tool` 要求同时声明 `tools`，否则 400）。

        ## ⚠️ 顺手把 `max_tokens` 托底

        强制回答时它往往已经想了很多轮，推理会吃掉大量预算。截断到空的话
        这次补救就白做了 —— 所以给一个下限，而不是让它用 `400` 去赌。
        """
        req.tools = None
        # 推理会吃掉预算（第八十七节那个坑），强制回答时给足
        req.max_tokens = max(int(req.max_tokens), 1200)
        try:
            await self._run_tool_round(req, say_id, collected)
        except Exception as exc:  # noqa: BLE001 —— 补救失败也不能让这一轮崩掉
            log.warning("强制回答失败：%s", exc)
        if collected:
            log.info("强制回答拿到正文（%d 字）", len("".join(collected)))
        else:
            # 到这一步就是真的没话说了 —— 明确记下来（用户会看到空气泡）
            log.warning("强制回答**仍然没有正文** —— 用户会看到没有回答")

    def _tool_schemas(self) -> Optional[list[dict[str, Any]]]:
        """工具清单（给 `LLMRequest.tools`）。没有工具时返回 `None`。

        ⚠️ 返回 `None` 而不是空列表：提供者据此**一个字节都不加**到请求体里
        （空列表在某些服务商那里也可能被判为"提供了 tools 但格式不对"）。
        """
        tools = self._tool_list()
        if not tools:
            return None
        try:
            from . import tools as tools_module

            return tools_module.schemas(tools)
        except Exception as exc:  # noqa: BLE001
            log.warning("生成工具 schema 失败：%s", exc)
            return None

    async def _execute_tool_calls(
        self, calls: list[dict[str, Any]], req: LLMRequest, reasoning: str = ""
    ) -> None:
        """执行模型要求的工具调用，并把结果**写回 `req`**（2026-10-12）。

        ## 为什么要并发

        实测 `deepseek-flash` **一次就发了两个搜索**（中英文各一条）。
        串行执行的话两次网络请求相加 —— 而它们之间毫无依赖，
        并发是白拿的一倍速度。

        `asyncio.gather` 而不是逐个 await。

        ## ⚠️ 结果必须**按 index 顺序**写回

        `gather` 保序，但如果将来改成别的调度方式，顺序错了的后果是
        「tool_call_id 与内容对不上」—— 服务端会报错，而那个错
        看起来像协议 bug，实际是配对错了。

        ## ⚠️ 每个 `call_id` 都要有一条结果，**一条都不能少**

        OpenAI 兼容接口要求：`tool_calls` 里声明的每一个 id，
        都必须有一条对应的 `role: "tool"` 消息。漏一个就报
        「缺少 tool 响应」。所以 `run_tool` 里的异常全部变成文本
        （见那里的说明）—— **宁可回一条"失败了"，也不能不回**。
        """
        from . import tools as tools_module

        tool_list = self._tool_list()

        async def run_one(call: dict[str, Any]) -> Any:
            from .brains.base import ToolExchange

            call_id = str(call.get("id") or "")
            name = str(call.get("name") or "")
            arguments = str(call.get("arguments") or "")
            # ⚠️ 把 `self.bus` 传下去 —— 工具据此给状态条发「搜索中」
            #    （2026-10-12 用户要求）。不传的话状态条不会变。
            content = await tools_module.run_tool(tool_list, name, arguments, self.bus)
            # ⚠️ `reasoning` 挂在**每一条**上，但只有第一条可能非空 ——
            # 它对应的是"那条 assistant 消息"，而一次可能要求调多个工具。
            # 提供者取第一条非空的（见 `_body` 里的 `next(...)`）。
            return ToolExchange(
                call_id=call_id,
                name=name,
                arguments=arguments,
                content=content,
                reasoning=reasoning,
            )

        results = await asyncio.gather(*(run_one(call) for call in calls))
        req.tool_results = list(results)
        # 回填之后**不能再当成"自主发言"**：那一轮是"接着说"，
        # 而且 `user_text=None` 会让模型以为该自己起话头，可能跑偏。
        # （`user_text` 本身在 req 里保持原样 —— 它就是本轮用户说的话。）

    # ------------------------------------------------------------------ Prompt

    def build_system_prompt(self, idle: bool = False) -> str:
        """
        每轮现拼 system prompt。

        为什么不缓存：人格、活泼度、情绪三者都会热更新，缓存就意味着用户拖了
        滑杆却半天不生效 —— 那正是 P5 要避免的体验。

        ## 提示词可被用户**关掉**（`persona.system_prompt_enabled`）

        用户要求：「提示词用户可自由选择可选可不选」。关掉时**人格那几段全部不加**
        （system_prompt / speech_style / idle_topics），只剩"活着的状态"
        （活泼度、情绪、主动开口的场合说明）—— 那些是**运行时状态**而不是人设，
        去掉会让宠物连"现在该主动说一句"都不知道。

        ⚠️ 缺省是**开启**：老配置文件里没有这个字段，不能因为读不到就悄悄把人设关了。
        """
        persona = self.config.persona
        parts: list[str] = []

        enabled_raw = persona.get("system_prompt_enabled")
        # 只有**显式为假**才关（`false` / `no` / `0` / `off`），其余（含缺失）都当开启
        enabled = True
        if enabled_raw is not None:
            enabled = str(enabled_raw).strip().lower() not in ("false", "no", "0", "off")

        if enabled:
            """
            ⚠️ `system_prompt` 也要走 `_render_prompt` 那套兜底（2026-10-07 修）。

            原来这里是裸的：

                parts.append(str(persona.get("system_prompt") or "").format(name=...))

            **没有 try** —— 而用户能编辑这段人格（设置页）。实测事故：
            `config/persona.yaml` 里把占位符写成了 `{小钰}`（本该是 `{name}`），
            于是 `.format(name=...)` 抛 `KeyError: '小钰'`。

            后果不是"人格没生效"，而是**每一轮对话都在这里炸掉，宠物一句话
            都说不出来** —— 而用户绝不会想到是"人格里那个花括号写错了"。

            `_render_prompt` 的注释里已经写明了这个道理（"绝不能让整轮对话崩掉"），
            只是这一行当时漏了。现在两边口径一致：渲染失败 -> 记 warning ->
            用**原始文本**继续（人格宁可带着字面的 `{小钰}` 发出去，
            也不能让宠物哑掉）。
            """
            raw_persona = str(persona.get("system_prompt") or "")
            if raw_persona.strip():
                try:
                    parts.append(raw_persona.format(name=self.config.pet_name).strip())
                except (KeyError, IndexError, ValueError) as exc:
                    log.warning(
                        "人格提示词 persona.system_prompt 渲染失败（%s）——"
                        "已按原文发出（花括号没被替换）。检查是否写了 `{name}` 之外的花括号。模板：%r",
                        exc,
                        raw_persona[:80],
                    )
                    parts.append(raw_persona.strip())
            styles = persona.get("speech_style") or []
            if styles:
                parts.append(
                    self._render_prompt(
                        "speech_style",
                        "说话风格要求：{styles}。",
                        styles="；".join(str(s) for s in styles),
                    )
                )
            topics = persona.get("idle_topics") or []
            if topics:
                parts.append(
                    self._render_prompt(
                        "idle_topics",
                        "你偶尔会主动聊起：{topics}。",
                        topics="、".join(str(t) for t in topics),
                    )
                )

        parts.append(self.config.activity_for_prompt())
        parts.append(self.emotion.describe())
        parts.append(
            self._render_prompt(
                "emotion",
                "当前情绪：精力 {energy}、心情 {mood}、亲近度 {intimacy}"
                "（0~1，仅供参考，不要在回复里报数字）。",
                energy=f"{self.emotion.energy:.2f}",
                mood=f"{self.emotion.mood:.2f}",
                intimacy=f"{self.emotion.intimacy:.2f}",
            )
        )
        # ── 注意力（2026-10-17）──
        #
        # 🔴 这是「注意力在用户身上 / 在自己身上」的分叉点。
        # 只给**语义**不给数字：模型看到"注意力 0.73"只会瞎编成"我的注意力有 73%"。
        # `describe()` 已经按三档转成自然语言，这里再补一条**行为约束** ——
        # 光描述状态不够，得告诉它"所以现在该怎么说话"。
        parts.append(self.subjectivity.describe())
        # ── 自己的兴趣（2026-10-17）──
        #
        # 用户要求「爱好由我后续对话形成」。`interest` 表里是**它自己**的关注点
        # （从反复聊到的内容里长出来的），所以措辞是"你自己在意的东西"，
        # 不是"用户喜欢的东西" —— 后者是记忆层的事。
        try:
            own = self._interest_topics(limit=5)
        except Exception:  # noqa: BLE001
            own = []
        if own:
            parts.append(
                "你自己有兴致的东西（从你们聊过的内容里慢慢长出来的）："
                + "、".join(own)
                + "。聊到这些时你可以有自己的看法，不必什么都顺着用户。"
            )
        # ── 它自己做过的事（2026-10-17 随机事件）──
        #
        # 用户要求随机事件「列为重点功能」。不告诉模型的话，它做了也**不知道
        # 自己做过** —— 于是我昨天搜到一句话想跟你说这种连续性永远不会发生。
        #
        # ⚠️ 只给最近 2 条，且明确不必主动提：
        #    塞太多会让它每轮都复述我刚刚去搜了哲学，变成话痨。
        try:
            recent = self.memory.db.recent_events(2) if self.memory else []
        except Exception:  # noqa: BLE001
            recent = []
        if recent:
            lines = [str(r.get("detail") or r.get("label") or "") for r in recent]
            lines = [x for x in lines if x]
            if lines:
                parts.append(
                    "你最近自己做过的事：" + "；".join(lines)
                    + "。这些是你自己的经历，**除非正好聊到，不用主动汇报**。"
                )
        if self.subjectivity.band == "self":
            parts.append(
                "你现在主要在想自己的事。可以聊聊自己的兴趣、自己的感受，"
                "不必急着把话题拉回到用户身上。"
            )
        elif self.subjectivity.band == "user":
            parts.append(
                "你现在的心思在用户身上。多关注他说的内容、他的状态，"
                "**不要**突然讲起自己的事（他会觉得你没在听）。"
            )
        if idle:
            # 主动开口与"被问到"是两种完全不同的场合，提示词必须分开。
            # 否则模型会把主动发言写成对空回复"有什么可以帮您"，非常出戏。
            parts.append(self._render_prompt("idle_occasion", _IDLE_OCCASION_FALLBACK))
            # 用户要求：注意力在用户身上时，主动开口**几乎不聊自己**；
            # 只有在用户结束对话 / 不在聊天时（attention 高），才聊自己的事。
            if not self.subjectivity.may_talk_about_self(in_conversation=False):
                parts.append(
                    "这次主动开口：**不要**聊你自己的事，围绕用户或周围发生的事说。"
                )
        # 当前时间放**最后**：它是"客观事实"，而上面那些是"人设与状态"。
        # 实测把时间插在人设中间会让模型把日期当成设定的一部分去复述
        # （"我是2026年10月11日出生的…"），放末尾更像背景信息。
        parts.append(self._current_time_prompt())
        return "\n".join(p for p in parts if p)

    def _current_time_prompt(self) -> str:
        """当前时间那段提示词（用户要求，2026-10-11）。

        ## 为什么要有它

        模型**不知道今天是几号** —— 它的时间感停在训练数据截止那天。
        不给的话会：把几年前的日期当"现在"、深夜说早上好、
        把很久以前的事说成"最近"、分不清工作日与周末。

        ## ⚠️ 三个刻意的设计

          ① **年份一定要给**。只给月和日，模型会用自己的先验补年份 ——
             而那正是错得最离谱的地方。
          ② **明确说"不要把训练截止日期当成现在"**。只说"现在是 X"时，
             模型仍可能把自己记忆里的日期搬出来（尤其谈到它"知道"的事件时）。
          ③ **明确说"不必主动报时间"**。否则每次回复都带一句"现在是晚上十点"，
             非常出戏 —— 与 `emotion` 那段"不要报数字"是同一条取舍。

        ## 为什么用本地时间而不是 UTC

        用户问"现在几点"要的是**他墙上的钟**。宠物跑在用户机器上，
        `datetime.now()` 就是本地时间，正确。
        """
        from datetime import datetime

        now = datetime.now()
        # 星期用中文：模型的输出是中文，中文星期比 "Saturday" 更不容易被抄错
        weekdays = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
        weekday = weekdays[now.weekday()]
        hour = now.hour
        if hour < 5:
            part = "凌晨"
        elif hour < 9:
            part = "早上"
        elif hour < 12:
            part = "上午"
        elif hour < 14:
            part = "中午"
        elif hour < 18:
            part = "下午"
        elif hour < 23:
            part = "晚上"
        else:
            part = "深夜"
        return self._render_prompt(
            "current_time",
            _CURRENT_TIME_FALLBACK,
            time=now.strftime("%Y年%m月%d日 %H:%M"),
            weekday=weekday,
            part_of_day=part,
        )

    def _prompt_templates(self) -> dict[str, Any]:
        """读 `prompts:` 段（顶层键，与 persona 平级）。

        单独一个方法而不是到处 `self.config.root.get("prompts")`：
        越少地方知道"模板住在配置的哪一层"，将来挪位置就越省事。
        """
        section = self.config.root.get("prompts")
        return section if isinstance(section, dict) else {}

    def _render_prompt(self, key: str, fallback: str, **values: Any) -> str:
        """按 `prompts.<key>` 渲染一段提示词；缺失/格式错时退回内置默认。

        ## ⚠️ 为什么失败要退回而**不是**抛错

        提示词模板是用户能编辑的（设置页）。他打错一个花括号（比如 `{name` 或
        写了不存在的 `{foo}`）时，**绝不能让整轮对话崩掉** ——
        那时宠物会完全不回话，而用户根本想不到是"提示词里少了个括号"。

        所以：渲染失败 -> 记 warning（要能看见）-> 用内置默认继续。
        这样最坏情况是"你的改动没生效"，而不是"宠物不说话了"。
        """
        raw = self._prompt_templates().get(key)
        template = str(raw) if isinstance(raw, str) and raw.strip() else fallback
        try:
            return template.format(**values).strip()
        except (KeyError, IndexError, ValueError) as exc:
            log.warning(
                "提示词模板 prompts.%s 渲染失败（%s），已退回内置默认。模板：%r",
                key,
                exc,
                template[:80],
            )
            try:
                return fallback.format(**values).strip()
            except (KeyError, IndexError, ValueError):
                return fallback.strip()

    def _compose_context_hint(
        self,
        user_text: Optional[str],
        trigger: Optional[dict[str, Any]],
        exclude_turn_id: int = 0,
    ) -> str:
        """
        把两类上下文拼成一段提示词：**长期记忆**与**当下所见**。

        分开拼而不是混在一起写，是因为它们的可信度与用途都不同：
          · 记忆回答"你是谁、我们聊过什么"，用于显得认识用户；
          · 观察回答"我现在看到了什么"，用于显得活在当下。
        两者都可能为空 —— 那时不留任何痕迹，比说一句"我什么都不记得"自然得多。

        `exclude_turn_id`：**排掉刚写进记忆的这一句**（见 `_recall_hint`）。
        """
        pieces: list[str] = []

        # 长期记忆：由 MemoryService 一次拼好三类来源
        #   ① 固定注入（习惯/人设/常说的话）  ② 最近记忆（时间窗口内原文）  ③ 检索（提炼结果）
        # 传 query 让它能把当前话题算进检索；自主发言时没有用户输入，就拿观察内容当查询。
        query = user_text or str((trigger or {}).get("context_hint") or "")
        recall = self._recall_hint(query, exclude_turn_id)
        if recall:
            pieces.append(recall)

        # 当下所见（P4）
        observation = str((trigger or {}).get("context_hint") or "")
        if observation:
            pieces.append(observation)

        # 截图对话的"描述有没有进提示词"要在这一层再确认一次 ——
        # 上一层的 hint 非空、但这里如果 observation 为空（键名写错/被覆盖），
        # 模型就会以为屏幕是空的（实测它真的会说"白茫茫一片"）。
        if (trigger or {}).get("source") == "snap":
            log.info(
                "截图对话：context_hint 组装完成（记忆 %d 字 + 观察 %d 字）",
                len(recall),
                len(observation),
            )

        return "\n\n".join(pieces)

    def _recall_hint(self, query: str = "", exclude_turn_id: int = 0) -> str:
        """
        取记忆提示词片段。

        单独一个方法而不是塞进 build_system_prompt：检索是有成本的（要算嵌入、
        要查数据库），而 build_system_prompt 在每轮都会被调用。分开之后
        谁在什么时候引入记忆一目了然。

        ⚠️ 注意它现在**即使 query 为空也要调用** —— 因为"固定注入"和"最近记忆"
        与当前话题无关，是每次都要带的。老实现用 `if query:` 挡住，会把这两类也挡掉。

        ## ⚠️ `exclude_turn_id`：排掉"刚写进去的这一句"（2026-10-08）

        不排的话，同一句话会在**两处**出现：

            messages[0] system   「[今天] 用户：周杰伦的雨下一整晚。」  ← 记忆段
            messages[1] user     「周杰伦的雨下一整晚。」              ← 本轮提问

        **模型看到两遍**，于是每轮都回「你说了两遍」——
        用户以为是网络传输重了（「好像是传输的时候传了两遍」），
        实际是**提示词里重复**。
        """
        if self.memory is None:
            return ""
        try:
            exclude = {exclude_turn_id} if exclude_turn_id else None
            hint = self.memory.inject_hint(query, exclude_ids=exclude)
        except Exception:  # noqa: BLE001 —— 记忆检索失败绝不能影响对话
            log.exception("记忆检索异常（已忽略，本轮不带记忆）")
            return ""

        # ── 兴趣命中的副作用（2026-10-17）──
        #
        # 用户要求"利用检索层自发形成"爱好，所以**检索命中**也是兴趣的强化信号。
        #
        # 🔴 但只在"兴趣**真的相关**"时才拉注意力，不能无条件拉：
        #    `on_self_hit` 一次 +0.20，而用户说话才 -0.25 ——
        #    若不判相关性，每轮检索都会把它抵消掉，
        #    表现为"用户说什么它都走神回自己的事"。这是设计上最容易错的一步。
        #
        # 判据：兴趣主题出现在**用户这句话**里，或出现在**命中的记忆**里。
        try:
            topics = self._interest_topics()
            if topics:
                blob = f"{query}\n{hint or ''}"
                hit = [t for t in topics if t and t in blob]
                if hit:
                    self.subjectivity.on_self_hit(hit)
                    log.debug("兴趣命中 -> 注意力偏向自己：%s", hit)
        except Exception:  # noqa: BLE001
            log.debug("更新注意力焦点失败（已忽略）", exc_info=True)
        return hint

    def _interest_topics(self, limit: int = 4) -> list[str]:
        """当前权重最高的几条兴趣（没记忆库就返回空）。"""
        if self.memory is None:
            return []
        try:
            rows = self.memory.db.top_interests(limit=limit)
        except Exception:  # noqa: BLE001
            return []
        return [str(r.get("topic") or "") for r in rows if r.get("topic")]

    async def _extract_in_background(self, user_text: str, reply_text: str) -> None:
        """
        后台把这一轮压成"长期事实"，写进检索记忆层。

        刻意不 await（调用处用 `create_task`）：用户不该为"宠物记笔记"多等一秒，
        提炼失败也绝不能影响对话本身。

        复用 `memory.py` 的 `FactExtractor`（它只负责"调 LLM 抽句子"这件事，
        与存储层无关），抽出来的结果交给 `MemoryService` 落库 ——
        这样存储从 JSONL 换到 SQLite 时，提炼逻辑一行都不用改。
        """
        if self.memory is None:
            return
        try:
            from .memory import FactExtractor, MemoryConfig

            extractor = getattr(self, "_fact_extractor", None)
            if extractor is None:
                extractor = FactExtractor(
                    self.brain,
                    MemoryConfig.from_config(self.config.memory or {}),
                    # 传应用级配置：提炼提示词（`prompts.extract`）也在可编辑的那批里
                    self.config,
                )
                self._fact_extractor = extractor
            else:
                # brain 可能因为设置变更被重建过，保持同步
                extractor.brain = self.brain
                # 提炼提示词也可能刚被改过，一并同步（否则要重启才生效）
                extractor.app_config = self.config

            facts = await extractor.extract([("user", user_text), ("assistant", reply_text)])
            if facts:
                # 默认进 'recent' 层：它们已经是提炼过的短事实，但**还不是**用户
                # 标记的"固定注入"。要变成习惯/人设，靠 tags + level 由用户或后续归纳决定。
                written = self.memory.remember_facts(
                    facts, kind="fact", tags="", level="recent", importance=0.5
                )
                if written:
                    log.info("提炼出 %d 条长期记忆", written)
        except Exception:  # noqa: BLE001 —— 提炼失败绝不能影响对话
            log.exception("后台提炼失败（已忽略）")

    # ------------------------------------------------------------------ 历史


    def _thinking_flag(self, brain_cfg: dict[str, Any]) -> Optional[bool]:
        """
        思考模式开关（用户在宠物菜单里切）—— 返回 `None` 表示**不传该参数**。

        ## ⚠️ 为什么**每次现读**配置，而不是在构造时固定下来

        这是用户可随时切换的开关。固定在对象里的话，改完要重启 sidecar 才生效
        —— 而用户按下菜单那一刻期待的是**立刻**。

        `self.config.providers` 是普通 dict，`config.update` 会替换它，
        所以现读拿到的就是最新值。

        ## ⚠️ 为什么默认是 `True`（开），但返回值可能是 `None`

        用户明确要求「**默认开启**」，所以配置里 `thinking: true`。

        但**只有 base_url 确实是 DeepSeek 时才把这个值传出去** ——
        `thinking` 是 DeepSeek 专有参数，而 `providers.brain` 支持任何
        OpenAI 兼容接口。对不认识它的服务商多传一个未知键可能 400。

        所以：
          · 配置里没写这个键      -> `None`（不传，尊重服务端默认）
          · 不是 DeepSeek         -> `None`（不传，避免 400）
          · 是 DeepSeek 且配了值  -> `True`/`False`

        ⚠️ 注意"配置里没写"和"配置里写了 false"必须**分得开**：
        用 `in` 判断而不是 `or`，否则 `false` 会被当成"没写"。
        """
        if "thinking" not in brain_cfg:
            return None
        # 以**当前生效厂商**的 profile 为准（它已经合并好 named + 平铺字段）
        try:
            profile = self.config.brain_profile()
        except Exception:  # noqa: BLE001 —— 取配置失败不该影响对话
            log.debug("读 brain_profile 失败，按传进来的 brain_cfg 处理", exc_info=True)
            profile = brain_cfg
        # ① 厂商显式声明不认这个参数（如硅基流动）-> 一个字节都不传
        if profile.get("supports_thinking") is False:
            log.debug("厂商声明不支持 thinking 参数，跳过")
            return None
        # ② 兜底判据：地址不是 DeepSeek -> 未知字段，传了有风险
        base_url = str(profile.get("base_url") or brain_cfg.get("base_url") or "")
        if "deepseek" not in base_url.lower():
            log.debug("base_url 不是 DeepSeek，跳过 thinking 参数（避免未知字段）")
            return None
        value = brain_cfg.get("thinking")
        # 兼容字符串写法（YAML 的 false / "false" / "off" 都当关）
        if isinstance(value, str):
            return value.strip().lower() not in ("false", "no", "0", "off")
        return bool(value)






    def stats(self) -> dict[str, Any]:
        return {
            "turns": len(self.history),
            "generating": self.current is not None and not self.current.done(),
            "emotion": self.emotion.as_dict(),
        }


class Gateway:
    """连接管理 + 编解码 + 总线接线。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        # 模块注册表（阶段一：开关层）。放在最前面 —— 后面的语音/自主/屏幕都要问它。
        from .modules import ModuleRegistry

        self.modules = ModuleRegistry(config)
        self.modules.log_summary()
        self.bus = Bus()
        # 先生成提供者，再建记忆与对话，最后把同一个 brain 实例交给双方。
        # 顺序不能乱：记忆提炼要用 brain，对话也要用 brain。
        self.brain = build_brain(config, self._build_emotion())
        # 记录当前提供者配置，用于热更新时判断"要不要重建大脑"
        self._brain_signature: dict[str, Any] = {
            key: config.providers.get("brain", {}).get(key)
            for key in ("provider", "base_url", "model", "api_key_env", "timeout_s")
        }
        self.memory = self._build_memory()
        self.conversation = Conversation(
            self.bus, config, memory=self.memory, brain=self.brain
        )
        self.conversation.attach()

        # 自主调度器：亲近度取自对话的情绪状态机，这样"养久了更爱搭话"是
        # 真实的耦合，而不是又一份独立状态。
        self.scheduler = Scheduler(
            config,
            self.bus,
            intimacy_provider=lambda: self.conversation.emotion.intimacy,
        )
        self._autonomy_task: Optional[asyncio.Task[None]] = None
        # 随机事件（2026-10-17，用户列为重点功能）
        self.events = EventEngine(
            base_chance=float(config.events.get("base_chance", 0.02)),
            min_gap_s=float(config.events.get("min_gap_s", 600.0)),
            idle_before_s=float(config.events.get("idle_before_s", 300.0)),
            quiet_hours=list(config.events.get("quiet_hours") or []),
            max_per_day=int(config.events.get("max_per_day", 12)),
        )
        self._events_task: Optional[asyncio.Task[None]] = None
        # 自主截图循环（2026-10-18）。**必须在这里初始化为 None** ——
        # start_autonomy 里靠 is None 判断要不要起任务，漏了这句就是
        # AttributeError（这个项目踩过一次"两边都写好了、中间那根线没接"）。
        self._autosnap_task: Optional[asyncio.Task[None]] = None
        # 上一次「截图说话」的时刻（monotonic）。0 = 还没说过。
        # 截图说完 60 秒内不搭话 —— 见 _on_autonomy_speak 里的优先级判断。
        self._last_screen_speak_at: float = 0.0
        # 免打扰是配置项，但调度器需要一个本地的即时状态：初始化时同步一次。
        self.scheduler.set_muted(bool(config.autonomy.get("muted", False)))

        # 屏幕感知（P4）。采集器按配置构建；不可用时退化为"不看屏幕"，
        # 整条链路照常工作，只是调度器少一个信号源。
        vision_cfg = dict(config.providers.get("vision", {}))
        content_chain = self._build_content_pipeline(vision_cfg)
        needs_image = any(name in ("ocr", "vlm") for name in content_chain.available_chain())
        self.vision = VisionLoop(
            bus=self.bus,
            capturer=build_capturer(vision_cfg, keep_image=needs_image),
            rules=PrivacyRules.from_config(config.privacy),
            detector=ChangeDetector(
                threshold=int(vision_cfg.get("change_threshold") or 5)
            ),
            interval_s=float(vision_cfg.get("interval_s") or 20.0),
            idle_backoff_max=int(vision_cfg.get("idle_backoff_max") or 4),
            content=content_chain if needs_image else None,
            content_timeout_s=float(vision_cfg.get("content_timeout_s") or 20.0),
        )
        self._vision_task: Optional[asyncio.Task[None]] = None
        # ⚠️ 构造时就要把配置里的开关**同步到采集器**，不能只等 `config.update`。
        #
        # 原因：`VisionLoop.__init__` 里 `self.watching = True`（默认开），
        # 而 `_scheduler_on_config` 只在收到 config.update 时才调 set_watching。
        # 于是"启动后从未改过设置"的情况下，`watching` 一直是 True ——
        # 界面上开关显示"关"（那个读的是配置），实际采集器却在跑，
        # 两者不一致，正是 vision.py 注释里警告的"假开关"。
        # 用户要求"默认关闭"之后这条尤其重要：默认关闭必须**真的**不截屏。
        self.vision.set_watching(bool(vision_cfg.get("enabled", False)))

        # 语音链路（P3）。音频 I/O 由前端提供（Web Audio API），Python 侧只拿到
        # PCM 字节做端点检测与识别 —— 这样这一层完全不依赖任何音频库。
        if self.modules.enabled("voice"):
            voice_pipeline = build_voice_pipeline(config, config.api_key())
        else:
            # 关掉语音 = **连引擎都不建**（本地大模型因此不会加载）。
            # 用 mock 顶上，是为了让 VoiceSession 的接口仍然齐 —— 不然所有
            # 调用方都要判空，反而容易漏。
            from .voice import MockAsrEngine, MockTtsEngine, VoicePipeline

            log.info("语音模块关掉了：不建 ASR/TTS 引擎（本地模型不会加载）")
            voice_pipeline = VoicePipeline(
                asr=MockAsrEngine(),
                tts=MockTtsEngine(),
                asr_note="语音模块已关闭",
                tts_note="语音模块已关闭",
            )
        # 启动时打一行"能力报告"（用户要求：选了云端就**不要**带着大模型启动，
        # 这行日志是验证"到底加载了什么"的唯一凭据 —— 光看配置不算数）。
        self._log_capability_report(config, voice_pipeline)
        self.audio_input = QueueAudioInput()
        self.voice = VoiceSession(
            config=config,
            bus=self.bus,
            asr=voice_pipeline.asr,
            tts=voice_pipeline.tts,
            audio_in=self.audio_input,
            audio_out=BroadcastAudioOutput(self._send_voice_audio),
        )
        self.voice.attach()
        # 把「发消息给主进程」的能力交给需要它的引擎（本地 TTS 要请求主进程
        # 起 PowerShell 合成 —— 见 voice.py 里 WindowsSapiTtsEngine 的说明）
        self.voice.attach_sender(self.broadcast)
        """
        ## 浏览器兜底也要这个能力（2026-10-13）

        和上面 TTS 那条**同一个理由**：`web.py` 做不到"执行 JS 过 WAF"，
        只有主进程的 Chromium 做得到。

        ⚠️ **不注入的话不是报错，而是静默不可用** ——
        `web.browser_available()` 返回 False，`_web_fetch` 就永远不走兜底。
        表现是"设置里的开关打开了、起点还是读不了"，而两边日志都正常。
        所以这一行是**功能的一部分**，不是可选装饰。

        ⚠️ 用 `self.broadcast`（和 TTS 一样）而不是 `send_to`：
        这类请求的终点是**主进程自己**，不需要知道是哪个客户端。
        """
        web.attach_browser(self.broadcast)
        self._voice_task: Optional[asyncio.Task[None]] = None
        # 试听任务（fire-and-forget）。
        #
        # ⚠️ 必须**持有引用**：`asyncio` 只保留弱引用，不存起来的话任务可能
        # 在跑完之前被垃圾回收 —— 表现是"偶尔不出声"，极难复现。
        self._voice_say_task: Optional[asyncio.Task[None]] = None
        # 当前语音引擎的"指纹"，用来判断 config.update 是否需要真的重建引擎
        # （见 `_on_config_voice_engines`：不加这个的话每次改配置都要重载 4 秒的模型）
        self._voice_engine_signature = self.voice.engine_signature()
        self._memory_task: Optional[asyncio.Task[None]] = None
        # 语音发送序号：二进制帧与 JSON 元信息靠它配对
        self._speech_seq = 0
        # 最近一次屏幕观察的时间戳（单调时钟）。P4 接上之前恒为 None，
        # 调度器因此只靠"空闲"一个信号决策 —— 这是有意的降级，不是漏实现。
        self._last_observation_at: Optional[float] = None

        self._wire_autonomy()

        # ⚠️ 灌回历史**不在这里**做，而是在 `Conversation.__init__` 末尾。
        #
        # 原因：`self.history` 是 **`Conversation` 的属性**，不是 `Gateway` 的。
        # 我一开始把 `bootstrap_history()` 放在这里，于是
        # `self.bootstrap_history()` 报 `AttributeError`（Gateway 上没有）。
        # 放回 Conversation 之后"历史"这件事完全自洽 ——
        # Gateway 不需要知道它的细节。

        self.plugins = PluginHub(self.bus, self, config.root)
        self.server = WebSocketServer(
            on_text=self._on_client_text,
            on_binary=self._on_client_binary,
            on_connect=self._on_client_connect,
            on_disconnect=self._on_client_disconnect,
        )
        # 订阅全部事件用于下行转发与日志（"*" 通配）
        self.bus.subscribe("*", self._on_bus_event)
        # 自主发言请求 -> 真正生成。调度器只发事件，不直接驱动大脑，
        # 这样"决定说话"与"怎么说话"彻底解耦。
        self.bus.subscribe("autonomy.speak", self._on_autonomy_speak)
        # 「自言自语」20 分钟到期回落（2026-10-17）：要把新档位告诉主进程，
        # 否则菜单上还勾着「自言自语」、实际已经安静了。
        self.bus.subscribe("autonomy.tier_decayed", self._on_tier_decayed)

        # 「自言自语」念的句子表（2026-10-17 用户要求：念什么来自一张表，
        # 不让模型自由编 —— 编出来的经文是假的）。
        #
        # ⚠️ 表在 `config/quotes.yaml`（和 persona.yaml 同目录）。
        #    以后"看小说自动收集好句子"也是往那个文件里追加。
        quotes_path = (config.path.parent / "quotes.yaml") if config.path else None
        # 句子表挪到数据库（2026-10-18 用户要求："这个表太慢，不好维护，
        # 用数据库单开一张表来存储"）。yaml 退成**一次性种子**：
        # 库空了才导入，之后以库为准。
        from .store import default_db_path

        # default_db_path 要项目根：pet.py 在 pps/brain/pet/，往上三层
        # 就是项目根（和 gateway 里 _default_sandbox_root 的算法一致）。
        self.quotes = quotes_mod.QuoteRepo(default_db_path(Path(__file__).resolve().parents[3]))
        self.quotes.ensure_kinds()
        imported = self.quotes.import_seed(quotes_path) if quotes_path is not None else 0
        # 随包发布的完整语料（18206 条）：首启动灌一次，之后以库为准（2026-10-18）
        if quotes_path is not None:
            corpus = quotes_path.parent / "quotes-corpus.jsonl"
            seeded = self.quotes.import_corpus(corpus)
            if seeded:
                imported += seeded
                logging.getLogger("pet.quotes").info("语料导入：%d 条（含种子 %d）", imported, imported - seeded)

        # 「自言自语」和用户聊起来之后，注意力会被拉回用户（2026-10-18 用户要求）：
        #   3 分钟内超过 3 轮 -> 拉回用户、暂停念经 3 分钟
        #   之后 3 分钟不搭话 -> 注意力回自己、继续念
        self._user_turns: list[float] = []
        self._monologue_hold_until: float = 0.0
        # 「自言自语」下用户聊得多就把注意力拉回来（2026-10-18 用户要求）
        self.bus.subscribe("user.text", self._on_user_text_for_monologue)

        # 最近念过的原文（避免连着两次念同一句 —— 念经的观感全靠"每次不一样"）
        self._recent_quotes: list[str] = []
        if imported:
            log.info("自言自语：从 quotes.yaml 导入 %d 句到数据库", imported)
        if self.quotes.count():
            log.info("自言自语：句子表（数据库）%s", self.quotes.summary())
        else:
            log.warning(
                "自言自语：句子表是空的（%s）—— 这一档会退化成平常的自主发言",
                quotes_path,
            )

    # ------------------------------------------------------------------ 自主行为接线

    def _build_emotion(self) -> EmotionState:
        """
        情绪状态机由对话持有，但 brain 构造时需要它。

        所以先建一个、由 Conversation 接管（两处引用同一个对象），
        这样后台提炼也能读到当前情绪，不必再造一份。
        """
        return EmotionState()

    def _build_memory(self) -> Any:
        """
        构建长期记忆（**SQLite 版**，2026-10-05 起）。

        数据落在 `apps/desktop/pet-data/memory/pet.db` —— 用户要求
        「记忆要全部保留在本地项目中」，而且实测受限环境里写用户目录会 EPERM。

        老实现（JSONL + `MemoryManager`）在 `memory.py` 里保留着，但已不再使用：
        新需求（原文全量留档、珍藏记忆、时间窗口注入、按标签分类）在 JSONL 上
        要么全文件扫描、要么没有事务。SQLite 是标准库，仍然零依赖。
        """
        from .memory2 import MemoryService

        return MemoryService(self.config)

    def _build_content_pipeline(self, vision_cfg: dict[str, Any]) -> Any:
        """
        构建内容抽取链。

        base_url / 密钥的复用顺序（让用户不必把同一个供应商配两遍）：
          · base_url：vision.base_url -> brain.base_url
          · 密钥：    vision.api_key_env 指向的环境变量 -> brain.api_key_env 指向的
        两级都留空时视觉模型不可用，内容链会自动停在标题级 —— 这是默认状态。
        """
        import os as _os

        from .content import build_content_pipeline  # 局部导入，避免无谓的启动开销

        brain = dict(self.config.providers.get("brain", {}))
        vision_key_env = str(vision_cfg.get("api_key_env") or "").strip()
        if vision_key_env:
            api_key = _os.environ.get(vision_key_env, "").strip()
        else:
            api_key = self.config.api_key()
        return build_content_pipeline(
            config=vision_cfg,
            brain_section=brain,
            api_key=api_key,
        )

    def _wire_autonomy(self) -> None:
        """
        把所有会影响自主发言时机的事件接到调度器上。

        接线集中在一处，而不是让各模块各自去调调度器：调度器是这个系统的
        "注意力"，它的输入必须有唯一、清晰的来源。
        """
        # 用户说话了：重置交互冷却。同时把宠物从"正在犯困"里叫醒。
        self.bus.subscribe("user.text", self._scheduler_on_user_text)
        # 一轮对话（含自主发言）结束：说完要歇一会儿
        self.bus.subscribe("conversation.finished", self._scheduler_on_finished)
        # 屏幕观察（P4 起用）：记录新鲜度供意愿评分使用
        self.bus.subscribe("vision.observation", self._scheduler_on_observation)
        # 手动"戳一下"：立刻开口，不看意愿分
        self.bus.subscribe("user.poke", self._scheduler_on_poke)
        # 配置热更新：免打扰时段是字符串，必须重新解析
        self.bus.subscribe("config.updated", self._scheduler_on_config)
        # 配置热更新：语音引擎按配置重建（切本地/云端时必须真的换，否则是假开关）
        self.bus.subscribe("config.updated", self._on_config_voice_engines)
    async def _scheduler_on_user_text(self, event: Event) -> None:
        self.scheduler.mark_interaction()

    async def _on_config_voice_engines(self, event: Event) -> None:
        """
        配置热更新时**真的把语音引擎换掉**。

        ## 🔴 为什么需要它（2026-10-08 用户实测发现）

        用户在设置页把语音识别从「本地」切到「云端（硅基流动）」，日志显示
        配置热更新成功了：

            21:01:48  配置已热更新：{'providers.asr.provider': 'openai'}

        但 `build_voice_pipeline()` **只在 Gateway 构造时调过一次**，
        `VoiceSession.asr` 也只在 `__init__` 里赋值。于是：

            · 界面写着「云端」，实际**还在用本地 Whisper 识别**
            · 本地 `large-v3-turbo` 继续占 **~800MB 内存 + ~1.5-2GB 显存**
              （实测切换前后内存一动不动：801MB -> 801MB）

        **这是"假开关"**：界面和实际行为不一致，而且不报任何错 ——
        用户只会觉得"切了好像没什么变化"，然后怀疑是不是自己没保存。

        ## ⚠️ 指纹比对：不是每次 config.update 都要重建

        `config.update` 在**改任何设置**时都会发（活泼度、免打扰、思考模式…）。
        每次都重建的话，本地 Whisper 重建一次要 **4 秒**
        （实测 `20:57:51 -> 20:57:55`）—— 用户只是拖了个滑杆却要等 4 秒，
        期间还听不到。所以先用 `engine_signature()` 比指纹，**只在真的变了时**重建。

        ## ⚠️ 重建失败要降级，不能让语音瘫痪

        `build_voice_pipeline` 本身的设计原则就是"缺依赖/缺密钥一律降级 mock"，
        所以这里直接复用它。额外再裹一层 try —— 万一降级路径本身也抛，
        至少保住旧引擎继续能用。
        """
        signature = self.voice.engine_signature()
        if signature == self._voice_engine_signature:
            return  # 指纹没变（改的是别的东西）-> 不碰引擎
        log.info("语音引擎配置变了：%s -> 正在重建…", signature)
        previous = self._voice_engine_signature
        try:
            pipeline = build_voice_pipeline(self.config, self.config.api_key())
            self.voice.swap_engines(pipeline.asr, pipeline.tts)
            self._voice_engine_signature = signature
            log.info(
                "语音引擎已切换：asr=%s model=%s",
                signature[0],
                signature[1] or "-",
            )
        except Exception as exc:  # noqa: BLE001 —— 重建失败必须降级，不能让语音瘫痪
            self._voice_engine_signature = previous
            log.error("语音引擎重建失败，保留原引擎继续用：%s", exc)

    async def _scheduler_on_finished(self, event: Event) -> None:
        self.scheduler.mark_spoke()

    async def _scheduler_on_observation(self, event: Event) -> None:
        # 单机单调时钟与 VisionLoop 内部用的时钟必须一致，否则新鲜度会算错。
        self._last_observation_at = time.monotonic()

    async def _scheduler_on_config(self, event: Event) -> None:
        self.scheduler.reload()
        # 活泼度变了要同步进主观层（2026-10-17）。
        # ⚠️ 不同步的话：用户拖完滑杆，`activity.overall` 变了、调度器阈值也变了，
        # 但主观层还拿着旧的 vitality —— 注意力漂移的倾向不同步，
        # 表现是"活泼度调了，它该多想自己的事还是没变"。
        self.conversation.sync_vitality()
        # 免打扰由前端/托盘经 config.update 切换，这里同步到调度器。
        # 单一真相源是配置：前端不再自己维护一份 paused 状态。
        self.scheduler.set_muted(bool(self.config.autonomy.get("muted", False)))
        # 提供者配置（provider / base_url / model / 密钥）变了就重建大脑。
        #
        # 为什么需要这条：早先 config.update 只刷新调度器，改 provider 之后
        # **不会生效** —— 用户在设置界面把 mock 切成真 API，界面显示成功，
        # 宠物却还在说假话。这种"改了没反应"极难排查，因为它没有任何报错。
        # 重建 brain 是安全的：它是无状态的（对话历史在 Conversation 里）。
        self._maybe_rebuild_brain()
        # 看屏幕的开关也必须在这里同步。采集器是构造时建好的，只改配置对象
        # 不会让它停下来 —— 那样界面上的开关就是假的（用户以为关掉了，其实还在截屏）。
        self.vision.set_watching(bool(self.config.providers.get("vision", {}).get("enabled", False)))

    def _maybe_rebuild_brain(self) -> None:
        """提供者配置有变化时重建 brain，并让记忆/对话都换到新实例。"""
        wanted = {
            key: self.config.providers.get("brain", {}).get(key)
            for key in ("provider", "base_url", "model", "api_key_env", "timeout_s")
        }
        if wanted == self._brain_signature:
            return
        previous = self._brain_signature.get("provider")
        try:
            rebuilt = build_brain(self.config, self._build_emotion())
        except Exception as exc:  # noqa: BLE001 —— 配置写坏了也要能继续跑
            log.error("按新配置重建大脑失败：%s（沿用原提供者）", exc)
            return

        self.brain = rebuilt
        self._brain_signature = wanted
        self.conversation.brain = rebuilt
        # 提炼器缓存在 Conversation 上（见 `_extract_in_background`），
        # 它持有 brain 引用，换了模型必须同步过去，否则提炼还在用旧模型。
        extractor = getattr(self.conversation, "_fact_extractor", None)
        if extractor is not None:
            extractor.brain = rebuilt
        log.info(
            "大脑提供者已切换：%s -> %s（model=%s）",
            previous,
            rebuilt.name,
            getattr(rebuilt, "model", "-"),
        )







    async def _scheduler_on_poke(self, event: Event) -> None:
        """
        手动点一下宠物 = **纯互动，不让它开口**（2026-10-07 用户要求）。

        ## 用户原话

        「现在主动点击**不要发送空内容、只是点击而已**」

        ## 为什么原来会说话

        原来这里发一条 `autonomy.speak` 让宠物立刻开口（"跳过意愿与冷却"）。
        那意味着**每点一下 = 一次真实的 LLM 调用**。而"点击"这个动作本身
        并不携带任何用户输入 —— 于是那条请求的 `user_text` 是 `None`
        （自主发言），接口侧直接以 `content should be a string or a list` 拒掉，
        用户看到的是一条报错气泡，而且上一条气泡被清空。

        ⚠️ 所以这不是"把报错盖掉"，而是**那个动作本来就不该产生一次生成**：
        点击是互动，不是发言。

        ## 改成了什么

        只做两件事，**都不花钱、也不产生气泡**：
          · `mark_interaction()` —— 记一次互动，让自主说话的计时器重新计
            （用户刚摸过它，不该马上又来一句）
          · 立绘的点击反应动画由**前端自己播**（见 `app.js` 的 `mouseup`）

        免打扰期间仍然什么都不做（与原来一致）。
        """
        blocked = self.scheduler.suppression_reason(include_disabled=False)
        if blocked is not None:
            log.info("手动点击被抑制门拦下：%s", blocked)
            return
        # 记一次互动（不 mark_spoke —— 它没有"说过话"，只是被摸了一下）
        self.scheduler.mark_interaction()
        log.info("收到手动点击：只做互动反馈，不让它开口")

    def _observation_age(self) -> Optional[float]:
        return self.vision.observation_age_s()

    async def _on_tier_decayed(self, event: "Event") -> None:
        """
        档位到期回落 -> 通知主进程（2026-10-17 用户要求）。

        主进程收到后把 `activity` 改成回落值：菜单下次打开就显示「安静」，
        而且**不写 persona.yaml**（活泼度本来就是运行时状态）。

        ⚠️ 这里不能自己发 `config.update` 回去 —— 方向反了，
        会变成"大脑改配置、主进程只是收件人"，两边状态谁准就说不清了。
        """
        # ⚠️ 第一个参数是 Event，数据在 .payload 里 —— 直接当成 dict
        # 用会 AttributeError，而外面包着 try/except，于是"广播失败"被吞掉，
        # 表现就是"回落了但菜单没变"。就是这么被抓出来的。
        info = dict(event.payload or {})
        try:
            payload = {
                "type": "activity.tier",
                "from": info.get("from"),
                "fromName": info.get("fromName"),
                "to": info.get("to"),
                "toName": info.get("toName"),
                "overall": info.get("overall"),
                "seconds": info.get("seconds"),
                "ts": int(time.time() * 1000),
            }
            await self.broadcast(payload)
            log.info(
                "活泼度：已通知主进程回落（%s -> %s，overall=%.2f）",
                info.get("fromName"),
                info.get("toName"),
                float(info.get("overall") or 0.0),
            )
        except Exception:  # noqa: BLE001 —— 广播失败不能让调度循环死
            log.exception("活泼度：回落广播失败（已忽略）")

    async def _on_autonomy_speak(self, event: Event) -> None:
        payload = dict(event.payload or {})
        # 把"刚看到什么"带进提示词。没有它，宠物就只会凭空闲瞎聊 ——
        # 那正是"看屏幕主动说话"这个需求的核心部分。
        observation = self.vision.last_observation
        if observation is not None and observation.age_s(time.monotonic()) < 180:
            hint = self._observation_hint(observation)
            if hint:
                payload["context_hint"] = hint

        # 「截图说话」优先于「搭话」（2026-10-18 用户要求）：
        #   ① 两者相隔不到 1 分钟时，先让截图说话；
        #   ② 截图说完后 1 分钟内不许再自主搭话。
        # 截图说的是"我刚看到你在干什么"，信息更新鲜；搭话随时都能补。
        now_mono = time.monotonic()
        since_screen = now_mono - self._last_screen_speak_at
        if self._last_screen_speak_at > 0 and since_screen < _SCREEN_SPEAK_HOLD_S:
            log.info(
                "自主发言：让给截图说话（刚 %.0f 秒前看过屏幕，%.0f 秒内不搭话）",
                since_screen,
                _SCREEN_SPEAK_HOLD_S - since_screen,
            )
            return
        if self.scheduler.screenshot_due():
            log.info("自主发言：截图也到点了，这一轮让给截图（截图优先）")
            return

        # 「自言自语」档：改念表里的句子（2026-10-17 用户要求）。
        #
        # 只在**这一档**生效：安静/无聊/好奇三档还是原来的自主发言，
        # 因为它们说的是"自己找事做 / 好奇你在干嘛"，不是念经。
        tier = self.config.activity_tier
        if tier.key == "monologue":
            # 注意力被拉回用户期间暂停念经（2026-10-18 用户要求）
            hold_left = self._monologue_hold_until - time.monotonic()
            if hold_left > 0:
                log.info(
                    "自言自语：注意力在用户身上（还剩约 %.0f 秒），这一轮不念", hold_left
                )
                return

            quote = self._pick_quote()
            if quote is not None and quotes_mod.choose_mode() == "copy":
                # 「一半可能直接按原本照抄输出，其他什么也不加」
                # -> **不过模型**。过一遍模型必然会加"嗯……""让我想想"之类，
                #    那就不是"什么也不加"了。
                log.info("自言自语：照抄一句 —— %s", quote.label)
                self.quotes.mark_used(quote.id)
                # 照抄也要带出处（2026-10-18 用户要求：破折号 + 作者/书名/朝代）
                await self.conversation.speak_verbatim(quotes_mod.display_text(quote))
                return
            if quote is not None:
                # 句子本体进**用户轮**（system 里会被记忆压过去，见 §129）
                payload["stage_direction"] = quotes_mod.direction_for(quote)
                payload["quote_line"] = quotes_mod.display_text(quote)
                base = quotes_mod.hint_for(quote, self.quotes)
                extra = str(payload.get("context_hint") or "").strip()
                payload["context_hint"] = f"{base} {extra}".strip()
                # 让日志能对上"这次念的是哪句"（用户听到好句子会问哪来的）
                payload["source"] = "monologue"
                log.info("自言自语：念心得 —— %s", quote.label)
                self.quotes.mark_used(quote.id)

        await self.conversation.speak_autonomously(payload)

    async def _on_user_text_for_monologue(self, event: Event) -> None:
        """
        「自言自语」状态下用户主动聊起来 -> 注意力拉回用户（2026-10-18 用户要求）。

        用户原话：「在自言自语状态和用户聊天时，3分钟内超过3轮对话将自动将注意力
        拉回用户身上，后续不和玩伴搭话3分钟，宠物注意力将继续回到自己身上继续念叨。」

        ## 为什么是"超过 3 轮"（>=4 条用户消息）而不是 "3 条"

        用户说的是"超过3轮"。一轮 = 用户一条 + 它一条。所以计数只看**用户消息**，
        「超过 3 轮」即窗口里第 4 条用户消息到达时触发。

        ## 为什么拉回之后要**停 3 分钟**念经

        注意力在用户身上时继续念叨，等于一边跟你说话一边背书 —— 那是精神分裂。
        所以 hold 期间 `_on_autonomy_speak` 直接跳过念经。
        """
        if self.config.activity_tier.key != "monologue":
            return
        try:
            info = dict(event.payload or {})
        except Exception:  # noqa: BLE001
            info = {}
        # 插件/测试可能没带文本，仍然算一轮
        now = time.monotonic()
        self._user_turns.append(now)
        # 只看最近 3 分钟
        self._user_turns = [t for t in self._user_turns if now - t <= 180.0]
        if len(self._user_turns) > 3:
            self._monologue_hold_until = now + 180.0
            self._user_turns.clear()
            try:
                # 主观层在 **Conversation** 上，不是 Gateway（2026-10-18 修：
                # 写成 `self.subjectivity` 时 `check-self-refs` 报"本类里没有定义"，
                # 而外面还套着 try/except，运行时会被静默吞掉 —— 两边都改了：
                # 句柄用 `self.conversation.subjectivity`，异常改成**打日志**。）
                self.conversation.subjectivity.on_user_message(str(info.get("text") or ""))
            except Exception:  # noqa: BLE001
                log.warning("自言自语：注意力拉回用户时出错（已忽略）", exc_info=True)
            log.info(
                "自言自语：3 分钟内聊了 %d 轮，注意力拉回用户；暂停念经 3 分钟",
                len(self._user_turns) + 3,
            )

    def monologue_hold_expired(self) -> bool:
        """hold 到期且最近 3 分钟没被搭话 -> 可以回自己身上（由 tick 循环调用）。"""
        if self._monologue_hold_until <= 0.0:
            return False
        now = time.monotonic()
        if now < self._monologue_hold_until:
            return False
        if self._user_turns and now - self._user_turns[-1] < 180.0:
            # 还在聊 —— 续上
            self._monologue_hold_until = now + 180.0
            return False
        self._monologue_hold_until = 0.0
        try:
            # 同上：主观层在 Conversation 上
            self.conversation.subjectivity.on_self_hit(["自言自语"])
        except Exception:  # noqa: BLE001
            log.warning("自言自语：注意力回到自己时出错（已忽略）", exc_info=True)
        log.info("自言自语：3 分钟没搭话，注意力回到自己身上，继续念叨")
        return True

    def _log_capability_report(self, config: Any, pipeline: Any) -> None:
        """启动时报告三处能力的运行位置（云 / 本地），以及会不会加载大模型。

        ## 为什么值得专门打一行日志

        用户的核心诉求是"选了云端就别带着大模型跑启动"。这件事**只看配置看不出来**：
        `asr.provider=cloud` 也可能因缺依赖降级成 mock，或某处提前 import 了本地实现。
        日志打出来才是事实（教训：日志和事实不符比没日志更坏）。
        """
        try:
            profile = config.brain_profile()
            brain_base = str(profile.get("base_url") or "")
            brain_where = (
                "本地"
                if ("127.0.0.1" in brain_base or "localhost" in brain_base)
                else "云端"
            )
            asr_note = str(getattr(pipeline, "asr_note", "") or "")
            tts_note = str(getattr(pipeline, "tts_note", "") or "")
            asr_where = (
                "本地" if "本地" in asr_note else ("云端" if "云端" in asr_note else "mock")
            )
            tts_where = (
                "本地"
                if "本地" in tts_note
                else ("云端" if ("云端" in tts_note or "edge" in tts_note.lower()) else "mock")
            )
            heavy = asr_where == "本地"
            log.info(
                "能力报告：大脑=%s（%s） 语音识别=%s 语音合成=%s 大模型=%s",
                brain_where,
                str(profile.get("model") or "?"),
                asr_where,
                tts_where,
                "会加载（本地 ASR）" if heavy else "不加载（纯云端，启动更快）",
            )
        except Exception:  # noqa: BLE001
            log.warning("能力报告生成失败（不影响运行）", exc_info=True)

    def _pick_quote(self) -> "quotes_mod.Quote | None":
        """
        从表里挑一句（回避最近念过的 8 句）。

        表空了返回 None —— 调用方会退回普通自主发言，而不是让这一档"没词儿"变哑巴。
        """
        if not self.quotes.count():
            return None
        quote = self.quotes.pick(recent=self._recent_quotes)
        if quote is None:
            return None
        self._recent_quotes.append(quote.text)
        # 只留最近 8 句：表里 20 多句，8 句的窗口足够避免"连着撞同一句"，
        # 又不会让可挑选的池子小到几乎总是重复。
        if len(self._recent_quotes) > 8:
            del self._recent_quotes[:-8]
        return quote

    @staticmethod
    def _observation_hint(observation: Any) -> str:
        """
        把一帧观察翻译成提示词里的一段话。

        分两种情况，因为信息的可信度不同：
          · 只有窗口标题   —— 只给**线索**，让模型自己组织语言。把标题当结论
            喂给模型容易让它过度演绎（标题常是"未命名 - 编辑器"这种废话）；
          · 有画面描述（视觉模型给的）—— 那是真的看到了内容，可以直接引用，
            但仍然约束它"不要补充描述里没有的细节"。
        """
        source = str(getattr(observation, "source", ""))
        content = str(getattr(observation, "content", "") or "")
        titles = [t for t in (getattr(observation, "titles", ()) or ()) if t.strip()]

        if source.endswith("vlm") and content:
            # 视觉模型看到的是整屏内容，比标题可靠得多
            return (
                f"你刚看了一眼用户的屏幕，看到的是：{content}"
                "如果想评论就围绕这个说；不要补充这段描述里没有的细节。"
                "一到两句，别说教。"
            )

        if titles:
            head = "、".join(f"「{t}」" for t in titles[:3])
            return (
                f"你刚瞥了一眼屏幕，当前最前面的窗口是：{head}。"
                "如果你想评论，就围绕这个说；如果没什么可说的，也可以只是打个招呼或说说时间。"
                "不要假装看到了标题以外的具体内容。"
            )
        return ""

    # ------------------------------------------------------------------ 下行

    async def _on_bus_event(self, event: Event) -> None:
        wire_type = _BUS_TO_WIRE.get(event.name)
        if wire_type is None:
            return
        payload = event.payload
        if not isinstance(payload, dict):
            payload = {"message": str(payload)}
        if payload.get("type") != wire_type:
            payload = {"type": wire_type, **{k: v for k, v in payload.items() if k != "type"}}
        payload.setdefault("ts", int(time.time() * 1000))
        await self.broadcast(payload)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        if not self.server.clients:
            return
        text = encode(payload)
        await self.server.broadcast(text)

    async def send_to(self, client: WebSocketClient, payload: dict[str, Any]) -> None:
        await client.send_text(encode(payload))

    async def _send_voice_audio(self, payload: bytes, fmt: str, text: str) -> None:
        """
        把合成好的语音发给前端播放。

        音频本体走**二进制帧**而不是塞进 JSON：MP3 动辄几十上百 KB，base64 之后
        还要再涨三分之一，而且在 Python 侧多做一次编解码纯属浪费。

        顺序很重要：先发带 `index` 的 JSON 元信息，再发对应的二进制帧。
        前端据此把两者配对 —— 一个 WebSocket 上二进制与文本是两条通道，
        没有这个序号就只能靠"第几个"去猜。
        """
        if not self.server.clients:
            return
        self._speech_seq += 1
        index = self._speech_seq
        log.debug("发送语音给前端播放：%s 格式 %d 字节（序号 %d）", fmt, len(payload), index)
        await self.broadcast(
            msg(
                "voice.audio",
                index=index,
                format=fmt,
                bytes=len(payload),
                text=text[:200],
            )
        )
        for client in list(self.server.clients):
            try:
                await client.send_binary(payload)
            except Exception:  # noqa: BLE001
                log.debug("语音发送失败，丢弃该连接")

    async def _on_client_binary(self, client: WebSocketClient, payload: bytes) -> None:
        """前端推来的麦克风 PCM。只入队，不做任何计算 —— 处理在语音会话里。"""
        self.audio_input.push(payload)

    # ------------------------------------------------------------------ 连接

    async def _on_client_connect(self, client: WebSocketClient) -> None:
        """客户端连上时只做登记，**不发 `ready`**。

        ⚠️ 这里原来直发了一条 `ready`，而 `_dispatch` 收到 `hello` 时**又发一条** ——
        前端于是每条连接都收到**两条完全相同的 `ready`**（各 3.3 KB）。

        实测确认（探针连一次、发一个 hello）：

            ['ready', 'pet.emotion', 'ready']
            #1 len=3385   #3 len=3385   ← 逐字节一致

        两条路径里，`hello -> ready` 才是**协议规定的握手**（前端必须发 `hello`），
        所以留着它、去掉这里的直发。去掉后 `pet.emotion` 仍然照发，
        它不在 `ready` 里，前端需要它来初始化情绪显示。
        """
        log.info("前端已连接：%s", client.peer)
        await self.send_to(
            client, msg(
                "pet.emotion",
                state=self.conversation.emotion.as_dict(),
                attention=self.conversation.attention_payload(),
            )
        )
        await self.bus.publish("client.connected", {"peer": str(client.peer)})

    def _ready_payload(self) -> dict[str, Any]:
        """ready 消息：前端拿它初始化滑杆、状态显示、语音开关与隐私开关。"""
        vision_cfg = self.config.providers.get("vision", {})
        # 配置快照（snapshot() 已剔除 api_key_env，密钥绝不出现在这条消息里）。
        # 设置窗口靠它显示"当前生效的 base_url / model" —— 没有它就只能显示
        # 界面里刚填的值，而用户分不清"填了"和"生效了"。
        config_snapshot = self.config.snapshot()
        return msg(
            "ready",
            protocolVersion=PROTOCOL_VERSION,
            brain=self.conversation.brain.name,
            persona=self.config.pet_name,
            activity=dict(self.config.activity),
            autonomy=self.scheduler.snapshot(),
            voice=self.voice.stats(),
            memory=self.memory.stats() if self.memory is not None else {"enabled": False},
            config=config_snapshot,
            # 看屏幕的**生效值**（不是配置里的原始值）。托盘与设置界面的勾选状态
            # 以它为准，这样"界面显示开着"和"真的在截屏"永远是同一件事。
            vision={
                "enabled": bool(vision_cfg.get("enabled", False)),
                "provider": str(vision_cfg.get("provider") or "none"),
                "watching": bool(getattr(self.vision, "watching", False)),
            },
        )

    async def _on_client_disconnect(self, client: WebSocketClient) -> None:
        log.info("前端断开：%s", client.peer)
        await self.bus.publish("client.disconnected", {"peer": str(client.peer)})

    # ------------------------------------------------------------------ 上行

    async def _on_client_text(self, client: WebSocketClient, text: str) -> None:
        try:
            incoming = parse_incoming(text)
        except ProtocolError as exc:
            log.warning("丢弃不合法的上行消息：%s", exc)
            await self.send_to(
                client, msg("system.error", level="warn", message=str(exc), where="protocol")
            )
            return
        try:
            await self._dispatch(client, incoming)
        except Exception as exc:  # noqa: BLE001 —— 一条消息出错不能断连接
            log.exception("处理上行消息 %s 时异常", incoming.type)
            await self.send_to(
                client,
                msg("system.error", level="error", message=f"{type(exc).__name__}: {exc}", where=incoming.type),
            )

    async def _dispatch(self, client: WebSocketClient, incoming: Incoming) -> None:
        if incoming.type == "hello":
            await self.send_to(client, self._ready_payload())
            return

        if incoming.type == "user.text":
            # ⚠️ 到达日志（2026-10-09）。之前这条链上**一处日志都没有**，
            # 于是"消息有没有到大脑"只能靠猜。用户消息是最重要的事件，必须可见。
            log.info("收到用户消息（%d 字）：%s", len(incoming.text), incoming.text[:40])
            await self.bus.publish("user.text", {"text": incoming.text, "requestId": incoming.request_id})
            return

        if incoming.type == "user.interrupt":
            await self.bus.publish("user.interrupt", {"requestId": incoming.request_id})
            return

        if incoming.type == "vision.snap":
            """
            「截图对话」（2026-10-17 用户要求）。

            用户原话：「点击按钮或者按下快捷键，屏幕将自动截图并发给ai，
            接受图片后，玩伴的注意力会直接被拉到好奇状态，玩伴会顺着图片
            和用户聊天。」

            ## 为什么放在 sidecar 而不是 Electron 里

            截图本身两边都能做，但"看懂 + 让它说话"这一整套（视觉模型、
            主观层、提示词、TTS、气泡）都在 Python 侧。放在这里就只多一次
            进程内调用；放在主进程则要把结果再送回来，多一个来回。

            ## ⚠️ 回执用 `requestId`，字段名必须对齐

            主进程用 `requestFromBrain()` 等这条回执（它按 `requestId` 匹配）。
            历史上这里写成 `reqId` 导致"永远超时"——**不报错，只是没反应**。
            """
            req_id = str(getattr(incoming, "request_id", "") or "")
            log.info("收到截图对话请求（requestId=%s）", req_id or "（空）")
            await self._snap_and_chat(req_id)
            return

        if incoming.type == "user.poke":
            # 手动戳一下：跳过意愿与冷却，立刻开口
            await self.bus.publish("user.poke", {"requestId": incoming.request_id})
            return

        if incoming.type == "memory.clear":
            """
            清空长期记忆。这是隐私上的**底线能力**：用户必须能一句话让宠物忘掉
            所有听过的事，而且必须真的从磁盘删掉，不是只在内存里清。
            """
            removed = 0
            if self.memory is not None:
                removed = self.memory.clear()
            log.info("用户清空了长期记忆（%d 条）", removed)
            await self.send_to(
                client,
                msg(
                    "memory.cleared",
                    removed=removed,
                    memory=self.memory.stats() if self.memory is not None else {"enabled": False},
                ),
            )
            return

        if incoming.type == "memory.backup":
            """
            手动备份记忆库，回执给请求方。

            为什么这个操作**必须**在 Python 侧做：只有这里持有打开的数据库连接，
            能用 `VACUUM INTO` 拿到**一致快照**。Electron 侧直接复制文件的话，
            WAL 模式下可能只复制到主库文件（新写入还在 -wal 里），备份就是残缺的。

            回执把 `reqId` 原样带回 —— 主进程靠它把结果交给等待的那个调用
            （见 `index.ts` 的 `requestFromBrain`）。
            """
            # ⚠️ 字段名必须是 `requestId`（协议惯例，Python 侧从它解析出 `request_id`）。
            # 曾经写成 `reqId`，两边对不上 -> 回执带空串 -> 主进程匹配不到等待者 -> 超时。
            req_id = str(getattr(incoming, "request_id", "") or "")
            result_ok = False
            message = "记忆库未启用"
            path = ""
            staged = False
            target_dir = ""
            if self.memory is not None:
                try:
                    res = self.memory.backup_now()
                    result_ok, message, path = res.ok, res.message, res.path
                    # 两段式：Python 可能没权限写用户选的文件夹，只生成快照；
                    # 搬运交给主进程（它有权限）。见 `store.backup_to` 的说明。
                    staged, target_dir = res.staged, res.target_dir
                except Exception as exc:  # noqa: BLE001 —— 备份失败不能影响运行
                    log.exception("手动备份异常")
                    message = f"备份异常：{exc}"
            log.info("手动备份：%s（%s）", "成功" if result_ok else "失败", message)
            await self.send_to(
                client,
                msg(
                    "memory.backup.done",
                    ok=result_ok,
                    message=message,
                    path=path,
                    staged=staged,
                    target=target_dir,
                    auto=False,
                    requestId=req_id,
                ),
            )
            return

        if incoming.type == "modules.query" or incoming.type == "modules.set":
            """模块开关：查询状态 / 试切一次（落盘由主进程做）。

            ## 为什么回执也带 requestId

            主进程用 `requestFromBrain` 等这个 id（与 memory.query 同一套）。
            字段名必须是 `requestId` —— 曾经写成 `reqId`，两边对不上，每次都超时。

            ## 热切换的边界

            见 `_apply_module_change()`：能停的循环真的停，不能热的（voice）如实报
            `restartRequired`，界面照实显示"重启后生效"，不许假装切了。
            """
            req_id = str(getattr(incoming, "request_id", "") or "")
            changed: dict[str, bool] = {}
            restart_required: list[str] = []

            if incoming.type == "modules.set":
                name = str(incoming.raw.get("name") or "").strip()
                enabled = bool(incoming.raw.get("enabled"))
                if not name:
                    await self.send_to(
                        client,
                        msg("modules.state", ok=False, message="缺少 name",
                            requestId=req_id, modules=self.modules.state()),
                    )
                    return
                try:
                    changed = await self._apply_module_change(name, enabled)
                    self.modules.set_enabled(name, enabled)
                    if name == "voice":
                        restart_required.append("voice")
                    log.info(
                        "模块切换：%s -> %s（热切 %s，需重启 %s）",
                        name,
                        "开" if enabled else "关",
                        "、".join(changed) or "无",
                        "、".join(restart_required) or "无",
                    )
                except KeyError:
                    await self.send_to(
                        client,
                        msg("modules.state", ok=False, message=f"没有这个模块：{name}",
                            requestId=req_id, modules=self.modules.state()),
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    log.warning("模块切换失败：%s", exc, exc_info=True)
                    await self.send_to(
                        client,
                        msg("modules.state", ok=False, message=str(exc)[:200],
                            requestId=req_id, modules=self.modules.state()),
                    )
                    return

            await self.send_to(
                client,
                msg(
                    "modules.state",
                    ok=True,
                    requestId=req_id,
                    modules=self.modules.state(),
                    changed=changed,
                    restartRequired=restart_required,
                ),
            )
            return

        if incoming.type == "memory.query":
            """
            记忆管理界面的一站式入口（设置页的「记忆」「珍藏」标签）。

            ## 为什么一个类型带 action，而不是十个消息类型

            记忆管理会持续加操作。每个操作一个消息类型就要同步改三处
            （Python 白名单、TS 联合类型、protocol.json），加得越多越容易漏，
            而漏了的表现是"点了没反应"——很难查。

            集中在这里只有一个分支要维护。**新增操作别忘了同时更新
            `packages/protocol/src/index.ts` 里的 `MemoryQueryAction` 联合类型。**

            ## 回执一律带 `requestId`

            主进程用 `requestFromBrain` 等这个 id（见 index.ts）。字段名必须是
            `requestId` —— 曾经写成 `reqId`，两边对不上导致每次都超时。
            """
            req_id = str(getattr(incoming, "request_id", "") or "")
            action = str(incoming.raw.get("action") or "")
            payload = incoming.raw.get("payload")
            args = payload if isinstance(payload, dict) else {}

            async def reply(ok: bool, **extra: Any) -> None:
                await self.send_to(
                    client,
                    msg("memory.result", ok=ok, action=action, requestId=req_id, **extra),
                )

            if self.memory is None:
                await reply(False, message="记忆库未启用（memory.enabled = false）")
                return

            try:
                if action == "raw_image":
                    # 原文记忆里「查看图片」按钮点下去走这里。
                    # 单独一个 action 而不是塞进 list_raw：一页 50 条各带一张截图
                    # 会让列表接口变成几十兆（见 store.recent_turns_page 的注释）。
                    raw_id = int(args.get("id") or 0)
                    got = self.memory.get_raw_image(raw_id)
                    if not got:
                        await reply(False, message="这条没有图片")
                        return
                    data, mime = got
                    import base64 as _b64

                    await reply(True, mime=mime, data=_b64.b64encode(data).decode("ascii"))
                    return

                if action == "list_raw":
                    rows = self.memory.list_raw(
                        limit=int(args.get("limit") or 50),
                        before_ts=args.get("beforeTs"),
                        # 时间范围（用户要求「加入时间搜索，方便通过时间查询」）。
                        # 前端传的是毫秒（JS 的 Date.getTime()），这里换成秒 ——
                        # 库里的 ts 是 `time.time()` 的秒。忘了换算会让范围查询
                        # 恒定落空（差 1000 倍），而表现只是"搜不到"。
                        since_ts=_ms_to_s(args.get("sinceMs")),
                        until_ts=_ms_to_s(args.get("untilMs")),
                        role=str(args["role"]) if args.get("role") else None,
                    )
                    await reply(True, items=rows, stats=self.memory.stats())
                elif action == "raw_context":
                    rows = self.memory.raw_context(
                        int(args.get("rawId") or 0),
                        before=int(args.get("before") or 0),
                        after=int(args.get("after") or 0),
                    )
                    await reply(True, items=rows)
                elif action == "search_raw":
                    rows = self.memory.search_raw(
                        str(args.get("query") or ""),
                        limit=int(args.get("limit") or 50),
                        # 只看"谁说的" —— 固定注入那个页面专门找宠物的口头禅
                        # 或用户的习惯说法
                        role=str(args["role"]) if args.get("role") else None,
                    )
                    await reply(True, items=rows, stats=self.memory.stats())
                elif action == "list_cherished":
                    await reply(True, items=self.memory.list_cherished(), stats=self.memory.stats())
                elif action == "cherish":
                    res = self.memory.cherish(
                        raw_id=args.get("rawId"),
                        # 勾选多条（用户要求：「可以勾选一条或多条对话进去」）
                        raw_ids=args.get("rawIds") if isinstance(args.get("rawIds"), list) else None,
                        text=str(args.get("text") or ""),
                        title=str(args.get("title") or ""),
                        pinned=bool(args.get("pinned", True)),
                    )
                    await reply(
                        bool(res.get("ok")),
                        message=str(res.get("message") or ""),
                        newId=res.get("id"),
                        items=self.memory.list_cherished(),
                    )
                elif action == "cherished_detail":
                    res = self.memory.cherished_detail(int(args.get("id") or 0))
                    await reply(
                        bool(res.get("ok")),
                        message=str(res.get("message") or ""),
                        entry=res.get("entry"),
                        turns=res.get("turns"),
                    )
                elif action == "expand_cherished":
                    res = self.memory.expand_cherished(
                        int(args.get("id") or 0),
                        int(args.get("rawId") or 0),
                        before=int(args.get("before") or 0),
                        after=int(args.get("after") or 0),
                    )
                    await reply(
                        bool(res.get("ok")),
                        message=str(res.get("message") or ""),
                        items=self.memory.list_cherished(),
                    )
                elif action == "remove_cherished_turn":
                    res = self.memory.remove_cherished_turn(int(args.get("turnId") or 0))
                    await reply(bool(res.get("ok")), message=str(res.get("message") or ""))
                elif action == "update_cherished":
                    res = self.memory.update_cherished(
                        int(args.get("id") or 0),
                        title=args.get("title"),
                        text=args.get("text"),
                        pinned=args.get("pinned"),
                    )
                    await reply(
                        bool(res.get("ok")),
                        message=str(res.get("message") or ""),
                        items=self.memory.list_cherished(),
                    )
                elif action == "remove_cherished":
                    res = self.memory.remove_cherished(int(args.get("id") or 0))
                    await reply(
                        bool(res.get("ok")),
                        message=str(res.get("message") or ""),
                        items=self.memory.list_cherished(),
                    )
                elif action == "list_memory":
                    level = args.get("level")
                    await reply(
                        True,
                        items=self.memory.list_memory(str(level) if level else None),
                        stats=self.memory.stats(),
                    )
                elif action == "remove_memory":
                    res = self.memory.remove_memory(int(args.get("id") or 0))
                    await reply(
                        bool(res.get("ok")),
                        message=str(res.get("message") or ""),
                        items=self.memory.list_memory(),
                    )
                elif action == "remove_memories":
                    """
                    批量**真删**检索层条目（用户 2026-10-07 要求）。

                    ## ⚠️ 与「记忆压缩」的语义区别（很重要）

                    · **压缩**是"不删只标记"（`merged_into`）—— 可一键还原，
                      因为用户要求"防止重要信息被重复度选中后丢失"；
                    · **删除**是**真删**：`memory` 行、`memory_vec` 向量、
                      `memory_vec0` KNN 索引三处一起清掉，**不可还原**。

                    两者并存是有意的：压缩治"冗余"，删除治"错的东西"。
                    界面上必须让用户分得清（删除按钮有二次确认）。

                    逐条走 `db.remove_memory`（产品代码）而不是一条 SQL 批删 ——
                    它负责同时清向量与 KNN 索引。少了任何一处都会留下
                    "幽灵条目"：正文取不回来，但排名占着位置。
                    """
                    ids = args.get("ids")
                    if not isinstance(ids, list):
                        await reply(False, message="ids 必须是数组")
                    else:
                        res = self.memory.remove_memories(ids)
                        await reply(
                            bool(res.get("ok")),
                            message=str(res.get("message") or ""),
                            removed=int(res.get("removed") or 0),
                            items=self.memory.list_memory(),
                        )
                elif action == "set_memory_level":
                    res = self.memory.set_memory_level(
                        int(args.get("id") or 0), str(args.get("level") or "recent")
                    )
                    await reply(
                        bool(res.get("ok")),
                        message=str(res.get("message") or ""),
                        items=self.memory.list_memory(),
                    )
                elif action == "fixed_list":
                    await reply(True, items=self.memory.fixed_list(), stats=self.memory.stats())
                elif action == "add_fixed":
                    res = self.memory.add_fixed(
                        str(args.get("text") or ""),
                        kind=str(args.get("kind") or "phrase"),
                    )
                    await reply(
                        bool(res.get("ok")),
                        message=str(res.get("message") or ""),
                        newId=res.get("id"),
                        items=self.memory.fixed_list(),
                    )
                elif action == "remove_fixed":
                    res = self.memory.remove_fixed(int(args.get("id") or 0))
                    await reply(
                        bool(res.get("ok")),
                        message=str(res.get("message") or ""),
                        items=self.memory.fixed_list(),
                    )
                elif action == "stats":
                    await reply(True, stats=self.memory.stats())
                # ---- 记忆压缩（schema v3 · 2026-10-05）----
                #
                # 用户指定的三个能力：手动审核 / 相似度自动 / AI 合并。
                # 这里只暴露**原子操作**，策略组合由界面决定 ——
                # 那样加新策略不用改协议。
                elif action == "compress_scan":
                    raw_th = args.get("threshold")
                    th = None
                    if raw_th is not None:
                        try:
                            th = float(raw_th)
                        except (TypeError, ValueError):
                            th = None
                    res = self.memory.compress_scan(th)
                    await reply(
                        bool(res.get("ok")),
                        message=str(res.get("message") or ""),
                        **{k: v for k, v in res.items() if k not in ("ok", "message")},
                    )
                elif action == "compress_apply":
                    groups = args.get("groups") or []
                    if not isinstance(groups, list):
                        await reply(False, message="groups 必须是数组")
                    else:
                        res = self.memory.compress_apply(
                            groups,
                            ai_merge=bool(args.get("ai_merge")),
                            prompt=str(args.get("prompt") or ""),
                        )
                        await reply(
                            bool(res.get("ok")),
                            message=str(res.get("message") or ""),
                            **{k: v for k, v in res.items() if k not in ("ok", "message")},
                        )
                elif action == "compress_undo":
                    ids = args.get("ids")
                    if ids is not None and not isinstance(ids, list):
                        await reply(False, message="ids 必须是数组或省略")
                    else:
                        res = self.memory.compress_undo(
                            [int(i) for i in ids] if ids else None
                        )
                        await reply(
                            bool(res.get("ok")),
                            message=str(res.get("message") or ""),
                            **{k: v for k, v in res.items() if k not in ("ok", "message")},
                        )
                elif action == "compress_list":
                    res = self.memory.compress_list()
                    await reply(
                        bool(res.get("ok")),
                        message=str(res.get("message") or ""),
                        **{k: v for k, v in res.items() if k not in ("ok", "message")},
                    )
                elif action == "compress_config":
                    await reply(True, config=self.memory.compress_config())
                elif action == "estimate_ai_cost":
                    # 用户要求：AI 压缩前给出**token 数与费用**，让用户自己对比
                    groups = args.get("groups") or []
                    if not isinstance(groups, list):
                        await reply(False, message="groups 必须是数组")
                    else:
                        est = self.memory.estimate_ai_cost(
                            groups, str(args.get("prompt") or "")
                        )
                        await reply(
                            bool(est.get("ok")),
                            message=str(est.get("message") or ""),
                            **{k: v for k, v in est.items() if k not in ("ok", "message")},
                        )
                elif action == "list_active":
                    # 检索层列表（活跃条目）—— 供「检索层」tab 显示
                    items = self.memory.list_memory(level=args.get("level"))
                    await reply(True, items=items, active=self.memory.stats().get("active", 0))
                else:
                    await reply(False, message=f"未知操作：{action!r}")
            except Exception as exc:  # noqa: BLE001 —— 界面操作失败不该拖垮 sidecar
                log.exception("记忆管理操作失败：%s", action)
                await reply(False, message=f"{type(exc).__name__}: {exc}")
            return

        if incoming.type == "voice.control":
            # 前端开关麦克风。开的时候顺带把语音状态统计回报一次，
            # 让界面能显示"识别引擎是谁、能不能用"。
            enabled = bool(incoming.raw.get("enabled", False))
            await self.voice.set_enabled(enabled)
            await self.send_to(
                client,
                msg("voice.state", state=self.voice.state.value, enabled=enabled),
            )
            return

        if incoming.type == "voice.ptt":
            """
            按住说话（push-to-talk）：**松开就是说完**。

            ## 用户要求

            「设置隐私里面设置自定义按键，**按下按键发声，停下结束**」

            ## 为什么需要这条消息（而不是只靠静音判定）

            平时的断句靠 `Endpointer` 的静音判定（默认 500ms）。但按住说话的
            语义是"松开就说完了" —— 用户按住说两个字就松手时，句尾可能只有
            100ms 静音，**静音判定根本不会触发**，那句话会卡在缓冲里等下一次开口。

            所以松开要**主动收尾**（`VoiceSession.force_end()`）。

            `phase`：
              · `start` —— 打开**采集闸门**并通知"开始说了"。
                采集本身由前端的 `VoiceIO` 负责，麦克风一直是开的 ——
                **不在这里开关麦克风**：反复开关会付权限提示与 AudioContext
                创建的开销，而按住说话可能一秒按好几次。
              · `end`   —— 关掉采集闸门 + **主动收尾**并送去识别。
            """
            phase = str(incoming.raw.get("phase") or "").strip().lower()
            # 收到就记一条（2026-10-10 加）。
            #
            # ## 为什么这条必须有
            #
            # 修「没按按键也能听见」时，前端已经确认发了 `stop`
            # （`window.__captureGate` 显示 `shouldListen: false`），
            # 但日志里**一条闸门记录都没有** —— 说明链路断在这之前或这里。
            # 而这一段原来**完全没日志**，黑的路径根本没法排查。
            #
            # ⚠️ 用 INFO 不用 DEBUG：正常运行是 INFO 级，DEBUG 看不到 ——
            # 而"消息到底有没有到"正是排查时最需要的。
            log.info("按住说话：收到 phase=%r", phase)
            if phase == "end":
                # ⚠️ 顺序很重要：**先收尾、再关闸门**。
                #
                # `set_capture(False)` 内部也会 `force_end()` 兜底一次，
                # 但那一次是在"闸门关闭"的语义下做的。先在这里收尾，
                # 关闸门时就没有半句可补 —— 路径唯一，不会重复识别。
                await self.voice.force_end()
                await self.voice.set_capture(False)
            elif phase == "start":
                await self.voice.set_capture(True)
            else:
                # ⚠️ 提到 warning：无法识别的 phase 意味着**闸门不会被切换** ——
                # 那不是"小噪音"，是功能失效（2026-10-10 改）
                log.warning("按住说话：收到无法识别的 phase %r —— 闸门不会切换", phase)
            return

        if incoming.type == "tts.result":
            # 主进程合成完了（或失败了）—— 唤醒等它的那个 TTS 引擎。
            #
            # 为什么走协议而不是让 Python 自己起 PowerShell：见 voice.py 里
            # `WindowsSapiTtsEngine` 的说明（Python 起的 PowerShell 拿不到 SAPI 音色，
            # Node 起的可以；生产里 Electron 就是父进程）。
            #
            # ⚠️ 字段从 `incoming.raw` 取 —— `Incoming` 只解析了 type/text/patch，
            # `seq`/`ok`/`pcm` 都在 `raw` 里（它是完整的原字典）。
            raw = incoming.raw
            handled = self.voice.resolve_tts(
                int(raw.get("seq") or 0),
                bool(raw.get("ok")),
                str(raw.get("pcm") or ""),
                str(raw.get("message") or ""),
            )
            if not handled:
                log.debug(
                    "收到 tts.result（seq=%s）但当前 TTS 引擎不认领（可能刚热切换过）",
                    raw.get("seq"),
                )
            return

        if incoming.type == "web.browser.result":
            """
            主进程用**隐藏浏览器**读完了（或失败了）—— 唤醒等它的那次 `web_fetch`。

            ## 方向：和 `tts.result` **完全同构**（2026-10-13 加）

            ```
            web.browser.fetch    Python -> 主进程  「帮我用浏览器读这个网址」
            web.browser.result   主进程 -> Python  「读到了 / 读不了，原因是…」
            ```

            ⚠️ **为什么需要它**：起点那种 WAF 挑战（`x-waf-captcha-referer` +
            `probe.js`）纯 HTTP 过不去，但 Chromium 能过 —— 而这个项目
            本来就是 Electron，所以主进程开个隐藏窗口就行。

            ⚠️ **默认关闭**（`tools.browser_fallback`）：走浏览器等于
            "以用户的身份浏览"（带真实 UA、留 cookie），必须用户显式同意。

            ## ⚠️ 字段从 `incoming.raw` 取

            `Incoming` 只解析了 `type` / `text` / `patch` / `requestId`，
            其余（`ok` / `text` / `metas` / `reason`）都在 `raw` 里 ——
            和 `tts.result` 一样的坑。

            ## ⚠️ 没人认领是**正常情况**，不是错误

            超时之后才到的回执就没人认领（Python 那边已经从
            `_browser_pending` 里摘掉了）。那只该记 debug ——
            记 warning 会让日志里出现一堆看着像故障的噪音
            （这个项目在 `tts.result` 上已经踩过这个坑）。
            """
            raw = incoming.raw
            request_id = str(raw.get("requestId") or "")
            handled = web.resolve_browser_result(request_id, raw)
            if not handled:
                log.debug(
                    "收到 web.browser.result（requestId=%r）但没人认领（多半是超时后才到）",
                    request_id,
                )
            return

        if incoming.type == "voice.say":
            # 设置页的「试听」。
            #
            # 刻意走 `VoiceSession.speak()` —— 和宠物真实说话**同一个方法**
            # （合成 + 算口型包络 + 交前端播放）。这样"试听成功"就等于
            # "宠物真能出声"，而不是另测一条可能骗人的路。
            text = incoming.text or str(incoming.raw.get("text") or "")
            if text.strip():
                log.info("试听：读一句（%d 字）", len(text))
                # ⚠️ **必须 fire-and-forget，不能 await**（2026-10-09 实测的死锁）。
                #
                # `speak()` 内部要 `await` 主进程回的 `tts.result`，而那条回执
                # **要从这个读循环进来** —— 在这里 await 就把读循环自己堵死了：
                #
                #     主进程 650ms 就合成完了 -> 回执到了 socket
                #     读循环却卡在 speak() 的 await 上，读不到那条回执
                #     -> 40s 超时 -> 抛异常 -> 读循环才恢复 -> 才读到（但已无人等）
                #
                # 表现极具迷惑性：**两边日志都正常，就是慢 40 秒**。
                # 通式：**处理下行消息的回调里，不要 await 任何"需要后续下行消息
                # 才能完成"的操作** —— 那会把收消息的循环堵死。
                self._voice_say_task = asyncio.get_running_loop().create_task(
                    self.voice.speak(text, "preview")
                )
            return

        if incoming.type == "config.update":
            effective = self.config.apply_patch(incoming.patch)
            if effective:
                log.info("配置已热更新：%s", effective)
            await self.bus.publish("config.updated", effective)
            # 只回一条 ready：它已经带上了全部活泼度字段。
            # 之前在这里额外发了一条 pet.emotion，导致前端收到的消息顺序不确定
            # （自检里「热更新未回报：pet.emotion」就是这个冗余造成的）。
            await self.broadcast_config_ack(effective)
            return

    async def broadcast_config_ack(self, effective: dict[str, Any]) -> None:
        """把最新配置广播给所有已连接的前端。

        ⚠️ **每调用一次，前端就多收一条 `ready`**（3.3 KB）。
        实测前端刚连上就会收到**两条完全相同的 `ready`**：
        第一条是 `_on_client_connect` 直发的，第二条来自这里。
        怀疑上游有一条"连接后触发配置同步"的路径，但**没能定位到具体触发者** ——
        所以这里加一条 INFO 日志把调用现场记下来（`effective` 为空特别可疑：
        没人改配置，却广播了一次）。

        排查入口：日志里搜「配置广播」。
        """
        payload = self._ready_payload()
        count = len(self.server.clients)
        log.info(
            "配置广播：%d 个客户端，effective=%s",
            count,
            effective if effective else "（空 —— 没人改配置却广播了，值得查）",
        )
        for client in list(self.server.clients):
            await self.send_to(client, payload)

    # ------------------------------------------------------------------ 生命周期

    async def start(self, host: str = "127.0.0.1", port: int = 8931, port_scan: int = 20) -> int:
        bound = await self.server.start(host=host, port=port, port_scan=port_scan)
        self.start_vision()
        self.start_voice()
        self.start_autonomy()
        self.start_memory_sync()
        return bound

    def start_memory_sync(self) -> None:
        """
        记忆落盘。

        ## SQLite 之后这里**不需要**定时同步了

        老实现（JSONL）要把内存里的条目定期写回文件，所以有个 30 秒的循环。
        现在每次写入都直接进数据库并 `commit`（WAL 模式下这是个很快的操作），
        **没有"内存里没落盘"的窗口**，所以这个任务只保留为"启动时备份"的入口。

        留这个方法而不是删掉，是为了不动 `start()` 的调用结构 ——
        将来若要恢复"定时归档/清理旧轮次"，加在这里最自然。
        """
        if self.memory is None:
            return
        # 启动时自动备份（用户开了才做）。放在这里是因为它要在数据库打开之后、
        # 且只做一次 —— 正好是 `start()` 的时机。
        try:
            self.memory.backup_on_start()
        except Exception:  # noqa: BLE001 —— 备份失败绝不能影响启动
            log.exception("启动备份失败（已忽略）")

    def start_voice(self) -> None:
        """启动语音会话。麦克风由前端开启；这里只是常驻的处理循环。"""
        if self._voice_task is not None and not self._voice_task.done():
            return
        self._voice_task = asyncio.get_running_loop().create_task(
            self.voice.run_forever(), name="voice"
        )

    def start_vision(self) -> None:
        """启动看屏幕循环。采集器不可用时也照常启动——它每轮返回 None，开销可忽略。"""
        if self._vision_task is not None and not self._vision_task.done():
            return
        self._vision_task = asyncio.get_running_loop().create_task(
            self.vision.run_forever(), name="vision"
        )

    async def _apply_module_change(self, name: str, enabled: bool) -> dict[str, bool]:
        """把模块开关的**副作用**落地（能热切的才切，其余的交给重启）。

        返回 `{副作用: 是否真的执行了}`，让回执能如实说明"这件事我真做了"。
        """
        done: dict[str, bool] = {}

        if name == "autonomy":
            if enabled:
                self.start_autonomy()
                done["start_autonomy"] = True
            else:
                for attr in ("_autonomy_task", "_events_task", "_autosnap_task"):
                    task = getattr(self, attr, None)
                    if task is not None and not task.done():
                        task.cancel()
                        done[attr] = True
                log.info("自主行为模块关闭：已取消调度/事件/截图循环")
        elif name == "screen":
            try:
                self.vision.set_watching(bool(enabled) and bool(
                    (self.config.root.get("providers", {}).get("vision", {}) or {}).get("enabled", False)
                ))
                done["vision.set_watching"] = True
            except Exception:  # noqa: BLE001
                log.warning("切换视觉监听失败", exc_info=True)
            task = getattr(self, "_autosnap_task", None)
            if not enabled and task is not None and not task.done():
                task.cancel()
                done["_autosnap_task"] = True
            elif enabled and (task is None or task.done()):
                self._autosnap_task = asyncio.get_running_loop().create_task(
                    self._run_autosnap_forever(), name="autosnap"
                )
                done["_autosnap_task"] = True
        # search / monologue / voice / 地基模块：无即时副作用
        return done

    def start_autonomy(self) -> None:
        """启动自主调度循环。重复调用是安全的（已有任务则忽略）。"""
        if not self.modules.enabled("autonomy"):
            log.info("自主行为模块关掉了（modules.autonomy）：三个循环都不启动")
            return
        if not self.config.autonomy.get("enabled", True):
            log.info("自主行为已在配置中关闭，不启动调度循环")
            return
        # 观察新鲜度由 VisionLoop 提供，而不是从事件时间戳另算一份 ——
        # 单一来源，避免"事件到了但 loop 那边没更新"这种不一致。
        if self._autonomy_task is None or self._autonomy_task.done():
            self._autonomy_task = asyncio.get_running_loop().create_task(
                self.scheduler.run_forever(self.vision.observation_age_s), name="autonomy"
            )
        # 随机事件循环（2026-10-17，用户列为重点功能）。
        #
        # 与自主发言**分开**两个任务：一个管"说不说话"，一个管"做不做事"。
        # 混在一起的话，"它去搜哲学"这种安静的事会被"要不要开口"的阈值挡掉 ——
        # 而用户要的恰恰是"它有自己的事要做"。
        if self._events_task is None or self._events_task.done():
            self._events_task = asyncio.get_running_loop().create_task(
                self._run_events_forever(), name="events"
            )

        # 自主截图循环（2026-10-18）：另一个任务，因为它管的是"看不看屏幕"，
        # 和"要不要说话"的节奏完全不同（10~30 分钟 vs 2~20 分钟）。
        if not self.modules.enabled("screen"):
            log.info("看屏幕模块关掉了（modules.screen）：不启动自主截图循环")
        elif self._autosnap_task is None or self._autosnap_task.done():
            self._autosnap_task = asyncio.get_running_loop().create_task(
                self._run_autosnap_forever(), name="autosnap"
            )

    async def _run_autosnap_forever(self) -> None:
        """
        「自己偶尔截一帧看一眼」（2026-10-18 用户给的数字）。

        节奏由 `Tier.screenshot_interval_s` 决定（好奇 30 分钟 / 自言自语 10 分钟），
        且**必须**用户已开「允许偶尔截图」—— 档位永远不能自己打开权限。

        ⚠️ 和"看屏幕循环"（`vision.run_forever`）是两件事：
              vision 循环   按 interval_s 抓帧、比指纹、有变化才分析
              这个循环      **到点主动截一帧**，不管画面有没有变化
        用户要的是后者（"30分钟截一次图聊天"），所以不能指望前者。
        """
        log.info("自主截图循环启动（节奏由活泼度档位决定）")
        while True:
            try:
                # 每 30 秒看一次"到点没有"。判定本身是纯计算，很便宜。
                await asyncio.sleep(30.0)
                # 念经的"注意力 hold"到期检查（2026-10-18）：3 分钟没搭话就回自己身上。
                # 挂在这个 30 秒循环里，而不是再起一个任务 —— 它本来就是周期性的。
                self.monologue_hold_expired()
                due, why = self.scheduler.screenshot_due()
                if not due:
                    log.debug("自主截图：不截 —— %s", why)
                    continue
                log.info("自主截图：到点，抓一帧看看（%s）", why)
                # wait=False：**忙就跳过，不排队**（2026-10-18 单飞）。
                # 排队解决不了排队 —— 每 2 分钟堆一个 15 秒的请求，队列只会越来越长。
                result = await capture_and_describe(self.config, wait=False)
                self.scheduler.mark_autosnap()
                if not result.ok:
                    log.info("自主截图：没看懂（%s）—— 这次跳过", result.message)
                    continue
                await self.conversation.speak_autonomously(
                    {
                        "source": "autosnap",
                        "context_hint": describe_for_prompt(result),
                        "stage_direction": stage_direction_for_self(result),
                        "screen": result.description,
                    },
                    # 原图进原文记忆（BLOB，用户要求"图片放进数据库存储"）。
                    # mime 不用操心：`store.remember_turn` 会按字节魔数归一化。
                    image=result.image,
                    image_mime="image/jpeg",
                )
                # 记下"刚说完截图的话"：接下来 60 秒内不许自主搭话
                # （2026-10-18 用户规则：截图优先，且截图说完 1 分钟内不搭话）。
                self._last_screen_speak_at = time.monotonic()
            except asyncio.CancelledError:
                log.info("自主截图循环已停止")
                raise
            except Exception:  # noqa: BLE001 —— 单轮异常不能让这个循环死
                log.exception("自主截图循环单轮异常（已忽略，继续）")

    async def serve_forever(self) -> None:
        await self.server.serve_forever()

    # ------------------------------------------------------ 随机事件循环（2026-10-17）
    #
    # 用户要求：「不定时的随机事件也写进去…顺便将随机事件列为重点功能」

    def _events_day_start(self, now: float) -> float:
        """今天 0 点的时间戳（日上限按自然日算，不按滚动 24 小时）。"""
        st = time.localtime(now)
        return time.mktime((st.tm_year, st.tm_mon, st.tm_mday, 0, 0, 0, 0, 0, -1))

    async def _run_events_forever(self) -> None:
        """
        事件循环：每隔 `tick_s` 抽一次"它要不要做点自己的事"。

        ## 与自主发言的区别

        ```
        自主发言（Scheduler）  决定"说不说话" —— 有阈值、有冷却
        随机事件（本方法）     决定"做不做事" —— 做了不一定说
        ```

        分开的理由：它去搜了句哲学，那是**行为**，未必要立刻开口。
        「做」和「说」耦合的话，"它有自己的事"就变成了"它又来找你说话"，
        反而更吵。
        """
        if not self.config.events.get("enabled", True):
            log.info("随机事件已在配置中关闭")
            return
        log.info(
            "随机事件循环启动：基础概率 %.3f、全局间隔 %.0fs、日上限 %d",
            float(self.config.events.get("base_chance", 0.02)),
            float(self.config.events.get("min_gap_s", 600.0)),
            int(self.config.events.get("max_per_day", 12)),
        )
        while True:
            try:
                await self._maybe_fire_event()
            except asyncio.CancelledError:
                log.info("随机事件循环已停止")
                raise
            except Exception:  # noqa: BLE001 —— 事件绝不能拖垮 sidecar
                log.exception("随机事件单轮异常（已忽略，继续下一轮）")
            await asyncio.sleep(max(1.0, float(self.config.autonomy.get("tick_s", 5.0))))

    async def _snap_and_chat(self, req_id: str = "") -> None:
        """
        截一张屏 -> 看懂 -> 把注意力拉到用户身上 -> 顺着画面开口。

        ## 三步的顺序是刻意的

        ```
        ① 抓屏 + 看懂   （失败/降级都要带出原因）
        ② 主观层        （注意力压到贴脸 + 表情变好奇）
        ③ 开口          （把描述塞进 context，让它像"自己看到的"一样说）
        ```

        ②必须在③之前：提示词里"你现在的状态"是**运行时读的**，
        如果先发言再改状态，那一句话里它还在"想着自己的事"，
        说出来的就不像在接你的图。

        ## 失败路径的取舍

        看不懂（没有视觉模型 / 配额 / 断网）时**仍然让它的表情变好奇、
        并且仍然开口** —— 只是它会说"我看看…这个我看不太清"，
        而不是**编造**画面内容。编造比看不见糟得多：用户当场就能发现。
        """
        section = self.config.screenshot
        if not section.get("enabled", True):
            log.info("截图对话已在配置中关闭")
            await self.broadcast(
                msg("vision.snap.done", ok=False, message="截图对话已在配置里关闭", requestId=req_id)
            )
            return

        result = await capture_and_describe(self.config)

        # ② 主观层：先拉注意力（无论看不看懂 —— 用户确实把屏幕给它看了）
        # 注意力 + 生气/亲近 + **好奇表情**都在这一步里（见 subjectivity 的说明：
        # 表情刻意放在那边设，避免两个调用点漏一个）。
        self.conversation.subjectivity.on_user_shows_screen()
        await self.bus.publish(
            "pet.emotion",
            msg(
                "pet.emotion",
                state=self.conversation.emotion.as_dict(),
                attention=self.conversation.attention_payload(),
                expression="curious",
            ),
        )

        await self.broadcast(
            msg(
                "vision.snap.done",
                ok=bool(result.ok),
                message=result.message,
                source=result.source,
                requestId=req_id,
                **_snap_fields(result),
            )
        )

        if not result.ok:
            # 彻底看不到（抓屏失败 / 被隐私挡住）。
            #
            # ⚠️ 这里**不发 `pet.say.end`**：没有配对的 `say.start`，
            #    前端拿到一条"结束"却没收尾过一条"开始"，气泡状态会各走各的。
            #    原因由主进程用 toast 显示（`snapshotChat()` 收到 ok=false 时会弹）。
            if result.message:
                await self.bus.publish(
                    "system.error",
                    msg("system.error", level="warn", message=result.message, where="vision.snap"),
                )
            return

        if not section.get("auto_speak", True):
            log.debug("截图已看懂，但 auto_speak=false，不主动开口")
            return

        # ③ 开口：把描述作为 context 塞进去，并走「主动发言」那条管线
        hint = describe_for_prompt(result)
        # 记一条长度日志：截图对话出问题时，第一个要确认的就是"描述到底有没有
        # 进提示词"。没有这条日志时只能在 gateway / subjectivity / 大脑之间反复猜。
        log.info("截图对话：提示词片段 %d 字：%s", len(hint), hint[:70].replace("\n", " "))
        try:
            await self.conversation.speak_autonomously(
                # ⚠️ 键名必须是 context_hint —— _compose_context_hint 只读这一个。
                #    写成 hint 的话它会被**静默忽略**（不报错、也不出现在提示词里），
                #    表现是它看得见屏幕但不说内容。这种字典键写错门禁查不出来。
                {
                    "source": "snap",
                    # 用户自己按的「截图对话」——这次回复必须给文字，
                    # 哪怕宠物正趴在屏幕边上（全屏态）。见 _generate 里 userInitiated 的说明。
                    "user_initiated": True,
                    "context_hint": hint,
                    # ⚠️ 同一份描述**再放进 user 轮**。看起来重复，但两处的用途不同：
                    #    system 里那份是背景（可能被记忆压过去），
                    #    user 轮这份是"现在就做这件事"的指令（模型一定会读）。
                    "stage_direction": stage_direction_for(result),
                    "screen": result.description,
                },
                # 原图进原文记忆（BLOB）—— 和自主截图那条路保持一致。
                #
                # ⚠️ 2026-10-19 用户反馈：「现在用户自主截图对话的画面还不能放在
                #    记忆管理的原文记忆中查看」。原因就是这个调用**漏传了 image**：
                #    `speak_autonomously` 只有拿到 image 才会挂 `_pending_image`，
                #    而 `_remember_assistant` 又只认这个字段 —— 于是用户在菜单里
                #    按「截图对话」触发的那一次，原文记忆里 has_image 永远是 0，
                #    只有自主截图（autosnap 循环）那一份带图。
                #    两条路必须传同样的东西，否则"用户主动看的"反而查不到。
                image=result.image,
                image_mime="image/jpeg",
            )
        except Exception:  # noqa: BLE001 —— 说话失败不该影响别的
            log.exception("截图对话开口失败")

    async def _maybe_fire_event(self) -> None:
        """抽一次并（可能）执行。抽不中就安静返回。"""
        if self.memory is None or self.memory.db is None:
            return
        now = time.time()
        db = self.memory.db
        try:
            last_by_kind: dict[str, float] = {}
            for row in db.recent_events(limit=50):
                kind = str(row.get("kind") or "")
                if kind and kind not in last_by_kind:
                    last_by_kind[kind] = float(row.get("created_at") or 0.0)
            spec, why = self.events.pick(
                now=now,
                last_interaction_at=self.scheduler.last_interaction_at,
                last_event_at=db.last_event_at(),
                events_today=db.count_events_since(self._events_day_start(now)),
                recent_kinds=set(list(last_by_kind)[:1]),   # 刚做过的不马上做
                last_by_kind=last_by_kind,
                in_conversation=self.conversation.current is not None,
                vitality=self.conversation.subjectivity.vitality,
            )
        except Exception:  # noqa: BLE001
            log.debug("事件判定失败（已忽略）", exc_info=True)
            return
        if spec is None:
            # ⚠️ 抽不中时**必须留一行 DEBUG**（2026-10-17 加的）。
            #
            # 不加的话，"事件循环到底在不在跑"在日志里完全看不出来：
            # 实测等了 8 分钟、event_log 还是 0，日志里一条事件相关的都没有 ——
            # 分不清是「在跑但没抽中」还是「循环根本没起来」。
            #
            # 现在这一行会说清原因（not_idle_enough / global_gap /
            # daily_cap / chance / quiet_hours / dormant:缺xxx）。
            log.debug("事件：不触发（%s）", why)
            return

        # 执行（可能真去联网）
        ctx = {
            "rng": self.events.rng,
            # ⚠️ 2026-10-18 去掉 `ctx["search"]`：哲学事件已删除，当前没有事件需要联网。
            # 第二版如果要有"自己去查东西"的事件，调 `tools.search_web()` ——
            # 它已经统一带上了「联网关了就不许搜」的闸门（见 tools.py）。
            # ⚠️ `_interest_topics` 定义在 **Conversation** 上（它要读 memory），
            #    不是 Gateway。2026-10-17 第一版写成 `self._interest_topics(...)`，
            #    于是**每一轮事件判定都抛 AttributeError**：
            #
            #        AttributeError: 'Gateway' object has no attribute '_interest_topics'
            #
            #    而异常被 `_run_events_forever` 的兜底 `except` 接住、只记一条
            #    ERROR 就"继续下一轮" —— 结果就是**随机事件永远不触发、
            #    event_log 永远是 0，而进程看起来一切正常**。
            #    真机验证才抓到（`tools/test-live-subjectivity.py`）。
            "interests": self.conversation._interest_topics(limit=5),
        }
        outcome = await self.events.run(spec, ctx)
        if outcome is None:
            log.debug("事件「%s」没做成（不记录）", spec.label)
            return

        # 施加效果：状态 + 印象 + 落库 + 通知前端
        try:
            actual = self.conversation.subjectivity.apply_event_effect(
                mood=outcome.mood_delta,
                energy=outcome.energy_delta,
                intimacy=outcome.intimacy_delta,
                attention=outcome.attention_delta,
            )
            if outcome.impression:
                self.conversation.subjectivity.record_impression(outcome.impression)
                db.add_impression(outcome.impression, source=spec.kind, now=now)
            db.record_event(
                spec.kind,
                spec.label,
                outcome.detail,
                # ⚠️ 记**实际生效**的改动，不是请求的改动 ——
                # 状态已到顶时 clamp01 会把增量吃掉，记请求值会让人以为生效了。
                mood_delta=actual["mood"],
                energy_delta=actual["energy"],
                intimacy_delta=actual["intimacy"],
                attention_delta=actual["attention"],
                now=now,
            )
            log.info(
                "随机事件「%s」：%s（心情%+.3f 精力%+.3f 亲近%+.3f 注意力%+.3f）",
                spec.label,
                outcome.detail,
                actual["mood"],
                actual["energy"],
                actual["intimacy"],
                actual["attention"],
            )
            await self.bus.publish(
                "pet.event",
                msg(
                    "pet.event",
                    kind=spec.kind,
                    label=spec.label,
                    detail=outcome.detail,
                    attention=self.conversation.attention_payload(),
                ),
            )
        except Exception:  # noqa: BLE001 —— 效果施加失败不影响已记录的事实
            log.exception("随机事件效果施加失败（已忽略）")

    async def shutdown(self) -> None:
        log.info("正在关闭 Gateway…")
        # 退出前把**当前这一段**收个尾（2026-10-17）。
        #
        # 不补这一下的话："今天聊了一晚上"的印象永远不落地 —— 因为段结束
        # 靠的是"10 分钟没说话"或"满 8 轮"，而用户直接关 App 两条都不满足。
        #
        # ⚠️ 这里要 await：进程马上要退，后台任务不一定活到跑完。
        # ⚠️ 而且要短超时：提炼要调模型，退出不能被它拖住十几秒。
        try:
            if self.conversation.segment.worth_consolidating():
                snapshot = self.conversation.segment.take()
                log.info("退出前收尾这一段：%d 轮", snapshot.get("turns", 0))
                # ⚠️ `_consolidate_segment` 在 **Conversation** 上，不在 Gateway。
                #    2026-10-17 第一版写成 `self._consolidate_segment(...)`，
                #    和 `_interest_topics` 是**同一个错误**（由
                #    `tools/check-self-refs.py` 抓出来）。
                await asyncio.wait_for(
                    self.conversation._consolidate_segment(snapshot, "shutdown"),
                    timeout=8.0,
                )
        except asyncio.TimeoutError:
            log.warning("退出前收尾超时（已跳过，不影响退出）")
        except Exception:  # noqa: BLE001 —— 收尾失败绝不能阻止退出
            log.debug("退出前收尾失败（已忽略）", exc_info=True)
        # 退出前收尾记忆库。SQLite 下**没有"内存里没落盘"的条目**（每次写入即 commit），
        # 所以这里只做一次干净的关闭（提交残留事务 + 释放连接与文件锁）。
        if self.memory is not None:
            try:
                stats = self.memory.stats()
                log.info(
                    "记忆库已关闭（检索 %s 条、最近 %s 轮、原文 %s 条）",
                    stats.get("memory"),
                    stats.get("recent_turn"),
                    stats.get("raw_log"),
                )
                self.memory.close()
            except Exception:  # noqa: BLE001 —— 关闭失败不该阻塞退出
                log.exception("记忆库关闭失败（忽略，继续退出）")
        for task_attr in ("_autonomy_task", "_vision_task", "_voice_task", "_memory_task"):
            task = getattr(self, task_attr, None)
            if task is None or task.done():
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.plugins.unload_all()
        if self.conversation.current is not None and not self.conversation.current.done():
            self.conversation.current.cancel()
            try:
                await self.conversation.current
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.server.close()

    def new_say_id(self) -> str:
        """给插件用的发言 id 分配器。"""
        return self.conversation.new_say_id()
