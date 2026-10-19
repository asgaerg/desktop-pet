"""
配置系统：默认值 + YAML 加载 + 校验 + 热更新。

为什么自己写 YAML 解析：见 ws_server.py 开头的说明 —— sidecar 零第三方依赖。
本模块只实现项目配置实际用到的 YAML 子集：

    key: value            # 标量（bool / int / float / str 自动推断）
    key:                  # 嵌套映射（靠缩进）
      sub: 1
    key:                  # 列表
      - item
      - item
    key: [a, b, c]        # 行内数组
    key: "带 空格 的字符串"  # 引号保留原样
    # 注释

不支持：锚点/引用、多行块标量(| >)、复杂嵌套行内对象、tab 缩进。
遇到不支持的语法会抛 ConfigError 并指出行号 —— 宁可启动失败，也不要静默读到错配置。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import tiers
from .protocol import clamp01, deep_merge

log = logging.getLogger("pet.config")


class ConfigError(Exception):
    """配置文件语法或取值非法。"""


# --------------------------------------------------------------------------- #
# 默认值：配置的唯一权威定义。UI 只读这里暴露出来的字段。
# --------------------------------------------------------------------------- #

DEFAULT_PERSONA: dict[str, Any] = {
    "name": "小灵",
    "user_title": "你",
    "system_prompt": (
        "你是住在用户电脑桌面上的小宠物，名字叫{name}。你不是助手，不列条款、不说"
        "\"作为一个AI\"这类话。你说话短、口语化、有情绪，像个真实的小家伙。"
        "你可以对用户在做的事情好奇、吐槽、撒娇或犯困，但永远不挖苦用户。"
        "回答控制在两三句以内，除非用户明确要你详细讲。"
    ),
    "speech_style": [
        "口语、短句，不说套话",
        "不说\"有什么可以帮您\"这类客服腔",
        "可以用语气词（诶、嗯、啊、呀）",
    ],
    "idle_topics": ["用户屏幕上的内容", "当前时间与作息", "自己刚看到/想到的小事"],
}

DEFAULT_ACTIVITY: dict[str, Any] = {
    "overall": 0.5,
    # 「自言自语」待多久回落到「安静」（秒，2026-10-17 用户要求）。
    # 用户原话「20分钟不到就会回到安静状态」—— 1200 秒是**大约**，
    # 所以做成可配：真嫌长/嫌短改这里就行，代码里没有第二个 20 分钟。
    "monologue_decay_s": 1200.0,
    "idle_chatter_rate": 0.35,
    "screen_watch_prob": 0.40,
    "reaction_latency_ms": 600,
    "interrupt_prob": 0.10,
    "message_length": 0.5,
    "emoji_sticker_rate": 0.3,
    "quiet_hours": [],
    "focus_guard": True,
}

# 自主行为（P5）。这是"活泼度"真正落地的地方：
# activity 描述"说话什么风格"，autonomy 决定"什么时候自己开口"。
DEFAULT_AUTONOMY: dict[str, Any] = {
    "enabled": True,
    "tick_s": 5.0,
    # 意愿分超过它才开口。阈值故意偏高：宁可少说，也不要变成话痨弹窗。
    "speak_threshold": 0.55,
    # 总活泼度映射到的阈值范围：拉到 0 几乎不主动，拉到 1 阈值降到 floor
    "threshold_floor": 0.22,
    "threshold_ceiling": 1.05,
    # 任何一次交互后强制安静这么久，避免"用户刚说完它就抢话"
    "cooldown_after_interaction_s": 45.0,
    "cooldown_after_speak_s": 60.0,
    # 用户安静超过这个时长，空闲因素才计入，并在此后持续增强
    # ⚠️ None = **跟随活泼度档位**（2026-10-17）。四档的门是
    # 30 分 / 12 分 / 5 分 / 2 分（见 `tiers.py`），配置里再写一个全局数字
    # 就是两份真源。显式写数字仍然能覆盖（测试和手工调参用）。
    "idle_after_s": None,
    # None = 跟随活泼度档位（2026-10-18）：四档的节奏是
    # 安静 20 分 / 无聊 15 分 / 好奇 10 分 / 自言自语 2 分。
    # 显式写数字覆盖（测试常用 0 关掉这道门）。
    "talk_interval_s": None,
    # None = 跟随档位（好奇 30 分 / 自言自语 10 分；安静与无聊 None = 不截）。
    # ⚠️ 这只是"愿意"，真正的许可仍是 providers.vision.enabled。
    "screenshot_interval_s": None,
    # None = 跟随活泼度档位（2026-10-18）：爬升速度也是档位的一部分。
    "idle_full_s": None,
    # 有屏幕变化时，更倾向"评论"而不是"闲聊"
    "require_observation": False,
    # 屏幕变化带来的新鲜度衰减（秒）
    "observation_decay_s": 180.0,
    "max_idle_tokens": 220,
    # 用户手动免打扰。它是配置而不是纯运行态：这样托盘与界面都能通过
    # config.update 打开/关闭它，sidecar 与前端也不会各存一份而失去同步。
    "muted": False,
}

# 随机事件（2026-10-17，用户列为重点功能）
#
# 「不定时的随机事件也写进去…顺便将随机事件列为重点功能，后面继续添加。」
#
# ⚠️ `base_chance` 是**每次 tick** 抽中的概率，而 tick 是 5 秒一次。
#    0.02 看着小，但 5 秒一次意味着期望约 250 秒抽中一次 ——
#    再叠加 `min_gap_s`（10 分钟）与各事件冷却，实际频率是一天几次到十几次。
#    调这个值前先想清楚这一层，否则会把"一天十几次"调成"每十分钟一次"。
DEFAULT_EVENTS: dict[str, Any] = {
    "enabled": True,
    "base_chance": 0.02,
    # 两次事件之间的全局最小间隔（秒）。防止连着冒事件刷屏。
    "min_gap_s": 600.0,
    # 用户安静这么久之后才允许它去做自己的事（刚聊完就跑去搜哲学很出戏）
    "idle_before_s": 300.0,
    # 一天最多几次。上限存在的意义：用户可以接受"它有点自己的生活"，
    # 但不能接受"它整天在干别的不理我"。
    "max_per_day": 12,
    # 安静时段：不做事也不发声。`[23, 8]` 表示 23:00 到次日 08:00。
    # ⚠️ 支持跨零点，判断逻辑在 `events.EventEngine.in_quiet_hours`。
    "quiet_hours": [],
}

# 「截图对话」（2026-10-17 用户要求）：按按钮/快捷键 -> 立刻抓屏 -> 看懂 -> 开口。
#
# ⚠️ 与 `providers.vision`（后台看屏幕）**是两套东西**：
#      vision      宠物自己持续看（默认关闭）
#      screenshot  用户主动给它看（本段）
# 所以这里**不读** `vision.enabled` —— 详见 `screen_chat.py` 的说明。
DEFAULT_SCREENSHOT: dict[str, Any] = {
    "enabled": True,
    # 送给模型前把长边压到这个尺寸。这一步直接决定 token 与耗时。
    "max_edge_px": 768,
    # 留空则复用 `providers.brain` 当前生效的那一家（模型 / base_url / 密钥）。
    #
    # 为什么默认复用大脑：`deepseek-flash` **本来就支持图片输入**（2026-10-17 实测），
    # 所以不需要让用户再配一个视觉供应商。
    "base_url": "",
    "model": "",
    "api_key_env": "",
    # 🔴 看图必须关思考模式：开着的话推理会把 max_tokens 吃光，正文是空的。
    #     实测 max_tokens=200 + 思考 -> 正文 0 字（而且**不报错**）
    "thinking": False,
    "max_tokens": 400,
    "timeout_s": 45.0,
    # 隐私黑名单（密码/银行/网银…）仍然生效 —— 那是"绝不能送出去"，不是意愿开关。
    "respect_privacy": True,
    # 看懂之后**立刻主动开口**（而不是等用户再打字）
    "auto_speak": True,
    # 看懂之后，在**这次回复的末尾另起一段**念出"它实际看到了什么"
    # （用户要求 2026-10-17，设置页里开）。用途是**核对它有没有看错**：
    #
    #   开了之后那一次回复长这样：
    #     「诶，你在 Steam 商店页看《活性物质》呀…
    #
    #      （补一句，我刚才看到的是：用户正在使用 Steam 客户端浏览游戏商店页面…）」
    #
    # ⚠️ 这一段**不进历史**（见 gateway 里 `screen_seen_note` 的说明）——
    #    进了历史就会被写进记忆，之后每一轮它都可能把这句复述一遍。
    "show_description": False,
}

DEFAULT_PROVIDERS: dict[str, Any] = {
    # brain: "mock" 零依赖本地回声；"openai" 走任何 OpenAI 兼容接口
    "brain": {
        "provider": "mock",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
        "temperature": 0.85,
        "max_tokens": 400,
        "timeout_s": 30,
        "history_turns": 12,
    },
    "asr": {"provider": "local", "model": "small", "device": "cuda"},
    # tts.enabled: 「现在要不要出声」的总开关（2026-10-14 加）。
    # ⚠️ 与 provider 是**正交**的两件事：provider 选"用哪个引擎"
    # （设置→语音里配，none = 一个都不要），这个开关管"引擎配好了，
    # 此刻说不说"。菜单里的「语音播报」改的就是它。
    # 缺省 True —— 与历史行为一致（老配置没这个键，不能一升级就哑巴）。
    "tts": {
        "provider": "none",
        "enabled": True,
        "voice": "zh-CN-XiaoxiaoNeural",
    },
    # vision.provider: "none"（不看屏幕，默认）/ "pillow"（真实截屏）
    "vision": {
        "provider": "none",
        "interval_s": 20,
        "max_edge_px": 768,
        "include_titles": True,
        # 画面无变化时把抓取间隔逐步拉长，最多到这个倍数（省电、省 CPU）
        "idle_backoff_max": 4,
        # 变化判定阈值：64 位指纹的汉明距离要达到多少才算"换了画面"。
        # 调高 = 更迟钝、更省钱；调低 = 更灵敏、更容易被播放中的视频触发。
        # 默认 5 是保守值；实测看视频/滚代码时 8~12 更安静。
        "change_threshold": 5,
    },
}

# 隐私规则。**默认就带一批保守项**：宁可多跳过，也不要让用户某天发现
# 宠物把他密码管理器或银行页面截走了。用户可自由增删。
DEFAULT_PRIVACY: dict[str, Any] = {
    "enabled": True,
    # 窗口标题命中即跳过。匹配是大小写不敏感的子串，不是正则 ——
    # 写配置的人不该被迫学正则，而且子串匹配的误报方向更安全。
    "title_patterns": [
        "密码",
        "password",
        "1password",
        "bitwarden",
        "lastpass",
        "keepass",
        "银行",
        "网银",
        "支付宝",
        "微信支付",
        "paypal",
        "stripe",
        "私密",
        "incognito",
        "无痕",
        "远程桌面",
    ],
    # 前台进程名命中即跳过（需要平台支持，缺失时静默忽略）
    "process_patterns": [],
    "mode": "skip",
}

# 长期记忆（P2）。默认开启，但**只记用户原话与提炼出的长期事实**，
# 且写入前有敏感度过滤（密钥、密码、证件号一律不落盘）。
DEFAULT_MEMORY: dict[str, Any] = {
    "enabled": True,
    # 留空/auto 时落在 ~/.desktop-pet/memory.jsonl —— 记忆属于用户，不属于项目目录
    "path": "auto",
    "max_items": 2000,
    # 检索至少要多像才算相关。太低会把无关记忆塞进提示词，让宠物说出莫名其妙的联想
    "min_score": 0.18,
    "top_k": 5,
    "duplicate_score": 0.92,
    "snippet_chars": 120,
    # 关掉就只记提炼出的事实，不记原话（更省、也更保护隐私）
    "keep_utterances": True,
    "profile_max": 30,
    "extract_timeout_s": 20.0,
    # 嵌入实现。目前只有内置哈希嵌入（零依赖、无需下载模型）
    "embedding": {"provider": "hashing", "dim": 256},
}

# 取值约束：字段 -> (最小值, 最大值)。不在表内的字段原样保留。
_ACTIVITY_BOUNDS: dict[str, tuple[float, float]] = {
    "overall": (0.0, 1.0),
    "monologue_decay_s": (0.0, 86400.0),
    "idle_chatter_rate": (0.0, 1.0),
    "screen_watch_prob": (0.0, 1.0),
    "reaction_latency_ms": (0.0, 5000.0),
    "interrupt_prob": (0.0, 1.0),
    "message_length": (0.0, 1.0),
    "emoji_sticker_rate": (0.0, 1.0),
}

_AUTONOMY_BOUNDS: dict[str, tuple[float, float]] = {
    "tick_s": (0.5, 600.0),
    "speak_threshold": (0.0, 2.0),
    "threshold_floor": (0.0, 2.0),
    "threshold_ceiling": (0.0, 2.0),
    "cooldown_after_interaction_s": (0.0, 7200.0),
    "cooldown_after_speak_s": (0.0, 7200.0),
    # 允许 None（跟随档位）；有数字时夹到 0~86400
    "idle_after_s": (0.0, 86400.0),
    "idle_full_s": (1.0, 86400.0),
    "observation_decay_s": (1.0, 86400.0),
    "max_idle_tokens": (16.0, 4096.0),
}

_AUTONOMY_FLAGS = ("enabled", "require_observation", "muted")


@dataclass
class Config:
    """运行期配置。任何修改都走 `apply_patch`，保证校验与联动一致。"""

    path: Optional[Path] = None
    root: dict[str, Any] = field(
        default_factory=lambda: {
            "persona": copy.deepcopy(DEFAULT_PERSONA),
            "activity": copy.deepcopy(DEFAULT_ACTIVITY),
            "autonomy": copy.deepcopy(DEFAULT_AUTONOMY),
            "events": copy.deepcopy(DEFAULT_EVENTS),
            "screenshot": copy.deepcopy(DEFAULT_SCREENSHOT),
            "providers": copy.deepcopy(DEFAULT_PROVIDERS),
        }
    )
    _listeners: list[Any] = field(default_factory=list, repr=False)

    # ------------------------------------------------------------------ 读

    @property
    def persona(self) -> dict[str, Any]:
        return self.root["persona"]

    @property
    def activity(self) -> dict[str, Any]:
        return self.root["activity"]

    @property
    def providers(self) -> dict[str, Any]:
        return self.root["providers"]

    @property
    def autonomy(self) -> dict[str, Any]:
        # setdefault 而不是直接取：插件或外部代码可能塞进来一个不完整的 root，
        # 缺一段就 KeyError 会让"活泼度"这种核心功能离奇失效。
        return self.root.setdefault("autonomy", copy.deepcopy(DEFAULT_AUTONOMY))

    @property
    def screenshot(self) -> dict[str, Any]:
        """「截图对话」设置。缺失时补默认值（老配置文件没有这一段）。"""
        return self.root.setdefault("screenshot", copy.deepcopy(DEFAULT_SCREENSHOT))

    @property
    def events(self) -> dict[str, Any]:
        """随机事件设置。缺失时补默认值（老配置文件没有这一段）。"""
        return self.root.setdefault("events", copy.deepcopy(DEFAULT_EVENTS))

    @property
    def privacy(self) -> dict[str, Any]:
        return self.root.setdefault("privacy", copy.deepcopy(DEFAULT_PRIVACY))

    @property
    def memory(self) -> dict[str, Any]:
        return self.root.setdefault("memory", copy.deepcopy(DEFAULT_MEMORY))

    @property
    def pet_name(self) -> str:
        return str(self.persona.get("name") or "小灵")

    def brain_provider(self) -> str:
        return str(self.providers.get("brain", {}).get("provider", "mock")).lower()

    # ---------------------------------------------------------------- 大脑多提供商
    #
    # 用户要求 2026-10-08：「两个都要，要和现有的 deepseek 有互切选项」。
    #
    # ## 设计：协议名与厂商名分开
    #
    #   `providers.brain.provider`  = **协议**（mock / openai）—— 决定用哪个实现
    #   `providers.brain.active`    = **厂商**（deepseek / siliconflow）—— 决定连哪儿
    #
    # 为什么不把厂商做成 provider 名：DeepSeek 和硅基流动**都走 OpenAI 兼容协议**，
    # 做成两个 provider 名等于把"协议"和"厂商"混在一起，以后接第三家又要动注册表。
    #
    # ## 兜底策略（重要）
    #
    # `named` 缺失、或 `active` 指向不存在的名字时，**退回旧的平铺字段**
    # （`brain.base_url` / `brain.model` / `brain.api_key_env`）。
    # 这样"配置文件被改坏"不会让宠物静默变成 mock（那种失败最难查）。

    def brain_active(self) -> str:
        """当前生效的厂商名（`providers.brain.active`）。"""
        return str(self.providers.get("brain", {}).get("active") or "deepseek").strip().lower()

    def brain_named(self) -> dict[str, Any]:
        """`providers.brain.named` 里所有厂商配置（以厂商标名为键）。"""
        raw = self.providers.get("brain", {}).get("named")
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}

    def brain_profile(self, name: Optional[str] = None) -> dict[str, Any]:
        """
        取某个厂商**生效的**配置（已与兜底字段合并）。

        合并顺序（后者只补前者**没有**的键）：
            ① `named.<厂商>` 里显式写的
            ② `brain` 下的平铺字段（base_url / model / api_key_env）

        ⚠️ 只在**同一个键**上做兜底，不做整块替换：厂商配置里通常只写它特有的
        几项（base_url / model），而 `temperature` / `max_tokens` / `timeout_s`
        这些**通用参数**仍然从平铺字段继承 —— 否则每个厂商都要抄一遍。
        """
        key = (name or self.brain_active()).strip().lower()
        flat = self.providers.get("brain", {})
        profile: dict[str, Any] = {}
        # 通用的那几项：从平铺字段继承（厂商没写时用）
        #
        # ⚠️ `history_token_budget` 必须在这个白名单里（2026-10-08 踩过）：
        # 它配在 `providers.brain` 下（**不在** `named.<厂商>` 里，因为它是
        # "发给模型多少历史"的通用参数，与厂商无关）。漏了它就表现为
        # "配置里明明写了、运行时读到 None"，然后静默退回按 `history_turns`
        # 折算的旧行为 —— 一个不报错的失效。
        for k in (
            "temperature",
            "max_tokens",
            "timeout_s",
            "history_turns",
            "history_token_budget",
            "context_window",
            "thinking",
        ):
            if k in flat:
                profile[k] = flat[k]
        named = self.brain_named().get(key)
        if isinstance(named, dict):
            profile.update(named)
        else:
            # `active` 指向了不存在的名字 -> 用平铺字段兜底，并留个标记供界面提示
            profile.setdefault("label", key or "默认")
            profile["_fallback"] = True
        for k in ("base_url", "model", "api_key_env"):
            if not profile.get(k) and flat.get(k):
                profile[k] = flat[k]
        return profile

    def api_key(self) -> str:
        """
        取 API 密钥。**环境变量优先**，其次用户目录下的凭据文件。

        两个来源各有各的场合：
          · 环境变量 —— 脚本化、CI、临时试用；优先级最高，避免被文件里的旧值遮住；
          · `~/.desktop-pet/credentials.json` —— 图形界面里填的密钥存这儿。

        为什么密钥**绝不写进 config/persona.yaml**：那是项目文件，会被 git 带走，
        一旦提交就等于泄露。凭据放在用户目录，天然不进仓库。

        ## 查找顺序（2026-10-05 加了后两项）

        ① 环境变量（`api_key_env` 指定的那个）
        ② `PET_CREDENTIALS_FILE` 指定**完整路径** —— 由 Electron 主进程注入
        ③ `~/.desktop-pet/credentials.json` —— 正常落点
        ④ 项目旁的 `apps/desktop/pet-data/credentials.json` —— **兜底落点**

        ⚠️ ③④ 都要试，因为**用户目录不一定可写**：实测在受限环境（沙箱 / 受限账户 /
        杀软拦截）里写 `~/.desktop-pet/` 会 `EPERM`，界面那边于是退到 ④。
        如果 Python 只认 ③，就会出现"界面显示已保存、大脑却报没密钥"这种
        最难查的不一致 —— 所以两边**必须查同一组路径**。

        返回空串表示"没有密钥"，由调用方决定报错还是降级。
        """
        env_name = str(self.brain_profile().get("api_key_env") or "")
        if env_name:
            from_env = os.environ.get(env_name, "").strip()
            if from_env:
                return from_env

        try:
            for cred_path in self._credential_paths():
                if not cred_path.exists():
                    continue
                data = json.loads(cred_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                for name in self._credential_names():
                    value = data.get(name)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        except (OSError, ValueError):
            # 凭据文件坏了不能影响启动；当作没有密钥
            pass
        return ""

    def api_key_for(self, provider: str) -> str:
        """
        按**厂商标**取密钥（`api_key()` 只认"当前生效的大脑"那一家）。

        ## 为什么需要它

        `api_key()` 是给文本大脑用的：它只认 `brain.active` 指向的那一家。
        而「截图对话」可能想用**另一家**的视觉模型（例如大脑用 DeepSeek、
        看图用硅基流动的 Qwen-VL）。

        ## 查找顺序

        ① 环境变量 `{PROVIDER}_API_KEY`
        ② 凭据文件里 `provider` 这个键

        ⚠️ **绝不跨厂商兜底**（踩过一次）：拿 DeepSeek 的密钥去请求硅基流动，
        只会得到 401 "Token is invalid"，而用户完全不知道"这一家还没配密钥"。
        """
        name = str(provider or "").strip()
        if not name:
            return ""
        import os

        env_name = "".join(ch if ch.isalnum() else "_" for ch in name).upper() + "_API_KEY"
        from_env = os.environ.get(env_name, "").strip()
        if from_env:
            return from_env
        try:
            for cred_path in self._credential_paths():
                if not cred_path.exists():
                    continue
                data = json.loads(cred_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                value = data.get(name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        except (OSError, ValueError):
            pass
        return ""

    def _credential_paths(self) -> tuple[Path, ...]:
        """
        凭据文件的候选路径，按优先级排列。

        ## 只有两个来源，都**显式**

        ① `PET_CREDENTIALS_FILE` —— Electron 主进程注入的**完整路径**。
           界面知道它实际写到哪了（用户目录可能不可写，那时会退到项目旁的
           `apps/desktop/pet-data/`），把这个事实告诉这里。
        ② `~/.desktop-pet/credentials.json` —— 正常落点。

        ## ⚠️ 不要在这里自己"猜"项目路径（踩过）

        我一度加过第三条：用 `__file__` 推导出 `<repo>/apps/desktop/pet-data/credentials.json`。
        看着更稳，实际**很糟** —— 它让**测试**也找到了开发者本机真实的密钥，
        于是自检里"无密钥时应当降级 mock"那条直接失败（`未降级：openai`）。

        教训：**凭据的查找路径必须是显式注入的，不能靠代码位置猜** ——
        隐式依赖会让"本机有密钥"和"测试环境没密钥"两种情形行为不一致。
        """
        explicit = os.environ.get("PET_CREDENTIALS_FILE", "").strip()
        if explicit:
            return (Path(explicit),)
        return (Path.home() / ".desktop-pet" / "credentials.json",)

    def _credential_names(self) -> tuple[str, ...]:
        """
        凭据文件里可能用的键名，按优先级排列。

        界面保存时用的是"提供者名"（deepseek / openai）。这里同时接受
        环境变量名的小写形式，这样用户手工写 `DEEPSEEK_API_KEY` 当键名也能work。

        ## ⚠️ 多厂商之后**不能跨厂商兜底**（2026-10-08 实测踩到）

        第一版我在候选里留了 `deepseek` / `openai` 当兜底，结果：

            切到 siliconflow（还没配它的密钥）
            -> 候选名 ('siliconflow', 'siliconflow_api_key', 'deepseek', 'openai')
            -> 取到了 **DeepSeek 的密钥**
            -> 请求硅基流动 -> HTTP 401 "Token is invalid"

        也就是说：**用别家的密钥去请求**，只会得到一个含糊的 401，
        而用户完全不知道"我还没配这一家的密钥"。这比直接报"没有密钥"糟得多 ——
        它会让人去怀疑"密钥写错了 / 硅基流动挂了"。

        所以：**配了 `named` 就只认这一家自己的槽位**（厂商标 + 它自己的
        `api_key_env`）。只有"没有 `named` 结构"的旧配置才退回平铺的
        `api_key_env` —— 那是单厂商时代的写法，兜底是必要的。
        """
        names: list[str] = []
        profile = self.brain_profile()
        active = self.brain_active()
        has_named = bool(self.brain_named())
        if active:
            names.append(active)
        env_name = str(profile.get("api_key_env") or "").strip()
        if env_name:
            names.append(env_name.lower())
        if not has_named:
            # 旧配置（没有 named）：保留原来的兜底顺序，行为不变
            base_url = str(profile.get("base_url") or "")
            host = ""
            try:
                host = urllib.parse.urlsplit(base_url).hostname or ""
            except ValueError:
                host = ""
            if host:
                parts = [p for p in host.split(".") if p and p not in ("api", "www", "com", "cn")]
                if parts:
                    names.append(parts[0])
            names.extend(["deepseek", "openai"])
        # 去重但保持顺序
        seen: set[str] = set()
        ordered: list[str] = []
        for name in names:
            if name and name not in seen:
                seen.add(name)
                ordered.append(name)
        return tuple(ordered)

    @property
    def activity_tier(self) -> "tiers.Tier":
        """
        当前活泼度对应的**档位**（2026-10-17）。

        档位不是第二份真源 —— 它就是 `activity.overall` 的最近档（`tiers.nearest`），
        这样"菜单点档位"和"滑杆拖数值"改的是同一个数，不会各说各话。

        ⚠️ 返回的档位里带 `screenshot`（这一档**愿意**自主截图），
        但它**不是许可**：真正的许可在 `providers.vision.enabled`（用户开关）。
        """
        return tiers.nearest(self.activity.get("overall"))

    def speak_threshold(self) -> float:
        """
        活泼度 -> 开口阈值。这是整个自主行为的"总闸"。

        总活泼度越高，阈值越低（越容易开口）。注意阈值区间与 overall 的映射是
        **线性但反向**的：overall=1 时阈值落到 floor（几乎见到机会就说），
        overall=0 时升到 ceiling（几乎永不主动）。UI 上是一个滑杆，
        这里是一条确定的曲线 —— 用户拖滑杆能预期到行为变化，而不是玄学。
        """
        a = self.activity
        auto = self.autonomy
        overall = clamp01(a.get("overall"), 0.5)
        floor = float(auto.get("threshold_floor", 0.22))
        ceiling = float(auto.get("threshold_ceiling", 1.05))
        return ceiling + (floor - ceiling) * overall

    def activity_for_prompt(self) -> str:
        """把活泼度翻译成自然语言约束，注入 system prompt 影响文风长度。"""
        a = self.activity
        overall = clamp01(a.get("overall"), 0.5)
        length = clamp01(a.get("message_length"), 0.5)
        # 档位驱动（2026-10-17）：先用档位给一段"这一档怎么做"的描述，
        # 再叠加下面的长度约束。四档的定义集中在 `tiers.py`，这里不再重复判断 ——
        # 早先这里是 0.25 / 0.55 / 0.8 三段 if/elif，和 `Tier` 是两份定义，
        # 改一处忘另一处就会出现"文案说安静、调度器却在催它说话"。
        tier = self.activity_tier
        mood = tier.style
        if length < 0.35:
            mood += " 回复尽量控制在一句话。"
        elif length > 0.7:
            mood += " 可以多说两句，但不超过四句。"
        quiet = a.get("quiet_hours") or []
        if quiet:
            mood += f" 免打扰时段：{', '.join(str(q) for q in quiet)}。"
        return mood

    # ------------------------------------------------------------------ 写

    def apply_patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        """
        应用配置补丁并返回"实际生效的差异"。

        返回生效差异（而不是请求的差异）很重要：UI 滑杆可以随便传，越界值会
        被夹取，前端拿回执校准显示，避免界面和真实状态不一致。
        """
        if not isinstance(patch, dict) or not patch:
            return {}
        before = copy.deepcopy(self.root)
        merged = deep_merge(self.root, patch)
        self.root = _validate(merged)
        return _diff(before, self.root)

    def reload_from_disk(self) -> dict[str, Any]:
        """重新读取配置文件，返回生效差异。用于手动改 yaml 后热加载。"""
        before = copy.deepcopy(self.root)
        loaded = load_config(self.path)
        self.root = loaded.root
        return _diff(before, self.root)

    def snapshot(self) -> dict[str, Any]:
        """可安全发给前端的配置快照（已剔除 providers 中的敏感项）。"""
        snap = copy.deepcopy(self.root)
        providers = snap.get("providers", {})
        for section in providers.values():
            if isinstance(section, dict):
                section.pop("api_key_env", None)
        return snap

    def prompt_config(self) -> dict[str, Any]:
        """给前端展示的、与"人格"有关的只读信息。"""
        return {
            "name": self.pet_name,
            "user_title": self.persona.get("user_title", "你"),
            "brain": self.brain_provider(),
        }


# --------------------------------------------------------------------------- #
# 校验与联动
# --------------------------------------------------------------------------- #


def _validate(root: dict[str, Any]) -> dict[str, Any]:
    """逐字段校验并夹取；未知字段保留（便于插件携带自己的配置）。"""
    out = copy.deepcopy(root)
    activity = out.setdefault("activity", {})
    if not isinstance(activity, dict):
        raise ConfigError("activity 必须是映射")

    # 注意：这里**不能**先用 clamp01 把 overall 夹一遍再进下面的循环。
    # clamp01 对非数值会回退到默认值，等于把 "overall: 很活泼" 这种错误配置
    # 静默改成 0.5 —— 用户改了配置文件却毫无反应，比直接报错难查得多。
    for key, (lo, hi) in _ACTIVITY_BOUNDS.items():
        if key not in activity or activity[key] is None:
            continue
        value = activity[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"activity.{key} 必须是数字，收到 {value!r}")
        activity[key] = max(lo, min(hi, float(value)))

    quiet = activity.get("quiet_hours", [])
    if isinstance(quiet, str):
        quiet = [quiet]
    if not isinstance(quiet, list):
        raise ConfigError("activity.quiet_hours 必须是列表或字符串")
    activity["quiet_hours"] = [str(q) for q in quiet]
    activity["focus_guard"] = bool(activity.get("focus_guard", True))

    out = _validate_autonomy(out)

    persona = out.setdefault("persona", {})
    if not isinstance(persona, dict):
        raise ConfigError("persona 必须是映射")
    for key, default in DEFAULT_PERSONA.items():
        if key not in persona:
            persona[key] = copy.deepcopy(default)
    if not isinstance(persona.get("speech_style"), list):
        persona["speech_style"] = list(DEFAULT_PERSONA["speech_style"])
    if not isinstance(persona.get("idle_topics"), list):
        persona["idle_topics"] = list(DEFAULT_PERSONA["idle_topics"])

    providers = out.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise ConfigError("providers 必须是映射")
    for section, defaults in DEFAULT_PROVIDERS.items():
        current = providers.get(section)
        if not isinstance(current, dict):
            providers[section] = copy.deepcopy(defaults)
        else:
            for key, value in defaults.items():
                current.setdefault(key, value)

    out = _validate_privacy(out)
    out = _validate_memory(out)

    return out


def _validate_memory(root: dict[str, Any]) -> dict[str, Any]:
    """校验记忆段。数值做范围夹取，类型不对就报错。"""
    section = root.setdefault("memory", {})
    if not isinstance(section, dict):
        raise ConfigError("memory 必须是映射")
    for key, default in DEFAULT_MEMORY.items():
        section.setdefault(key, copy.deepcopy(default))

    numeric = {
        "max_items": (10, 100_000),
        "min_score": (-1.0, 1.0),
        "top_k": (1, 50),
        "duplicate_score": (0.5, 1.0),
        "snippet_chars": (20, 1000),
        "profile_max": (1, 500),
        "extract_timeout_s": (1.0, 300.0),
    }
    for key, (low, high) in numeric.items():
        value = section.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"memory.{key} 必须是数字，收到 {value!r}")
        section[key] = max(low, min(high, float(value)))
    section["max_items"] = int(section["max_items"])
    section["top_k"] = int(section["top_k"])
    section["snippet_chars"] = int(section["snippet_chars"])
    section["profile_max"] = int(section["profile_max"])

    for key in ("enabled", "keep_utterances"):
        if not isinstance(section.get(key), bool):
            raise ConfigError(f"memory.{key} 必须是布尔值，收到 {section.get(key)!r}")

    embedding = section.setdefault("embedding", copy.deepcopy(DEFAULT_MEMORY["embedding"]))
    if not isinstance(embedding, dict):
        raise ConfigError("memory.embedding 必须是映射")
    for key, default in DEFAULT_MEMORY["embedding"].items():
        embedding.setdefault(key, default)
    dim = embedding.get("dim")
    if not isinstance(dim, int) or dim < 16:
        raise ConfigError(f"memory.embedding.dim 必须是 ≥16 的整数，收到 {dim!r}")
    return root


def _validate_privacy(root: dict[str, Any]) -> dict[str, Any]:
    """校验隐私规则段。列表项一律转成字符串，非法类型报错。"""
    section = root.setdefault("privacy", {})
    if not isinstance(section, dict):
        raise ConfigError("privacy 必须是映射")
    for key, default in DEFAULT_PRIVACY.items():
        section.setdefault(key, copy.deepcopy(default))

    for key in ("title_patterns", "process_patterns"):
        value = section.get(key)
        if isinstance(value, str):
            value = [value]
        if value is None:
            value = []
        if not isinstance(value, list):
            raise ConfigError(f"privacy.{key} 必须是列表或字符串")
        section[key] = [str(item) for item in value]

    section["enabled"] = bool(section.get("enabled", True))
    mode = str(section.get("mode", "skip"))
    if mode not in ("skip",):
        raise ConfigError(f"privacy.mode 只支持 'skip'，收到 {mode!r}")
    section["mode"] = mode
    return root


def _validate_autonomy(root: dict[str, Any]) -> dict[str, Any]:
    """校验 autonomy 段。与 activity 一样：类型不对就报错，不静默兜底。"""
    section = root.setdefault("autonomy", {})
    if not isinstance(section, dict):
        raise ConfigError("autonomy 必须是映射")
    for key, default in DEFAULT_AUTONOMY.items():
        section.setdefault(key, copy.deepcopy(default))

    for key, (lo, hi) in _AUTONOMY_BOUNDS.items():
        value = section.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"autonomy.{key} 必须是数字，收到 {value!r}")
        section[key] = max(lo, min(hi, float(value)))

    for key in _AUTONOMY_FLAGS:
        value = section.get(key)
        if not isinstance(value, bool):
            raise ConfigError(f"autonomy.{key} 必须是布尔值，收到 {value!r}")

    # 阈值区间反了就自动掰正，而不是报错 —— 这只是用户拖滑杆时的边界情况
    if section["threshold_floor"] > section["threshold_ceiling"]:
        section["threshold_floor"], section["threshold_ceiling"] = (
            section["threshold_ceiling"],
            section["threshold_floor"],
        )
    # `idle_after_s` 可以是 None（跟随档位）——那时这条校验没得比，跳过。
    idle_after = section.get("idle_after_s")
    idle_full = section.get("idle_full_s")
    # 两个都可以是 None（跟随档位）—— 那时没得比，跳过
    if (
        isinstance(idle_after, (int, float))
        and isinstance(idle_full, (int, float))
        and idle_full <= float(idle_after)
    ):
        section["idle_full_s"] = float(idle_after) + 1.0
    if section["max_idle_tokens"] < 16:
        section["max_idle_tokens"] = 16.0
    section["max_idle_tokens"] = int(section["max_idle_tokens"])
    return root


def _diff(before: dict[str, Any], after: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """计算两份配置的实际差异，扁平化成点号路径 -> 新值。"""
    out: dict[str, Any] = {}
    for key in set(before) | set(after):
        path = f"{prefix}{key}"
        old, new = before.get(key), after.get(key)
        if isinstance(old, dict) and isinstance(new, dict):
            out.update(_diff(old, new, prefix=f"{path}."))
        elif old != new:
            out[path] = new
    return out


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #


def load_config(path: Optional[Path] = None) -> Config:
    """
    加载配置。文件不存在时用全默认值并记录日志（首次启动就是这种情况，不是错误）。
    文件存在但语法错误时抛 ConfigError —— 静默忽略错配置比崩溃更危险。
    """
    cfg = Config(path=Path(path) if path else None)
    if cfg.path is None or not cfg.path.exists():
        log.info("未找到配置文件%s，使用内置默认值", f"（{cfg.path}）" if cfg.path else "")
        return cfg

    raw_text = cfg.path.read_text(encoding="utf-8")
    data = parse_yaml(raw_text, source=str(cfg.path))
    merged = deep_merge(cfg.root, data)
    cfg.root = _validate(merged)
    log.info("已加载配置 %s", cfg.path)
    return cfg


def parse_yaml(text: str, source: str = "<string>") -> dict[str, Any]:
    """解析 YAML 子集，返回顶层映射。"""
    parser = _MiniYaml(text, source)
    result = parser.parse()
    if not isinstance(result, dict):
        raise ConfigError(f"{source}: 顶层必须是映射")
    return result


class _MiniYaml:
    """缩进驱动的递归下降解析器。"""

    _SCALAR_INT = re.compile(r"^[+-]?\d+$")
    _SCALAR_FLOAT = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")

    def __init__(self, text: str, source: str) -> None:
        self.source = source
        self.lines: list[tuple[int, int, str]] = []  # (行号, 缩进, 内容)
        for lineno, raw in enumerate(text.splitlines(), start=1):
            if "\t" in raw[: len(raw) - len(raw.lstrip())]:
                raise ConfigError(f"{source}:{lineno}: 缩进请用空格，不要用 Tab")
            stripped = _strip_comment(raw).rstrip()
            if not stripped.strip():
                continue
            indent = len(stripped) - len(stripped.lstrip(" "))
            self.lines.append((lineno, indent, stripped.strip()))
        self.pos = 0

    # -------------------------------------------------------------- 对外

    def parse(self) -> Any:
        if not self.lines:
            return {}
        value = self._parse_block(self.lines[0][1])
        if self.pos != len(self.lines):
            lineno, _, content = self.lines[self.pos]
            raise ConfigError(f"{self.source}:{lineno}: 无法解析的内容 {content!r}（缩进不一致？）")
        return value

    # -------------------------------------------------------------- 内部

    def _parse_block(self, indent: int) -> Any:
        if self.pos >= len(self.lines):
            return {}
        _, current_indent, content = self.lines[self.pos]
        if current_indent < indent:
            return {}
        if content.startswith("- "):
            return self._parse_list(indent)
        if content == "-":
            return self._parse_list(indent)
        return self._parse_map(indent)

    def _parse_list(self, indent: int) -> list[Any]:
        items: list[Any] = []
        while self.pos < len(self.lines):
            lineno, line_indent, content = self.lines[self.pos]
            if line_indent < indent or not (content.startswith("- ") or content == "-"):
                break
            if line_indent > indent:
                raise ConfigError(f"{self.source}:{lineno}: 列表项缩进过深")
            self.pos += 1
            rest = content[2:].strip() if content.startswith("- ") else ""
            if not rest:
                # "-" 后面换行：子块整体属于该项
                child_indent = self._peek_indent()
                items.append(self._parse_block(child_indent) if child_indent > indent else None)
            elif _is_mapping_entry(rest):
                # "- key: value"：列表项是一个映射。
                # 不修改 self.lines（曾因此污染解析状态），而是把行内那对
                # 键值手动收进映射，再继续收集同层级的后续键。
                key, _, raw_value = rest.partition(":")
                entry: dict[str, Any] = {}
                value_text = raw_value.strip()
                if value_text:
                    entry[key.strip()] = _parse_scalar(value_text, self.source, lineno)
                else:
                    child_indent = self._peek_indent()
                    entry[key.strip()] = (
                        self._parse_block(child_indent) if child_indent > indent else None
                    )
                self._collect_map_entries(entry, indent + 2)
                items.append(entry)
            else:
                items.append(_parse_scalar(rest, self.source, lineno))
        return items

    def _parse_map(self, indent: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        self._collect_map_entries(out, indent)
        return out

    def _collect_map_entries(self, out: dict[str, Any], indent: int) -> None:
        """收集缩进恰好为 `indent` 的连续 `key: value` 行，写入 out。"""
        while self.pos < len(self.lines):
            lineno, line_indent, content = self.lines[self.pos]
            if line_indent < indent:
                break
            if line_indent > indent:
                raise ConfigError(f"{self.source}:{lineno}: 意外的缩进（内容 {content!r}）")
            if content.startswith("- "):
                break
            key, sep, raw_value = content.partition(":")
            if not sep:
                raise ConfigError(f"{self.source}:{lineno}: 缺少冒号 -> {content!r}")
            key = key.strip()
            if not key:
                raise ConfigError(f"{self.source}:{lineno}: 空键名")
            if not re.fullmatch(r"[A-Za-z0-9_.\-]+", key):
                raise ConfigError(f"{self.source}:{lineno}: 键名含非法字符 -> {key!r}")
            value_text = raw_value.strip()
            self.pos += 1
            if value_text in ("|", "|-", ">", ">-"):
                out[key] = self._parse_block_scalar(indent, folded=value_text.startswith(">"))
            elif value_text:
                out[key] = _parse_scalar(value_text, self.source, lineno)
            else:
                child_indent = self._peek_indent()
                out[key] = self._parse_block(child_indent) if child_indent > indent else None

    def _parse_block_scalar(self, parent_indent: int, folded: bool) -> str:
        """
        读取 `key: |` / `key: >` 块标量：把比父行更深缩进的行整体作为文本。
        这是人格提示词的关键能力 —— 长文本非得能写成多行，否则配置没法读也没法改。
        """
        raw_lines: list[str] = []
        block_indent: Optional[int] = None
        while self.pos < len(self.lines):
            lineno, line_indent, content = self.lines[self.pos]
            if line_indent <= parent_indent:
                break
            if block_indent is None:
                block_indent = line_indent
            raw_lines.append(content)
            self.pos += 1
        if folded:
            return " ".join(raw_lines).strip()
        return "\n".join(raw_lines)

    def _peek_indent(self) -> int:
        if self.pos >= len(self.lines):
            return -1
        return self.lines[self.pos][1]


def _is_mapping_entry(text: str) -> bool:
    key, sep, _ = text.partition(":")
    return bool(sep) and bool(key.strip()) and bool(re.fullmatch(r"[A-Za-z0-9_.\-]+", key.strip()))


def _strip_comment(line: str) -> str:
    """去掉行尾注释，但不动引号内的 # 和 URL 里的 #。"""
    out = []
    quote: Optional[str] = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
            out.append(ch)
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out)


def _parse_scalar(text: str, source: str, lineno: int) -> Any:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip(), source, lineno) for part in _split_inline(inner)]
    if text == "{}":
        return {}
    if text == "[]":
        return []
    lowered = text.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "~", ""):
        return None
    if _MiniYaml._SCALAR_INT.match(text):
        return int(text)
    if _MiniYaml._SCALAR_FLOAT.match(text):
        return float(text)
    return text


def _split_inline(inner: str) -> list[str]:
    """按逗号切分行内数组，忽略引号内的逗号。"""
    parts: list[str] = []
    buf: list[str] = []
    quote: Optional[str] = None
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            continue
        if ch == ",":
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return [p for p in parts if p.strip()]
