"""MCP 搜索客户端 —— 把 `websearch-mcp.exe` 当联网搜索的后端（2026-10-15）。

## 这是什么

桌宠的 `web_search` 工具原来自己抓 HTML（`pet/web.py`，引擎 = DDG/Bing/360）。
现在多了一条路：把搜索交给一个 **MCP 服务器**去做。

```
pet/tools.py  _web_search()
      │
      ├─ mcp_search.search()  ──HTTP──>  websearch-mcp.exe  ──>  360 / Bing / ...
      │      （优先，可关）
      └─ web.search()         自己抓 HTML（退路）
```

## 🔴 为什么要用 MCP，而不是继续自己抓

自己抓 HTML 有**三个天然的坑**，MCP 那条路一次解决：

```
① 解析规则随改版失效，而且是**静默** 0 条（§102 花了两轮才查清 Bing 的问题）
② 没有多引擎融合 —— 单引擎返回垃圾时我们没有客观判据识别
③ 输出是原始 HTML 片段，模型要自己从噪声里找答案
```

那个服务器带 **Wigolo 评分增强 + MMR 多样性去重 + 相关性分数**，
输出的结构（标题/url/来源/相关性/内容）对模型友好得多。

## 🔴 为什么不用官方的 `mcp` Python SDK

实测（2026-10-15）：

```
mcp 2.3.0 的 streamable_http_client：
  握手 ✅   tools/list ✅   tools/call ✅
  但退出时发 session termination -> 服务器回 503
  -> 异常在 TaskGroup 里冒出来，把整个 async with 块染红
  -> **一个结果都拿不到**
```

⚠️ 而且它在 2.3.0 里改了名（`streamablehttp_client` -> `streamable_http_client`）。
所以这里**手写 JSON-RPC**（就四个请求），换来的是完全可控 + 零依赖。

## MCP Streamable HTTP 的四个请求

```
① POST initialize                  -> 响应头 mcp-session-id
② POST notifications/initialized   （无 id）
③ POST tools/list
④ POST tools/call                  {"name":"smartsearch","arguments":{"query":...}}
```

⚠️ 三个必须做对的细节（写错了症状都是「连不上」）：
  1. `Accept` 要**同时**给 `application/json` 和 `text/event-stream`
  2. ②③④ 都要带 `mcp-session-id` 头
  3. 回包可能是 SSE（`data: {...}`），也可能直接是 JSON —— 两种都要解

## 实测性能（2026-10-15，不开 VPN）

```
'天官赐邪 作者'   1.5s  ✅ 100% 命中   来源 so360
'幻梦猎人 新书'   1.5s  ✅ 100%
'银松镇 游戏'     1.6s  ✅ 100%
'北京今天天气'    1.6s  ✅ 100%
```

对比自己抓 HTML 的 `pet/web.py`：**1.5s vs 10.8s**（快 7 倍）。
"""

from __future__ import annotations

from . import netutil, paths

import json
import logging
import os
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("pet.mcp_search")

# MCP 协议版本。⚠️ 要和服务器的 `protocolVersion` 对得上 ——
# 服务器实测回的是 `2025-06-18`。写错的话握手会被拒。
PROTOCOL_VERSION = "2025-06-18"

# 默认端口。`websearch-mcp` 自己的默认值是 8338，两边必须一致。
DEFAULT_URL = "http://127.0.0.1:8338/mcp"

# 单次调用的超时。实测单查询 1.5 秒，给足余量；
# ⚠️ 但也不能太长 —— 搜索在生成回复的路径上，用户等着。
DEFAULT_TIMEOUT_S = 20.0

# 服务器可执行文件的候选位置（按顺序找第一个存在的，**源**位置）。
#
# 🔴 为什么要多个位置：开发时它编译在 `tools/goruntime/`，
# 打包后应该跟 Python 包放在一起。写死一个位置的话，
# 打包出来会「找不到 exe」，而那个失败是**静默**的（退回到抓 HTML）。
_SOURCE_CANDIDATES = (
    # ① 跟包放一起（打包后的形态）
    Path(__file__).resolve().parent / "websearch-mcp.exe",
        paths.project_root() / "pet" / "websearch-mcp.exe",
        paths.project_root() / "apps" / "brain" / "pet" / "websearch-mcp.exe",
    # ② 开发时的编译产物
    Path(__file__).resolve().parents[3] / "tools" / "goruntime" / "websearch-mcp.exe",
    # ③ 允许用环境变量覆盖（排查/换版本用）
    Path(os.environ.get("PET_MCP_SEARCH_EXE", "") or "__none__"),
)


