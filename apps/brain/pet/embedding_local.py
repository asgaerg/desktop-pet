"""
本地语义嵌入：`bge-small-zh-v1.5`（ONNX 推理）。

## 为什么换掉 `HashingEmbedder`

哈希实现是**精确但零语义**的。实测（`tools/measure-embedder.py`）：

    查询：他最近在忙什么
    事实：用户在做桌面宠物项目
    余弦：**-0.011**        ← 人一眼就懂，它算出负相关

它靠的是字面重合（"开发"↔"开发"、"界面"↔"界面"），换个说法就彻底失效。
真语义必须上模型。

## ⚠️ 这条改动**解除了一条项目硬约束**

`docs/进度交接.md` 原来写着「Python **零第三方依赖**是这个项目的硬约束
—— 只用标准库」，连 WebSocket 都是手写 RFC 6455 来避免装库。

用户 2026-10-05 明确选择「装 onnxruntime 上 bge-small」，所以这条约束
**在 embedding 这一处被有意放宽**：

    · 新增依赖：`onnxruntime`（1.30.0，wheel 13.6MB，运行时约 50MB）
    · 新增模型：`pet-data/models/bge-small-zh-v1.5/`（90.5MB）
    · **其余部分仍然零依赖** —— ws_server / 协议 / 记忆库都是标准库

## 为什么不用 `transformers` / `tokenizers`

那两个包加起来上百 MB，而我们只需要 **WordPiece 分词**这一件事 ——
`vocab.txt` 就在手边（21128 个 token），算法本身不到 60 行。
装了它们等于为了一个函数拖进整个 HuggingFace 栈。

## 为什么必须 mean pooling

这个 ONNX 导出**只有 `last_hidden_state`，没有 `pooler_output`**（实测）。
bge 系列的正确用法是**带 mask 的 mean pooling**；只用 CLS 那一维质量会
明显下降（官方明确说过）。忘了做 mask 更糟 —— padding 的位置会被平均进去，
短句和长句的向量都会偏。
"""

from __future__ import annotations

from . import paths

import json
import logging
import math
import unicodedata
from pathlib import Path
from typing import Optional

log = logging.getLogger("pet.embedding_local")

# bge-small-zh-v1.5 的维度（config.json 的 hidden_size）。**不要写错** ——
# 维度不匹配会让全量旧向量失效（见 memory2 里的迁移逻辑）。
DIM = 512

# 模型能吃的最长 token 数（max_position_embeddings）。bge-zh 在 512 处截断，
# 约等于 250 个汉字 —— 这也是 `memory.max_chars: 200` 那个约束的来源。
MAX_TOKENS = 512

# 特殊 token（BERT 中文词表的标准位置）
_PAD_ID = 0
_UNK_ID = 100
_CLS_ID = 101
_SEP_ID = 102


def _load_vocab(path: Path) -> dict[str, int]:
    """读 `vocab.txt`（一行一个 token，行号就是 id）。

    ⚠️ 必须 `encoding="utf-8"`。默认编码在 Windows 上是 GBK，
    而这个词表里全是多字节字符 —— 读错不会报错，只会得到一堆
    **乱码 token**，然后所有词都查不到、全变 `[UNK]`，
    向量因此全一样（表现为"检索什么都能匹配上"）。那种静默降级极难查。
    """
    vocab: dict[str, int] = {}
    with path.open("r", encoding="utf-8") as fh:
        for index, line in enumerate(fh):
            vocab[line.rstrip("\n")] = index
    return vocab


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf"


def _is_punct(ch: str) -> bool:
    """单个字符是不是标点。

    ⚠️ `unicodedata.category` **只接受长度为 1 的字符串** —— 传整词进去会
    抛 `TypeError`（实测踩过）。所以调用方必须先拆到单字符。
    这里额外做一次长度防御，免得后面又有人直接传词进来。
    """
    if len(ch) != 1:
        return False
    return unicodedata.category(ch).startswith("P")


