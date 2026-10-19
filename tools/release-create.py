"""建 Release v0.1.0，并把正文从 docs/RELEASE-v0.1.0.md 塞进去。

用到的 token 从环境变量 GH_TOKEN 读，不写进文件。
"""

import json
import os
import pathlib
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(r"F:\ai")
TOKEN = os.environ["GH_TOKEN"]
PROXY = "http://127.0.0.1:10809"
API = "https://api.github.com"
REPO = "asgaerg/desktop-pet"

body = (ROOT / "docs" / "RELEASE-v0.1.0.md").read_text(encoding="utf-8")

payload = {
    "tag_name": "v0.1.0",
    "target_commitish": "main",
    "name": "DesktopPet v0.1.0（可安装，但内容还没做完）",
    "body": body,
    "draft": False,
    "prerelease": True,
}
p = ROOT / "build" / "_release.json"
p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

r = subprocess.run(
    [
        "curl", "-s", "-w", "\nHTTP=%{http_code}", "--proxy", PROXY,
        "-X", "POST", f"{API}/repos/{REPO}/releases",
        "-H", f"Authorization: Bearer {TOKEN}",
        "-H", "Accept: application/vnd.github+json",
        "-H", "X-GitHub-Api-Version: 2022-11-28",
        "-H", "Content-Type: application/json",
        "--data-binary", f"@{p}",
    ],
    capture_output=True,
)
out = r.stdout.decode("utf-8", "replace")
code = out.rsplit("HTTP=", 1)[-1].strip()
print("  HTTP", code)
if code != "201":
    print(out[:1200])
    sys.exit(1)

data = json.loads(out.rsplit("\nHTTP=", 1)[0])
print("  ✅ Release 已创建")
print("   id:", data["id"])
print("   页面:", data["html_url"])
(ROOT / "build" / "_release_id.txt").write_text(str(data["id"]), encoding="utf-8")
