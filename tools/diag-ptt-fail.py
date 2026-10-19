"""精确看一次失败的「按住说话」——为什么端点没攒下句子。

## 现象（19:14:31-19:14:44，用户说"没用"那一次）

```
19:14:31,154  phase='start' -> 采集闸门：开        ✅ 按键生效
19:14:32,477  energy=0.63004 -> 语音              ✅ 音频在推、VAD 判成语音
19:14:44,309  phase='end'                          ✅ 松开
（**没有「主动收尾」** -> force_end() 返回 None -> 没识别）
```

**关键**：这一轮 `phase='start'` 和 `采集电平` **都正常** ——
所以**不是"不聚焦就收不到"**，而是端点没攒成句子。

## 判据

`force_end()` 返回 None 只有两种可能：

  ① `Endpointer.state` 是 **IDLE** —— 从来没攒起过
  ② 攒过，但**已经被静音判定收走并丢弃**了
     （`_finish(reason="silence")` 里，`speech_ms < min_speech_ms(250)` -> 丢）

② 的样子是：**说了几句短的、每句之间停顿 > 500ms（end_silence_ms）**，
每段都因为太短被丢掉；等真的松手时，端点已经空了。

用户的日志正好是这个形态 —— 有一段 `0.00320` 的静音夹在中间：

```
19:14:42,618  energy=0.07404  语音
19:14:42,927  energy=0.00320  静音   <- 停顿
19:14:43,268  energy=0.14108  语音
```

## 这个脚本要查什么

把那一轮的**完整电平序列**打出来，并数一下：
  · 有多长是"语音"
  · 有没有超过 500ms 的静音段（会触发静音判定）
  · 静音判定发生时，累计语音时长够不够 250ms
"""

import pathlib
import re
import sys

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

LOG = ROOT / "apps" / "desktop" / "pet-data" / "main.log"

PAT = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) INFO\s+pet\.voice\s+采集电平："
    r"energy=(?P<e>[\d.]+) threshold=(?P<t>[\d.]+) (?P<kind>语音|静音) \| "
    r"语音态=(?P<state>\w+) 门态=(?P<gate>\w+) 已攒语音=(?P<acc>\d+)ms 丢弃=(?P<drop>\d+)"
)


def parse(lines: list[str]) -> list[dict]:
    out = []
    for l in lines:
        if "[brain]" in l:
            continue
        m = PAT.match(l.strip())
        if m:
            out.append(
                {
                    "ts": m.group("ts"),
                    "e": float(m.group("e")),
                    "t": float(m.group("t")),
                    "kind": m.group("kind"),
                    "state": m.group("state"),
                    "gate": m.group("gate"),
                    "acc": int(m.group("acc")),
                    "drop": int(m.group("drop")),
                }
            )
    return out


lines = LOG.read_text(encoding="utf-8", errors="replace").split("\n")
levels = parse(lines)

print(f"  解析出 {len(levels)} 条采集电平")

# 找"最后一次闸门开 -> 闸门关"那一段
starts = [i for i, l in enumerate(lines) if "采集闸门：开" in l and "[brain]" not in l]
ends = [i for i, l in enumerate(lines) if "采集闸门：关" in l and "[brain]" not in l]

if not starts:
    print("  ❌ 没有「采集闸门：开」")
    raise SystemExit(0)

# 取最后一次「开」
s_i = starts[-1]
s_ts = lines[s_i].strip()[:23]
e_i = next((i for i in ends if i > s_i), len(lines) - 1)
e_ts = lines[e_i].strip()[:23]

print()
print(f"  ═══ 最后一次按住：{s_ts} -> {e_ts} ═══")

seg = [x for x in levels if s_ts <= x["ts"] <= e_ts]
if not seg:
    print("     这段里没有电平记录")
    raise SystemExit(0)

print(f"     {len(seg)} 帧电平（约 {len(seg) * 0.33:.1f} 秒）")
print()
print(f"     {'时间':<14} {'energy':>9} {'threshold':>10}  判定  门态    已攒")
for x in seg:
    mark = "🔊" if x["kind"] == "语音" else "  "
    print(
        f"     {x['ts'][11:]:<14} {x['e']:>9.5f} {x['t']:>10.5f}  "
        f"{mark}{x['kind']}  {x['gate']:<8} {x['acc']:>5}ms"
    )

# ── 统计 ──────────────────────────────────────────────────────────────
print()
speech = [x for x in seg if x["kind"] == "语音"]
silence = [x for x in seg if x["kind"] == "静音"]
print(f"     语音帧 {len(speech)} / 静音帧 {len(silence)}")
if speech:
    print(f"     语音帧平均间隔 0.33s -> 约 {len(speech) * 0.33:.1f} 秒语音")
if seg:
    print(f"     最后累计语音时长 = {seg[-1]['acc']}ms")
    print(f"     丢弃计数 = {seg[-1]['drop']}（>0 说明有句子被 min_speech_ms 丢掉）")

print()
print("  ═══ 判读 ═══")
last = seg[-1]
if last["acc"] == 0:
    print("     ⚠️ 结束那一刻「已攒语音 = 0ms」—— 端点里是空的")
    print("        说明攒起来的句子**已经被静音判定收走并丢弃**了")
    print("        （或者中途 reset 过）")
if last["drop"] > 0:
    print(f"     🔴 **丢弃计数 = {last['drop']}** —— 有 {last['drop']} 段被当成'太短'丢掉了")
    print("        机理：说话中间停顿 > end_silence_ms(500ms) -> 静音判定收尾")
    print("              -> 那一段 speech_ms < min_speech_ms(250) -> 丢弃")
    print("              -> 松手时端点已空 -> force_end() 返回 None -> 没识别")

# 找长静音段
gaps = []
run = 0
for x in seg:
    if x["kind"] == "静音":
        run += 1
    else:
        if run >= 2:  # 2 帧 ≈ 660ms > 500ms
            gaps.append(run)
        run = 0
if run >= 2:
    gaps.append(run)
if gaps:
    print(f"     ⚠️ 检测到 {len(gaps)} 段长静音（{gaps} 帧 ≈ {[round(g * 0.33, 2) for g in gaps]} 秒）")
    print("        每段都可能触发一次静音判定")