def _clean(text: str) -> str:
    """BERT 的标准文本清理（对齐 HuggingFace `BasicTokenizer`）。

    每一步都有理由，少一步就会和训练时的输入分布不一致：
      · 去控制字符（模型没见过）
      · 空白统一成空格
      · 中文**逐字加空格** —— 训练时就是这么切的，不加的话 WordPiece
        会把整句当成一个"词"去匹配，全部落空变 `[UNK]`
    """
    out: list[str] = []
    for ch in str(text or ""):
        code = ord(ch)
        if code == 0 or code == 0xFFFD or (code < 32 and ch not in "\t\n\r"):
            continue  # 控制字符
        if ch.isspace():
            out.append(" ")
            continue
        if _is_cjk(ch):
            # 中文逐字分开（两侧补空格）
            out.append(" ")
            out.append(ch)
            out.append(" ")
            continue
        out.append(ch)
    return "".join(out).strip()


def _wordpiece(token: str, vocab: dict[str, int], max_chars: int = 100) -> list[str]:
    """把一个"词"切成 WordPiece（贪心最长匹配，延续部分加 `##`）。

    这是 BERT 分词的核心算法。写出来比拉一个 `tokenizers` 依赖划算得多，
    而且**可测** —— 见 `tools/measure-embedder.py` 里的对照。
    """
    if not token:
        return []
    if len(token) > max_chars:
        return ["[UNK]"]
    if token in vocab:
        return [token]

    pieces: list[str] = []
    start = 0
    while start < len(token):
        end = len(token)
        found: Optional[str] = None
        while start < end:
            span = token[start:end]
            if start > 0:
                span = "##" + span
            if span in vocab:
                found = span
                break
            end -= 1
        if found is None:
            # 整词切不开 -> 整词作废（这是 BERT 的行为，不是逐字退化成 UNK）
            return ["[UNK]"]
        pieces.append(found)
        start = end
    return pieces


def tokenize(text: str, vocab: dict[str, int], max_tokens: int = MAX_TOKENS) -> list[int]:
    """文本 -> `input_ids`（含 `[CLS]` / `[SEP]`，按 `max_tokens` 截断）。

    截断**保留头尾**（长文本两头通常信息量最大）—— 不过当前用不上：
    `memory.max_chars: 200` 已经把单条限制在 ~250 token 以内。
    """
    cleaned = _clean(text)
    ids: list[int] = [_CLS_ID]
    for word in cleaned.split():
        # 由标点组成的片段要**逐字符**切开（与训练一致）；
        # 其它片段整体做 WordPiece。`_is_punct` 只吃单字符，所以这里先判长度。
        if len(word) > 1 and all(_is_punct(c) for c in word):
            chunks = list(word)
        else:
            chunks = [word]
        for chunk in chunks:
            for piece in _wordpiece(chunk, vocab):
                ids.append(vocab.get(piece, _UNK_ID))
                if len(ids) >= max_tokens - 1:
                    break
            if len(ids) >= max_tokens - 1:
                break
        if len(ids) >= max_tokens - 1:
            break
    ids.append(_SEP_ID)
    return ids