def runtime_dir() -> Path:
    """服务器的工作目录（**唯一**允许它写东西的地方）。

    ## 🔴 为什么要单独开一个目录（2026-10-15，踩得很惨）

    `websearch-mcp.exe` 是个"会到处拉屎"的程序。它启动时会在
    **exe 所在目录**和**工作目录**里建这些：

    ```
    dashboard.yaml              含一个管理口令的配置文件
    .websearch.pid              进程状态
    websearch.log               日志
    data/dashboard.db           遥测 SQLite 库
    websearch-green.ico         快捷方式图标
    websearch-shortcut.theme    快捷方式主题
    + 桌面上一个「WebSearchMCP」快捷方式
    ```

    ⚠️ 后果有三个，都很难看：

    ```
    ① 把 exe 放进 `apps/brain/pet/` 之后，**产品源码目录被这些文件污染** ——
       用户打开项目看到一堆不属于项目的东西
    ② 它第一步要 `mkdir data`，**在那个目录不可写时整个服务器起不来**，
       报的却是 "Access is denied"（和"配置错了"长得一模一样，很难查）
    ③ 桌面上凭空多个图标，用户不知道那是什么
    ```

    ⚠️ 所以：**把 exe 复制到一个专门的运行目录，让它在那里随便写**。
    这个目录在 `pet-data/` 下面 —— 那本来就是"运行时会变的数据"的归处，
    而且**打包时要排除**（见路线图 ⑥）。
    """
    override = os.environ.get("PET_MCP_SEARCH_DIR", "")
    if override:
        return Path(override)
    return paths.mcp_runtime_dir()


def find_exe() -> Optional[Path]:
    """找到**可用**的服务器可执行文件。

    ⚠️ 优先用运行目录里的 —— 如果那里已经有一份，就不用再复制
    （32 MB，每次启动复制一遍会拖慢启动）。
    """
    staged = runtime_dir() / "websearch-mcp.exe"
    try:
        if staged.is_file():
            return staged
    except OSError:
        pass
    for candidate in _SOURCE_CANDIDATES:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _stage_runtime_dir() -> Optional[Path]:
    """把 exe 复制到运行目录，并在那里写好配置文件。返回 exe 的路径。

    ⚠️ 只在「运行目录里还没有」或「源文件变了（大小不同）」时复制。
    每次都复制的话，32 MB 的 IO 会明显拖慢冷启动。
    """
    target_dir = runtime_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("建不出 MCP 运行目录 %s：%s", target_dir, exc)
        return None

    # 找源 exe（跳过运行目录自己，否则会自己复制自己）
    source: Optional[Path] = None
    for candidate in _SOURCE_CANDIDATES:
        try:
            if candidate.is_file() and candidate.parent != target_dir:
                source = candidate
                break
        except OSError:
            continue
    if source is None:
        log.warning("找不到 MCP 搜索服务器的可执行文件，检查过这些位置：%s",
                    [str(item) for item in _SOURCE_CANDIDATES])
        return None

    target_exe = target_dir / "websearch-mcp.exe"
    try:
        needs_copy = True
        if target_exe.is_file() and target_exe.stat().st_size == source.stat().st_size:
            needs_copy = False
        if needs_copy:
            log.info("把 MCP 搜索服务器复制到运行目录（%.1f MB）…",
                     source.stat().st_size / 1024 / 1024)
            shutil.copy2(source, target_exe)
    except OSError as exc:
        log.warning("复制 MCP 搜索服务器失败：%s", exc)
        return None

    # 配置文件：**由我们自己生成**，不依赖 `config.pet.yaml`。
    #
    # ⚠️ 为什么不复制现成的配置：那样配置的来源就有两处（仓库里的文件
    #    和这个生成的文件），改了一处另一处不生效 —— 又一个"假开关"。
    #    生成的话，**唯一的真相在代码里**，测试也能直接读它。
    try:
        (target_dir / "websearch-mcp.config.yaml").write_text(
            _CONFIG_YAML, encoding="utf-8"
        )
        # 🔴 这个也必须在运行目录里 —— 否则它会重新生成一份
        #    `enabled: true` 的默认配置，把我们关掉的遥测又打开。
        (target_dir / "dashboard.yaml").write_text(
            _DASHBOARD_YAML, encoding="utf-8"
        )
    except OSError as exc:
        log.warning("写 MCP 配置文件失败：%s", exc)
        return None

    return target_exe


