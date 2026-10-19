"""
sidecar 入口：`python -m pet [选项]`

启动时会往 stdout 打一行机器可读的 READY JSON，Electron 靠它拿到真实端口
（端口被占用时 sidecar 会自动让位，前端不能假设端口固定）：

    PET_READY {"port": 8931, "protocolVersion": 1, "pid": 1234, "brain": "mock"}

其余所有日志走 stderr，避免污染这行机器协议。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Optional

from . import paths
from . import __version__
from .config import ConfigError, load_config
from .gateway import Gateway
from .protocol import DEFAULT_PORT, PROTOCOL_VERSION

log = logging.getLogger("pet.main")


def _force_utf8_stdio() -> None:
    """
    Windows 控制台默认可能是 GBK，中文日志会变成乱码或直接抛
    UnicodeEncodeError 把进程带崩。强制 UTF-8 是必须的，不是讲究。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


# 单个日志文件的上限与保留份数。
#
# ⚠️ 为什么需要轮转（2026-10-09）：`python.log` 用普通 `FileHandler`，
# **无限增长** —— 清理时它已经长到 **168 MB**，全是这一个文件。
LOG_MAX_BYTES = 20 * 1024 * 1024  # 20 MB
LOG_BACKUP_COUNT = 3


def _rotate_if_needed(path: Path, max_bytes: int = LOG_MAX_BYTES, backups: int = LOG_BACKUP_COUNT) -> None:
    """
    日志超了就轮转：`path` -> `path.1` -> `path.2` -> `path.3` -> 删除。

    ## 为什么不用标准库的 `RotatingFileHandler`

    ```python
    class FileHandler(StreamHandler):
        def _open(self):
            return open_func(self.baseFilename, self.mode, ...)   # 用的是 self.mode
    ```

    而 `RotatingFileHandler.__init__` 在 **`FileHandler.__init__` 返回之后**才把
    `self.mode` 改成 `'a'` —— 它内部那次 rollover 打开文件用的是**构造时传入的
    mode**。`delay=False` 时默认 `'a'`（碰巧对），但这依赖实现细节。
    而且它的 rollover 是为**单进程**设计的。

    ## 这个版本

    自己判断、自己改名，**不碰 `_open()`** —— 行为完全可预期。

    ## ⚠️ 只在**启动时**调一次（在 `_setup_logging` 里）。这样：
      · 不需要每次写日志都检查大小
      · 没有"两个进程同时轮转"的窗口
      · 多进程同时**追加**不会互相破坏（`O_APPEND` 写是原子的）

    ⚠️ 用**字节**判断而不是行数：日志行长度差异很大（一行可能是几百字的中文
    转写），按行会在长行时失控。

    ## 🔴 2026-10-14：启动时轮转**不够**，另见 `_SizeRotatingFileHandler`

    实测 `python.log.1` 已经 **41 MB**、主进程 `main.log.1` **110 MB**，
    而上限写的是 20 MB —— 因为桌宠是**长期挂机**程序，一次会话就能写上百 MB，
    "只在重启那一刻轮转"等于没有上限。所以真正的轮转交给写入时的 handler，
    这个函数保留作为**兜底**（收掉上次会话遗留的超大文件）。
    """
    try:
        if not path.is_file() or path.stat().st_size < max_bytes:
            return
    except OSError:
        return

    # 从最老的开始往后挪，最后删掉最老那份
    try:
        oldest = path.with_name(f"{path.name}.{backups}")
        if oldest.exists():
            oldest.unlink()
        for i in range(backups - 1, 0, -1):
            src = path.with_name(f"{path.name}.{i}")
            if src.exists():
                src.replace(path.with_name(f"{path.name}.{i + 1}"))
        path.replace(path.with_name(f"{path.name}.1"))
    except OSError as exc:
        # 轮转失败**不能影响启动** —— 大不了这次不轮转（下次启动再试）
        print(f"日志轮转失败 {path}：{exc}", file=sys.stderr)


