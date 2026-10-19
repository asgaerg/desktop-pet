"""清理项目冗余数据（**先 dry-run 打印，再按 --yes 执行**）。

## 保留清单（明确不删）

  · `pet-data/asr-models/**`              —— 用户要求：本地 ASR 模型都留着
  · `pet-data/vendor/simple/**`           —— `memory2.py` 加载 `simple.dll` 做中文分词
  · `pet-data/models/bge-small-zh-v1.5/**`—— 本地语义嵌入，正在用
  · `pet-data/memory/**`                  —— 记忆数据库
  · `pet-data/backups/**`                 —— 记忆备份（`memory.backup.keep=4`）
  · `pet-data/instance-state/**`          —— 单实例锁（运行中）
  · `pet-data/ptt-tmp/**`                 —— 按住说话临时目录
  · `pet-data/unihan/**`                  —— 见下面说明（未确认，先不动）
  · `apps/desktop/dist/**`                —— 真构建输出，应用在读
  · `.test-build/*.cjs`                   —— `npm test` 需要的编译产物（可重建）
  · `node_modules/**`、`.venv/**`

## 删除清单（每项都有依据）

  A. `.test-build/<名字>/`    86 个 —— 旧测试的 userData 残渣（10-01~10-08），
                              名字都是 `bubbleprobe` / `yamlprobe` 这种一次性探针
  B. `pet-data/cuda-wheels/`  541 MB —— 离线 whl 缓存（`nvidia_cublas_cu12`），
                              装完就没用；代码里无引用
  C. `pet-data/_staging/`     空
  D. `pet-data/backup-before-migration/`  空
  E. `pet-data/tts/`          空
  F. `pet-data/tts-tmp/*`     探测残留（wav/txt/ps1）
  G. `pet-data/backups/*.db-shm|*-wal`  —— 孤儿 WAL（对应的 .db 已经不在了）
  H. `**/__pycache__/`        1.1 MB，可再生
  I. `pet-data/python.log`    167.6 MB —— **就地截断**（进程开着，删不掉）
  J. `pet-data/tts-diag.log`  我排查时加的诊断日志

## 为什么 python.log 用截断而不是删除

sidecar 进程正开着这个文件（`--log-file`），Windows 下删除会失败。
`open('w')` 截断是允许的（共享模式允许写）。
"""

from __future__ import annotations

import pathlib
import shutil
import sys

ROOT = pathlib.Path(r"F:\ai项目")
DRY = "--yes" not in sys.argv
PD = ROOT / "apps" / "desktop" / "pet-data"
TB = ROOT / "apps" / "desktop" / ".test-build"

KEEP_DIRS = {
    "asr-models", "vendor", "models", "memory", "backups",
    "instance-state", "ptt-tmp", "unihan",
}


def size_of(p: pathlib.Path) -> int:
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def mb(n: int) -> str:
    return f"{n / 1048576:,.1f} MB"


freed_total = 0
removed: list[str] = []


def zap(p: pathlib.Path, why: str) -> None:
    global freed_total
    if not p.exists():
        return
    s = size_of(p)
    freed_total += s
    removed.append(f"{mb(s):>12}  {p.relative_to(ROOT) if ROOT in p.parents else p}   [{why}]")
    if DRY:
        return
    try:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink()
    except OSError as e:
        print(f"    ⚠️ 删不掉 {p}: {e}")


print("  ═══════════════════════════════════════════════════════════")
print(f"  {'【试运行】只打印，不真删' if DRY else '【执行删除】'}")
print("  ═══════════════════════════════════════════════════════════")

# ── A. 旧测试的 userData ────────────────────────────────────────────────
print("\n  ── A. .test-build 下旧测试的 userData ──")
n_a = 0
for p in sorted(TB.iterdir()):
    if p.is_dir():
        zap(p, "旧测试 userData 残渣")
        n_a += 1
print(f"    （{n_a} 个目录）")

# ── B~F. pet-data 下的冗余 ──────────────────────────────────────────────
print("\n  ── B~F. pet-data 冗余 ──")
zap(PD / "cuda-wheels", "离线 whl 缓存，无引用")
zap(PD / "_staging", "空目录")
zap(PD / "backup-before-migration", "空目录")
zap(PD / "tts", "空目录")
if (PD / "tts-tmp").exists():
    for p in sorted((PD / "tts-tmp").iterdir()):
        zap(p, "TTS 探测残留")

# ── G. 孤儿 WAL ─────────────────────────────────────────────────────────
print("\n  ── G. backups 里的孤儿 WAL/SHM（对应 .db 已不存在）──")
bk = PD / "backups"
if bk.exists():
    stems = {p.stem for p in bk.iterdir() if p.suffix == ".db"}
    for p in sorted(bk.iterdir()):
        if p.suffix in (".db-shm", ".db-wal"):
            base = p.name.rsplit("-", 1)[0]
            if base not in stems and not (bk / f"{base}.db").exists():
                zap(p, "孤儿 WAL/SHM")
    # 保底：把 keep 之外的旧 .db 也点出来（不删，让用户看）
    dbs = sorted(
        (p for p in bk.iterdir() if p.suffix == ".db"),
        key=lambda p: p.stat().st_mtime,
    )
    print(f"    记忆备份 .db 共 {len(dbs)} 个（配置 keep=4）：")
    for p in dbs:
        print(f"      {mb(p.stat().st_size):>10}  {p.name}")

# ── K. 项目根目录下 pip 的临时目录 ──────────────────────────────────────
print("\n  ── K. 项目根目录下 pip 的临时目录 ──")
for p in sorted(ROOT.glob("pip-*")):
    zap(p, "pip 安装时的临时目录（361 MB 那个是 cublas wheel 解包）")

# ── L. 根目录那个笔误留下的 .test-build（真正的在 apps/desktop 下）──────
print("\n  ── L. 根目录 .test-build（笔误产物）──")
zap(ROOT / ".test-build", "根目录下的测试残渣（只有两个空目录）")

# ── H. __pycache__ ──────────────────────────────────────────────────────
print("\n  ── H. __pycache__ ──")
for p in sorted(ROOT.rglob("__pycache__")):
    if any(x in p.parts for x in ("node_modules", ".venv", ".test-build")):
        continue
    zap(p, "Python 字节码缓存")

# ── I~J. 日志 ───────────────────────────────────────────────────────────
print("\n  ── I~J. 日志 ──")
for name, why in (
    ("python.log", "截断（进程开着，167 MB）"),
    ("tts-diag.log", "我排查时加的诊断日志"),
):
    p = PD / name
    if p.exists():
        s = size_of(p)
        freed_total += s
        removed.append(f"{mb(s):>12}  pet-data/{name}   [{why}]")
        if not DRY:
            try:
                with p.open("w", encoding="utf-8"):
                    pass
            except OSError as e:
                print(f"    ⚠️ 截断失败 {p}: {e}")

# ── 汇总 ────────────────────────────────────────────────────────────────
print()
print("  ═══════════════════════════════════════════════════════════")
print("  清单")
print("  ═══════════════════════════════════════════════════════════")
for line in removed:
    print(f"  {line}")
print()
print(f"  {'将释放' if DRY else '已释放'}: {mb(freed_total)}")
print()
print("  ═══ 保留 ═══")
for d in sorted(KEEP_DIRS):
    p = PD / d
    if p.exists():
        print(f"    {mb(size_of(p)):>12}  pet-data/{d}")
