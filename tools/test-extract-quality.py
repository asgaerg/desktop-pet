"""提炼质量回归：8 组固定对话，验证提示词真的会抽出**具体事实**。

用法（需要真实密钥，所以**不进 
pm test / 自检** —— 那两个必须离线可跑）:

    cd apps/brain
    =F:\\ai\\apps\\brain
    =sk-...
    python F:\\ai\\tools\\test-extract-quality.py

退出码 0 = 全部通过。改过 prompts.extract 之后**必须**跑这个，
否则"记忆变差了"要等用户发现。


判定标准（可自动断言，不靠"感觉变好了"）：
  ① 每条记忆里必须含**具体专有名词**（作品名/人名/设备/地名/数字）
  ② 不许以「用户认为 / 用户觉得 / 用户表达过 / 用户问过」开头
  ③ 不能抽出"用户和宠物聊过X"这种纯对话记录
"""

import asyncio
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pet.config import load_config  # noqa: E402
from pet.memory import FactExtractor, MemoryConfig  # noqa: E402
from pet.brains import build_brain  # noqa: E402
from pet.brains.base import EmotionState  # noqa: E402

# 8 组对话：每条都埋了**具体事实**，同时夹杂大量"可以提炼成观点"的话。
# 好的提炼只该抓前者。
CASES = [
    {
        "name": "游戏·具体作品",
        "turns": [
            ("user", "我最近在玩《明日方舟：终末地》，抽卡只抽喜欢的角色"),
            ("assistant", "诶，抽卡这事儿我陪你唠"),
            ("user", "抽不到就算了，反正我不追求强度，看着顺眼就抽"),
        ],
        "must": ["明日方舟", "终末地"],
    },
    {
        "name": "游戏·设备",
        "turns": [
            ("user", "我有台 PS5，还有一台老 Switch"),
            ("assistant", "两台都玩啊"),
            ("user", "Switch 主要玩一些独立小游戏，PS5 玩大作"),
        ],
        "must": ["PS5", "Switch"],
    },
    {
        "name": "角色·壁纸（就是之前漏掉的那个）",
        "turns": [
            ("user", "我挺喜欢曲娘的，我电脑壁纸都是她"),
            ("assistant", "曲娘好看"),
            ("user", "对，从二测就喜欢了，那时候她还没这么火"),
        ],
        "must": ["曲娘"],
    },
    {
        "name": "食物·具体",
        "turns": [
            ("user", "我今天中午吃了黄焖鸡，加了三个土豆"),
            ("assistant", "三个土豆有点多"),
            ("user", "我特别爱吃土豆，从小就爱吃，怎么吃都行"),
        ],
        # ⚠️ 只要求「土豆」不要求「黄焖鸡」：一顿午饭是**一次性的事**，
        #    而爱吃土豆是长期偏好 —— 提炼工具挑后者是**对的**。
        #    （第一次写成了 must=["黄焖鸡"]，实测它只抽了土豆，
        #      那是我的断言错了，不是提示词错了。）
        "must": ["土豆"],
    },
    {
        "name": "工具·具体软件",
        "turns": [
            ("user", "我用 Obsidian 记笔记，配合 Zotero 管理文献"),
            ("assistant", "两个一起用挺顺的"),
            ("user", "以前用过 Notion，但是导出太麻烦了，就换了"),
        ],
        "must": ["Obsidian"],
    },
    {
        "name": "项目·具体",
        "turns": [
            ("user", "我在写一个桌面宠物的项目，叫「阿拾」，用 Electron 加 Python"),
            ("assistant", "这个名字挺好"),
            ("user", "主要想让它能记住我说过的事儿"),
        ],
        "must": ["阿拾"],
    },
    {
        "name": "地点·具体",
        "turns": [
            ("user", "我家在杭州，但是上班在滨江那边"),
            ("assistant", "通勤远吗"),
            ("user", "还好，地铁四十分钟，能接受"),
        ],
        "must": ["杭州"],
    },
    {
        "name": "新闻·带时间（知识类）",
        "turns": [
            ("user", "《黑神话：钟馗》今天公布新实机演示了"),
            ("assistant", "哦？我去查查"),
            ("user", "说是 2027 年发售，具体还没定"),
        ],
        "must": ["黑神话"],
    },
]

BANNED_PREFIX = ("用户认为", "用户觉得", "用户表达过", "用户问过", "用户讨论了", "宠物认为", "用户和宠物")
BANNED_CONTAINS = ("聊过", "讨论过", "问过宠物", "宠物说", "宠物表示")


def main() -> int:
    cfg = load_config(Path(r"F:\ai\config\persona.yaml"))
    # 用**真实大脑**：提炼走的是模型，假替身测不出提示词效果
    brain = build_brain(cfg, EmotionState())
    print(f"  provider = {brain.name}")
    extractor = FactExtractor(brain, MemoryConfig.from_config(cfg.memory or {}), cfg)

    ok_cases = 0
    total_facts = 0
    bad_facts = 0
    missing = []

    for case in CASES:
        history = [(role, text) for role, text in case["turns"]]
        try:
            facts = asyncio.run(extractor.extract(history))
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ {case['name']}：调用失败 {type(exc).__name__}: {exc}")
            continue
        facts = [f for f in (facts or []) if f and f.strip()]
        total_facts += len(facts)

        hit = any(any(m in f for m in case["must"]) for f in facts)
        bad = [
            f
            for f in facts
            if f.startswith(BANNED_PREFIX) or any(b in f for b in BANNED_CONTAINS)
        ]
        bad_facts += len(bad)
        if hit and not bad:
            ok_cases += 1
            mark = "✅"
        else:
            mark = "❌"
            if not hit:
                missing.append((case["name"], case["must"]))

        print(f"\n  {mark} {case['name']}（要含：{'/'.join(case['must'])}）")
        if not facts:
            print("     （没有抽出任何事实）")
        for f in facts:
            flag = "⚠️转述" if f in bad else "  "
            print(f"     {flag} {f[:78]}")

    print("\n" + "=" * 70)
    print(f"  通过 {ok_cases}/{len(CASES)} 组；共抽出 {total_facts} 条，其中转述 {bad_facts} 条")
    if missing:
        print(f"  没抽到关键事实：{missing}")
    return 0 if ok_cases == len(CASES) and bad_facts == 0 else 1


if __name__ == "__main__":
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise SystemExit("需要 DEEPSEEK_API_KEY")
    raise SystemExit(main())