"""
## 配置文件的内容（唯一真相在代码里）

⚠️ 两个关键选择，都写在注释里了，改的时候照着理由改。
"""
_CONFIG_YAML = """\
# 由 `pet/mcp_search.py` 生成 —— **不要手改**（下次启动会被覆盖）。
#
# 改引擎开关请改这里（`_CONFIG_YAML`），那才是唯一真相。

port: 8338
log_level: warn
mode: engine
network: china
black_list_host: []

rate_limit:
  per_sec: 3
  per_min: 60

# 360 搜索 —— **主力**。零 Key、国内直连、不用代理。
# 实测 6/6 查询相关，单次 1.5 秒。
so360:
  enabled: true

# Bing —— 备用。⚠️ 实测对带第二个词的中文查询经常返回无关结果
# （HTTP 200、结果结构正常，但内容是字典页 —— 静默的错）。
bing:
  enabled: true

# DuckDuckGo —— 质量最好，但**国内直连不通**（服务器自己也会跳过它）。
# 开着无害：有代理时它会自动生效。
duckduckgo:
  enabled: true

# 百度 —— 服务器自己标注「被反爬识别 CAPTCHA」，会自己禁用。
baidu:
  enabled: true

# Google —— 被反爬拦，项目自己标注暂不可用。
google:
  enabled: false

# 国内直连超时。
wikipedia:
  enabled: false
google_news:
  enabled: false

# 学术搜索跟桌宠无关，关掉省启动时间。
academic:
  enabled: false

# 🔴 **显式禁用代理** —— 我们要的就是"不开 VPN 也能用"。
# 留空或不设才是"自动检测系统代理"。
proxy:
  enabled: false
  api_providers: false

# 桌宠自己已有 `web_fetch` 工具，这里的抓取关掉避免重复。
cleanfetch:
  enabled: false
  timeout_sec: 30
  use_system_proxy: false

pdf_parser:
  enabled: false

cache:
  cleanup_interval: 30

log:
  max_size: 1
  max_age: 1
"""

_DASHBOARD_YAML = """\
# 由 `pet/mcp_search.py` 生成 —— **不要手改**。
#
# 🔴 控制中心必须**关掉**，理由见 `mcp_search.runtime_dir()` 的文档：
#    · 它会建 `data/dashboard.db`，而那个 mkdir 失败会让**整个服务器起不来**
#    · 它会生成带**管理口令**的配置
#    · 它会在**桌面建快捷方式**
#
# 桌宠要的只是一个搜索后端，这些既用不上又添乱。
dashboard:
  enabled: false
  shortcut: off
  storage_path: ""
"""


