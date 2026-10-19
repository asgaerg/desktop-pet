"""向量路的**性能代价**（第十七节要求的"复杂性"，一直没有量过）。

## 为什么必须量

`_retrieve` 的向量路是 **O(n) 线性扫描**：

    for mid, stored in self.db.all_vectors():   # 把全部向量读进内存
        sim = _cosine(vec, stored)              # 逐条算余弦（纯 Python 循环）

换成 bge 之后每条向量从 256 维变 **512 维（2KB）**，扫描成本翻倍。
`memory.max_items: 2000` 就是这条路的预算上限 ——
**但"上限"从没被实测过**。用户会觉得"越用越卡"，而那时已经晚了。

## 量什么

| 指标 | 为什么重要 |
|---|---|
| 建向量耗时（每条） | 写入路径的代价（每轮对话后台跑）|
| `all_vectors()` 读盘耗时 | 每次检索都要读全部向量 |
| 余弦扫描耗时 | 随 n 线性增长的那部分 |
| **端到端检索耗时** | 用户真正感受到的 |
| 单进程内存占用 | 模型常驻 + 向量常驻 |

## ⚠️ 用**临时库**测，不碰真实记忆

测 2000 条会往库里灌大量测试数据。真实记忆是用户的东西，
**不该被基准测试污染** —— 所以这里建一条临时库，测完删掉。
"""

from __future__ import annotations

import gc
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "apps" / "brain"))

_DLL = ROOT / "apps" / "desktop" / "pet-data" / "vendor" / "simple" / "libsimple-windows-x64"
if (_DLL / "dict" / "jieba.dict.utf8").is_file():
    os.chdir(_DLL)

from pet.embedding import build_embedder  # noqa: E402
from pet.store import PetDatabase, unpack_vector  # noqa: E402

# 拟真的中文事实（长度与真实提炼结果接近：15~30 字）
TOPICS = ["桌面宠物", "咖啡", "作息", "Rust", "摄影", "跑步", "猫", "工作",
          "天气", "音乐", "电影", "旅行", "读书", "游戏", "做饭", "健身"]
PATTERNS = [
    "用户喜欢{t}",
    "用户经常聊到{t}",
    "用户在研究{t}相关的东西",
    "用户对{t}有明确的偏好，不喜欢别人替他决定",
    "用户提到{t}的时候情绪会变好",
]
PREFIX = ["", "其实", "说起来", "顺便提一句，", "对了，"]

SIZES = [100, 500, 2000]


