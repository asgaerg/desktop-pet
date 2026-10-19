/**
 * 与 sidecar 通信的 WebSocket 客户端。
 *
 * 为什么不用 npm 上的 ws：主进程与 sidecar 只在本机环回上通信，
 * RFC 6455 的文本帧 + ping/pong 就够用。自己实现换来的是零运行时依赖、
 * 无版本升级风险、以及能精确控制掩码与分片的行为。
 *
 * 这个文件是前端唯一碰网络的地方：其余模块只依赖它的 send/on 接口。
 */

import net from 'node:net';
import crypto from 'node:crypto';
import { EventEmitter } from 'node:events';
import type { ToBrainMsg, ToUiMsg } from '@pet/protocol';
import { parseMessage } from '@pet/protocol';

const GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';

const OP_CONT = 0x0;
const OP_TEXT = 0x1;
// 二进制帧：用来承载语音音频。**曾经漏了这个常量**，而代码里又引用了它 ——
// 结果是收到任何二进制帧都抛 ReferenceError，语音永远收不到。
// 这个 bug 活了很久才被发现，因为它在受限环境里根本跑不到（没有语音输出）。
const OP_BINARY = 0x2;
const OP_CLOSE = 0x8;
const OP_PING = 0x9;
const OP_PONG = 0xa;

const MAX_PAYLOAD = 8 * 1024 * 1024;

