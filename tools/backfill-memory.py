"""
从 `raw_log`（历史原文）**批量回填提炼**，把检索层从"摆设"变成有料。

## 为什么需要它

检索层（`memory` 表）是唯一被检索、唯一被压缩的层，但它是**对话时实时提炼**的
产物 —— 而历史 596 条原文里绝大部分是接上 DeepSeek **之前**产生的
（那时 `provider: mock`，提炼不出任何东西）。所以实测下来检索层只有 9 条、
其中还有 `（空行）` 这种垃圾，**等于摆设**。

## 花钱前先在本地过滤（省 79% 的钱）

实测 `raw_log`：

    596 条 / 111 个会话
      <8 字：267 条（"嗨" "嗯" "1" —— 提炼不出任何事实）
      逐字去重后可省 471 条（79%）

所以流程是：
  ① 按**会话**分组（提炼需要上下文，单条"还在吗"看不出任何东西）
  ② 会话内**去掉逐字重复**（早期复读把库填成了这样）
  ③ 丢掉"全是短句噪音"的会话（没有一条 >= 阈值的）
  ④ 相邻且时间接近的会话**合并**成块（减少调用次数）
  ⑤ 才送去提炼

## 两种模式

    python tools/backfill-memory.py            # 干跑：只估算，不花钱
    python tools/backfill-memory.py --run      # 真跑：调用 API 并写入
    python tools/backfill-memory.py --run --limit 5   # 只跑前 5 块（先看质量）

⚠️ **写入走 `remember_facts`**（产品代码），所以自带精确 + 语义查重 ——
   同一件事被多个块抽到也不会在检索层堆一串重复。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

from pet.brains import build_brain  # noqa: E402
from pet.brains.base import EmotionState  # noqa: E402
from pet.config import load_config  # noqa: E402
from pet.memory import FactExtractor  # noqa: E402
from pet.memory2 import MemoryService  # noqa: E402


def score_importance(text: str) -> float:
    """用 `tools/score-memory.py` 的规则给一条事实打分。

    ## 为什么复用那个工具而不是在这里再写一份

    那份规则是**要拿来评估和调参**的（用户明确要求"后续需要评估的时候
    能用来优化结构"）。如果回填这里另写一套，那评估出来的分布和实际入库的
    分数就是两回事 —— "两份实现迟早漂移"是这个项目反复踩的坑。

    工具文件名带连字符，不能直接 `import`，所以用 importlib 按路径加载。

    ⚠️ 加载失败时退回 0.5（= 旧行为），**不能让回填因为打分器出错而中断**。
    """
    global _scorer
    if _scorer is None:
        try:
            import importlib.util

            path = ROOT / "tools" / "score-memory.py"
            spec = importlib.util.spec_from_file_location("score_memory", path)
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            # ⚠️ **必须先注册进 `sys.modules` 再 exec**（2026-10-20 踩到）。
            #
            # 打分器里用了 `@dataclass`，而 dataclass 在编织类的时候会做
            # `sys.modules.get(cls.__module__).__dict__` —— 模块还没注册
            # 就会拿到 None，报 `AttributeError: 'NoneType' object has no
            # attribute '__dict__'`，而且报错点在 dataclasses 内部，看不出根因。
            #
            # 这是 `module_from_spec` + dataclass 的固定组合坑。
            sys.modules.setdefault("score_memory", module)
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            _scorer = module
        except Exception as exc:  # noqa: BLE001
            print(f"    ⚠️ 打分器加载失败（退回 0.5）：{type(exc).__name__}: {exc}")
            _scorer = False
    if _scorer is False:
        return 0.5
    return float(_scorer.score_text(text).score)


_scorer: Any = None

# 与之前实测一致的中文口径
TOKENS_PER_CHAR = 1.25
# DeepSeek 价格（USD / 1M token，空闲时段；官方 2026-10-05 核实）
PRICE_INPUT_MISS = 0.15
PRICE_OUTPUT = 0.6
USD_TO_CNY = 6.7

# 提炼输入上限：`FactExtractor.extract` 自己会在 4000 字处截断，
# 这里主动切得更小，避免"截断"把后半段丢掉（那等于白付钱）
MAX_CHARS_PER_CALL = 1800


def db_path() -> Path:
    return ROOT / "apps" / "desktop" / "pet-data" / "memory" / "pet.db"


def load_turns() -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return list(
            con.execute(
                "SELECT id, session_id, role, text, ts FROM raw_log ORDER BY ts, id"
            ).fetchall()
        )
    finally:
        con.close()


def build_chunks(turns: list[sqlite3.Row], min_chars: int, merge_gap_s: float) -> list[dict[str, Any]]:
    """
    把原文按会话切块。

    ⚠️ **提炼需要上下文** —— 单条"还在吗"看不出任何东西，所以以会话为单位。
    """
    # ① 按会话分组
    by_sess: dict[str, list[sqlite3.Row]] = {}
    for t in turns:
        by_sess.setdefault(str(t["session_id"]), []).append(t)

    sessions: list[dict[str, Any]] = []
    for sid, rows in by_sess.items():
        # ② 会话内去掉**逐字重复**（保留首次出现，按时间顺序）
        seen: set[str] = set()
        kept: list[sqlite3.Row] = []
        for r in rows:
            text = str(r["text"] or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            kept.append(r)
        if not kept:
            continue
        body = "\n".join(
            f"{'用户' if r['role'] == 'user' else '宠物'}：{str(r['text']).strip()}"
            for r in kept
        )
        chars = sum(len(str(r["text"]).strip()) for r in kept)
        sessions.append(
            {
                "sid": sid,
                "rows": kept,
                "body": body,
                "chars": chars,
                "ts0": float(kept[0]["ts"] or 0),
                "ts1": float(kept[-1]["ts"] or 0),
                # ③ 噪音判据：**没有任何一条**达到 min_chars 就丢掉整个会话
                #    （全是"嗨""嗯""1"的会话提炼不出事实）
                "has_substance": any(len(str(r["text"]).strip()) >= min_chars for r in kept),
            }
        )

    # ④ 相邻且时间接近的会话合并（节省调用次数）
    sessions.sort(key=lambda s: s["ts0"])
    merged: list[dict[str, Any]] = []
    for s in sessions:
        if not s["has_substance"]:
            continue
        if merged and (s["ts0"] - merged[-1]["ts1"]) <= merge_gap_s:
            prev = merged[-1]
            prev["body"] = prev["body"] + "\n" + s["body"]
            prev["chars"] += s["chars"]
            prev["ts1"] = max(prev["ts1"], s["ts1"])
            prev["sids"].append(s["sid"])
            prev["dropped"] += 0
        else:
            merged.append(
                {
                    "sid": s["sid"],
                    "sids": [s["sid"]],
                    "body": s["body"],
                    "chars": s["chars"],
                    "ts0": s["ts0"],
                    "ts1": s["ts1"],
                    "dropped": 0,
                }
            )

    # ⑤ 按 MAX_CHARS_PER_CALL 再切（避免被 FactExtractor 内部截断）
    out: list[dict[str, Any]] = []
    for m in merged:
        text = m["body"]
        if len(text) <= MAX_CHARS_PER_CALL:
            out.append(m)
            continue
        # 按行贪心切分
        buf: list[str] = []
        size = 0
        part = 0
        for line in text.split("\n"):
            if size + len(line) + 1 > MAX_CHARS_PER_CALL and buf:
                out.append({**m, "body": "\n".join(buf), "chars": size, "part": part})
                part += 1
                buf, size = [], 0
            buf.append(line)
            size += len(line) + 1
        if buf:
            out.append({**m, "body": "\n".join(buf), "chars": size, "part": part})
    for i, c in enumerate(out):
        c["idx"] = i
    return out


def fmt_time(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "?"


async def run_extract(chunks: list[dict[str, Any]], limit: int | None, dry: bool) -> int:
    cfg = load_config(ROOT / "config" / "persona.yaml")
    mem = MemoryService(cfg)
    brain = build_brain(cfg, EmotionState())

    # ⚠️ `FactExtractor` 要的是 **`MemoryConfig`**，不是应用级 `Config`。
    #    我第一版传了 `mem.config`（那是应用级 Config），于是它在
    #    `self.config.enabled` 上抛 `AttributeError` —— 真实提炼路径里
    #    这个对象是由 `MemoryService` 内部构造的，工具这边得自己造一份。
    from pet.memory import MemoryConfig  # noqa: PLC0415

    mem_cfg = MemoryConfig.from_config((cfg.memory or {}))
    extractor = FactExtractor(brain, mem_cfg, cfg)

    before = mem.stats()
    print(f"  提炼前：检索层 {before.get('active', '?')} 条活跃")

    todo = chunks if limit is None else chunks[:limit]
    total_facts = 0
    total_calls = 0
    total_fail = 0

    for c in todo:
        if dry:
            continue
        facts = await extractor.extract_from_transcript(c["body"], max_facts=4)
        total_calls += 1
        if not facts and extractor.failures:
            total_fail += 1
        if facts:
            """
            ## ⚠️ 逐条写入，每条**自己打分**（2026-10-20 改）

            原来是整批传 `importance=0.5` —— 但那是个假分数：
            `recent_memories`（泛问句兜底）**靠 importance 排序**，
            全是 0.5 就等于按时间排，兜底必然端上"最近一次闲聊"（实测过）。

            所以逐条调 `tools/score-memory.py` 的规则打分器
            （纯规则、不花钱、可解释、可复现）。

            ⚠️ 逐条写 = 多几次 SQL 往返，但回填本来就是离线批量跑，
            这点开销远小于"排序没区分力"的代价。
            """
            written = 0
            for fact in facts:
                written += mem.remember_facts(
                    [fact],
                    kind="fact",
                    level="recent",
                    importance=score_importance(fact),
                    src_table="raw_log",
                    # ⚠️ **必须传时间**：这些事实来自几天前的对话，不传就全变成"今天"，
                    #    而提示词的每一段都要带时间 -> "用户在做桌面宠物项目（今天）"
                    #    就在说谎，AI 会把旧事当成刚发生的事说。
                    ts=c["ts0"] or None,
                )
            total_facts += written
            print(
                f"    [{c['idx']:>2}] {fmt_time(c['ts0'])} {c['chars']:>5} 字 -> "
                f"抽到 {len(facts)} 条，写入 {written} 条"
            )
            for f in facts:
                print(f"          · {f}")
        else:
            print(f"    [{c['idx']:>2}] {fmt_time(c['ts0'])} {c['chars']:>5} 字 -> 无")

    if not dry:
        after = mem.stats()
        print()
        print(f"  提炼后：检索层 {after.get('active', '?')} 条活跃（新增 {after.get('active', 0) - before.get('active', 0)}）")
        print(f"  调用 {total_calls} 次（失败 {total_fail}）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="从 raw_log 回填提炼，填充检索层")
    ap.add_argument("--run", action="store_true", help="真跑（会花 API 的钱）")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 块")
    ap.add_argument("--min-chars", type=int, default=12, help="会话里至少要有这么长的一条才算有料")
    ap.add_argument("--merge-gap", type=float, default=1800.0, help="相邻会话间隔小于这个秒数就合并")
    args = ap.parse_args()

    turns = load_turns()
    chunks = build_chunks(turns, args.min_chars, args.merge_gap)

    print("=" * 78)
    print("回填提炼：原文 -> 检索层")
    print("=" * 78)
    print(f"  原文      : {len(turns)} 条")
    print(f"  切出块数  : {len(chunks)} 块（已按会话分组、会话内去重、噪音会话丢弃、相邻合并）")
    if not chunks:
        print("  没有可提炼的内容")
        return 0

    chars = sum(c["chars"] for c in chunks)
    # 输入 + 提示词开销（每块 system 提示约 200 字，近似）
    prompt_chars = chars + len(chunks) * 200
    in_tok = int(prompt_chars * TOKENS_PER_CHAR)
    out_tok = len(chunks) * 120  # 每块最多 4 条 * 30 字
    cost = in_tok / 1e6 * PRICE_INPUT_MISS + out_tok / 1e6 * PRICE_OUTPUT
    print(f"  送入字数  : {chars} 字（+ 每块提示词开销）")
    print(f"  估算 token: 输入 ≈ {in_tok}，输出 ≈ {out_tok}")
    print()
    print(f"  **估算费用: ${cost:.5f} ≈ ¥{cost * USD_TO_CNY:.4f}**（空闲时段；高峰翻倍）")
    print()
    print("  前 8 块概览：")
    for c in chunks[:8]:
        head = c["body"].split("\n")[0][:44]
        part = f" (第{c['part'] + 1}段)" if "part" in c else ""
        print(f"    [{c['idx']:>2}] {fmt_time(c['ts0'])} {c['chars']:>5} 字{part}  {head}")

    if not args.run:
        print()
        print("  这是**干跑**（没花钱）。确认上面的估算后加 `--run` 真跑。")
        print(f"  建议先小批验证质量： python tools/backfill-memory.py --run --limit 5")
        return 0

    print()
    print("=" * 78)
    print(f"开始真跑（{'前 ' + str(args.limit) + ' 块' if args.limit else '全部 ' + str(len(chunks)) + ' 块'}）")
    print("=" * 78)
    return asyncio.run(run_extract(chunks, args.limit, dry=False))


if __name__ == "__main__":
    raise SystemExit(main())
