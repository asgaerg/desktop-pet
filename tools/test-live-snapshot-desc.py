"""真机验证：「截图对话时说一句它看到了什么」这个设置开关。

验四件事：
  ① 设置页那个开关真的存在（DOM 里能找到）
  ② 它读得出来（初始值来自 `persona.yaml`）
  ③ 打开之后，截图对话那一次回复的**末尾多出一段**「（补一句，我刚才看到的是…）」
  ④ 关掉之后，回复恢复原样（不能一直挂着）

⚠️ 会真调两次模型（看图 1~3 秒），并且会**把当前屏幕发给模型**。
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

LOG = pathlib.Path(r"F:\ai项目\apps\desktop\pet-data\main.log")
YAML = pathlib.Path(r"F:\ai项目\config\persona.yaml")


class Session:
    def __init__(self, ws_url: str) -> None:
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self.seq = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 40.0) -> dict:
        self.seq += 1
        self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = json.loads(self.ws.recv())
            if message.get("id") == self.seq:
                return message
        raise TimeoutError(method)

    def eval(self, expression: str) -> object:
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        payload = result.get("result", {})
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


def yaml_value() -> bool | None:
    text = YAML.read_text(encoding="utf-8")
    section = re.search(r"^screenshot:\s*$([\s\S]*?)(?=^\S)", text, re.M)
    if not section:
        return None
    match = re.search(r"^\s+show_description:\s*(true|false)\s*$", section.group(1), re.M)
    return bool(match) and match.group(1) == "true"


COLLECT = (
    "(function(){window.__snap=[];"
    "window.pet.on('pet:message', function(m){"
    "var t=String((m&&m.type)||'');"
    "if(t==='pet.say.end'){window.__snap.push({kind:'say', text:String(m.text||'')});}"
    "else if(t==='voice.audio'){window.__snap.push({kind:'voice', text:String(m.text||'')});}"
    "});"
    "return 'ok';})()"
)


def run_once(settings: Session, pet: Session) -> str:
    """点一次截图对话，返回它说的话（优先 say.end，没有就用 voice.audio 的文本）。"""
    menu = find("petMenu.html")
    if not menu:
        pet.eval("(function(){window.pet.openContextMenu();return 1;})()")
        time.sleep(2.0)
        menu = find("petMenu.html")
    assert menu, "菜单窗口没出现"
    pet.eval("window.__snap=[]")
    menu_session = Session(menu["webSocketDebuggerUrl"])
    menu_session.eval("window.petMenu.action({action: 'snapshot-chat'})")
    menu_session.close()

    deadline = time.time() + 70
    say = ""
    while time.time() < deadline:
        time.sleep(2.5)
        raw = pet.eval("JSON.stringify(window.__snap || [])")
        captured = json.loads(raw) if isinstance(raw, str) else []
        for item in captured:
            if item.get("kind") in ("say", "voice") and item.get("text"):
                say = item["text"]
        if say:
            break
    return say


def main() -> int:
    failures: list[str] = []

    def check(ok: bool, label: str, detail: str = "") -> None:
        print(f"    {'OK  ' if ok else 'FAIL'} {label}{(' —— ' + detail) if (detail and not ok) else ''}")
        if not ok:
            failures.append(label)

    print("=" * 78)
    print("  「它看到了什么」设置开关 · 真机验证")
    print("=" * 78)

    settings = find("settings.html")
    pet = find("index.html")
    if not settings and pet:
        # 设置窗口是**按需创建**的：没打开过就不在 CDP 目标里。
        opener = Session(pet["webSocketDebuggerUrl"])
        opener.eval("(function(){window.pet.openSettings();return 1;})()")
        opener.close()
        time.sleep(3.0)
        settings = find("settings.html")
    if not settings or not pet:
        print(f"  ❌ 找不到窗口：设置={bool(settings)} 宠物={bool(pet)}")
        return 1

    # ── ① 开关在设置页里 ──
    print()
    print("  ① 设置页里的开关")
    session = Session(settings["webSocketDebuggerUrl"])
    present = session.eval("Boolean(document.getElementById('snapshot-desc'))")
    check(bool(present), "设置页有 #snapshot-desc 这个开关")
    label = session.eval(
        "(function(){var el=document.getElementById('snapshot-desc');"
        "if(!el)return '';var row=el.closest('.setting');"
        "return row?((row.querySelector('.setting-title')||{}).textContent||'').trim():'';})()"
    )
    print(f"         标题：{label}")
    check("看到" in str(label), "开关标题说得清是干什么的")
    # 页面加载时应该已经读过一次初值
    checked = session.eval("document.getElementById('snapshot-desc').checked")
    print(f"         界面上的勾选状态：{checked}（yaml 里是 {yaml_value()}）")
    check(bool(checked) == bool(yaml_value()), "界面初值与 yaml 真源一致")

    # ── ② 打开开关（走真实 IPC）──
    print()
    print("  ② 打开开关")
    turned_on = session.eval(
        "(function(){return window.petSettings.setSnapshotDescription(true)"
        ".then(function(r){return JSON.stringify(r);});})()"
    )
    info = json.loads(turned_on) if isinstance(turned_on, str) else {}
    print(f"         IPC 返回：{info}")
    check(bool(info.get("showDescription")), "IPC 回报已打开")
    check(bool(info.get("saved")), "已落盘（saved=true）")
    check(yaml_value() is True, "persona.yaml 里真的写成了 true")
    time.sleep(1.5)

    # ── ③ 截一次，看末尾有没有那一段 ──
    print()
    print("  ③ 截图对话（开关开着）")
    pet_session = Session(pet["webSocketDebuggerUrl"])
    pet_session.eval(COLLECT)
    said_on = run_once(session, pet_session)
    print(f"         「{said_on[:220]}」")
    check("看到的是" in said_on, "回复末尾有「（补一句，我刚才看到的是…）」")
    # 必须是**另一段**（两个换行），不是接在同一句后面
    check("\n\n" in said_on, "这一部分是**另起一段**（有段落分隔）")
    tail = said_on.split("\n\n")[-1] if "\n\n" in said_on else ""
    check(tail.strip().startswith("（"), "最后一段是括号里的补充说明", tail[:60])

    # ── ④ 关掉，再截一次，必须恢复原样 ──
    print()
    print("  ④ 关掉开关再截一次")
    turned_off = session.eval(
        "(function(){return window.petSettings.setSnapshotDescription(false)"
        ".then(function(r){return JSON.stringify(r);});})()"
    )
    info_off = json.loads(turned_off) if isinstance(turned_off, str) else {}
    check(info_off.get("saved") is True, "已保存关闭状态")
    check(yaml_value() is False, "persona.yaml 里写回了 false")
    time.sleep(1.5)
    said_off = run_once(session, pet_session)
    print(f"         「{said_off[:220]}」")
    check("看到的是" not in said_off, "关掉之后不再带那一段")

    session.close()
    pet_session.close()

    print()
    print("=" * 78)
    if failures:
        print(f"  ❌ {len(failures)} 项失败：")
        for item in failures:
            print(f"     · {item}")
    else:
        print("  ✅ 全部通过：开关在设置里、落盘、开着才加、另起一段、关掉即恢复")
    print("=" * 78)
    return 1 if failures else 0


raise SystemExit(main())
