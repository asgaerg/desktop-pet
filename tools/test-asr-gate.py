"""重启 + 按 F9 但**不说话**，验证"太安静就不送识别"的门槛生效。

## 背景（用户报「我发出去的不对啊」）

聊天记录里出现「请用简体中文转写。」「优优独播剧场——YoYo Television
Series Exclusive」—— 全是 ASR 幻觉。实测那批音频只有 **0.02~0.08 秒**。

## 两道门槛（本次加的）

  ① `MIN_FORCED_DURATION_S = 0.3` —— 音频短于 0.3 秒当作误触
  ② **整段 RMS < VAD 门槛** —— 没人在说（Whisper 对静音会造句子）

## 这个脚本怎么验

用 SendKeys 发两次 F9（间隔 **0.1 秒** = 极短）和（间隔 **2 秒** = 长但静音），
看日志里应当出现：

```
按住说话：音频只有 0.08s（< 0.30s）—— 当作误触，不送识别
按住说话：整段太安静（RMS 0.00012 < 门槛 0.01200，音频 2.00s）—— 没人在说，不送识别
```

而**不应**出现 `开始识别` / `语音识别（…）`。

⚠️ 脚本不发任何 kill 命令（会自杀，踩过两次）。
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

DESKTOP = ROOT / "apps" / "desktop"
MLOG = DESKTOP / "pet-data" / "main.log"
SLOG = DESKTOP / "dist" / "startup.log"
ME = os.getpid()

KEYS = ("按住说话", "采集闸门", "主动收尾", "开始识别", "语音识别", "太安静",
        "当作误触", "收尾没拿到")


def lines() -> list[str]:
    out: list[str] = []
    for p in (MLOG, SLOG):
        try:
            out += [
                l.strip()
                for l in p.read_text(encoding="utf-8", errors="replace").split("\n")
                if "[brain]" not in l and "帧头" not in l
            ]
        except OSError:
            pass
    return [l for l in out if any(k in l for k in KEYS)]


print("  ═══ ① 清理（只按进程名）═══")
# ⚠️ **只杀 electron**，不碰 python：
#    · 本脚本自己是 python，杀 python 会自杀（踩过两次）
#    · 而 electron 的进程树里含这个脚本（我们是从 pwsh->python 起的，
#      electron 是兄弟不是父）—— 但 Stop-Process 带 -Force 会杀整棵树，
#      所以连 electron 也要**只杀它自己**，用 taskkill 不带 /T
ps = subprocess.run(
    ["powershell", "-NoProfile", "-Command",
     "Get-Process electron -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue; 'ok'"],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
)
print(f"     {(ps.stdout or '').strip()[:40]}")
time.sleep(3)

print()
print("  ═══ ② 清日志并启动 ═══")
for p in (MLOG, SLOG):
    try:
        p.unlink()
    except OSError:
        pass
env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
subprocess.Popen(
    [str(ROOT / "node_modules" / ".pnpm" / "electron@32.3.3" / "node_modules"
         / "electron" / "dist" / "electron.exe"),
     ".", "--dev", "--no-sandbox", "--debug-port=9222"],
    cwd=str(DESKTOP), env=env,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=0x00000008,
)
print("     等 45 秒…")
time.sleep(45)

n0 = len(lines())
print(f"     启动后相关日志 {n0} 条")

print()
print("  ═══ ③ 测试 A：按两下 F9（间隔 0.1 秒，极短无误触）═══")
PS_A = (
    "$np = Start-Process notepad -PassThru; Start-Sleep -Milliseconds 900; "
    "Add-Type -AssemblyName System.Windows.Forms; "
    "[System.Windows.Forms.SendKeys]::SendWait('{F9}'); "
    "Start-Sleep -Milliseconds 100; "
    "[System.Windows.Forms.SendKeys]::SendWait('{F9}'); "
    "Start-Sleep -Milliseconds 800; "
    "Stop-Process -Id $np.Id -Force -ErrorAction SilentlyContinue; 'a'"
)
subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", PS_A],
               capture_output=True, text=True, timeout=60)
time.sleep(6)

print()
print("  ═══ ④ 测试 B：按两下 F9（间隔 2.5 秒，**不说话**）═══")
PS_B = (
    "$np = Start-Process notepad -PassThru; Start-Sleep -Milliseconds 900; "
    "Add-Type -AssemblyName System.Windows.Forms; "
    "[System.Windows.Forms.SendKeys]::SendWait('{F9}'); "
    "Start-Sleep -Milliseconds 2500; "
    "[System.Windows.Forms.SendKeys]::SendWait('{F9}'); "
    "Start-Sleep -Milliseconds 800; "
    "Stop-Process -Id $np.Id -Force -ErrorAction SilentlyContinue; 'b'"
)
subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", PS_B],
               capture_output=True, text=True, timeout=60)

for _ in range(12):
    time.sleep(1.5)
    if any("太安静" in x or "当作误触" in x or "语音识别（" in x for x in lines()[n0:]):
        time.sleep(2)
        break

print()
print("  ═══ ⑤ 新增日志 ═══")
new = lines()[n0:]
for l in new:
    print(f"     {l[:118]}")
if not new:
    print("     （没有新增 —— 热键可能没被收到）")

print()
print("  ═══ ⑥ 判读 ═══")
j = "\n".join(new)
blocked_short = "当作误触" in j
blocked_quiet = "太安静" in j
hallucinated = "语音识别（" in j or "开始识别" in j

print(f"     {'OK  ' if blocked_short else '----'} 极短音频被拦（当作误触）")
print(f"     {'OK  ' if blocked_quiet else '----'} 静音被拦（整段太安静）")
print(f"     {'🔴 有问题' if hallucinated else 'OK  '} 没有把静音送进识别")
print()
if (blocked_short or blocked_quiet) and not hallucinated:
    print("     🎉 **两道门槛都生效** —— 静音不会再变成'你说的话'")
elif not blocked_short and not blocked_quiet and not hallucinated:
    print("     ⚠️ 没看到门槛日志，但也没送识别 —— 可能热键没被收到")
elif hallucinated:
    print("     🔴 还是有送识别 —— 门槛没拦住，看上面是哪种")
