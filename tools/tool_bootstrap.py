"""
工具的共用引导 —— **复刻 sidecar 的运行环境**。

## ⚠️ 为什么必须有这个（2026-10-07 踩到）

直接 `cd 仓库根 && python tools/xxx.py` 跑工具会静默降级，而且**会污染真实数据库**：

```
① cwd 不对 -> cppjieba 找不到 `./dict/jieba.dict.utf8`
   -> 中文检索退化为逐字切（只是变慢变差，看不出来）

② 嵌入器降级 -> `BgeEmbedder` 加载失败时回落到 `HashingEmbedder`（256 维）
   -> 工具**往真实 pet.db 里写了一条 256 维向量**
   -> 下次应用启动读到"库里 [256]，当前 512" -> **全库重建**
```

第 ② 条实测发生过：`tools/measure-hint-dup.py` 跑完，应用重启日志里出现
`向量维度不匹配（库里 [256]，当前嵌入器 512）—— 正在重建全部向量`。
虽然应用能自愈（重建 512），但"测量工具改动真实数据"是绝不能接受的。

## 应用的正确环境（见 `apps/desktop/src/main/sidecar.ts` 的 spawn 参数）

| 项 | 值 |
|---|---|
| 解释器 | `<repo>/.venv/Scripts/python.exe` |
| **cwd** | `<repo>/apps/desktop/pet-data/vendor/simple/libsimple-windows-x64`（dict/ 就在旁边）|
| PYTHONPATH | `<repo>/apps/brain`（cwd 让给 DLL 了，所以包要靠它找）|
| 凭据 | `PET_CREDENTIALS_FILE` 指向 `<repo>/apps/desktop/pet-data/credentials.json` |

## 用法

    from tool_bootstrap import setup          # noqa: E402
    setup()                                   # 必须**在最前面**调用
    from pet.config import load_config        # 之后才能 import pet.*

⚠️ `setup()` 会 `os.chdir()`，所以**必须先算好绝对路径**再调它 ——
   模块里凡是相对路径都要在 `setup()` 之前 `resolve()` 好。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["ROOT", "setup", "simple_dir"]

# 这个模块住在 tools/ 下，所以仓库根是它的上一级 —— 用 __file__ 定位，
# 不依赖 cwd（否则 setup() 一 chdir 就找不到自己了）。
ROOT = Path(__file__).resolve().parent.parent


def simple_dir() -> Path:
    """分词 DLL 所在目录（`dict/` 就在它旁边，所以 cwd 必须是它）。"""
    return ROOT / "apps" / "desktop" / "pet-data" / "vendor" / "simple" / "libsimple-windows-x64"


def setup(verbose: bool = True) -> Path:
    """
    把当前进程调整成"和 sidecar 一样"的环境。

    返回实际使用的 cwd（供调用方打印诊断）。
    """
    brain = ROOT / "apps" / "brain"
    if str(brain) not in sys.path:
        sys.path.insert(0, str(brain))

    # 凭据：不注入的话 `config.api_key()` 找不到密钥，
    # 会误判成"没配密钥"（我第一版就这么错过）。
    cred = ROOT / "apps" / "desktop" / "pet-data" / "credentials.json"
    if cred.is_file():
        os.environ.setdefault("PET_CREDENTIALS_FILE", str(cred))

    sd = simple_dir()
    dict_ok = (sd / "dict" / "jieba.dict.utf8").is_file()
    # cwd 让给 DLL；拿不到词典才退回 brain 目录（那时工具会明确告知降级）
    work = sd if dict_ok else brain
    os.chdir(work)

    if verbose:
        print(f"[bootstrap] cwd = {work}")
        print(f"[bootstrap] 分词词典 = {'已就位' if dict_ok else '**缺失（中文检索会降级）**'}")
        print(f"[bootstrap] 凭据文件 = {'已注入' if cred.is_file() else '**未找到**'}")
    return work