class _SizeRotatingFileHandler(logging.FileHandler):
    """按大小在**写入时**轮转的文件 handler（2026-10-14 加）。

    ## 为什么必须自己做

    原来的方案是"启动时调一次 `_rotate_if_needed`"。这对**长期挂机**的程序
    等于没有上限：实测 `python.log.1` 41 MB、主进程 `main.log.1` 110 MB，
    而上限设的是 20 MB —— 只要不重启就会一直写下去。

    标准库的 `RotatingFileHandler` 也不能直接用（见 `_rotate_if_needed`
    的说明：它在 `FileHandler.__init__` 之后才把 mode 改成 'a'，行为依赖实现细节）。

    ## ⚠️ Windows 上必须先关句柄再改名

    打开着的文件**不能 rename**（`PermissionError`）。所以超线时：
    `self.close()` -> 轮转 -> `self._open()` 重新打开。

    ## ⚠️ 记账用起始文件大小 + 每条消息的 UTF-8 字节

    不用 `self.stream.tell()`：文本模式下它的值受编码器缓冲影响，不可靠。
    """

    def __init__(
        self,
        filename: str,
        max_bytes: int = LOG_MAX_BYTES,
        backups: int = LOG_BACKUP_COUNT,
        **kwargs: Any,
    ) -> None:
        super().__init__(filename, encoding="utf-8", **kwargs)
        self._max_bytes = max_bytes
        self._backups = backups
        try:
            self._written = os.path.getsize(filename)
        except OSError:
            self._written = 0

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            msg = self.format(record)
            size = len(msg.encode("utf-8", "replace")) + len(self.terminator)
            if self._written + size > self._max_bytes:
                # 关句柄 -> 轮转 -> 重开（Windows 上改名必须先关，见类注释）
                self.close()
                _rotate_if_needed(Path(self.baseFilename), self._max_bytes, self._backups)
                self.stream = self._open()
                self._written = 0
            self._pending = size
        except Exception:  # noqa: BLE001
            # 轮转出问题也不能把日志搞丢（handleError 会打到 stderr）
            self._pending = 0
            self.handleError(record)
        super().emit(record)
        self._written += getattr(self, "_pending", 0)


def _setup_logging(verbose: bool, log_file: Optional[Path]) -> None:
    """
    配置日志。

    ## ⚠️ 主日志文件写不了时必须**退到可写的地方**，不能只打印一句就放弃

    主进程传进来的 `--log-file` 是 `userData/logs/pet.log`，而**这个目录在受限环境里
    不可写**（实测：AppData 下创建失败）。原来的实现只 `print` 一句"无法写日志文件"
    就继续跑 —— 后果是 **Python 侧的诊断全部丢失**，只剩 stderr 那一份（主进程会转发，
    但一旦主进程那边有问题就什么都没有了）。

    排查"手动备份失败"时正是被这个坑住：错误只在 Python 侧有，而 Python 没地方写。

    所以加一条**兜底落点**：`<项目根>/apps/desktop/pet-data/python.log`。
    那个目录一直在（记忆数据库就在那儿），而且用户/开发者随手就能打开看。
    """
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]

    candidates: list[Path] = []
    if log_file is not None:
        candidates.append(log_file)
        # 兜底：与记忆数据库同处（那个目录必然可写 —— 否则记忆功能本身就没法工作）
        candidates.append(paths.data_dir() / "python.log")

    for candidate in candidates:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            # ⚠️ 打开之前先轮转（见 `_rotate_if_needed` 的说明）—— 收掉上次
            #    会话遗留的超大文件。本次会话的轮转由
            #    `_SizeRotatingFileHandler` 在写入时负责。
            _rotate_if_needed(candidate)
            handlers.append(_SizeRotatingFileHandler(candidate))
            break  # 成功一个就够，不要重复写同一份日志到多处
        except OSError as exc:
            print(f"无法写日志文件 {candidate}：{exc}", file=sys.stderr)

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
        handlers=handlers,
        force=True,
    )
    # 第三方库的噪音压低
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def _project_root() -> Path:
    """
    仓库根目录。本文件在 apps/brain/pet/main.py，向上四级才是根。

    别改成 parents[2]：那会算出 apps/，于是 config/persona.yaml 永远找不到、
    静默退化成内置默认值 —— 用户改配置却毫无反应，且不报错。
    """
    return paths.project_root()


def _default_config_path() -> Path:
    return _project_root() / "config" / "persona.yaml"


def _print_ready(port: int, brain: str, pid: int) -> None:
    payload = {
        "port": port,
        "protocolVersion": PROTOCOL_VERSION,
        "pid": pid,
        "brain": brain,
        "version": __version__,
        "ts": int(time.time() * 1000),
    }
    print(f"PET_READY {json.dumps(payload, ensure_ascii=False)}", flush=True)


