"""把产物传到已建好的 GitHub Release。

用法：
    python tools/release-upload.py 0.2.3
    python tools/release-upload.py 0.2.3 --file release/xxx.exe   # 只传指定文件

每个传完会**核对 GitHub 返回的 digest 与本地 SHA256**。
GitHub 对大文件分块上传，网络抖动时可能"看起来成功"但内容不全 ——
所以校验不能省。

Token 从环境变量 `GH_TOKEN` 读。

## ⚠️ 上传走 uploads.github.com，和 API 不是同一个域

API 可能要走代理（`--proxy`），但上传**直连通常更快**，
代理反而可能因为缓冲大文件而超时。所以这里默认 `--noproxy`。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
REPO = "asgaerg/desktop-pet"
LOG = ROOT / "build" / "upload.log"


def say(msg: str) -> None:
    print(msg, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(msg + "\n")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(4 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def get_release_id(token: str, tag: str, proxy: str | None) -> int:
    """按 tag 查已有 Release 的 id —— 不依赖 _release_id.txt（那个会过期）。"""
    cmd = ["curl", "-sS", "-m", "60",
           f"https://api.github.com/repos/{REPO}/releases/tags/{tag}",
           "-H", f"Authorization: Bearer {token}",
           "-H", "Accept: application/vnd.github+json"]
    if proxy:
        cmd += ["--proxy", proxy]
    r = subprocess.run(cmd, capture_output=True)
    out = r.stdout.decode("utf-8", "replace")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise SystemExit(f"❌ 查 Release 失败：{out[:300]}")
    if "id" not in data:
        raise SystemExit(
            f"❌ 找不到 tag {tag} 的 Release（{data.get('message')}）——"
            f"先用 release-create.py 建一个"
        )
    return int(data["id"])


def default_files(ver: str) -> list[Path]:
    """按 version 猜产物位置。"""
    cands = [
        ROOT / "release" / f"DesktopPet-{ver}-online-setup.exe",
        ROOT / "release" / "portable" / f"DesktopPet-{ver}-online-portable.zip",
        ROOT / "release" / f"DesktopPet-{ver}-offline-setup.exe",
        ROOT / "release" / "portable" / f"DesktopPet-{ver}-offline-portable.zip",
    ]
    return [p for p in cands if p.is_file()]


def main() -> int:
    ap = argparse.ArgumentParser(description="上传产物到 GitHub Release")
    ap.add_argument("version", help="版本号，如 0.2.3（不要带 v）")
    ap.add_argument("--file", action="append", default=None,
                    help="只传这个文件（可重复）；不给就自动找 release/ 下的产物")
    ap.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"),
                    help="上传走代理（默认直连）")
    ns = ap.parse_args()

    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        raise SystemExit("❌ 没设 GH_TOKEN 环境变量")

    ver = ns.version.lstrip("v")
    tag = f"v{ver}"
    rel_id = get_release_id(token, tag, ns.proxy)
    say("=" * 60)
    say(f"Release {tag}  id={rel_id}")

    files = [Path(f) for f in ns.file] if ns.file else default_files(ver)
    if not files:
        raise SystemExit(f"❌ 没找到 {ver} 的产物，用 --file 指定")
    total = sum(f.stat().st_size for f in files if f.is_file())
    say(f"待传 {len(files)} 个文件，共 {total / 1024 / 1024:,.0f} MB")

    noproxy = ["--noproxy", "*"] if not ns.proxy else ["--proxy", ns.proxy]
    failed = 0
    for f in files:
        if not f.is_file():
            say(f"⚠️ 跳过（不存在）：{f}")
            continue
        name = f.name
        size_mb = f.stat().st_size / 1024 / 1024
        local = sha256(f)
        say(f"[{time.strftime('%H:%M:%S')}] 上传 {name}  {size_mb:,.0f} MB  sha={local[:16]}…")
        t0 = time.time()

        r = subprocess.run(
            [
                "curl", "-sS", "--progress-bar", "-X", "POST", *noproxy,
                "-m", "3600",
                f"https://uploads.github.com/repos/{REPO}/releases/{rel_id}/assets?name={name}",
                "-H", f"Authorization: Bearer {token}",
                "-H", "Accept: application/vnd.github+json",
                "-H", "X-GitHub-Api-Version: 2022-11-28",
                "-H", "Content-Type: application/octet-stream",
                "--data-binary", f"@{f}",
            ],
            capture_output=True,
        )
        took = time.time() - t0
        out = r.stdout.decode("utf-8", "replace")
        """
        ## 🔴 解析响应：`rfind("{")` 会切出残片（2026-11-02 修）

        原实现：

            brace = out.rfind("{")
            data = json.loads(out[brace:]) if brace >= 0 else {}

        `rfind` 找的是**最后一个 `{`**，而响应 JSON 里对象是嵌套的
        （`"uploader": {...}` / `"digest"` 之后的字段），于是截出来的是
        一段**以 `{` 开头的残片**，`json.loads` 抛 JSONDecodeError，
        `data` 变成 `{}`，然后被判成"失败"。

        实际后果（v0.2.4 发版时踩到）：**文件真的传上去了**
        （GitHub 那边 `state=uploaded`、digest 与本地一致），
        但脚本报「❌ 失败」，于是我以为没传、又传了一遍。

        修法：`--progress-bar` 的进度条走 **stderr**，JSON 在 **stdout**，
        所以直接 `json.loads(out)` 即可；兜底再试"从第一个 `{` 到最后一个 `}`"。
        """
        data: dict[str, object] = {}
        for candidate in (
            out.strip(),                       # 正常情况下 stdout 就是纯 JSON
            out[out.find("{"): out.rfind("}") + 1] if "{" in out else "",
        ):
            if not candidate:
                continue
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                data = parsed
                break

        if "id" not in data:
            say("   ❌ 失败：" + (out[-400:] if out else (r.stderr or b"").decode('utf-8','replace')[:300]))
            failed += 1
            continue

        digest = (data.get("digest") or "").replace("sha256:", "")
        ok = (digest == local) if digest else None
        say(f"   ✅ 完成 id={data['id']}  {took / 60:.1f} 分钟  {size_mb / (took or 1):.1f} MB/s")
        if ok is True:
            say(f"   校验：digest 与本地一致 ({digest[:16]}…)")
        elif ok is False:
            say(f"   ⚠️ 校验不一致！本地 {local[:16]}… GitHub {digest[:16]}…")
            failed += 1
        else:
            say("   校验：GitHub 没返回 digest，跳过比对")

    say(f"[{time.strftime('%H:%M:%S')}] 结束，失败 {failed} 个")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
