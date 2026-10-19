"""
自检与冒烟测试用的最小 WebSocket 客户端。

存在的理由：端到端验证"sidecar 真的能跑"，而不引入 pytest / websockets 依赖。
它只实现客户端必需的部分：发送掩码文本帧、读取服务端帧、ping/pong 应答。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import struct
from typing import Any, Optional

log = logging.getLogger("pet.testing")

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class TestClient:
    """
    一个够用的 WS 客户端：连上去、发消息、收消息、关掉。

    注意 `_buf` 这个持久缓冲区不是可有可无的：握手响应和紧随其后的第一帧
    常常在同一次 TCP 读取里到达，用 `reader.readuntil` 会把帧首部一起吃掉。
    所有读取都必须经过 `_read_exact` / `_read_until`，它们只消费自己需要的字节。
    （这个 bug 只在同进程、同一事件循环的测试里暴露：跨进程时两者分两次到达，
    看起来一切正常 —— 端到端测试的价值正在于此。）
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8931) -> None:
        self.host = host
        self.port = port
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.received: list[dict[str, Any]] = []
        # 已取用到 received 里的第几条（下标），见 pending() 的说明
        self._cursor = 0
        self._buf = bytearray()

    # ------------------------------------------------------------------ 底层读

    async def _fill(self) -> None:
        assert self.reader is not None
        chunk = await self.reader.read(65536)
        if not chunk:
            raise ConnectionError("连接已被对端关闭")
        self._buf.extend(chunk)

    async def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            await self._fill()
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    async def _read_until(self, sep: bytes) -> bytes:
        while True:
            idx = self._buf.find(sep)
            if idx >= 0:
                end = idx + len(sep)
                out = bytes(self._buf[:end])
                del self._buf[:end]
                return out
            await self._fill()

    # ------------------------------------------------------------------ 握手

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Origin: http://127.0.0.1\r\n"
            f"\r\n"
        )
        self.writer.write(request.encode())
        await self.writer.drain()
        head = await asyncio.wait_for(self._read_until(b"\r\n\r\n"), timeout=5)
        status = head.split(b"\r\n", 1)[0].decode()
        if "101" not in status:
            raise RuntimeError(f"握手失败：{status}")
        log.debug("握手完成，缓冲余 %d 字节（可能已含第一帧）", len(self._buf))

    async def send(self, payload: dict[str, Any]) -> None:
        await self.send_text(json.dumps(payload, ensure_ascii=False))

    async def send_text(self, text: str) -> None:
        assert self.writer is not None
        data = text.encode("utf-8")
        header = bytearray()
        header.append(0x81)
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", n))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", n))
        header.extend(mask)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        self.writer.write(bytes(header) + masked)
        await self.writer.drain()
        log.debug("已发送文本帧：%d 字节载荷，帧头 %s", n, bytes(header).hex())

    async def recv(self, timeout: float = 5.0) -> dict[str, Any]:
        """
        收一条**尚未被取用**的消息；非 JSON 或控制帧会被跳过。

        所有收到的消息都会留在 `self.received` 里（不因被取用而删除），
        这样测试可以用 `expect`/`pending` 按类型检索，而不必依赖到达顺序 ——
        宠物是异步主动说话的，消息顺序本来就不该被测试强依赖。
        """
        pending = self.pending()
        if pending:
            return self._consume(pending[0])
        await self._read_until_deadline(timeout)
        pending = self.pending()
        if not pending:
            raise asyncio.TimeoutError("等待消息超时")
        return self._consume(pending[0])

    def pending(self, type_: Optional[str] = None) -> list[dict[str, Any]]:
        """
        尚未被取用的消息（可按类型过滤）。

        取用状态用一个**单调递增的下标**表示，而不是 id() 或值比较：
        · 用 id() 会被 CPython 的地址复用坑（对象回收后地址给新对象）；
        · 用值比较会在两条内容相同的消息上出错。
        消息只追加不删除，所以"已取用到第几条"是唯一可靠的表示。
        """
        rest = self.received[self._cursor :]
        return [m for m in rest if type_ is None or m.get("type") == type_]

    def _consume(self, message: dict[str, Any]) -> dict[str, Any]:
        # 按下标推进：只允许顺序取用，避免在 list 里做值查找
        for index in range(self._cursor, len(self.received)):
            if self.received[index] is message:
                self._cursor = index + 1
                break
        return message

    async def expect(self, type_: str, timeout: float = 8.0) -> dict[str, Any]:
        """等待指定类型的消息。已到达的其他消息留在缓冲区，不会丢失。"""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            found = self.pending(type_)
            log.debug(
                "expect(%s)：缓冲 %d 条 %s，游标 %d，匹配 %d 条",
                type_,
                len(self.received),
                [m.get("type") for m in self.received],
                self._cursor,
                len(found),
            )
            if found:
                return self._consume(found[0])
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                seen = [m.get("type") for m in self.pending()]
                raise asyncio.TimeoutError(f"等待 {type_} 超时，缓冲区还有：{seen}")
            await self._read_until_deadline(remaining)

    async def _read_until_deadline(self, timeout: float) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError("等待消息超时")
            payload = await asyncio.wait_for(self._read_frame(), timeout=remaining)
            if payload is None:
                raise ConnectionError("服务端关闭了连接")
            try:
                message = json.loads(payload.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                log.debug("丢弃无法解析的帧：%s（%r）", exc, payload[:80])
                continue
            self.received.append(message)
            # 注意：这里**只入缓冲，不推进游标**。取用只能由 recv/expect 决定，
            # 否则刚读到的第一条消息会立刻被自己标记成已取用（曾导致 ready 永远匹配不到）。
            log.debug("已入缓冲 type=%s", message.get("type"))
            return message

    async def collect_until(self, type_: str, timeout: float = 8.0) -> list[dict[str, Any]]:
        """持续收消息直到出现指定类型，返回期间收到的全部消息。"""
        deadline = asyncio.get_running_loop().time() + timeout
        out: list[dict[str, Any]] = []
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError(f"等待 {type_} 超时，已收到：{[m.get('type') for m in out]}")
            message = await self.recv(timeout=remaining)
            out.append(message)
            if message.get("type") == type_:
                return out

    async def _read_frame(self) -> Optional[bytes]:
        try:
            head = await self._read_exact(2)
        except (ConnectionError, asyncio.IncompleteReadError, ConnectionResetError):
            return None
        opcode = head[0] & 0x0F
        length = head[1] & 0x7F
        log.debug("收到帧首部 head=%s opcode=%#x len7=%d 缓冲余 %d", head.hex(), opcode, length, len(self._buf))
        if length == 126:
            (length,) = struct.unpack("!H", await self._read_exact(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", await self._read_exact(8))
        payload = await self._read_exact(length) if length else b""
        log.debug("帧载荷 %d 字节：%r", len(payload), payload[:60])

        if opcode == 0x9:  # ping -> pong（需要掩码）
            await self._send_control(0xA, payload)
            return await self._read_frame()
        if opcode == 0x8:  # close
            return None
        if opcode == 0xA:  # pong
            return await self._read_frame()
        return payload

    async def _send_control(self, opcode: int, payload: bytes) -> None:
        assert self.writer is not None
        mask = os.urandom(4)
        frame = bytes([0x80 | opcode, 0x80 | len(payload)]) + mask
        frame += bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self.writer.write(frame)
        await self.writer.drain()

    async def close(self) -> None:
        if self.writer is not None:
            try:
                await self._send_control(0x8, struct.pack("!H", 1000))
            except Exception:  # noqa: BLE001
                pass
            try:
                self.writer.close()
            except Exception:  # noqa: BLE001
                pass