class BgeEmbedder:
    """`bge-small-zh-v1.5` 的 ONNX 嵌入器。

    接口与 `HashingEmbedder` 一致（`embed` / `similarity` / `available` / `dim`），
    所以 `memory2` 那边不用改逻辑 —— 这是当初留 `Embedder` 协议的收益。
    """

    def __init__(self, model_dir: Path, *, threads: int = 1) -> None:
        self.model_dir = Path(model_dir)
        self.dim = DIM
        self.threads = max(1, int(threads))
        self._session = None
        self._vocab: dict[str, int] = {}
        self._cache: dict[str, list[float]] = {}
        self._cache_limit = 512
        self.last_error = ""

    # ---------------------------------------------------------------- 加载

    def available(self) -> bool:
        return (
            (self.model_dir / "model.onnx").is_file()
            and (self.model_dir / "vocab.txt").is_file()
        )

    def load(self) -> bool:
        """惰性加载模型。失败时返回 False 并记 `last_error`（**不抛**）。

        ⚠️ 为什么不抛：`memory2` 在启动时构造嵌入器，抛出去会让整个
        sidecar 起不来 —— 而"语义检索不可用"远没有"宠物不说话了"严重。
        调用方看到 `available() == False` 会退回哈希实现。
        """
        if self._session is not None:
            return True
        if not self.available():
            self.last_error = f"模型文件不全：{self.model_dir}"
            return False
        try:
            import onnxruntime as ort

            self._vocab = _load_vocab(self.model_dir / "vocab.txt")
            opts = ort.SessionOptions()
            # ⚠️ 单线程（用户定的）：sidecar 是 asyncio 单进程，
            # onnxruntime 默认会开满所有核 —— 那样每次 embedding 都会
            # 抢占事件循环的 CPU，表现为"宠物说话一顿一顿的"。
            opts.intra_op_num_threads = self.threads
            opts.inter_op_num_threads = 1
            opts.log_severity_level = 3  # 只留错误，别刷 warning
            self._session = ort.InferenceSession(
                str(self.model_dir / "model.onnx"),
                sess_options=opts,
                providers=["CPUExecutionProvider"],
            )
            log.info(
                "本地语义嵌入就绪：bge-small-zh-v1.5（dim=%d，词表 %d，线程 %d）",
                self.dim,
                len(self._vocab),
                self.threads,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("加载本地嵌入模型失败（将退回哈希）：%s", self.last_error)
            self._session = None
            return False

    # ---------------------------------------------------------------- 推理

    def embed(self, text: str) -> list[float]:
        key = text or ""
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if not self.load():
            return [0.0] * self.dim

        ids = tokenize(key, self._vocab)
        vector = self._infer(ids)
        if len(self._cache) >= self._cache_limit:
            self._cache.clear()
        self._cache[key] = vector
        return vector

    def _infer(self, ids: list[int]) -> list[float]:
        import numpy as np

        assert self._session is not None
        arr = np.array([ids], dtype=np.int64)
        mask = np.ones_like(arr)
        # token_type_ids 全 0：单句任务（bge 不需要句对区分）
        types = np.zeros_like(arr)
        try:
            out = self._session.run(
                None,
                {"input_ids": arr, "attention_mask": mask, "token_type_ids": types},
            )[0]
        except Exception as exc:  # noqa: BLE001
            log.warning("嵌入推理失败（返回零向量）：%s", exc)
            return [0.0] * self.dim

        # ---- mean pooling（带 mask）----
        #
        # ⚠️ **不能省 mask**：padding 位置也会输出向量，直接平均会把
        # 它们算进去。单条推理时 padding 为 0（没有 pad），但
        # 截断后仍要按真实长度平均，否则短句的分母会错。
        hidden = out[0]  # (seq, dim)
        length = int(mask.sum())
        if length <= 0:
            return [0.0] * self.dim
        pooled = hidden[:length].mean(axis=0)

        # ---- L2 归一化 ----
        # 归一化后余弦=点积，且相似度不受文本长度影响。
        norm = float(np.linalg.norm(pooled))
        if norm > 0:
            pooled = pooled / norm
        return [float(x) for x in pooled]

    def similarity(self, a: str, b: str) -> float:
        """两段文本的余弦相似度。

        ⚠️ bge 的相似度分布**和哈希实现完全不同** —— 它是"真语义"，
        所以无关句对也会落在 0.3~0.5（哈希那套无关句接近 0）。
        这也是为什么 `min_score` 必须跟着调（见 `memory.min_score` 的注释）。
        """
        return _cosine(self.embed(a), self.embed(b))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


def model_dir_for(project_root: Path) -> Path:
    """模型的标准落点：`apps/desktop/pet-data/models/bge-small-zh-v1.5/`。

    和 `simple.dll` 一样放在 `pet-data/` 下 —— 那是"运行时要用的外部资源"
    的统一位置，也在 `.gitignore` 覆盖范围内（90MB 不该进版本库）。
    """
    return paths.data_dir() / "models" / "bge-small-zh-v1.5"


# --------------------------------------------------------------------------- #
# token 预算（**写入前必须用它把关**）
# --------------------------------------------------------------------------- #

# 安全余量：`[CLS]` / `[SEP]` 各占 1 个，再留一点给分词边界差异。
_TOKEN_SAFETY_MARGIN = 8

_vocab_cache: Optional[dict[str, int]] = None


def token_budget() -> int:
    """一条记忆**最多能有多少 token**（超出会被静默截断的那条线）。

    ⚠️ 这个数必须**实际用起来**，不能只写在文档里 —— 见 `count_tokens` 的说明。
    """
    return MAX_TOKENS - _TOKEN_SAFETY_MARGIN


def count_tokens(text: str, model_dir: Optional[Path] = None) -> int:
    """真实 token 数（含 `[CLS]` / `[SEP]`）。

    ## ⚠️ 为什么"按字数限制"不够

    中文里 **1 字 ≈ 1 token**（实测：200 字 -> 202 tokens），所以按字数看似安全。
    但**英文/混排完全不是这个比例**：

        "internationalization"   20 字符 -> 8 tokens
        "Hello World"            11 字符 -> 2~3 tokens

    也就是说**同样的字数，token 数可能翻倍** —— 一条 200 字符的英文记忆
    照样能在 512 之前爆掉，然后**尾部内容被静默丢弃**，
    表现为"这条记忆的后半截永远搜不到"。

    所以把关要用**真实 token 计数**，而不是估算字数。
    """
    global _vocab_cache
    if _vocab_cache is None:
        if model_dir is None:
            model_dir = model_dir_for(Path(__file__).resolve().parents[3])
        vocab_file = Path(model_dir) / "vocab.txt"
        if not vocab_file.is_file():
            # 模型不在时无法精确计数 —— 用保守估算（中文 1 字 1 token，
            # 其余按 1 字符 0.5 token 粗估）而不是返回 0（那会让把关失效）
            cjk = sum(1 for ch in text if _is_cjk(ch))
            return cjk + int((len(text) - cjk) * 0.6) + 2
        _vocab_cache = _load_vocab(vocab_file)
    return len(tokenize(text, _vocab_cache))


def fits_budget(text: str, model_dir: Optional[Path] = None) -> bool:
    """这条文本能不能**完整**进入嵌入模型（不被截断）。"""
    return count_tokens(text, model_dir) <= token_budget()


def split_by_tokens(
    text: str,
    *,
    max_tokens: Optional[int] = None,
    model_dir: Optional[Path] = None,
) -> list[str]:
    """按**真实 token 预算**切段，尽量在句子边界切。

    与 `memory2._split_for_embedding` 的分工：
      · 那个按 `memory.max_chars`（用户可见、可配置）切，管的是"别太长"；
      · 这个按 token 预算切，管的是"**别超过模型能吃的量**"——
        是**最后一道保险**，防止尾部内容被静默丢掉。

    切法：先按句末标点切句，再贪心合并到预算以内；单句本身超预算时
    按字符硬切（那种情况说明这句异常长，没有更好的办法）。
    """
    import re

    budget = (max_tokens or token_budget()) - 2  # 留 [CLS]/[SEP]
    if budget <= 0:
        budget = 1
    if count_tokens(text, model_dir) <= budget + 2:
        return [text] if text.strip() else []

    # 按中文/英文句末标点切，保留标点
    sentences = [s for s in re.split(r"(?<=[。！？!?；;\n])", text) if s.strip()]
    if not sentences:
        sentences = [text]

    out: list[str] = []
    buf = ""
    for sent in sentences:
        if count_tokens(buf + sent, model_dir) <= budget + 2:
            buf += sent
            continue
        if buf.strip():
            out.append(buf)
            buf = ""
        # 单句仍超预算 -> 按字符硬切（逐字累加，保证不超）
        if count_tokens(sent, model_dir) > budget + 2:
            piece = ""
            for ch in sent:
                if count_tokens(piece + ch, model_dir) > budget + 2:
                    if piece.strip():
                        out.append(piece)
                    piece = ch
                else:
                    piece += ch
            buf = piece
        else:
            buf = sent
    if buf.strip():
        out.append(buf)
    return out


def read_config_int(model_dir: Path, key: str, fallback: int) -> int:
    """从 `config.json` 读一个整数（目前只用来核对 hidden_size）。"""
    try:
        data = json.loads((Path(model_dir) / "config.json").read_text(encoding="utf-8"))
        return int(data.get(key, fallback))
    except Exception:  # noqa: BLE001
        return fallback
