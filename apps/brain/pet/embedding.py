"""
零依赖的文本嵌入与相似度。

为什么不用现成的嵌入模型：本机没有 sentence-transformers / torch / onnxruntime，
也没有任何向量库，而下载模型权重在这个网络环境里基本不可行。但"长期记忆"这件事
**不能因此不做** —— 用户说过的重要事情必须能被想起来。

所以这里用经典的**哈希技巧**（feature hashing）自己造一个够用的向量空间：

    文本 -> 字符 n-gram 集合 -> 带符号哈希到固定维度 -> L2 归一化

它当然不如真正的神经嵌入（语义相近但用词不同的句子匹配不到），但它有三个
对桌宠来说更重要的性质：

  1. **零依赖、零下载、零显存** —— 立刻可用，永不因为环境而失效。
  2. **完全确定** —— 同样的文本永远得到同样的向量，可测试、可复现、可持久化。
  3. **对中文有效** —— 用字符 n-gram 而不是词，天然绕开中文分词。

而且这是**可替换的一层**：`Embedder` 是接口，将来装上真模型只要实现同样的
`embed()` / `similarity()` 就能平滑升级，记忆库里的旧向量重新算一遍即可。
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from pathlib import Path
from typing import Iterable, Optional, Protocol

log = logging.getLogger("pet.embedding")

# 默认维度。256 维对"几十到几千条记忆"的规模足够：维度太低会撞哈希，
# 太高则白占内存（每条记忆一个 256 维 float 列表约 2KB）。
DEFAULT_DIM = 256

# 字符 n-gram 的长度范围。1-gram 抓住字面重合，2/3-gram 抓住短语结构。
# 只用 1-gram 会太粗糙（"今天"和"天今"都会匹配），只用 3-gram 又太脆（错一个字就全不匹配）。
_NGRAM_SIZES = (2, 3)

# 常见虚词与标点：去掉它们能显著提升信噪比（"的/了/是"这类字几乎出现在所有句子里，
# 保留它们只会让所有记忆互相似）。
_STOPWORDS = frozenset(
    "的 了 是 在 我 你 他 她 它 们 和 与 及 或 也 就 都 而 但 还 又 很 太 更 最 把 被 让 给 对 从 到 上 下 里 外 这 那 有 没 不 要 会 能 可以 什么 怎么 为什么 吗 呢 吧 啊 呀 哦 嗯".split()
)
_PUNCT = re.compile(r"[\s，。！？、；：,.!?;:'\"“”‘’()（）\[\]【】<>《》\-—~·|/\\]+")


class Embedder(Protocol):
    """文本嵌入接口。实现者必须保证：相同文本 -> 相同向量（可持久化、可测试）。"""

    dim: int

    def embed(self, text: str) -> list[float]:  # pragma: no cover
        ...


def _is_cjk_char(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def _split_by_script(text: str) -> list[str]:
    """
    按字符类型切分，汉字逐字独立、拉丁字母/数字连续成词。

    这里刻意**不用正则**：早先用 `re.sub(r"([\\u4e00-\\u9fff])([a-z0-9])|...", r"\\1 \\2", ...)`
    拼接，因为分支里有未参与匹配的分组，结果把词尾吃掉了 ——
    "rust" 变成了 "rus"，而且悄无声息（相似度只是变差，不报错）。
    这种"静默截断"最难查，所以改成一眼能看懂的逐字符扫描。
    """
    tokens: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            tokens.append("".join(buffer))
            buffer.clear()

    for ch in text:
        if _is_cjk_char(ch):
            flush()
            tokens.append(ch)
        elif ch.isalnum():
            buffer.append(ch)
        else:
            flush()
    flush()
    return tokens


def normalize_text(text: str) -> str:
    """
    归一化：小写、去标点、去虚词，并**按字符类型统一分词**。

    最后一步很关键。中文与拉丁文混排时，空格的位置是不稳定的：
    "我在学 Rust 这门语言" 与 "我在学Rust这门语言" 是同一句话，但按空格分词
    会得到两套不同的特征，余弦相似度掉到 0.63，去重因此失效（实测踩过）。

    所以这里主动重排：汉字逐字独立，拉丁/数字连续成词。这样无论用户有没有敲空格，
    分词结果都一致 —— 归一化必须做到这一点，否则所有依赖"相同文本产生相同向量"
    的逻辑都会在这里漏掉。
    """
    lowered = str(text or "").lower()
    cleaned = _PUNCT.sub(" ", lowered)
    tokens = [token for token in _split_by_script(cleaned) if token not in _STOPWORDS]
    return " ".join(tokens)


def _is_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def ngrams(text: str, sizes: Iterable[int] = _NGRAM_SIZES) -> list[str]:
    """取字符 n-gram 字符串（无权重）。保留它因为关键词重合度计算要用。"""
    return [feature for feature, _weight in weighted_features(text, sizes)]


# 特征权重。这个映射是调出来的，不是拍的：
#   · 整词/整串匹配是**强信号** —— "rust" 出现在查询和记忆里，基本就是同一件事；
#   · 字符 2-gram 是主力，但短查询字符 n-gram 太少，不能让它独占话语权；
#   · 字符 3-gram 更精确但也更脆（错一个字就全不匹配），权重压低。
# 不这样分层的话，短查询（"猫"、"桌面宠物"）几乎检索不到任何东西 —— 实测过：
# 未分层时 "猫" vs "我们家的猫叫豆豆" 只有 0.24，"桌面宠物" 甚至是负数。
_WEIGHT_WORD = 1.0
_WEIGHT_BIGRAM = 0.45
_WEIGHT_TRIGRAM = 0.3
# 词内单字：权重最低。它解决的是**跨词边界**的匹配问题 ——
# 查询里的"桌面"是独立词，而记忆里的"桌面宠物"是一个词，整词特征对不上，
# 但两者共享汉字。没有这一层时 "桌面宠物" → "…桌面宠物…" 居然检索不到。
_WEIGHT_CHAR = 0.28


def weighted_features(text: str, sizes: Iterable[int] = _NGRAM_SIZES) -> list[tuple[str, float]]:
    """
    产出 (特征, 权重) 对。权重决定了它对相似度的贡献。

    分四层，逐层更鲁棒：
      1. 整串归一化文本（最高）—— 精确命中；
      2. 整词（含相邻词对）—— 关键词命中，短查询主要靠这一层；
      3. 词内单字（低）—— 跨词边界的局部匹配；
      4. 字符 n-gram —— 中文短语结构，抗错字与语序变化。
    """
    normalized = normalize_text(text)
    if not normalized:
        return []
    compact = normalized.replace(" ", "")
    words = normalized.split()

    features: list[tuple[str, float]] = []
    if len(compact) >= 2:
        features.append((f"#doc:{compact}", _WEIGHT_WORD))
    for word in words:
        features.append((f"#w:{word}", _WEIGHT_WORD))
        # 词内单字：让"桌面"能匹配到"桌面宠物"这类更长的词
        if len(word) >= 2:
            for char in word:
                features.append((f"#ch:{char}", _WEIGHT_CHAR))
    for index in range(len(words) - 1):
        features.append((f"#w2:{words[index]} {words[index + 1]}", _WEIGHT_WORD))

    # 字符 n-gram。中文没有空格，所以按去空格后的整串切。
    if len(compact) >= 1:
        features.append((f"#c1:{compact}", _WEIGHT_BIGRAM * 0.6))
    for size, weight in ((2, _WEIGHT_BIGRAM), (3, _WEIGHT_TRIGRAM)):
        if len(compact) < size:
            continue
        for index in range(len(compact) - size + 1):
            features.append((f"#c{size}:{compact[index:index + size]}", weight))
    return features


def _signed_hash(token: str, dim: int, seed: int = 0) -> tuple[int, float]:
    """
    把 token 哈希到 (下标, 符号)。

    **带符号**是关键：不带符号的话，所有 token 只会做加法，任何两段文本的
    向量都会互相靠近（因为分量都非负），相似度就失去区分力。带符号后不同的
    token 会互相抵消，相似度才有意义。这是哈希技巧里最容易被忽略的一步。
    """
    digest = hashlib.blake2b(f"{seed}:{token}".encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    index = value % dim
    sign = 1.0 if (value >> 63) & 1 else -1.0
    return index, sign


class HashingEmbedder:
    """
    基于哈希技巧的嵌入器。确定性、零依赖、对中文有效。

    诚实说明它的局限：它匹配的是**字面重合**，不是语义。所以
    "我在写 Python" 与 "最近在折腾代码" 匹配不到。要语义匹配必须换真模型。
    实践中的缓解办法是**检索时多给几条**（top_k 取 4~6），而不是只给最像的一条。
    """

    def __init__(self, dim: int = DEFAULT_DIM, seed: int = 20260930) -> None:
        if dim < 16:
            raise ValueError("维度太小会让哈希大量碰撞，至少 16")
        self.dim = dim
        self.seed = seed
        # 缓存：同一段文本在一次会话里会被反复嵌入（写入、检索、去重），
        # 缓存能省掉大量重复计算。用文本本身当键，容量设上限防止无限增长。
        self._cache: dict[str, list[float]] = {}
        self._cache_limit = 512

    def embed(self, text: str) -> list[float]:
        key = text or ""
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        vector = [0.0] * self.dim
        features = weighted_features(text)
        if not features:
            self._remember(key, vector)
            return vector

        for feature, weight in features:
            index, sign = _signed_hash(feature, self.dim, self.seed)
            vector[index] += sign * weight

        # L2 归一化：让相似度只跟方向有关，不受文本长度影响。
        # 不做这一步的话，长文本的向量模长更大，余弦相似度会系统性偏高。
        norm = math.sqrt(sum(value * value for value in vector))
        if norm > 0:
            vector = [value / norm for value in vector]

        self._remember(key, vector)
        return vector

    def _remember(self, key: str, vector: list[float]) -> None:
        if len(self._cache) >= self._cache_limit:
            # 简单的清空策略：缓存只是加速，清掉不影响正确性
            self._cache.clear()
        self._cache[key] = vector

    def similarity(self, a: str, b: str) -> float:
        return cosine(self.embed(a), self.embed(b))

    def available(self) -> bool:
        return True


def cosine(a: list[float], b: list[float]) -> float:
    """
    余弦相似度，范围 -1~1。

    两个向量都已归一化时它就是点积 —— 这里不假设已归一化，
    因为记忆从磁盘读回来时可能不是（比如手改过文件）。
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


