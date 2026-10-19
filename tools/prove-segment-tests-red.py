"""证明「一段交流 + 印象 + 强化 + 线」的新测试能红（2026-10-17）。

## 为什么单独一个脚本

项目铁律：**每一条新检查都必须被证明"能失败"**。
只跑一遍全绿，说明不了它真的在检查东西 —— 可能是因为它压根没断言到。

这个脚本对三处注入 bug，逐一验证"只有对应的那条红"：

```
① segments.should_close 永远返回 False  -> 段切不了，测试该红
② 印象解析去掉贴标签过滤                -> 标签能进库，测试该红
③ bump_importance 不封顶               -> importance 能超 1，测试该红
```

每次注入后跑自检、抓相关条目、然后还原并再跑一次确认回到全绿。

用法：
    python tools/prove-segment-tests-red.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

ROOT = Path(r"F:\ai项目")
BRAIN = ROOT / "apps" / "brain"
PY = ROOT / ".venv" / "Scripts" / "python.exe"

# (文件, 原文, 替换, 期望变红的检查名关键字, 说明)
INJECTIONS: list[tuple[Path, str, str, str, str]] = [
    (
        BRAIN / "pet" / "segments.py",
        '        if now - self.last_at >= self.gap_s:\n            return True, "time_gap"',
        '        if False:\n            return True, "time_gap"',
        "一段交流",
        "时间断点失效（段永远切不了）",
    ),
    (
        BRAIN / "pet" / "memory.py",
        '        if any(pattern.search(item) for pattern in _IMPRESSION_LABEL_PATTERNS):\n            continue',
        "        pass",
        "印象解析",
        "去掉贴标签过滤（\"用户是个话少的人\"能进库）",
    ),
    (
        BRAIN / "pet" / "gateway.py",
        "impressions = await extractor.extract_impressions(turns, existing=existing)",
        "impressions = await extractor.extract_impressions(turns)",
        "印象：已有",
        "不传已有印象（参数加了但没人传 = 线没接）",
    ),
    (
        BRAIN / "pet" / "store.py",
        "f\"UPDATE memory SET importance = MIN(1.0, importance + ?) WHERE id IN ({ph})\"",
        "f\"UPDATE memory SET importance = importance + ? WHERE id IN ({ph})\"",
        "强化",
        "不封顶（importance 能超过 1.0）",
    ),
]


def run_selftest() -> str:
    # ⚠️ 必须在**现有环境**上叠加，不能新建一个只含三项的 dict ——
    # Windows 子进程少了 SYSTEMROOT 之类会直接起不来，
    # 表现就是"输出为空、结果：? "（第一版就是这么踩的）。
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(BRAIN)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [str(PY), "-m", "pet.selftest"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=600,
    )
    return proc.stdout + proc.stderr


def failed_checks(output: str) -> list[str]:
    return re.findall(r"\[FAIL\]\s+(.+)", output)


def tail(output: str, lines: int = 6) -> str:
    """子进程输出为空时，必须能看到它到底说了什么。"""
    parts = [line for line in output.splitlines() if line.strip()]
    return "\n".join("       | " + line.strip()[:100] for line in parts[-lines:])


def main() -> int:
    print("=" * 78)
    print("  证明新测试能红（四段注入）")
    print("=" * 78)
    bad: list[str] = []

    for path, old, new, keyword, desc in INJECTIONS:
        print()
        print(f"  ▶ 注入：{desc}")
        print(f"     文件：{path.relative_to(ROOT)}")
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print("     ❌ 找不到注入锚点（代码变了，请更新脚本）")
            bad.append(f"{desc}：锚点缺失")
            continue
        backup = path.with_suffix(path.suffix + ".provbak")
        shutil.copy2(path, backup)
        try:
            path.write_text(original.replace(old, new, 1), encoding="utf-8")
            out = run_selftest()
            fails = failed_checks(out)
            matched = [f for f in fails if keyword in f]
            print(f"     变红的检查：{fails or '（无）'}")
            if matched:
                print(f"     ✅ 抓到了：{matched[0].strip()}")
            else:
                print("     ❌ 没有对应的检查变红 —— 这条断言形同虚设")
                if not fails:
                    print("     子进程输出尾部（用于判断是不是根本没跑起来）：")
                    print(tail(out))
                bad.append(f"{desc}：没抓到")
        finally:
            shutil.copy2(backup, path)
            backup.unlink(missing_ok=True)

    print()
    print("  ═══ 还原后应该全绿 ═══")
    out = run_selftest()
    fails = failed_checks(out)
    m = re.search(r"结果：(\d+)/(\d+)", out)
    print(f"     结果：{m.group(0) if m else '?'}")
    if fails:
        print(f"     ❌ 还原后仍有失败：{fails}")
        bad.append("还原后未恢复全绿")
    else:
        print("     ✅ 全绿")

    print()
    print("=" * 78)
    if bad:
        print(f"  ❌ {len(bad)} 项问题：")
        for item in bad:
            print(f"     · {item}")
    else:
        print("  ✅ 四条注入全部被对应检查抓到，还原后全绿")
    print("=" * 78)
    return 1 if bad else 0


raise SystemExit(main())