def _detached_kwargs() -> dict[str, Any]:
    """让子进程**脱离父进程**，父进程不等它。

    ## 🔴 为什么必须 detached（这一条踩了两次）

    `websearch-mcp.exe start` **不是**一个"启动完就返回"的命令 ——
    它**在前台运行整个服务器**，永远不退出。

    ```
    第一版写法：subprocess.run(args, capture_output=True, timeout=20)
      -> 等它退出 -> 永远等不到 -> TimeoutExpired
      -> 而服务器其实**已经起来了**（只是被当成失败了）
    ```

    ⚠️ 这个错误特别有欺骗性：日志里写的是「启动失败」，
    但 `curl` 一下端口发现服务好好地跑着。

    ⚠️ `capture_output=True` 还有第二个坑：管道要被**所有**继承了它的进程关闭
    才会读到 EOF。服务器是长命的，所以父进程读管道会一直挂着 ——
    就算不等 `run()` 返回，`communicate()` 也不会返回。

    所以：**输出丢弃、不建管道、进程脱离**。
    要日志的话服务器自己写 `websearch.log`（比管道可靠）。
    """
    if os.name != "nt":
        # POSIX：新会话，脱离控制终端
        return {"start_new_session": True,
                "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}

    # Windows：CREATE_NO_WINDOW 让控制台程序**不开窗口**（2026-10-16 修：
    # 用户实测 free-search-mcp.exe 拉起时弹了个黑框）。CREATE_NEW_PROCESS_GROUP
    # 让它不接收父进程的 Ctrl+C。
    #
    # ⚠️ 之前这里还加了 DETACHED_PROCESS —— 但它和 CREATE_NO_WINDOW
    # **互斥**（MSDN：两者不能同用，行为未定义），实测组合的结果就是
    # 控制台窗口照样弹出来。所以去掉 DETACHED_PROCESS：
    # CREATE_NO_WINDOW 一个就够（子进程共享父进程的（无）控制台）。
    flags = 0
    for name in ("CREATE_NO_WINDOW", "CREATE_NEW_PROCESS_GROUP"):
        flags |= getattr(subprocess, name, 0)
    return {
        "creationflags": flags,
        # 🔴 必须指向 DEVNULL 而不是管道 —— 管道会被长命子进程一直占着
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }


def _server_process_ids() -> list[int]:
    """列出所有在跑的 `websearch-mcp.exe` 的 pid。

    ## 🔴 为什么不问服务器自己的 `status` 命令

    `status` 会**在每个需要它的地方再起一个进程、再读一次配置**，
    而我们要的只是"有没有残留"。代价和复杂度都不划算。

    ⚠️ 实现上刻意用 ctypes 而不是 `tasklist` 或 PowerShell：
    后两者要**起一个子进程 + 建管道读输出**，而 sidecar 的启动路径上
    不该再套一层进程（§103 的教训：管道没关干净会让父进程一直挂着）。
    ctypes 是纯 API 调用，零子进程。

    ⚠️ 全平台都有兜底：拿不到就返回空列表（当成"没有残留"）。
    拿不到的时候退化成"不复用、每次重新拉起"，功能不受影响。
    """
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == INVALID_HANDLE_VALUE:
            return []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            found: list[int] = []
            ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                if str(entry.szExeFile).lower() == "websearch-mcp.exe":
                    found.append(int(entry.th32ProcessID))
                ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            return found
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception as exc:  # noqa: BLE001
        log.debug("枚举 websearch-mcp 进程失败（当成没有残留）：%s", exc)
        return []


def _reap_orphans(timeout_s: float = 6.0) -> int:
    """杀掉所有在跑的 `websearch-mcp.exe`，返回杀掉的个数。

    ## 🔴 为什么必须"杀"而不是"复用"

    第一版是"如果端口在响应就复用它"（`probe()` 短路）。看着聪明，其实错了：

    ```
    sidecar 崩溃 -> 服务器留下（引用计数 1）
    新 sidecar 起来 -> probe() 说"活着" -> 跳过启动
      -> 但**新 sidecar 从没 start 过**，所以它退出时的 stop 会把计数减到 0
      -> 服务器在**还有另一个 sidecar 认为它在跑**的时候死掉
    ```

    ⚠️ 更隐蔽的是：复用意味着我们**继承了别人的引用计数**，
    而这个计数是多少我们根本不知道（可能崩了 5 次，计数就是 5）。
    那 `stop` 一次永远减不到 0。

    ## 所以：把"引用计数"换成"自己管进程"

    ```
    启动：杀掉所有残留（不管计数是几）-> 自己拉起一个 -> 计数必然从 0 开始
    退出：杀掉自己那个
    ```

    ⚠️ 这样**永远是干净的** —— 崩溃、断电、任务管理器硬杀，
    下次启动一律清干净。不依赖任何外部状态。
    """
    pids = _server_process_ids()
    if not pids:
        return 0
    log.info("发现 %d 个残留的 websearch-mcp 进程 %s，清掉", len(pids), pids)
    killed = 0
    for pid in pids:
        try:
            if os.name == "nt":
                # ⚠️ 只按 pid 杀，**绝不按进程名** —— 本项目有"按名字杀"
                # 误伤自己的前科（见交接文档里的自伤警告）。
                #
                # 🔴 这里**不能**再传 capture_output ——
                #    `_detached_kwargs()` 已经给了 stdout/stderr=DEVNULL，
                #    两个一起传会 `ValueError`，而那个异常会被下面的 except 吞掉
                #    -> 一个进程都杀不掉，且日志上只有一行 DEBUG。
                #    （2026-10-15 实测踩到：表现为"退出后服务器还在跑"。）
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    timeout=10,
                    **_detached_kwargs(),
                )
            else:
                os.kill(pid, 15)
            killed += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("清 websearch-mcp pid %s 失败：%s", pid, exc)

    # 等它们真的退掉 —— 不等的话新实例会和正在退出的旧实例抢 8338
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _server_process_ids():
            break
        time.sleep(0.3)
    return killed


"""
## 我们自己声明的那个服务器实例的 pid

⚠️ 为什么要记这个：`ensure_running()` 每次都要判断
「已经跑着的这个是不是**我的**」。

```
是"我的"        -> 直接复用（0.01 秒）
是"别人的/孤儿"  -> 清掉重新声明（1 秒）
```

⚠️ 光看端口通不通**不够**：端口可能在响应一个孤儿实例，
而我们并没有为它声明过引用计数。

⚠️ 进程重启后这个变量会重置为 None —— 那正是我们要的：
新 sidecar 不知道旧实例的来历，所以走"清掉重新声明"这条路（安全）。
"""
_owned_pid: Optional[int] = None


def _owns_live_server() -> bool:
    """我们记着的那个实例还活着吗。"""
    if _owned_pid is None:
        return False
    return _owned_pid in _server_process_ids()