def keyword_overlap(a: str, b: str) -> float:
    """
    关键词重合度（Jaccard），作为字面相似度的补充信号。

    哈希嵌入在短文本上偶尔会把两条无关的话算得比较像（碰撞），
    用这个便宜的指标做一次交叉校验能压掉大部分误召回。
    """
    set_a = {g for g in ngrams(a, sizes=(2,))}
    set_b = {g for g in ngrams(b, sizes=(2,))}
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def keywords(text: str, min_len: int = 2) -> list[str]:
    """
    提取查询里的关键词。

    这里有个**中文特有的坑**：用一个固定的最小长度过滤会把"猫"、"茶"这种
    单字关键词直接丢掉，而它们恰恰是中文里信息量很高的词。所以规则是
    "长度≥2 的任意词，或单个汉字" —— 单个拉丁字母（a、I）仍会被过滤掉，
    那类确实没有检索价值。
    """
    result: list[str] = []
    for word in normalize_text(text).split():
        if len(word) >= min_len or (len(word) == 1 and _is_cjk(word)):
            result.append(word)
    return result


def keyword_boost(query: str, text: str, base_score: float, max_boost: float = 0.55) -> float:
    """
    精确关键词命中是**强证据**，应当把分数提上去。

    为什么需要它：查询"Rust 学得怎么样了"里的"学得怎么样了"会在向量空间里
    贡献大量噪声，把"Rust"这个真正的信号稀释掉（实测余弦只有 0.15，低于
    默认阈值 0.18）。而"Rust 同时出现在查询与记忆里"这件事本身极具信息量。

    与其把阈值压低（会放进噪声），不如把这个强信号单独提出来计分。
    """
    words = keywords(query)
    if not words:
        return base_score
    body = normalize_text(text).replace(" ", "")
    hits = sum(1 for word in words if word.replace(" ", "") in body)
    if hits == 0:
        return base_score
    return min(1.0, base_score + max_boost * (hits / len(words)))


