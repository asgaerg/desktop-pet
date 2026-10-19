"""模块注册表（模块化设计 阶段一，2026-10-18）。

## 这一层只做一件事：**开关**

用户原话（转述设计文档）：「将后端的功能整合成一个个可换下接入的模块设计」。
阶段一的底线是：**不拆业务代码，只让"关掉 = 不启动 / 不订阅"**，
而且关掉必须能解释原因（日志 + 界面），不许静默。

## 真源：`persona.yaml` 的 `modules:` 段

设计文档原本写的是独立文件 `config/modules.yaml`。实现时改成 persona.yaml 里的一段，
理由只有一个：**避免第二个配置文件产生双份真相**（这个项目已经因为"两份实现/两份配置"
栽过多次）。配置仍然是"一处改、全线生效"。

```yaml
modules:
  voice: {enabled: true}
  screen: {enabled: false}
  search: {enabled: true}
```

也支持简写：

```yaml
modules:
  voice: true
```

## 依赖

`requires` 表示"关掉它，依赖它的模块也必须一起关"（比如屏幕关了，截图对话就没了）。
解析成**闭包**而不是逐项判断，避免"上层关了、下层还在跑"的半开状态。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger("pet.modules")


@dataclass(frozen=True)
class Module:
    """一个可切换的模块。"""

    name: str
    label: str
    #: 缺配置时的默认值（一律开 —— 升级不能静默关掉用户已有的功能）
    default: bool = True
    #: 依赖的其它模块（被依赖者关掉时，本模块也一起关）
    requires: tuple[str, ...] = ()
    #: 阶段一能不能真正"关掉"（否则只是登记，不生效，必须如实说明）
    gate: bool = True
    note: str = ""


#: 阶段一登记表。**加模块只改这里** + persona.yaml 的 modules 段。
MODULES: tuple[Module, ...] = (
    Module("memory", "记忆", default=True, gate=False,
           note="地基：关了对话就没有历史，阶段一只登记"),
    Module("persona", "提示词/人设", default=True, gate=False,
           note="地基：关了宠物就没有性格"),
    Module("chat", "聊天", default=True, gate=False,
           note="地基：关了没有主循环"),
    Module("subjectivity", "主观层", default=True, gate=False,
           note="情绪/印象，阶段一只登记"),
    Module("voice", "语音", default=True,
           note="关掉则不建 ASR/TTS 引擎（顺带省掉本地大模型）"),
    Module("autonomy", "自主行为", default=True,
           note="自主发言、随机事件、自主截图三个循环的启动闸门"),
    Module("monologue", "自言自语", default=True),
    Module("screen", "看屏幕", default=True,
           note="关掉则停止视觉/截图（与权限开关是 AND 关系）"),
    Module("search", "联网搜索", default=True,
           note="关掉则联网一律不调（叠加菜单里的三档模式）"),
    Module("sprite", "立绘动画", default=True,
           note="关掉只隐藏立绘（程序还在跑，开回来立刻可见）"),
    Module("ui.windows", "窗口/气泡", default=True,
           note="关掉气泡窗口不再弹出"),
)

_BY_NAME = {m.name: m for m in MODULES}


class ModuleRegistry:
    """从配置读开关，算依赖闭包，给出"关了为什么"。"""

    def __init__(self, config: Any) -> None:
        self.config = config
        raw = {}
        try:
            raw = dict(config.root.get("modules", {}) or {})
        except Exception:  # noqa: BLE001 —— 配置坏了也要能跑
            log.warning("modules 段读不了，全部按默认值", exc_info=True)
        self._want: dict[str, bool] = {}
        for mod in MODULES:
            entry = raw.get(mod.name)
            if isinstance(entry, dict):
                value = bool(entry.get("enabled", mod.default))
            elif entry is None:
                value = mod.default
            elif isinstance(entry, str):
                # ⚠️ 项目自带的 YAML 解析器**不支持流式映射**：`{enabled: false}`
                # 会被读成字符串 `"{enabled: false}"`，而 bool(那个) 恒为 True ——
                # 表现是"配置里明明写了 false，模块却还开着"。
                # 这里对字符串兜底，至少能把 false 认出来。
                value = "false" not in entry.strip().lower()
            else:
                value = bool(entry)
            self._want[mod.name] = value

    # ------------------------------------------------------------ 判定

    def enabled(self, name: str) -> bool:
        """模块是否生效（**已算上依赖闭包**）。"""
        return self._effective().get(name, True)

    def reason(self, name: str) -> Optional[str]:
        """关着时给出人话原因；开着返回 None。"""
        if self.enabled(name):
            return None
        mod = _BY_NAME.get(name)
        if not self._want.get(name, True):
            return f"{name} 在配置里关掉了"
        for req in (mod.requires if mod else ()):
            if not self.enabled(req):
                return f"依赖的 {req} 关掉了"
        return f"{name} 关掉了"

    def set_enabled(self, name: str, value: bool) -> None:
        """只改内存（落盘由设置页/主进程负责）。"""
        if name not in _BY_NAME:
            raise KeyError(f"没有这个模块：{name}")
        self._want[name] = bool(value)
        log.info("模块 %s -> %s", name, "开" if value else "关")

    def _effective(self) -> dict[str, bool]:
        """依赖闭包：被依赖者关掉 -> 本模块也关（递归到稳定）。"""
        eff = dict(self._want)
        changed = True
        while changed:
            changed = False
            for mod in MODULES:
                if eff.get(mod.name) and any(not eff.get(r, True) for r in mod.requires):
                    eff[mod.name] = False
                    changed = True
        return eff

    # ------------------------------------------------------------ 报告

    def state(self) -> list[dict[str, Any]]:
        """给日志 / 协议 / 设置页用。"""
        out: list[dict[str, Any]] = []
        for mod in MODULES:
            out.append(
                {
                    "name": mod.name,
                    "label": mod.label,
                    "enabled": self.enabled(mod.name),
                    "gate": mod.gate,
                    "reason": self.reason(mod.name),
                    "note": mod.note,
                }
            )
        return out

    def log_summary(self) -> None:
        off = [m["name"] for m in self.state() if not m["enabled"]]
        log.info(
            "模块：共 %d 个，关闭 %d 个%s",
            len(MODULES),
            len(off),
            f"（{'、'.join(off)}）" if off else "",
        )
