"""PyInstaller 的入口脚本（2026-10-18）。

为什么需要它：`python -m pet` 走的是 `pet/__main__.py` 里的**相对导入**
（`from .main import main`），直接把这个文件当脚本喂给 PyInstaller 会
`ImportError: attempted relative import with no known parent package`。

所以用一个普通脚本从**包外**导入 `pet.main`，配合 `--paths apps/brain`
就能被正确分析。
"""

import sys

from pet.main import main

if __name__ == "__main__":
    sys.exit(main())
