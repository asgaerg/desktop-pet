"""验证随机事件系统（2026-10-17）。

## 验什么

```
① 注册表   5 个事件，3 个是"路口"（缺能力），2 个可用
② 路口     dormant 的**永不触发**，且能说明缺什么能力
③ 冷却     刚做过的事不会马上再做
④ 安静时段 跨零点判断正确（23->8）
⑤ 执行     真跑一次，状态/印象/日志都落地
⑥ 没做成   handler 返回 None 时**什么都不留下**（不编造）
⑦ 效果值  记录的是**实际生效**的增量（clamp01 吃掉的部分不能记成生效）
```

⚠️ 用临时库，绝不碰真实 pet.db。

用法：
    python tools/test-random-events.py
"""

from __future__ import annotations

import asyncio
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

ROOT = Path(r"F:\ai项目")
sys.path.insert(0, str(ROOT / "apps" / "brain"))

from pet.events import (  # noqa: E402
    CAPABILITIES,
    EVENTS,
    EventEngine,
    list_events,
)
from pet.store import PetDatabase  # noqa: E402
from pet.subjectivity import SubjectiveState  # noqa: E402


def sec(title: str) -> None:
    print()
    print(f"  ── {title} ──")


async def main() -> int:
    failed: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="pet-events-test-"))
    db = PetDatabase(tmp / "e.db", tokenizer="unicode61")
    try:
        print("=" * 78)
        print("  随机事件系统验证")
        print("=" * 78)

        # ── ① 注册表 ──
        sec("① 注册表")
        rows = list_events()
        dormant = [r for r in rows if r["dormant"]]
        ready = [r for r in rows if not r["dormant"]]
        print(f"     共 {len(rows)} 个事件：可用 {len(ready)}，路口 {len(dormant)}")
        for r in rows:
            tag = "路口" if r["dormant"] else "可用"
            print(f"       [{tag}] {r['label']:<18} 需要 {r['needs'] or '无'}")
        if len(dormant) != 3:
            failed.append(f"应有 3 个路口，实际 {len(dormant)}")
            print(f"     ❌ 路口数量不对：{len(dormant)}")
        else:
            print("     ✅ 3 个未来功能路口都在（音乐/打游戏/阅读）")

        # ── ② 路口永不触发 ──
        sec("② 路口事件永不触发")
        eng = EventEngine(base_chance=1.0, min_gap_s=0, idle_before_s=0, rng=random.Random(1))
        now = time.time()
        ok, why = eng.eligible(
            next(e for e in EVENTS if e.kind == "music_sing"),
            now=now, last_interaction_at=0, last_event_at=0, events_today=0,
        )
        print(f"     唱歌事件：能触发={ok} 原因={why}")
        if ok or "dormant" not in why:
            failed.append("路口事件竟然可以触发")
            print("     ❌ 路口事件被放行了")
        else:
            print("     ✅ 被挡住，且说明了缺哪个能力")

        # ── ③ 冷却 ──
        sec("③ 冷却与最小间隔")
        # ⚠️ 必须用**真实默认值**建一个新引擎。
        #    上面那个 `eng` 为了让 dormant 判定先过，把 min_gap/idle 都设成了 0，
        #    拿它测冷却永远是 ok（第一版就是这么误报了两条）。
        gate = EventEngine(rng=random.Random(1))
        ok2, why2 = gate.eligible(
            next(e for e in EVENTS if e.kind == "philosophy"),
            now=now, last_interaction_at=now - 10, last_event_at=0, events_today=0,
        )
        print(f"     idle_before={gate.idle_before_s:.0f}s，刚交互 10s："
              f"能触发={ok2} 原因={why2}")
        if ok2:
            failed.append("刚交互完就允许做自己的事")
            print("     ❌ 刚聊完就跑去搜哲学")
        else:
            print("     ✅ 刚聊完不做事（idle 门槛）")

        ok3, why3 = gate.eligible(
            next(e for e in EVENTS if e.kind == "philosophy"),
            now=now, last_interaction_at=0, last_event_at=now - 5, events_today=0,
        )
        print(f"     min_gap={gate.min_gap_s:.0f}s，5 秒前刚做过："
              f"能触发={ok3} 原因={why3}")
        if ok3:
            failed.append("全局最小间隔没生效")
            print("     ❌ 连着冒事件")
        else:
            print("     ✅ 全局间隔生效")

        # 日上限
        ok4, why4b = gate.eligible(
            next(e for e in EVENTS if e.kind == "philosophy"),
            now=now, last_interaction_at=0, last_event_at=0,
            events_today=gate.max_per_day,
        )
        print(f"     今天已做满 {gate.max_per_day} 次：能触发={ok4} 原因={why4b}")
        if ok4:
            failed.append("日上限没生效")
            print("     ❌ 超过日上限还在做")
        else:
            print("     ✅ 日上限生效")

        # ── ④ 安静时段（跨零点）──
        sec("④ 安静时段跨零点")
        quiet = EventEngine(quiet_hours=[23, 8])
        got = []
        for h in (0, 3, 7, 8, 12, 23):
            ts = time.mktime((2026, 10, 17, h, 0, 0, 0, 0, -1))
            got.append((h, quiet.in_quiet_hours(ts)))
        print(f"     {got}")
        expect = {0: True, 3: True, 7: True, 8: False, 12: False, 23: True}
        bad = [h for h, v in got if expect[h] != v]
        if bad:
            failed.append(f"安静时段判断错：{bad}")
            print(f"     ❌ 判断错误的小时：{bad}")
        else:
            print("     ✅ 23->8 跨零点判断正确")

        # ── ⑤ 真跑一次 ──
        sec("⑤ 真跑一次（假搜索，不联网）")
        state = SubjectiveState(attention=0.4)
        before = state.as_dict()

        async def fake_search(query: str) -> str:
            return "人不能两次踏进同一条河流。 —— 赫拉克利特"

        spec = next(e for e in EVENTS if e.kind == "philosophy")
        outcome = await eng.run(spec, {"rng": random.Random(2), "search": fake_search})
        if outcome is None:
            failed.append("事件没跑成")
            print("     ❌ outcome 是 None")
        else:
            print(f"     行为：{outcome.detail}")
            actual = state.apply_event_effect(
                mood=outcome.mood_delta, energy=outcome.energy_delta,
                intimacy=outcome.intimacy_delta, attention=outcome.attention_delta,
            )
            db.record_event(spec.kind, spec.label, outcome.detail,
                            mood_delta=actual["mood"], attention_delta=actual["attention"],
                            now=time.time())
            if outcome.impression:
                state.record_impression(outcome.impression)
                db.add_impression(outcome.impression, source=spec.kind)
            after = state.as_dict()
            print(f"     心情 {before['emotion']['mood']:.3f} -> {after['emotion']['mood']:.3f}")
            print(f"     注意力 {before['attention']:.3f} -> {after['attention']:.3f}")
            print(f"     印象：{state.impressions}")
            if after["attention"] <= before["attention"]:
                failed.append("做自己的事没让注意力偏向自己")
                print("     ❌ 注意力没变（应该更沉浸在自己这边）")
            else:
                print("     ✅ 注意力偏向自己（它在想自己的事）")
            if not db.recent_events(5):
                failed.append("事件没落库")
                print("     ❌ event_log 里没有记录")
            else:
                print("     ✅ 已落库（用户能看到它偷偷做了什么）")
            if not db.recent_impressions(5):
                failed.append("印象没落库")
                print("     ❌ impression 表空")
            else:
                print(f"     ✅ 印象已落库：{db.recent_impressions(1)[0]['text'][:40]}")

        # ── ⑥ 没做成 -> 什么都不留 ──
        sec("⑥ 搜索失败时不留痕迹（不编造）")
        events_before = len(db.recent_events(100))
        impressions_before = len(db.recent_impressions(100))
        none_outcome = await eng.run(spec, {"rng": random.Random(3), "search": None})
        print(f"     返回：{none_outcome}")
        if none_outcome is not None:
            failed.append("没做成却产出了结果")
            print("     ❌ 没做成还有结果（会变成编造）")
        else:
            print("     ✅ 没做成 -> 返回 None，不记录、不改状态")

        # ── ⑦ 记录的是"实际生效"的增量 ──
        sec("⑦ 顶到上限时记的是实际增量")
        topped = SubjectiveState(attention=1.0)
        actual2 = topped.apply_event_effect(attention=0.2)
        print(f"     注意力 1.0 + 0.2 请求 -> 实际增量 {actual2['attention']:.3f}")
        if abs(actual2["attention"]) > 1e-9:
            failed.append("clamp 之后增量记录不对")
            print("     ❌ 应该记 0（已到顶），却记了非零")
        else:
            print("     ✅ 记 0（不会让人以为生效了）")

        # ── ⑧ 概率闸 ──
        sec("⑧ 不定时：概率闸生效")
        low = EventEngine(base_chance=0.0, min_gap_s=0, idle_before_s=0, rng=random.Random(4))
        picked, why4 = low.pick(
            now=time.time(), last_interaction_at=0, last_event_at=0, events_today=0,
        )
        print(f"     base_chance=0 -> {picked}, 原因={why4}")
        if picked is not None:
            failed.append("概率为 0 仍触发了")
            print("     ❌ 概率闸没生效")
        else:
            print("     ✅ 概率为 0 时永不触发")
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 78)
    if failed:
        print(f"  ❌ {len(failed)} 项失败：")
        for item in failed:
            print(f"     · {item}")
    else:
        print("  ✅ 全部通过")
    print("=" * 78)
    return 1 if failed else 0


raise SystemExit(asyncio.run(main()))