def build_embedder(config: Optional[dict] = None, project_root: Optional[Path] = None) -> Embedder:
    """按配置构建嵌入器。

    ## 两个实现

    | `provider` | 实现 | 依赖 | 语义 |
    |---|---|---|---|
    | `local`（**推荐**）| `BgeEmbedder`（bge-small-zh-v1.5, ONNX）| onnxruntime + 90MB 模型 | **真语义** |
    | `hashing` | `HashingEmbedder` | 无 | 只有字面重合 |

    ## ⚠️ `local` 失败时**降级到 hashing**，不是抛异常

    模型文件可能没下、onnxruntime 可能没装、90MB 可能被误删。
    这时候"宠物不说话了"远比"语义检索差一点"严重得多 —— 所以降级并记一条
    明显的 warning（**不能静默**：用户会以为语义还在工作）。

    ## ⚠️ 换 provider 会让旧向量失效

    两个实现的维度不同（512 vs 256），旧向量在数学上无法复用。
    `memory2.MemoryService` 会检测到维度不匹配并**重建全部向量** ——
    见那里的 `_migrate_vectors()`。
    """
    section = config or {}
    provider = str(section.get("provider") or "hashing").lower()
    dim = int(section.get("dim") or DEFAULT_DIM)

    if provider in ("local", "bge", "onnx"):
        from .embedding_local import BgeEmbedder, model_dir_for

        root = Path(project_root) if project_root else _default_project_root()
        emb = BgeEmbedder(model_dir_for(root))
        if emb.load():
            return emb
        log.warning(
            "本地语义嵌入不可用（%s），**已降级为哈希实现** —— "
            "语义检索会失效，只剩字面重合能匹配。"
            "检查：onnxruntime 是否装上、模型目录是否存在（%s）",
            emb.last_error,
            model_dir_for(root),
        )
        return HashingEmbedder(dim=DEFAULT_DIM)

    if provider != "hashing":
        log.warning(
            "未知的 memory.embedding.provider=%r，回退到内置哈希嵌入（无需下载模型）",
            provider,
        )
    return HashingEmbedder(dim=dim)


def _default_project_root() -> Path:
    """从本文件位置推出项目根（`apps/brain/pet/embedding.py` -> 仓库根）。

    ⚠️ 这里**可以**用 `__file__` 推路径，与 `config._credential_paths()` 的
    情形不同：那条注释说的是**凭据**不能靠位置猜（会让测试读到开发者本机的
    真实密钥）。而模型目录是**项目内的资源**，位置是确定的、也不敏感。
    """
    return Path(__file__).resolve().parents[3]
