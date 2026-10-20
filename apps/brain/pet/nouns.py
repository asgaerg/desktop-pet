r"""专有名词提取（2026-10-17）。

## 一个提取器，三个用途

用户的原话是「爱好由对话形成，利用检索层自发形成」。但要"自发形成爱好"，
得先能**认出对话里反复出现的那个东西**。所以这个模块提供三处共用的底层能力：

```
① 兴趣表      「银松镇」在对话里反复出现 -> 成为它的关注点
② 检索兜底    §114 改动 4：语义检索之外，专有名词命中直接进候选
③ 矛盾检测    §114 改动 5：两条记忆有没有共用同一个专有名词
```

三处各写一遍正则会各不相同（而且各漏一半），所以抽出来。

## 认哪些算「专有名词」

| 形态 | 例子 | 可靠性 |
|---|---|---|
| 书名号 | `《银松镇》` | **极高** —— 用户已经标注了"这是名字" |
| 引号 | `「转盘机关」` `"Silver Pines"` | **极高** |
| 日期 | `2026-11-19` | 高 |
| 版本号 | `v1.2.3` | 高 |
| ASCII 名 | `GTA6` `PS5` `ILL` | 高 |
| CJK 片段 | `银松镇`（**不带引号时**） | **低** —— 靠分词 + 停用词表 |

## 🔴 为什么不 `import jieba`

本项目**没有 Python 的 jieba**（`pip list` 里没有，`requirements` 里也没有）。
中文分词一直走的是 `simple.dll` 里的 **cppjieba**（SQLite 扩展，用
`jieba_query()` 这个 SQL 函数）。所以这里**不引入新依赖**，改成"注入分词器"：

```python
# memory2.py 里这样接（它的连接已经加载了 simple.dll）
nouns.set_segmenter(lambda text: 从 jieba_query 的输出解析)
```

⚠️ **没有注入分词器时的行为是"只提引号/ASCII/日期"，CJK 无引号片段一律跳过。**
这是刻意选择：**宁可漏，也不要放进去"最近""今天"这种词** ——
那正是用户抱怨的「太过模糊」的来源。

## ⚠️ 为什么不用 `jieba.posseg` 的 `nr`/`ns` 标注

一是不在本项目依赖里；二是它对**作品名**基本无效（"银松镇"会被标成地名或普通
名词，"明日方舟终末地"会被切碎）。而引号/书名号是作者给的**强信号**，
比词性猜测可靠。
"""

from __future__ import annotations

import re
from typing import Callable, Iterable, Optional

# ── 引号包裹的片段 ──
_QUOTED = re.compile(
    r"《([^》]{1,40})》"          # 《银松镇》
    r"|「([^」]{1,40})」"        # 「转盘机关」
    r"|“([^”]{1,40})”"          # “Silver Pines”
    r"|\"([^\"]{1,40})\"",       # "Silver Pines"
)

# ── ASCII 连续字母数字：GTA6 / PS5 / ILL / Xbox ──
# 至少含一个字母（纯数字是数量，不是名字）
_ASCII_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]{0,30}")