/** 把 ws://127.0.0.1:8931 解析成 TCP 连接参数，顺便校验只连本机。 */
export function parseWsUrl(url: string): { host: string; port: number; path: string } {
  const match = /^ws:\/\/([^/:]+)(?::(\d+))?(\/[^\s?#]*)?(?:\?[^\s#]*)?$/.exec(url.trim());
  if (!match) throw new Error(`不是合法的 ws:// 地址：${url}`);
  const [, host, portText, pathText] = match;
  const port = portText ? Number(portText) : 80;

  // 安全边界：sidecar 只在本机，客户端也只允许连本机。
  // 否则一条被改过的 URL 就能把宠物的大脑换成别人的服务器。
  if (host !== '127.0.0.1' && host !== 'localhost' && host !== '::1') {
    throw new Error(`拒绝连接非本机地址：${host}（sidecar 只监听环回地址）`);
  }
  return { host: host === 'localhost' ? '127.0.0.1' : host, port, path: pathText || '/' };
}

export interface PetSocketOptions {
  url: string;
  /** 重连退避的起始与上限 */
  minBackoffMs?: number;
  maxBackoffMs?: number;
}

export class PetSocket extends EventEmitter {
  private socket: net.Socket | null = null;
  private buffer = Buffer.alloc(0);
  private handshakeDone = false;
  private closing = false;
  private backoff: number;
  private readonly minBackoff: number;
  private readonly maxBackoff: number;
  private reconnectTimer: NodeJS.Timeout | null = null;

  constructor(private readonly options: PetSocketOptions) {
    super();
    this.minBackoff = options.minBackoffMs ?? 500;
    this.maxBackoff = options.maxBackoffMs ?? 8000;
    this.backoff = this.minBackoff;
  }

  get connected(): boolean {
    return this.handshakeDone && this.socket !== null && !this.socket.destroyed;
  }

  connect(): void {
    if (this.socket) return;
    this.closing = false;
    const { host, port } = parseWsUrl(this.options.url);

    const socket = net.createConnection({ host, port });
    this.socket = socket;
    this.buffer = Buffer.alloc(0);
    this.handshakeDone = false;

    socket.setNoDelay(true);
    socket.on('connect', () => socket.write(this.buildHandshake(host, port)));
    // 回调参数显式标注类型：net.Socket 的类型来自 Node 的 @types，
    // 而本项目用一份最小 shim 代替它（见 src/types/node-shim.d.ts），
    // 所以这里必须自己写清楚，否则 noImplicitAny 会报"参数隐式为 any"。
    socket.on('data', (chunk: Buffer) => this.onData(chunk));
    socket.on('error', (error: Error) => this.emit('log', `连接错误：${error.message}`));
    socket.on('close', () => this.onClose());
  }

  private buildHandshake(host: string, port: number): string {
    const key = crypto.randomBytes(16).toString('base64');
    this.expectedAccept = crypto
      .createHash('sha1')
      .update(key + GUID)
      .digest('base64');
    return (
      'GET / HTTP/1.1\r\n' +
      `Host: ${host}:${port}\r\n` +
      'Upgrade: websocket\r\n' +
      'Connection: Upgrade\r\n' +
      `Sec-WebSocket-Key: ${key}\r\n` +
      'Sec-WebSocket-Version: 13\r\n' +
      `Origin: http://${host}:${port}\r\n` +
      '\r\n'
    );
  }

  private expectedAccept = '';

  private onData(chunk: Buffer): void {
    this.buffer = Buffer.concat([this.buffer, chunk]);

    if (!this.handshakeDone) {
      const end = this.buffer.indexOf('\r\n\r\n');
      if (end < 0) {
        // 防止服务端发一堆垃圾把我们撑爆
        if (this.buffer.length > 16384) this.fail('握手响应异常（头部过大）');
        return;
      }
      const head = this.buffer.subarray(0, end).toString('latin1');
      this.buffer = this.buffer.subarray(end + 4);

      if (!head.startsWith('HTTP/1.1 101')) {
        this.fail(`握手失败：${head.split('\r\n')[0]}`);
        return;
      }
      // 校验 Sec-WebSocket-Accept：确认对端真的是我们的 sidecar
      const acceptLine = /sec-websocket-accept:\s*(\S+)/i.exec(head);
      if (!acceptLine || acceptLine[1] !== this.expectedAccept) {
        this.fail('Sec-WebSocket-Accept 校验不通过');
        return;
      }

      this.handshakeDone = true;
      this.backoff = this.minBackoff;
      this.emit('open');
    }

    this.drainFrames();
  }

  /** 从缓冲区里把所有完整帧取出来处理。 */
  private drainFrames(): void {
    for (;;) {
      const frame = this.readFrame();
      // readFrame 的契约有两种"没有帧"：
      //   undefined —— 数据还不够，等下一批（正常情况，最常见）
      //   null      —— 帧非法，readFrame 内部已经关闭了连接
      // 这里合起来处理即可；分两个 if 检查会被 TS 收窄成 never（第二个永不成立）。
      if (frame === undefined || frame === null) return;
      const { opcode, payload } = frame;
      if (opcode === OP_TEXT) {
        const text = payload.toString('utf-8');
        const parsed = parseMessage(text) as ToUiMsg | null;
        if (parsed) this.emit('message', parsed);
        else this.emit('log', `丢弃无法识别的消息：${text.slice(0, 120)}`);
      } else if (opcode === OP_BINARY) {
        // 二进制帧目前只承载语音音频。单独发事件，让主进程转给渲染层播放。
        this.emit('binary', payload);
      } else if (opcode === OP_PING) {
        this.sendFrame(OP_PONG, payload);
      } else if (opcode === OP_CLOSE) {
        this.emit('log', 'sidecar 要求关闭连接');
        this.socket?.end();
        return;
      }
      // pong 忽略：本机连接，不做延迟探测
    }
  }

  private readFrame(): { opcode: number; payload: Buffer } | null | undefined {
    const buf = this.buffer;
    if (buf.length < 2) return undefined;
    const b0 = buf[0];
    const b1 = buf[1];
    const opcode = b0 & 0x0f;
    const masked = (b1 & 0x80) !== 0;
    let length = b1 & 0x7f;
    let offset = 2;

    if (length === 126) {
      if (buf.length < offset + 2) return undefined;
      length = buf.readUInt16BE(offset);
      offset += 2;
    } else if (length === 127) {
      if (buf.length < offset + 8) return undefined;
      const big = buf.readBigUInt64BE(offset);
      if (big > BigInt(MAX_PAYLOAD)) {
        this.fail('收到的帧过大');
        return null;
      }
      length = Number(big);
      offset += 8;
    }
    if (length > MAX_PAYLOAD) {
      this.fail('收到的帧过大');
      return null;
    }

    let mask: Buffer | null = null;
    if (masked) {
      if (buf.length < offset + 4) return undefined;
      mask = buf.subarray(offset, offset + 4);
      offset += 4;
    }
    if (buf.length < offset + length) return undefined;

    let payload = Buffer.from(buf.subarray(offset, offset + length));
    if (mask) {
      for (let i = 0; i < payload.length; i += 1) payload[i] ^= mask[i & 3];
    }
    this.buffer = buf.subarray(offset + length);
    // 分片（opcode=0）在 sidecar 侧不会出现：它只发小 JSON。
    // 真遇到就显式报错，别静默拼错数据。
    if (opcode === OP_CONT) {
      this.fail('收到意外的分片续帧');
      return null;
    }
    return { opcode, payload };
  }

  /** 发送一条协议消息。未连接时安全丢弃并告知调用方。 */
  send(message: ToBrainMsg): boolean {
    if (!this.connected) {
      this.emit('log', `未连接，丢弃 ${message.type}`);
      return false;
    }
    const data = Buffer.from(JSON.stringify(message), 'utf-8');
    this.sendFrame(OP_TEXT, data);
    return true;
  }

  /**
   * 发送一段二进制数据（麦克风 PCM）。
   *
   * 走二进制帧而不是塞进 JSON：音频每秒 50 帧、每帧 640 字节，base64 之后还要
   * 再涨三分之一，而且两侧都要多做一次编解码。协议本来就是二进制安全的。
   */
  sendBinary(payload: Buffer | Uint8Array): boolean {
    if (!this.connected) return false;
    this.sendFrame(OP_BINARY, Buffer.isBuffer(payload) ? payload : Buffer.from(payload));
    return true;
  }

  private sendFrame(opcode: number, payload: Buffer): void {
    const socket = this.socket;
    if (!socket || socket.destroyed) return;

    const mask = crypto.randomBytes(4);
    const header = Buffer.alloc(2 + (payload.length >= 126 ? (payload.length < 65536 ? 2 : 8) : 0) + 4);
    header[0] = 0x80 | opcode;
    let offset = 2;
    if (payload.length < 126) {
      header[1] = 0x80 | payload.length;
    } else if (payload.length < 65536) {
      header[1] = 0x80 | 126;
      header.writeUInt16BE(payload.length, offset);
      offset += 2;
    } else {
      header[1] = 0x80 | 127;
      header.writeBigUInt64BE(BigInt(payload.length), offset);
      offset += 8;
    }
    mask.copy(header, offset);

    const masked = Buffer.from(payload);
    for (let i = 0; i < masked.length; i += 1) masked[i] ^= mask[i & 3];

    socket.write(Buffer.concat([header, masked]));
  }

  /** 主动关闭，并停止重连。 */
  close(): void {
    this.closing = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    const socket = this.socket;
    if (socket && !socket.destroyed) {
      try {
        this.sendFrame(OP_CLOSE, Buffer.from([0x03, 0xe8])); // 1000
      } catch {
        /* 关闭阶段的写入失败无所谓 */
      }
      socket.end();
    }
    this.socket = null;
    this.handshakeDone = false;
  }

  private onClose(): void {
    const wasConnected = this.handshakeDone;
    this.socket = null;
    this.handshakeDone = false;
    if (wasConnected) this.emit('close');
    if (this.closing) return;

    // 退避重连：sidecar 重启或端口让位时前端要能自己回来
    const delay = this.backoff;
    this.backoff = Math.min(this.backoff * 2, this.maxBackoff);
    this.emit('log', `${delay}ms 后重连 sidecar`);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  private fail(reason: string): void {
    this.emit('log', `协议错误：${reason}`);
    this.socket?.destroy();
  }
}
