"""按域名决定要不要走系统代理（2026-10-19）。

## 为什么需要它

Windows 上 `urllib` 会**从注册表读系统代理**（"Internet 选项"里的设置）。
用户开着 V2Ray 时，本机所有 Python 请求都会被绕到代理上 —— 包括发往
**国内**大模型接口的请求。实测（同一台机器、同一个 query、都返回 200）：

```
                     走系统代理      直连
DeepSeek 搜索         10.7~17.7 秒   0.31~0.39 秒
DeepSeek 对话（chat） 11.13 / 10.92  0.86 / 0.59 秒
```

也就是**每回一句话白等 10 秒**。而关掉 VPN 时系统代理又没了 —— 于是这个
"要不要走代理"的问题变成"看你今天开没开 VPN"，不该由用户承担。

## 解决方式：按**域名**分流，而不是全局开关

```
命中直连名单（国内模型接口） -> ProxyHandler({})   无视系统代理 + 环境变量
其它（OpenAI / 任意网页）     -> 默认 opener       照旧走系统代理
```

这样：开 VPN 时对话照样快（国内域名直连），需要翻墙的请求（OpenAI）照旧走代理。

## 名单怎么配

默认 `api.deepseek.com` / `api.siliconflow.cn`（本项目用的两家）。
`persona.yaml` 的 `net.direct_hosts` 可以覆盖；空列表 = 全部走代理（等于关掉本特性）。

⚠️ 诊断用：环境变量 `PET_FORCE_SYSTEM_PROXY=1` 会强制**全部走代理**
（怀疑"直连反而有问题"时，用它对照，不用改配置文件）。
"""

from __future__ import annotations

import os
import urllib.parse
import urllib.request
from typing import Iterable

DEFAULT_DIRECT_HOSTS: tuple[str, ...] = ("api.deepseek.com", "api.siliconflow.cn")

_direct_hosts: tuple[str, ...] = DEFAULT_DIRECT_HOSTS


def set_direct_hosts(hosts: Iterable[str] | None) -> None:
    """设置直连名单（网关在配置变更时调）。

    `None` = 回到默认名单（配置里没写 `net.direct_hosts` 时的行为）。
    """
    global _direct_hosts
    if hosts is None:
        _direct_hosts = DEFAULT_DIRECT_HOSTS
        return
    cleaned = tuple(
        str(h).strip().lower().lstrip(".") for h in hosts if str(h).strip()
    )
    _direct_hosts = cleaned


def direct_hosts() -> tuple[str, ...]:
    """给日志/诊断用。"""
    return _direct_hosts


def host_of(url: str) -> str:
    """取 URL 的 host（解析失败返回空串，不抛）。"""
    try:
        return (urllib.parse.urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return ""


def _force_system_proxy() -> bool:
    return os.environ.get("PET_FORCE_SYSTEM_PROXY", "").strip() in ("1", "true", "yes", "on")


def should_bypass(url: str) -> bool:
    """这个 URL 该不该**绕开**系统代理。"""
    if _force_system_proxy():
        return False
    host = host_of(url)
    if not host:
        return False
    for name in _direct_hosts:
        # 相等或**子域**（`beta.api.deepseek.com` 也算）——
        # ⚠️ 用 `.` 前缀匹配，不能用 `endswith(name)`：
        # 那样 `evil-deepseek.com` 也会命中，等于把名单变成通配符。
        if host == name or host.endswith("." + name):
            return True
    return False


def opener_for(url: str, *, force_direct: bool | None = None) -> urllib.request.OpenerDirector:
    """返回适合这个 URL 的 opener。

    `force_direct` 显式指定时绕过域名判断（调用方自己知道要不要代理）。
    """
    direct = should_bypass(url) if force_direct is None else bool(force_direct)
    if direct:
        # 空 ProxyHandler = 连环境变量里的代理也不用（urllib 的默认行为会被覆盖）
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def urlopen(
    request: urllib.request.Request | str,
    *,
    timeout: float,
    force_direct: bool | None = None,
):
    """`urllib.request.urlopen` 的替代品：按域名选 opener。

    用法和 `urlopen` 完全一致（可直接 `with ... as response:`）。
    ⚠️ 项目里**所有发往模型/搜索服务的请求都该走这里** ——
    直接用 `urllib.request.urlopen` 会重新掉进系统代理的坑。
    """
    url = getattr(request, "full_url", None) or str(request)
    return opener_for(url, force_direct=force_direct).open(request, timeout=timeout)
