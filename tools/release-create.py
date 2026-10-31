"""建 GitHub Release，正文取自 docs/RELEASE-v<ver>.md。

用法：
    python tools/release-create.py 0.2.3
    python tools/release-create.py 0.2.3 --draft        # 建草稿，不公开
    python tools/release-create.py 0.2.3 --prerelease   # 标为预发布

Token 从环境变量 `GH_TOKEN` 读，**不写进文件、不落盘**。

## ⚠️ 关于代理

早期版本把所有请求硬编码走 `http://127.0.0.1:10809`（本地代理）。
换机器/代理没开时直接失败，而且报错是 `curl: (7) Failed to connect`
——看起来像"GitHub 挂了"。

现在改成：**先直连，失败再试代理**（`HTTPS_PROXY` / `HTTP_PROXY` 环境变量，
或显式 `--proxy`）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"
REPO = "asgaerg/desktop-pet"


def curl_json(
    method: str, url: str, token: str, payload_path: Path | None = None,
    proxy: str | None = None, timeout: int = 120,
) -> tuple[int, str]:
    """发请求，返回 (HTTP 码, 响应体)。直连失败才退回代理。"""
    base = [
        "curl", "-sS", "-w", "\nHTTP=%{http_code}", "-m", str(timeout),
        "-X", method, url,
        "-H", f"Authorization: Bearer {token}",
        "-H", "Accept: application/vnd.github+json",
        "-H", "X-GitHub-Api-Version: 2022-11-28",
    ]
    if payload_path is not None:
        base += ["-H", "Content-Type: application/json",
                 "--data-binary", f"@{payload_path}"]

    attempts: list[list[str]] = [base]
    if proxy:
        attempts.append(base[:1] + ["--proxy", proxy] + base[1:])

    last_err = ""
    for i, cmd in enumerate(attempts):
        r = subprocess.run(cmd, capture_output=True)
        out = r.stdout.decode("utf-8", "replace")
        if "HTTP=" in out:
            body, code_s = out.rsplit("\nHTTP=", 1)
            code = int(code_s.strip() or 0)
            # 连上了就给结果（哪怕 4xx，那是业务错误，不是网络问题）
            if code:
                return code, body
        last_err = (r.stderr or b"").decode("utf-8", "replace")[:200]
        if i + 1 < len(attempts):
            print(f"   直连失败（{last_err}），改用代理重试…")
    raise SystemExit(f"❌ 所有请求方式都失败：{last_err}")


def main() -> int:
    ap = argparse.ArgumentParser(description="创建 GitHub Release")
    ap.add_argument("version", help="版本号，如 0.2.3（不要带 v）")
    ap.add_argument("--draft", action="store_true", help="建成草稿")
    ap.add_argument("--prerelease", action="store_true", help="标为预发布")
    ap.add_argument("--title", default=None, help="Release 标题，默认自动生成")
    ap.add_argument("--notes-file", default=None, help="正文文件，默认 docs/RELEASE-v<ver>.md")
    ap.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"))
    ns = ap.parse_args()

    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        raise SystemExit("❌ 没设 GH_TOKEN 环境变量")

    ver = ns.version.lstrip("v")
    tag = f"v{ver}"
    notes = Path(ns.notes_file) if ns.notes_file else ROOT / "docs" / f"RELEASE-v{ver}.md"
    if not notes.is_file():
        raise SystemExit(f"❌ 找不到正文：{notes}")
    body = notes.read_text(encoding="utf-8")

    # 标题从正文第一行的 # 标题取，取不到就用默认
    title = ns.title
    if not title:
        for line in body.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
    if not title:
        title = f"DesktopPet {tag}"

    payload = {
        "tag_name": tag,
        "target_commitish": "main",
        "name": title,
        "body": body,
        "draft": bool(ns.draft),
        "prerelease": bool(ns.prerelease),
    }
    tmp = ROOT / "build" / f"_release-{ver}.json"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    print(f"  创建 Release {tag}（draft={ns.draft} prerelease={ns.prerelease}）")
    code, out = curl_json("POST", f"{API}/repos/{REPO}/releases", token, tmp, ns.proxy)
    if code == 422:
        # 已存在同名 tag/release 时 GitHub 返回 422
        print("  ⚠️ 422 —— 这个 tag 的 Release 可能已经存在：")
        try:
            msg = json.loads(out).get("errors") or json.loads(out).get("message")
            print("    ", msg)
        except Exception:
            print("    ", out[:300])
        print("  → 用 release-upload.py 往已有 Release 里传文件即可，或先在网页上删掉旧的。")
        return 1
    if code != 201:
        print("  ❌ HTTP", code)
        print(out[:800])
        return 1

    data = json.loads(out)
    print("  ✅ 已创建")
    print("     id  :", data["id"])
    print("     页面:", data["html_url"])
    (ROOT / "build" / "_release_id.txt").write_text(str(data["id"]), encoding="utf-8")
    tmp.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
