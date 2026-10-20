"""DeepSeek 官方网页搜索（2026-10-19 接入）。

## 它是什么

DeepSeek 自 2026-10-17 起开放了 Search / Fetch 两个 REST 接口：

```
POST https://api.deepseek.com/v1/tools/search
POST https://api.deepseek.com/v1/tools/fetch
```

本文档只接 **Search**（Fetch 已经有 `pet/web.py` 的抓取，够用）。

请求体：`{query, count(1..50), ori_question, localization, need_content}`
响应：`{results: [{title, url, snippet, summary, content, site_name, date, authority, rerank_score}]}`

- `snippet` 短摘要（100~200 字），`summary` 长摘要（600~800 字）—— 给模型读后者的信息量大得多
- `need_content=true` 才返回网页正文；我们**不用**（正文动辄几千字，烧 token）
- 计费：**按请求次数**，每 1000 次成功请求 5 元；失败/空结果不计费
- 限速：每账号 100 QPS（远超我们需要的）

## ⚠️ 三个必须绕开的坑

### ① 系统代理会让它慢 30 倍（实测）

这台机器的 Windows 系统代理是 `127.0.0.1:10809`（V2Ray），而 `urllib` 在
Windows 上会**从注册表读系统代理** —— 于是发给 `api.deepseek.com` 的请求
被绕到境外节点再回来：

```
走系统代理     10.7 ~ 17.7 秒
直连（绕代理）  0.31 ~ 0.39 秒
```

所以默认**绕开系统代理**（`ProxyHandler({})`）。DeepSeek 的域名在国内，
不需要代理；反而走代理会烂。配置项 `tools.deepseek_search_use_system_proxy`
留给"人在海外/公司强制代理"这种特殊情况。

### ② 401 和 402 的处置完全不同

```
401  密钥无效       -> 用户要重填密钥，别重试
402  余额不足       -> 用户要充值，别重试
429  限速           -> 等一会儿可以重试
5xx  服务端问题     -> 可以重试
```

失败消息里**必须写清是哪一种**，否则上层只会把它当成"搜索坏了"然后
悄悄退回本地搜索 —— 用户永远不知道自己的密钥/余额有问题。

### ③ 空结果不是错误

搜索正常结束但没命中时返回 `results: []`（HTTP 200）。这和"网络失败"
是两回事：前者要告诉模型"确实没搜到"，后者要退回本地后端。
所以本模块**不把空结果当异常**，返回一个 `hits=[]` 的 outcome 让上层决定。
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from typing import Any

from . import netutil, web

DEFAULT_URL = "https://api.deepseek.com/v1/tools/search"

# 短摘要给模型读时截断到这个长度：3~6 条 x 300 字 ≈ 900~1800 字，token 预算可控。
# DeepSeek 的 summary 是 600~800 字，全塞进去会挤占对话上下文。
SNIPPET_CHARS = 300


class DeepSeekSearchError(Exception):
    """搜索失败。`message` 里是**给用户看的**原因（含 401/402 这种可行动的提示）。"""


def available(api_key: str) -> bool:
    """有没有密钥 —— 没有就不用这个后端（"仅 DeepSeek API 可用"就是这条）。"""
    return bool(str(api_key or "").strip())


def build_body(query: str, count: int = 6) -> dict[str, Any]:
    """拼请求体（拆出来是为了测试：不用联网就能钉住字段名和范围）。"""
    q = str(query or "").strip()
    if not q:
        raise DeepSeekSearchError("没有给搜索词")
    n = max(1, min(10, int(count)))
    return {
        "query": q[:128],
        "count": n,
        # 短摘要 + 长摘要都要（snippet 给得短，summary 给模型读）
        "need_content": False,
    }


def _squash(text: str) -> str:
    """把多行摘要压成一行（模型读一行比读十来行省 token，也更不容易误解换行）。"""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def parse_results(payload: dict[str, Any]) -> list[web.SearchHit]:
    """把官方响应变成我们自己的 `SearchHit` 列表（纯函数，可注入测试）。

    ⚠️ 摘要优先用 `summary`（长摘要，信息量大），没有才退 `snippet`。
    实测 summary 里常常直接带着答案（日期、来源、事件经过），
    而 `snippet` 只是被截断的开头一句。
    """
    hits: list[web.SearchHit] = []
    for item in (payload or {}).get("results") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = _squash(item.get("title") or "") or url
        if not url:
            continue
        text = _squash(item.get("summary") or "") or _squash(item.get("snippet") or "")
        if len(text) > SNIPPET_CHARS:
            text = text[:SNIPPET_CHARS].rstrip() + "…"
        hits.append(web.SearchHit(title=title[:120], url=url, snippet=text))
    return hits


def describe_http_error(code: int, detail: str = "") -> str:
    """把 HTTP 状态码翻成**能行动**的中文。"""
    if code == 401:
        return "DeepSeek API 密钥无效（401）—— 去设置页重填密钥"
    if code == 402:
        return "DeepSeek 账户余额不足（402）—— 需要充值才能继续搜索"
    if code == 429:
        return "DeepSeek 搜索被限速（429）—— 稍后会自动重试别的后端"
    if code == 403:
        return "DeepSeek 拒绝了这次请求（403）"
    if code >= 500:
        return f"DeepSeek 服务端出错（{code}）"
    extra = f"：{detail[:80]}" if detail else ""
    return f"DeepSeek 搜索失败（HTTP {code}）{extra}"


def _post_json(
    url: str,
    body: dict[str, Any],
    api_key: str,
    timeout_s: float,
    use_system_proxy: bool,
) -> dict[str, Any]:
    """发一次 POST。失败抛 `DeepSeekSearchError`（消息已翻成人话）。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    # 走 `netutil`（2026-10-19）：它按域名决定要不要代理，默认名单里就有
    # `api.deepseek.com` —— 所以这里不用自己判断。`use_system_proxy=True`
    # 是"强制走代理"的逃生门（配置项给了这个可能）。
    try:
        with netutil.urlopen(
            request, timeout=timeout_s, force_direct=not use_system_proxy
        ) as response:
            raw = response.read()
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        raise DeepSeekSearchError(describe_http_error(int(exc.code), detail)) from exc
    except urllib.error.URLError as exc:
        raise DeepSeekSearchError(f"连不上 DeepSeek 搜索服务：{exc.reason}") from exc
    except TimeoutError as exc:
        raise DeepSeekSearchError("DeepSeek 搜索超时") from exc

    if status != 200:
        raise DeepSeekSearchError(describe_http_error(int(status)))
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise DeepSeekSearchError("DeepSeek 返回的不是 JSON") from exc
    if not isinstance(payload, dict):
        raise DeepSeekSearchError("DeepSeek 返回结构不对（顶层不是对象）")
    return payload


