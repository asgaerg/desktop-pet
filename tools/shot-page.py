#!/usr/bin/env python3
"""通用页面截图工具（Electron 离屏渲染，2026-10-14 建，2026-10-16 重建）。

渲染层的透明窗口（状态条 / 对话条 / 气泡…）在真实桌面上受 DWM、主题、
背景影响，肉眼看"灰圈"说不清来源。这个工具把页面在**离屏** BrowserWindow
里渲染并 capturePage 成 PNG，得到的就是"页面自己画的东西"——
透明区在 Pillow 里读 alpha，窗口级伪影（DWM 边框）不在此列。

## 用法

    python tools/shot-page.py <html> <out.png> [width] [height] [theme] [key=value ...]

位置参数：
    html     要截的页面（src/renderer/*.html，相对项目根或绝对路径）
    out.png  输出 PNG 路径
    width    宽（默认 360）
    height   高（默认 240）
    theme    light / dark（默认 light，写进 <html data-theme=...>）

key=value 参数（顺序不限，放最后）：
    focus=选择器   截图前聚焦该元素；input/textarea 还会塞入 fill 文字
                   （模拟用户点进输入框的 :focus 态，2026-10-16 加）
    fill=文字      配合 focus 用的输入内容（不给且目标是输入框就用默认演示句）
    settle=毫秒    渲染后额外等待（默认 500，等字体/动画落定）
    preload=路径   给页面挂 preload 脚本（页面依赖 window.* 桥时用）

## ⚠️ 教训（2026-10-14）

往 executeJavaScript 的 JS 字符串里拼主题值，必须 `json.dumps()`（它产出的
就是合法 JS 字面量）。手写引号转义早晚会产出非法 JS，让 Electron 卡死。

## ⚠️ 环境注意

需要能跑 Electron（离屏渲染不需要真 GUI，但沙箱可能拦）。
electron.exe 依次找：ELECTRON 环境变量 → apps/desktop/node_modules → 根 node_modules。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = Path(__file__).resolve().parent / ".shot-page-runner.cjs"
DEFAULT_FILL = "你好，宠物！今天天气怎么样？"


def find_electron() -> Path:
    """定位 electron.exe（环境变量 → 两个 node_modules 位置）。"""
    env = Path(sys.argv[0]).anchor  # noqa: F841 —— 占位，真正找法在下面
    candidates = []
    override = __import__("os").environ.get("ELECTRON")
    if override:
        candidates.append(Path(override))
    for base in (ROOT / "apps" / "desktop", ROOT):
        exe = base / "node_modules" / "electron" / "dist" / "electron.exe"
        if exe.is_file():
            candidates.append(exe)
    for exe in candidates:
        if exe.is_file():
            return exe
    raise SystemExit("找不到 electron.exe —— 用 ELECTRON 环境变量指一下")


def build_runner(cfg: dict) -> None:
    """生成离屏截图 runner（一次性写到 tools/.shot-page-runner.cjs）。

    ⚠️ 所有动态值（主题、选择器、文字）一律 json.dumps 注入 —— 见文件头教训。
    ⚠️ 唯一例外是 js= 的用户代码：**原样拼进代码体**（见 runner 里那段说明，
    new Function 会被页面 CSP 拒掉）。所以 js= 里不要出现反引号。
    """
    js = f"""// 由 shot-page.py 生成，勿手改。
const {{ app, BrowserWindow }} = require('electron');
const fs = require('fs');

const CFG = {json.dumps(cfg)};

app.disableHardwareAcceleration();

app.whenReady().then(async () => {{
  try {{
    const win = new BrowserWindow({{
      width: CFG.width,
      height: CFG.height,
      show: false,
      frame: false,
      transparent: true,
      resizable: false,
      webPreferences: {{
        offscreen: true,
        contextIsolation: true,
        nodeIntegration: false,
        preload: CFG.preload || undefined,
      }},
    }});
    await win.loadFile(CFG.html);

    await win.webContents.executeJavaScript(`(() => {{
      const THEME = ${{JSON.stringify(CFG.theme)}};
      const FOCUS = ${{JSON.stringify(CFG.focus || '')}};
      const FILL = ${{JSON.stringify(CFG.fill || '')}};
      if (THEME) document.documentElement.setAttribute('data-theme', THEME);
      if (FOCUS) {{
        const el = document.querySelector(FOCUS);
        if (!el) return 'focus-miss:' + FOCUS;
        el.focus();
        if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {{
          el.value = FILL;
          el.dispatchEvent(new Event('input', {{ bubbles: true }}));
        }}
      }}
      return 'ok';
    }})()`)
      .then(async () => {{
        /*
         * 🔴 用户 JS（CFG.jsFile）**读文件后执行**，绝不拼进本文件源码：
         * 之前是原样拼进这个模板字符串 —— 用户代码里的 `\\n` 等转义会被
         * **外层模板字面量**先解释成真实换行，到页面里就成了
         * "单引号字符串里出现换行"的语法错误（实测踩到）。
         * executeJavaScript 拿运行时字符串没这个坑，也不受页面 CSP 管。
         * ⚠️ 因此用户代码是**顶层脚本**，不能有 `return`。
         */
        if (CFG.jsFile) {{
          const userJs = fs.readFileSync(CFG.jsFile, 'utf8');
          await win.webContents.executeJavaScript(userJs);
        }}
        await new Promise((r) => setTimeout(r, CFG.settle));
        const image = await win.webContents.capturePage();
        fs.writeFileSync(CFG.out, image.toPNG());
        console.log('OK ' + CFG.out);
        app.exit(0);
      }})
      .catch((err) => {{
        console.error('SHOT-FAIL ' + (err && err.stack || err));
        app.exit(1);
      }});
  }} catch (err) {{
    console.error('SHOT-FAIL ' + (err && err.stack || err));
    app.exit(1);
  }}
}});

