"""重启 + 用**真实语音**做端到端测试（TTS 合成后从音箱放，麦克风收）。

## 为什么这样测

前面用 SendKeys 测过"按了但不说话"（验证门槛），
但**没验过"按了且说话"能不能成功识别** —— 而那才是用户真正要的。

我没法让麦克风听到"我说话"，但可以让**电脑外放一段合成语音**：
扬声器 -> 空气 -> 麦克风，走的是完整的真实链路。

## 怎么做

  ① 重启应用（加载新的 VAD 参数）
  ② 先合成本地音频文件（用项目的云端 TTS），用系统播放器放出来
  ③ 在播放期间用 SendKeys 按一次 F9，放完再按一次
  ④ 看识别结果是不是接近那句原文

## 判据

  · 识别文本和原文**对得上** -> 整条链路（含新的 VAD 门槛）都通
  · 识别为空 / 幻觉      -> 门槛误杀了，要继续调
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
SPEAK_TEXT = "桌面宠物的程序用的是 Python 和 Electron"

KEYS = ("按住说话", "采集闸门", "采集电平", "主动收尾", "开始识别", "语音识别",
        "以语音输入", "太安静", "当作误触", "收尾没拿到")


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


print("  ═══ ① 合成本地音频（给音箱放）═══")
AUDIO = pathlib.Path(os.environ.get("TEMP", ".")) / "ptt-e2e.mp3"
synth = f'''
import asyncio, pathlib, sys
sys.path.insert(0, r"{ROOT / 'apps' / 'brain'}")
from pet.config import load_config
from pet.voice import build_voice_pipeline
async def main():
    cfg = load_config(pathlib.Path(r"{ROOT / 'config' / 'persona.yaml'}"))
    pipe = build_voice_pipeline(cfg, api_key=cfg.api_key() or "")
    payload, fmt = await pipe.tts.synthesize({SPEAK_TEXT!r})
    pathlib.Path(r"{AUDIO}").write_bytes(payload)
    print(f"{{fmt}} {{len(payload)}} bytes")
asyncio.run(main())
'''
r = subprocess.run(
    [sys.executable, "-c", synth],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
)
print(f"     {(r.stdout or '').strip()[:60]}")
if (r.stderr or "").strip():
    print(f"     stderr: {r.stderr.strip()[:200]}")
if not AUDIO.is_file():
    print("     ❌ 合成失败，退出")
    raise SystemExit(1)
print(f"     -> {AUDIO.name}  {AUDIO.stat().st_size} 字节")

print()
print("  ═══ ② 清理并启动应用 ═══")
subprocess.run(
    ["powershell", "-NoProfile", "-Command",
     "Get-Process electron -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue"],
    capture_output=True, text=True,
)
time.sleep(3)
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
print("  ═══ ③ 播放语音 + 同时按 F9（开始/结束）═══")
# 一次 PowerShell 里做三件事：起播放、按 F9、等放完、再按 F9
PS = (
    "$np = Start-Process notepad -PassThru; Start-Sleep -Milliseconds 800; "
    "Add-Type -AssemblyName System.Windows.Forms; "
    f"$p = Start-Process -FilePath '{AUDIO}' -PassThru; "
    "Start-Sleep -Milliseconds 300; "
    "[System.Windows.Forms.SendKeys]::SendWait('{F9}'); "
    "Start-Sleep -Seconds 6; "                       # 等音频放完
    "[System.Windows.Forms.SendKeys]::SendWait('{F9}'); "
    "Start-Sleep -Milliseconds 500; "
    "try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch {}; "
    "Stop-Process -Id $np.Id -Force -ErrorAction SilentlyContinue; 'played'"
)
r = subprocess.run(
    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", PS],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
)
print(f"     {(r.stdout or '').strip()[:40]}")

for _ in range(16):
    time.sleep(1.5)
    if any("语音识别（" in x or "太安静" in x or "收尾没拿到" in x for x in lines()[n0:]):
        time.sleep(3)
        break

print()
print("  ═══ ④ 新增日志 ═══")
new = lines()[n0:]
levels = [l for l in new if "采集电平" in l]
for l in new:
    if "采集电平" in l:
        continue
    print(f"     {l[:118]}")
if levels:
    print(f"     （采集电平 {len(levels)} 条，下面单独看）")

if levels:
    print()
    print("  ═══ ⑤ 采集电平（看阈值和语音判定的关系）═══")
    for l in levels[:14]:
        print(f"     {l[:124]}")

print()
print("  ═══ ⑥ 判读 ═══")
j = "\n".join(new)
rec = [l for l in new if "语音识别（" in l]
print(f"     识别结果：{rec[-1].split('）：')[-1][:60] if rec else '（没有）'}")
print()
checks = [
    ("F9 被收到", "按下（F9）"),
    ("有采集电平", "采集电平"),
    ("走到收尾", "主动收尾"),
    ("识别出结果", "语音识别（"),
]
for label, needle in checks:
    print(f"     {'OK  ' if needle in j else '----'} {label}")
print()
if rec:
    got = rec[-1]
    key = "Python" in got or "python" in got or "Electron" in got or "electron" in got
    if key:
        print("     🎉 **成功** —— 识别内容和播放的语音对得上")
    else:
        print("     ⚠️ 有识别结果但和原文对不上（看看是不是幻觉）")
elif "太安静" in j:
    print("     ⚠️ 被「整段太安静」门槛拦了 —— 阈值还是太高，要继续调")
elif "主动收尾" in j:
    print("     ⚠️ 走到收尾但没识别（可能空文本）")
else:
    print("     ❌ 没走到识别")

# 清理测试音频
try:
    AUDIO.unlink()
except OSError:
    pass
