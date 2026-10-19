#!/usr/bin/env python3
"""A/B 对比三个搜索后端：free-search（精准）vs websearch-mcp（快速）vs 自抓 HTML。

## 背景（2026-10-16，本次实测的原始脚本重建）

用户要评估 free-search-mcp 能否替代 websearch-mcp，且明确要求：
**下载可以开 VPN，搜索测试必须国内直连**。实测结果（国内直连）：

| 查询 | free-search | websearch-mcp | 自抓 HTML |
|---|---|---|---|
| b站 热门视频 | 10.1s / 2723 字 / B站热门页真实链接 | 1.5s / 2 条官网首页 | 20.4s / 全垃圾 |
| 二重螺旋 新版本 | 20.1s / 3229 字 / 官方 10月28日公测公告 | 1.4s / "二"字汉典 | 20.3s / "二"字字典 |

结论：命中质量 free-search 碾压，代价 10~20 秒 -> 菜单做「精准/快速/关闭」三选一。

## ⚠️ 直连纪律

环境里可能残留 VPN 客户端的 `HTTPS_PROXY`（127.0.0.1:xxxx）—— VPN 一关代理
就死，urllib 全灭。**第一行代码就装直连 opener**（2026-10-16 实测坑）。

## 用法

    python tools/probe-ab-search.py [查询1 查询2 ...] [--only free|fast|scrape] [--timeout S]

    默认查询：b站 热门视频 / 二重螺旋 新版本
    --only   只跑一个后端（free=精准 / fast=websearch-mcp / scrape=自抓）
    --timeout 单后端超时秒数（默认 45）

输出表格：后端 | 耗时 | 字符数 | 头部样本（单行化）。
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


def one_line(text: str, n: int = 150) -> str:
    return " ".join(text.split())[:n]


def run_backend(name: str, fn, query: str, timeout: float) -> tuple[float, str]:
    t0 = time.time()
    try:
        text = fn(query) if name != "自抓 HTML" else _scrape(fn, query)
    except Exception as exc:  # noqa: BLE001
        return time.time() - t0, f"❌ 失败：{str(exc)[:120]}"
    return time.time() - t0, text


def _scrape(web, query: str) -> str:
    outcome = web.search(query, limit=8)
    if not outcome.hits:
        note = "；".join(outcome.attempts) or "无结果"
        return f"(0 条，{note})"
    lines = [f"[{outcome.engine} | ignored={outcome.query_ignored}]"]
    for h in outcome.hits[:5]:
        lines.append(f"{h.title} :: {h.url}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    # 兼容 `--only scrape`（空格）与 `--only=scrape`（等号）两种写法
    opts: dict[str, str] = {}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--") and "=" in a:
            k, v = a[2:].split("=", 1)
            opts[k] = v
        elif a.startswith("--") and i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            opts[a[2:]] = argv[i + 1]
            i += 1
        elif a.startswith("--"):
            opts[a[2:]] = ""
        else:
            rest.append(a)
        i += 1
    only = opts.get("only", "")
    timeout = float(opts.get("timeout", 45))
    queries = rest or DEFAULT_QUERIES

    mcp = load_standalone("pet_mcp_ab", PET / "mcp_search.py")
    web = load_standalone("pet_web_ab", PET / "web.py")

    backends = [
        ("精准 free-search", lambda q: mcp.search_free(q, timeout_s=timeout)),
        ("快速 websearch-mcp", lambda q: mcp.search(q, timeout_s=timeout)),
        ("自抓 HTML", web),
    ]
    if only:
        backends = [b for b in backends if only in ("free", "fast", "scrape") and
                    (only == "free") == (b[0].startswith("精准")) and
                    (only == "fast") == (b[0].startswith("快速")) and
                    (only == "scrape") == (b[0].startswith("自抓"))]

    for q in queries:
        print(f"\n{'=' * 76}\n查询：{q}\n{'=' * 76}")
        for name, fn in backends:
            elapsed, text = run_backend(name, fn, q, timeout)
            size = f"{len(text)} 字" if not text.startswith("❌") else "-"
            print(f"\n[{name}]  {elapsed:.1f}s  {size}")
            print(f"  {one_line(text, 200)}")

    print(
        "\n判读要点：看条数与首条相关性，别只看耗时 ——\n"
        "  websearch-mcp 快但对中文长尾词静默失败（分词劣化，结果像成功）；\n"
        "  free-search 慢 10 倍但能拿到官方源头。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
