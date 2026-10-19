"""实测：大脑进程 334 MB 都是什么占的。

## 背景

用户在任务管理器看到「Python (2) 303.9 MB」，问「这个 python 又是哪里来的」。
已查明是两个父子进程（venv 启动器 + 真解释器），都是**大脑 sidecar**。
但 **334 MB 值得解释清楚**。

## 怎么测

逐个 import 重量级模块，每次用 `GetProcessMemoryInfo` 记工作集，
算出每个模块的**增量**。这样能说清"哪一块占了多少"，而不是猜。

（`ctypes` 那个调用要注意 `GetCurrentProcess` 的返回类型 ——
默认会被当成 int32，句柄一大就 OverflowError，必须显式声明 `restype = HANDLE`。）
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt


class PMC(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


k32 = ctypes.WinDLL("kernel32")
psapi = ctypes.WinDLL("psapi")
# ⚠️ 都必须显式声明 restype/argtypes：默认按 int32 处理，64 位句柄会 OverflowError
k32.GetCurrentProcess.restype = wt.HANDLE
k32.GetCurrentProcess.argtypes = []
psapi.GetProcessMemoryInfo.restype = wt.BOOL
psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(PMC), wt.DWORD]


def rss() -> float:
    p = PMC()
    p.cb = ctypes.sizeof(PMC)
    psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(p), p.cb)
    return p.WorkingSetSize / 1048576


MODEL = r"F:\ai项目\apps\desktop\pet-data\models\bge-small-zh-v1.5\model.onnx"

steps: list[tuple[str, float, float]] = []
b = rss()
steps.append(("裸解释器", b, 0.0))
prev = b

import numpy as np  # noqa: E402

cur = rss()
steps.append(("+ numpy", cur, cur - prev))
prev = cur

import onnxruntime as ort  # noqa: E402

cur = rss()
steps.append(("+ onnxruntime", cur, cur - prev))
prev = cur

import pathlib  # noqa: E402

if pathlib.Path(MODEL).exists():
    sess = ort.InferenceSession(MODEL, providers=["CPUExecutionProvider"])
    cur = rss()
    steps.append(("+ bge 模型（90MB 权重）", cur, cur - prev))
    prev = cur

    # 真跑一次推理，看会不会再涨（arena 预分配）
    import numpy as _np  # noqa: E402

    # 按模型声明的输入名喂（bge 要 input_ids + attention_mask + token_type_ids）
    feed = {
        i.name: _np.zeros((1, 32), dtype=_np.int64) for i in sess.get_inputs()
    }
    for _ in range(3):
        sess.run(None, feed)
    cur = rss()
    steps.append(("+ 推理 3 次后", cur, cur - prev))
    prev = cur

import sqlite3  # noqa: E402
import yaml  # noqa: E402
import httpx  # noqa: E402

cur = rss()
steps.append(("+ sqlite3/yaml/httpx", cur, cur - prev))

print()
print("  ═══ 内存增量（实测）═══")
for name, cum, inc in steps:
    bar = "#" * int(inc / 8)
    print(f"  {name:<24} {inc:>7.1f} MB  累计 {cum:>7.1f} MB  {bar}")
print()
print(f"  合计 {steps[-1][1]:.1f} MB")