# ── 日期 / 版本号 ──
_DATE = re.compile(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?")
_VERSION = re.compile(r"\bv?\d+\.\d+(?:\.\d+)*\b")

# ── CJK 连续片段 ──
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")

# ── 停用词 ──
#
# ⚠️ 这份表**决定误报率**。原则：凡是"说明时间/程度/泛指"的词全禁。
# 宁可漏掉真名字，也不要让"今天""最近"变成它的兴趣。
STOPWORDS: frozenset[str] = frozenset(
    """
    今天 昨天 明天 前天 后天 刚才 刚刚 现在 最近 以后 之前 之后 当时 那时
    时候 时间 一直 有点 一点 一些 一个 这个 那个 这些 那些 这样 那样 什么
    怎么 为什么 因为 所以 但是 可是 而且 如果 虽然 然后 就是 还是 也是
    可以 可能 应该 需要 觉得 感觉 知道 不知 好像 似乎 大概 也许
    我们 你们 他们 她们 它们 自己 别人 大家 东西 事情 问题 地方 情况
    比较 非常 特别 真的 确实 其实 反正 总之 而已 罢了 之类 等等
    用户 宠物 你好 谢谢 再见 好的 嗯嗯 哈哈 嘿嘿 随便 无所谓 差不多
    我查 我玩 我在 我想 我是 我不 我没 我要 我会 我去 我看 我说
    在家 玩玩 一下 那个 这个 了 的 是 在 和 与 或 也 都 就 还 又 很
    游戏 视频 音乐 小说 电影 动漫 工作 学习 生活 电脑 手机 屏幕 桌面
    """.split()
)

# 单独出现时不足以成为兴趣的泛词
WEAK_TERMS: frozenset[str] = frozenset(
    "游戏 视频 音乐 小说 电影 动漫 玩法 角色 版本 剧情 属性 装备 任务 活动 内容".split()
)

# ── 🔴 记忆模板里的谓语词（实测必须禁）──
#
# 这些是**提炼模板自带的动词**，不是用户的内容。实测在 42 条真实记忆上，
# 不加这份表时提取结果的前几名全是它们：
#
#     8x 喜欢   4x 关注   4x 风格   3x 恐怖   3x 感兴趣
#     2x 习惯   2x 通过   2x 偏好   2x 使用
#
# 也就是说：**提取器把"用户喜欢X"里的"喜欢"当成了兴趣**。
# 这份表就是针对这个的补丁，名字直白点叫"模板动词"。
TEMPLATE_VERBS: frozenset[str] = frozenset(
    """
    喜欢 关注 偏好 习惯 使用 通过 认为 打算 要求 自称 容易 经常 常常 常
    感兴趣 会 在 有 买 买了 玩 玩过 看 看过 听 听过 会看 会听 会玩 在玩
    开发 了解 获取 观看 收集 结局 预告 信息 版本 新版本 功能 模式
    风格 彩色 横版 手绘 生存 恐怖 解谜 动作 射击 开放世界 银河 恶魔城
    类 类型 这类 这款 这个 那个 一个 一些 有点 更好 不宽裕
    男性 女性 名字 网名 台词 幽默 古诗文 人生 意义 哲学 句子
    服务 本地 页面 调试 沉浸 输入 语音 字幕 识别 表达 交流 互动
    """.split()
)

MIN_CJK_LEN = 2
MAX_TERM_LEN = 40

# 不带引号的 CJK 词至少要在这么多条不同文本里出现过，才算专有名词。
#
# ⚠️ 为什么需要这个门槛：中文分词切出来的"好久""这款""有点累"看起来都像词，
# 但只有**反复出现**的那个才是真有意义的关注点。引号/ASCII/日期不需要这个门槛
# ——它们本身就是强信号。
MIN_CJK_COUNT = 2

# 注入的分词器（memory2 会接上 simple.dll 的 cppjieba）
_SEGMENTER: Optional[Callable[[str], list[str]]] = None


def set_segmenter(fn: Optional[Callable[[str], list[str]]]) -> None:
    """
    注入中文分词器。传 `None` 取消。

    ⚠️ 为什么用"注入"而不是直接 import：本项目没有 Python jieba，
    分词能力在 `simple.dll`（cppjieba）里，而那个扩展只能从
    **已经加载它的 SQLite 连接**上调用。所以由拥有连接的模块
    （`memory2`）把能力注进来。
    """
    global _SEGMENTER
    _SEGMENTER = fn


def _norm(text: str) -> str:
    return str(text or "").strip().strip("《》「」“”\"'").strip()


def _split_cjk(run: str) -> list[str]:
    """切中文片段。没注入分词器时返回空（宁可漏）。"""
    if _SEGMENTER is None:
        return []
    try:
        pieces = _SEGMENTER(run)
    except Exception:  # noqa: BLE001 —— 分词是加分项，绝不能让它拖垮调用方
        return []
    out: list[str] = []
    for piece in pieces or []:
        piece = str(piece).strip()
        if len(piece) < MIN_CJK_LEN:
            continue
        if piece in STOPWORDS or piece in TEMPLATE_VERBS or piece in WEAK_TERMS:
            continue
        if piece not in out:
            out.append(piece)
    return out


# 强信号来源（不需要"出现多次"这个门槛）
_STRONG = "strong"
# 弱信号来源（不带引号的中文，要配合出现次数）
_WEAK = "weak"


def extract_ranked(texts: Iterable[str]) -> list[tuple[str, str, int]]:
    """
    从多条文本里抽专有名词，带**信号强弱**和出现次数。

    返回 `[(词, 'strong'|'weak', 出现条数)]`，按出现次数降序。

    这是"兴趣自发形成"的入口：只有 `strong`（引号/ASCII/日期）
    或者出现 ≥ `MIN_CJK_COUNT` 次的 `weak` 才值得变成兴趣。

    ⚠️ 为什么返回强弱而不是直接过滤：调用方（兴趣表）需要知道
    "这个词有多可信"，好决定初始权重。直接过滤掉就丢了这个信息。
    """
    counts: dict[str, int] = {}
    order: list[str] = []
    kind: dict[str, str] = {}

    def bump(term: str, strength: str) -> None:
        if term not in counts:
            counts[term] = 0
            order.append(term)
            kind[term] = strength
        counts[term] += 1
        # 强信号覆盖弱信号（同一个词在别处以引号出现 -> 整体算强）
        if strength == _STRONG:
            kind[term] = _STRONG

    for text in texts:
        raw = str(text or "")
        if not raw.strip():
            continue
        seen_here: set[str] = set()

        # 强信号
        for match in _QUOTED.finditer(raw):
            term = _norm(next(g for g in match.groups() if g))
            if term and len(term) <= MAX_TERM_LEN and term not in seen_here:
                seen_here.add(term)
                bump(term, _STRONG)
        for regex in (_DATE, _VERSION):
            for match in regex.finditer(raw):
                term = match.group(0)
                if term not in seen_here:
                    seen_here.add(term)
                    bump(term, _STRONG)
        for match in _ASCII_NAME.finditer(raw):
            token = match.group(0)
            if len(token) >= 2 and token not in seen_here:
                seen_here.add(token)
                bump(token, _STRONG)

        # 弱信号（CJK）
        for run in _CJK_RUN.findall(raw):
            for piece in _split_cjk(run):
                if piece not in seen_here:
                    seen_here.add(piece)
                    bump(piece, _WEAK)

    ranked = [(t, kind[t], counts[t]) for t in order]
    ranked.sort(key=lambda item: (-item[2], -len(item[0])))
    return ranked


def extract_terms(text: str, *, include_weak: bool = False) -> list[str]:
    """
    从一段文本里抽出专有名词，**按出现顺序去重**。

    `include_weak=False` 时过滤 `WEAK_TERMS`（如"游戏"）——
    "喜欢玩游戏"不是兴趣，"喜欢银松镇"才是。
    """
    raw = str(text or "")
    if not raw.strip():
        return []

    found: list[str] = []

    def push(value: str) -> None:
        term = _norm(value)
        if not term or len(term) > MAX_TERM_LEN:
            return
        # 纯标点/空白丢掉
        if not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", term):
            return
        if term in found:
            return
        if not include_weak and term in WEAK_TERMS:
            return
        found.append(term)

    # ① 引号/书名号里的永远算（作者已经标注了"这是个名字"）
    for match in _QUOTED.finditer(raw):
        push(next(g for g in match.groups() if g))

    # ② 日期、版本号
    for match in _DATE.finditer(raw):
        push(match.group(0))
    for match in _VERSION.finditer(raw):
        push(match.group(0))

    # ③ ASCII 名字：GTA6 / PS5 / ILL
    # ⚠️ 要求 ≥2 字符且含字母，否则会把"I"、"a"这种当名字。
    for match in _ASCII_NAME.finditer(raw):
        token = match.group(0)
        if len(token) >= 2 and token not in STOPWORDS:
            push(token)

    # ④ CJK 片段：交给注入的分词器
    for run in _CJK_RUN.findall(raw):
        for piece in _split_cjk(run):
            push(piece)

    return found


def extract_terms_multi(
    texts: Iterable[str], *, include_weak: bool = False
) -> list[str]:
    """多段文本合并提取，保持首次出现顺序。"""
    merged: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for term in extract_terms(text, include_weak=include_weak):
            if term not in seen:
                seen.add(term)
                merged.append(term)
    return merged


def shared_terms(a: str, b: str) -> list[str]:
    """
    两条文本**共用**的专有名词（矛盾检测用）。

    ⚠️ 大小写不敏感：`GTA6` 与 `gta6` 是同一个东西。
    中文没有大小写，不受影响。
    """
    left = {t.lower() for t in extract_terms(a, include_weak=True)}
    right = {t.lower() for t in extract_terms(b, include_weak=True)}
    common = left & right
    if not common:
        return []
    ordered: list[str] = []
    for term in extract_terms(a, include_weak=True):
        if term.lower() in common and term not in ordered:
            ordered.append(term)
    return ordered


def parse_jieba_query(expr: str) -> list[str]:
    """
    把 `jieba_query()` 的输出解析成词表。

    输入形态（simple.dll 的实际输出）：

        '"我" AND "最近" AND "在" AND "玩" AND "银松镇" AND "2026"*'

    ⚠️ 两处要处理：
      ① 尾部有 `*`（前缀匹配标记），要去掉；
      ② 单字（"在"、"的"）会被 `_split_cjk` 的长度过滤掉，这里不用管。
    """
    out: list[str] = []
    for piece in str(expr or "").split("AND"):
        token = piece.strip().strip('"').strip()
        token = token.rstrip("*").strip()
        if not token:
            continue
        token = token.strip('"').strip()
        if token and token not in out:
            out.append(token)
    return out


# ────────────────────────────────────────────── 矛盾检测（§114 改动 5，2026-10-20）
#
# 用户明确要求：「矛盾检测只在检索层对比」。所以这份表只服务
# `memory2.remember_facts()` 的"写入前查矛盾"这一步，**不**给提炼提示词用。
#
# ## 为什么按"成对"判，而不是按"有没有否定词"
#
# 真实条目长这样（都是库里实际出现过的形态）：
#
#     用户在打《银松镇》
#     用户还没开玩《银松镇》          <- 矛盾
#     用户买了《银松镇》              <- 不矛盾（补充信息）
#     用户在打《银松镇》，被转盘机关卡住  <- 不矛盾（同向，更详细）
#
# 只看"有没有'没'"会把第三条判成矛盾吗？不会；但会把
# 「用户不喜欢《银松镇》的结局」判成与「用户喜欢《银松镇》」矛盾 —— 那是对的。
# 真正的风险在反方向：**同向的两条被误判**。所以每个词都要有明确的
# 反义项才算一"对"，找不到对就不判（宁可并存）。
#
# ⚠️ 顺序有意义：先匹配长的（"还没开玩"必须先于"玩"被看到），
#    否则「还没开玩」会因为含"玩"而被判成"在玩"，两条同向 —— 永远不会报矛盾。
POLARITY_PAIRS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    # (维度名, 正面说法, 反面说法)
    (
        "playing",
        ("在玩", "在打", "正在玩", "开玩了", "通关了", "玩过", "玩通了"),
        ("还没开玩", "还没玩", "没玩过", "没玩", "没通关", "没打通", "弃坑了"),
    ),
    (
        "owning",
        ("买了", "入手了", "已购买", "已经在玩"),
        ("没买", "没入手", "还没买", "没购买", "不打算买"),
    ),
    (
        "liking",
        ("喜欢", "爱玩", "挺喜欢", "很喜欢", "感兴趣"),
        ("不喜欢", "讨厌", "不感兴趣", "不爱玩", "弃了"),
    ),
    (
        "having",
        ("有", "拥有", "已经装了", "装好了"),
        ("没有", "还没安装", "没装", "没安装", "没有装"),
    ),
)


