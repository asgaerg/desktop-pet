"""编译 `iohook` 给 Electron 32 用（没有预编译，只能现场编）。

## 现状调查结论（已查实）

  · 预编译只到 `electron-v87`（Electron 12）—— **没有 ABI 128**
  · 直接从 GitHub 请求 `iohook-v0.9.3-electron-v128-win32-x64.tar.gz` -> 502/不存在
  · **但 MSVC 在**：`VS 2019 BuildTools` + `MSVC 14.29.30133`
  · iohook 的 `build.js` 用 **node-gyp**（不是 CMake；CMakeLists 只是给手动构建用的）

## 风险（事先说清楚）

```
iohook 最后更新: 2022-06
目标运行时:      Electron 32（Node 20 内核，V8 11.x）
```

2022 年的原生插件编 2025 年的 V8 头文件**可能大面积报错**
（`nan.h` 里的 `error C2039: is not a member of 'v8::...'`）。
**设定止损点：改源码超过 3 处就停下汇报**，不无限投入。

## 步骤

  ① 把 iohook 源码放到一个独立构建目录
  ② `npm install` 它的构建依赖（nan / node-gyp / node-abi …）
  ③ `node build.js --runtime=electron --abi=128 --version=32.3.3`
     或直接调 node-gyp
  ④ 只报告结果，不往产品里接 —— 先证明"能编出来"
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

NODE24 = r"C:\Users\tianyi\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin"
BUILD = ROOT / "apps" / "desktop" / "pet-data" / "ptt-tmp" / "iohook-build"
SRC_TGZ = ROOT / "apps" / "desktop" / "pet-data" / "ptt-tmp" / "iohook-src" / "iohook-0.9.3.tgz"

print("  ═══ ① 准备构建目录 ═══")
if BUILD.exists():
    shutil.rmtree(BUILD, ignore_errors=True)
BUILD.mkdir(parents=True, exist_ok=True)

# 解压 tarball 到构建目录（保留 package/ 层级）
r = subprocess.run(
    ["tar", "-xzf", str(SRC_TGZ), "-C", str(BUILD)],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
)
pkg = BUILD / "package"
print(f"  解压到 {pkg}  存在={pkg.is_dir()}")
if not pkg.is_dir():
    print("  ❌ 解压失败")
    raise SystemExit(1)

# 让 npm install 认出它是 CommonJS 包（它自己的 package.json 就是，不用改）
pkgjson = json.loads((pkg / "package.json").read_text(encoding="utf-8"))
print(f"  iohook version={pkgjson.get('version')}  main={pkgjson.get('main')}")

print()
print("  ═══ ② 装构建依赖（nan / node-gyp / node-abi …）═══")
env = {
    **os.environ,
    "PATH": NODE24 + os.pathsep + os.environ.get("PATH", ""),
    # node-gyp 要能找到 Python
    "PYTHON": str(ROOT / ".venv" / "Scripts" / "python.exe"),
    # ⚠️ 没有 TTY 时 pnpm 会拒绝清理 node_modules，CI=true 让它放行
    "CI": "true",
}
env.pop("ELECTRON_RUN_AS_NODE", None)

t0 = time.monotonic()
# ⚠️ 踩过的坑（都记下来）：
#   · Node 24 那个运行时**只有 node.exe，没有 npm**
#   · DSH 自带的 pnpm 会**向上找到项目根的 workspace**，去动
#     F:\ai项目\node_modules（实测报 EPERM unlink 'node_modules/ajv/LICENSE'）
#     —— **绝不能让它碰项目依赖**
#   · 所以用**系统 npm 6（Node 14）**，并显式 --prefix 到隔离目录。
#     nan / node-gyp / node-abi 都是老包，Node 14 完全够用；
#     真正需要 Node 20 头文件的是**编译 iohook 本身**（由 node-gyp 拉 Electron 头）
NPM = r"D:\Node.js\npm.cmd"
env_npm = {**env, "PATH": r"D:\Node.js" + os.pathsep + env["PATH"]}
print(f"  npm install exit={r.returncode}  ({time.monotonic() - t0:.0f}s)")
tail = (r.stdout or "").strip().split("\n")[-12:]
for l in tail:
    print(f"    {l[:110]}")
if r.returncode != 0:
    print("  stderr:", (r.stderr or "")[:600])

# 确认关键工具
for name in ("nan", "node-gyp", "node-abi"):
    hit = (pkg / "node_modules" / name).is_dir()
    print(f"    {name}: {'✅' if hit else '❌'}")

print()
print("  ═══ ③ 查 node-gyp 位置和可用性 ═══")
ng = pkg / "node_modules" / ".bin" / "node-gyp.cmd"
print(f"  node-gyp.cmd: {'✅' if ng.is_file() else '❌'}  {ng}")
if ng.is_file():
    r = subprocess.run([str(ng), "--version"], cwd=str(pkg), env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(f"  node-gyp --version -> {(r.stdout or r.stderr or '').strip()[:80]}")

print()
print("  ═══ ④ 编译（target=Electron 32.3.3, abi=128）═══")
if not ng.is_file():
    print("  ❌ 没有 node-gyp，停在这里")
    raise SystemExit(1)

env2 = {
    **env,
    "npm_config_runtime": "electron",
    "npm_config_target": "32.3.3",
    "npm_config_arch": "x64",
    "npm_config_disturl": "https://electronjs.org/headers",
    "npm_config_build_from_source": "true",
    "npm_config_msvs_version": "2019",
    "npm_config_loglevel": "warn",
    "ARCH": "x64",
}

t0 = time.monotonic()
r = subprocess.run(
    [sys.executable, "build.js", "--runtime=electron", "--abi=128", "--version=32.3.3"],
    cwd=str(pkg), env=env2,
    capture_output=True, text=True, encoding="utf-8", errors="replace",
    timeout=1800,
)
dt = time.monotonic() - t0
print(f"  exit={r.returncode}  耗时 {dt:.0f}s")
print()
print("  --- build.js 输出（尾 30 行）---")
for l in (r.stdout or "").strip().split("\n")[-30:]:
    print(f"    {l[:118]}")
if (r.stderr or "").strip():
    print("  --- stderr（尾 20 行）---")
    for l in r.stderr.strip().split("\n")[-20:]:
        print(f"    {l[:118]}")

print()
print("  ═══ ⑤ 产物检查 ═══")
builds = pkg / "builds"
found = []
if builds.is_dir():
    for p in builds.rglob("*.node"):
        found.append(p)
        print(f"  ✅ {p.relative_to(pkg)}  {p.stat().st_size // 1024}KB")
if not found:
    print("  ❌ 没有生成 .node 文件")
    bd = pkg / "build"
    if bd.is_dir():
        print("  （build/ 目录里有：）")
        for p in list(bd.rglob("*"))[:20]:
            if p.is_file():
                print(f"     {p.relative_to(pkg)}")
else:
    print()
    print("  🎉 **编译成功** —— 可以进入下一步：用最小 Electron 程序验证它收不收得到键")
