"""验证「截图对话」的后端链路（不经过 UI）。

## 验什么

```
① 配置能读到 screenshot 段
② capture_and_describe 真能抓屏 + 看懂（走真实模型）
③ 隐私黑名单能挡住（伪造标题）
④ 看懂之后注意力被拉到贴脸、表情变好奇
⑤ describe_for_prompt 产出的那段话里有"主动聊"的指令
```

⚠️ 这个脚本会真调一次模型（约 1~6 秒、几分钱），并且会**把当前屏幕发给模型**。
   那是这个功能的固有行为，不是 bug。

用法：
    python tools/test-screenshot-chat.py
    python tools/test-screenshot-chat.py --no-vlm    # 只验链路，不调模型
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
ROOT = pathlib.Path(r"F:\ai项目")
sys.path.insert(0, str(ROOT / "apps" / "brain"))
# 让 sidecar 能找到真实密钥（正常运行时由 Electron 主进程注入）
import os

os.environ.setdefault("PET_CREDENTIALS_FILE", str(ROOT / "apps" / "desktop" / "pet-data" / "credentials.json"))

from pet.config import load_config  # noqa: E402
from pet.screen_chat import (  # noqa: E402
    DESCRIBE_PROMPT,
    _describe_prompt,
    capture_and_describe,
    describe_for_prompt,
)
from pet.subjectivity import ATTENTION_ON_SCREEN, SubjectiveState  # noqa: E402
from pet.vision import PrivacyRules  # noqa: E402


def sec(title: str) -> None:
    print()
    print(f"  ── {title} ──")


async def main() -> int:
    only_chain = "--no-vlm" in sys.argv
    failed: list[str] = []

    print("=" * 78)
    print("  「截图对话」后端链路验证")
    print("=" * 78)

    cfg = load_config(ROOT / "config" / "persona.yaml")

    # ① 配置
    sec("① 配置")
    sc = cfg.screenshot
    print(f"     enabled={sc['enabled']} max_edge_px={sc['max_edge_px']} "
          f"thinking={sc['thinking']} max_tokens={sc['max_tokens']}")
    if sc["thinking"]:
        failed.append("thinking 开着 —— 看图会得到空正文")
        print("     ❌ thinking 开着！看图会被推理吃掉 token")
    else:
        print("     ✅ thinking=false（看图必须关）")
    if sc["respect_privacy"] is not True:
        print("     ⚠️ respect_privacy 不是 true —— 隐私黑名单被关了")
    profile = cfg.brain_profile()
    print(f"     复用大脑：{profile.get('model')} @ {profile.get('base_url')}")
    print(f"     密钥：{'有' if cfg.api_key() else '❌ 没有'}")

    # ② 真抓屏 + 看懂
    sec("② 真抓屏 + 看懂")
    result = await capture_and_describe(cfg)
    print(f"     ok={result.ok} 来源={result.source} "
          f"{result.width}x{result.height} PNG {result.image_bytes / 1024:.0f}KB "
          f"{result.elapsed_ms:.0f}ms")
    print(f"     标题 {list(result.titles)[:4]}")
    if result.description:
        print(f"     描述：{result.description[:160]}")
    if result.message:
        print(f"     说明：{result.message}")
    if only_chain:
        print("     （--no-vlm：只验链路，成败不计入）")
    elif not result.ok:
        failed.append(f"截图链路失败：{result.message}")
        print("     ❌ 失败")
    elif result.source != "vlm":
        failed.append(f"没有真正看懂（来源={result.source}）")
        print(f"     ❌ 降级到 {result.source}，视觉模型没工作")
    else:
        print("     ✅ 视觉模型真看懂了")
        if result.elapsed_ms > 20000:
            print(f"     ⚠️ 耗时 {result.elapsed_ms:.0f}ms 偏慢（思考模式没关？）")

    # ③ 隐私黑名单
    sec("③ 隐私黑名单能挡住")
    rules = PrivacyRules.from_config(cfg.privacy)
    verdict = rules.check(titles=("1Password 密码管理器",))
    print(f"     伪造标题「1Password 密码管理器」-> allowed={verdict.allowed} 原因={verdict.reason}")
    if verdict.allowed:
        failed.append("隐私黑名单没挡住密码管理器")
        print("     ❌ 没挡住")
    else:
        print("     ✅ 被挡住")
    ok_verdict = rules.check(titles=("Untitled - Notepad",))
    if not ok_verdict.allowed:
        failed.append("普通窗口标题被误挡")
        print(f"     ❌ 误挡了普通窗口：{ok_verdict.reason}")
    else:
        print("     ✅ 普通窗口标题不误挡")

    # ④ 注意力 + 好奇
    sec("④ 注意力被拉到贴脸")
    state = SubjectiveState(attention=0.9)
    before = state.attention
    state.on_user_shows_screen()
    print(f"     注意力 {before:.3f} -> {state.attention:.3f}"
          f"（档位 {state.band}）")
    print(f"     焦点：{state.focus}")
    print(f"     好奇表情到期：{state.emotion._curious_until:.0f}"
          f" -> dominant={state.emotion.dominant}")
    if state.attention > ATTENTION_ON_SCREEN + 1e-9:
        failed.append("注意力没被压到屏幕级")
        print("     ❌ 没压下来")
    else:
        print("     ✅ 压到 0.06（全在用户身上）")
    if state.emotion.dominant != "curious":
        failed.append("表情没变好奇")
        print(f"     ❌ 表情是 {state.emotion.dominant}")
    else:
        print("     ✅ 表情变 curious")
    if state.may_talk_about_self(False):
        print("     ⚠️ 注意力在用户身上却还想聊自己（硬门失效）")
    else:
        print("     ✅ 这时它不会聊自己的事")

    # ⑤ 提示词
    sec("⑤ 给模型的那段话")
    hint = describe_for_prompt(result) if result.ok else ""
    print(f"     {hint[:120] or '（空）'}")
    if result.ok:
        if "主动" not in hint:
            failed.append("hint 里没有\"主动聊\"的指令")
            print("     ❌ 没让主动聊")
        else:
            print("     ✅ 含\"主动跟他说说这个画面\"")
    print()
    print(f"     yaml 与内置兜底一致：{_describe_prompt(cfg).strip() == DESCRIBE_PROMPT.strip()}")

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