def make_fact(i: int) -> str:
    t = TOPICS[i % len(TOPICS)]
    p = PATTERNS[(i // len(TOPICS)) % len(PATTERNS)]
    pre = PREFIX[i % len(PREFIX)]
    return f"{pre}{p.format(t=t)}（第 {i} 条）"


def rss_mb() -> float:
    """当前进程的**工作集**（MB）。

    ⚠️ 两个坑（第一次测全是 0，就是因为这两个）：

      ① `GetCurrentProcess()` 返回的是伪句柄 **-1**，而 ctypes 默认把返回值
         当 **32 位 int** —— 在 64 位下会被截断成 `0xFFFFFFFF`，调用直接失败。
         必须显式声明 `restype = wintypes.HANDLE`。
      ② `argtypes` / `restype` 不声明时，`byref` 传结构体也可能出错。
         全部声明一遍最稳。
    """
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    k32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PMC),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    ok = psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
    return (pmc.WorkingSetSize / 1024 / 1024) if ok else -1.0


def main() -> int:
    print("=" * 84)
    print("向量路的性能代价（bge-small-zh-v1.5 / 512 维）")
    print("=" * 84)

    base_rss = rss_mb()
    emb = build_embedder({"provider": "local", "dim": 512}, project_root=ROOT)
    name = type(emb).__name__
    print(f"  嵌入器        {name}")
    if not emb.load() if hasattr(emb, "load") else False:
        print("  ❌ 模型没加载")
        return 1
    print(f"  维度          {emb.dim}")

    # ---- 建向量吞吐 ----
    print()
    print("─" * 84)
    print("① 建向量（写入路径的代价）")
    print("─" * 84)
    probes = [make_fact(i) for i in range(30)]
    emb._cache.clear() if hasattr(emb, "_cache") else None
    t0 = time.perf_counter()
    for p in probes:
        emb.embed(p)
    cold = (time.perf_counter() - t0) / len(probes) * 1000
    print(f"  单条平均     {cold:.2f} ms（冷，含首次推理初始化）")
    print(f"  -> 2000 条约 {cold * 2000 / 1000:.1f} 秒（只在换模型/全量重建时发生）")

    tmp = Path(tempfile.mkdtemp(prefix="pet-bench-"))
    db_path = tmp / "bench.db"
    db = PetDatabase(db_path, tokenizer="simple", simple_dll=_DLL / "simple.dll")
    after_model_rss = rss_mb()
    print(f"  模型加载后内存 {after_model_rss:.0f} MB（进程基线 {base_rss:.0f} MB）")
    print()
    print(f"  临时库       {db_path.name}")
    print("  （**没有碰真实记忆库**）")

    written = 0
    t0 = time.perf_counter()
    for i in range(max(SIZES)):
        text = make_fact(i)
        v = emb.embed(text)
        db.add_memory(text, kind="fact", level="recent", vector=v)
        written += 1
    build_s = time.perf_counter() - t0
    print(f"  写入 {written} 条总耗时 {build_s:.1f}s（含 embed + 入库）")

    # ---- 分级测量 ----
    print()
    print("─" * 84)
    print("② 检索耗时（真正的用户感受）")
    print("─" * 84)
    print(
        f"  {'条数':>6}{'读向量ms':>10}{'余弦扫描ms':>12}{'端到端ms':>10}"
        f"{'向量MB':>9}{'RSS MB':>9}"
    )

    queries = ["他喜欢什么", "用户在做桌面宠物项目", "咖啡", "最近在忙什么"]
    all_ids = [r["id"] for r in db.conn.execute("SELECT id FROM memory ORDER BY id")]

    # ⚠️ 测量用的**产品代码**：`all_vectors_raw` + `_cosine_batch_raw` ——
    # 就是 `_retrieve` 现在实际调的那两个。
    #
    # 这个基准脚本改过两次口径，值得记一笔：
    #   ① 第一版内联写了一遍余弦 -> 那是"测我手写的循环"，不是测产品代码；
    #   ② 第二版改用 `_cosine`（逐条）-> 产品代码随后优化成了批量，口径又过期；
    #   ③ 现在直接调产品代码里的函数，**优化前后都能如实反映**。
    from pet.memory2 import _cosine_batch_raw  # noqa: E402

    def time_embed_query(q: str) -> float:
        """查询向量化的耗时（ms）。

        ⚠️ 每次**先清嵌入缓存**，否则第二次起 query 直接命中缓存，
        端到端就漏掉了"把查询变成向量"这一段的真实成本 —— 第一版就是
        这样低估的（4 个查询里 3 个是缓存命中）。
        """
        if hasattr(emb, "_cache"):
            emb._cache.clear()
        t0 = time.perf_counter()
        emb.embed(q)
        return (time.perf_counter() - t0) * 1000

    for n in SIZES:
        ids = all_ids[:n]
        ph = ",".join("?" * len(ids))
        vec_times, scan_times, embed_times = [], [], []
        for q in queries:
            embed_times.append(time_embed_query(q))
            qv = emb.embed(q)  # 复用（上面那次已把向量算好并缓存）

            t0 = time.perf_counter()
            rows_raw = db.conn.execute(
                f"SELECT memory_id, vector FROM memory_vec WHERE memory_id IN ({ph})", ids
            ).fetchall()
            mids = [int(r["memory_id"]) for r in rows_raw]
            blobs = [bytes(r["vector"]) for r in rows_raw]
            vec_times.append((time.perf_counter() - t0) * 1000)

            t0 = time.perf_counter()
            _cosine_batch_raw(qv, blobs, len(qv))  # 产品代码里的实现
            scan_times.append((time.perf_counter() - t0) * 1000)

        vec_bytes = len(ids) * 512 * 4
        med_embed = statistics.median(embed_times)
        med_read = statistics.median(vec_times)
        med_scan = statistics.median(scan_times)
        e2e = med_embed + med_read + med_scan
        print(
            f"  {n:>6}{med_read:>10.1f}{med_scan:>12.1f}{e2e:>10.1f}"
            f"{vec_bytes / 1024 / 1024:>9.2f}{rss_mb():>9.0f}"
        )
        print(
            f"        （其中：查询向量化 {med_embed:.1f}ms + 读向量 {med_read:.1f}ms"
            f" + 余弦扫描 {med_scan:.1f}ms）"
        )

    print()
    print("─" * 84)
    print("③ 结论")
    print("─" * 84)
    del db
    gc.collect()
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)
    print("  临时库已删除（真实记忆库未被触碰）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
