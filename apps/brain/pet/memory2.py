"""记忆服务：把 `store.py` 的数据库包装成"大脑直接可用"的接口。

## 职责

把「用户说了一句话」到「拼进提示词」中间的所有事做完：

    写入：对话落库（最近 + 原文两份）
    注入：按三类来源拼出提示词片段
          ① 固定注入（习惯 / 人设 / 常说的话）
          ② 最近记忆（时间窗口内的原文，整段）
          ③ 检索记忆（向量 + 关键词两路，**唯一提炼过的一层**）
    提炼：后台把对话压成短事实写进检索层
    备份：启动时把数据库复制到用户指定文件夹

## ⚠️ cppjieba 的 cwd 依赖（这一条最要命）

`simple.dll` 里的 cppjieba 用**相对路径** `./dict/jieba.dict.utf8` 找词典，
cwd 不对时它调 `log.FATAL` —— **整个进程直接死，不是抛异常**。

所以本模块**所有**可能触发分词的操作（建 FTS 表、`jieba_query`）之前，
都会先做一次纯文件检查（`_dict_reachable`）。启动时的 cwd 由 `sidecar.ts` 保证。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from .embedding import build_embedder
from .store import BackupResult, PetDatabase, default_db_path

log = logging.getLogger("pet.memory2")

# 泛问句的疑问/求列举词（`_looks_like_general_question` 用）。
#
# ⚠️ 只收"问有什么"这一类，**不收**「为什么」「怎么样」「什么时候」
#    —— 那几种问句即使落空，正确回答也是"不知道"，不该端最近记忆上去。
_GENERAL_QUESTION_WORDS: tuple[str, ...] = (
    "什么",
    "哪些",
    "哪个",
    "啥",
    "干什么",
    "干啥",
    "玩什么",
    "玩啥",
    "喜欢什么",
    "喜欢啥",
)

# 🔴 这些词一出现就**否决**泛问判定（2026-10-20 加的守卫）。
#
# 为什么必须有：「为什么天是蓝的」含"什么"，按上面那张表会被判成泛问，
# 于是把最近 5 条记忆端上去 —— 而正确回答是"因为瑞利散射"，
# 记忆帮不上任何忙。实测这条直接把自检打红了。
#
# 判据方向是"求列举 vs 求解释/求方法"：
#   · 求列举（"我在玩什么""我喜欢啥"）-> 答案就在记忆里
#   · 求解释（"为什么…"）、求方法（"怎么…""如何…"）、求时间（"什么时候…"）
#     -> 答案是知识，不是记忆
_GENERAL_QUESTION_VETO: tuple[str, ...] = (
    "为什么",
    "为何",
    "怎么",
    "如何",
    "怎样",
    "什么时候",
    "多久",
    "多少",
)


class MemoryService:
    """记忆的统一入口。大脑只跟它打交道，不直接碰 SQL。"""

    def __init__(self, config: Any) -> None:
        self.config = config
        section = config.memory or {}
        self.enabled = bool(section.get("enabled", True))

        self.db_path = self._resolve_db_path(section)
        self.dll_path = self._resolve_dll_path(section)

        self.db: Optional[PetDatabase] = None
        self._last_fact_ts = 0.0
        self.injections = 0
        self.facts_written = 0
        # 因矛盾被归档的旧条目数（§114 改动 5，2026-10-20）。
        # ⚠️ 单独计数而不是混进 `facts_written`：一个是"写了新的"，
        # 一个是"旧的被取代了"，出问题时这两种要能分开看。
        self.archived_by_contradiction = 0
        # 最近一次检索命中的记忆 id（「一段交流结束」时用来强化，2026-10-17）
        self.last_hit_ids: list[int] = []
        # 单条检索记忆的字数上限（超过就切段，见 `_split_for_embedding`）。
        #
        # 默认 200 —— `bge-small-zh-v1.5` 的上限是 512 token（≈250 汉字），
        # 且超了是**静默截断**（后半句不参与向量）。200 字留出了 tokenizer
        # 开销的余量。可在 `memory.max_chars` 调。
        self.max_chars = int(section.get("max_chars") or 200)

        # 嵌入器：由配置决定用真模型（local）还是哈希（hashing）。
        # ⚠️ 构造可能失败并**自动降级**（见 `embedding.build_embedder`）——
        # 降级时 `embedder_name()` 会如实返回，测量报告里能看出来。
        self.embedder = build_embedder(
            dict(section.get("embedding") or {}),
            project_root=Path(self._project_root()),
        )

        if not self.enabled:
            log.info("记忆已关闭（memory.enabled=false）")
            return

        try:
            self.db = PetDatabase(
                self.db_path,
                tokenizer="simple",
                simple_dll=self.dll_path,
            )
            self._migrate_vectors()
            counts = self.db.count()
            log.info(
                "记忆库就绪：%s（检索记忆 %d 条、最近 %d 轮、原文 %d 条、珍藏 %d 段）",
                self.db_path,
                counts["memory"],
                counts["recent_turn"],
                counts["raw_log"],
                counts["cherished"],
            )
            if not self.db._fts_ok:
                log.warning("⚠️ 中文分词未生效，检索会退化为逐字/短语匹配")
            # ── 给兴趣提取接上分词器（2026-10-17）──
            #
            # `nouns.py` 刻意不 `import jieba`（本项目没有那个依赖），
            # 而是等这里注入 —— 因为中文分词能力在 `simple.dll`（cppjieba）里，
            # 只能从**已经加载它的这个连接**上调用。
            #
            # 没有这一步的话，`nouns.extract_terms` 对不带引号的中文一律返回空，
            # 兴趣就永远只能从《书名号》里长出来（实测召回很低）。
            self._wire_segmenter()
        except Exception as exc:  # noqa: BLE001 —— 记忆坏掉绝不能影响对话
            log.exception("记忆库打开失败（本次不带记忆运行）：%s", exc)
            self.db = None

    def _wire_segmenter(self) -> None:
        """把 `simple.dll` 的 cppjieba 注入 `nouns`（失败就保持"只提引号"）。"""
        from . import nouns

        db = self.db
        if db is None:
            return

        def segment(text: str) -> list[str]:
            row = db.conn.execute("SELECT jieba_query(?)", (str(text),)).fetchone()
            if not row or not row[0]:
                return []
            return nouns.parse_jieba_query(row[0])

        # 先探一次：函数不存在就别装，免得上层每次调用都抛
        try:
            segment("测试")
        except Exception as exc:  # noqa: BLE001
            log.debug("分词器不可用（兴趣只从引号里提取）：%s", exc)
            return
        nouns.set_segmenter(segment)
        log.info("兴趣提取已接上中文分词器（simple.dll / cppjieba）")

    # ------------------------------------------------------------ 路径解析

    def _resolve_db_path(self, section: dict[str, Any]) -> Path:
        raw = str(section.get("path") or "auto").strip()
        if raw and raw != "auto":
            return Path(raw)
        return default_db_path(Path(self._project_root()))

    def _resolve_dll_path(self, section: dict[str, Any]) -> Optional[Path]:
        """`simple.dll` 的位置。

        `auto` 的推导方式：**从当前 cwd 反推** —— 因为 sidecar 启动时会把 cwd
        设成 DLL 所在目录（词典要求），所以 cwd 就是最可靠的线索。

        找不到就返回 None（退化为 unicode61，中文检索不可用但不崩）。
        """
        raw = str(section.get("simple_dll") or "auto").strip()
        if raw and raw != "auto":
            p = Path(raw)
            return p if p.exists() else None

        cwd = Path.cwd()
        candidates = [cwd / "simple.dll"]
        # 兼容"cwd 是它的父目录"的写法
        for sub in cwd.glob("*/simple.dll"):
            candidates.append(sub)
        for c in candidates:
            if c.exists():
                return c
        log.info("未找到 simple.dll（中文分词不可用）：试过 %s", [str(c) for c in candidates])
        return None

    def _project_root(self) -> str:
        # apps/brain/pet/memory2.py -> 上溯四级
        return str(Path(__file__).resolve().parents[3])

    # ------------------------------------------------------------ 注入

    def recent_window_days(self) -> int:
        section = self.config.memory or {}
        lo = int(section.get("recent_window_min_days") or 1)
        hi = int(section.get("recent_window_max_days") or 365)
        want = int(section.get("recent_window_days") or 30)
        return max(lo, min(hi, want))

    def _memory_prompts(self) -> dict[str, str]:
        """读 `prompts.memory` 段（用户可在设置页改的记忆注入文案）。

        找不到就用内置默认 —— 见 `_render` 的说明（用户打错不该让宠物不回话）。
        """
        section = (self.config.root.get("prompts") or {}).get("memory")
        return section if isinstance(section, dict) else {}

    def _render(self, key: str, fallback: str, **values: Any) -> str:
        """渲染一段记忆注入文案；缺失/格式错时静默退回内置默认。"""
        raw = self._memory_prompts().get(key)
        template = str(raw) if isinstance(raw, str) and raw.strip() else fallback
        try:
            return template.format(**values).strip()
        except (KeyError, IndexError, ValueError) as exc:
            log.warning("记忆提示词 prompts.memory.%s 渲染失败（%s），退回默认", key, exc)
            try:
                return fallback.format(**values).strip()
            except (KeyError, IndexError, ValueError):
                return fallback.strip()

    @staticmethod
    def _row_ts(row: Any) -> float:
        """从数据库行里取时间戳（`ts` 或 `created_at`，都没有就 0）。

        ⚠️ 抽成方法是因为 `raw_log` 的行可能只有其中一个 ——
        2026-10-28 加日期前缀时，直接 `r["ts"]` 会在缺少该列时抛 KeyError，
        而那时"历史注不进去"表现为**宠物重启后完全不记得刚才聊过什么**。
        """
        try:
            getter = row.get if hasattr(row, "get") else (lambda k: row[k])
            for key in ("ts", "created_at"):
                try:
                    value = getter(key)
                except (KeyError, IndexError):
                    continue
                if value:
                    return float(value)
        except (TypeError, ValueError):
            pass
        return 0.0

    @staticmethod
    def _when(ts: Any) -> str:
        """把时间戳格式化成**给 AI 看的**相对时间。

        ## 为什么用相对时间而不是绝对日期（用户要求）

        用户原话：「提示词将所有数据库中的最近记忆和固定注入和检索出来的
        检索层数据当提示词给 ai 时，**需要将每条数据的时间加到每条数据上**，
        这样 ai 会根据时间来精准分辨」

        AI 判断"这件事还算不算数"靠的是**距今多久**：

            「用户在做桌宠项目（今天）」    -> 正在做
            「用户在做桌宠项目（3 个月前）」  -> 可能早做完了

        绝对日期（`2026-07-14`）需要 AI 自己算差值，而它对"今天是几号"
        并不敏感 —— 相对天数才是它真正用得上的信号。

        ## 格式

            今天 / 昨天 / N 天前 / N 个月前（>30 天）/ N 年前（>365 天）
            时间戳无效 -> 空串（调用方就不加时间，而不是显示 `1970-01-01`）

        ⚠️ 跨年时"12 个月前"和"1 年前"会重复，所以 >365 天直接说"N 年前"。
        """
        try:
            value = float(ts or 0.0)
        except (TypeError, ValueError):
            return ""
        if value <= 0:
            return ""
        delta = time.time() - value
        if delta < 0:
            # 时钟回拨 / 未来时间 —— 说"今天"比说"未来"自然
            return "今天"
        days = int(delta // 86400)
        if days <= 0:
            return "今天"
        if days == 1:
            return "昨天"
        if days < 30:
            return f"{days} 天前"
        if days < 365:
            return f"{max(1, days // 30)} 个月前"
        return f"{max(1, days // 365)} 年前"

    @staticmethod
    def _dedupe_recent(lines: list[str]) -> list[str]:
        """
        去掉"最近记忆"注入里的**逐字重复行**，保留**最新**那一次。

        ## 为什么要它（2026-10-07 实测）

        用户问"提示词怎么组成的、真实发过去多少"，实测发现：

            ③ 最近记忆(原文)   120 轮 / 8044 字 / ≈10055 token   ← 占注入体积 **98%**
               其中只有 46 轮**内容不同** —— 74 条是逐字一模一样的

            逐字重复最多的：
              ×17  小灵：看到嘿，你觉得ai会怎么发展了。需要我安静点吗？
              ×17  小灵：又在跟嘿，你觉得ai会怎么发展较劲？
              ×14  小灵：「嘿，你觉得ai会怎么发展」……你在忙这个呀
              ×12  用户：我今天写代码卡住了，你说说看

        ## 为什么这是"去重"而不是"压缩"

        这些行是**逐字相同**的（同样的 role、同样的文本），去掉多余的副本**不丢
        任何信息** —— 与"把几条不同的记忆合成一条"（那是压缩，要用户过目）性质不同。

        ⚠️ **不修改数据库**：原文层一个字都不动，只在**拼提示词时**少放几份副本。
        所以这完全可逆、零成本、无副作用。

        ## 为什么"保留最新"

        `recent_turns()` 是**新→旧**排序的（见它的 SQL），所以第一次遇到某个文本
        就是最新那次。时间标注也跟着那一条走 —— 用户要求"每条数据都要带时间"，
        而这个时间必须是**它真正发生的时间**，不能拿旧副本的时间。
        """
        seen: set[str] = set()
        out: list[str] = []
        for line in lines:
            # ⚠️ 比较前必须**剥掉 `[2 天前] ` 这个时间前缀**。
            #    同一句话在两天里说了 12 次，带上时间前缀之后每行都不一样，
            #    一条也去不掉 —— 那就白做了。
            body = line[line.find("] ") + 2 :] if line.startswith("[") and "] " in line else line
            if body in seen:
                continue
            seen.add(body)
            out.append(line)
        return out

    def recent_dialogue(
        self,
        limit: int = 120,
        *,
        max_age_s: Optional[float] = None,
        max_total_chars: int = 0,
        session_gap_s: float = 0,
        exclude_texts: Optional[Sequence[str]] = None,
    ) -> list[dict[str, str]]:
        """
        从 `raw_log` 取最近的对话，**按时间正序**返回 `[{"role","text"}]`。

        ## 用途：给 `Gateway.bootstrap_history()` 灌对话历史

        用户要求（2026-10-08）：

        > 「如果能不靠本地的最近记忆来让重启的 api 得到之前的上下文，那也不必
        > 保留最近记忆了」

        **能做到** —— `raw_log` 就是逐字原文的永久底本，`role` + `text`
        交替完整，直接就能当 `messages` 用。所以「最近对话」这件事
        从**提示词注入**改成了**对话历史**：

        | | 旧（提示词注入） | 新（灌进 history） |
        |---|---|---|
        | 发送形式 | system 里一段纯文本 | `messages` 里的真实轮次 |
        | 重复风险 | ⚠️ 与历史重复（"你说了两遍"的成因） | ✅ 只有一个来源 |
        | 每轮成本 | ~10000 token（实测占注入体积 98%） | 0（本来就随 messages 发） |

        ## ⚠️ 为什么读 `raw_log` 而不是 `recent_turn`

        两张表内容基本一样（`remember_turn` 同时写），但语义不同：

          · `raw_log`   = **永不淘汰**的永久底本
          · `recent_turn` = 会被压缩/清理的检索层

        要"接着上次说"就该读**不会丢**的那张。

        ## ⚠️ 只取成对的轮次，且丢掉孤立的第一条

        历史以 `user` 开头、`assistant` 结尾对模型最自然。如果切出来的
        第一条是 `assistant`（上一轮被截断在中间），把它丢掉 ——
        否则模型会看到"助手凭空开口"。

        ## 参数

        · `limit`           最多取几条（含 user 与 assistant）
        · `max_age_s`       只取这个时间窗内的（None = 不限）
        · `max_total_chars` 总字数上限（0 = 不限）。**这是 token 预算的落地**：
                            中文 1 字 ≈ 1 token（`count_tokens` 实测过），
                            所以按字数裁剪等价于按 token 裁剪，且不需要分词器。
        · `exclude_texts`   要跳过的文本（见 `Gateway` 里过滤旧污染数据的说明）
        """
        if not self.db:
            return []
        try:
            since = (time.time() - max_age_s) if max_age_s else None
            # `list_raw` 是**最新在前**的
            rows = self.list_raw(limit=max(1, int(limit)), since_ts=since)
        except Exception:  # noqa: BLE001 —— 读不到历史不该让对话起不来
            log.exception("读取最近对话失败（当作没有历史）")
            return []

        # ⚠️ **只保留"最后一段连续会话"**（相邻两条间隔不超过 `session_gap_s`）
        #
        # ## 为什么（2026-10-08 实测发现）
        #
        # 排查「文本发了两次」那个 bug 时，宠物每轮都回「你又说两遍啦」——
        # 那场对话**整场**都被污染了。实测灌回 76 条里有 **17 条（22%）**
        # 是这类内容，位置 `#1..#65`（也就是最早那场对话）。
        #
        # 把它灌回去会怎样：模型看到自己"一直在说两遍"，然后**继续**这个话题 ——
        # 用户会发现宠物莫名其妙还在纠结一件早就修好的事。
        #
        # ## 为什么用"时间间隔"而不是"删掉那些句子"
        #
        # 按内容过滤有两个问题：
        #   · 那些回复里**夹着真实内容**（比如 #1 同时说了"天气不错"和"你说了两遍"）
        #     —— 整条删掉会连真实的一起丢
        #   · 关键词很难列全，而且**用户真的连着说两遍时会被误伤**
        #
        # 按时间切分则很自然：「你现在接着的是**最近这一段**对话」。
        # 中间隔了几个小时的，本来就是"上一次的事"，不灌回来更合理。
        #
        # `session_gap_s = 0` 表示不切分（灌回窗口内的全部）。
        if session_gap_s > 0 and rows:
            cut: list[Any] = []
            for i, r in enumerate(rows):  # rows 是最新在前
                if i == 0:
                    cut.append(r)
                    continue
                prev_ts = rows[i - 1].get("ts") or rows[i - 1].get("created_at") or 0
                cur_ts = r.get("ts") or r.get("created_at") or 0
                try:
                    gap = abs(float(prev_ts) - float(cur_ts))
                except (TypeError, ValueError):
                    gap = 0.0
                if gap > session_gap_s:
                    log.debug(
                        "灌回历史在此断开：与上一条相隔 %.1f 分钟（阈值 %.1f 分钟）",
                        gap / 60,
                        session_gap_s / 60,
                    )
                    break
                cut.append(r)
            rows = cut

        skip = {t.strip() for t in (exclude_texts or ()) if t and t.strip()}

        # 最新在前 -> 反成时间正序
        ordered: list[dict[str, str]] = []
        last_stamp = ""   # 上一条的日期前缀（同日不重复加）
        for r in reversed(rows):
            role = str(r.get("role") or "")
            text = str(r.get("text") or "")
            if role not in ("user", "assistant") or not text.strip():
                continue
            if text.strip() in skip:
                continue
            # 给**跨天**的消息加日期前缀（2026-10-28）。
            #
            # ## 🔴 为什么必须加
            #
            # 用户报：「昨天宠物的时间观念还在很久以前我说的新版本上」。
            #
            # 根因：这段历史是**直接灌进 `messages`** 的（不是一个提示词片段），
            # 所以里面**一个字的时间信息都没有**。模型看到几十条对话，
            # 根本分不清哪条是昨天、哪条是三天前 —— 于是把几天前说的
            # 「刚出了个新版本」当成现在的状态。
            #
            # 实测证据（真实库，2026-10-28）：
            #
            #     10-21 19:14  user: 如果模型思考时候的内部参数能达到…
            #     10-27 23:07  assistant: 那个新版本明天开，希望你能玩得开心
            #     10-27 23:08  user: 新版本早过了，现在是10月27号
            #
            # 中间隔了 6 天，但**历史里完全看不出来**。模型把 10-21 的
            # 语境当成"昨天"，于是把上周的事说成"明天"。
            #
            # 时间提示词里虽然写了「历史里的时间都是过去」，但**历史里根本没写时间**，
            # 那句话就落空了。所以要在这里真的标出来。
            #
            # ⚠️ 只给**跨天**的加：同一天内的几十条如果每条都带日期，
            #    纯属噪音（而且会占掉不少 token）。同一天只用一次前缀。
            #
            # ⚠️ 用 `_when()` 的相对说法（"今天"/"3 天前"）而不是绝对日期：
            #    模型对"距今多久"敏感，对"2026-10-21"不敏感
            #    （详见 `_when` 的说明）。
            stamp = self._when(self._row_ts(r))
            if stamp and stamp != last_stamp:
                last_stamp = stamp
                text = f"〔{stamp}〕{text}"
            ordered.append({"role": role, "text": text})

        # 丢掉开头孤立的 assistant（上一轮被截断，模型看到"助手凭空开口"很怪）
        while ordered and ordered[0]["role"] != "user":
            ordered.pop(0)

        # 按总字数从**最新**往回保留（预算不够时优先留住近的）
        if max_total_chars > 0:
            kept: list[dict[str, str]] = []
            total = 0
            for item in reversed(ordered):
                n = len(item["text"])
                if kept and total + n > max_total_chars:
                    break
                kept.append(item)
                total += n
            ordered = list(reversed(kept))
            # 截断后又可能出现开头孤立 assistant，再修一次
            while ordered and ordered[0]["role"] != "user":
                ordered.pop(0)

        return _merge_consecutive_same_role(ordered)

    def history_usage(self, days: int, budget: int = 0) -> dict[str, Any]:
        """
        统计某个天数窗口内的对话**总共多少**，以及**实际会发多少**。

        ## 用途：设置页那行"实时数字"（用户要求 2026-10-08）

        用户原话：

        > 「最近记忆可以按照设置的天数自动搜索数据库取得字数，计算出总
        >    token 消耗，再和云端的大模型上下文计算，看总占比。」

        所以设置页不只是个输入框，还会显示：

            最近 30 天: 共 90 条 / 4535 字 / ≈4535 token
                        实际发送 4535 token（占预算 30000 的 15%、占窗口 1M 的 0.5%）

        ## 为什么"总共"和"实际发"要分开报

        两个上限的分工是**天数决定取多少、预算决定最多发多少**。
        窗口内的量超预算时会被截断 —— 那时只报"总共"会让人以为
        全发出去了（而占窗口的百分比也会是假的）。

        ## ⚠️ token 用**字数**估算

        中文 1 字 ≈ 1 token（`count_tokens` 实测过：200 字 → 202 tokens）。
        不引分词器 —— 这个数字是给用户方向感的，精度够用。
        """
        out: dict[str, Any] = {
            "days": days,
            "turns": 0,
            "chars": 0,
            "tokens": 0,
            "sentTokens": 0,
            "truncated": False,
        }
        if not self.db:
            return out
        max_age = (days * 86400) if days > 0 else None
        try:
            rows = self.recent_dialogue(
                limit=100000,  # 只要总量，不要条数上限
                max_age_s=max_age,
                max_total_chars=0,  # 不截断 —— 先算"总共"
            )
        except Exception:  # noqa: BLE001
            log.exception("统计对话用量失败（当作 0）")
            return out

        total = sum(len(r["text"]) for r in rows)
        out["turns"] = len(rows)
        out["chars"] = total
        out["tokens"] = total  # 中文 1 字 ≈ 1 token

        # 再算"预算截断后实际发多少"
        if budget > 0 and total > budget:
            out["truncated"] = True
            acc = 0
            kept_n = 0
            for item in reversed(rows):  # 从最新往回攒（与 recent_dialogue 同口径）
                n = len(item["text"])
                if kept_n and acc + n > budget:
                    break
                acc += n
                kept_n += 1
            out["sentTokens"] = acc
        else:
            out["sentTokens"] = total
        return out


    def inject_hint(self, query: str = "", exclude_ids: Optional[set[int]] = None) -> str:
        """
        拼出要注入系统提示词的那几段。

        顺序有意义：**固定注入在前**（它是"你是谁、我是谁"的底子），
        最近记忆居中（交代沟通方式），检索结果在后（与当前话题最相关，离提问最近）。

        ## ⚠️ 每一段都带时间（用户 2026-10-05 要求）

        四段全都加上了时间标注 —— 没有时间，AI 会把三个月前的事
        当成现在的事说。格式见 `_when()`。

        ## `exclude_ids` —— 排除刚写进去的那条（2026-10-08 加）

        ## ⚠️ 不排除会怎样：「消息好像传输了两次」

        `gateway.ask()` 先写用户这句（为了让检索算上当前话题），再拼提示词。
        而"最近记忆"是整段注入 `recent_turn` 的，于是同一句话**同时**出现在：

            messages[0] system   「[今天] 用户：周杰伦的雨下一整晚。」
            messages[1] user     「周杰伦的雨下一整晚。」

        **模型看到两遍** -> 每轮都回「你说了两遍」。
        用户以为是网络传重了，实际是提示词里重复了。

        ⚠️ **用 id 排除而不是文本匹配**：用户完全可能连着说两遍同一句话，
        那时两条都该在 —— 按文本排除会把**真的重复**也去掉。

        ⚠️ 只排"最近记忆"那段，**不动固定注入和检索**：
        那两段是按重要度/相关度挑的，不是"把刚才这句复述一遍"。
        """

        _exclude = exclude_ids or set()
        if not self.db:
            return ""
        pieces: list[str] = []

        # ① 固定注入：习惯 / 人设 / 常说的话
        try:
            fixed = self.db.fixed_memories()
            if fixed:
                lines = []
                for r in fixed[:40]:
                    when = self._when(r["ts"] if "ts" in r.keys() else None)
                    stamp = f"（{when}）" if when else ""
                    lines.append(f"- {r['summary']}{stamp}")
                header = self._render("fixed_header", "你记得关于用户的事：")
                pieces.append(f"{header}\n" + "\n".join(lines))
        except Exception:  # noqa: BLE001
            log.exception("固定注入失败（已忽略）")

        # ② 珍藏记忆（用户手动标记、且开了固定注入）
        try:
            pinned = self.db.pinned_cherished()
            for row in pinned[:5]:
                title = row["title"] or "一段珍藏的记忆"
                when = self._when(row["ts"] if "ts" in row.keys() else None)
                stamp = f"（{when}）" if when else ""
                header = self._render(
                    "cherished_header", "【{title}】{stamp}", title=title, stamp=stamp
                )
                pieces.append(f"{header}\n{row['text']}")
        except Exception:  # noqa: BLE001
            log.exception("珍藏注入失败（已忽略）")

        # ③ 最近记忆：**已移除**（2026-10-08，用户要求）
        #
        # ## 🔴 为什么删掉它
        #
        # 用户实测发现这段占了注入体积的 **98%**：
        #
        #     ③ 最近记忆(原文)   120 轮 / 8044 字 / ≈10055 token   ← 占注入体积 98%
        #
        # 而真正有价值的「检索事实」只有 ~100 字。也就是**每轮花一万 token
        # 去复述一遍刚才说过的话**。
        #
        # ## 它还是个 bug 的根源
        #
        # 这段是"整段注入 `recent_turn` 的原文"，而同一句话**同时**在
        # `messages` 的历史里。于是模型看到两遍 —— 这正是用户报的
        # 「文本发了两次 / 你说了两遍」的成因。当时打的补丁是
        # `exclude_ids` 排掉"刚写进去的那条"，但那只堵住了**当前这一句**，
        # 历史里更早的轮次仍然是重复的。
        #
        # ## 现在改用什么
        #
        # **`raw_log` 直接灌进对话历史**（见 `recent_dialogue()` 与
        # `Gateway.bootstrap_history()`）。好处：
        #
        #   · 一句话只出现在**一个**地方 -> 重复问题从根上消失
        #   · 以 `messages` 的形式发送，模型对"谁说的"分辨得更清
        #     （原文里是 `用户：xxx` / `小灵：xxx` 的纯文本行）
        #   · 省掉每轮一万 token
        #
        # ⚠️ `recent_turn` 表**继续写入**（`remember_turn` 一个字不改）——
        # 它还是记忆管理界面「原文对话」页的数据来源。只是不再进提示词。

        # ④ 检索记忆：与当前这句话相关的**提炼结果**
        if query.strip():
            try:
                hits = self._retrieve_hits(query)
                if hits:
                    lines = []
                    for item in hits:
                        when = self._when(item.get("ts"))
                        stamp = f"（{when}）" if when else ""
                        lines.append(f"- {item['text']}{stamp}")
                    """
                    ⚠️ 泛问句兜底的结果要**明说**它不是"被问到的"（2026-10-20）。

                    负向检索（三条路全落空）之后按时间端上来的那几条，
                    与用户此刻问的话题**未必相关**。如果沿用"你想起以前聊到过的
                    相关内容"，模型会以为这就是答案 —— 用户问「我在玩什么游戏」，
                    它可能开始念「用户喜欢曲娘」（那是壁纸的事）。

                    标题换成"你最近记得的几条"，语义从"这些是相关的"
                    变成"这些是它最近记下的，自己判断相不相关"。
                    """
                    if any(item.get("fallback") for item in hits):
                        header = self._render(
                            "recall_header_general",
                            "（这几条是你最近记得的，未必与这句话相关，"
                            "只在确实对得上时才提）",
                        )
                    else:
                        header = self._render("recall_header", "你想起以前聊到过的相关内容：")
                    pieces.append(f"{header}\n" + "\n".join(lines))
            except Exception:  # noqa: BLE001
                log.exception("记忆检索失败（已忽略）")

        if not pieces:
            return ""
        self.injections += 1
        pieces.append(
            self._render(
                "tail",
                "这些记忆仅供参考。**只在确实相关时自然提起**，不要生硬复述，"
                "也不要假装记得没提到的事。",
            )
        )
        return "\n\n".join(pieces)

    def _score_floor(self) -> float:
        """向量路的**地板**相似度。配置优先，否则按嵌入器实现取默认。

        ## ⚠️ 为什么默认值要跟着实现走（2026-10-05 实测踩到）

        两个实现的相似度分布**差一个量级**：

            哈希：无关句对 ≈ 0（-0.05 ~ 0.05）
            bge ：无关句对 ≈ 0.22 ~ 0.33（mean-pooled 句向量天然有更高基线）

        所以"0.18"和"0.25"各自只对一个实现合适：

          · 对 bge 用 0.18 -> 什么都通过（形同没有过滤）；
          · 对哈希用 0.25 -> **把正确答案也滤掉**（实测：哈希下最相关的一条
            只有 0.199，于是"含检索段"那条测试直接失败）。

        写死一个数字必然对另一个实现是错的。默认值必须按实现分：
        配置里显式写了 `min_score` 就听配置（`.yaml` 里现在是给 bge 的 0.25）。
        """
        raw = (self.config.memory or {}).get("min_score")
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
        return 0.25 if self.embedder_name().startswith("bge") else 0.18

    def retrieve_debug(self, query: str) -> dict[str, Any]:
        """检索的**分路明细**（关键词路 / 向量路各自命中了什么）。

        第十七节要区分"B 词语找回"和"C 语义找回"—— 只看合并结果分不清
        是哪条路找回来的，也就没法定位失败原因。这个方法是给那个测量用的。
        """
        if not self.db:
            return {"ok": False, "message": "记忆库未启用"}
        top_k = int((self.config.memory or {}).get("top_k") or 5)
        min_score = self._score_floor()

        keyword_ids: list[int] = []
        for row in self.db.search_memory(query, limit=top_k * 2):
            keyword_ids.append(int(row["id"]))

        vector_hits: list[tuple[int, float]] = []
        try:
            vec = self._embed(query)
            if vec:
                # 与 `_retrieve` 走**同一套**路径（KNN 优先，退回线性扫描），
                # 否则两条路的数字会对不上。
                #
                # ⚠️ **维度不符必须显式暴露**（2026-10-05 实测踩过）：
                # `_cosine` 见长度不等会**直接返回 0** —— 安全但**完全静默**，
                # 整个向量路会无声失效、日志一个字都没有
                # （表现为"语义检索突然不灵了"）。
                # 什么时候会这样：换过 embedding 实现（256 -> 512）、
                # 或**运行期间库被换成旧备份**（迁移只在 `__init__` 跑，检测不到）。
                knn = self.db.vec_search(vec, limit=max(4, top_k * 4))
                if knn is not None:
                    for mid, sim in knn:
                        if sim > min_score:
                            vector_hits.append((int(mid), round(sim, 4)))
                else:
                    mids, blobs, dim = self.db.all_vectors_raw()
                    if dim and dim != len(vec):
                        log.warning(
                            "库里的向量是 %d 维，当前嵌入器是 %d 维 —— 向量路已跳过。"
                            "通常意味着换过 embedding 实现，或库被换成了旧备份；"
                            "重启一次会触发 `_migrate_vectors()` 重建",
                            dim,
                            len(vec),
                        )
                    else:
                        for mid, sim in zip(mids, _cosine_batch_raw(vec, blobs, dim)):
                            if sim > min_score:
                                vector_hits.append((int(mid), round(sim, 4)))
        except Exception:  # noqa: BLE001
            log.debug("向量检索调试失败", exc_info=True)
        vector_hits.sort(key=lambda kv: -kv[1])

        def texts(ids: list[int]) -> list[str]:
            if not ids:
                return []
            ph = ",".join("?" * len(ids))
            rows = self.db.conn.execute(
                f"SELECT id, summary FROM memory WHERE id IN ({ph})", ids
            ).fetchall()
            by_id = {int(r["id"]): str(r["summary"]) for r in rows}
            return [by_id[i] for i in ids if i in by_id]

        return {
            "ok": True,
            "query": query,
            "keyword_ids": keyword_ids,
            "keyword_texts": texts(keyword_ids),
            "vector_hits": vector_hits,
            "vector_texts": texts([m for m, _ in vector_hits]),
            "merged": self._retrieve(query),
            "top_k": top_k,
            "min_score": min_score,
            "embedder": self.embedder_name(),
        }

    def _migrate_vectors(self) -> int:
        """检查 `memory_vec` 的维度是否与当前嵌入器一致，不一致就**全部重建**。

        ## ⚠️ 为什么必须做（换 embedding 最容易踩的坑）

        哈希实现是 **256 维**，bge-small 是 **512 维**。旧向量在新空间里
        **数学上没有意义** —— 而代码不会报错，只会：

            余弦算出来是 0（长度不等，`_cosine` 直接返回 0）
            -> 向量路**静默失效**，只剩关键词路在工作

        表现就是"换了模型语义反而更差了"，而且看日志什么都没有。
        这比崩溃难查得多。

        ## 重建而不是清空

        向量的来源是 `memory.summary`（正文还在表里），所以**重建是无损的**：
        读出来、重新 embed、写回去。用户不会丢任何记忆。

        代价：bge 单条约 3.4ms（实测），2000 条约 7 秒 —— 只在换模型那次发生。
        """
        if not self.db:
            return 0
        rows = self.db.conn.execute(
            "SELECT DISTINCT dim FROM memory_vec"
        ).fetchall()
        dims = {int(r["dim"]) for r in rows if r["dim"] is not None}
        want = int(getattr(self.embedder, "dim", 0) or 0)
        if not dims:
            return 0  # 还没有任何向量，不用迁移
        if dims == {want}:
            return 0  # 维度一致，正常情况

        log.warning(
            "向量维度不匹配（库里 %s，当前嵌入器 %d）—— 正在**重建全部向量**。"
            "这会在换 embedding 实现后发生一次，重建是无损的（正文还在表里）",
            sorted(dims),
            want,
        )
        count = 0
        try:
            items = list(self.db.conn.execute("SELECT id, summary FROM memory"))
            # ⚠️ 先把 KNN 索引按新维度重建，否则 `set_vector` 写不进 vec0
            # （vec0 的维度建表时固定，维度变了必然失败）。
            self.db.vec_rebuild(want)
            for row in items:
                self.db.set_vector(int(row["id"]), self.embedder.embed(str(row["summary"])))
                count += 1
            self.db.conn.commit()
        except Exception:  # noqa: BLE001 —— 迁移失败不该让 sidecar 起不来
            log.exception("重建向量失败（向量路可能暂时不可用）")
        log.info("向量重建完成：%d 条", count)
        return count

    def embedder_name(self) -> str:
        """当前用的是哪种 embedding —— 报告里必须标明。

        ⚠️ 这个值**可能是降级后的结果**（模型缺失 -> 退回 hashing）。
        测量脚本靠它决定要不要打"结论不可用"的警告。
        """
        name = type(self.embedder).__name__
        if name == "BgeEmbedder":
            # 确认真的加载成功了（available 为真也可能没 load）
            return "bge" if getattr(self.embedder, "_session", None) is not None else "bge(未加载)"
        return "hashing"

    def _lexical_fallback_enabled(self) -> bool:
        """专有名词兜底路开不开（`memory.lexical_fallback`，默认开）。

        ⚠️ 留这个开关不是为了"调参"，而是为了**能做对照实验**：
        改动 4 到底有没有提升召回，唯一可靠的验证方法是关掉它跑同一批问句再对比。
        没有开关就只能靠"感觉变好了"——那不叫验证。
        """
        value = (self.config.memory or {}).get("lexical_fallback")
        return True if value is None else bool(value)

    def _lexical_fallback_max_terms(self) -> int:
        """一句话里最多拿几个专有名词去做子串匹配（默认 6）。

        每个词一次全表 LIKE，实测单次 5ms 量级 —— 6 个约 30ms，
        相对一次向量编码（bge 约 15~40ms）不算大头，但也没必要更多：
        一句话里真有 6 个以上专有名词时，前 6 个已经覆盖了主体。
        """
        raw = (self.config.memory or {}).get("lexical_max_terms")
        try:
            return max(1, min(20, int(raw if raw is not None else 6)))
        except (TypeError, ValueError):
            return 6

    def _retrieve_hits(self, query: str) -> list[dict[str, Any]]:
        """两路检索后合并，返回**带元数据**的命中列表。

        每项是 `{"id", "text", "ts", "created_at", "kind", "level"}`。

        ## 为什么不再返回 `list[str]`（2026-10-05 改）

        用户要求：**提示词里每条记忆都要带上时间** ——
        「这样 ai 会根据时间来精准分辨」。AI 拿到"用户在做桌宠项目"和
        "用户在做桌宠项目（3 个月前）"判断完全不同：前者是当下，
        后者可能早做完了。**没有时间，AI 会把三个月前的事当成现在的事说。**

        所以检索结果必须带 `ts` —— 而旧接口只回文本，时间就丢了。
        与其在 `inject_hint` 里回查一次（多一次 SQL、还容易错），
        不如在这里一次带出来。

        ## 为什么两路都要

          · 关键词能命中"专有名词、代码、数字"这类向量不敏感的东西；
          · 向量能命中"意思相近但用词不同"的（"咖啡" ↔ "提神饮料"）。

        ## ⚠️ 已压缩的条目**不参与**（用户选定的 A 方案）

        两条路都排除了：`search_memory` 的 SQL 里有 `merged_into IS NULL`；
        KNN 索引在压缩时就 `vec_drop` 掉了。这是"净化召回"真正的落点。
        """
        assert self.db is not None
        top_k = int((self.config.memory or {}).get("top_k") or 5)
        snippet = int((self.config.memory or {}).get("snippet_chars") or 120)

        seen: dict[int, float] = {}

        # 关键词路
        for rank, row in enumerate(self.db.search_memory(query, limit=top_k * 2)):
            seen[int(row["id"])] = seen.get(int(row["id"]), 0.0) + 1.0 / (1.0 + rank)

        # ---- 专有名词兜底路（§114 改动 4，2026-10-20 接入）----
        #
        # 用户问「银松镇那个机关怎么过」时，最该命中的是含「银松镇」的那条记忆。
        # 但两条正路都可能漏掉它：
        #
        #   · 关键词路：`jieba_query` 不认识"银松镇"，把它切成
        #     `"银" AND "松" AND "镇"` —— 三个字各自命中一堆无关条目，
        #     真正那条反而被挤出 top_k；
        #   · 向量路：句意相似度对"专有名词"不敏感（"银松镇"和"明日方舟"在向量上
        #     可能比"银松镇的机关"更近 —— 都是游戏名）。
        #
        # 所以加一条**字面子串**路：从问句里抽专有名词，命中就进候选。
        # ⚠️ 权重刻意给得比正路低（0.5）：它是"保证不漏"，不是"保证排第一"。
        #    真正排前面仍靠语义分 —— 否则一个被反复提到的词会永远霸占榜首。
        fallback_hits = 0
        if self._lexical_fallback_enabled():
            try:
                from . import nouns

                terms = nouns.extract_terms(query)
                for term in terms[: self._lexical_fallback_max_terms()]:
                    rows = self.db.search_memory_like(term, limit=max(3, top_k))
                    if not rows:
                        continue
                    fallback_hits += 1
                    for rank, row in enumerate(rows):
                        mid = int(row["id"])
                        # `+0.5/(1+rank)`：比关键词路（1.0/(1+rank)）低一档，
                        # 但比"没命中"高得多 —— 只要它出现在候选里就不会被剪掉。
                        seen[mid] = seen.get(mid, 0.0) + 0.5 / (1.0 + rank)
            except Exception:  # noqa: BLE001 —— 兜底失败绝不能影响主检索
                log.debug("专有名词兜底检索跳过", exc_info=True)

        """
        ## 「你自己呢？」—— 优先召回 `self` 类（2026-10-21）

        用户要求检索层能体现"玩伴自己的事实"。但光有数据不够：
        用户问「你自己怎么想的」「你喜欢什么」时，三条正路都会优先返回
        **用户**的事实（因为它们和"喜欢"这个词的语义更近）。

        所以这里加一路：问句里出现**指向它自己**的词时，
        把 `kind='self'` 的条目按重要度拉进候选。

        ⚠️ 权重给 0.8：比关键词路（1.0）低、比专有名词兜底（0.5）高 ——
        它只保证"它自己的事有机会被想起来"，不保证霸榜。

        ⚠️ 只在问句**确实指向它**时才走。否则每一轮都会把它自己的事
        塞进提示词，用户聊自己的事时会被它抢话题。
        """
        if self._asks_about_self(query):
            try:
                for rank, row in enumerate(self.db.self_memories(limit=max(3, top_k))):
                    mid = int(row["id"])
                    seen[mid] = seen.get(mid, 0.0) + 0.8 / (1.0 + rank)
            except Exception:  # noqa: BLE001
                log.debug("「它自己」检索跳过", exc_info=True)

        # 向量路
        try:
            vec = self._embed(query)
            if vec:
                floor = self._score_floor()
                # ---- 优先走 sqlite-vec 的 KNN（计算下推到 SQL 层）----
                #
                # KNN 只取**前 N 条**，而不是把全表搬进内存 —— 这是它的结构优势
                # （线性扫描是 O(n)：每次检索都要读全部向量）。
                # `top_k * 4` 是给合并阶段留余量（关键词路也会贡献候选）。
                knn = self.db.vec_search(vec, limit=max(4, top_k * 4))
                if knn is not None:
                    for mid, sim in knn:
                        if sim > floor:
                            seen[mid] = seen.get(mid, 0.0) + sim
                else:
                    # ---- 退回线性扫描（KNN 不可用时）----
                    #
                    # ⚠️ 取全部向量是 **O(n)** 的 —— 这是这条退路的固有代价。
                    # 走 `all_vectors_raw()`（零拷贝）而不是 `all_vectors()`：
                    # 后者逐条 struct.unpack，实测 2000 条时那一趟占 61.5ms。
                    mids, blobs, dim = self.db.all_vectors_raw()
                    if dim and dim != len(vec):
                        log.warning(
                            "库里的向量是 %d 维，当前嵌入器是 %d 维 —— 向量路已跳过。"
                            "通常意味着换过 embedding 实现，或库被换成了旧备份；"
                            "重启一次会触发 `_migrate_vectors()` 重建",
                            dim,
                            len(vec),
                        )
                    else:
                        for mid, sim in zip(mids, _cosine_batch_raw(vec, blobs, dim)):
                            if sim > floor:
                                seen[mid] = seen.get(mid, 0.0) + sim
        except Exception:  # noqa: BLE001
            log.debug("向量检索跳过", exc_info=True)

        if not seen:
            """
            ## 🔴 泛问句兜底（2026-10-20）

            用户问「我在玩什么游戏」「我喜欢什么」这类**泛问**时，
            上面三条路全部命中 0 条：

              · 关键词路：`我在玩什么游戏` 分词后全是虚词，FTS 匹配不到东西；
              · 专有名词路：抽不出专有名词（"什么游戏"不是名词）；
              · 向量路：它跟任何一条具体记忆的相似度都低于 `min_score`
                （实测 0.4 门槛下全是 0.2x）——"我在玩什么"和"用户在玩《明日方舟》"
                在句向量上**不像**，因为一个是疑问、一个是陈述。

            结果就是：库里明明有"用户在玩《明日方舟：终末地》"，
            它却回答"我不太清楚你在玩什么"。**这比记错更伤**。

            ## 为什么这样兜底是安全的

            只在**全部落空**时才走这条路 —— 任何一条正路命中，就完全不碰它。
            而且返回的条目会在 `inject_hint` 里被标成"**按时间列的，可能与当前话题无关**"，
            让模型知道"这不是被问到的，是它最近记得的"。

            ## 为什么不按 kind 筛

            用户问"我在玩什么"想要的是 fact，但 `kind` 只有 fact/phrase 两种粗分类
            （见 `add_memory` 的 kind 参数），没有"游戏"这种语义桶。
            按 kind 筛等于什么都没筛。所以**按时间取最近的一组**，
            让模型自己从里面挑 —— 它的上下文理解比我们的分类准。
            """
            fallback = self._recent_fallback(query, top_k)
            if not fallback:
                return []
            seen = {int(item["id"]): 1.0 - idx * 0.01 for idx, item in enumerate(fallback)}
            fallback_ids = [int(item["id"]) for item in fallback]
            # 直接取出来返回（下面的通用查询会再查一遍，白跑；这里走捷径）
            out = [
                {
                    "id": int(item["id"]),
                    "text": str(item["summary"] or "")[:snippet],
                    "kind": str(item["kind"] or "fact"),
                    "level": str(item["level"] or "recent"),
                    "ts": float(item["ts"] or 0.0),
                    "created_at": str(item["created_at"] or ""),
                    # ⚠️ 标记来源：`inject_hint` 据此加一句"这是按时间列的"
                    "fallback": True,
                }
                for item in fallback
            ]
            if fallback_ids:
                self.db.bump_hits(fallback_ids)
                self.last_hit_ids = fallback_ids
            log.debug("检索全落空 -> 泛问句兜底返回最近 %d 条", len(out))
            return out

        # 按合并分数排序，取前 K
        ordered = sorted(seen.items(), key=lambda kv: -kv[1])[:top_k]
        ids = [mid for mid, _ in ordered]
        placeholders = ",".join("?" * len(ids))
        rows = self.db.conn.execute(
            f"SELECT id, summary, kind, level, ts, created_at FROM memory"
            f" WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        by_id = {int(r["id"]): r for r in rows}
        out: list[dict[str, Any]] = []
        for mid in ids:
            row = by_id.get(mid)
            if row is None:
                continue
            text = str(row["summary"] or "")
            if not text:
                continue
            out.append(
                {
                    "id": mid,
                    "text": text[:snippet],
                    "kind": str(row["kind"] or "fact"),
                    "level": str(row["level"] or "recent"),
                    "ts": float(row["ts"] or 0.0),
                    "created_at": str(row["created_at"] or ""),
                }
            )

        # ⚠️ **累加命中次数**（补上一个死字段，2026-10-05）
        #
        # `hits` 列从建表起就有，注释写着"用于淘汰时判断有没有用" ——
        # 但生产路径从来不累加（只有已被弃用的 `memory.py` 会加），
        # 于是它永远是 0，任何"按使用频率"的判断都无从下手，
        # 包括记忆压缩里"保留哪条作为代表"（最可靠的判据就是"它被用过"）。
        #
        # 放在这里而不是调用方：**所有**走 `_retrieve` 的路径都该计数。
        if out:
            self.db.bump_hits([item["id"] for item in out])
            # 记下这次命中的 id（2026-10-17）。
            #
            # 用途：「一段交流结束后强化」需要知道**这段对话里被想起过哪些记忆**。
            # 只存最近一次命中的 id，由调用方（gateway）在段内累积 ——
            # 不在这里累积，因为"段"的边界是对话层的概念，记忆层不该知道。
            self.last_hit_ids = [int(item["id"]) for item in out]
        else:
            self.last_hit_ids = []
        return out

    def _recent_fallback(self, query: str, top_k: int) -> list[dict[str, Any]]:
        """泛问句兜底：返回最近的一组记忆（见 `_retrieve_hits` 里那段说明）。

        ## 🔴 触发条件比"检索落空"更严

        只看"落空"是不够的 —— 用户问「那个 C_Thru 插件怎么配」（一个库里没有的名字）
        也会落空，那时**不该**拿一堆无关记忆去糊弄他，正确回答是"我不知道"。

        所以要**同时**满足：

        ```
        ① 检索真的全部落空（调用方保证）
        ② 问句是**泛问**：含疑问词，且不含任何专有名词
        ③ 库里确实有记忆
        ```

        ②的判据看 `_looks_like_general_question`。少这一条，兜底就会
        在"用户问了个具体但没见过的东西"时乱答 —— 那比不回答更糟。

        ## 为什么还要一个开关

        `memory.general_fallback`（默认开）。关掉 = 回到 2026-10-20 之前的行为。
        和 `lexical_fallback` 一样的理由：**留开关才能做对照实验**。
        """
        if not self._general_fallback_enabled():
            return []
        if not self._looks_like_general_question(query):
            return []
        assert self.db is not None
        try:
            rows = self.db.recent_memories(limit=max(1, min(20, top_k)))
        except Exception:  # noqa: BLE001 —— 兜底失败就当没兜底
            log.debug("泛问句兜底查询失败", exc_info=True)
            return []
        return [dict(row) for row in rows if str(row["summary"] or "").strip()]

    def _looks_like_general_question(self, query: str) -> bool:
        """这句话是不是"泛问"（问的是"有什么/是什么"，不是某个具体东西）。

        ## 两个条件都要满足

        ```
        ① 含疑问/求列举的词：什么 / 哪些 / 哪个 / 干啥 / 玩啥 / 喜欢啥 …
        ② **不含**专有名词（含了就是具体问题，不该兜底）
        ```

        ## ⚠️ 为什么必须排除"含专有名词"

        反例：「那个 C_Thru 插件怎么配」——含"怎么"，但 `C_Thru` 是专有名词。
        这种落空是**真的没有这条记忆**，正确回答是"我不知道"，
        而不是把最近 5 条记忆端上去，让它开始胡说。

        同理「银松镇那个机关怎么过」含"怎么"，但 `银松镇` 是专有名词 ->
        走专有名词兜底路，不走这里。
        """
        raw = str(query or "").strip()
        if not raw:
            return False
        # ① 先否决"求解释/求方法/求时间"的问句 —— 它们的答案不在记忆里
        if any(word in raw for word in _GENERAL_QUESTION_VETO):
            return False
        # ② 必须是"求列举"的问法
        if not any(word in raw for word in _GENERAL_QUESTION_WORDS):
            return False
        try:
            from . import nouns

            if nouns.extract_terms(raw):
                return False
        except Exception:  # noqa: BLE001 —— 抽词失败时宁可保守（不兜底）
            return False
        return True

    def _general_fallback_enabled(self) -> bool:
        """泛问句兜底开不开（`memory.general_fallback`，默认开）。"""
        value = (self.config.memory or {}).get("general_fallback")
        return True if value is None else bool(value)

    def _retrieve(self, query: str) -> list[str]:
        """**兼容旧接口**：只返回文本列表（测量脚本与老测试在用）。

        ⚠️ 新代码请用 `_retrieve_hits()` —— 它带时间戳与元数据，
        是提示词时间戳（用户要求）所必需的。这个方法只是薄封装。
        """
        return [item["text"] for item in self._retrieve_hits(query)]

    def retrieve(self, query: str) -> list[str]:
        """**公开**的检索入口：拿一句查询，返回相关记忆的文本列表。

        与 `_retrieve` 是同一个实现 —— 单独留一个公开名字，是为了让
        **质量测量脚本**能直接调它（`check-memory-quality.py`，历史实测脚本）。

        为什么不把测量脚本塞进 `inject_hint` 那条路：那个方法还会拼
        "固定注入 / 珍藏 / 最近对话"三段，把检索结果淹在里面，
        量出来的"找回率"就不干净了。

        ⚠️ 名字里没有下划线是**有意的** —— 它是给外部用的稳定接口，
        改内部实现时不该动它。
        """
        return self._retrieve(query)

    def _embed(self, text: str) -> list[float]:
        """算向量。

        ## 现在是**真语义模型**（bge-small-zh-v1.5 / ONNX）

        以前是 `HashingEmbedder`（零依赖，但**只有字面重合**）。
        实测对比（`check-bge.py`，历史实测脚本）：

            查询「喝咖啡」 vs 事实「每天喝一杯冰美式」
              哈希 0.044  ->  bge 0.549     ← 零共同字，纯语义
            查询「熬夜」   vs 事实「凌晨两点多睡」
              哈希 0.110  ->  bge 0.611

        ## ⚠️ 维度变了（256 -> 512），旧向量必须重建

        见 `_migrate_vectors()` —— 在 `__init__` 里调用。
        """
        return self.embedder.embed(text)

    # ------------------------------------------------------------ 写入

    def remember_turn(
        self,
        session_id: str,
        role: str,
        text: str,
        *,
        image: Optional[bytes] = None,
        image_mime: Optional[str] = None,
    ) -> int:
        """
        每轮对话都调：同时写"最近记忆"和"原文记忆"。

        ## 返回刚写进去的 `recent_turn.id`（0 表示没写）

        ## ⚠️ 为什么需要返回它（2026-10-08 修「消息好像传输了两次」）

        `gateway.ask()` 的调用顺序是：**先把用户这句写进记忆**（这样本轮检索
        能把刚说的话算进查询），**再拼提示词**。而"最近记忆"那段是把
        `recent_turn` 整段注入的 —— 于是**同一句话同时出现在两个地方**：

            messages[0] system  ← 记忆段里有「[今天] 用户：周杰伦的雨下一整晚。」
            messages[1] user    ← 本轮要问的也是「周杰伦的雨下一整晚。」

        模型看到两遍，于是**每轮都回「你说了两遍」** —— 用户以为是网络传输
        重了，实际是提示词里重复了。

        返回 id 让调用方能把"刚写的这条"从注入里排除掉
        （用 id 而不是文本匹配：用户完全可能连着说两遍同一句话，
         那时**两条都该在**，按文本排除会把真重复也去掉）。
        """
        if not self.db or not text.strip():
            return 0
        try:
            if image:
                # 截图回复：原图跟着这一句一起进原文记忆（BLOB 存库）
                return int(
                    self.db.add_turn(
                        session_id, role, text, image=image, image_mime=image_mime
                    )
                    or 0
                )
            return int(self.db.add_turn(session_id, role, text) or 0)
        except Exception:  # noqa: BLE001
            log.exception("写入对话失败（已忽略）")
            return 0

    def _fit_embedding_budget(self, text: str) -> list[str]:
        """把一条要入库的文本切成**每段都能完整进入嵌入模型**的碎片。

        ## 两道关卡，管的是不同的事

        | 关卡 | 依据 | 管什么 |
        |---|---|---|
        | ① `_split_for_embedding` | `memory.max_chars`（默认 200，**用户可配**）| "别太长" |
        | ② **本方法** | 模型的 **token 预算**（`token_budget()`，实测）| "**别超过模型能吃的量**" |

        ②是**最后一道保险**。为什么必须有它：`max_chars` 是按**字符**算的，
        而 token 与字符的比例**随语言变化** ——

            中文     1 字 ≈ 1 token     （实测 200 字 -> 202 tokens）
            英文     "internationalization" 20 字符 -> 8 tokens
                     "Hello World"         11 字符 -> 2~3 tokens

        所以同样的 200 字符，英文记忆完全可能在 512 token 之前爆掉。
        超了会怎样：**模型静默丢弃尾部**（`tokenize` 里 `max_tokens` 截断），
        那条记忆的后半截**永远检索不到** —— 而日志里一个字都没有。

        ⚠️ 所以这里**不是**"超了就丢弃"，而是**切开分别入库**：
        内容一条都不少，只是分散成几条向量。检索时各自独立命中，
        再合并回上下文（这正是切段设计的目的）。
        """
        pieces = self._split_for_embedding(text)
        if not self._has_local_embedder():
            return pieces

        from .embedding_local import count_tokens, split_by_tokens, token_budget

        budget = token_budget()
        out: list[str] = []
        for piece in pieces:
            used = count_tokens(piece)
            if used <= budget:
                out.append(piece)
                continue
            # 超预算 -> 按 token 切（尽量在句子边界）
            sub = split_by_tokens(piece)
            log.warning(
                "一条记忆占 %d tokens（预算 %d）—— 已切成 %d 段入库，"
                "避免尾部内容被静默丢弃：%r",
                used,
                budget,
                len(sub),
                piece[:36],
            )
            out.extend(sub)
        return out if out else pieces

    def _has_local_embedder(self) -> bool:
        """当前嵌入器是不是那个有 token 上限的本地模型。"""
        return type(self.embedder).__name__ == "BgeEmbedder"

    def _split_for_embedding(self, text: str) -> list[str]:
        """把过长的一条切成多段，让每段都在 embedding 模型的窗口内。

        ## 为什么必须切（不能靠"截断"）

        现在用的 hashing embedder 没有长度上限，但**计划要换的 `bge-small-zh-v1.5`
        硬上限是 512 token（约 250 个汉字）**，而且**超了是静默截断** ——
        不报错、不返回警告，后半句直接不参与向量。

        那种失败最难查：库里明明有这条记忆，检索却永远命中不到它后半句说的东西。

        所以**现在就把约束加上**（即使当前 embedder 不需要），这样：
          · 换模型那天不会突然开始丢内容；
          · 约束的边界由我们控制（切段），不是由模型的静默行为控制。

        ## 为什么是 200 字

        512 token ÷ 2（中文约 2 token/字）≈ 256 字，但要给 tokenizer 的
        分词开销留余量 —— 实测中文 BPE 在标点密集时能到 2.5 token/字。
        取 **200 字**留出足够余量，同时和 `snippet_chars: 120` 的注入口径兼容。
        """
        limit = self.max_chars
        body = text.strip()
        if len(body) <= limit:
            return [body] if body else []

        # 优先在句末标点处切，读起来自然；找不到就硬切
        min_tail = 20  # 尾段短于这个就并回上一段（见下）
        pieces: list[str] = []
        rest = body
        while len(rest) > limit:
            window = rest[:limit]
            cut = max(window.rfind(ch) for ch in "。！？；\n.!?;")
            # 切点太靠前就硬切（避免产出碎片）
            if cut < limit // 2:
                cut = limit - 1
            pieces.append(rest[: cut + 1].strip())
            rest = rest[cut + 1 :].strip()
            # ⚠️ 尾部只剩几个字时并回上一段，**不要单独成条**。
            #
            # 踩过：201 字被切成 `[200, 1]` —— 那个 1 字的条目对检索毫无价值，
            # 却照样占一个额度、算一次向量、在检索结果里占一个位置。
            # 宁可让某一段略超上限（超一点不会静默丢内容，因为切段的目的是"别超太多"），
            # 也不要产出没意义的碎片。
            if 0 < len(rest) < min_tail and pieces:
                pieces[-1] = f"{pieces[-1]}{rest}"
                rest = ""
        if rest:
            pieces.append(rest)
        log.warning(
            "一条记忆超过 %d 字（%d 字），已切成 %d 段 —— "
            "切段是为了兼容将来 embedding 模型的输入窗口（见 _split_for_embedding 的说明）",
            limit,
            len(body),
            len(pieces),
        )
        return [p for p in pieces if p]

    def remember_facts(
        self,
        facts: list[str],
        *,
        kind: str = "fact",
        tags: str = "",
        level: str = "recent",
        importance: float = 0.5,
        src_table: Optional[str] = None,
        src_id: Optional[int] = None,
        ts: Optional[float] = None,
    ) -> int:
        """把提炼出来的短事实写进**检索层**（唯一被提炼的一层）。

        ⚠️ 超过 `max_chars`（默认 200 字）的条目会被**切成多段**分别入库，
        而不是截断 —— 理由见 `_split_for_embedding`。

        ## ⚠️ 写入前查重（2026-10-05 实测发现的问题）

        用户会**反复聊同一件事**（"我在做桌宠"说了三次），而 `_extract_in_background`
        每轮都跑 —— 不去重的话库里会堆一串近乎一样的条目：

            用户在做桌面宠物项目
            用户在做桌面宠物项目          <- 完全重复
            用户使用 Electron 开发
            用户使用 Electron 开发项目     <- 语义重复（同一条事实换个说法）

        三个后果，都不是"占点空间"那么轻：
          ① 检索时**同一条记忆占掉多个 `top_k` 名额**，真正相关的其他记忆被挤掉；
          ② 提示词里出现重复内容，白占 token；
          ③ 界面上看到一串"复读"的记录，用户会以为程序坏了。

        **判据**：完全相同的正文 -> 跳过（`find_memory_by_text`）。
        语义近似的（"使用 Electron 开发" vs "使用 Electron 开发项目"）**这里不处理** ——
        那需要相似度判定，属于"记忆合并"功能（另做）；这里先解决最常见的
        "一字不差的重复"。

        ## ⚠️ `ts`：**回填历史时必须传**（2026-10-07 加）

        实时提炼不传 `ts`，于是落库时间是"现在" —— 那是对的，因为事实就是刚说的。

        但**回填历史**（`tools/backfill-memory.py`）不一样：那些事实来自几天前的
        对话，如果不传 `ts`，检索层里会全是 `10-07 21:05`。而提示词的每一段
        **都要带时间**（用户明确要求，见 `inject_hint` 的说明）—— 于是
        "用户在做桌面宠物项目（今天）"就在**说谎**：AI 会把旧事当成刚发生的事说。

        所以把 `ts` 一路透传到 `add_memory`，让记忆的时间是**它真正发生的时间**。
        """
        if not self.db or not facts:
            return 0
        written = 0
        skipped = 0
        for fact in facts:
            for text in self._fit_embedding_budget(fact):
                try:
                    # ---- 查重：先精确、再语义 ----
                    #
                    # 精确：同层同正文（`find_memory_by_text`）。
                    #
                    # 语义：正文不同但意思一样的。实测的重复形态：
                    #     用户在做桌面宠物项目
                    #     用户在做一个桌面宠物项目      <- 只差一个「一个」
                    #     用户使用 Electron 开发
                    #     用户使用 Electron 开发项目     <- 只多两个字
                    # 这类**精确查重抓不到**，而它会占掉检索名额
                    # （实测：因为这类重复，语义找回率从 100% 掉到 80%）。
                    if self.db.find_memory_by_text(text, level=level) is not None:
                        skipped += 1
                        continue
                    if self._is_semantic_duplicate(text, level):
                        skipped += 1
                        continue

                    # ---- 查 3：矛盾检测（§114 改动 5，2026-10-20）----
                    #
                    # 用户要求：「矛盾检测只在检索层对比」—— 就在这。
                    # 前两步（精确 / 语义）解决"一样的话说两遍"，
                    # 这一步解决"说的不一样、但互相否定"：
                    #
                    #     用户在打《银松镇》        <- 已在库
                    #     用户还没开玩《银松镇》     <- 现在要写，与前一条矛盾
                    #
                    # 两条同时存在的话，检索会把两条都召回、一起塞进提示词，
                    # AI 就会在"你在玩"和"你还没玩"之间摇摆 —— 比"想不起来"更糟，
                    # 因为它会**说错**。
                    #
                    # ⚠️ 命中后旧条目不删，降成 `archived`（见 `_find_contradiction`）。
                    contradicted = self._find_contradiction(text, level)
                    if contradicted is not None:
                        old_id, why = contradicted
                        try:
                            self.db.set_memory_level(old_id, "archived")
                            # 已归档的要从向量索引里摘掉，否则 KNN 还会召回它
                            # （`search_memory_like` 已经排除了 archived，
                            #   但 `vec_search` 只看索引）。
                            self.db.vec_drop(old_id)
                            self.archived_by_contradiction += 1
                            log.info("[记忆] 矛盾：#%d 被新条目取代（%s）", old_id, why)
                        except Exception:  # noqa: BLE001 —— 归档失败就并存，绝不能让写入崩
                            log.warning("[记忆] 矛盾命中但归档失败，两条并存（%s）", why)

                    vec = self._embed(text)
                    self.db.add_memory(
                        text,
                        # 按**主语**自动分 fact / self（2026-10-21）。
                        # 调用方传 kind="fact" 是"默认归类"，这里再按句首细分 ——
                        # 这样 gateway / backfill 都不用改，一处生效。
                        kind=self._kind_for(text, kind),
                        tags=tags,
                        level=level,
                        importance=importance,
                        src_table=src_table,
                        src_id=src_id,
                        vector=vec,
                        ts=ts,
                    )
                    written += 1
                except Exception:  # noqa: BLE001
                    log.exception("写入记忆失败：%r", text[:40])
        self.facts_written += written
        if skipped:
            log.info("跳过 %d 条重复的提炼结果（精确或语义）", skipped)
        # ── 兴趣自发形成（2026-10-17）──
        #
        # 用户原话：「爱好由我后续对话形成，利用检索层自发形成」。
        #
        # 关键设计：**从记忆条目里提词，不从原始对话里提**。
        #
        #   原始对话："我最近在玩银松镇，那个转盘机关卡了我好久"
        #     -> 分词切出 "好久"、"这款"、"有点累" 这种噪声（实测）
        #   记忆条目："用户在玩《银松镇》，被转盘机关卡住"
        #     -> 提炼那一步已经按"必须具体"筛过一遍，还带了《》
        #
        # 所以记忆层是**天然的过滤器**：LLM 提炼负责精度，这里负责累积。
        # 这也正好接上用户说的"利用检索层" —— 兴趣是从被检索的记忆里长出来的。
        if written:
            try:
                self._grow_interests(facts)
            except Exception:  # noqa: BLE001 —— 兴趣失败绝不影响记忆写入
                log.debug("更新兴趣失败（已忽略）", exc_info=True)
        return written

    def _grow_interests(self, facts: list[str]) -> None:
        """从刚写入的记忆里抽词并加权（见 `remember_facts` 里的说明）。"""
        from . import nouns

        if not facts:
            return
        ranked = nouns.extract_ranked(facts)
        if not ranked:
            return

        # ⚠️ 日期和版本号要**排除**（2026-10-17 真机验证发现）。
        #
        # `extract_ranked` 把它们标成 strong（它们确实是强信号），
        # 于是"GTA6 定在 2026-11-19 发售"之后，兴趣表里会多一条 `2026-11-19`。
        # 真机实测就是这个结果：
        #
        #     银松镇 0.67 / GTA6 0.55 / 2026-11-19 0.55
        #
        # 但"兴趣"要注入提示词、决定它聊什么（"你自己有兴致的东西：GTA6、2026-11-19"）。
        # 一句话里塞个日期进去很怪，它也没法"对日期感兴趣"。
        #
        # 日期在**检索兜底**里仍然有用（用户问"11月19号那个游戏"要能匹配），
        # 所以只在"变成兴趣"这一步过滤，不动 `extract_terms` 本身。
        def is_date_like(term: str) -> bool:
            return bool(nouns._DATE.fullmatch(term) or nouns._VERSION.fullmatch(term))

        strong = [
            t for t, kind, _ in ranked if kind == "strong" and not is_date_like(t)
        ]
        weak = [
            t
            for t, kind, count in ranked
            if kind == "weak"
            and count >= nouns.MIN_CJK_COUNT
            and not is_date_like(t)
        ]
        if strong:
            self.db.bump_interests(strong, strength="strong", source="memory")
        if weak:
            self.db.bump_interests(weak, strength="weak", source="memory")
        if strong or weak:
            log.info("兴趣更新：强信号 %s · 弱信号 %s", strong[:4], weak[:4])

    def _find_contradiction(self, text: str, level: str) -> tuple[int, str] | None:
        """找一条被 `text` **推翻**的既有记忆（§114 改动 5，2026-10-20）。

        返回 `(旧条目 id, 人话原因)`；没有矛盾返回 `None`。

        ## 为什么用纯文本、不走向量

        隔壁 `_is_semantic_duplicate()` 走向量，而它在 `_embed()` 失败时
        （`if not vec: return False`）**静默降级成"不重复"**。
        矛盾检测若也走向量，嵌入一旦不可用（模型缺失、维度漂移、降级成 hashing）
        它就会**悄悄失效** —— 用户看到的只是"它又开始把新旧说法混着说了"，
        没有任何报错。这种无声失效最难查，所以这里不依赖向量。

        而且语义相似度对**极性**本来就不敏感：「在打银松镇」和「还没玩银松镇」
        的向量可能比「在打银松镇」和「在打明日方舟」还近。

        ## 三个安全阀（缺一个都可能变成"悄悄删记忆"）

        ```
        ① 拿不准就不动 —— `nouns.is_contradiction` 要求两个条件同时满足，否则 False
        ② 旧的**不删**，只降成 level='archived' —— 记忆管理页面还看得见、能改回来
        ③ 每次命中都打日志 —— "[记忆] 矛盾：#12 被新条目取代（共用「银松镇」，玩没玩相反）"
        ```

        ⚠️ 第 ② 条尤其重要：**误判的代价必须是"能撤销"，不能是"信息没了"**。

        ## 为什么候选只按专有名词粗筛

        全表逐条比对太慢（每轮提炼都要跑）。而两条矛盾的记忆**必然**共用
        同一个专有名词（否则说的根本不是同一个东西），所以先用
        `memory_candidates_for_contradiction` 在 SQL 里筛出"至少含一个共同词"的，
        再逐条做极性判断。实测库里有 100 多条记忆时，一次筛出 0~5 条。
        """
        from . import nouns

        if level == "archived":
            # 写进归档层的本来就不参与检索，没有矛盾可言
            return None
        try:
            terms = nouns.extract_terms(text)
            if not terms:
                return None
            candidates = self.db.memory_candidates_for_contradiction(terms)
            for row in candidates:
                old_text = str(row["summary"] or "")
                old_level = str(row["level"] or "")
                if old_level == "archived":
                    continue
                # ⚠️ 固定注入层不参与：那是用户手选的"每次都带上"，
                #    自动归档它等于替用户改了设定。
                if old_level == "fixed":
                    continue
                hit, why = nouns.is_contradiction(old_text, text)
                if hit:
                    return int(row["id"]), why
        except Exception:  # noqa: BLE001 —— 矛盾检测是加分项，失败就当没矛盾
            log.debug("矛盾检测跳过（异常）", exc_info=True)
        return None

    def _asks_about_self(self, query: str) -> bool:
        """这句问话是不是在问**它自己**（2026-10-21）。

        ## 为什么必须窄

        这个判据决定"要不要把它自己的事拉进候选"。判宽了，用户聊自己的事
        它也会把自己的经历塞进提示词 —— 那会变成**抢话题**，
        比"想不起来自己的事"更让人烦。

        所以只认明确的第二人称/指向它的说法：

        ```
        你 / 你自己 / 你怎么想 / 你叫什么 / 你有啥想法
        ```

        ⚠️ **不认**"你喜欢什么"这种 —— 那多半是在问"你喜欢什么（东西）"，
        而用户真正想聊的是自己的喜好。实测这类歧义句里，用户的意图
        几乎都是问自己。
        """
        raw = str(query or "").strip()
        if not raw:
            return False
        # 明确的"你自己"
        if "你自己" in raw or "你自个" in raw:
            return True
        # 直接问它的名字/身份/感受
        for pattern in ("你叫什么", "你是谁", "你的名字", "你怎么想", "你觉得呢", "你有啥", "你有什么"):
            if pattern in raw:
                return True
        return False

    def _kind_for(self, text: str, default: str = "fact") -> str:
        """按**主语**把提炼结果分到 `fact` / `self`（2026-10-21）。

        ## 为什么要分

        用户要求：「检索层要能显示玩伴自己的事实，比如不顺着用户的话而是
        自己说出要改名成阿拾一样，或者安慰人等等。」

        这些条目原本和"用户喜欢曲娘"混在同一个 `fact` 里。后果有两个：

          · **检索时张冠李戴** —— 问"我喜欢什么"，可能召回"宠物喜欢安静"，
            而那是它自己的偏好；
          · **界面看不出区别** —— 用户翻列表时分不清哪条是谁的事。

        ## 判据：句首主语

        ```
        宠物 / 它自己 / 阿拾 开头  -> self
        其它（用户 / 他 / 她）      -> fact
        ```

        ⚠️ 只认**句首**，不认句中。"用户说宠物答错了"的主语是用户，
        它记的是用户做了什么 —— 归 `fact` 才对，句中那个"宠物"不算。

        ⚠️ 认不出来就归 `default`。**宁可错归 fact，也不要把用户的事
        标成它自己的** —— 后者会让它把用户的信息当成自我认知。
        """
        raw = str(text or "").strip()
        if not raw:
            return default
        if raw.startswith(("宠物", "它自己", "阿拾")):
            return "self"
        return default

    def _is_semantic_duplicate(self, text: str, level: str) -> bool:
        """这条是不是已经有一条**意思相同**的（正文不必相同）。

        ## 阈值 0.92 是**保守**的，有意的

        bge 的相似度分布（`tools/calibrate-min-score.py` 实测）：

            无关句对          0.17 ~ 0.39
            相关但不同的事     0.40 ~ 0.60
            **几乎同义**       0.90+          ← 只有这一档才算重复

        取 0.92 意味着"只合并几乎一模一样的"，宁可漏合也不误合 ——
        因为**误合会静默丢信息**（用户说过的事被当成重复丢掉），
        而漏合只是多占一个名额。两边的代价不对称，所以偏向保守。

        ⚠️ 这里**只比正文**，不做跨语言/跨表达的语义合并 ——
        那属于「记忆合并」功能（把两条合成一条更完整的），是另一件事。
        """
        try:
            vec = self._embed(text)
            if not vec:
                return False
            for other in self.db.list_memory(limit=500, level=level):
                row = self.db.conn.execute(
                    "SELECT vector FROM memory_vec WHERE memory_id = ?",
                    (int(other["id"]),),
                ).fetchone()
                if row is None:
                    continue
                from .store import unpack_vector  # 局部导入避免循环

                other_vec = unpack_vector(bytes(row["vector"]))
                # ⚠️ 维度不等时 `_cosine` 返回 0（安全），但那种情况本该被
                # `_migrate_vectors()` 修掉；这里再判一次是防御。
                if len(other_vec) == len(vec) and _cosine(vec, other_vec) >= 0.92:
                    log.debug("语义重复，跳过：%r ≈ %r", text[:24], str(other["summary"])[:24])
                    return True
        except Exception:  # noqa: BLE001 —— 查重失败不该阻止写入
            log.debug("语义查重失败（按不重复处理）", exc_info=True)
        return False

    def remember_image(self, session_id: str, description: str) -> None:
        """图片内容也进原文记忆（用户要求"原文字或图片内容"）。

        只存**描述或路径**，不存二进制 —— 原因写在 `raw_log.kind` 的注释里。
        """
        if not self.db:
            return
        try:
            self.db.conn.execute(
                "INSERT INTO raw_log(session_id, role, text, kind, created_at, ts)"
                " VALUES (?,?,?,'image',?,?)",
                (session_id, "user", description, _now_iso(), time.time()),
            )
            self.db.conn.commit()
        except Exception:  # noqa: BLE001
            log.exception("写入图片描述失败（已忽略）")

    # ------------------------------------------------------------ 管理界面用

    def list_raw(
        self,
        limit: int = 50,
        before_ts: Optional[float] = None,
        *,
        since_ts: Optional[float] = None,
        until_ts: Optional[float] = None,
        role: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """原文对话分页（最新在前）。

        支持**时间范围**（用户要求：「加入时间搜索，方便通过时间查询」）
        与**按角色过滤**（只看我说的 / 只看宠物说的）。
        """
        if not self.db:
            return []
        return [
            _row_dict(r)
            for r in self.db.recent_turns_page(
                limit=limit,
                before_ts=before_ts,
                since_ts=since_ts,
                until_ts=until_ts,
                role=role,
            )
        ]

    def get_raw_image(self, raw_id: int) -> Optional[tuple[bytes, str]]:
        """取某条原文的截图原文（BLOB）。没有图返回 None。"""
        if not self.db:
            return None
        return self.db.raw_image(raw_id)

    def raw_context(self, raw_id: int, before: int = 5, after: int = 5) -> list[dict[str, Any]]:
        """取某条原文**上下 N 条**（用户要求：「上下 1-100 条的数据范围搜索展示」）。

        这是"标记珍藏时想看看上下文再勾选"的数据来源。
        """
        if not self.db:
            return []
        return [_row_dict(r) for r in self.db.raw_neighborhood(raw_id, before=before, after=after)]

    def search_raw(
        self, query: str, limit: int = 50, role: Optional[str] = None
    ) -> list[dict[str, Any]]:
        if not self.db or not query.strip():
            return []
        return [_row_dict(r) for r in self.db.search_raw(query, limit=limit, role=role)]

    def list_cherished(self) -> list[dict[str, Any]]:
        if not self.db:
            return []
        out = []
        for r in self.db.list_cherished():
            d = _row_dict(r)
            # SQLite 存的是 0/1，发给前端前转成真正的布尔 ——
            # 否则渲染层要写 `pinned === 1`，那种约定很容易在别处漏掉
            d["pinned"] = bool(r["pinned"])
            out.append(d)
        return out

    def cherished_detail(self, cher_id: int) -> dict[str, Any]:
        """一段珍藏的**完整内容**：元信息 + 每一轮。

        这是"点开一条珍藏、复现那几轮对话"的数据来源（用户要求）。
        复现要按发生顺序、区分谁说的，所以带上 `role` 与**原文当时的时间**。
        """
        if not self.db:
            return {"ok": False, "message": "记忆库未启用"}
        row = self.db.get_cherished(cher_id)
        if row is None:
            return {"ok": False, "message": "该段珍藏不存在（可能已被删）"}
        info = _row_dict(row)
        info["pinned"] = bool(row["pinned"])
        return {
            "ok": True,
            "entry": info,
            "turns": [_row_dict(t) for t in self.db.cherished_turns(cher_id)],
        }

    @staticmethod
    def _turn_from_raw(row: Any) -> dict[str, Any]:
        """`raw_log` 行 -> 珍藏轮次。

        把"原文行"转成"珍藏轮"，**复制**文本与时间。
        之所以复制而不是只存 id 去 JOIN：珍藏是"永不淘汰"的一层，
        它不该依赖于原文表的寿命（虽然当前原文也不删，但把依赖去掉更稳）。
        """
        return {
            "role": str(row["role"]),
            "text": str(row["text"]),
            "created_at": str(row["created_at"]),
            "ts": float(row["ts"]),
            "src_raw_id": int(row["id"]),
        }

    def cherish(
        self,
        *,
        raw_id: Optional[int] = None,
        raw_ids: Optional[list[int]] = None,
        text: str = "",
        title: str = "",
        pinned: bool = True,
    ) -> dict[str, Any]:
        """把**一条或多条**对话（或任意文本）存成一段珍藏记忆。

        三种来源，优先级从高到低：
          · `raw_ids` —— 界面上**勾选多条**（用户要求：
            「添加珍藏回忆可以勾选一条或多条对话进去」）；
          · `raw_id`  —— 单条（保留旧的调用方式，不必让所有调用点都改成数组）；
          · `text`    —— 直接给文本（手动新建）。

        ⚠️ **不修改 `raw_log`**：这里只把原文的副本写进珍藏。
        """
        if not self.db:
            return {"ok": False, "message": "记忆库未启用"}

        ids: list[int] = []
        if raw_ids:
            ids = [int(i) for i in raw_ids]
        elif raw_id is not None:
            ids = [int(raw_id)]

        session_id: Optional[str] = None
        src_from: Optional[int] = None
        src_to: Optional[int] = None
        body = text.strip()
        turns: Optional[list[dict[str, Any]]] = None

        if ids:
            rows = self.db.raw_by_ids(ids)
            if not rows:
                return {"ok": False, "message": "选中的对话都找不到了（可能已被清理）"}
            turns = [self._turn_from_raw(r) for r in rows]
            # 注入用的拼接结果（父行的 text 会被子表重算，这里给一个初值）
            body = "\n".join(
                f"{'用户' if t['role'] == 'user' else self.config.pet_name}：{t['text']}"
                for t in turns
            )
            first, last = rows[0], rows[-1]
            session_id = str(first["session_id"])
            src_from, src_to = int(first["id"]), int(last["id"])
            if not title.strip():
                # 自动起标题：一轮就用它原文；多轮用首尾各一小段
                who = "我" if first["role"] == "user" else self.config.pet_name
                if len(turns) == 1:
                    title = f"{who}：{turns[0]['text'][:14]}"
                else:
                    title = f"{who}：{turns[0]['text'][:10]}…（{len(turns)} 轮）"

        if not body and not turns:
            return {"ok": False, "message": "内容是空的，没什么可珍藏的"}

        try:
            new_id = self.db.add_cherished(
                body,
                title=title,
                pinned=pinned,
                session_id=session_id,
                src_from=src_from,
                src_to=src_to,
                turns=turns,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("珍藏失败")
            return {"ok": False, "message": f"珍藏失败：{exc}"}
        n = len(turns) if turns else 0
        log.info("珍藏 #%d：%d 轮 / %d 字，固定注入=%s", new_id, n, len(body), pinned)
        desc = f"{n} 轮对话" if n > 1 else f"{len(body)} 字"
        return {"ok": True, "id": new_id, "message": f"已珍藏（{desc}）"}

    def expand_cherished(
        self,
        cher_id: int,
        raw_id: int,
        *,
        before: int = 0,
        after: int = 0,
    ) -> dict[str, Any]:
        """把某条原文**上下 N 条**并入已有的一段珍藏（用户要求：一键扩充）。

        「也可以一键勾选填上下多少数据来添加进当前这条珍藏数据扩充此条珍藏
        记忆的对话长度」

        以 `raw_id` 为锚点：取它上下 N 条（含它自己），追加到珍藏里。
        重复的会被跳过（`cherished_turn` 上的唯一索引 + 这里的去重）——
        用户会反复点"扩充"，不去重的话复现里会出现两遍同一句话。
        """
        if not self.db:
            return {"ok": False, "message": "记忆库未启用"}
        if before <= 0 and after <= 0:
            return {"ok": False, "message": "上下条数不能都是 0"}

        rows = self.db.raw_neighborhood(raw_id, before=int(before), after=int(after))
        if not rows:
            return {"ok": False, "message": f"找不到原文 #{raw_id}"}
        added = self.db.add_cherished_turns(cher_id, [self._turn_from_raw(r) for r in rows])
        total = len(self.db.cherished_turns(cher_id))
        if added == 0:
            return {"ok": True, "message": f"这 {len(rows)} 轮都已经在里面了", "added": 0, "total": total}
        return {"ok": True, "message": f"已扩充 {added} 轮（现在共 {total} 轮）", "added": added, "total": total}

    def remove_cherished_turn(self, turn_id: int) -> dict[str, Any]:
        """从珍藏里**移除某一轮**。

        ⚠️⚠️ **只删珍藏里的这一行，`raw_log`（原文记忆）一个字都不动。**

        用户明确要求：「这些删除的操作不作用于原文记忆本身的数据，
        只是从那些需要删除操作的珍藏记忆中消失去除掉而已，
        不动原文记忆本身数据」。
        """
        if not self.db:
            return {"ok": False, "message": "记忆库未启用"}
        if not self.db.remove_cherished_turn(turn_id):
            return {"ok": False, "message": "这一段不存在（可能已被删）"}
        return {"ok": True, "message": "已从珍藏里移除这一段（原文记忆未受影响）"}

    def update_cherished(
        self,
        cher_id: int,
        *,
        title: Optional[str] = None,
        text: Optional[str] = None,
        pinned: Optional[bool] = None,
    ) -> dict[str, Any]:
        if not self.db:
            return {"ok": False, "message": "记忆库未启用"}
        changed = False
        if title is not None or text is not None:
            changed |= self.db.update_cherished(cher_id, title=title, text=text)
        if pinned is not None:
            changed |= self.db.set_cherished_pinned(cher_id, pinned)
        if not changed:
            return {"ok": False, "message": "没有可改的内容（或该条不存在）"}
        return {"ok": True, "message": "已保存"}

    def remove_cherished(self, cher_id: int) -> dict[str, Any]:
        if not self.db:
            return {"ok": False, "message": "记忆库未启用"}
        if not self.db.remove_cherished(cher_id):
            return {"ok": False, "message": "该条不存在（可能已被删）"}
        return {"ok": True, "message": "已取消珍藏"}

    def list_memory(self, level: Optional[str] = None) -> list[dict[str, Any]]:
        if not self.db:
            return []
        return [_row_dict(r) for r in self.db.list_memory(level=level)]

    def remove_memory(self, memory_id: int) -> dict[str, Any]:
        if not self.db:
            return {"ok": False, "message": "记忆库未启用"}
        if not self.db.remove_memory(memory_id):
            return {"ok": False, "message": "该条不存在（可能已被删）"}
        return {"ok": True, "message": "已删除"}

    def remove_memories(self, ids: list[int]) -> dict[str, Any]:
        """
        **批量真删**检索层条目，返回实际删掉的条数。

        ## 与「压缩」的语义区别（用户 2026-10-07 要求）

        | | 压缩 | 删除 |
        |---|---|---|
        | 做法 | 标记 `merged_into` | **真删**（memory + 向量 + KNN 索引）|
        | 可还原 | ✅ 一键还原 | ❌ **不可还原** |
        | 治什么 | 冗余（同样的信息说太多遍）| **错的东西**（记错了、不该记）|

        两者并存是有意的 —— 压缩的"不删只标记"是为了"防止重要信息被重复度
        选中后丢失"，而有些条目是**根本不该存在**（提炼错了、敏感信息），
        那时"标记"没有意义，必须真删。

        ## 为什么逐条走 `db.remove_memory`

        它负责**同时**清三处：`memory` 行、`memory_vec` 向量、`memory_vec0`
        KNN 索引。少清任何一处都会留下"幽灵条目"：正文取不回来，但排名占着
        位置 —— 那种最难查。

        不合法/不存在的 id 静默跳过（界面可能拿着过期的选择）。
        """
        if not self.db:
            return {"ok": False, "message": "记忆库未启用", "removed": 0}
        removed = 0
        for raw in ids:
            try:
                mid = int(raw)
            except (TypeError, ValueError):
                continue
            try:
                if self.db.remove_memory(mid):
                    removed += 1
            except Exception:  # noqa: BLE001 —— 单条失败不该中断整批
                log.exception("删除记忆 %s 失败（继续删其余的）", mid)
        return {"ok": True, "removed": removed, "message": f"真删了 {removed} 条（不可还原）"}

    def set_memory_level(self, memory_id: int, level: str) -> dict[str, Any]:
        """改层级。`fixed` = 每次对话无条件注入（这就是"固定记忆"的手动方式）。"""
        if not self.db:
            return {"ok": False, "message": "记忆库未启用"}
        try:
            ok = self.db.set_memory_level(memory_id, level)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        if not ok:
            return {"ok": False, "message": "该条不存在（可能已被删）"}
        label = {"fixed": "固定注入", "recent": "按需检索", "archived": "已归档"}.get(level, level)
        return {"ok": True, "message": f"已改为「{label}」"}

    def fixed_list(self) -> list[dict[str, Any]]:
        """当前**固定注入**的全部条目（「固定注入提示词」页面的右半边）。

        `fixed_memories()` 只返回注入要用的几个字段；这里要更多
        （id/kind/tags/importance/created_at），因为界面上要显示并支持删除。
        """
        if not self.db:
            return []
        return [_row_dict(r) for r in self.db.list_memory(limit=2000, level="fixed")]

    def add_fixed(self, text: str, *, kind: str = "phrase", tags: str = "手动") -> dict[str, Any]:
        """把一句话加进**固定注入**（每次对话无条件带上）。

        ## ⚠️ 幂等：已经在了就不再新增

        同一句口头禅被加两次，注入里就会出现两遍 —— 白白占提示词，
        而且用户很难发现（列表里两条一模一样的）。所以先按正文查重。

        ## 与 `remember_facts` 的区别

        那个是**自动提炼**的入口（模型抽出来的事实，进 `recent` 层）；
        这个是**用户手动**选的（进 `fixed` 层，重要性给满）。
        两者的来源与可信度都不同，混在一个方法里会让"这条是怎么来的"变模糊。
        """
        if not self.db:
            return {"ok": False, "message": "记忆库未启用"}
        body = text.strip()
        if not body:
            return {"ok": False, "message": "内容是空的"}

        existing = self.db.find_memory_by_text(body, level="fixed")
        if existing is not None:
            return {
                "ok": True,
                "id": int(existing["id"]),
                "message": "已经在固定注入里了",
                "already": True,
            }

        try:
            # 切段：与其它入口同一套约束（固定注入也是要注入的，太长同样有害）
            pieces = self._split_for_embedding(body)
            first_id = 0
            for piece in pieces:
                mid = self.db.add_memory(
                    piece,
                    kind=kind,
                    tags=tags,
                    level="fixed",
                    importance=0.9,  # 用户手动选的，比自动提炼的重要
                    vector=self._embed(piece),
                )
                first_id = first_id or mid
        except Exception as exc:  # noqa: BLE001
            log.exception("加入固定注入失败")
            return {"ok": False, "message": f"加入失败：{exc}"}
        log.info("加入固定注入：%r（%d 字，切 %d 段）", body[:30], len(body), len(pieces))
        suffix = f"（太长，切成 {len(pieces)} 段）" if len(pieces) > 1 else ""
        return {"ok": True, "id": first_id, "message": f"已加入固定注入{suffix}"}

    def remove_fixed(self, memory_id: int) -> dict[str, Any]:
        """从固定注入里移除。

        实现就是 `remove_memory` —— 因为"固定注入"只是 `memory.level='fixed'`，
        没有单独的表。但这里**单独给个名字**：调用方读到 `remove_fixed` 时
        意图清楚，而 `remove_memory` 听起来像"删掉一条记忆"（容易被误解为
        连原文一起删）。语义清楚比少一个方法重要。
        """
        return self.remove_memory(memory_id)

    # ------------------------------------------------------------ 备份

    def backup_on_start(self) -> BackupResult:
        """启动时自动备份。

        ## 语义（2026-10-05 澄清）

        用户在设置页勾了开关，就认为"自动备份是开着的"。所以这里：
          · **文件夹没设** -> 明确提示"还没选文件夹"（这不是失败，是待办）；
          · **文件夹设了** -> 直接备份，不再看 `enabled`。

        为什么不再看 `enabled`：勾选框和"有没有文件夹"**表达的是同一件事** ——
        上一次的实现要求两者同时满足，于是"勾上了但没选文件夹"会**静默什么都不做**，
        而"选了文件夹但忘了勾"也一样。两处状态一条路径，少一个能不同步的地方。
        """
        if not self.db:
            return BackupResult(False, "记忆库未打开")
        section = (self.config.memory or {}).get("backup") or {}
        folder = str(section.get("dir") or "").strip()
        if not folder:
            return BackupResult(False, "还没设置备份文件夹")
        # keep <= 0 表示保留全部（见 store.backup_to）
        keep = int(section.get("keep") or 0)
        try:
            result = self.db.backup_to(Path(folder), keep=keep, stage_dir=self._stage_dir())
        except Exception as exc:  # noqa: BLE001
            log.exception("备份异常")
            return BackupResult(False, f"备份异常：{exc}")
        if result.ok:
            log.info("记忆库已备份到 %s", result.path)
        else:
            log.warning("记忆库备份失败：%s", result.message)
        return result

    # ------------------------------------------------------------ 记忆压缩

    def compress_config(self) -> dict[str, Any]:
        """压缩相关的配置（带默认值与**夹取**）。

        ⚠️ 阈值范围是 **0.5 ~ 1.0**（用户指定）—— 夹取要在这里做，
        不能只靠界面校验：配置是文本文件，用户可能手改成任何值，
        而一个 0.1 的阈值会让"什么都被聚成一组"，把记忆**全部压掉**。
        """
        raw = dict((self.config.memory or {}).get("compress") or {})
        # ⚠️ `pricing` 是给 `estimate_ai_cost()` 用的，不进界面配置接口 ——
        # 这里显式排除，免得两个地方各回一份、以后改一处漏一处。
        section = {k: v for k, v in raw.items() if k != "pricing"}
        try:
            threshold = float(section.get("similarity") or 0.75)
        except (TypeError, ValueError):
            threshold = 0.75
        threshold = min(1.0, max(0.5, threshold))
        try:
            min_group = int(section.get("min_group") or 3)
        except (TypeError, ValueError):
            min_group = 3
        min_group = min(50, max(2, min_group))
        return {
            "similarity": threshold,
            "min_group": min_group,
            "mode": str(section.get("mode") or "manual"),
            "prompt": str(section.get("prompt") or ""),
        }

    def _cluster_memories(
        self,
        items: list[dict[str, Any]],
        threshold: float,
        min_group: int,
    ) -> list[dict[str, Any]]:
        """对记忆做**单链聚类**，返回大小达标的组。

        ## 为什么是单链（而不是"跟簇心比"）

        同一件事的多种说法之间往往是**链式**相似的
        （"用户问在不在" ~ "用户说在吗" ~ "用户问你在干嘛"，
        首尾两条可能差得较远）。用簇心会漏掉这种链。

        ⚠️ 单链的风险是"链式串大"（长链把无关的也拉进来）。
        实测在 0.75 阈值下没发生（`check-compress-detect.py`，历史实测脚本），
        但要盯着 —— 所以**簇大小只是个"值得看一眼"的信号，不是自动合并的依据**。

        ## 为什么不能只卡相似度（实测结论）

        「用户在学 Rust」vs「用户使用 Electron 开发」相似度 **0.772** ——
        都是编程话题，向量上就是近，**但它们是两件不同的事**。
        卡阈值必然二选一：要么漏掉琐事，要么把真事实也合掉。

        而**簇大小**能把它们分开：真的"常说的话"会反复出现（≥3 条成组），
        无关的两件事偶尔相似只形成大小 2 的对。实测：琐事组识别 5/5、
        真实库条目零误聚。
        """
        vecs: dict[int, list[float]] = {}
        for it in items:
            v = it.get("vec")
            if v:
                vecs[int(it["id"])] = v

        clusters: list[list[dict[str, Any]]] = []
        for it in items:
            mid = int(it["id"])
            mine = vecs.get(mid)
            if not mine:
                continue
            placed = False
            for group in clusters:
                # 单链：与**任一**成员够像就算同组
                for member in group:
                    other = vecs.get(int(member["id"]))
                    if other and _cosine(mine, other) >= threshold:
                        group.append(it)
                        placed = True
                        break
                if placed:
                    break
            if not placed:
                clusters.append([it])

        out: list[dict[str, Any]] = []
        for group in clusters:
            if len(group) < min_group:
                continue
            # ---- 选"代表条"（不合并时保留哪条）----
            #
            # 判据顺序（实测出来的优先级）：
            #   ① `hits` 最多 —— "它被用过"是最可靠的信号；
            #      ⚠️ 但 `hits` 2026-10-05 之前从不累加（死字段），
            #      所以历史条目全是 0，会退到 ②；
            #   ② 到组内其他成员的**平均相似度**最高（最"中心"的那条）；
            #   ③ 最长（信息量最大）。
            def avg_sim(member: dict[str, Any]) -> float:
                mv = vecs.get(int(member["id"]))
                if not mv:
                    return 0.0
                sims = [
                    _cosine(mv, vecs[int(o["id"])])
                    for o in group
                    if int(o["id"]) != int(member["id"]) and int(o["id"]) in vecs
                ]
                return sum(sims) / len(sims) if sims else 0.0

            rep = max(
                group,
                key=lambda m: (
                    int(m.get("hits") or 0),
                    round(avg_sim(m), 4),
                    len(str(m.get("summary") or "")),
                ),
            )
            # 组内两两相似度（给界面一个"这一组有多像"的概览）
            pairwise: list[float] = []
            for i, a in enumerate(group):
                for b in group[i + 1 :]:
                    va, vb = vecs.get(int(a["id"])), vecs.get(int(b["id"]))
                    if va and vb:
                        pairwise.append(_cosine(va, vb))
            out.append(
                {
                    "size": len(group),
                    "avg_similarity": round(sum(pairwise) / len(pairwise), 4) if pairwise else 0.0,
                    "min_similarity": round(min(pairwise), 4) if pairwise else 0.0,
                    "keep_id": int(rep["id"]),
                    "members": [
                        {
                            "id": int(m["id"]),
                            "summary": str(m.get("summary") or ""),
                            "hits": int(m.get("hits") or 0),
                            "level": str(m.get("level") or "recent"),
                            "when": self._when(m.get("ts")),
                        }
                        for m in group
                    ],
                }
            )
        # 大组排前面（更值得处理）
        out.sort(key=lambda g: (-g["size"], -g["avg_similarity"]))
        return out

    def compress_scan(self, threshold: Optional[float] = None) -> dict[str, Any]:
        """扫描检索层，找出**检索重复信息 ≥ N** 的组（不修改任何数据）。

        「检索重复信息 ≥3」是用户指定的说法 —— 界面上就该这么显示，
        不要用"簇"这种技术词。

        ⚠️ **只扫活跃条目**（`merged_into IS NULL`）—— 已经压过的不该再报一次。
        """
        if not self.db:
            return {"ok": False, "message": "记忆库未启用"}
        conf = self.compress_config()
        th = conf["similarity"] if threshold is None else min(1.0, max(0.5, float(threshold)))
        floor = conf["min_group"]

        rows = list(
            self.db.conn.execute(
                "SELECT id, summary, kind, level, hits, ts, created_at"
                "  FROM memory WHERE merged_into IS NULL ORDER BY id"
            )
        )
        items: list[dict[str, Any]] = []
        # 取向量（一次性，避免逐条查）
        mids, blobs, dim = self.db.all_vectors_raw(active_only=True)
        from .store import unpack_vector

        by_id = {int(m): unpack_vector(b) for m, b in zip(mids, blobs)}
        for r in rows:
            mid = int(r["id"])
            items.append(
                {
                    "id": mid,
                    "summary": str(r["summary"]),
                    "kind": str(r["kind"]),
                    "level": str(r["level"]),
                    "hits": int(r["hits"] or 0),
                    "ts": float(r["ts"] or 0.0),
                    "vec": by_id.get(mid),
                }
            )

        groups = self._cluster_memories(items, th, floor)
        return {
            "ok": True,
            "scanned": len(items),
            "threshold": th,
            "min_group": floor,
            "groups": groups,
            "group_count": len(groups),
            "merged_count": self.db.count_merged(),
        }

    def compress_apply(
        self,
        groups: list[dict[str, Any]],
        *,
        ai_merge: bool = False,
        prompt: str = "",
    ) -> dict[str, Any]:
        """执行压缩：把每组里"非代表条"标记为已压缩。

        ## 两种模式（用户要的三选一里的两种）

        | 模式 | 做什么 |
        |---|---|
        | **保留代表条**（`ai_merge=False`）| 标记其余 → 代表条不动，检索层少几条 |
        | **AI 合并**（`ai_merge=True`）| 让模型把整组写成**一条更完整的**，新建一条，原组全部指向它 |

        ⚠️ 两种都**不删数据**：被标记的条目只是不参与检索，可在压缩页还原。

        `groups` 的形状与 `compress_scan` 返回的一致（界面把它回传即可）——
        ⚠️ 但**不信任界面传来的 keep_id**：必须校验它确实在本组成员里，
        否则一个构造出来的 id 会把不相干的记忆压到一起。
        """
        if not self.db:
            return {"ok": False, "message": "记忆库未启用"}
        merged_total = 0
        created: list[int] = []
        skipped: list[str] = []

        for group in groups or []:
            members = [int(m.get("id")) for m in (group.get("members") or []) if m.get("id")]
            keep_id = int(group.get("keep_id") or 0)
            # 护栏：代表条必须在组内
            if keep_id not in members:
                skipped.append(f"代表条 {keep_id} 不在组内，已跳过该组")
                continue
            others = [m for m in members if m != keep_id]
            if not others:
                continue

            if ai_merge:
                texts = [
                    str(m.get("summary") or "")
                    for m in (group.get("members") or [])
                    if int(m.get("id") or 0) in members
                ]
                body, err = self._ai_merge_texts(texts, prompt)
                if not body:
                    skipped.append(f"AI 合并失败（{err}），该组保持原样")
                    continue
                # 新建一条"合并后的记忆"（视为**提炼过的记忆**，只进检索层）
                new_id = self.db.add_memory(
                    body,
                    kind="fact",
                    tags="合并",
                    level="recent",
                    importance=0.6,
                    vector=self._embed(body),
                )
                created.append(new_id)
                # 原组**全部**指向新条目（包括原来的代表条）
                merged_total += self.db.merge_memory_into(members, new_id)
            else:
                merged_total += self.db.merge_memory_into(others, keep_id)

        return {
            "ok": True,
            "merged": merged_total,
            "created": created,
            "skipped": skipped,
            "remaining": self.db.count_active(),
        }

    def _pricing(self) -> dict[str, Any]:
        """AI 压缩的计费参数（全部来自配置，**不写死在代码里**）。

        ## 为什么放配置

        价格会变（2026-10-05 核对时官方刚调过价、还改了模型名），
        而且**请求名与实际计费模型可能不同** —— 实测请求 `deepseek-chat`
        返回的 `model` 是 `deepseek-flash`（按新模型计费）。
        这种随时会变的东西写死在代码里，迟早会给出错的费用估算。
        """
        raw = dict((self.compress_config_raw().get("pricing") or {}))
        # ⚠️ `compress_config()` 会把 pricing 过滤掉（它只回界面要的字段），
        # 所以这里读原始段
        def num(key: str, fallback: float) -> float:
            try:
                return float(raw.get(key, fallback))
            except (TypeError, ValueError):
                return fallback

        return {
            "model": str(raw.get("model") or "deepseek-flash"),
            "currency": str(raw.get("currency") or "USD"),
            "usd_to_cny": num("usd_to_cny", 6.7),
            "input_miss_peak": num("input_miss_peak", 0.30),
            "input_miss_offpeak": num("input_miss_offpeak", 0.15),
            "input_hit_peak": num("input_hit_peak", 0.006),
            "input_hit_offpeak": num("input_hit_offpeak", 0.003),
            "output_peak": num("output_peak", 1.2),
            "output_offpeak": num("output_offpeak", 0.6),
            "peak_hours": raw.get("peak_hours") or [[9, 12], [14, 18]],
            "est_output_tokens_per_group": int(num("est_output_tokens_per_group", 200)),
        }

    def compress_config_raw(self) -> dict[str, Any]:
        """`memory.compress` 整段（含 `pricing` 子段）。"""
        return dict((self.config.memory or {}).get("compress") or {})

    @staticmethod
    def is_peak_now(peak_hours: list[Any], now: Optional[float] = None) -> bool:
        """现在是不是**高峰时段**（决定价格）。

        官方定义（2026-10-05 核对）：
            高峰 = UTC 01:00-04:00 与 06:00-10:00，**周一至周五**，
                   排除中国法定节假日
            其余 = 空闲（价格是高峰的一半）

        ⚠️ 官方按 UTC 写，而配置里给的是**北京时间**（UTC+8）小时区间：
            UTC 01:00-04:00 -> 北京 09:00-12:00
            UTC 06:00-10:00 -> 北京 14:00-18:00
        两边是同一段时间，但**用本地时间判断更不容易错**
        （用户看表就是北京时间）。

        ⚠️ **中国法定节假日判断不了** —— 那需要一张节假日表，
        每年都变。这里只判"工作日 + 时段"，所以节假日会被误判成高峰，
        **估算偏高**。偏差方向是安全的（宁可让用户以为贵一点）。
        """
        t = time.localtime(time.time() if now is None else now)
        if t.tm_wday >= 5:  # 5=周六 6=周日
            return False
        hour = t.tm_hour
        for span in peak_hours:
            try:
                start, end = int(span[0]), int(span[1])
            except (TypeError, ValueError, IndexError):
                continue
            if start <= hour < end:
                return True
        return False

    def estimate_ai_cost(self, groups: list[dict[str, Any]], prompt: str = "") -> dict[str, Any]:
        """估算 AI 压缩这批数据要花多少钱（**给用户看的**）。

        ## 用户要求（2026-10-05）

        「ai 压缩的话，需要进行对应的确认提示弹窗哦，不然 api 余额蒸发
        还不知道干什么了，还要在 ai 压缩里面写个提示，**压缩多少多少文本
        需要多少 token**，将现在的 deepseek 的**峰谷价格和峰值价格**给用户看，
        **再换算成实际价格**给用户，让用户自己对比价格」

        所以返回里要有：条数、字符数、**token 估算**、峰谷两套单价、
        峰谷两套换算后的人民币金额、以及"现在处于哪一档"。

        ## ⚠️ 这是**估算**，不是账单

        真实费用以 API 返回的 `usage` 为准。估算的偏差来源：

          · **输出 token** 是猜的（`est_output_tokens_per_group`，默认 200）——
            实测一组 4 条约 92（含思考）/ 10（不含）token，
            `deepseek-flash` **默认开思考模式**，波动很大；
          · **输入按"缓存未命中"** 算（首次跑基本都是未命中）——
            同一批数据短时间内重跑会命中缓存，实际便宜 **50 倍**；
          · 系统提示词与格式开销按经验加了固定量。

        偏差方向：**偏保守（估算偏高）**，宁可让用户以为贵一点。
        """
        p = self._pricing()
        n_groups = len(groups or [])
        total_chars = sum(
            len(str(m.get("summary") or ""))
            for g in (groups or [])
            for m in (g.get("members") or [])
        )
        # 输入 token：逐组算（每组一次独立请求），用真实 tokenizer 数
        from .embedding_local import count_tokens

        input_tokens = 0
        for g in groups or []:
            body = "\n".join(
                f"- {m.get('summary') or ''}" for m in (g.get("members") or [])
            )
            # `- ` 前缀 + 换行 + 系统提示词 + "请合并。"
            input_tokens += count_tokens(body) + count_tokens(prompt or "（默认提示词）") + 12
        output_tokens = n_groups * int(p["est_output_tokens_per_group"])
        total_tokens = input_tokens + output_tokens

        def cost(peak: bool) -> float:
            """按峰值/空闲算美元。"""
            in_rate = p["input_miss_peak"] if peak else p["input_miss_offpeak"]
            out_rate = p["output_peak"] if peak else p["output_offpeak"]
            return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate

        usd_peak, usd_off = cost(True), cost(False)
        rate = float(p["usd_to_cny"])
        return {
            "ok": True,
            "model": p["model"],
            "groups": n_groups,
            "chars": total_chars,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            # 峰谷单价（美元/百万 token）—— 按用户要求**两个都给他看**
            "rates": {
                "currency": p["currency"],
                "input_peak": p["input_miss_peak"],
                "input_offpeak": p["input_miss_offpeak"],
                "output_peak": p["output_peak"],
                "output_offpeak": p["output_offpeak"],
                "usd_to_cny": rate,
            },
            "usd_peak": round(usd_peak, 6),
            "usd_offpeak": round(usd_off, 6),
            "cny_peak": round(usd_peak * rate, 4),
            "cny_offpeak": round(usd_off * rate, 4),
            "is_peak_now": self.is_peak_now(p["peak_hours"]),
            "est_output_tokens_per_group": p["est_output_tokens_per_group"],
            "note": "估算值；真实费用以 API 返回的 usage 为准（输出含思考 token，波动较大）",
        }

    def _ai_merge_texts(self, texts: list[str], prompt: str) -> tuple[str, str]:
        """让模型把一组重复的表述合成**一条更完整清晰的**。

        返回 `(合并后的文本, 错误说明)`；失败时文本为空串。

        ## 为什么用模型（而不是"取最长的那条"）

        取最长最简单，但它会**丢掉其他说法的信息**。比如：
            用户问还在吗 / 用户问在不在 / 用户说在吗
        取最长还是"用户问在不在"—— 没有变好。
        模型可以写成「用户常用"在吗/在不在/你在干嘛"这类话打招呼」，
        **信息保留更多、还更短**。

        ## 提示词

        `prompt` 为空时用内置默认（用户要求可自定义，所以这里只是**兜底**）。
        """
        builtin = (
            "下面几行是同一个意思的多种说法（来自长期记忆库，可能重复啰嗦）。\n"
            "请合并成**一条**简洁、信息完整的陈述句。要求：\n"
            "1. 只输出合并后的那一句话，不要解释、不要编号、不要引号\n"
            "2. 保留所有有信息量的细节，去掉重复的措辞\n"
            "3. 用第三人称陈述（主语是「用户」或「宠物」），不要写成对话\n"
            "4. 不超过 80 字"
        )
        system = (prompt or "").strip() or builtin
        body = "\n".join(f"- {t}" for t in texts if t.strip())
        if not body:
            return "", "没有内容"
        try:
            from .brains import LLMRequest, Turn, build_brain
            from .brains.base import EmotionState
            import asyncio

            brain = build_brain(self.config, EmotionState())
            if getattr(brain, "name", "") == "mock":
                return "", "当前没有可用的模型（provider=mock）"
            req = LLMRequest(
                system=system,
                history=[Turn(role="user", text=body)],
                user_text="请合并。",
                temperature=0.2,
                max_tokens=200,
                # 🔴 **必须关思考**（2026-10-12）。
                #
                # 与 `memory.py` 里抽取事实是同一个坑：思考模式和正文
                # **共享 `max_tokens`**，200 会被推理吃光，于是合并
                # **永远返回空**（走到下面那句 `return "", "模型返回空"`），
                # 而调用方多半只记 DEBUG 日志。
                #
                # 合并是机械的文本处理，本来也不需要思考 —— 推理只会白烧钱。
                thinking=False,
                timeout_s=30,
            )

            async def run() -> str:
                out: list[str] = []
                async for ev in brain.stream(req):
                    if ev.get("kind") == "delta":
                        out.append(str(ev.get("text") or ""))
                    elif ev.get("kind") == "error":
                        return ""
                return "".join(out)

            text = asyncio.run(run()).strip()
            # 清理：模型有时会带引号/编号/前缀
            text = text.strip().strip('"').strip("「」").strip()
            for prefix in ("合并后：", "合并：", "结果："):
                if text.startswith(prefix):
                    text = text[len(prefix) :].strip()
            if not text:
                return "", "模型返回空"
            return text[:200], ""
        except Exception as exc:  # noqa: BLE001
            log.exception("AI 合并失败")
            return "", f"{type(exc).__name__}: {exc}"

    def compress_undo(self, memory_ids: Optional[list[int]] = None) -> dict[str, Any]:
        """**还原**被压缩的记忆（单个 / 全部）。

        还原后必须 `vec_sync_from_memory()` —— 否则条目活跃了但不在 KNN 索引里，
        **语义检索永远找不回来**（关键词路还能，症状很隐蔽）。
        """
        if not self.db:
            return {"ok": False, "message": "记忆库未启用"}
        n = self.db.unmerge_memory(memory_ids)
        return {"ok": True, "restored": n, "remaining": self.db.count_active()}

    def compress_list(self, limit: int = 200) -> dict[str, Any]:
        """列出已压缩的条目（供界面显示与还原）。"""
        if not self.db:
            return {"ok": False, "message": "记忆库未启用"}
        rows = self.db.list_merged(limit=limit)
        return {
            "ok": True,
            "items": [
                {
                    "id": int(r["id"]),
                    "summary": str(r["summary"]),
                    "kind": str(r["kind"]),
                    "level": str(r["level"]),
                    "merged_into": int(r["merged_into"] or 0),
                    "target": str(r["target_summary"] or ""),
                    "when": self._when(r["ts"]),
                    "merged_at": str(r["merged_at"] or ""),
                }
                for r in rows
            ],
        }

    # ------------------------------------------------------------ 清空

    def clear(self) -> int:
        """彻底清空记忆，返回删掉的条数。

        ## ⚠️ 连"原文底本"一起删 —— 这是刻意的

        `raw_log` 是"永不淘汰"的永久底本，但**用户主动说"忘掉一切"时它必须也删**。
        否则"宠物的记忆里还留着我们全部对话"这句话就是假的 —— 那是隐私上的底线问题，
        不是功能取舍。

        ## ⚠️ 必须把 KNN 索引也清掉（2026-10-07 实测发现漏了）

        实测清空之后：

            memory              0 行   ✅
            memory_vec          0 行   ✅
            memory_vec0_rowids **10 行**  ❌   <- 索引里还留着 rowid
            memory_vec0_vector_chunks00  1 行（一个 2MB 的预分配块）

        也就是说 `clear()` 清了 `memory` 与 `memory_vec`，却**没清 `memory_vec0`**
        —— 而那正是 `remove_memory` 的注释里警告过的"幽灵条目"：
        KNN 索引里留着一个**取不回正文**的 id，检索时它会占着排名。

        为什么之前没暴露：`clear()` 之后用户通常直接重新开始，不查 KNN；
        而库清空的瞬间索引里没有新数据，`vec_search` 也就返回空。
        **但这个不一致是真实存在的**，所以这里显式一起清。

        `DELETE FROM memory_vec0`（虚拟表）而不是 DROP —— 表结构留着，
        首次写入时不必重建（`_create_vec_index` 里"空库不建表"的分支也就不会触发）。

        ⚠️ 这一步在**没加载 vec0 扩展**的连接上会失败（`no such module: vec0`），
        所以单独 try 住：**它失败不该让"清空"整体失败**。
        """
        if not self.db:
            return 0
        total = 0
        knn = 0
        try:
            for table in ("memory_vec", "memory", "cherished", "recent_turn", "raw_log"):
                cur = self.db.conn.execute(f"DELETE FROM {table}")
                total += int(cur.rowcount or 0)
            # KNN 索引单独清（需要 vec0 扩展；失败不致命）。
            #
            # ⚠️ 它删掉的行**不计入返回值** —— 返回值是"删掉的记忆条数"，而索引行
            # 不是记忆（同一份数据在 memory / memory_vec / memory_vec0 里各有一份，
            # 都算进去会把条数报成三倍）。自检里那条"清空条数不对：期望 8，回执 10"
            # 就是这么抓出来的 —— **测试是对的，是我的计数口径错了**。
            try:
                cur = self.db.conn.execute("DELETE FROM memory_vec0")
                knn = int(cur.rowcount or 0)
            except Exception:  # noqa: BLE001
                log.debug("清空 KNN 索引跳过（vec0 未加载？）", exc_info=True)
            # FTS 是 external content 表，删 memory 后靠触发器同步；
            # 但为了保险再 rebuild 一次，避免索引里残留已删文本。
            try:
                self.db.conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
            except Exception:  # noqa: BLE001
                pass
            self.db.conn.commit()
            try:
                self.db.conn.execute("VACUUM")
            except Exception:  # noqa: BLE001 —— VACUUM 失败不影响"已删除"这个事实
                log.debug("VACUUM 跳过", exc_info=True)
        except Exception:  # noqa: BLE001
            log.exception("清空记忆失败")
        if knn:
            log.info("顺带清空 KNN 索引 %d 行（不计入返回的记忆条数）", knn)
        return total

    def backup_now(self) -> BackupResult:
        """立刻备份一次（手动按钮）。

        ⚠️ 与 `backup_on_start` 的区别：**不看 `backup.enabled`**。
        用户点"立即备份"就是明确意图，此时若因为"自动备份没开"而拒绝，
        是拿配置开关去否定一个更直接的指令。所以这里只要求"有数据库 + 有文件夹"。

        文件夹没设时给一句能行动的提示，而不是一句"失败"。
        """
        if not self.db:
            return BackupResult(False, "记忆库未打开")
        section = (self.config.memory or {}).get("backup") or {}
        folder = str(section.get("dir") or "").strip()
        if not folder:
            return BackupResult(False, "还没设置备份文件夹 —— 先在设置页选一个")
        keep = int(section.get("keep") or 0)
        try:
            result = self.db.backup_to(Path(folder), keep=keep, stage_dir=self._stage_dir())
        except Exception as exc:  # noqa: BLE001
            log.exception("手动备份异常")
            return BackupResult(False, f"备份异常：{exc}")
        return result

    def _stage_dir(self) -> Path:
        """staging 目录：库旁边，**必然可写**（否则记忆功能本身就没法工作）。

        用途见 `store.backup_to` 的说明 —— Python 子进程可能对用户选的备份文件夹
        没有写权限（实测就是如此），那时先在 staging 生成一致快照，
        再由 Electron 主进程复制过去。搬运工换了人，快照仍然一致完整。
        """
        return self.db_path.parent / "_staging"

    def stats(self) -> dict[str, Any]:
        if not self.db:
            return {"enabled": self.enabled, "open": False}
        out: dict[str, Any] = {"enabled": self.enabled, "open": True}
        out.update(self.db.count())
        # 压缩相关计数（schema v3）—— 界面要显示"净化后还剩多少 / 压了多少"
        out["active"] = self.db.count_active()
        out["merged"] = self.db.count_merged()
        out["fts_ok"] = self.db._fts_ok
        out["dll"] = str(self.dll_path) if self.dll_path else None
        out["db"] = str(self.db_path)
        out["injections"] = self.injections
        out["facts_written"] = self.facts_written
        out["window_days"] = self.recent_window_days()
        return out

    def close(self) -> None:
        if self.db:
            self.db.close()
            self.db = None


def _merge_consecutive_same_role(turns: list[dict[str, str]]) -> list[dict[str, str]]:
    """
    把**连续的同角色**消息并成一条（内容一条不丢，用换行连起来）。

    ## 为什么会有连续同角色（2026-10-08 查证，是真实数据不是 bug）

    实测 `raw_log` 里有这些情况：

      · **连续 assistant** —— 宠物**自主发言**（没有用户输入，它自己开口）。
        实测有 `#19/#20/#21` 三连。
      · **连续 user** —— 用户**连发两条**（前一条被打断、或没等到回复又发）。
        实测有 `你带我说话吗?` 紧跟 `啦啦啦啦。`

    这些在真实交互里完全正常，但直接放进 `messages` 有两个问题：

      ① 有些服务端对"连续同角色"有额外校验或行为不确定
      ② 模型的注意力会分散——它看到两条并列的 user，可能只回其中一条

    合并之后：`user1\\nuser2` 是一句话，模型会一起回应，符合用户本意
    （他本来就是连着说的两件事）。
    """
    out: list[dict[str, str]] = []
    for item in turns:
        # 🔴 带日期前缀的消息**不并进上一条**（2026-10-28）。
        #
        # ## 为什么
        #
        # 加了 `〔今天〕` 这类日期前缀之后，如果这条恰好和上一条同角色，
        # 它会被并进去 —— 前缀就落到了字符串**中间**：
        #
        #     "...所以真给我眼睛那天，我估计还得你带着我认。 〔今天〕诶，都快十一点了。"
        #                                                     ^^^^^^^ 埋在中间
        #
        # 模型对"开头的时间锚点"最敏感，埋在中间等于白标。
        # 而且那两条本来就是**不同时间**说的，并成一条反而抹掉了时间边界。
        #
        # ⚠️ 代价是可能出现"连续同角色两条"（本来就是不同日期的两段话）。
        #    这是**有意的取舍**：时间边界的清晰度 > 角色交替的整齐。
        #    服务端对连续同角色只是"行为不确定"，不是报错；而时间错乱是
        #    用户已经明确报过的问题。
        stamped = item.get("text", "").startswith("〔")
        if out and out[-1]["role"] == item["role"] and not stamped:
            out[-1]["text"] = f"{out[-1]['text']}\n{item['text']}"
        else:
            out.append(dict(item))
    return out


def _row_dict(row: Any) -> dict[str, Any]:
    """`sqlite3.Row` -> 普通 dict（JSON 可序列化，发给界面用）。

    `sqlite3.Row` 不能用 `json.dumps` 直接序列化（它不是 dict、也不支持
    `default=` 之外的方式），所以每次发给界面前都要转一下。
    """
    return {k: row[k] for k in row.keys()}


def _cosine_batch_raw(query: list[float], blobs: list[bytes], dim: int) -> list[float]:
    """`_cosine_batch` 的**零拷贝**版本：直接吃原始 BLOB。

    ## 为什么还要一个"raw"版

    `vector` 列存的就是 `float32` 紧凑字节，所以可以：

        np.frombuffer(blob)   # 直接读同一块内存，不复制

    而"先 `unpack_vector` 成 Python list、再 `np.asarray` 转回来"要多走
    两趟（struct.unpack 建 512 个 Python float 对象，再转 numpy）。
    **实测 2000 条时那一趟占 61.5ms**，比余弦本身还贵。

    这里把整批 BLOB **拼成一个大 buffer**，一次 `frombuffer` 出
    `(n, dim)` 矩阵 —— 只复制一次（拼接），之后全是 numpy 内部计算。

    ## 数值等价

    与 `_cosine_batch` 用同样的 float32 / 同样的公式，差异仅来自
    矩阵乘的实现 —— 已在 `check-cosine-equivalence.py`（历史实测脚本）里验证
    过阈值判定 100% 一致。
    """
    if not query or not blobs or dim <= 0:
        return []
    try:
        import numpy as np

        qv = np.asarray(query, dtype=np.float32)
        if len(qv) != dim:
            # 查询向量维度与库不一致 —— 交给调用方（它会统计 mismatched）
            return [0.0] * len(blobs)

        # 只拼"长度正确"的那些行；其余位置留 0（调用方会看见 0 并跳过）
        good = [i for i, b in enumerate(blobs) if len(b) == dim * 4]
        out = [0.0] * len(blobs)
        if not good:
            return out
        packed = b"".join(blobs[i] for i in good)
        mat = np.frombuffer(packed, dtype=np.float32).reshape(len(good), dim)
        dots = mat @ qv
        norms = np.linalg.norm(mat, axis=1) * float(np.linalg.norm(qv))
        for slot, i in enumerate(good):
            denom = float(norms[slot])
            out[i] = float(dots[slot] / denom) if denom > 0 else 0.0
        return out
    except Exception:  # noqa: BLE001
        log.debug("零拷贝批量余弦失败，退回逐条解包", exc_info=True)
        return _cosine_batch(
            query,
            [unpack_vector(b) for b in blobs],
        )


def _cosine_batch(query: list[float], stored: list[list[float]]) -> list[float]:
    """一次算 query 与**一批**向量的余弦相似度。

    ## 为什么要有这个（实测的性能瓶颈）

    `tools/bench-vector.py` 量出来的（512 维）：

        条数    逐条 `_cosine`     批量
         100        7.0 ms        ~1 ms
         500       34.6 ms        ~2 ms
        2000      137.7 ms        ~3 ms

    逐条写法有两层浪费：
      ① **纯 Python 逐元素循环**（512 次乘加/条）；
      ② 每条都要 `list -> numpy` 转一次 dtype。

    批量做法把整个矩阵一次性交给 numpy（一次转、一次算），
    省掉的不只是循环，还有重复转换。

    ## ⚠️ 数值等价**已验证**（改之前必须做，不能省）

    `check-cosine-equivalence.py`，历史实测脚本：423 组样本，最大绝对差 **8.4e-08**，
    且 0.25 / 0.40 / 0.92 三个阈值的**过阈值判定 100% 一致**。

    浮点差异会让 `sim > floor` 在边界翻转，表现为"偶尔少召回一条" ——
    属于最难查的偶发 bug，所以必须先用数据证明等价再换。

    ## 退化情形

    空输入、维度不符、零向量 —— 全部按 0.0 处理，与 `_cosine` 一致。
    维度不符的条目**调用方应先自己筛掉**（见 `_retrieve` 里的 `mismatched`）。
    """
    if not query or not stored:
        return []
    try:
        import numpy as np

        dim = len(query)
        usable = [i for i, v in enumerate(stored) if len(v) == dim]
        out = [0.0] * len(stored)
        if not usable:
            return out
        mat = np.asarray([stored[i] for i in usable], dtype=np.float32)
        qv = np.asarray(query, dtype=np.float32)
        dots = mat @ qv
        norms = np.linalg.norm(mat, axis=1) * float(np.linalg.norm(qv))
        for slot, i in enumerate(usable):
            denom = float(norms[slot])
            out[i] = float(dots[slot] / denom) if denom > 0 else 0.0
        return out
    except Exception:  # noqa: BLE001 —— numpy 不可用时退回逐条（行为一致）
        log.debug("批量余弦失败，退回逐条计算", exc_info=True)
        return [_cosine(query, v) for v in stored]


def _cosine(a: list[float], b: list[float]) -> float:
    """两个向量的余弦相似度（**逐条**版本）。

    ⚠️ 检索路径现在走 `_cosine_batch`（快一个数量级）。这个函数保留用于：
      · 单条比较（语义查重、`BgeEmbedder.similarity`）；
      · `_cosine_batch` 的**降级路径**；
      · 作为**等价性基准**（`check-cosine-equivalence.py`（历史实测脚本）拿它对照）。
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / ((na**0.5) * (nb**0.5))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + time.strftime("%z", time.localtime())