setTimeout(() => {{
  console.error('SHOT-FAIL 超时（90 秒没截到）');
  app.exit(2);
}}, 90_000);
"""
    RUNNER.write_text(js, encoding="utf-8")


def main(argv: list[str]) -> int:
    args = [a for a in argv if "=" not in a or a.startswith("-")]
    opts = dict(a.split("=", 1) for a in argv if "=" in a and not a.startswith("-") and a.index("=") > 0)
    if len(args) < 2:
        print(__doc__)
        return 2

    user_js = str(opts.get("js", "") or "")
    html = Path(args[0])
    if not html.is_absolute():
        html = ROOT / html
    if not html.is_file():
        print(f"页面不存在：{html}")
        return 2
    out = Path(args[1])
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)

    def num(value: str, key: str, default: int) -> int:
        if value is None or value == "":
            return default
        try:
            return int(value)
        except ValueError:
            print(f"⚠️ {key}={value!r} 不是数字，用默认 {default}")
            return default

    # 位置参数：html out [width] [height] [theme]；key=value 可覆盖任意一项
    width = num(opts.get("width", args[2] if len(args) > 2 else ""), "width", 360)
    height = num(opts.get("height", args[3] if len(args) > 3 else ""), "height", 240)
    theme = opts.get("theme", args[4] if len(args) > 4 else "light")
    focus = opts.get("focus", "")
    fill = opts.get("fill", DEFAULT_FILL if focus else "")
    settle = num(opts.get("settle", ""), "settle", 500)
    preload = opts.get("preload", "")
    # js= 任意页面内 JS（截完图前执行，返回值会打到输出里便于断言）
    user_js = opts.get("js", "")
    if preload and not Path(preload).is_absolute():
        preload = str(ROOT / preload)

    # 用户 JS 写独立文件（运行时由 runner 读取执行 —— 为什么不拼源码见 runner 注释）
    user_js = str(opts.get("js", "") or "")
    USER_JS_FILE = RUNNER.parent / ".shot-page-user.js"
    if user_js:
        USER_JS_FILE.write_text(user_js, encoding="utf-8")

    cfg = {
        "html": str(html),
        "out": str(out),
        "width": width,
        "height": height,
        "theme": theme,
        "focus": focus,
        "fill": fill,
        "settle": settle,
        "preload": preload,
        "jsFile": str(USER_JS_FILE) if user_js else "",
    }
    build_runner(cfg)

    exe = find_electron()
    t0 = time.time()
    # ⚠️ 沙箱/终端可能带 ELECTRON_RUN_AS_NODE=1（此时 electron 退化成纯 Node，
    # require('electron') 是 undefined）。截图必须走真 Electron —— 显式清掉。
    env = {k: v for k, v in __import__("os").environ.items() if k != "ELECTRON_RUN_AS_NODE"}
    proc = subprocess.run(
        [
            str(exe),
            # 沙箱/CI 里 GPU 进程常直接崩（0xC0000005）—— 离屏截图用不着 GPU；
            # --no-sandbox：受限环境里 chrome 沙箱起不来（只加载本地文件，可接受）
            "--disable-gpu",
            "--disable-gpu-compositing",
            "--no-sandbox",
            str(RUNNER),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    elapsed = time.time() - t0

    if proc.returncode != 0 or not out.is_file():
        print(f"截图失败（exit={proc.returncode}，{elapsed:.1f}s）")
        if proc.stdout.strip():
            print("stdout:", proc.stdout.strip()[-800:])
        if proc.stderr.strip():
            print("stderr:", proc.stderr.strip()[-800:])
        return 1
    print(f"OK {out}（{out.stat().st_size} 字节，{elapsed:.1f}s，theme={theme}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
