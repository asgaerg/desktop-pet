"""运行时的路径真源（2026-10-18 打包引入）。

开发态和打包态的目录布局**不一样**，但业务代码不该到处判断：

```
开发态   <repo>/config、<repo>/apps/desktop/pet-data
打包态   <resources>/config（只读）、%APPDATA%\桌宠\pet-data（可写）
```

所以集中在这里，由主进程用环境变量告诉 sidecar：

    PET_PROJECT_ROOT   只读资源根
    PET_DATA_DIR       可写数据根

两个都不设时退回 `parents[3]`（开发态行为，保证不传环境变量也能跑）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def project_root() -> Path:
    """只读资源根（config、vendor、websearch-mcp.exe 都在它下面）。"""
    env = os.environ.get("PET_PROJECT_ROOT")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        # 冻结后 __file__ 在 _internal 里，靠 exe 的位置更可靠
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


def data_dir() -> Path:
    """可写数据根（记忆库、日志、备份、模型、搜索运行时都放这儿）。"""
    env = os.environ.get("PET_DATA_DIR")
    if env:
        return Path(env)
    return project_root() / "apps" / "desktop" / "pet-data"


def models_dir() -> Path:
    """嵌入模型目录（bge-small-zh-v1.5）。"""
    return data_dir() / "models"


def vendor_dir() -> Path:
    """cppjieba 的 simple.dll 与词典。"""
    return data_dir() / "vendor"


def mcp_runtime_dir() -> Path:
    """websearch-mcp.exe 的运行时落点。"""
    return data_dir() / "mcp-search"