def polarity_of(text: str) -> tuple[str, str] | None:
    """判断一条记忆在**哪个维度**上是正还是负。

    返回 `(维度, "pos"|"neg")`，判不出来返回 `None`。

    ## 判定顺序：先找否定，再找肯定

    这是**刻意**的，不是随手写的：

        "用户还没玩《银松镇》"   含有"玩"（正面词）也含有"还没玩"（反面词）
                                  -> 必须先命中反面，否则两条同向、永远不报矛盾
        "用户不喜欢《银松镇》"   含"喜欢"和"不喜欢" -> 同理

    所以每个维度内**反面词优先**。而且反面词按长度降序匹配，
    保证"还没开玩"不会被更短的"没玩"抢先（两者都是反面，结果一样，但如果
    将来加了"没玩过但预购了"这种，顺序就要紧）。

    ## ⚠️ 一条只能有一个维度

    返回单个维度而不是集合：矛盾检测要求"同一维度、方向相反"。
    如果一条同时提到"喜欢"和"没买"（「喜欢《银松镇》但还没买」），
    拿哪个？—— 实测这类复合句出现得少，且**误判代价高**（可能把一条
    很真实的信息归档掉）。所以取"最先出现的那个说法"，并且同一个维度里
    正反都出现时返回 `None`（自己跟自己矛盾，交给用户看，不自动处理）。
    """
    raw = str(text or "")
    if not raw:
        return None
    best: tuple[int, str, str] | None = None  # (位置, 维度, 极性)
    for name, pos_words, neg_words in POLARITY_PAIRS:
        pos_pos = _first_index(raw, pos_words)
        neg_pos = _first_index(raw, neg_words)
        if pos_pos is not None and neg_pos is not None:
            # 同一个维度里正反都出现了（"不喜欢"里含"喜欢"）。
            #
            # ⚠️ 这里**不是**直接丢掉 —— 那会让「用户不喜欢《X》的结局」
            # 整条判不出极性（实测），而它恰恰是最该被抓到的一类。
            # 规则：**否定词覆盖了肯定词**（否定出现在肯定之前，且两者相隔很近）
            # 就当反面；否则认为是两句不同的评价，返回 None 不参与判断。
            if neg_pos <= pos_pos and pos_pos - neg_pos <= _NEGATION_REACH:
                candidate = (neg_pos, name, "neg")
            else:
                continue
        elif neg_pos is not None:
            candidate = (neg_pos, name, "neg")
        elif pos_pos is not None:
            candidate = (pos_pos, name, "pos")
        else:
            continue
        if best is None or candidate[0] < best[0]:
            best = candidate
    if best is None:
        return None
    return best[1], best[2]


