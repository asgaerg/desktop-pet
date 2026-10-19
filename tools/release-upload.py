"""把四个产物传到已建好的 Release（2026-10-19）。

顺序：先在线两个（小、快），再离线两个（3.4 GB）。
每个传完核对 GitHub 返回的 digest 与本地 SHA256 是否一致。
"""

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(r"F:\ai")
TOKEN = os.environ["GH_TOKEN"]
PROXY = None  # uploads.github.com 可直连，代理反而更慢
REPO = "asgaerg/desktop-pet"
REL = (ROOT / "build" / "_release_id.txt").read_text(encoding="utf-8").strip()
LOG = ROOT / "build" / "upload.log"
PROXY_ARGS = ["--noproxy", "*"] if PROXY is None else ["--proxy", PROXY]

FILES = [
    ROOT / "release" / "DesktopPet-0.1.0-online-setup.exe",
    ROOT / "release" / "portable" / "DesktopPet-0.1.0-online-portable.zip",
]


def say(msg: str) -> None:
    print(msg, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(msg + "\n")


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(4 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


say("=" * 60)
for f in FILES:
    name = f.name
    size_mb = f.stat().st_size / 1024 / 1024
    local = sha256(f)
    say(f"[{time.strftime('%H:%M:%S')}] 上传 {name}  {size_mb:,.0f} MB  sha={local[:16]}…")
    t0 = time.time()

    r = subprocess.run(
        [
            "curl", "-s", "--progress-bar", "-X", "POST", *PROXY_ARGS,
            f"https://uploads.github.com/repos/{REPO}/releases/{REL}/assets?name={name}",
            "-H", f"Authorization: Bearer {TOKEN}",
            "-H", "Accept: application/vnd.github+json",
            "-H", "X-GitHub-Api-Version: 2022-11-28",
            "-H", "Content-Type: application/octet-stream",
            "--data-binary", f"@{f}",
        ],
        capture_output=True,
    )
    took = time.time() - t0
    out = r.stdout.decode("utf-8", "replace")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        say("   ❌ 返回不是 JSON：" + out[:300])
        continue

    if "id" not in data:
        say("   ❌ 失败：" + json.dumps(data, ensure_ascii=False)[:400])
        continue

    digest = (data.get("digest") or "").replace("sha256:", "")
    ok = digest == local if digest else None
    say(
        f"   ✅ 上传完成 id={data['id']}  {took / 60:.1f} 分钟  "
        f"{size_mb / (took or 1):.1f} MB/s"
    )
    if ok is True:
        say(f"   校验：GitHub digest 与本地一致 ({digest[:16]}…)")
    elif ok is False:
        say(f"   ⚠️ 校验不一致！本地 {local[:16]}… GitHub {digest[:16]}…")
    else:
        say("   校验：GitHub 没返回 digest，跳过比对")

say(f"[{time.strftime('%H:%M:%S')}] 全部结束")
