"""真机端到端验「自言自语 -> 20 分钟回落安静」这条链。

链路（每一环都要看到证据，不看"应该会"）：

    菜单/设置 设 activity=1.0
        -> sidecar 日志「进入「自言自语」」
        -> 调度器每秒 tick 一次；到期 check_tier_decay 返回回落描述
        -> 总线 autonomy.tier_decayed -> 网关广播 activity.tier
        -> 主进程 setActivity(0.1)：日志「到期回落 -> 安静」
        -> 再打开菜单，「活泼度」显示「安静」、勾选在「安静」上

⚠️ 20 分钟没法现场等，所以把 `activity.monologue_decay_s` 临时改成 8 秒
   （这正是那个键存在的第二个理由：链路不缩短就等于没验）。跑完恢复 1200。
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

import websocket  # noqa: E402

ROOT = pathlib.Path(r"F:\ai项目")
LOG = ROOT / "apps" / "desktop" / "pet-data" / "main.log"
YAML = ROOT / "config" / "persona.yaml"


class Session:
    def __init__(self, url: str) -> None:
        self.ws = websocket.create_connection(url, timeout=30, suppress_origin=True)
        self.seq = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self.seq += 1
        self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == self.seq:
                return message

    def eval(self, expression: str) -> object:
        payload = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        ).get("result", {})
        if "exceptionDetails" in payload:
            raise RuntimeError(str(payload["exceptionDetails"])[:400])
        return payload.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def targets() -> list[dict]:
    with urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def find(*needles: str) -> dict | None:
    for target in targets():
        url = str(target.get("url") or "")
        if target.get("type") == "page" and any(n in url for n in needles):
            return target
    return None


def log_size() -> int:
    try:
        return LOG.stat().st_size
    except OSError:
        return 0


def log_since(offset: int) -> str:
    try:
        with LOG.open("rb") as handle:
            handle.seek(offset)
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def menu_tiers(pet: Session) -> dict:
    """打开菜单，读出「活泼度」当前档位名 + 四档各自的勾选状态。"""
    pet.eval("(function(){window.pet.openContextMenu();return 1;})()")
    time.sleep(2.0)
    menu = find("petMenu.html")
    if not menu:
        return {"error": "菜单窗口没出现"}
    session = Session(menu["webSocketDebuggerUrl"])
    raw = session.eval(
        """(function(){
          var rows = Array.from(document.querySelectorAll('[role=menuitem]'));
          var parent = rows.filter(function(r){ return (r.__item||{}).label === '活泼度'; })[0];
          var tiers = rows.filter(function(r){
            var a = String((r.__item||{}).action || '');
            return a.indexOf('activity:') === 0;
          }).map(function(r){ return {label: (r.__item||{}).label, checked: !!(r.__item||{}).checked}; });
          return JSON.stringify({
            value: parent ? (parent.__item||{}).value : null,
            tiers: tiers
          });
        })()"""
    )
    session.close()
    return json.loads(raw) if isinstance(raw, str) else {}


def set_activity(pet: Session, value: float) -> None:
    pet.eval(f"(function(){{window.pet.setActivity({value});return 1;}})()")


def main() -> int:
    failures: list[str] = []

    def check(ok: bool, label: str, detail: str = "") -> None:
        print(f"    {'OK  ' if ok else 'FAIL'} {label}{(' —— ' + detail) if (detail and not ok) else ''}")
        if not ok:
            failures.append(label)

    print("=" * 78)
    print("  自言自语 -> 自动回落（真机端到端）")
    print("=" * 78)

    pet_target = find("index.html")
    if not pet_target:
        print("  ❌ 找不到宠物窗口（应用没起来？）")
        return 1
    pet = Session(pet_target["webSocketDebuggerUrl"])

    # ── ① 读当前回落时长（剧本：调用方先把它调短 + 重启，本脚本只读） ──
    print()
    print("  ① 读配置里的回落时长")
    text = YAML.read_text(encoding="utf-8")
    original = re.search(r"^(\s*monologue_decay_s:\s*)(\d+)\s*$", text, re.M)
    assert original, "yaml 里找不到 monologue_decay_s"
    # ⚠️ 侧车只在**启动时**读 yaml，所以这里不能自己改文件 ——
    # 改完不重启，验的还是旧的 1200 秒（真等 20 分钟）。调用方负责：
    #   改 yaml -> 重启 -> 跑本脚本 -> 改回 -> 再重启
    decay_s = int(original.group(2))
    print(f"         monologue_decay_s = {decay_s} 秒")

    # ── ② 设成「自言自语」 ──
    print()
    print("  ② 切到「自言自语」")
    before = log_size()
    set_activity(pet, 1.0)
    time.sleep(6.0)  # tick 5 秒一轮，等它至少进一次
    entry = log_since(before)
    check("进入「自言自语」" in entry or "进入「自言自语」" in log_since(max(0, before - 20000)),
          "sidecar 认定进入了「自言自语」", entry[-200:])
    shown = menu_tiers(pet)
    print(f"         菜单现在显示：活泼度={shown.get('value')!r}")
    check(shown.get("value") == "自言自语", "菜单「活泼度」显示「自言自语」", str(shown))

    # ── ③ 等它自己回落 ──
    print()
    print(f"  ③ 等 {decay_s} 秒档位到期（调度器 5 秒一轮，最多等 {decay_s + 40} 秒）")
    mark = log_size()
    decayed_line = ""
    deadline = time.time() + decay_s + 40
    while time.time() < deadline:
        time.sleep(3)
        chunk = log_since(mark)
        if "回落" in chunk:
            decayed_line = chunk
            break
    check(bool(decayed_line), "日志里出现了回落", decayed_line[-300:])
    for line in decayed_line.splitlines():
        if "回落" in line:
            print(f"         {line.strip()[:150]}")

    # ── ④ 菜单跟着变 ──
    print()
    print("  ④ 菜单应该跟着变到「安静」")
    after = menu_tiers(pet)
    print(f"         菜单现在显示：活泼度={after.get('value')!r}")
    tiers = after.get("tiers") or []
    checked = [t["label"] for t in tiers if t.get("checked")]
    print(f"         勾选在：{checked}")
    check(after.get("value") == "安静", "菜单「活泼度」显示「安静」", str(after))
    check(any(c.startswith("安静") for c in checked), "单选项勾在「安静」上", str(checked))
    pet.close()

    print()
    print("=" * 78)
    if failures:
        print(f"  ❌ {len(failures)} 项失败：")
        for item in failures:
            print(f"     · {item}")
    else:
        print("  ✅ 全链路通过：进入档位 -> 计时 -> 回落 -> 广播 -> 菜单跟着变")
    print("=" * 78)
    return 1 if failures else 0


raise SystemExit(main())