def ensure_running(timeout_s: float = 25.0) -> bool:
    """确保服务器在跑，而且是**我们自己声明的那个实例**。

    ## 判据

    ⚠️ 判据是**能不能完成 MCP 握手**，不是"进程在不在"，也不是退出码。
    这一点必须这样：`start` 压根不退出，
    第一版就是在退出码上踩的坑 —— 日志写着"启动失败"，其实已经跑起来了。

    ## 🔴 为什么"不是我的就清掉重来"

    这个 exe **没有**"跑服务器本体"的子命令（试过 `serve` / `run` / `server`，
    全部返回 usage）—— 只有 `start`，而 `start` 是**引用计数**的：
    计数存在它自己的状态文件里，`start` +1、`stop` -1、归零才退出。

    引用计数本身没错，错的是**它和我们的生命周期对不上**：

    ```
    sidecar 崩溃后会自动重启（§103：主进程监听 ready 事件重连）
      -> 每次重启 start 一次（+1）
      -> 而 stop 只在**最终优雅退出**时调一次（-1）
      -> 计数只增不减 -> 永远减不到 0
      -> **关掉 App 之后后台还挂着 websearch-mcp.exe**
      -> 用户发现"这个文件夹删不掉"（文件被占用），却不知道那是什么
    ```

    ⚠️ 硬杀（崩溃、任务管理器结束进程、断电）**根本不会**执行 `stop`，
    所以这个问题一定会发生，不是"万一"。

    ## 解法：把自己的计数**归零**再声明

    ```
    启动：kill（强制清零，不管之前是几）-> start（计数必然 = 1）
    退出：stop（计数 -> 0）
    ```

    ⚠️ 于是"活着的计数"永远只反映**当前这一个 sidecar** ——
    崩溃多少次都不累积。**自愈**。

    ⚠️ 代价：如果用户自己另外 `websearch-mcp start` 起过实例，
    我们启动时会把它清掉。这是刻意的取舍 ——
    本机上除了桌宠没有别的调用方，而**孤儿进程占着 8338**
    会让下次启动的服务器直接失败，那个症状比"收掉一个陌生实例"难查得多。
    """
    global _owned_pid

    # ⓪ 已经是我自己的实例在跑 -> 直接复用，一次系统调用都不多做
    if _owns_live_server() and probe(timeout_s=1.5):
        return True

    exe = _stage_runtime_dir()
    if exe is None:
        return False

    work_dir = exe.parent
    config = work_dir / "websearch-mcp.config.yaml"
    args_base = [str(exe)]
    if config.is_file():
        args_base += ["-c", str(config)]
    else:
        log.warning("配置文件不在（%s），服务器会用内置默认值", config)

    # ① 强制清零（异步发出 + 等它真的退掉）
    _reap_orphans()
    _owned_pid = None
    try:
        subprocess.Popen(args_base + ["kill"], cwd=str(work_dir), **_detached_kwargs())
    except Exception as exc:  # noqa: BLE001
        log.debug("发 kill 时出错（继续）：%s", exc)

    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if not probe(timeout_s=0.6) and not _server_process_ids():
            break
        time.sleep(0.4)

    # ② 声明（计数从 0 变成 1）
    try:
        subprocess.Popen(args_base + ["start"], cwd=str(work_dir), **_detached_kwargs())
        log.info("已拉起 MCP 搜索服务器（%s），等它就绪…", exe)
    except Exception as exc:  # noqa: BLE001
        log.warning("启动 MCP 搜索服务器出错：%s", exc)
        return False

    # ③ 轮询到握手成功，并**认领**这个实例
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        # ⚠️ 认领要在"握手成功"之后（说明它真的服务起来了），
        #    而且取的是**当前**那批 pid —— 中途换了进程也不会认错
        pids = _server_process_ids()
        if pids and probe(timeout_s=1.5):
            _owned_pid = pids[0]
            log.info("MCP 搜索服务器已就绪（pid %s）", _owned_pid)
            return True
        time.sleep(0.5)
    log.warning("MCP 搜索服务器启动后 %s 秒内仍不可用（运行目录 %s）", timeout_s, work_dir)
    return False


def stop() -> None:
    """收掉服务器。退出时调。

    ⚠️ **两种方式都做**，因为它们各自会漏：

    ```
    stop 命令    引用计数 -1 —— 但计数可能不是 1（崩溃过），于是减不到 0
    按 pid 杀   一定能杀掉 —— 但会留下"计数文件"，下次 start 从错误的值开始
    ```

    先 `stop`（把计数清成 0，让状态文件干净），再 `_reap_orphans()`
    （兜住计数不匹配导致没退掉的情况）。

    ⚠️ 这里是**唯一**该按进程名杀的地方 —— `mcp_search` 是它唯一的调用方，
    而且 `websearch-mcp.exe` 这个名字不会撞上别的程序。
    （别处不能这么干：本项目有"按名字杀误伤自己"的前科。）
    """
    exe = runtime_dir() / "websearch-mcp.exe"
    if not exe.is_file():
        found = find_exe()
        if found is None:
            return
        exe = found
    work_dir = exe.parent
    args = [str(exe)]
    config = work_dir / "websearch-mcp.config.yaml"
    if config.is_file():
        args += ["-c", str(config)]

    # ① 先把引用计数减掉（让它的状态文件回到 0）
    try:
        subprocess.Popen(args + ["stop"], cwd=str(work_dir), **_detached_kwargs())
    except Exception as exc:  # noqa: BLE001
        log.debug("发 stop 时出错（继续按 pid 清）：%s", exc)

    # ② 给它一点时间自己退；退不掉就按 pid 清
    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline:
        if not probe(timeout_s=0.5):
            break
        time.sleep(0.3)

    killed = _reap_orphans(timeout_s=3.0)
    if killed:
        log.info("已按 pid 收掉 %d 个 MCP 搜索服务器进程", killed)
    global _owned_pid
    _owned_pid = None


