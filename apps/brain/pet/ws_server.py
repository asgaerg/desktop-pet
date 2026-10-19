"""
零依赖 WebSocket 服务端（RFC 6455 子集），跑在 asyncio 上。

为什么不用 `websockets` / `fastapi`：
  sidecar 要做到零 pip 依赖 —— 启动快、打包不炸、换机器不用配环境。
  我们只需要 §4 握手 + §5 数据帧 + ping/pong + close，约 300 行足够。

支持的帧：text / binary / ping / pong / close，含分片续帧与掩码解包。
不支持（用不到）：扩展、压缩、子协议协商。

线程模型：单事件循环。每个连接一个 reader task + 一个 writer 锁。
所有上层发送都走 `Client.send_*`，内部加锁保证帧不被交错写坏。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import struct
from typing import Awaitable, Callable, Optional

log = logging.getLogger("pet.ws")

# RFC 6455 §1.3 固定 GUID
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

_MAX_PAYLOAD = 16 * 1024 * 1024  # 16MB 上限，防止恶意长度字段撑爆内存


class WSError(Exception):
    """协议级错误，触发连接关闭。"""


class WebSocketClient:
    """一个已握手的连接。上层只依赖 send_text / close / 两个回调。"""

    __slots__ = (
        "reader",
        "writer",
        "peer",
        "on_text",
        "on_binary",
        "on_close",
        "_write_lock",
        "_closed",
        "_buf",
    )

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        on_text: Callable[["WebSocketClient", str], Awaitable[None]],
        on_binary: Optional[Callable[["WebSocketClient", bytes], Awaitable[None]]] = None,
        on_close: Optional[Callable[["WebSocketClient"], Awaitable[None]]] = None,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.peer = writer.get_extra_info("peername")
        self.on_text = on_text
        self.on_binary = on_binary
        self.on_close = on_close
        self._write_lock = asyncio.Lock()
        self._closed = False
        # 跨 read() 边界的残包缓冲：TCP 不保证一次 read 拿到完整帧
        self._buf = bytearray()

    # ---------------------------------------------------------------- 读

    async def _read_exact(self, n: int) -> bytes:
        """读满 n 字节，优先消费残包缓冲。"""
        while len(self._buf) < n:
            chunk = await self.reader.read(65536)
            if not chunk:
                log.debug(
                    "连接在读到完整帧之前就断开了 peer=%s（需要 %d 字节，缓冲里只有 %d）",
                    self.peer,
                    n,
                    len(self._buf),
                )
                raise WSError("连接在读到完整帧之前就断开了")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    async def _read_frame(self) -> tuple[bool, int, bytes]:
        """返回 (fin, opcode, payload)。"""
        b0, b1 = await self._read_exact(2)
        fin = bool(b0 & 0x80)
        rsv = b0 & 0x70
        if rsv:
            raise WSError(f"不支持扩展位 rsv={rsv:#x}")
        opcode = b0 & 0x0F

        masked = bool(b1 & 0x80)  # 客户端发来的帧必须掩码（RFC 6455 §5.1）
        length = b1 & 0x7F
        log.debug(
            "帧头 b0=%#x b1=%#x opcode=%#x fin=%s masked=%s len7=%d 缓冲余 %d",
            b0,
            b1,
            opcode,
            fin,
            masked,
            length,
            len(self._buf),
        )
        if length == 126:
            (length,) = struct.unpack("!H", await self._read_exact(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", await self._read_exact(8))
        if length > _MAX_PAYLOAD:
            raise WSError(f"载荷过大：{length}")

        mask = await self._read_exact(4) if masked else b""
        payload = await self._read_exact(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[i & 3] for i, byte in enumerate(payload))
        return fin, opcode, payload

    async def run(self) -> None:
        """读循环，直到对端关闭或出错。异常不外抛，交给 on_close 收尾。"""
        frag_op: Optional[int] = None
        frag_buf = bytearray()
        try:
            while True:
                fin, opcode, payload = await self._read_frame()

                if opcode == OP_CLOSE:
                    await self._send_frame(OP_CLOSE, payload[:125])
                    break
                if opcode == OP_PING:
                    await self._send_frame(OP_PONG, payload)
                    continue
                if opcode == OP_PONG:
                    continue

                if opcode == OP_CONT:
                    if frag_op is None:
                        raise WSError("收到续帧但没有起始帧")
                    frag_buf.extend(payload)
                    if fin:
                        await self._dispatch(frag_op, bytes(frag_buf))
                        frag_op, frag_buf = None, bytearray()
                elif fin:
                    await self._dispatch(opcode, payload)
                else:
                    frag_op, frag_buf = opcode, bytearray(payload)
        except (WSError, ConnectionResetError, asyncio.IncompleteReadError) as exc:
            log.debug("连接结束 peer=%s: %s", self.peer, exc)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 单连接出错绝不能拖垮整轮服务
            log.exception("连接读循环异常 peer=%s", self.peer)
        finally:
            self._closed = True
            if self.on_close is not None:
                try:
                    await self.on_close(self)
                except Exception:  # noqa: BLE001
                    log.exception("on_close 回调异常")
            try:
                self.writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _dispatch(self, opcode: int, payload: bytes) -> None:
        if opcode == OP_TEXT:
            await self.on_text(self, payload.decode("utf-8", errors="replace"))
        elif opcode == OP_BINARY and self.on_binary is not None:
            await self.on_binary(self, payload)

    # ---------------------------------------------------------------- 写

    async def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        if self._closed and opcode != OP_CLOSE:
            return
        header = bytearray()
        header.append(0x80 | opcode)  # 服务端发出的帧不掩码
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header.extend(struct.pack("!H", n))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", n))
        async with self._write_lock:
            self.writer.write(bytes(header) + payload)
            await self.writer.drain()

    async def send_text(self, text: str) -> None:
        await self._send_frame(OP_TEXT, text.encode("utf-8"))

    async def send_binary(self, data: bytes) -> None:
        await self._send_frame(OP_BINARY, data)

    async def close(self, code: int = 1000) -> None:
        if self._closed:
            return
        try:
            await self._send_frame(OP_CLOSE, struct.pack("!H", code))
        except Exception:  # noqa: BLE001
            pass
        self._closed = True
        try:
            self.writer.close()
        except Exception:  # noqa: BLE001
            pass


class WebSocketServer:
    """
    asyncio WebSocket 服务端。

    用法：
        server = WebSocketServer(on_text=..., on_connect=..., on_disconnect=...)
        await server.start(port=8931)
        await server.serve_forever()
    """

    def __init__(
        self,
        on_text: Callable[[WebSocketClient, str], Awaitable[None]],
        on_binary: Optional[Callable[[WebSocketClient, bytes], Awaitable[None]]] = None,
        on_connect: Optional[Callable[[WebSocketClient], Awaitable[None]]] = None,
        on_disconnect: Optional[Callable[[WebSocketClient], Awaitable[None]]] = None,
        max_clients: int = 4,
        allow_origin_check: bool = True,
    ) -> None:
        self._on_text = on_text
        self._on_binary = on_binary
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self.max_clients = max_clients
        # 只接受环回地址与本地来源：sidecar 绝不对外网暴露
        self.allow_origin_check = allow_origin_check
        self.clients: set[WebSocketClient] = set()
        self.port: Optional[int] = None
        self._server: Optional[asyncio.AbstractServer] = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self, host: str = "127.0.0.1", port: int = 8931, port_scan: int = 20) -> int:
        """
        绑定端口。被占用时自动向上探测，避免"另一个实例还活着"导致启动失败。
        port=0 表示由系统分配空闲端口（自检与测试用）。
        返回实际绑定的端口。
        """
        if port == 0:
            self._server = await asyncio.start_server(self._handle_client, host=host, port=0)
            assert self._server.sockets
            self.port = self._server.sockets[0].getsockname()[1]
            log.info("WebSocket 服务已监听 ws://%s:%d（系统分配）", host, self.port)
            return self.port

        last_err: Optional[Exception] = None
        for candidate in range(port, port + port_scan + 1):
            try:
                self._server = await asyncio.start_server(
                    self._handle_client, host=host, port=candidate, reuse_address=True
                )
                self.port = candidate
                log.info("WebSocket 服务已监听 ws://%s:%d", host, candidate)
                return candidate
            except OSError as exc:
                last_err = exc
                log.debug("端口 %d 不可用：%s", candidate, exc)
        raise RuntimeError(f"{port}~{port + port_scan} 全部端口被占用，无法启动") from last_err

    async def serve_forever(self) -> None:
        assert self._server is not None, "先调用 start()"
        async with self._server:
            await self._server.serve_forever()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        client: Optional[WebSocketClient] = None
        try:
            await self._handshake(reader, writer)
            if len(self.clients) >= self.max_clients:
                log.warning("连接数超上限(%d)，拒绝 %s", self.max_clients, writer.get_extra_info("peername"))
                writer.close()
                return
            client = WebSocketClient(
                reader,
                writer,
                on_text=self._on_text,
                on_binary=self._on_binary,
                on_close=self._handle_disconnect,
            )
            self.clients.add(client)
            if self._on_connect is not None:
                await self._on_connect(client)
            await client.run()
        except WSError as exc:
            log.debug("握手失败 %s：%s", writer.get_extra_info("peername"), exc)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            log.exception("处理连接时异常")
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
        finally:
            if client is not None:
                self.clients.discard(client)

    async def _handle_disconnect(self, client: WebSocketClient) -> None:
        self.clients.discard(client)
        if self._on_disconnect is not None:
            await self._on_disconnect(client)

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # 请求行 + 头部，读到空行为止；限制总长避免被垃圾数据灌爆
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        if len(head) > 16384:
            raise WSError("握手头过大")
        lines = head.decode("latin-1").split("\r\n")
        request_line = lines[0]
        parts = request_line.split(" ")
        if len(parts) < 3 or parts[0].upper() != "GET":
            raise WSError(f"非法请求行：{request_line!r}")

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            if ":" not in line:
                raise WSError(f"非法头部：{line!r}")
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()

        if headers.get("upgrade", "").lower() != "websocket":
            raise WSError("缺少 Upgrade: websocket")
        # RFC 6455 §4.1 只要求 Connection 头包含 "Upgrade" 这个 token，
        # 浏览器/Electron 发的是 "Connection: Upgrade"。早期这里要求必须含
        # "websocket" 会导致所有真实客户端握手失败（自检抓到过）。
        connection = headers.get("connection", "").lower()
        if "upgrade" not in connection and "websocket" not in connection:
            raise WSError("缺少 Connection: Upgrade")
        key = headers.get("sec-websocket-key")
        if not key:
            raise WSError("缺少 Sec-WebSocket-Key")
        if self.allow_origin_check:
            origin = headers.get("origin", "")
            if origin and not _is_local_origin(origin):
                raise WSError(f"拒绝非本机来源：{origin}")

        accept = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        writer.write(
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n".encode()
        )
        await writer.drain()

    async def broadcast(self, text: str) -> None:
        """向所有连接发送；单个连接失败不影响其他连接。"""
        for client in list(self.clients):
            try:
                await client.send_text(text)
            except Exception:  # noqa: BLE001
                log.debug("广播失败，丢弃该连接")

    async def close(self) -> None:
        for client in list(self.clients):
            await client.close()
        self.clients.clear()
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass


def _is_local_origin(origin: str) -> bool:
    lowered = origin.lower()
    return (
        lowered.startswith("http://127.0.0.1")
        or lowered.startswith("http://localhost")
        or lowered.startswith("file://")
        or lowered.startswith("app://")
        or lowered == "null"
    )


def default_sidecar_env() -> dict[str, str]:
    """给 Electron 拉起 sidecar 时用的最小环境（便于诊断）。"""
    return {
        "PET_WS_HOST": os.environ.get("PET_WS_HOST", "127.0.0.1"),
    }