async def _run(args: argparse.Namespace) -> int:
    config_path = Path(args.config) if args.config else _default_config_path()
    try:
        config = load_config(config_path if config_path.exists() else None)
    except ConfigError as exc:
        # 配置错误要给出可操作的提示，而不是堆栈
        print(f"配置错误：{exc}", file=sys.stderr)
        print(f"配置文件：{config_path}", file=sys.stderr)
        return 2

    gateway = Gateway(config)
    host = args.host or os.environ.get("PET_WS_HOST", "127.0.0.1")
    try:
        port = await gateway.start(host=host, port=args.port)
    except RuntimeError as exc:
        print(f"无法启动服务：{exc}", file=sys.stderr)
        return 3

    # 端口文件：给不解析 stdout 的调用方（脚本、调试工具）留一条路
    port_file = Path(args.port_file) if args.port_file else None
    if port_file is not None:
        try:
            port_file.parent.mkdir(parents=True, exist_ok=True)
            port_file.write_text(
                json.dumps({"port": port, "pid": os.getpid(), "protocolVersion": PROTOCOL_VERSION}),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("无法写端口文件 %s：%s", port_file, exc)

    """
    ## 🔴 MCP 搜索服务器：启动时就拉起来（2026-10-15）

    ⚠️ 为什么要**提前**拉而不是等第一次搜索：

    那个 exe 自己的冷启动要几秒（读配置 + 探引擎）。第一次搜索时才拉的话，
    用户那次搜索会平白多等几秒 —— 而用户感受到的是「宠物搜东西很慢」。

    ⚠️ 为什么用 `ensure_running()` 而不是 `Popen` 自己养：

    它有**引用计数**（`start` 加一 / `stop` 减一 / 归零才退）。
    自己 `Popen` + kill 会绕过计数，而且 Windows 上 detached 子进程的清理
    很容易漏 —— §103 刚因为这个看错过日志。

    ⚠️ 失败**绝不能**影响 sidecar 启动：搜索是加分项。
    所以整段包在 try 里，失败只记一行 warning，搜的时候再按需重试。

    ⚠️ 用 `to_thread` —— `ensure_running()` 内部是阻塞的 subprocess + urllib，
    在事件循环里直接调会把整个 sidecar 卡住（连语音都停）。
    """
    tools_section = config.root.get("tools", {}) or {}
    mcp_wanted = bool(tools_section.get("mcp_search", True)) and bool(
        tools_section.get("web_search", False)
    )
    if mcp_wanted:
        try:
            from . import mcp_search

            """
            ## 🔴 先放掉上一次的引用（2026-10-15，踩过）

            sidecar 崩溃后会**重启**（见 §103：主进程监听 `ready` 事件重连）。
            每次重启都会走到这里 `start` 一次 —— 引用计数 +1。

            而 `stop` 只在**最终退出**时调一次（-1）。于是：

            ```
            重启 5 次 -> 计数 = 5
            最终退出 -> 计数 = 4  -> 永远减不到 0 -> **服务器留在后台收不掉**
            ```

            ⚠️ 这个 bug 的表现是「关掉 App 之后，任务管理器里还挂着一个
            websearch-mcp.exe，而且删不掉这个文件夹（文件被占用）」。
            用户完全不知道那是什么。

            ⚠️ 所以每个新 sidecar **先 `stop` 一次再 `start`** ——
            保证"活的引用数"始终是 1（只管自己这一次）。
            """
            await asyncio.to_thread(mcp_search.stop)
            ready = await asyncio.to_thread(mcp_search.ensure_running)
            if ready:
                log.info("MCP 搜索服务器已就绪（联网搜索将走它）")
            else:
                log.warning(
                    "MCP 搜索服务器没起来 —— 联网搜索会退回自己抓 HTML"
                    "（慢但能用）。检查 exe 在不在：%s",
                    mcp_search.find_exe(),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("拉起 MCP 搜索服务器时出错（不影响启动）：%s", exc)

    log.info(
        "sidecar 就绪 v%s：protocol=%d brain=%s persona=%s activity.overall=%.2f",
        __version__,
        PROTOCOL_VERSION,
        gateway.conversation.brain.name,
        config.pet_name,
        float(config.activity.get("overall", 0.5)),
    )
    _print_ready(port, gateway.conversation.brain.name, os.getpid())

    stop = asyncio.Event()

    def _request_stop(*_: object) -> None:
        log.info("收到退出信号，准备关闭…")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows 上部分信号不支持 add_signal_handler，退回 signal.signal
            try:
                signal.signal(sig, _request_stop)
            except (ValueError, OSError):
                pass

    serve_task = loop.create_task(gateway.serve_forever(), name="serve")
    stop_task = loop.create_task(stop.wait(), name="stop-wait")
    try:
        done, _pending = await asyncio.wait(
            {serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            exc = task.exception()
            if exc is not None:
                log.error("服务异常退出：%s", exc)
                return 4
    finally:
        stop_task.cancel()
        serve_task.cancel()
        await gateway.shutdown()
        if port_file is not None:
            try:
                port_file.unlink(missing_ok=True)
            except OSError:
                pass
        """
        ## MCP 搜索服务器：礼貌地放掉引用（2026-10-15）

        ⚠️ 它自己是**引用计数**的（`start` +1 / `stop` -1 / 归零才退出）。
        我们启动时 `start` 过一次，所以退出时要 `stop` 一次 ——
        **不配对的话计数会一直挂着**，下次启动的实例会以为自己不是第一个，
        于是 `stop` 永远减不到 0，进程留在后台收不掉。

        ⚠️ 用 `to_thread` + 短超时：关不掉也不能卡住 sidecar 退出
        （用户点了"退出"却半天不退，比留一个后台进程更烦人）。

        ⚠️ 但**退出路径上 `to_thread` 有风险**：事件循环正在收尾，
        `to_thread` 的线程可能来不及被调度（尤其收到 SIGINT 时）。
        实测这个坑让"退出后服务器没被收掉"。所以现在是
        **直接同步调 `stop()`** —— 它内部已经是 detached 的 `Popen`，
        不等子进程，毫秒级返回，不会卡住退出。
        """
        if mcp_wanted:
            try:
                from . import mcp_search

                mcp_search.stop()
            except Exception as exc:  # noqa: BLE001
                log.debug("关闭 MCP 搜索服务器时出错（无所谓）：%s", exc)
        log.info("sidecar 已退出")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pet",
        description="桌面宠物的 Python 大脑（sidecar）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m pet                  启动服务（默认端口 8931）\n"
            "  python -m pet --selftest       离线自检，不需要前端和密钥\n"
            "  python -m pet --check-config   只校验配置并退出\n"
        ),
    )
    parser.add_argument("--host", default=None, help="监听地址（默认 127.0.0.1，仅本机）")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("PET_WS_PORT", DEFAULT_PORT)), help="监听端口"
    )
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config/persona.yaml）")
    parser.add_argument("--port-file", default=None, help="把实际端口写入该 JSON 文件")
    parser.add_argument("--log-file", default=None, help="同时把日志写入该文件")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--selftest", action="store_true", help="运行离线自检后退出")
    parser.add_argument(
        "--vision-test",
        action="store_true",
        help="对着真实屏幕验证屏幕感知（需要有桌面会话）",
    )
    # 给 --vision-test 用的参数。放在主解析器里，否则会被 argparse 判为未知参数 ——
    # 顶层解析失败会直接以退出码 2 结束，根本走不到子命令。
    parser.add_argument("--frames", type=int, default=3, help="屏幕自检观察多少帧（默认 3）")
    parser.add_argument(
        "--interval", type=float, default=1.5, help="屏幕自检两帧之间的间隔秒数（默认 1.5）"
    )
    parser.add_argument("--check-config", action="store_true", help="校验配置后退出")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    _force_utf8_stdio()
    args = build_parser().parse_args(argv)
    log_file = Path(args.log_file) if args.log_file else None
    _setup_logging(args.verbose, log_file)

    if args.selftest:
        from .selftest import main as selftest_main

        return selftest_main(argv)

    if args.vision_test:
        from .vision_test import main as vision_main

        return vision_main(
            ["--interval", str(args.interval), "--frames", str(args.frames)]
        )

    if args.check_config:
        path = Path(args.config) if args.config else _default_config_path()
        try:
            config = load_config(path if path.exists() else None)
        except ConfigError as exc:
            print(f"配置错误：{exc}", file=sys.stderr)
            return 2
        print(f"配置文件：{path if path.exists() else '（不存在，使用内置默认值）'}")
        print(json.dumps(config.snapshot(), ensure_ascii=False, indent=2))
        print(f"生效的 brain 提供者：{config.brain_provider()}")
        key = config.api_key()
        print(f"API 密钥：{'已从环境变量读到' if key else '未配置（将使用 mock 提供者）'}")
        return 0

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
