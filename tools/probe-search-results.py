#!/usr/bin/env python3
"""诊断搜索后端对指定查询的真实返回（2026-10-16 重建）。

## 背景

用户报 MCP 搜索返回「空壳」。排查结论**不是**文字长度截断（截断上限
tools 6000 字 / web 4000 字，只在超长时砍尾），真因有三：

1. 通用引擎对「二重螺旋 新版本」这类查询**分词劣化** —— 命中"二"字
   汉典/百科，相关性 0.009，8 条全被过滤诊断丢弃；
2. MCP smartsearch 默认 `fetch_top_n=0`，**只给摘要不抓正文**；
3. B 站等 JS 站 web_fetch 抓回来就是空壳（浏览器兜底可解但默认关）。

另外项目里有专用 `bilibili_search` 工具（tools.py -> video.search_bilibili
站内接口），搜 B 站内容应该走它而不是 web_search。

## ⚠️ 直连纪律

环境里可能残留 VPN 客户端的 `HTTPS_PROXY`（127.0.0.1:xxxx）—— 代理一挂，
urllib 全灭，看起来像"引擎都坏了"。**第一行代码就装直连 opener**。
用户要求：下载可以走 VPN，搜索测试必须国内直连。

## 用法

    python tools/probe-search-results.py [查询1 查询2 ...] [--mcp] [--free] [--limit N]

    默认查询：b站 热门视频 / 二重螺旋 新版本（当时的两个故障样本）
    --mcp    追加跑 websearch-mcp 后端（要它先在跑，不在跑会自动拉起）
    --free   追加跑 free-search 后端（自动拉起，10~20 秒）
    --limit  自抓引擎的结果条数（默认 8）

输出每个后端：耗时 / 条数 / query_ignored / 前几条标题+URL，
相关性一眼可见 —— "全是字典页"这种静默失败靠它现形。
"""

from __future__ import annotations

# 🔴 必须在 import 任何会发请求的模块之前装直连 opener
import urllib.request
from urllib.request import ProxyHandler, build_opener

urllib.request.install_opener(build_opener(ProxyHandler({})))  # noqa: E402

import importlib.util  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PET = ROOT / "apps" / "brain" / "pet"

DEFAULT_QUERIES = ["b站 热门视频", "二重螺旋 新版本"]


def load_standalone(name: str, path: Path):
    """独立加载 pet/ 下的模块 —— 绕开 pet/__init__（它会拉起 gateway 全链）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def show_self_scrape(query: str, limit: int) -> None:
    web = load_standalone("pet_web_probe", PET / "web.py")
    t0 = time.time()
    outcome = web.search(query, limit=limit)
    elapsed = time.time() - t0
    print(f"\n### 自抓 HTML（{outcome.engine or '无引擎命中'}）  {elapsed:.1f}s")
    print(f"    条数={len(outcome.hits)}  query_ignored={outcome.query_ignored}")
    if outcome.attempts:
        for a in outcome.attempts:
            print(f"    [尝试] {a}")
    for i, hit in enumerate(outcome.hits[:6], 1):
        print(f"    {i}. {hit.title[:48]}")
        print(f"       {hit.url[:90]}")
        if hit.snippet:
            print(f"       {hit.snippet[:80]}")


def show_mcp(query: str, backend: str) -> None:
    mcp = load_standalone(f"pet_mcp_probe_{backend}", PET / "mcp_search.py")
    fn = mcp.search_free if backend == "free" else mcp.search
    label = "free-search（精准）" if backend == "free" else "websearch-mcp（快速）"
    t0 = time.time()
    try:
        text = fn(query)
    except Exception as exc:  # noqa: BLE001
        print(f"\n### {label}  失败：{str(exc)[:160]}")
        return
    elapsed = time.time() - t0
    one_line = " ".join(text.split())
    print(f"\n### {label}  {elapsed:.1f}s  {len(text)} 字")
    print(f"    头 400 字：{one_line[:400]}")


def main(argv: list[str]) -> int:
    flags = {a for a in argv if a.startswith("--")}
    args = [a for a in argv if not a.startswith("--")]
    limit = int(args[-1]) if args and args[-1].isdigit() else 8
    queries = [a for a in args if not a.isdigit()] or DEFAULT_QUERIES

    for q in queries:
        print(f"\n{'=' * 72}\n查询：{q}\n{'=' * 72}")
        show_self_scrape(q, limit)
        if "--mcp" in flags:
            show_mcp(q, "fast")
        if "--free" in flags:
            show_mcp(q, "free")
    print("\n提示：B 站内容请用 bilibili_search 工具（站内接口，带播放量），web_search 只作兜底。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
