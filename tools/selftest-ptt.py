#!/usr/bin/env python
"""
**一条命令跑完**「按住说话」全链路自检，并直接给结论。

## 为什么要做成一条命令（2026-10-08）

排查过程中让用户做了太多手工步骤：启动应用、按特定时机按键、贴日志……
而且我给的那些 `.ps1` 诊断脚本在这台机器上**被执行策略拦住**
（`LocalMachine = RemoteSigned` 却报"未签名"，见交接文档第四十九节）。

所以收敛成一个 Python 脚本：**它自己启动应用、自己发键、自己查日志、自己判读**。
用户只需要跑一条命令。

## 它做什么

  ① 杀掉旧实例，确认从干净状态开始
  ② 启动应用，等 `监视器就绪（READY 165）`
  ③ 记录三处日志的基线（主进程 / sidecar）
  ④ 用 `keybd_event` 发 2 次右 Alt（**带扫描码 0x38**，不带的话监视器采不到）
  ⑤ 等几秒，把三处日志的**新增行**打出来
  ⑥ 判读：断在哪一层

## 用法

    python tools/selftest-ptt.py
"""

from __future__ import annotations

import argparse
import ctypes
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

DESKTOP = ROOT / "apps" / "desktop"
STARTUP_LOG = DESKTOP / "dist" / "startup.log"
SIDECAR_LOG = DESKTOP / "pet-data" / "python.log"
ELECTRON = ROOT / "node_modules" / ".pnpm" / "electron@32.3.3" / "node_modules" / "electron" / "dist" / "electron.exe"

USER32 = ctypes.WinDLL("user32", use_last_error=True)
EXT_KEY = 0x0001
KEY_UP = 0x0002
VK_RIGHT_ALT = 0xA5
SCAN_RIGHT_ALT = 0x38


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def new_lines(before: str, after: str) -> list[str]:
    """只取新增的行（按长度差切 —— 日志是纯追加的）。"""
    if len(after) <= len(before):
        return []
    return [ln for ln in after[len(before) :].splitlines() if ln.strip()]


def kill_existing() -> int:
    """杀掉 electron 与 probe-key 子进程。返回杀掉的个数。"""
    killed = 0
    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Process electron -ErrorAction SilentlyContinue | "
                "ForEach-Object { $_.Id }",
            ],
            capture_output=True,
            text=True,
            timeout=25,
        )
        pids = [p.strip() for p in (out.stdout or "").split() if p.strip().isdigit()]
        for pid in pids:
            subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, timeout=15)
            killed += 1
    except Exception:  # noqa: BLE001
        pass
    time.sleep(3)
    return killed


def launch_app() -> None:
    """启动应用（照抄正常启动方式）。"""
    import os

    env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
    env["PET_FULLSCREEN_WATCH"] = "0"
    subprocess.Popen(
        [str(ELECTRON), ".", "--dev", "--no-sandbox", "--debug-port=9222"],
        cwd=str(DESKTOP),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


def count_process(name: str) -> int:
    """数同名进程（用 PowerShell 查，避免依赖 psutil）。"""
    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-Process -Name '{name.replace('.exe', '')}' -ErrorAction SilentlyContinue | "
                "Measure-Object).Count",
            ],
            capture_output=True,
            text=True,
            timeout=25,
        )
        return int((out.stdout or "0").strip() or 0)
    except Exception:  # noqa: BLE001
        return -1


def count_monitor() -> int:
    """
    数 `probe-key.ps1` 监视器进程。

    ⚠️ 必须按 **`-File` 后面跟 probe-key.ps1** 匹配，
    不能只匹配 `probe-key` 字样 —— 否则**本脚本自己的命令行**也会被数进去
    （踩过：得到"2 个监视器"的假结论）。
    """
    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_Process -Filter \"Name = 'powershell.exe'\" | "
                "Where-Object { $_.CommandLine -match '-File.*probe-key\\.ps1' } | "
                "Measure-Object).Count",
            ],
            capture_output=True,
            text=True,
            timeout=25,
        )
        return int((out.stdout or "0").strip() or 0)
    except Exception:  # noqa: BLE001
        return -1


def wait_for_ready(timeout: float = 40.0) -> bool:
    """等 `监视器就绪（READY 165）` 出现在主进程日志里。"""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if "监视器就绪" in read(STARTUP_LOG):
            return True
        time.sleep(0.5)
    return False