# 否定词能"管到"后面多远的肯定词。
#
# 「不喜欢」：否定词"不喜欢"本身含"喜欢"，相隔 0 —— 必须覆盖。
# 「没买…但很喜欢」：相隔很远，是两个评价，不该合并 —— 所以设个上限。
# 实测中文记忆条目很短（`max_chars=200`，实际大多 <40 字），
# 6 个字足够覆盖"不喜欢X的游戏""没买X但是"这类，又不会被长句误并。
_NEGATION_REACH = 6


def _first_index(text: str, words: tuple[str, ...]) -> int | None:
    """第一个命中的词的位置（越靠前越可能是主谓）。"""
    found: int | None = None
    for word in sorted(words, key=len, reverse=True):
        index = text.find(word)
        if index >= 0 and (found is None or index < found):
            found = index
    return found


def is_contradiction(old_text: str, new_text: str) -> tuple[bool, str]:
    """两条记忆是否**互相矛盾**（§114 改动 5 的判据）。

    返回 `(是否矛盾, 人话原因)`。

    ## 两个条件必须**同时**满足

    ```
    ① 共用同一个专有名词   —— 否则"用户在打A"和"还没买B"会被判成矛盾
    ② 同一维度、极性相反   —— 否则"在打"和"买了"（都是补充信息）会被误杀
    ```

    缺任何一个都会误杀，而误杀的代价是"一条真的信息被归档"。
    所以这里**宁可漏判**：判不出来就并存，等以后有更好的办法再说。

    ⚠️ 纯文本、不走向量。理由见 `memory2._find_contradiction` 的说明：
    嵌入不可用时向量路会**静默降级**，而这种"悄悄失效"最难发现。
    """
    shared = shared_terms(old_text, new_text)
    if not shared:
        return False, ""
    left = polarity_of(old_text)
    right = polarity_of(new_text)
    if left is None or right is None:
        return False, ""
    if left[0] != right[0]:
        # 不同维度（比如一条说"买了"、一条说"不喜欢"）—— 补充信息，不是矛盾
        return False, ""
    if left[1] == right[1]:
        return False, ""
    return True, f"共用「{shared[0]}」，{_DIM_LABEL.get(left[0], left[0])}相反"


_DIM_LABEL = {
    "playing": "玩没玩",
    "owning": "买没买",
    "liking": "喜不喜欢",
    "having": "有没有",
}
