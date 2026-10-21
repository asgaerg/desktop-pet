"""
长期记忆（P2）—— 让宠物记住用户说过的重要事情。

分三层，各自独立可测：

    ① 存储层  MemoryStore    条目、检索、去重、容量上限、JSONL 持久化
    ② 提炼层  FactExtractor  从对话里抽出**长期有效**的事实（"我在学 Rust"）
    ③ 注入层  MemoryManager  检索相关记忆 + 用户画像 -> 拼进系统提示词

为什么要长期记忆：没有它，宠物每次对话都从零开始，"你上次说的那个项目做完了吗"
这种话永远说不出来 —— 而"记得你说过的事"恰恰是宠物感的重要来源。

**为什么用 JSONL 而不是向量数据库**：本机没有任何向量库，而记忆规模是
"几十到几千条"这个量级。JSONL 的好处很实在：追加写入（崩溃不会毁掉整个库）、
可人工打开检查、出问题能直接改。等记忆上万条时再换 sqlite-vec 之类也不迟，
`MemoryStore` 的接口就是为此留的。

**隐私**：这一层要特别小心。它会长期保存用户的话，所以写入前有敏感度过滤
（密码、密钥、身份证号这类一律不存），而且用户可以一键清空。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from .embedding import Embedder, HashingEmbedder, cosine, keyword_boost, keyword_overlap

log = logging.getLogger("pet.memory")


# --------------------------------------------------------------------------- #
# 条目
# --------------------------------------------------------------------------- #


@dataclass
class MemoryItem:
    """一条记忆。`kind` 决定它的可信度与保留优先级。"""

    text: str
    kind: str = "utterance"  # utterance（用户说过的话）/ fact（提炼出的长期事实）
    ts: float = field(default_factory=time.time)
    importance: float = 0.4  # 0~1，越高越不容易被容量淘汰
    source: str = "user"
    hits: int = 0  # 被检索命中过多少次（用于淘汰时判断"有没有用"）
    vector: list[float] = field(default_factory=list)
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            # 用内容 + 时间戳生成稳定 id：便于日志追踪，也便于去重时比对
            seed = f"{self.text}|{int(self.ts * 1000)}|{self.kind}"
            self.id = f"m{abs(hash(seed)) % (10**12):012d}"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryItem":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def age_days(self) -> float:
        return max(0.0, (time.time() - self.ts) / 86400.0)


@dataclass(slots=True)
class ScoredMemory:
    item: MemoryItem
    score: float


# --------------------------------------------------------------------------- #
# 敏感度过滤
# --------------------------------------------------------------------------- #

# 这些东西绝不能长期落盘。它们一旦写进记忆文件，就会在之后的每次对话里
# 被检索出来并塞进提示词 —— 等于把密钥反复送给模型供应商。
_SENSITIVE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(?i)\b(sk|pk|ghp|xox[baprs])[-_][A-Za-z0-9_\-]{16,}", "疑似 API 密钥"),
    (r"(?i)(密码|口令|passwd|password)\s*[:：=是]?\s*\S{4,}", "疑似密码"),
    (r"\b\d{15,19}\b", "疑似银行卡/身份证号"),
    (r"(?i)(api[_\s-]?key|secret|token)\s*[:：=]\s*\S{8,}", "疑似凭据"),
    (r"\b1[3-9]\d{9}\b", "疑似手机号"),
)


def sensitivity_check(text: str) -> Optional[str]:
    """
    返回命中的敏感类型，或 None 表示安全。

    故意做得保守（宁可漏存一条普通信息，也不要存下密钥）：命中就不写入记忆，
    并在日志里说明原因。用户能在日志里看到"这条我没记"，而不是默默记下。
    """
    raw = str(text or "")
    for pattern, label in _SENSITIVE_PATTERNS:
        if re.search(pattern, raw):
            return label
    return None


# --------------------------------------------------------------------------- #
# 存储层
# --------------------------------------------------------------------------- #


@dataclass
class MemoryConfig:
    enabled: bool = True
    path: Optional[Path] = None
    max_items: int = 2000
    # 检索时至少要多像才算"相关"。太低会把无关记忆也塞进提示词，
    # 让宠物说出莫名其妙的联想；太高则等于没记忆。
    min_score: float = 0.18
    top_k: int = 5
    # 去重阈值：与已有记忆超过它就不再重复写入
    duplicate_score: float = 0.92
    # 每条记忆注入提示词时的截断长度
    snippet_chars: int = 120
    # 是否记录用户的原话（关掉就只记提炼出的事实，更省也更能保护隐私）
    keep_utterances: bool = True
    # 用户画像最多保留多少条事实
    profile_max: int = 30

    @classmethod
    def from_config(cls, section: Any) -> "MemoryConfig":
        if not isinstance(section, dict):
            return cls()
        path = section.get("path")
        return cls(
            enabled=bool(section.get("enabled", True)),
            path=Path(str(path)) if path else None,
            max_items=int(section.get("max_items") or 2000),
            min_score=float(section.get("min_score") or 0.18),
            top_k=int(section.get("top_k") or 5),
            duplicate_score=float(section.get("duplicate_score") or 0.92),
            snippet_chars=int(section.get("snippet_chars") or 120),
            keep_utterances=bool(section.get("keep_utterances", True)),
            profile_max=int(section.get("profile_max") or 30),
        )


class MemoryStore:
    """
    记忆库：条目管理 + 向量检索 + 持久化。

    检索是**线性扫描**。听起来很朴素，但对几千条记忆来说每次检索只有几毫秒，
    而省掉了引入向量索引库的整套复杂度。等规模真的上来再换索引，
    接口（`search` / `add`）不用变。
    """

    def __init__(
        self,
        embedder: Optional[Embedder] = None,
        config: Optional[MemoryConfig] = None,
    ) -> None:
        self.embedder = embedder or HashingEmbedder()
        self.config = config or MemoryConfig()
        self.items: list[MemoryItem] = []
        self.profile: list[str] = []
        self.rejected_sensitive = 0
        self.rejected_duplicate = 0
        self.pruned = 0

    # ------------------------------------------------------------------ 写入

    def add(
        self,
        text: str,
        kind: str = "utterance",
        importance: float = 0.4,
        source: str = "user",
        ts: Optional[float] = None,
    ) -> Optional[MemoryItem]:
        """
        写入一条记忆。返回写入的条目；被拒绝（敏感/重复/关闭/空）时返回 None。

        这里做三件事，每一件都对应一个真实的失败模式：
          1. **敏感度过滤** —— 密钥、密码不该被长期保存并反复送进模型；
          2. **近重复去重** —— 用户重复说同一件事时不该攒出一堆副本，
             否则检索结果会被同一句话淹没；
          3. **容量淘汰** —— 内存与提示词预算都有限。
        """
        if not self.config.enabled:
            return None
        cleaned = " ".join(str(text or "").split())
        if len(cleaned) < 2:
            return None

        reason = sensitivity_check(cleaned)
        if reason:
            self.rejected_sensitive += 1
            log.info("该内容不写入长期记忆（%s）", reason)
            return None

        vector = self.embedder.embed(cleaned)

        # 去重：只跟同类条目比，但"事实"与"原话"之间也要防重
        for existing in self.items[-200:]:  # 只比最近 200 条，够用且恒定开销
            if self._is_duplicate(vector, cleaned, existing):
                self.rejected_duplicate += 1
                # 重复出现说明这件事更重要：提升权重并刷新时间
                existing.hits += 1
                existing.importance = min(1.0, existing.importance + 0.05)
                log.debug("记忆重复，已合并到已有条目：%s", cleaned[:40])
                return existing

        item = MemoryItem(
            text=cleaned,
            kind=kind,
            importance=max(0.0, min(1.0, importance)),
            source=source,
            vector=vector,
            ts=ts if ts is not None else time.time(),
        )
        self.items.append(item)
        self._enforce_capacity()
        return item

    def _is_duplicate(self, vector: list[float], text: str, existing: MemoryItem) -> bool:
        """
        判断是否近重复。

        余弦相似度是主判据；关键词重合度只用来**补充**，不能用来否决 ——
        这里曾经写错过一次：`overlap >= 0.5 or score >= 0.98` 要求余弦高**且**
        关键词重合高，结果"我在学Rust这门语言。"与"我在学 Rust 这门语言"
        （余弦 0.98）因为字符级 Jaccard 只有 0.5 多而被判为不同，去重失效。
        正确的关系是且：余弦高，且（关键词也有重合 或 余弦极高）。
        """
        if not existing.vector:
            return False
        score = cosine(vector, existing.vector)
        if score < self.config.duplicate_score:
            return False
        overlap = keyword_overlap(text, existing.text)
        return overlap >= 0.45 or score >= 0.97

    # ------------------------------------------------------------------ 检索

    def search(
        self,
        query: str,
        top_k: Optional[int] = None,
        min_score: Optional[float] = None,
        kinds: Optional[Iterable[str]] = None,
    ) -> list[ScoredMemory]:
        """
        按相关度检索记忆。返回按分数降序的列表（已过滤低分）。

        分数 = 余弦相似度 + 关键词命中提权。后者是必要的：短查询里的关键词
        会被大量无关字词稀释（"Rust 学得怎么样"余弦只有 0.15），而关键词同时
        出现在两处本身是极强的证据。只看余弦会大量漏召回。
        """
        if not self.config.enabled or not self.items:
            return []
        limit = top_k if top_k is not None else self.config.top_k
        floor = self.config.min_score if min_score is None else min_score
        wanted = set(kinds) if kinds else None

        query_vector = self.embedder.embed(query)
        scored: list[ScoredMemory] = []
        for item in self.items:
            if wanted is not None and item.kind not in wanted:
                continue
            if not item.vector:
                continue
            base = cosine(query_vector, item.vector)
            score = keyword_boost(query, item.text, base)
            if score < floor:
                continue
            scored.append(ScoredMemory(item=item, score=score))
        scored.sort(key=lambda entry: entry.score, reverse=True)

        selected = scored[:limit]
        for entry in selected:
            entry.item.hits += 1
        return selected

    # ------------------------------------------------------------------ 淘汰

    def _enforce_capacity(self) -> None:
        """
        超出容量时淘汰。

        淘汰打分 = 重要度 + 使用次数加成 - 时间衰减。刻意把"被检索过"算进去：
        经常被想起来的记忆显然更重要，而一次都没用过的长尾条目留着只是占地方。
        """
        overflow = len(self.items) - self.config.max_items
        if overflow <= 0:
            return

        def keep_score(item: MemoryItem) -> float:
            recency = 1.0 / (1.0 + item.age_days / 30.0)  # 一个月半衰
            usage = min(0.3, item.hits * 0.03)
            kind_bonus = 0.15 if item.kind == "fact" else 0.0  # 事实比原话更值得留
            return item.importance + usage + kind_bonus + recency * 0.2

        self.items.sort(key=keep_score)
        removed = self.items[:overflow]
        self.items = self.items[overflow:]
        self.pruned += len(removed)
        # 每条都打 INFO 会在持续对话时刷屏（一次淘汰一条很常见），
        # 所以只在积压明显时记录，其余降到 debug。
        log.log(
            logging.INFO if len(removed) > 3 else logging.DEBUG,
            "记忆超出上限，淘汰 %d 条（保留 %d 条）",
            len(removed),
            len(self.items),
        )

    # ------------------------------------------------------------------ 持久化

    def save(self, path: Optional[Path] = None) -> bool:
        """
        追加保存新增条目，并整体重写画像文件。

        追加而不是全量重写：记忆库会持续增长，全量重写既慢又有"写到一半崩溃
        就全丢"的风险。代价是文件里可能有已淘汰条目的残留，读取时按 id 取最新即可。
        """
        target = path or self.config.path
        if target is None:
            return False
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("w", encoding="utf-8") as handle:
                for item in self.items:
                    handle.write(item.to_json() + "\n")
            if self.profile:
                profile_path = target.with_suffix(".profile.json")
                profile_path.write_text(
                    json.dumps(self.profile, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            return True
        except OSError as exc:
            log.warning("记忆保存失败：%s", exc)
            return False

    def load(self, path: Optional[Path] = None) -> int:
        """
        从磁盘读取记忆。文件不存在是正常情况（第一次运行），不报错。

        损坏的行会被跳过而不是让整个库读不进来 —— 一条坏记录不该毁掉全部记忆。
        """
        target = path or self.config.path
        if target is None or not target.exists():
            return 0
        loaded = 0
        broken = 0
        try:
            for line in target.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = MemoryItem.from_dict(json.loads(line))
                except (json.JSONDecodeError, TypeError, ValueError):
                    broken += 1
                    continue
                if not item.text:
                    continue
                if not item.vector:
                    item.vector = self.embedder.embed(item.text)
                self.items.append(item)
                loaded += 1
        except OSError as exc:
            log.warning("记忆读取失败：%s", exc)
            return 0

        profile_path = target.with_suffix(".profile.json")
        if profile_path.exists():
            try:
                data = json.loads(profile_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self.profile = [str(entry) for entry in data]
            except (json.JSONDecodeError, OSError):
                log.debug("画像文件读取失败，忽略")

        if broken:
            log.warning("记忆文件里有 %d 行无法解析（已跳过）", broken)
        log.info("已载入 %d 条长期记忆、%d 条画像", loaded, len(self.profile))
        return loaded

    def clear(self) -> int:
        """清空全部记忆。用户可以随时这么做，这是隐私上的底线能力。"""
        count = len(self.items)
        self.items = []
        self.profile = []
        target = self.config.path
        if target is not None:
            for path in (target, target.with_suffix(".profile.json")):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        log.info("长期记忆已清空（%d 条）", count)
        return count

    # ------------------------------------------------------------------ 画像

    def add_profile_fact(self, fact: str) -> bool:
        """写入一条用户画像事实。与记忆一样做去重与敏感过滤。"""
        cleaned = " ".join(str(fact or "").split())
        if len(cleaned) < 3:
            return False
        if sensitivity_check(cleaned):
            return False
        vector = self.embedder.embed(cleaned)
        for existing in self.profile:
            if cosine(vector, self.embedder.embed(existing)) >= self.config.duplicate_score:
                return False
        self.profile.append(cleaned)
        if len(self.profile) > self.config.profile_max:
            self.profile = self.profile[-self.config.profile_max :]
        return True

    # ------------------------------------------------------------------ 查询

    def stats(self) -> dict[str, Any]:
        facts = sum(1 for item in self.items if item.kind == "fact")
        return {
            "enabled": self.config.enabled,
            "items": len(self.items),
            "facts": facts,
            "utterances": len(self.items) - facts,
            "profile": len(self.profile),
            "rejected_sensitive": self.rejected_sensitive,
            "rejected_duplicate": self.rejected_duplicate,
            "pruned": self.pruned,
            "embedder": type(self.embedder).__name__,
            "dim": getattr(self.embedder, "dim", 0),
            "path": str(self.config.path) if self.config.path else None,
        }


# --------------------------------------------------------------------------- #
# 提炼层
# --------------------------------------------------------------------------- #

# ⚠️ 这段**必须与 `config/persona.yaml` 的 `prompts.extract` 逐字一致**
#    （门禁 `tools/check-extract-prompt.py` 会断言，改一处就得改两处）。
#
# 为什么不直接用 YAML 里那份：`_extract_prompt()` 在 YAML 读不到 / 用户打错
# 花括号时会退回这里。而"提炼规则"是决定记忆质量的核心 —— 退回时如果用的
# 是旧规则，就会**静默地**重新长出 `用户在玩游戏` 这种模糊条目。
#
# ## 🔴 2026-10-17 重写（三类并记 + 禁止模糊）
#
# 用户原话：
#   「提炼出来的比如 `用户在玩游戏`、`用户关注二游` 这种太过模糊，不建议这样提炼，
#     应该由用户明确说明很喜欢或者想要过什么什么样的生活的时候提炼出来才是这种」
_EXTRACT_PROMPT_FALLBACK = (
    "从下面这段对话里，抽出**值得长期记住**的信息。分三类，都能记：\n"
    "\n"
    "【A 用户事实】用户是谁、在做什么、喜欢什么、生活状态如何。\n"
    "  例：用户在写一本玄幻小说；用户有 PS5；用户喜欢吃面包。\n"
    "\n"
    "【B 共享知识】用户告诉你的、或你们一起查到的**关于这个世界的事实**。\n"
    "  例：《银松镇》是手绘风格的生存恐怖游戏；"
    "GTA6 定在 2026-11-19 发售（2026-10 得知）。\n"
    "\n"
    "【C 共同经历】只记**有情绪或有转折**的事，不记\"又聊了一次\"。\n"
    "  例：用户第一次让宠物唱歌；用户说宠物答错了。\n"
    "\n"
    "【D 你自己的事】你做过、说过、决定过的事，**主语是\"你\"**。\n"
    "  例：你自己提出想改名叫\"阿拾\"；你那晚安慰了情绪不好的用户；\n"
    "      你承认不知道机关的正确顺序；你第一次看到自己的立绘，有点不习惯。\n"
    "\n"
    "  ⚠️ 这一类要**以\"宠物\"作主语**来写，例如「宠物自己提出改名成阿拾」。\n"
    "  它和 A 类（用户的事）必须分开 —— 否则检索时会把\"你说过的\"\n"
    "  当成\"用户的情况\"，答起话来张冠李戴。\n"
    "\n"
    "  ⚠️ 只记**有分量**的：自己做的决定、安慰/顶撞用户、承认不知道、\n"
    "  第一次经历某事。**不要记**\"宠物回答了用户的问题\"这种流水账。\n"
    "\n"
    "## 🔴 最重要的一条：必须具体，不许模糊\n"
    "\n"
    "**每一条都必须能让人知道\"具体是什么\"**。写不出具体名字的，宁可整条不记。\n"
    "\n"
    "绝对不要写这种（都是实测抓到的垃圾）：\n"
    "    用户在玩游戏            <- 哪个游戏？\n"
    "    用户关注二游            <- 哪个二游？\n"
    "    用户关注国际新闻        <- 什么新闻？\n"
    "    用户关注新游戏发售信息    <- 哪款游戏？\n"
    "    用户玩横版跑图游戏       <- 哪个？\n"
    "\n"
    "应该写成这样：\n"
    "    用户在玩《银松镇》，一款手绘风格生存恐怖游戏\n"
    "    用户在玩《明日方舟：终末地》，抽卡只抽喜欢的角色\n"
    "    用户关注 GTA6，打算等它 2026-11-19 发售\n"
    "\n"
    "如果用户只说了\"我在玩游戏\"而没说玩什么，就**不要记这一条**。\n"
    "\n"
    "## 🔴 第二重要：记**物**，不记**话**\n"
    "\n"
    "这条比\"具体\"更容易被违反，而且是实测最严重的问题（2026-10-20 抽查\n"
    "最近 40 条记忆，有 38 条是这个毛病）：\n"
    "\n"
    "    用户认为情感的温度来自\"被人接住\"的那一刻，而非情感本身\n"
    "    用户认为人生岔路上的选择由自己做出，但习惯也常替人做决定\n"
    "    用户不确定自己能否等到科技发达、买得起随身机器人的那天\n"
    "    宠物认为这段文字讲的是落不完的想念需要找地方流出去\n"
    "\n"
    "这些**都是对话记录，不是记忆**。它们的问题有三个：\n"
    "\n"
    "  ① 检索不到 —— 用户下次说\"我那个壁纸\"时，跟这些句子向量上毫无关系；\n"
    "  ② 占名额 —— 每条都挤掉一个 top_k，真正有用的那条被挤出去；\n"
    "  ③ 越积越多 —— 一场深聊能产出十几条这种，库里全是\"他说过什么\"。\n"
    "\n"
    "**判断标准：这条信息，明天、下个月、明年还用得上吗？**\n"
    "\n"
    "    用户说\"我挺喜欢曲娘，电脑壁纸都是她\"\n"
    "      -> 该记：用户喜欢《明日方舟》的曲娘，电脑壁纸是她\n"
    "      -> 不该记：用户和宠物聊到对某个角色的喜欢\n"
    "\n"
    "    用户说\"活着是为了薯片\"，宠物回了什么观感\n"
    "      -> 该记：用户喜欢\"活着是为了薯片\"这句话\n"
    "      -> 不该记：用户觉得这个回答挺有感觉（这是当时的反应，不是事实）\n"
    "\n"
    "只记**用户身上、他生活里能指着认出来的东西**：\n"
    "在玩什么、喜欢谁、有什么设备、在做什么项目、家里人、宠物、习惯。\n"
    "\n"
    "## 其他规则\n"
    "\n"
    "- **只用事实陈述，不要写\"用户可能…\"。** 用户明确说过的（买了 / 在玩 / 不喜欢）\n"
    "  就直接写。真的不确定才用\"可能\"，且这种条目优先不记 —— 宁可少记不要记错。\n"
    "- **不要写对用户的评价或心理揣测**（\"用户倾向于模糊表述\"\"用户反复问同一个问题\"\n"
    "  这类都不是事实，且会让人不舒服）。\n"
    "- **A 类里的\"喜欢/想要\"要具体到对象**：写\"用户喜欢吃面包\"，\n"
    "  不要写\"用户有自己的饮食偏好\"。\n"
    "- 知识类（B）**必须带时间基准**，因为模型不能联网：「（2026-10 得知）」。\n"
    "  涉及\"将来会发生\"的事，务必标出，免得半年后当成新闻说。\n"
    "- 不要包含密码、密钥、证件号、手机号、住址、财务状况等敏感信息。\n"
    "- 每条不超过 80 字，用第三人称陈述句。\n"
    "- 同一件事只写一条，不要换个说法重复写。\n"
    "- **不要以\"用户认为\"\"用户觉得\"\"用户表达过\"\"用户问过\"开头** ——\n"
    "  这类是转述对话，不是事实。真的值得记住的观点，也要落到**具体的对象**上：\n"
    "  写\"用户喜欢《银松镇》的结局\"，不要写\"用户认为结局的意义在于……\"。\n"
    "- **一次最多 3 条，宁缺毋滥。** 如果这段对话里只能抽出 1 条具体的，就只写 1 条。\n"
    "- 如果没有值得记的，就输出空行（**这是常态，不是失败**）。\n"
    "- **每行一条，不要编号，不要解释，不要输出 JSON。**"
)


def _extract_prompt(app_config: Any = None) -> str:
    """取"事实提炼"用的系统提示词；用户可在 `prompts.extract` 覆盖。

    ## 为什么要在运行时取，不能是模块级常量

    这段提示词决定"什么会被记住"—— 它属于「要输入给 AI 的提示词」，
    用户要求全部可改（`prompts:` 段 + 设置页）。

    ## 失败就退回内置默认

    与 `gateway._render_prompt` 同样的取舍：用户打错花括号时，
    最坏结果是"你的改动没生效"，而不是"提炼功能整个不工作"。

    `app_config` 是 `pet.config.Config`（可以没有 —— 单元测试里常传 None）。
    """
    root = getattr(app_config, "root", None)
    section = (root or {}).get("prompts") if isinstance(root, dict) else None
    raw = (section or {}).get("extract") if isinstance(section, dict) else None
    if isinstance(raw, str) and raw.strip():
        return raw
    return _EXTRACT_PROMPT_FALLBACK

_FACT_LINE = re.compile(r"^[\s\-•*\d.、)）]*(?P<fact>.+?)[。.\s]*$")

# ── 印象提炼（2026-10-17，配合「一段交流结束」的节拍）──
#
# 用户要求「一大段关联的交流结束后 AI 可自发提炼和强化」。
# 「强化」是给记忆加 importance（`store.bump_importance`），
# 「提炼」就是这里 —— 提炼**印象**，而不是再提炼一批事实。
#
# ## 印象和事实的区别（这是这段提示词存在的全部理由）
#
# ```
# 事实：用户在玩《银松镇》            -> 可检索的客观信息
# 印象：他聊到那个机关的时候语气很兴奋 -> 你对**这个人**的观察
# ```
#
# ⚠️ 印象最容易变成**贴标签**（"用户是个急躁的人"），所以规则里必须写死：
#    只写"我观察到的"，不写"我给他下的定义"。标签一旦存进去，
#    以后每轮都会强化它，最后它对你的判断就固化了 —— 那是最坏的结果。
_IMPRESSION_PROMPT_FALLBACK = (
    "下面是你和用户刚刚这一大段聊天的记录。\n"
    "\n"
    "请写下 1~2 条**你对他的印象**（最多 2 条）。\n"
    "\n"
    "## 什么算'印象'\n"
    "\n"
    "是**你观察到的东西**，不是给他的定义。\n"
    "\n"
    "要写这样（有具体场景、语气，能看出是哪一刻）：\n"
    "    他聊到那个机关的时候语气很兴奋，像是终于找到人说了\n"
    "    他今晚回得很短，一两个字就结束，可能有点累\n"
    "    他记得我上次忘了什么，会主动再提一遍\n"
    "\n"
    "**绝对不要**写这样（这是贴标签，会让他觉得被评判）：\n"
    "    用户是个话少的人\n"
    "    用户喜欢恐怖游戏（这是事实，不是印象）\n"
    "    用户性格急躁\n"
    "\n"
    "## 规则\n"
    "\n"
    "- 用「他…」或「用户…」开头都可以，但内容必须是**你看到的**；\n"
    "- 不确定的可以留余地（「可能有点累」），不要武断；\n"
    "- 不要写评价、不要写建议、不要写对未来的预测；\n"
    "- 每条不超过 40 字；\n"
    "- 如果这段聊天没什么可观察的（比如只是问了句天气），就输出空行；\n"
    "- **每行一条，不要编号，不要解释。**"
)


def _impression_prompt(app_config: Any = None) -> str:
    """取印象提炼提示词；用户可在 `prompts.impression` 覆盖（失败退回内置）。"""
    root = getattr(app_config, "root", None)
    section = (root or {}).get("prompts") if isinstance(root, dict) else None
    raw = (section or {}).get("impression") if isinstance(section, dict) else None
    if isinstance(raw, str) and raw.strip():
        return raw
    return _IMPRESSION_PROMPT_FALLBACK


# 印象的长度范围。比事实（6~80）窄得多，因为：
#   < 8 字  几乎必然是标签（"用户很累""他很急"）
#   > 40 字 变成复述对话，进提示词只会让它照着念
IMPRESSION_MIN_CHARS = 8
IMPRESSION_MAX_CHARS = 40

# 🔴 贴标签句式：一律挡掉（这是印象层最危险的污染源）
#
# 为什么必须在**解析层**挡、而不是只在提示词里说"不要写"：
# 模型不听话的情况实测很常见，而提示词约束是软的。
# 一条"用户是个急躁的人"存进库，以后每轮都会被注入、被强化。
_IMPRESSION_LABEL_PATTERNS = (
    re.compile(r"^用户(是|属于|性格|为人|本质上)"),
    re.compile(r"^他(是|属于|性格|为人)"),
    re.compile(r"用户(是个|是一个|的性格|很|非常)"),
    re.compile(r"(性格|天性|本质上|总的来说|总体来看)"),
)

_NO_IMPRESSION_MARKERS = (
    "没有可观察",
    "无印象",
    "没有印象",
    "无内容",
    "没有内容",
    "无法提炼",
)


def parse_impression_lines(text: str, limit: int = 2) -> list[str]:
    """
    解析印象输出。**比 `parse_fact_lines` 更严**，理由见文件里印象段的注释。

    ```
    · 长度必须在 8~40 字之间
    · 挡掉贴标签句式（"用户是个…""性格…"）
    · 挡掉"没有可观察的"这类说明
    · 去掉编号/项目符号
    ```
    """
    out: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _FACT_LINE.match(line)
        item = (match.group("fact") if match else line).strip()
        item = item.strip("「」“”\"'")
        if not item:
            continue
        if len(item) < IMPRESSION_MIN_CHARS or len(item) > IMPRESSION_MAX_CHARS:
            continue
        if any(marker in item for marker in _NO_IMPRESSION_MARKERS):
            continue
        if any(pattern.search(item) for pattern in _IMPRESSION_LABEL_PATTERNS):
            continue
        if item in out:
            continue
        out.append(item)
        if len(out) >= limit:
            break
    return out


# 🔴 2026-10-17：容量从 3 条/60 字 提到 **5 条/80 字**。
#
# 为什么：提炼规则从"只抽用户事实"扩成"用户事实 + 共享知识 + 共同经历"三类，
# 3 条根本不够 —— 用户事实常常就占满了，教它的知识一条都挤不进去。
#
# 为什么 80 字：一句"用户在玩《银松镇》，一款手绘风格生存恐怖游戏"是 24 字，
# 但要带上"抽卡只抽喜欢的角色"这种补充就可能到 40~50 字。60 字会把带细节的
# 条目**截断丢弃**（`len(fact) > max_chars` 是整条跳过），80 字留出余量。
#
# ⚠️ 上限不能无限加：每条记忆都会进提示词、占检索名额。
EXTRACT_LIMIT = 5
EXTRACT_MAX_CHARS = 80


def parse_fact_lines(
    text: str, limit: int = EXTRACT_LIMIT, max_chars: int = EXTRACT_MAX_CHARS,
) -> list[str]:
    """
    从模型的自由文本里抽出事实行。

    刻意只做逐行解析：要求模型输出 JSON 在真实使用中失败率不低（多一句话、
    多个逗号就解析不了），而"每行一条"这种约定几乎不会被违反。
    模型不听话时最坏结果是把一句废话当事实存下来，代价很小。

    ## ⚠️ "没有事实"的几种输出必须挡掉（2026-10-07 回填时实测）

    回填历史时模型经常**用一行字表达"这条对话里没东西可记"**，而那一行会被
    当成事实存进检索层。实测抓到的垃圾条目：

        id=19  （无长期事实可抽取）     <- 模型在解释自己
        id=24  （空行）                <- 模型在描述它输出了什么
        id=27  用户卡                  <- 3 个字，`< 3` 那条判据刚好放过它

    这些东西会占检索名额、出现在提示词里、在界面上看着像程序坏了。

    判据分三层（从窄到宽，都要**同时**满足"短"才动手，避免误杀长句）：
      · 长度 < `MIN_FACT_CHARS`（"用户卡"这类）；
      · 整行是括号/方括号包着的（`（空行）`）；
      · 短行（< 24 字）里含"没有事实可记"这类**否定式自述**。
    """
    # 4 -> 6：三个字抽不出完整事实（"用户卡"就是反例）
    MIN_FACT_CHARS = 6
    # 这些是"我没抽到东西"的自述，不是事实
    NO_FACT_MARKERS = (
        "无长期事实",
        "没有长期事实",
        "没有值得记",
        "无值得记",
        "无事实可",
        "没有事实可",
        "空行",
        "无内容",
        "无有效",
    )

    facts: list[str] = []
    for raw in str(text or "").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if any(marker in line for marker in ("规则", "例如", "没有值得记", "以下")):
            continue
        match = _FACT_LINE.match(line)
        fact = (match.group("fact") if match else line).strip()
        if len(fact) < MIN_FACT_CHARS or len(fact) > max_chars:
            continue
        # 整行被括号包着 —— 那是模型在"描述自己"，不是陈述事实
        if len(fact) >= 2 and fact[0] in "（([【" and fact[-1] in "）)]】":
            continue
        # 短句里出现"我没抽到东西"式的自述
        if len(fact) < 24 and any(m in fact for m in NO_FACT_MARKERS):
            continue
        if sensitivity_check(fact):
            continue
        if fact not in facts:
            facts.append(fact)
        if len(facts) >= limit:
            break
    return facts


class FactExtractor:
    """
    用生成模型从对话里提炼长期事实。

    两个刻意的设计：

    · **只在一轮对话结束后异步跑**，绝不在生成回复的路径上 —— 用户不该为
      "宠物记笔记"多等一秒。
    · **失败就当作没抽到**：模型不可用、返回乱码、超时都不影响对话本身。
      记忆是加分项，不是关键路径。
    """

    def __init__(self, brain: Any, config: MemoryConfig, app_config: Any = None) -> None:
        self.brain = brain
        self.config = config
        """
        `pet.config.Config`（应用级配置）。用来读 `prompts.extract` —— 提炼提示词
        也是"要输入给 AI 的提示词"，用户要求可改。

        可选：单元测试里常常没有完整的应用配置，那时会退回内置默认提示词。
        """
        self.app_config = app_config
        self.calls = 0
        self.failures = 0

    async def extract(self, turns: list[tuple[str, str]], max_facts: int = EXTRACT_LIMIT) -> list[str]:
        """turns 是 [(role, text), ...]。返回提炼出的事实列表。"""
        if not self.config.enabled or not turns:
            return []
        transcript = "\n".join(
            f"{'用户' if role == 'user' else '宠物'}：{text}" for role, text in turns if text.strip()
        )
        if len(transcript) < 8:
            return []
        return await self.extract_from_transcript(transcript, max_facts=max_facts)

    async def extract_from_transcript(self, transcript: str, max_facts: int = EXTRACT_LIMIT) -> list[str]:
        """
        从**已经拼好的对话文本**里提炼事实。

        ## 为什么要单独一个方法（2026-10-07）

        实时提炼走 `extract(turns)`；而**回填历史**（`tools/backfill-memory.py`）
        手里的东西不是"这一轮的问答对"，而是"一个会话的整段原文" ——
        它已经按 `用户：…/宠物：…` 拼好了，也按会话去过重、切过段。

        让回填去逆向拆成 `(role, text)` 再拼回来是多余的，而且会丢掉
        "这段来自哪个时间段"的信息。所以把"送进模型"这一步单独暴露出来，
        两个调用方共用同一套提示词、温度、解析与失败处理。

        ⚠️ 长度上限 `transcript[:4000]` 是 `FactExtractor` 自己的保护 ——
        调用方（回填工具）会**主动切得比它小**，否则截断会把后半段静默丢掉，
        那部分等于白付钱。
        """
        if not self.config.enabled or not transcript.strip():
            return []
        from .brains import LLMRequest, Turn  # 局部导入避免循环依赖

        if len(transcript) < 8:
            return []

        request = LLMRequest(
            system=_extract_prompt(self.app_config),
            history=[
                Turn(role="user", text=transcript[:4000]),
            ],
            user_text="请按上面的规则抽取事实。",
            temperature=0.2,  # 抽取要稳，不要创作
            max_tokens=200,
            # 🔴 **必须关思考**（2026-10-12）。
            #
            # 实测的同一个坑（用户报「没有回答」那次的原始数据）：
            #
            #     completion_tokens=200
            #     completion_tokens_details={'reasoning_tokens': 200}   <- 全在推理上
            #     正文 0 字
            #
            # 思考模式和正文**共享 `max_tokens`**。抽取事实只要 200 token，
            # 思考一开就全被推理吃掉 —— 正文一个不剩，**抽出的事实永远是空的**。
            #
            # 而这里的失败路径是 `log.debug`，用户永远看不到：
            # 表现就是「聊了很久，它什么都不记得」，而日志里一句错都没有。
            # （这正是第七十九节里「记忆检索不到东西」的一个隐藏成因。）
            #
            # ⚠️ 而且抽取本来**不需要思考** —— 它是机械的文本处理
            # （把「我最喜欢吃番茄炒蛋」变成一条事实），推理只会白烧钱。
            thinking=False,
            timeout_s=float(self.config.__dict__.get("extract_timeout_s", 20.0)),
        )
        try:
            collected: list[str] = []
            async for event in self.brain.stream(request):
                if event.get("kind") == "delta":
                    collected.append(str(event.get("text") or ""))
                elif event.get("kind") == "error":
                    raise RuntimeError(str(event.get("message")))
            self.calls += 1
        except Exception as exc:  # noqa: BLE001 —— 提炼失败不影响对话
            self.failures += 1
            log.debug("事实提炼失败（忽略）：%s", exc)
            return []
        return parse_fact_lines("".join(collected), limit=max_facts)

    def stats(self) -> dict[str, int]:
        return {"calls": self.calls, "failures": self.failures}

    # ------------------------------------------------------ 印象提炼（2026-10-17）

    async def extract_impressions(
        self,
        turns: list[tuple[str, str]],
        max_items: int = 2,
        existing: Optional[list[str]] = None,
    ) -> list[str]:
        """
        从**一整段**对话里提炼"对用户的印象"。

        ## 和 `extract()` 的分工

        ```
        extract()            每轮都跑，抽**事实**（可检索的客观信息）
        extract_impressions() 一段结束时才跑，抽**印象**（对这个人 的观察）
        ```

        ## `existing`：已有的印象，用来**防止换个说法说同一件事**

        这是必需的（2026-10-17 真机验证发现）。同一件事会在**不同段**里被
        反复观察到，于是印象表里堆满近义句：

        ```
        他今晚几次重复同样的话，自己好像没察觉，可能确实累了
        他今晚反复把同一件事说好几遍，像是自己没察觉，可能有点累或心不在焉   <- 同一件事
        ```

        ⚠️ 试过用字面相似度（字符二元组 Jaccard）去重 —— **不行**：
        上面这两句只有 **0.16** 相似度，而两条**不同**的印象也只有 0.04~0.12，
        根本分不开。同义改写的相似度天然很低，字面比对在这件事上没有分辨力。

        ✅ 所以交给模型：把已有的印象一起给它，明确要求"不要换个说法说同一件事"。
           语义判断是模型擅长的事，而字面相似度不是。

        ## 为什么必须严格解析

        印象会被注入提示词、会一直存在。一条"用户性格暴躁"这种**标签**
        会污染后续所有判断。所以解析比事实那层更严（见 `parse_impression_lines`）。

        ⚠️ 失败返回空列表，绝不抛异常 —— 段结束的处理不能因为提炼失败而中断。
        """
        if not self.config.enabled or not turns:
            return []
        transcript = "\n".join(
            f"{'用户' if role == 'user' else '宠物'}：{text}"
            for role, text in turns
            if str(text or "").strip()
        )
        if len(transcript) < 40:
            # 太短的段不值得提炼印象（"今天天气不错"这种没有可观察的）
            return []

        from .brains import LLMRequest, Turn  # 局部导入避免循环依赖

        # 把已有的印象写进**用户消息**（而不是 system），
        # 因为 system 是"规则"，user 是"这次的材料"。
        user_parts = [transcript[:4000]]
        known = [str(x).strip() for x in (existing or []) if str(x or "").strip()]
        if known:
            user_parts.append(
                "\n\n【你已经写过的印象 —— 不要重复，也不要换个说法说同一件事】\n"
                + "\n".join(f"- {item}" for item in known[:12])
                + "\n\n如果你这次想写的和上面某一条是同一件事，就写别的；"
                "实在没有新的，就输出空行。"
            )

        request = LLMRequest(
            system=_impression_prompt(self.app_config),
            history=[Turn(role="user", text="\n".join(user_parts))],
            user_text="请按上面的规则写下你对他的印象。",
            temperature=0.6,
            max_tokens=220,
            # 与事实提炼同样的理由：思考模式会吃掉全部 token，正文一个不剩。
            thinking=False,
            timeout_s=float(self.config.__dict__.get("extract_timeout_s", 20.0)),
        )
        try:
            collected: list[str] = []
            async for event in self.brain.stream(request):
                if event.get("kind") == "delta":
                    collected.append(str(event.get("text") or ""))
                elif event.get("kind") == "error":
                    raise RuntimeError(str(event.get("message")))
            self.calls += 1
        except Exception as exc:  # noqa: BLE001 —— 提炼失败不影响任何主流程
            self.failures += 1
            log.debug("印象提炼失败（忽略）：%s", exc)
            return []
        return parse_impression_lines("".join(collected), limit=max_items)


# --------------------------------------------------------------------------- #
# 注入层
# --------------------------------------------------------------------------- #


class MemoryManager:
    """
    把记忆接进对话：检索相关条目 + 用户画像，拼成一段提示词上下文。

    注入时特别克制，原因有两个：
      · 提示词预算有限，塞太多会让宠物跑题；
      · 记忆可能相关度不高，硬塞进去会让它说出莫名其妙的联想（"你上次说……"
        然后说错了）。所以宁可只给两三条高分的。
    """

    def __init__(
        self,
        store: Optional[MemoryStore] = None,
        extractor: Optional[FactExtractor] = None,
        config: Optional[MemoryConfig] = None,
    ) -> None:
        self.store = store or MemoryStore()
        self.extractor = extractor
        self.config = config or self.store.config
        self.injections = 0
        # 有改动但还没落盘。
        #
        # 这个标志不是优化，是**功能完整性**的一部分：早先的实现只提供 save()，
        # 但没有任何地方调用它 —— 记忆写进了内存、检索也正常，看起来一切都对，
        # 直到重启才发现全丢了。加一个脏标志并让上层在合适的时机 sync()，
        # 才能保证"记住了"是真的记住了。
        self.dirty = False
        self.saves = 0
        self._dirty_since: Optional[float] = None

    # ------------------------------------------------------------------ 落盘

    def mark_dirty(self) -> None:
        self.dirty = True
        # 记下第一次变脏的时刻，用于周期性落盘（见 run_periodic_sync）
        if self._dirty_since is None:
            self._dirty_since = time.time()

    def sync(self, force: bool = False) -> bool:
        """
        把未落盘的改动写进文件。没改动时什么都不做（避免频繁写盘）。

        调用时机由上层决定：一轮对话结束后、以及进程退出前。
        """
        if not self.dirty and not force:
            return False
        if self.config.path is None:
            self.dirty = False
            return False
        ok = self.store.save()
        if ok:
            self.dirty = False
            self._dirty_since = None
            self.saves += 1
        return ok

    async def run_periodic_sync(self, interval_s: float = 30.0) -> None:
        """
        周期性落盘。

        为什么需要它：原话可能攒很久都没有新事实（只有抽到事实时才顺手落盘），
        而用户可能聊完就直接关掉窗口 —— 极端情况下连退出钩子都来不及跑。
        定期写一次把"最多丢一点"变成"最多丢几十秒"，代价只是偶尔一次文件写入。
        """
        log.info("记忆定时落盘已启动（每 %.0fs，仅在有待写内容时实际写盘）", interval_s)
        while True:
            await asyncio.sleep(max(5.0, interval_s))
            if self.dirty:
                if self.sync():
                    log.debug("记忆已定期落盘（%d 条）", len(self.store.items))
                else:
                    log.warning("记忆定期落盘失败（稍后重试）")

    # ------------------------------------------------------------------ 检索

    def recall_hint(self, query: str) -> str:
        """
        为一次发言拼装"你还记得这些"的提示词片段。没有相关记忆时返回空串
        —— 这一点很重要：不留痕迹，比说一句"我没有相关记忆"自然得多。
        """
        if not self.config.enabled:
            return ""
        pieces: list[str] = []

        if self.store.profile:
            # 画像每条都短，全给也就一两行，但它对"像不像认识你"影响最大
            joined = "；".join(self.store.profile[-12:])
            pieces.append(f"你记得关于用户的事：{joined}。")

        hits = self.store.search(query, top_k=max(2, self.config.top_k - 2))
        if hits:
            lines = []
            for entry in hits:
                snippet = entry.item.text[: self.config.snippet_chars]
                lines.append(f"- {snippet}")
            pieces.append("你想起以前聊到过的相关内容：\n" + "\n".join(lines))

        if not pieces:
            return ""
        self.injections += 1
        pieces.append(
            "这些记忆仅供参考。**只在确实相关时自然提起**，不要生硬复述，"
            "也不要假装记得没提到的事。"
        )
        return "\n\n".join(pieces)

    # ------------------------------------------------------------------ 写入

    def remember_utterance(self, user_text: str) -> Optional[MemoryItem]:
        """
        立刻记下用户的原话。**必须在生成回复之前调用**：

        · 敏感度过滤要在落盘前完成（密钥绝不进记忆文件）；
        · 同一轮里如果要检索记忆，刚说的话应当已经可被检索到。
        事实提炼是另一件事，见 `extract_facts`（那个才适合异步）。
        """
        if not self.config.keep_utterances:
            return None
        item = self.store.add(user_text, kind="utterance", importance=0.35)
        if item is not None:
            self.mark_dirty()
        return item

    async def extract_facts(self, user_text: str, reply_text: str = "") -> list[str]:
        """
        异步提炼长期事实。在一轮对话**结束后**调用，绝不在生成路径上 ——
        用户不该为"宠物记笔记"多等一秒。
        """
        if not self.config.enabled or self.extractor is None:
            return []
        turns = [("user", user_text)]
        if reply_text:
            turns.append(("assistant", reply_text))
        facts = await self.extractor.extract(turns)
        stored: list[str] = []
        for fact in facts:
            if self.store.add(fact, kind="fact", importance=0.7):
                self.store.add_profile_fact(fact)
                stored.append(fact)
        if stored:
            log.info("记住 %d 条关于用户的事实：%s", len(stored), "；".join(stored))
            self.mark_dirty()
            # 抽到新事实就顺手落盘：这些是最有价值的内容，
            # 不该等到进程退出才写（崩溃就没了）。
            self.sync()
        return stored

    async def remember_turn(self, user_text: str, reply_text: str = "") -> list[str]:
        """便捷方法：存原话 + 提炼事实。主要用于测试与脚本化调用。"""
        self.remember_utterance(user_text)
        return await self.extract_facts(user_text, reply_text)

    def stats(self) -> dict[str, Any]:
        result = self.store.stats()
        result["injections"] = self.injections
        if self.extractor is not None:
            result["extractor"] = self.extractor.stats()
        return result
