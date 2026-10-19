"""打包前的密钥护栏：扫"将被装进包里的东西"，有真密钥就退出码 1（2026-10-18）。

用法：

    python tools/check-pack-secrets.py                     # 扫默认范围
    python tools/check-pack-secrets.py <目录> [<目录>...]   # 扫指定目录（打包产物）

## 它能抓什么

    sk-…        DeepSeek / OpenAI 风格密钥（长度 >= 20）
    hf_…        HuggingFace 令牌
    Bearer …    明文的 Bearer 令牌

## 它**不**抓什么

    · `api_key_env: DEEPSEEK_API_KEY` 这种**字段名**（本来就没密钥）
    · 已知的测试假密钥（selftest 里的 `sk-selftest-*` 等，见 ALLOW）

## 为什么要单独一个脚本

"打包时别忘了排除 credentials" 靠记性迟早出事 —— 密钥泄露是不可逆的。
所以把它做成**可执行的门禁**：打包脚本最后一步跑它，红了就不发布。
"""

from __future__ import annotations

import pathlib
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 测试用的假密钥 + 文档里的占位符：都不是秘密
ALLOW = re.compile(
    r"sk-(selftest|deepseek-fake|should-not-be-used|abcdefghijklmnop|test|fake|dummy|x{6,})",
    re.I,
)

PATTERNS = [
    ("sk 密钥", re.compile(rb"sk-[A-Za-z0-9_\-]{20,}")),
    ("hf 令牌", re.compile(rb"hf_[A-Za-z0-9]{25,}")),
    ("Bearer", re.compile(rb"Bearer\s+[A-Za-z0-9_\-\.]{25,}")),
]

# 默认扫描范围：会被打进包的东西（不含 node_modules/.venv/pet-data/dist）
DEFAULT_TARGETS = [
    ROOT / "config",
    ROOT / "apps" / "desktop" / "src",
    ROOT / "packages",
    ROOT / "apps" / "brain" / "pet",
]

SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", ".git", "pet-data", "dist", "backups"}


def mask(raw: bytes) -> str:
    s = raw.decode("utf-8", "replace")
    return s[:6] + "…" + s[-4:] if len(s) > 12 else s[:3] + "***"


def scan(path: pathlib.Path, hits: list[tuple[pathlib.Path, str, str]]) -> None:
    """
    扫一个目录。

    ⚠️ 跳过规则只看**相对目标根**的路径：
    以前用绝对路径的 parts 判断，于是 `check-pack-secrets.py apps/desktop/pet-data`
    会把自己要扫的目录跳过（-- 显式点名要查，却什么都没查）。
    这类"扫了个寂寞"的护栏比没有护栏更危险。
    """
    root = path.resolve()
    for p in path.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel_parts = set(x.lower() for x in p.resolve().relative_to(root).parts[:-1])
        except ValueError:
            rel_parts = set()
        if rel_parts & SKIP_DIRS:
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        # 二进制文件跳过（2026-10-18）：
        #
        # 第一次扫 `release/win-unpacked` 时，两条命中全是**二进制里的巧合字节**：
        #   vulkan-1.dll   -> "sk-com…eter"（"disk-com…" 之类的片段）
        #   locales\nb.pak -> "sk-fil…this"
        # 真密钥只会出现在文本（json/yaml/ts/py/log）里，扫二进制纯属制造噪音，
        # 而噪音会让这个护栏失去意义（每次都报，人就学会无视它）。
        if b"\x00" in data[:8192]:
            continue
        for label, pat in PATTERNS:
            for m in pat.finditer(data):
                if ALLOW.search(m.group(0).decode("utf-8", "replace")):
                    continue
                hits.append((p, label, mask(m.group(0))))


def main(argv: list[str]) -> int:
    targets = [pathlib.Path(a) for a in argv[1:]] or DEFAULT_TARGETS
    hits: list[tuple[pathlib.Path, str, str]] = []
    for t in targets:
        if not t.exists():
            print(f"  跳过（不存在）：{t}")
            continue
        scan(t, hits)

    if not hits:
        print(f"  ✅ 只扫了 {len(targets)} 个目录，没发现真密钥")
        return 0

    print(f"  ❌ 发现 {len(hits)} 处疑似真密钥：")
    for p, label, masked in hits:
        try:
            rel = p.relative_to(ROOT)
        except ValueError:
            rel = p
        print(f"     [{label}] {rel}  {masked}")
    print("\n  打包前必须处理：把文件移出打包范围，或改成运行时让用户填。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
