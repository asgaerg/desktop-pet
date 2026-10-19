r"""核对 开发日志-2026-10-17 里每一节的功能是否真的落地（2026-10-17）。

## 为什么要核对而不是相信日志

昨天（10-16）的日志里写着「状态栏四档状态机」，核对时发现实际是**六档**
（`thinking` 紫 + `voice` 青 一直在跑）。而今天的日志开头明确说：

> 「前置：昨天最后一项做的「状态栏状态机」今天整个重做了，
>   `statusKindFor` / `refreshActivityStatus` / `statusWindow.ts` 的现状以**本篇为准**」

所以更要对**代码本体**验一遍 —— 日志是人的叙述，代码才是事实。

## 🔴 关键：必须**去掉注释**再匹配

第一版直接 `needle in file_text`，结果 3 项"对不上"**全是假阳性**：

```
① DETACHED_PROCESS   实际只在注释里：
     "# ⚠️ 之前这里还加了 DETACHED_PROCESS —— 但它和 CREATE_NO_WINDOW 互斥"
   —— 那是**解释为什么删掉它**，正是该留的文档

② 'voice'            实际只在注释里：
     "// 🔴 'voice'（语音准备中）2026-10-17 删"

③ onPlaybackEnded    我查错了文件：它在 app.js，不在 index.ts
```

⚠️ 这个坑和 §111 那次一模一样（"已删项残留：状态："是 bundle 里的注释）。
**凡是"检查某个词不该存在"，都必须先剥注释**，否则检查的是文档而不是代码。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(r"F:\ai项目")


def strip_comments(text: str, suffix: str) -> str:
    """去掉注释，只留可能被执行的代码。

    · .py  -> 用 tokenize（比正则可靠，能正确处理字符串里的 #）
    · .ts/.js/.css/.html -> 正则去掉 // 与 /* */（对 TS 里的 URL 会误伤，
      但这些检查用的词都不是 URL 片段，够用）
    """
    if suffix == ".py":
        import io
        import tokenize

        out: list[str] = []
        try:
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type == tokenize.COMMENT:
                    continue
                out.append(tok.string)
        except (tokenize.TokenError, IndentationError):
            # 解析不了就退回正则
            return re.sub(r"#[^\n]*", "", text)
        return " ".join(out)
    # TS / JS / CSS / HTML：块注释 + 行注释
    text = re.sub(r"/\*[\s\S]*?\*/", " ", text)
    text = re.sub(r"(?m)//[^\n]*", " ", text)
    # HTML 注释
    text = re.sub(r"<!--[\s\S]*?-->", " ", text)
    return text


# (日志节, 功能名, 文件, 必须存在的代码特征, 必须**不存在**的特征)
CHECKS: list[tuple[str, str, str, tuple[str, ...], tuple[str, ...]]] = [
    # ── §1 重建的脚本 ──
    ("§1", "check-log-rotation.py 重建",
     "tools/check-log-rotation.py", ("ast",), ()),
    ("§1", "check-tts-gate.py 重建",
     "tools/check-tts-gate.py", (), ()),
    ("§1", "probe-search-results.py 重建",
     "tools/probe-search-results.py", (), ()),
    ("§1", "probe-ab-search.py 重建",
     "tools/probe-ab-search.py", (), ()),
    ("§1", "shot-page.py 重建",
     "tools/shot-page.py", (), ()),

    # ── §2 搜索拉起不再弹黑框 ──
    ("§2", "只用 CREATE_NO_WINDOW，不用 DETACHED_PROCESS",
     "apps/brain/pet/mcp_search.py", ("CREATE_NO_WINDOW",), ("DETACHED_PROCESS",)),

    # ── §3 状态条高度随字数 ──
    ("§3", "heightForLabel 公式",
     "apps/desktop/src/main/statusWindow.ts", ("heightForLabel", "78", "15.3"), ()),
    ("§3", ".bar 高度跟窗口",
     "apps/desktop/src/renderer/status.css", ("100vh",), ()),

    # ── §4 状态条五档 ──
    ("§4", "StatusKind 五档（'voice' 已删）",
     "apps/desktop/src/main/statusWindow.ts",
     ("'idle'", "'thinking'", "'searching'", "'speaking'", "'error'"),
     ("'voice'",)),
    ("§4", "voicePrep 变量已删",
     "apps/desktop/src/main/index.ts", (), ("voicePrep",)),
    ("§4", "CSS 的 voice 档已删",
     "apps/desktop/src/renderer/status.css",
     ("state='idle'", "state='thinking'", "state='searching'",
      "state='speaking'", "state='error'"), ("state='voice'",)),
    ("§4", "说话中改青色 #32ade6",
     "apps/desktop/src/renderer/status.css", ("#32ade6",), ()),
    ("§4", "文字全称「联网搜索中」",
     "apps/desktop/src/main/index.ts", ("联网搜索中",), ()),
    ("§4", "文字全称「说话中」",
     "apps/desktop/src/main/index.ts", ("说话中",), ()),
    ("§4", "起点信号 notifyVoiceSpeaking（preload）",
     "apps/desktop/src/preload/index.ts", ("notifyVoiceSpeaking",), ()),
    ("§4", "终点信号 notifyVoicePlayed（preload）",
     "apps/desktop/src/preload/index.ts", ("notifyVoicePlayed",), ()),
    ("§4", "IPC pet:voice-speaking",
     "apps/desktop/src/main/index.ts", ("pet:voice-speaking",), ()),
    ("§4", "IPC pet:voice-played",
     "apps/desktop/src/main/index.ts", ("pet:voice-played",), ()),
    ("§4", "看门狗纳入 voiceSpeaking",
     "apps/desktop/src/main/index.ts", ("voiceSpeaking",), ()),
    # ⚠️ onPlaybackEnded 在 app.js（第一版查 index.ts 查错了）
    ("§4", "每次播放前无条件重挂 onPlaybackEnded",
     "apps/desktop/src/renderer/app.js", ("audio.onPlaybackEnded",), ()),
    ("§4", "播放结束幂等 report（时长兜底）",
     "apps/desktop/src/renderer/voice-io.js",
     ("onPlaybackEnded", "duration"), ()),

    # ── §5 气泡排版 ──
    ("§5", "段落化 renderParagraphs",
     "apps/desktop/src/renderer/bubble.js", ("renderParagraphs",), ()),
    ("§5", "bubbleRaw 缓冲",
     "apps/desktop/src/renderer/bubble.js", ("bubbleRaw",), ()),
    ("§5", "--para-gap 变量",
     "apps/desktop/src/renderer/bubble.css", ("--para-gap",), ()),
    ("§5", ".para + .para 段间距",
     "apps/desktop/src/renderer/bubble.css", (".para + .para",), ()),
    ("§5", "宽度分档 520 / 300",
     "apps/desktop/src/renderer/bubble.js", ("520", "300"), ()),
    ("§5", "settingsStore 的 bubbleParaGap",
     "apps/desktop/src/main/settingsStore.ts", ("bubbleParaGap",), ()),
    ("§5", "设置页段间距输入框",
     "apps/desktop/src/renderer/settings.html", ("段间距",), ()),
    ("§5", "行高 1.45",
     "apps/desktop/src/renderer/bubble.css", ("1.45",), ()),

    # ── §6 shot-page js= 能力 ──
    ("§6", "shot-page 支持 js= 注入",
     "tools/shot-page.py", (".shot-page-user.js",), ()),
]


def main() -> int:
    print("=" * 88)
    print("  核对 开发日志-2026-10-17：每节功能是否真的落地")
    print("=" * 88)
    bad: list[str] = []
    for section, name, rel, must, must_not in CHECKS:
        path = ROOT / rel
        if not path.exists():
            print(f"  ❌ {section} {name}")
            print(f"       文件不存在：{rel}")
            bad.append(name)
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        code = strip_comments(raw, path.suffix)
        missing = [n for n in must if n not in code]
        leftover = [n for n in must_not if n in code]
        if missing or leftover:
            print(f"  ❌ {section} {name}")
            if missing:
                print(f"       缺：{missing}")
            if leftover:
                print(f"       本该删掉却在**代码**里：{leftover}")
            bad.append(name)
        else:
            print(f"  ✅ {section} {name}")

    print()
    print("=" * 88)
    if bad:
        print(f"  ⚠️ {len(bad)} 项对不上：")
        for name in bad:
            print(f"      · {name}")
    else:
        print(f"  ✅ 日志里 {len(CHECKS)} 项功能，代码全部对得上")
        print("     （注释里提到「已删的东西」不算残留 —— 那是该留的文档）")
    print("=" * 88)
    return 1 if bad else 0


raise SystemExit(main())