def press_right_alt(hold_ms: int = 700) -> None:
    """**必须带扫描码**（0x38）—— 不带的话监视器采不到，而且不报错。"""
    USER32.keybd_event(VK_RIGHT_ALT, SCAN_RIGHT_ALT, EXT_KEY, 0)
    time.sleep(hold_ms / 1000.0)
    USER32.keybd_event(VK_RIGHT_ALT, SCAN_RIGHT_ALT, EXT_KEY | KEY_UP, 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--times", type=int, default=2, help="发几次")
    ap.add_argument("--hold", type=int, default=700, help="每次按住多少毫秒")
    ap.add_argument("--no-restart", action="store_true", help="不重启应用（用现有实例）")
    args = ap.parse_args()

    print("=" * 76)
    print(" 按住说话 · 全链路自检")
    print("=" * 76)
    print()
    print(f"  应用: {ELECTRON}")
    print(f"  存在: {ELECTRON.is_file()}")
    print()

    if not ELECTRON.is_file():
        print("  ❌ 找不到 electron.exe —— 路径不对")
        return 2

    if not args.no_restart:
        print("  ① 清理旧实例…")
        n = kill_existing()
        print(f"     杀掉 {n} 个 electron 进程")

        # ⚠️ **必须清空旧日志**（2026-10-08 踩过）：
        # 第一次跑这个自检时没清，于是 `wait_for_ready()` 读到的是**上一次运行**
        # 留下的 `监视器就绪`，误以为应用起来了 —— 后面所有判读都建立在
        # "日志是这次的"这个错误前提上。
        for f in (STARTUP_LOG,):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass

        print("  ② 启动应用（等监视器就绪，最多 40 秒）…")
        launch_app()
        if not wait_for_ready():
            print("     ❌ 等不到「监视器就绪」—— 应用没起来，或按住说话没配置")
            tail = [ln for ln in read(STARTUP_LOG).splitlines() if ln.strip()][-6:]
            for ln in tail:
                print(f"        {ln.strip()[:104]}")
            return 1
        print("     ✅ 就绪")

        # 确认应用与监视器**真的在跑**（不只是日志里有那行）
        elec = count_process("electron.exe")
        mon = count_monitor()
        print(f"     electron 进程 {elec} 个 · probe-key 监视器 {mon} 个")
        if elec == 0:
            print("     🔴 应用进程不在！启动失败（看上面日志尾部）")
            return 1
        if mon == 0:
            print("     🔴 监视器进程不在！按住说话没能拉起子进程")
            return 1
    else:
        print("  ①② 跳过（--no-restart）")
        if "监视器就绪" not in read(STARTUP_LOG):
            print("     ⚠️ 但日志里没有「监视器就绪」—— 应用可能没在跑")
    print()

    # 基线
    base_startup = read(STARTUP_LOG)
    base_sidecar = read(SIDECAR_LOG)

    # 发键 —— 用 PowerShell 起一个**独立的**发键进程（不占本进程）
    print(f"  ③ 发 {args.times} 次右 Alt（按住 {args.hold}ms，带扫描码 0x38）")
    for i in range(1, args.times + 1):
        press_right_alt(args.hold)
        print(f"     第 {i} 次 @ {time.strftime('%H:%M:%S')}")
        time.sleep(1.0)

    print("  ④ 等 6 秒让识别跑完…")
    time.sleep(6.0)

    # 取新增
    got_press = new_lines(base_startup, read(STARTUP_LOG))
    got_sidecar = new_lines(base_sidecar, read(SIDECAR_LOG))

    print()
    print("  === 主进程日志新增 ===")
    keys = ("监视器输出", "按住说话]", "[语音]")
    shown = [ln.strip() for ln in got_press if any(k in ln for k in keys)]
    print("    （无）" if not shown else "")
    for ln in shown:
        print(f"    {ln[:110]}")

    print()
    print("  === sidecar 日志新增 ===")
    skeys = ("采集闸门", "语音识别（", "以语音输入", "ASR")
    sshown = [ln.strip() for ln in got_sidecar if any(k in ln for k in skeys)]
    print("    （无）" if not sshown else "")
    for ln in sshown:
        print(f"    {ln[:110]}")

    # ---------------- 判读 ----------------
    raw_down = sum(1 for ln in got_press if "监视器输出 <- DOWN" in ln)
    pressed = sum(1 for ln in got_press if "按住说话] 按下" in ln)
    gate_open = sum(1 for ln in got_sidecar if "采集闸门 打开" in ln)
    recognized = sum(1 for ln in got_sidecar if "语音识别（" in ln and "耗时" in ln)
    to_chat = sum(1 for ln in got_sidecar if "以语音输入发起对话" in ln)

    print()
    print("  === 各层计数 ===")
    print(f"    监视器发出 DOWN        {raw_down}  /  期望 {args.times}")
    print(f"    主进程触发「按下」      {pressed}  /  期望 {args.times}")
    print(f"    sidecar 闸门打开        {gate_open}  /  期望 {args.times}")
    print(f"    sidecar 完成识别        {recognized}")
    print(f"    走到「以语音输入发起对话」{to_chat}")
    print()

    print("  === 结论 ===")
    if raw_down == 0:
        print("  🔴 **监视器一行都没输出** —— 断点在「按键 -> 监视器 stdout」")
        print("     （本脚本发键带了扫描码 0x38；若物理按键也这样，查键盘/驱动）")
        return 1
    if pressed == 0:
        print("  🔴 **监视器输出了 DOWN，但主进程没触发「按下」** ——")
        print("     断点在 handleLine（最可能是 this.down 卡在 true，去重把事件吃了）")
        return 1
    if gate_open == 0:
        print("  🔴 **主进程触发了「按下」，但 sidecar 没开闸门** ——")
        print("     断点在「渲染层 -> 主进程 -> socket」（宠物窗口可能没收到 pet:ptt）")
        return 1
    if recognized == 0:
        print("  ⚠️ 闸门开了但没识别出结果 —— 音频帧可能没送到 sidecar")
        print("     （麦克风没开/权限/设备问题；用 microphone 菜单换一支试试）")
        return 1
    print("  ✅ **全链路通过**：按下 -> 闸门 -> 识别 -> 对话")
    if to_chat:
        print(f"     实际走到了对话（{to_chat} 次）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
