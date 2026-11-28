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


def knowledge_dir() -> Path:
    """信息存储归档根（可写）。

    ## 🔴 2026-11-28 修：打包态不能写在 resources 里

    原来 `knowledge.knowledge_dir()` 用的是 `project_root() / "pet-knowledge"`。
    开发态没问题（就是仓库根），但**打包后 `project_root()` 是
    `<安装目录>\resources`** —— 那是 Program Files / ProgramData，
    普通用户**没有写权限**。

    实测（打包版日志）：

        知识归档就绪：0 个实体 / 0 个版本
        （C:\ProgramData\...\resources\pet-knowledge）

    后果有两个：
      · 归档要么静默失败（写不进去），要么写进安装目录（重装就没了）
      · 设置页「数据 → 信息存储 → 打开」打开的是**只读目录**

    ## 现在的规则

        ① 环境变量 `PET_KNOWLEDGE_DIR`（自检用来改道）
        ② `data_dir() / "pet-knowledge"`（可写，开发态也是它）

    ⚠️ 开发态路径会从 `<repo>/pet-knowledge` 变成
       `<repo>/apps/desktop/pet-data/pet-knowledge` —— 这是**刻意的**：
       所有可写数据都归到 pet-data 下，和 memory / models / web-sandbox 一致。
       已有归档要迁过去（见 tools/migrate-knowledge.py）。
    """
    import os

    env_dir = os.environ.get("PET_KNOWLEDGE_DIR")
    if env_dir:
        return Path(env_dir)
    return data_dir() / "pet-knowledge"


def mcp_runtime_dir() -> Path:
    """websearch-mcp.exe 的运行时落点。"""
    return data_dir() / "mcp-search"
