r"""真机验证：新提炼提示词能不能产出「具体」的记忆（2026-10-17）。

## 为什么必须真机跑

提示词是人写的，**模型怎么理解是另一回事**。用户抱怨的那几个垃圾条目
（`用户在玩游戏` / `用户关注二游`）就是这么来的。

所以必须喂几段真实形态的对话，看**模型实际吐出来什么**：

```
断言 1  说了具体游戏名 -> 抽出来的条目必须含那个名字
断言 2  只说「我在玩游戏」（不说玩什么）-> 必须**不记**这一条
断言 3  用户教了一个知识 -> 必须记成「共享知识」形态（带时间基准）
断言 4  一起查到的答案 -> 必须记下来（旧规则下会全丢）
断言 5  不许出现「可能」当挡箭牌（用户明确说过的事）
断言 6  不许出现对用户的评价（"倾向于模糊表述"那种）
```

⚠️ 这个脚本**要花钱**（每段对话一次 LLM 请求，约 6~8 次）。
所以默认只跑 3 个最关键的场景，`--all` 才跑全部。

## 与产品代码的关系

它**不写任何记忆**（不碰 `pet.db`），只调 `FactExtractor.extract()`
看返回值 —— 提炼是纯函数式的（输入对话、输出字符串列表）。

用法：
    python tools/test-extract-quality.py          # 3 个关键场景
    python tools/test-extract-quality.py --all    # 全部 6 个
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

ROOT = Path(r"F:\ai项目")
sys.path.insert(0, str(ROOT / "apps" / "brain"))

# 场景：(名字, 对话, 必须含的关键词列表, 必须**不含**的关键词列表)
SCENARIOS: list[tuple[str, list[tuple[str, str]], list[str], list[str]]] = [
    (
        "说了具体游戏名 -> 必须带出名字",
        [
            ("user", "我最近在玩银松镇，那个机关卡了我好久"),
            ("assistant", "银松镇那个转盘机关？我记得要先点亮三盏灯诶"),
            ("user", "对就是那个！我绕了半小时才找到第二盏"),
        ],
        ["银松镇"],
        [],
    ),
    (
        "只说「在玩游戏」不说玩什么 -> 不许记这条",
        [
            ("user", "今天有点累，就在家玩玩游戏"),
            ("assistant", "那挺好的，放松一下嘛"),
            ("user", "嗯，随便玩玩"),
        ],
        [],
        ["用户在玩游戏", "用户玩游戏"],
    ),
    (
        "用户教了一个知识 -> 记成共享知识 + 带时间",
        [
            ("user", "对了，银松镇是手绘风格的生存恐怖游戏，画风特别怪诞"),
            ("assistant", "哦原来是这样，难怪看着那么有味道"),
            ("user", "它今年 10 月 28 号才公测的"),
        ],
        ["银松镇"],
        [],
    ),
    (
        "一起查到的答案 -> 必须记下（旧规则全丢）",
        [
            ("user", "帮我查一下 GTA6 什么时候发售"),
            ("assistant", "查到了，GTA6 定在 2026 年 11 月 19 日发售，平台是 PS5 和 Xbox"),
            ("user", "行，那还早呢"),
        ],
        ["GTA6"],
        [],
    ),
    (
        "用户明确说的偏好 -> 直说，不许加「可能」",
        [
            ("user", "我不喜欢吃香菜，闻到就想吐"),
            ("assistant", "哈哈懂，很多人这样"),
            ("user", "对，点外卖一定要备注不要香菜"),
        ],
        ["香菜"],
        ["可能不喜欢"],
    ),
    (
        "评价用户的话不许记（旧规则会记）",
        [
            ("user", "嗯嗯"),
            ("assistant", "你今天话不多诶"),
            ("user", "有点累"),
        ],
        [],
        ["倾向于", "模糊表述", "反复询问"],
    ),
]


async def main() -> int:
    from pet.config import load_config
    from pet.memory import FactExtractor, MemoryConfig

    # 🔴 `Config.api_key()` 的第 ④ 条兜底路径（`apps/desktop/pet-data/credentials.json`）
    #    **是靠 `PET_CREDENTIALS_FILE` 环境变量注入的** —— 由 Electron 主进程设置。
    #    独立跑脚本时没人设它，于是 `api_key()` 返回空、brain 降级成 mock、
    #    所有场景都"没抽到任何条目"，看起来像提示词坏了（实测就是这么被骗的）。
    #
    # ✅ 所以这里主动指一次。⚠️ 只设路径，不读、不打印密钥。
    import os

    cred = ROOT / "apps" / "desktop" / "pet-data" / "credentials.json"
    if cred.exists() and not os.environ.get("PET_CREDENTIALS_FILE"):
        os.environ["PET_CREDENTIALS_FILE"] = str(cred)

    cfg = load_config(ROOT / "config" / "persona.yaml")

    key = cfg.api_key()
    env_name = str(
        ((cfg.root.get("providers") or {}).get("brain") or {}).get("api_key_env") or ""
    ).strip()
    if key and env_name and not os.environ.get(env_name):
        os.environ[env_name] = key
        print(f"  （已把密钥挂到 {env_name}，值不打印）")
    if not key:
        print("  ❌ 找不到 API 密钥 —— 这条真机验证跑不了")
        print(f"     试过：{cred}")
        return 1

    # 复用真实的 brain（跟产品同一套配置 / 密钥）
    # ⚠️ `build_brain(config, emotion)` 要两个参数 —— 签名的第二项是情绪状态，
    #    提炼用不到它，给个空的就行。
    from pet.brains import EmotionState, build_brain

    brain = build_brain(cfg, EmotionState())
    if brain is None:
        print("  ❌ 构建 brain 失败")
        return 1

    extractor = FactExtractor(brain, MemoryConfig(enabled=True), cfg)
    limit = 3 if "--all" not in sys.argv else len(SCENARIOS)

    print("=" * 86)
    print(f"  真机验证记忆提炼质量（{limit} 个场景）")
    print("=" * 86)

    passed = 0
    failed: list[str] = []
    for name, turns, must, must_not in SCENARIOS[:limit]:
        print()
        print(f"  ▶ {name}")
        facts = await extractor.extract(turns)
        if not facts:
            print("     （没抽到任何条目）")
        for f in facts:
            print(f"       · {f}")

        ok = True
        blob = "\n".join(facts)
        for word in must:
            if word not in blob:
                print(f"     ❌ 缺少关键内容：{word!r}")
                ok = False
        for word in must_not:
            if word in blob:
                print(f"     ❌ 出现了不该有的：{word!r}")
                ok = False
        if ok:
            passed += 1
            print("     ✅ 通过")
        else:
            failed.append(name)

    print()
    print("=" * 86)
    print(f"  结果：{passed}/{passed + len(failed)} 通过")
    for name in failed:
        print(f"    ❌ {name}")
    print("=" * 86)
    return 1 if failed else 0


raise SystemExit(asyncio.run(main()))
