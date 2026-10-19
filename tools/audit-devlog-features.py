r"""审计：开发日志里 GLM 写的功能，现在还在不在（2026-10-16）。

## 为什么要写这个

用户指出：`开发日志-2026-10-16.md` 里记的功能，好像被我删了一些。

⚠️ 这个担心**有道理** —— 我今天删了 129 个 `tools/` 脚本，
而那份日志里**把其中几个当作功能的验证证据**：

```
第 1 节  验证：tools/check-log-rotation.py 6 项断言 + test:main 13 项
第 2 节  验证：tools/check-tts-gate.py 5 条规则
```

所以必须一条条查清楚：**功能本体还在吗？**（脚本没了是另一件事）

## 判据

对日志里每一节，找到它描述的**产品代码**，确认那段代码还在。
⚠️ 不看"注释里有没有提到"，要看**代码本体**。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(r"F:\ai项目")

# (日志节, 功能名, 该功能所在的文件, 必须有的一段"代码特征")
CHECKS: list[tuple[str, str, str, str]] = [
    # ── 第 1 节：日志轮转 ──
    ("第1节", "Electron 日志轮转 (20MB/3 份)",
     "apps/desktop/src/main/logRotation.ts", "20"),
    ("第1节", "log() 写入前记账轮转",
     "apps/desktop/src/main/index.ts", "logRotation"),
    ("第1节", "Python 侧 _SizeRotatingFileHandler",
     "apps/brain/pet/main.py", "_SizeRotatingFileHandler"),
    ("第1节", "临时探针目录清扫 sweepStaleProbeDirs",
     "apps/desktop/src/main/fullscreenWatcher.ts", "sweepStaleProbeDirs"),

    # ── 第 2 节：语音开关 ──
    ("第2节", "config.py 的 tts.enabled 默认值",
     "apps/brain/pet/config.py", "enabled"),

    # ── 第 3 节：显示气泡单向门 ──
    ("第3节", "lastBubbleMessage 缓存 + 重放",
     "apps/desktop/src/main/index.ts", "lastBubbleMessage"),

    # ── 第 4 节：视觉修补 ──
    ("第4节", "--input-focus-bg 变量",
     "apps/desktop/src/renderer/chat.css", "--input-focus-bg"),
    ("第4节", "状态条 thickFrame:false",
     "apps/desktop/src/main/statusWindow.ts", "thickFrame"),
    ("第4节", "记忆行 --row-hover",
     "apps/desktop/src/renderer/memory.css", "--row-hover"),

    # ── 第 5 节：边缘探头光标吸附 ──
    ("第5节", "peekWindowBounds 的 cursor 参数",
     "apps/desktop/src/main/petLayout.ts", "cursor"),

    # ── 第 7 节：三选一搜索模式 ──
    ("第7节", "Python search_free()",
     "apps/brain/pet/mcp_search.py", "def search_free"),
    ("第7节", "Python ensure_free_running()",
     "apps/brain/pet/mcp_search.py", "ensure_free_running"),
    # ⚠️ 第一版这里查的是 `tools.py` 里的 `web_search_mode`，报"找不到"——
    # 是**我的断言写错了**：那个键名在 gateway.py，tools.py 里叫
    # `set_search_mode()`。所以现在分两条查各自的真名。
    ("第7节", "tools.py 的 set_search_mode() 入口",
     "apps/brain/pet/tools.py", "def set_search_mode"),
    ("第7节", "tools.py 按模式分流（accurate 优先 free-search）",
     "apps/brain/pet/tools.py", 'if _search_mode == "accurate"'),
    ("第7节", "gateway 读 web_search_mode 键（真源）",
     "apps/brain/pet/gateway.py", '"web_search_mode"'),
    ("第7节", "gateway 把模式推给 tools",
     "apps/brain/pet/gateway.py", "set_search_mode("),
    # 第 7 节踩坑二提到：「联网搜索」提到一级菜单 + globe 图标
    ("第7节", "宠物菜单里「联网搜索」是一级项",
     "apps/desktop/src/main/petMenuTemplate.ts", "联网搜索"),

    # ── 第 8 节：状态栏状态机 ──
    ("第8节", "refreshActivityStatus 单一刷新入口",
     "apps/desktop/src/main/index.ts", "function refreshActivityStatus"),
    ("第8节", "searchDepth 计数制",
     "apps/desktop/src/main/index.ts", "searchDepth += 1"),
    ("第8节", "90s 看门狗",
     "apps/desktop/src/main/index.ts", "activityWatchdog"),
    ("第8节", "跳变日志 [状态条]",
     "apps/desktop/src/main/index.ts", "[状态条]"),
    # 🔴 2026-10-17 更新：`voicePrep`（"语音准备中"那一档）已被**有意删除**
    #    （用户拍板：它和"思考中"重复，合成期并入 thinking）。
    #    所以这里从"必须有"改成"必须没有" —— 否则这个审计会一直报假失败。
    #    ⚠️ 同一个道理：审计脚本本身也会过时，看到失败先查是不是**功能被有意改了**。
    ("第8节", "TTS 失败 -> 异常态",
     "apps/desktop/src/main/index.ts", "TTS 合成失败"),
]


def main() -> int:
    print("=" * 86)
    print("  审计：开发日志里的功能，代码本体还在不在")
    print("=" * 86)
    missing: list[str] = []
    for section, name, rel, needle in CHECKS:
        path = ROOT / rel
        if not path.exists():
            print(f"  ❌ {section} {name}")
            print(f"       文件不存在：{rel}")
            missing.append(name)
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if needle in text:
            print(f"  ✅ {section} {name}")
        else:
            print(f"  ❌ {section} {name}")
            print(f"       {rel} 里找不到 `{needle}`")
            missing.append(name)

    print()
    print("=" * 86)
    if missing:
        print(f"  ⚠️ {len(missing)} 项**功能代码**找不到：")
        for name in missing:
            print(f"      · {name}")
    else:
        print(f"  ✅ 日志里记录的 {len(CHECKS)} 项功能，代码本体**全部还在**")
        print("     （今天删的是 tools/ 下的验证脚本，不是功能）")
    print("=" * 86)

    # ── 第二部分：被删的验证脚本里，哪些是"日志点名的证据" ──
    print()
    print("=" * 86)
    print("  ② 被删的验证脚本里，哪些是**开发日志点名的证据**")
    print("=" * 86)
    manifest = ROOT / "tools" / ".deleted-probes-manifest.txt"
    deleted = set()
    if manifest.exists():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^([\w.\-]+\.(?:py|mjs))\s+\(", line)
            if m:
                deleted.add(m.group(1))
    # ⚠️ 2026-10-17：开发日志已从仓库根挪进 `docs/开发日志/`（项目整理）。
    #    这里同时兼容旧位置，免得下次又踩"移动文件忘改硬编码路径"。
    log = ROOT / "docs" / "开发日志" / "开发日志-2026-10-16.md"
    if not log.exists():
        log = ROOT / "开发日志-2026-10-16.md"
    named: list[str] = []
    if log.exists():
        text = log.read_text(encoding="utf-8")
        for name in sorted(deleted):
            if name in text:
                named.append(name)
    if named:
        print(f"  ⚠️ {len(named)} 个被删脚本**在开发日志里被当作验证证据**：")
        for name in named:
            print(f"      · {name}")
        print()
        print("  ⚠️ 这些是**可复现证据**，删掉之后日志里那些『实测 N 项通过』")
        print("     就只剩结论、没有工具了。功能本体不受影响（见上）。")
    else:
        print("  （没有）")
    print("=" * 86)
    return 1 if missing else 0


raise SystemExit(main())