def probe(timeout_s: float = 2.0, url: str = DEFAULT_URL) -> bool:
    """服务器活着吗（能完成一次 MCP 握手就算活着）。"""
    try:
        client = McpClient(url, timeout_s=timeout_s)
        return client.initialize()
    except Exception:  # noqa: BLE001
        return False


class McpError(RuntimeError):
    """MCP 调用失败。消息里**必须**说清是哪一步坏的。"""


class McpClient:
    """一个极简的 MCP Streamable HTTP 客户端。

    ⚠️ 刻意只支持「初始化 + 列工具 + 调工具」这三件事 ——
    我们不需要 prompts / resources / sampling，多写只会多坏。
    """

    def __init__(self, url: str = DEFAULT_URL, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.url = url
        self.timeout_s = timeout_s
        self.session_id: Optional[str] = None
        self._next_id = 1
        self.server_name: str = ""

    # ---------------------------------------------------------------- HTTP

    def _post(self, payload: Optional[dict], timeout_s: Optional[float] = None):
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        headers = {
            "Content-Type": "application/json",
            # 🔴 两个 Accept 都要给 —— 服务器可能用 SSE 回包，
            # 只给 application/json 会被拒（406）或收不到内容
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "User-Agent": "desktop-pet/0.1 (mcp-search)",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id

        request = urllib.request.Request(
            self.url, data=body, headers=headers, method="POST"
        )
        timeout = timeout_s if timeout_s is not None else self.timeout_s
        try:
            # 本地 127.0.0.1 的 MCP 服务：**强制直连**。
            # 系统代理若不排除 localhost，请求会被丢给代理（连不上/慢）。
            with netutil.urlopen(request, timeout=timeout, force_direct=True) as response:
                return response.status, dict(response.headers), response.read().decode(
                    "utf-8", "replace"
                )
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            raise McpError(f"HTTP {exc.code}：{text[:200]}") from exc
        except Exception as exc:  # noqa: BLE001
            raise McpError(f"{type(exc).__name__}：{exc}") from exc

    def _call(self, method: str, params: Optional[dict] = None,
              with_id: bool = True) -> dict:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if with_id:
            payload["id"] = self._next_id
            self._next_id += 1
        payload["params"] = params or {}
        status, headers, text = self._post(payload)

        # 会话 id 只在 initialize 的响应头里给一次
        for key, value in headers.items():
            if key.lower() == "mcp-session-id":
                self.session_id = value

        if not with_id:
            return {}
        parsed = _parse_mcp_body(text)
        if parsed is None:
            raise McpError(f"{method} 的响应解析不了（前 200 字）：{text[:200]}")
        if parsed.get("error"):
            raise McpError(f"{method} 报错：{json.dumps(parsed['error'], ensure_ascii=False)[:200]}")
        return parsed.get("result") or {}

    # ------------------------------------------------------------- 协议

    def initialize(self) -> bool:
        result = self._call(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "desktop-pet", "version": "0.1"},
            },
        )
        self.server_name = ((result.get("serverInfo") or {}).get("name")) or ""
        # 通知：我初始化完了。**无 id**，服务器回 202。
        self._call("notifications/initialized", {}, with_id=False)
        return bool(self.server_name or result)

    def list_tools(self) -> list[dict]:
        return self._call("tools/list").get("tools") or []

    def call_tool(self, name: str, arguments: dict) -> str:
        result = self._call("tools/call", {"name": name, "arguments": arguments})
        pieces: list[str] = []
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                pieces.append(block.get("text") or "")
        if result.get("isError"):
            raise McpError(f"工具 {name} 返回错误：{''.join(pieces)[:200]}")
        return "".join(pieces)


