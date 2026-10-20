#!/usr/bin/env python3
"""改版本号（打包前跑一次）。

## 为什么要有这个脚本

版本号散在**四个地方**，手工改必然漏一个：

    package.json                    （根，workspace 的版本）
    apps/desktop/package.json       （electron-builder 的 ${version} 取这里）
    packages/protocol/package.json
    apps/brain/pet/__init__.py      （__version__，Python 侧 / 日志里显示）

漏改的后果不是报错，而是**两个地方显示不同版本**：安装包文件名是新的、
关于页/日志里还是旧的 —— 用户看到会以为装错了。

## 用法

    python tools/bump-version.py 0.1.1        # 改
    python tools/bump-version.py --check      # 只看现在多少、有没有不一致
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

ROOT = Path(__file__).resolve().parent.parent

JSON_FILES = [
    ROOT / "package.json",
    ROOT / "apps" / "desktop" / "package.json",
    ROOT / "packages" / "protocol" / "package.json",
]
PY_FILE = ROOT / "apps" / "brain" / "pet" / "__init__.py"

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def read_json_version(path: Path) -> str:
    return str(json.loads(path.read_text(encoding="utf-8")).get("version", ""))


def read_py_version(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r'^__version__\s*=\s*["\']([^"\']+)["\']', line)
        if m:
            return m.group(1)
    return ""


def current() -> dict[str, str]:
    out = {str(p.relative_to(ROOT)): read_json_version(p) for p in JSON_FILES}
    out[str(PY_FILE.relative_to(ROOT))] = read_py_version(PY_FILE)
    return out


def write_json_version(path: Path, version: str) -> bool:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if data.get("version") == version:
        return False
    data["version"] = version
    # 保持两空格缩进 + 结尾换行（和仓库里现有文件一致）
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def write_py_version(path: Path, version: str) -> bool:
    raw = path.read_text(encoding="utf-8")
    new = re.sub(
        r'^(__version__\s*=\s*)["\'][^"\']+["\']',
        lambda m: f'{m.group(1)}"{version}"',
        raw,
        count=1,
        flags=re.MULTILINE,
    )
    if new == raw:
        return False
    path.write_text(new, encoding="utf-8")
    return True


def main() -> int:
    args = [a for a in sys.argv[1:]]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    now = current()
    if args[0] == "--check":
        print("  当前版本：")
        for name, version in now.items():
            print(f"    {name:<38} {version or '(没读到)'}")
        values = {v for v in now.values() if v}
        if len(values) == 1:
            print(f"  ✅ 四处一致：{values.pop()}")
            return 0
        print(f"  ❌ 不一致：{sorted(values)}")
        return 1

    version = args[0].lstrip("v")
    if not SEMVER.match(version):
        print(f"  ❌ 版本号要写成 0.1.1 这种三段式，收到：{version}")
        return 2

    if version in now.values():
        print(f"  ⚠️ 已经是 {version} 了，什么都没改")
        return 0

    changed: list[str] = []
    for path in JSON_FILES:
        if write_json_version(path, version):
            changed.append(str(path.relative_to(ROOT)))
    if write_py_version(PY_FILE, version):
        changed.append(str(PY_FILE.relative_to(ROOT)))

    print(f"  ✅ 版本号 {sorted(set(now.values()))} -> {version}")
    for name in changed:
        print(f"    改：{name}")

    release_doc = ROOT / "docs" / f"RELEASE-v{version}.md"
    print()
    print("  接着做（打包前清单）：")
    print(f"    1. 版本号已改：{version}")
    print(f"    2. 写发布说明：docs/RELEASE-v{version}.md"
          + ("" if release_doc.exists() else "   ← 还没有，记得写"))
    print("    3. 跑测试：cd apps/desktop && npm test"
          "（以及 cd apps/brain && python -m pet.selftest）")
    print("    4. 提交 + 打标签：")
    print(f"         git add -A && git commit -m \"release: v{version}\"")
    print(f"         git tag v{version} && git push origin main --tags")
    print("    5. 打包：cd apps/desktop && npm run package:online")
    print("    6. 上传 Release（tools/release-create.py / release-upload.py）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