def search(
    query: str,
    limit: int = 6,
    *,
    api_key: str,
    url: str = DEFAULT_URL,
    timeout_s: float = 12.0,
    use_system_proxy: bool = False,
) -> web.SearchOutcome:
    """搜一次，返回 `web.SearchOutcome`（和 `web.search` 同构，上层格式化只有一份）。

    ⚠️ **同步阻塞**（urllib）—— 调用方必须 `asyncio.to_thread`，
    否则会卡死整个 sidecar（这条规矩 `web.py` / `mcp_search.py` 都踩过）。
    """
    if not available(api_key):
        raise DeepSeekSearchError("没有 DeepSeek API 密钥")

    body = build_body(query, limit)
    payload = _post_json(
        str(url or DEFAULT_URL), body, api_key, timeout_s, use_system_proxy
    )
    hits = parse_results(payload)
    if not hits:
        return web.SearchOutcome(
            hits=[],
            engine="deepseek",
            attempts=["DeepSeek 搜索没有返回结果（可能是查询太冷门）"],
        )
    return web.SearchOutcome(hits=hits, engine="deepseek")


# 让 `python -m pet.deepseek_search 查询词` 能直接试（排查时不用起整个 App）。
def _main(argv: list[str]) -> int:
    if not argv:
        print("用法：python -m pet.deepseek_search 搜索词 [条数]")
        return 2
    from .config import Config

    cfg = Config.load()
    outcome = search(argv[0], int(argv[1]) if len(argv) > 1 else 5, api_key=cfg.api_key_for("deepseek"))
    print(f"  引擎={outcome.engine} 结果={len(outcome.hits)} 条")
    for i, hit in enumerate(outcome.hits, 1):
        print(f"  {i}. {hit.title}")
        print(f"     {hit.url}")
        print(f"     {hit.snippet[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