def _parse_mcp_body(text: str) -> Optional[dict]:
    """解析 MCP 回包。

    ⚠️ 可能是 SSE（`event: message` + `data: {...}`），也可能直接是 JSON。
    两种都要支持 —— 实测这个服务器两种都用过（取决于请求）。
    """
    text = text.strip()
    if not text:
        return None
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("data:"):
            chunk = stripped[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------- 对外接口

def _search_once(
    query: str,
    *,
    url: str,
    timeout_s: float,
    fetch_top_n: int,
) -> str:
    """搜一次，**不做恢复**。失败抛 `McpError`。"""
    client = McpClient(url, timeout_s=timeout_s)
    if not client.initialize():
        raise McpError("MCP 握手失败（服务器没报出自己的名字）")

    tools = client.list_tools()
    names = [item.get("name") for item in tools if isinstance(item, dict)]
    if "smartsearch" in names:
        tool_name = "smartsearch"
    elif "web_search" in names:
        tool_name = "web_search"
    elif names:
        tool_name = names[0]
    else:
        raise McpError("服务器没提供任何工具")

    arguments: dict[str, Any] = {"query": query}
    # smartsearch 支持对前 N 条抓正文。默认 0（不抓）——
    # 抓正文会让单次搜索从 1.5 秒涨到十几秒，由调用方决定要不要。
    if tool_name == "smartsearch" and fetch_top_n:
        arguments["fetch_top_n"] = max(0, min(5, int(fetch_top_n)))

    text = client.call_tool(tool_name, arguments)
    log.info(
        "mcp_search q=%r tool=%s -> %d 字（服务器 %s）",
        query, tool_name, len(text), client.server_name or "?",
    )
    return text


def search(
    query: str,
    limit: int = 6,
    *,
    url: str = DEFAULT_URL,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    fetch_top_n: int = 0,
) -> str:
    """用 MCP 服务器搜一次，返回**给模型读的文本**。

    ⚠️ 这是**同步阻塞**函数（urllib）—— 调用方要用
    `asyncio.to_thread(...)`，否则会卡住整个 sidecar（连语音都停）。
    这条规则和 `pet/web.py` 一样，那边也踩过。

    ⚠️ 失败一律抛 `McpError`，**消息里带上是哪一步坏的**
    （握手 / 列工具 / 调用）。含糊的失败会让调用方只能"猜着重试"。

    ## 🔴 连不上时会自动重启服务器并重试一次（2026-10-15 实测补）

    **实测抓到的问题**：App 开着的时候，服务器**中途自己退了**
    （22:01:05 还在正常服务，22:03 就没了 —— 而侧车和 Electron 都活着）。

    ```
    现象：这一刻起，本次会话的联网搜索**永久降级**成自己抓 HTML
          1.5 秒 -> 18.5 秒，而且**没有任何提示**
    原因：`search()` 原来只连一次，连不上就抛错；上层退回自抓
    后果：用户感觉"搜索越来越慢"，重启 App 才好 —— 而他不知道为什么
    ```

    ✅ 修法：`ensure_running()` 会清掉残留并重新拉起，然后重试一次。

    ⚠️ 为什么**只重试一次**：如果重启之后还是连不上，那说明问题不是
    "服务器偶尔挂了"，而是配置/端口/权限一类**确定性**的问题 ——
    再试十次也是十次失败，只会让用户多等。
    这种情况交给上层退回自抓（快得多），并在日志里留一行 warning。
    """
    query = str(query or "").strip()
    if not query:
        raise McpError("没有给搜索词")

    try:
        return _search_once(query, url=url, timeout_s=timeout_s, fetch_top_n=fetch_top_n)
    except McpError as exc:
        """
        ⚠️ **只在用默认端点时才尝试恢复。**

        调用方显式传了别的 `url`（测试、或者将来支持多服务器）时，
        "重启"应该针对**那个**服务器，而不是偷偷去拉起默认端口的那个 ——
        那会启动一个调用方没要求的进程，还会污染引用计数。
        """
        if url != DEFAULT_URL:
            raise
        log.warning("MCP 搜索失败（%s）—— 试着重启服务器再搜一次", str(exc)[:140])

    if not ensure_running():
        raise McpError("服务器没起来，重启后仍不可用")

    return _search_once(query, url=url, timeout_s=timeout_s, fetch_top_n=fetch_top_n)


# --------------------------------------------------------------------------- #
# free-search-mcp（2026-10-16 加）：「精准搜索」后端
# --------------------------------------------------------------------------- #

FREE_DEFAULT_URL = "http://127.0.0.1:8123/mcp"
# 默认的启动命令（venv 在 ASCII 路径上，避开中文路径的坑）。
# 网关会从 persona.yaml 的 tools.free_search_cmd 里读配置覆盖它。
FREE_DEFAULT_EXE = (
    r"C:/Users/tianyi/.workbuddy/binaries/python/envs/free-search"
    r"\Scripts\free-search-mcp.exe"
)
FREE_DEFAULT_ARGS = ["--transport", "streamable-http", "--port", "8123"]

_free_url = FREE_DEFAULT_URL
_free_exe = FREE_DEFAULT_EXE
_free_args: list[str] = list(FREE_DEFAULT_ARGS)


def set_free_endpoint(url: Optional[str] = None, cmd: Optional[str] = None) -> None:
    """网关在配置变更时调：设置 free-search 的端点与启动命令。

    ⚠️ `cmd` 是**整条命令行**（exe + 参数），用 `shlex.split` 拆 ——
    和 `websearch-mcp.exe` 那套"写死的 args_base"不同，
    这个服务器的端口/传输方式都可能被用户改。
    """
    global _free_url, _free_exe, _free_args
    if url and url.strip():
        _free_url = url.strip()
    if cmd and cmd.strip():
        parts = shlex.split(cmd.strip())
        if parts:
            _free_exe = parts[0]
            _free_args = parts[1:]


def free_endpoint() -> str:
    """当前配置的 free-search 端点（诊断日志用）。"""
    return _free_url


def _free_once(query: str, url: str, timeout_s: float) -> str:
    """握手 -> 找 `search` 工具 -> 调用。失败抛 `McpError`。"""
    client = McpClient(url, timeout_s=timeout_s)
    if not client.initialize():
        raise McpError("MCP 握手失败（服务器没报出自己的名字）")
    tools = client.list_tools()
    names = [t.get("name") for t in tools if isinstance(t, dict)]
    # ⚠️ 工具名**写死挑 "search"**，不能像 `_search_once` 那样拿 names[0]：
    # 这个服务器的工具是按字母序返回的，names[0] 会是 `cache_search`
    # （查本地缓存）—— 拿它当搜索用，第一次永远返回空缓存。
    if "search" not in names:
        raise McpError(f"服务器没有 search 工具：{names}")
    return client.call_tool("search", {"query": query})


def ensure_free_running() -> bool:
    """端点不通时按配置命令拉起 free-search 服务器，重试一次。

    与 `ensure_running()`（websearch-mcp.exe 那个）的差异：
    free-search **没有** start/stop 引用计数，它本身就是服务器 ——
    所以直接 `Popen` 脱离进程即可，不需要"把自己的计数归零"那套。
    已经在跑的话，第二个实例绑不上端口会自己退出，无害。
    """
    try:
        client = McpClient(_free_url, timeout_s=2.5)
        if client.initialize():
            return True
    except Exception:  # noqa: BLE001 —— 端点不通就走拉起
        pass
    exe = Path(_free_exe)
    if not exe.is_file():
        log.warning("free-search 的 exe 不存在：%s —— 请检查 tools.free_search_cmd", _free_exe)
        return False
    try:
        subprocess.Popen([str(exe)] + list(_free_args), **_detached_kwargs())
    except Exception as exc:  # noqa: BLE001
        log.warning("拉起 free-search 失败：%s", exc)
        return False
    time.sleep(3.0)  # 等它初始化（首启可能要装好运行时目录）
    try:
        client = McpClient(_free_url, timeout_s=5.0)
        return bool(client.initialize())
    except Exception:  # noqa: BLE001
        return False


def search_free(
    query: str,
    limit: int = 6,
    *,
    url: Optional[str] = None,
    timeout_s: float = 45.0,
) -> str:
    """用 free-search-mcp 的 `search` 工具搜一次（精准搜索后端）。

    ## 它和 `search()`（websearch-mcp.exe）的分工

    A/B 实测（2026-10-16，`probe-ab-search.py`（历史实测脚本），国内直连）：
    命中质量明显更好——搜「二重螺旋 新版本」它能拿到官网公测公告，
    而老后端只会命中"二"字百科 —— 但要 10~20 秒。所以菜单里是
    「精准搜索」（free-search 优先）与「快速搜索」（老后端）两档。

    ## ⚠️ 2026-10-19 起「快速搜索」的第一后端不是 `search()` 了

    用户要求接入 DeepSeek 官方搜索，它成了 fast 档的首选（实测 0.3~0.5 秒，
    见 `pet/deepseek_search.py`）。本模块的 `search()` 从"快速档首选"
    降级为**它的兜底**（没有 DeepSeek 密钥、或官方接口失败时才用）。
    所以这里的实现不用改，变的是 `tools._web_search` 里的调用顺序。

    ⚠️ 与 `search()` 相同的三条纪律：
      · **同步阻塞**（urllib），调用方必须 `asyncio.to_thread`；
      · 失败抛 `McpError`，消息带是哪一步坏的；
      · 连不上自动拉起服务器并重试**一次**。
    """
    q = str(query or "").strip()
    if not q:
        raise McpError("没有给搜索词")
    target = url or _free_url

    try:
        return _free_once(q, target, timeout_s)
    except McpError as exc:
        # 调用方显式传了别的 url（测试用）时不要去拉默认端口的进程
        if target != _free_url:
            raise
        log.warning("free-search 失败（%s）—— 试着拉起服务器再搜一次", str(exc)[:140])

    if not ensure_free_running():
        raise McpError("free-search 服务器没起来，重启后仍不可用")
    return _free_once(q, target, timeout_s)
