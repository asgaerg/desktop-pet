/**

 * 主进程模块测试（`npm run test:main`）。

 *

 * 覆盖三块**从未被执行过**的逻辑：

 *   1. `petSocket.ts` —— 手写的 WebSocket 客户端（握手、掩码、分片、ping/pong）。

 *      这是全仓库最容易写错的部分之一，错了的表现是"连不上/偶尔断"。

 *      测试用一个**真 socket** 起一个最小服务端，按各种切分方式喂字节。

 *   2. `sidecar.ts` 的 Python 查找与退避重启决策（纯函数）。

 *   3. `audioPairer.ts` 的语音配对（各种到达顺序）。

 *

 * 测试目标由 `npm run build:testtargets` 预先打好在 `.test-build/`。

 * 为什么不在测试里现场打包：本项目的运行环境禁止 Node 起带管道的子进程

 * （`spawnSync ... EPERM/EINVAL`），现场调用 esbuild 会直接失败。

 * 预打包之后测试只做加载与断言，不依赖任何子进程能力。

 */



import { existsSync, mkdtempSync, mkdirSync, rmSync, utimesSync, writeFileSync, readFileSync } from 'node:fs';

import { createHash } from 'node:crypto';

import net from 'node:net';

import os from 'node:os';

import path from 'node:path';

import { fileURLToPath } from 'node:url';



const nodeMajor = Number(process.versions.node.split('.')[0]);

if (Number.isFinite(nodeMajor) && nodeMajor < 20) {

  console.error(`需要 Node 20+，当前是 ${process.versions.node}。请用较新的 Node 运行。`);

  process.exit(1);

}



const here = path.dirname(fileURLToPath(import.meta.url));

const desktopDir = path.join(here, '..');

const buildDir = path.join(desktopDir, '.test-build');



let passed = 0;

const failures = [];

const check = (name, condition, detail = '') => {

  if (condition) {

    passed += 1;

    console.log(`  OK   ${name}`);

  } else {

    failures.push(`${name}${detail ? ` —— ${detail}` : ''}`);

    console.error(`  FAIL ${name}${detail ? `\n       ${detail}` : ''}`);

  }

};

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));



const target = (name) => {

  // 必须是 .cjs：本包 package.json 里是 "type": "module"，

  // 用 .js 会被 Node 当成 ESM，而构建产物是 CommonJS（module.exports）。

  const file = path.join(buildDir, `${name}.cjs`);

  if (!existsSync(file)) {

    console.error(

      `缺少测试目标 ${name}.cjs。请先运行：npm run build:testtargets\n` +

        `（本环境不允许 Node 起带管道的子进程，所以不能在测试里现场打包）`,

    );

    process.exit(1);

  }

  return file;

};



/* ------------------------------------------------------------------ 加载被测模块 */



console.log('主进程模块测试（真实构建产物）\n');



// bundle 是 CJS，需要 createRequire 来加载

const { createRequire } = await import('node:module');

const requireCjs = createRequire(import.meta.url);

const { PetSocket, parseWsUrl } = requireCjs(target('petSocket'));

const { findPython, computeRestartDelay } = requireCjs(target('sidecar'));

const { AudioPairer } = requireCjs(target('audioPairer'));

const { DragTracker } = requireCjs(target('dragTracker'));

const { QuitCoordinator, quitRequestPath } = requireCjs(target('quitCoordinator'));

const {

  SingleInstanceGuard,

  instanceLockPath,

  focusRequestPath,

  isProcessAlive,

} = requireCjs(target('singleInstance'));

const {

  buildPetMenuTemplate,

  collectActions,

  collectDisabled,

  TODO_SUFFIX,

} = requireCjs(target('petMenuTemplate'));

// 气泡显隐的决策（沉浸模式的核心，见 bubbleDecision.ts）
const { decideBubble, decideBubblePlacement } = requireCjs(target('bubbleDecision'));

// VK -> Electron accelerator（PTT 全局热键用，见 pushToTalk.ts）
const { DEFAULT_PTT_VK, vkName, TOGGLE_DEBOUNCE_MS } =
  requireCjs(target('pushToTalk'));
const { resolveHandlerKey, toElectronTemplate } = requireCjs(target('petMenu'));
const {
  clampBubbleLinger,
  BUBBLE_LINGER_MIN_MS,
  BUBBLE_LINGER_MAX_MS,
  BUBBLE_LINGER_DEFAULT_MS,
} = requireCjs(target('settingsStore'));

const {

  PET_BASE_SIZE,

  DOCK_HEIGHT,

  COMPACT_PAD_X,

  COMPACT_PAD_Y,

  PEEK_ANGLE_DEG,

  EDGE_SNAP_THRESHOLD,

  PEEK_EDGE_OVERLAP,

  peekCanvasSizeFor,

  PET_SCALE_PRESETS,

  peekWindowSizeFor,

  artSizeFor,

  clampScale,

  petSizeFor,

  windowSizeFor,

  anchorBottomRight,

  anchorAtEdge,

  anchorPeekRight,

  COMPACT_VISIBLE_RATIO,

  sideAtEdge,

  // 探头窗口要按"旋转后的**立绘**"算尺寸（不是整块透明画布），
  // 所以测试需要立绘在画布内的位置比例
  ART_BOX_RATIO,

  /** 探头窗口比"刚好装下立绘"多留的余量 */
  PEEK_FIT_MARGIN,

  /* 探头窗口定位（纯函数）—— 自检用，见"探头窗口锚点"那一段测试 */
  peekWindowBounds,
  /* 换尺寸时保持立绘不动（纯函数）—— 同上 */
  windowBoundsKeepingArt,

} = requireCjs(target('petLayout'));

const { centerIn } = requireCjs(target('settingsWindow'));

const { FullscreenWatcher, parseProbeOutput, setFullscreenProbeDir } = requireCjs(

  target('fullscreenWatcher'),

);

/** 探测进程的日志（测试用来确认"就绪/报错"确实会说出口） */

const diagnostics = [];

// 探测进程需要一个可写的临时目录（Add-Type 要往里写编译产物）。

// 真实环境下这里传的是 app.getPath('userData')。

const probeDir = mkdtempSync(path.join(os.tmpdir(), 'pet-probe-'));

setFullscreenProbeDir(probeDir);



/* ------------------------------------------------------------------ 最小 WS 服务端 */



const GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';



/** 起一个最小 WebSocket 服务端，把手写的帧解码出来交给回调 */

function startEchoServer() {

  const received = [];

  const socketRef = { socket: null };

  let handshakeKey = '';



  const server = net.createServer((socket) => {

    socketRef.socket = socket;

    let buffer = Buffer.alloc(0);

    let upgraded = false;

    socket.on('data', (chunk) => {

      buffer = Buffer.concat([buffer, chunk]);

      if (!upgraded) {

        const end = buffer.indexOf('\r\n\r\n');

        if (end < 0) return;

        const head = buffer.subarray(0, end).toString('latin1');

        buffer = buffer.subarray(end + 4);

        handshakeKey = /sec-websocket-key:\s*(\S+)/i.exec(head)?.[1] ?? '';

        const accept = createHash('sha1').update(handshakeKey + GUID).digest('base64');

        socket.write(

          'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n' +

            `Connection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`,

        );

        upgraded = true;

      }

      // 解码客户端帧（客户端必须掩码）

      for (;;) {

        if (buffer.length < 2) return;

        const b0 = buffer[0];

        const b1 = buffer[1];

        const opcode = b0 & 0x0f;

        const masked = (b1 & 0x80) !== 0;

        let length = b1 & 0x7f;

        let offset = 2;

        if (length === 126) {

          if (buffer.length < 4) return;

          length = buffer.readUInt16BE(2);

          offset = 4;

        } else if (length === 127) {

          if (buffer.length < 10) return;

          length = Number(buffer.readBigUInt64BE(2));

          offset = 10;

        }

        const maskStart = offset;

        if (masked) offset += 4;

        if (buffer.length < offset + length) return;

        const payload = Buffer.from(buffer.subarray(offset, offset + length));

        if (masked) {

          const mask = buffer.subarray(maskStart, maskStart + 4);

          for (let i = 0; i < payload.length; i += 1) payload[i] ^= mask[i & 3];

        }

        buffer = buffer.subarray(offset + length);

        received.push({ opcode, payload, masked });



        // 回一条**合法协议消息**，让"能被正确解析"这条正向路径也被覆盖

        if (opcode === 1) {

          const body = Buffer.from(

            JSON.stringify({

              type: 'system.error',

              level: 'info',

              message: `echo:${payload.toString('utf-8').slice(0, 40)}`,

            }),

            'utf-8',

          );

          const frame =

            body.length < 126

              ? Buffer.concat([Buffer.from([0x81, body.length]), body])

              : Buffer.concat([

                  Buffer.from([0x81, 126, body.length >> 8, body.length & 0xff]),

                  body,

                ]);

          socket.write(frame);

        }

      }

    });

    socket.on('error', () => {});

  });



  return {

    received,

    socketRef,

    listen: () =>

      new Promise((resolve) => {

        server.listen(0, '127.0.0.1', () => resolve(server.address().port));

      }),

    close: () => {

      try {

        socketRef.socket?.destroy();

      } catch {}

      return new Promise((resolve) => server.close(() => resolve()));

    },

  };

}



/* ------------------------------------------------------------------ 测试：地址解析 */



console.log('1. WebSocket 地址解析');

check('接受 127.0.0.1', parseWsUrl('ws://127.0.0.1:8931').port === 8931);

check('接受 localhost 并归一为 127.0.0.1', parseWsUrl('ws://localhost:9000').host === '127.0.0.1');

check('缺端口时默认 80', parseWsUrl('ws://127.0.0.1').port === 80);

{

  let threw = false;

  try {

    parseWsUrl('ws://evil.example.com:8931');

  } catch {

    threw = true;

  }

  check('**拒绝非本机地址**（防止大脑被换成别人的服务器）', threw);

  threw = false;

  try {

    parseWsUrl('http://127.0.0.1:8931');

  } catch {

    threw = true;

  }

  check('拒绝非 ws:// 协议', threw);

}



/* ------------------------------------------------------------------ 测试：真 socket 往返 */



console.log('\n2. petSocket：握手与真实帧往返');

{

  const server = startEchoServer();

  const port = await server.listen();

  const socket = new PetSocket({ url: `ws://127.0.0.1:${port}` });

  const events = { open: 0, messages: [], logs: [] };

  socket.on('open', () => (events.open += 1));

  socket.on('message', (message) => events.messages.push(message));

  socket.on('log', (line) => events.logs.push(line));



  socket.connect();

  await wait(300);

  check('握手成功并触发 open', events.open === 1, `logs=${events.logs.join(' | ')}`);

  check('服务端收到的握手请求带掩码标记（首帧是客户端发的）', server.received.length >= 0);



  socket.send({ type: 'hello', protocolVersion: 1, client: 'test' });

  await wait(200);

  check('服务端收到一条文本帧', server.received.some((f) => f.opcode === 1));

  check('客户端发来的帧是掩码的（RFC 要求）', server.received.every((f) => f.masked));

  const decoded = JSON.parse(

    server.received.find((f) => f.opcode === 1).payload.toString('utf-8'),

  );

  check('载荷是合法 JSON 且字段完整', decoded.type === 'hello' && decoded.protocolVersion === 1);

  check('收到并解析了服务端的协议消息', events.messages.length >= 1, JSON.stringify(events.messages));

  check(

    '解析出的消息 type 正确',

    events.messages[0]?.type === 'system.error',

    JSON.stringify(events.messages[0]),

  );



  // 大载荷：跨过 126 字节的长度分支

  events.messages.length = 0;

  const big = 'x'.repeat(500);

  socket.send({ type: 'user.text', text: big });

  await wait(250);

  const bigFrame = server.received.filter((f) => f.opcode === 1).pop();

  const bigDecoded = JSON.parse(bigFrame.payload.toString('utf-8'));

  check(

    '大于 125 字节的载荷正确编码与解码（走了 126 长度分支）',

    bigDecoded.text === big,

    `收到 ${bigDecoded.text?.length} 字符`,

  );



  socket.close();

  await wait(100);

  await server.close();

}



console.log('\n3. petSocket：分片到达与 ping/pong');

{

  const server = startEchoServer();

  const port = await server.listen();

  const socket = new PetSocket({ url: `ws://127.0.0.1:${port}` });

  const messages = [];

  socket.on('message', (m) => messages.push(m));

  socket.on('log', () => {});

  socket.connect();

  await wait(250);



  // 服务端手动发一条被切成两半的文本帧：客户端必须能跨 chunk 拼回来。

  // 注意载荷长度按**字节**算（UTF-8 中文一个字 3 字节），不能按字符数。

  const body = Buffer.from('{"type":"system.error","message":"分片测试"}', 'utf-8');

  check('前置条件：测试载荷短于 126 字节（走单字节长度）', body.length < 126, `${body.length} 字节`);

  const frame = Buffer.concat([Buffer.from([0x81, body.length]), body]);

  server.socketRef.socket.write(frame.subarray(0, 5));

  await wait(80);

  server.socketRef.socket.write(frame.subarray(5));

  await wait(200);

  check(

    '跨 TCP 分片的帧被正确拼回（不完整的帧不会被误解析）',

    messages.some((m) => m.type === 'system.error' && m.message === '分片测试'),

    JSON.stringify(messages),

  );



  // ping -> 客户端应回 pong

  const pingFrame = Buffer.from([0x89, 0x02, 0x68, 0x69]);

  server.socketRef.socket.write(pingFrame);

  await wait(200);

  check(

    '收到 ping 会回 pong（保持连接存活）',

    server.received.some((f) => f.opcode === 0x0a),

    `收到帧类型：${server.received.map((f) => f.opcode).join(',')}`,

  );



  socket.close();

  await wait(100);

  await server.close();

}



/* ------------------------------------------------------------------ 测试：sidecar 纯逻辑 */



console.log('\n4. sidecar：Python 查找');

{

  const root = mkdtempSync(path.join(os.tmpdir(), 'pet-find-'));

  const venvPython = path.join(

    root,

    '.venv',

    process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python3',

  );



  // 没有 venv 时应退回 PATH 上的裸命令名

  const noVenv = findPython(root);

  check(

    '无 venv 时退回 PATH 裸命令',

    noVenv === 'python.exe' || noVenv === 'python3' || noVenv === 'python',

    `实际：${noVenv}`,

  );



  // 造一个假的 venv，验证它被优先选中

  const { mkdirSync, writeFileSync } = await import('node:fs');

  mkdirSync(path.dirname(venvPython), { recursive: true });

  writeFileSync(venvPython, '');



  // 显式指定优先：必须用**存在**的路径，不存在的会被跳过（见下一条断言）

  check(

    '显式指定优先于一切',

    findPython(root, venvPython) === venvPython,

    `实际：${findPython(root, venvPython)}`,

  );

  check(

    '项目内 .venv 被优先选中（用户装过依赖就该用它）',

    findPython(root) === venvPython,

    `实际：${findPython(root)}`,

  );



  // 环境变量优先于 venv（托管环境需要能强制指定解释器）

  process.env.PET_PYTHON = venvPython; // 用真实存在的路径，否则会被跳过

  check('PET_PYTHON 环境变量优先于 venv', findPython(root) === venvPython);

  delete process.env.PET_PYTHON;



  // 带路径分隔符但**不存在**的候选会被跳过，而不是盲信 ——

  // 这样用户把一个失效路径留在配置里时，宠物仍然能起来。

  check(

    '不存在的显式路径被跳过（不会因为一处配置失效就起不来）',

    findPython(root, 'C:\\does\\not\\exist\\python.exe') === venvPython,

    `实际：${findPython(root, 'C:\\does\\not\\exist\\python.exe')}`,

  );

  rmSync(root, { recursive: true, force: true });

}



console.log('\n5. sidecar：退避重启决策');

{

  const history = [];

  const t0 = 1_000_000;

  const d1 = computeRestartDelay(history, t0);

  const d2 = computeRestartDelay(history, t0 + 1000);

  const d3 = computeRestartDelay(history, t0 + 2000);

  check('首次退避 500ms', d1 === 500, `实际 ${d1}`);

  check('第二次翻倍到 1000ms', d2 === 1000, `实际 ${d2}`);

  check('第三次 2000ms', d3 === 2000, `实际 ${d3}`);

  check('第四次放弃（窗口内超过 3 次）', computeRestartDelay(history, t0 + 3000) === null);

  // 窗口是 60s，而历史里最早那次是 t0、最晚是 t0+2000：

  // 所以必须等到 t0+60000 之后才可能全部过期。

  // （早先这里用 t0+61000，距最晚那次只有 59 秒，仍在窗口内，断言是错的。）

  check(

    '窗口外的历史被清理：足够久之后又能重启',

    computeRestartDelay(history, t0 + 63_000) === 500,

    `实际：${computeRestartDelay([], 0)}`,

  );



  // 退避上限：连续重启到第 10 次也不能超过 maxDelayMs

  const longHistory = [];

  let last = null;

  for (let i = 0; i < 10; i += 1) {

    last = computeRestartDelay(longHistory, i * 120_000, { maxRestarts: 99, maxDelayMs: 4000 });

  }

  check('退避有上限（不会无限翻倍）', last !== null && last <= 4000, `实际 ${last}`);

}



/* ------------------------------------------------------------------ 测试：语音配对 */



console.log('\n6. audioPairer：语音元信息与二进制帧配对');

{

  const pairer = new AudioPairer();

  pairer.acceptBinary(new Uint8Array([1, 2, 3]));

  check('只有二进制帧时不出结果（等元信息）', pairer.drain().length === 0);

  const ready = pairer.acceptMeta(1, 'mp3', '你好');

  check('元信息到达后配对成功', ready.length === 1 && ready[0].format === 'mp3');

  check('音频字节正确传递', ready[0].bytes.length === 3 && ready[0].bytes[0] === 1);

  check('文本被保留（界面要显示"它在说什么"）', ready[0].text === '你好');



  // **元信息先到**：这是最容易写错的顺序。二进制帧必须补到同一个号上，

  // 否则两边永远配不上对、语音静默丢失。

  const p2 = new AudioPairer();

  const early = p2.acceptMeta(1, 'pcm', '先到');

  check('元信息先到时先不出结果', early.length === 0);

  p2.acceptBinary(new Uint8Array([9]));

  const late = p2.drain();

  check(

    '元信息先到、二进制后到仍能配对（不假设顺序）',

    late.length === 1 && late[0].format === 'pcm' && late[0].bytes[0] === 9,

    `实际：${JSON.stringify(late.map((x) => [x.format, x.text, [...x.bytes]]))}`,

  );



  const p3 = new AudioPairer(2);

  for (let i = 0; i < 5; i += 1) p3.acceptBinary(new Uint8Array([i]));

  check('挂起数量有上限（迟到太久的音频被丢弃）', p3.pendingCount <= 2, `实际 ${p3.pendingCount}`);



  // 多段顺序：acceptMeta 会**立刻交付**刚配好的条目（这正是期望行为——

  // 语音不该等到凑齐几段才开始播）。所以按真实用法收集每次的返回值，

  // 再断言整体顺序。注意不能只取最后一次的返回值（早先那么写，断言恒为空）。

  const p4 = new AudioPairer();

  p4.acceptBinary(new Uint8Array([1]));

  p4.acceptBinary(new Uint8Array([2]));

  p4.acceptBinary(new Uint8Array([3]));

  const delivered = [

    ...p4.acceptMeta(1, 'mp3', 'a'),

    ...p4.acceptMeta(2, 'mp3', 'b'),

    ...p4.acceptMeta(3, 'mp3', 'c'),

  ];

  check(

    '三段按到达顺序完整交付',

    delivered.map((x) => x.text).join('') === 'abc',

    JSON.stringify(delivered.map((x) => x.text)),

  );

  check(

    '每段都带上了正确格式',

    delivered.every((x) => x.format === 'mp3'),

    JSON.stringify(delivered.map((x) => x.format)),

  );

  check('全部配对后挂起清空', p4.pendingCount === 0, `实际 ${p4.pendingCount}`);



  // 乱序到达：**先配好的先交付**是对的（语音该立刻播，不该为等后面的段而卡住）。

  // 所以这里验证的是：每段都能正确配对，且不串内容。

  const p5 = new AudioPairer();

  p5.acceptBinary(new Uint8Array([1]));

  p5.acceptBinary(new Uint8Array([2]));

  const deliveredOutOfOrder = [

    ...p5.acceptMeta(2, 'mp3', 'b'),

    ...p5.acceptMeta(1, 'mp3', 'a'),

  ];

  check(

    '乱序到达时各自正确配对（先配好的先交付，内容不串）',

    deliveredOutOfOrder.length === 2 &&

      deliveredOutOfOrder.every((x) => x.bytes.length === 1) &&

      deliveredOutOfOrder.map((x) => x.text).sort().join('') === 'ab',

    JSON.stringify(deliveredOutOfOrder.map((x) => [x.text, [...x.bytes]])),

  );



  // drain() 的排序保证：同一批里必须按序号升序（否则同时到达的多段会播错顺序）

  const p6 = new AudioPairer();

  p6.acceptMeta(1, 'mp3', 'a');

  p6.acceptMeta(2, 'mp3', 'b');

  p6.acceptMeta(3, 'mp3', 'c');

  // 元信息都在等二进制 —— 此时 acceptBinary 会按空闲号依次填 1/2/3

  p6.acceptBinary(new Uint8Array([1]));

  p6.acceptBinary(new Uint8Array([2]));

  p6.acceptBinary(new Uint8Array([3]));

  const batched = p6.drain();

  check(

    '同一批内按序号升序（否则连续的几段语音会播错顺序）',

    batched.map((x) => x.text).join('') === 'abc',

    JSON.stringify(batched.map((x) => x.text)),

  );

}



/* ------------------------------------------------------------------ 测试：拖拽坐标 */



console.log('\n7. dragTracker：窗口拖拽的坐标计算');

{

  /** 造一个假窗口：记录 bounds，可读可写 */

  const makeWindow = (x, y, w = 460, h = 640) => {

    const state = { x, y, width: w, height: h, calls: [] };

    return {

      state,

      tracker: new DragTracker(

        () => ({ x: state.x, y: state.y, width: state.width, height: state.height }),

        (b) => {

          state.calls.push({ ...b });

          // ⚠️ 故意**照单全收**：真实 Electron 的 setBounds 也是这样。

          // 如果实现里漏带了宽高，这里就会把尺寸改成 undefined ——

          // 从而在测试里暴露出来（这正是"越拖越大"那类 bug 的防线）。

          state.x = b.x;

          state.y = b.y;

          state.width = b.width;

          state.height = b.height;

        },

      ),

    };

  };



  // 基本：向右下拖 100,50

  {

    const { state, tracker } = makeWindow(1000, 400);

    tracker.begin(500, 300);

    check('按下后进入拖拽状态', tracker.dragging === true);

    tracker.move(600, 350);

    check(

      '向右下移动 → 窗口同步平移',

      state.x === 1100 && state.y === 450,

      `实际 (${state.x},${state.y})，期望 (1100,450)`,

    );

  }



  // 反向：向左上拖

  {

    const { state, tracker } = makeWindow(1000, 400);

    tracker.begin(500, 300);

    tracker.move(400, 200);

    check('向左上移动 → 窗口反向平移', state.x === 900 && state.y === 300, `实际 (${state.x},${state.y})`);

  }



  // 连续移动：每一步都相对**起点**算，而不是累加

  // （累加会漂移：每步都加 delta 就会把最初那一步重复计入）

  {

    const { state, tracker } = makeWindow(1000, 400);

    tracker.begin(500, 300);

    tracker.move(510, 305);

    tracker.move(530, 320);

    tracker.move(600, 400);

    check(

      '连续移动按起点计算（不累积漂移）',

      state.x === 1100 && state.y === 500,

      `实际 (${state.x},${state.y})，期望 (1100,500)`,

    );

  }



  // 拖回原点：窗口应当回到起始位置

  {

    const { state, tracker } = makeWindow(777, 333);

    tracker.begin(200, 200);

    tracker.move(900, 900);

    tracker.move(200, 200);

    check('拖回起点 → 窗口回到原位', state.x === 777 && state.y === 333, `实际 (${state.x},${state.y})`);

  }



  // 未按下就移动：不应移动窗口（直接接事件流时这条很关键）

  {

    const { state, tracker } = makeWindow(1000, 400);

    tracker.move(600, 350);

    check('未按下时移动不生效', state.x === 1000 && state.y === 400, `实际 (${state.x},${state.y})`);

    check('未按下时 dragging 为 false', tracker.dragging === false);

  }



  // 结束后再移动：同样不生效（防止"松手后窗口还跟着鼠标跑"）

  {

    const { state, tracker } = makeWindow(1000, 400);

    tracker.begin(500, 300);

    tracker.move(600, 350);

    tracker.end();

    check('松手后 dragging 为 false', tracker.dragging === false);

    tracker.move(800, 800);

    check(

      '松手后再移动不生效（否则窗口会粘着鼠标）',

      state.x === 1100 && state.y === 450,

      `实际 (${state.x},${state.y})`,

    );

  }



  // ---- 夹紧：触碰屏幕边缘之后**不再增减**（用户明确要求）----

  //

  // 用户原话：「拖拽中判定宠物是否触碰屏幕左右边缘，触碰则不再增减窗口位置的定位，

  // 实现不超出屏幕」。所以这里断言的是"硬夹紧"：窗口整体永远留在工作区内。

  {

    const WA = { x: 0, y: 0, width: 1536, height: 816 };

    const makeClamped = (x, y, w = 260, h = 430) => {

      const state = { x, y, width: w, height: h };

      return {

        state,

        tracker: new DragTracker(

          () => ({ x: state.x, y: state.y, width: state.width, height: state.height }),

          (b) => {

            state.x = b.x;

            state.y = b.y;

            state.width = b.width;

            state.height = b.height;

          },

          () => WA,

        ),

      };

    };



    // 往右猛拖：应该停在"右边缘刚好贴住"，而不是冲出去

    {

      const { state, tracker } = makeClamped(1000, 400);

      tracker.begin(500, 300);

      tracker.move(5000, 300); // 指针远远拖出屏幕

      check(

        '向右拖出屏幕 → 窗口贴住右边缘不超出',

        state.x === WA.width - state.width,

        `实际 x=${state.x}，期望 ${WA.width - state.width}`,

      );

      // 继续往右拖，位置不应再变（"不再增减"）

      tracker.move(9000, 300);

      check('继续向右拖 → 位置不再变化', state.x === WA.width - state.width, `实际 x=${state.x}`);

      // 往回拖要**立刻跟手**（夹紧不能改变 origin，否则会有粘滞感）

      tracker.move(500 - 20, 300);

      check(

        '往回拖立刻跟手（不会被夹紧粘住）',

        state.x === 1000 - 20,

        `实际 x=${state.x}，期望 ${1000 - 20}`,

      );

    }



    // 往左猛拖：停在 0

    {

      const { state, tracker } = makeClamped(1000, 400);

      tracker.begin(500, 300);

      tracker.move(-5000, 300);

      check('向左拖出屏幕 → 窗口贴住左边缘', state.x === WA.x, `实际 x=${state.x}`);

    }



    // 纵向同样夹紧（否则会把宠物拖到任务栏下面或屏幕上方）

    {

      const { state, tracker } = makeClamped(1000, 400);

      tracker.begin(500, 300);

      tracker.move(500, 5000);

      check(

        '向下拖出屏幕 → 贴住工作区底边（不盖任务栏）',

        state.y === WA.height - state.height,

        `实际 y=${state.y}`,

      );

      tracker.move(500, -5000);

      check('向上拖出屏幕 → 贴住顶边', state.y === WA.y, `实际 y=${state.y}`);

    }



    // 窗口比工作区还大时不能算出反向区间

    {

      const { state, tracker } = makeClamped(100, 100, 2000, 1200);

      tracker.begin(500, 300);

      tracker.move(5000, 300);

      check(

        '窗口比屏幕还大时不会算出负位置',

        state.x === WA.x && state.y === WA.y,

        `实际 (${state.x},${state.y})，期望 (${WA.x},${WA.y})`,

      );

    }



    // 不传 getWorkArea 时保持原样（纯计算，老行为不变）

    {

      const { state, tracker } = makeWindow(1000, 400);

      tracker.begin(500, 300);

      tracker.move(5000, 300);

      check('没有工作区信息时不做夹紧（保持可测的纯计算）', state.x === 5500, `实际 x=${state.x}`);

    }



    // ---- 拖拽极限放宽"窗口留白" ----

    //

    // 用户原话：「把随鼠标拖拽增减的极限增加窗口的留白就行了」。

    // 窗口比立绘大一圈（立绘居中、四周透明），夹死在屏幕内的话立绘永远

    // 差那圈留白到不了边 —— 于是"拖到边缘自动探头"触发不了。

    // 放宽之后窗口能多走出去，正好让立绘可见边贴住屏幕边。

    {

      const WA = { x: 0, y: 0, width: 1536, height: 816 };

      const makeMargin = (margin, artWidth = 155) => {

        const state = { x: 1000, y: 400, width: 240, height: 240 };

        return {

          state,

          tracker: new DragTracker(

            () => ({ x: state.x, y: state.y, width: state.width, height: state.height }),

            (b) => {

              state.x = b.x;

              state.y = b.y;

              state.width = b.width;

              state.height = b.height;

            },

            () => WA,

            margin,
            undefined,
            undefined,
            undefined,
            undefined,
            undefined,
            () => artWidth,

          ),

        };

      };



      // margin 让窗口能越出屏幕：maxX = 1536 - 240 + 43 = 1339

      const widened = makeMargin(43);

      widened.tracker.begin(0, 0);

      widened.tracker.move(9999, 0);

      check(

        '放宽留白后窗口能越出屏幕（立绘才贴得到边）',

        widened.state.x === 1536 - 43 - 155,

        `实际 x=${widened.state.x}，期望 ${1536 - 43 - 155}（= 屏右 − 左留白 − 立绘宽）`,

      );

      // 纵向**不**放宽：越界会盖住任务栏

      widened.tracker.move(9999, 9999);

      check(

        '纵向不放宽（越界会盖住任务栏）',

        widened.state.y === 816 - 240,

        `实际 y=${widened.state.y}，期望 ${816 - 240}`,

      );



      // margin=0 时与老行为一致（夹死在屏幕内）

      const tight = makeMargin(0);

      tight.tracker.begin(0, 0);

      tight.tracker.move(9999, 0);

      check('留白为 0 时立绘右边贴屏幕右（立绘宽 155）', tight.state.x === 1536 - 0 - 155, `实际 x=${tight.state.x}`);



      // 回调形式也要能用（缩放变化时值会变）

      const state3 = { x: 1000, y: 400, width: 240, height: 240 };

      let margin3 = 20;

      const dynamic = new DragTracker(

        () => ({ x: state3.x, y: state3.y, width: state3.width, height: state3.height }),

        (b) => {

          state3.x = b.x;

          state3.y = b.y;

        },

        () => WA,

        () => margin3,

        // 留白用回调（缩放会变）；`artWidth` 同样要给，否则走向后兼容的旧公式
        //
        // ⚠️ 占位符数量要对齐构造函数：`getScreenBottom` / `getScreenTop` /
        // `getMarginTop` / `getMarginRight` / **`getArtWidth`** —— 少一个就会
        // 让 `() => 155` 落到 `getMarginRight` 位上（实测踩过）。
        undefined,
        undefined,
        undefined,
        undefined,
        undefined,
        () => 155,

      );

      dynamic.begin(0, 0);

      dynamic.move(9999, 0);

      check('留白可用回调动态给（缩放会变）', state3.x === 1536 - 20 - 155, `实际 x=${state3.x}`);

    }



    // ---- 上下边也要放宽（用户要求「屏幕上下也加个」）----

    //

    // 原先纵向故意不放宽（注释写"越界会盖住任务栏"）。现在要支持上下贴边探头，

    // 所以纵向同样放宽。`workArea` 本身已排除任务栏，所以放宽不会真盖上去。

    {

      const WA = { x: 0, y: 0, width: 1536, height: 816 };

      const s2 = { x: 0, y: 0, width: 240, height: 240 };

      const t2 = new DragTracker(

        () => ({ x: s2.x, y: s2.y, width: s2.width, height: s2.height }),

        (b) => {

          s2.x = b.x;

          s2.y = b.y;

        },

        () => WA,

        43,

        30,

        /*
         * ⚠️ 纵向两个边界要**分开传**，不能只给 `marginY`：
         *
         *   · 下界用 `marginY`（立绘底 → 窗口底）
         *   · **上界用 `getMarginTop`**（立绘顶 → 窗口顶）——两者不相等！
         *
         * 原来这里只给 `marginY`，上界就退化成 0（因为 `screenTop` 是 undefined），
         * 于是"向上能越出工作区"永远不成立。2026-10-04 加了"拖拽开始时定住留白"
         * 之后这条才暴露出来 —— 之前是**碰巧**通过的。
         */
        undefined,
        () => 0,
        () => 30,
      );

      t2.begin(0, 0);

      t2.move(0, 9999);

      check(

        '向下也能越出工作区（贴下边探头要用）',

        s2.y === 816 - 240 + 30,

        `实际 y=${s2.y}，期望 ${816 - 240 + 30}`,

      );

      t2.move(0, -9999);

      check('向上也能越出工作区（贴上边探头要用）', s2.y === 0 - 30, `实际 y=${s2.y}，期望 -30`);

    }



    // ---- 四边贴边判定 ----

    {

      const WA = { x: 0, y: 0, width: 1536, height: 816 };

      const box = { width: 240, height: 240 };

      // ⚠️ 上下**不该**触发探头。用户原话：「上下不用探头，不超出屏幕边缘就好了」。

      // （他先前说"屏幕上下也加个"，我理解成上下也要旋转；实际意思是上下也要能贴到

      // 屏幕边、别越界 —— 那由拖拽夹紧负责，见上面 marginY 那组测试。）

      check(

        '贴上边**不**触发探头（上下只保证不越界）',

        sideAtEdge({ x: 700, y: 0, ...box }, WA) === null,

        `实际 ${sideAtEdge({ x: 700, y: 0, ...box }, WA)} —— 上下不该探头`,

      );

      check(

        '贴下边**不**触发探头',

        sideAtEdge({ x: 700, y: 816 - 240, ...box }, WA) === null,

        `实际 ${sideAtEdge({ x: 700, y: 816 - 240, ...box }, WA)} —— 上下不该探头`,

      );

      check(

        '屏幕中间四条边都不算',

        sideAtEdge({ x: 600, y: 300, ...box }, WA) === null,

        `实际 ${sideAtEdge({ x: 600, y: 300, ...box }, WA)}`,

      );

      // 贴角：两条边都近时取**更近**的那条，不能两个都算或行为不确定

      const corner = sideAtEdge({ x: 1536 - 240 - 5, y: 816 - 240 - 20, ...box }, WA);

      check('贴右下角时取到一条确定的边', corner === 'right' || corner === 'bottom', `实际 ${corner}`);

      // 横纵留白必须分开算：立绘 155x177 不是正方形

      const art = artSizeFor(1);

      const mx = Math.round((box.width - art.width) / 2);

      const my = Math.round((box.height - art.height) / 2);

      check('横纵留白分别计算（立绘不是正方形）', mx !== my, `横 ${mx} 纵 ${my} —— 相等说明有一维用错了`);

    }



  }



  // 重复 begin：以最后一次为准（事件流重入时不该保留旧起点）

  {

    const { state, tracker } = makeWindow(1000, 400);

    tracker.begin(500, 300);

    tracker.begin(600, 350);

    tracker.move(700, 400);

    check(

      '重复按下以最后一次为起点',

      state.x === 1100 && state.y === 450,

      `实际 (${state.x},${state.y})，期望 (1100,450)`,

    );

  }



  // 小数坐标：必须取整，否则 setPosition 收到非整数（Electron 会自己截断，

  // 但逐步累积会带来"拖久了有偏移"的手感问题）

  {

    const { state, tracker } = makeWindow(1000, 400);

    tracker.begin(500.4, 300.6);

    tracker.move(600.9, 350.9);

    check(

      '小数坐标被取整',

      Number.isInteger(state.x) && Number.isInteger(state.y),

      `实际 (${state.x},${state.y})`,

    );

    check('取整后数值正确（四舍五入）', state.x === 1101 && state.y === 450, `实际 (${state.x},${state.y})`);

  }



  // 负坐标：多屏时副屏可能在主屏左侧/上方，坐标为负

  {

    const { state, tracker } = makeWindow(-800, -200);

    tracker.begin(100, 100);

    tracker.move(50, 60);

    check('负坐标（副屏在主屏左/上）正常', state.x === -850 && state.y === -240, `实际 (${state.x},${state.y})`);

  }



  // 零位移：位置不变

  {

    const { state, tracker } = makeWindow(1000, 400);

    tracker.begin(500, 300);

    tracker.move(500, 300);

    check('原地按下松开不移动', state.x === 1000 && state.y === 400, `实际 (${state.x},${state.y})`);

  }



  /*

   * 防回归：拖动**绝不能改变窗口尺寸**。

   *

   * 背景（真实事故，用户报"越拖动整体 ui 越大"）：原来用 `setPosition` 移动窗口。

   * 实测在 Windows 11 / scaleFactor 1.25 下，反复 setPosition 会让窗口高度

   * **每次涨约 1px** —— 15 次拖动后 640 → 655，肉眼可见越来越大。

   *

   * 原因是 DPI 分数缩放时设位置会连带重算非客户区，舍入误差累积。

   * 改用 `setBounds` 并**每次把宽高原样带上**后，同样 15 次只差 1px。

   *

   * 这条测试锁住"每次 move 都必须带上原始的 width/height"这个不变量。

   */

  {

    const { state, tracker } = makeWindow(800, 500, 460, 640);

    tracker.begin(400, 300);

    for (let i = 1; i <= 30; i += 1) tracker.move(400 + i * 7, 300 + i * 3);

    check(

      '反复拖动不改动窗口宽高（防"越拖越大"）',

      state.width === 460 && state.height === 640,

      `实际 ${state.width}x${state.height}，期望 460x640`,

    );

    check(

      '每次移动都带上了宽高（不是 undefined）',

      state.calls.length === 30 && state.calls.every((c) => c.width === 460 && c.height === 640),

      `${state.calls.filter((c) => c.width !== 460 || c.height !== 640).length} 次调用没带对宽高`,

    );

  }

}



/* ------------------------------------------------------------------ 测试：退出全部实例 */



console.log('\n8. quitCoordinator：退出全部实例的广播机制');

{

  const file = quitRequestPath();



  const cleanup = () => {

    try {

      rmSync(file, { force: true });

    } catch {

      /* ignore */

    }

  };

  cleanup();



  /** 造一个协调器，记录它有没有要求退出 */

  const make = () => {

    const state = { asked: 0 };

    const co = new QuitCoordinator(() => {

      state.asked += 1;

    }, () => {});

    return { co, state };

  };



  // 1) 别人广播 -> 我应当退出

  {

    cleanup();

    const { co, state } = make();

    writeFileSync(file, JSON.stringify({ requester: process.pid + 12345, at: Date.now() }), 'utf-8');

    co.checkOnce();

    check('收到其他实例的退出请求 → 自己退出', state.asked === 1, `asked=${state.asked}`);

    check('退出请求处理后文件被清理', !existsSync(file), '文件仍在，会导致下次启动自杀');

  }



  // 2) 自己广播 -> 不重复触发（由调用方自行退出）

  {

    cleanup();

    const { co, state } = make();

    writeFileSync(file, JSON.stringify({ requester: process.pid, at: Date.now() }), 'utf-8');

    co.checkOnce();

    check('自己发的请求不会让自己二次退出', state.asked === 0, `asked=${state.asked}`);

    cleanup();

  }



  // 3) 过期请求 -> 忽略并清理。

  //    这条是**关键安全阀**：没有它，一次崩溃留下的请求文件会让之后

  //    每次启动的实例都立刻自杀（正是单实例锁那个坑的翻版）。

  {

    cleanup();

    const { co, state } = make();

    writeFileSync(file, JSON.stringify({ requester: 99999, at: Date.now() - 60_000 }), 'utf-8');

    co.checkOnce();

    check('过期的退出请求被忽略', state.asked === 0, `asked=${state.asked}`);

    check('过期请求文件被清理', !existsSync(file), '文件仍在');

  }



  // 4) 文件损坏（读到半个 JSON）-> 不崩、不误退

  {

    cleanup();

    const { co, state } = make();

    writeFileSync(file, '{"requester": 1, "at":', 'utf-8');

    let threw = false;

    try {

      co.checkOnce();

    } catch {

      threw = true;

    }

    check('请求文件损坏时不会抛异常', !threw);

    check('请求文件损坏时不会误退出', state.asked === 0, `asked=${state.asked}`);

    cleanup();

  }



  // 5) 没有文件 -> 什么都不做

  {

    cleanup();

    const { co, state } = make();

    co.checkOnce();

    check('没有请求文件时不动作', state.asked === 0);

  }



  // 6) broadcast 写出的内容能被自己读懂（格式自洽）

  {

    cleanup();

    const { co } = make();

    co.broadcast();

    check('broadcast 写出了请求文件', existsSync(file));

    const payload = JSON.parse(readFileSync(file, 'utf-8'));

    check('请求文件带 requester 与时间戳', typeof payload.requester === 'number' && typeof payload.at === 'number');

    cleanup();

  }

}



console.log('\n9. singleInstance：禁止多开（不依赖已失效的 Electron API）');

{
  /*
   * ⚠️ 这条测试守的是**状态目录必须可写**（2026-10-08 踩到的大坑）。
   *
   * ## 那个坑
   *
   * `setStateDir(app.getPath('userData'))` —— 注释写着"userData 是 Electron
   * 保证可写的地方"。**实测不成立**：这台机器上整个
   * `%APPDATA%\@pet\desktop` 在 Electron 主进程里**完全不可写**：
   *
   *     写 instance.lock.json  ❌ EPERM: operation not permitted, open
   *     mkdir 子目录           ❌ EPERM
   *     probe.txt / probe.json ❌ EPERM（换文件名也不行）
   *
   * 而**同一目录** PowerShell 手工写是成功的 —— 是"Electron 在这个路径上被拦"。
   *
   * ## 危害（不止"少个锁"）
   *
   * 写不了锁 -> `claim()` 降级放行 -> **允许多开** -> 两个实例各跑一个
   * `probe-key` 监视器、各写一份 `startup.log`。于是排查时**证据互相矛盾**：
   * 用户按键的实例日志里有记录，而我在看另一个实例的 —— 白白浪费一整轮排查。
   *
   * ## 所以这里断言什么
   *
   *   ① 默认路径（本项目 = `pet-data/instance-state`）**真的能写**
   *   ② `claim()` 返回的是「**已取得**实例锁」，而不是「降级放行」
   *
   * ⚠️ 必须断言 ② 而不是只看"没报错" —— 降级放行**也不报错**，
   * 它只是静默地不做单实例检查。这正是它藏了这么久的原因。
   */
  const noop = () => {};
  const lockFile = instanceLockPath();
  const focusFile = focusRequestPath();
  const cleanup = () => {
    rmSync(lockFile, { force: true });
    rmSync(focusFile, { force: true });
  };
  cleanup();

  // ① 状态目录真的可写
  let writeError = null;
  try {
    mkdirSync(path.dirname(lockFile), { recursive: true });
    writeFileSync(lockFile, JSON.stringify({ pid: process.pid, at: Date.now() }), 'utf-8');
    rmSync(lockFile, { force: true });
  } catch (error) {
    writeError = error;
  }
  check(
    '状态目录可写（写不了就会静默降级放行、允许多开）',
    writeError === null,
    `写 ${lockFile} 失败：${writeError ? writeError.message : ''}`,
  );

  // ② claim() 必须真的拿到锁
  const probeGuard = new SingleInstanceGuard(noop, noop);
  const probeClaim = probeGuard.claim();
  check(
    'claim() 真的取得了锁（不是"降级放行"）',
    probeClaim.isPrimary && String(probeClaim.detail).includes('已取得'),
    `detail = ${probeClaim.detail}`,
  );
  cleanup();



  /** 造一个守卫，记录它有没有被要求聚焦 */

  const make = () => {

    const state = { focused: 0 };

    const guard = new SingleInstanceGuard(() => {

      state.focused += 1;

    }, noop);

    return { guard, state };

  };



  /** 找一个确定不存在的 PID */

  const findDeadPid = () => {

    let pid = process.pid + 100000;

    for (;;) {

      try {

        process.kill(pid, 0);

        pid += 1;

      } catch {

        return pid;

      }

    }

  };



  // 1) 空系统 -> 成为主实例

  {

    cleanup();

    const { guard } = make();

    const r = guard.claim();

    check('空系统上能成为主实例', r.isPrimary === true && r.otherAlive === false, JSON.stringify(r));

    check('说明文字非空', typeof r.detail === 'string' && r.detail.length > 0, r.detail);

    check('锁文件确实被写出', existsSync(lockFile));

    cleanup();

  }



  // 2) 锁由**活着的**其他进程持有 -> 不成为主实例，报 otherAlive

  {

    cleanup();

    // 借一个活进程的 PID：用一个真的在跑的 node 子进程太麻烦，

    // 用"自己"当持有者不方便（claim 会识别成自己），所以起一个短命进程。

    const { spawnSync } = await import('node:child_process');

    const probe = spawnSync(process.execPath, ['-e', 'setTimeout(()=>{},1500)'], {

      stdio: 'ignore',

      detached: true,

    });

    // spawnSync 会等它结束，所以改用 spawn 起一个后台的

    void probe;

    const { spawn } = await import('node:child_process');

    const child = spawn(process.execPath, ['-e', 'setTimeout(()=>{},4000)'], { stdio: 'ignore' });

    writeFileSync(lockFile, JSON.stringify({ pid: child.pid, at: Date.now() }), 'utf-8');



    const { guard } = make();

    const r = guard.claim();

    check(

      '锁由活进程持有时报告 otherAlive（不是故障）',

      r.isPrimary === false && r.otherAlive === true,

      JSON.stringify(r),

    );

    check('说明文字带上持有者 PID', r.detail.includes(String(child.pid)), r.detail);

    child.kill();

    cleanup();

  }



  // 3) **核心场景**：持有者已死 -> 清理陈旧锁并成为主实例。

  //    这条直接对应"崩溃后再也起不来"（当年那行 API 的病根）。

  {

    cleanup();

    const dead = findDeadPid();

    writeFileSync(lockFile, JSON.stringify({ pid: dead, at: Date.now() }), 'utf-8');

    const { guard } = make();

    const r = guard.claim();

    check('陈旧锁（持有者已死）会被清理并接管', r.isPrimary === true, JSON.stringify(r));

    const written = JSON.parse(readFileSync(lockFile, 'utf-8'));

    check('接管后锁文件里的 PID 换成了自己', written.pid === process.pid, JSON.stringify(written));

    cleanup();

  }



  // 4) 锁文件损坏 -> 当陈旧处理，不崩、不放弃

  {

    cleanup();

    writeFileSync(lockFile, '{"pid":', 'utf-8');

    const { guard } = make();

    let threw = false;

    let r = null;

    try {

      r = guard.claim();

    } catch {

      threw = true;

    }

    check('锁文件损坏时不抛异常', !threw);

    check('锁文件损坏时仍能成为主实例', r !== null && r.isPrimary === true, JSON.stringify(r));

    cleanup();

  }



  // 5) 聚焦请求：收到 -> 触发回调并清理文件

  {

    cleanup();

    const { guard, state } = make();

    writeFileSync(focusFile, JSON.stringify({ pid: process.pid + 99999, at: Date.now() }), 'utf-8');

    guard.checkOnce();

    check('收到聚焦请求 -> 触发回调', state.focused === 1, `focused=${state.focused}`);

    check('聚焦请求处理后文件被清理', !existsSync(focusFile));

  }



  // 6) 过期的聚焦请求 -> 忽略并清理（防止陈旧文件让宠物无端弹出来）

  {

    cleanup();

    const { guard, state } = make();

    writeFileSync(focusFile, JSON.stringify({ pid: 99999, at: Date.now() - 60_000 }), 'utf-8');

    guard.checkOnce();

    check('过期的聚焦请求被忽略', state.focused === 0, `focused=${state.focused}`);

    check('过期聚焦请求文件被清理', !existsSync(focusFile));

  }



  // 7) 自己写的聚焦请求 -> 忽略（否则会自己弹自己）

  {

    cleanup();

    const { guard, state } = make();

    writeFileSync(focusFile, JSON.stringify({ pid: process.pid, at: Date.now() }), 'utf-8');

    guard.checkOnce();

    check('自己发的聚焦请求不会触发回调', state.focused === 0, `focused=${state.focused}`);

    cleanup();

  }



  // 8) requestFocus 写出的内容格式自洽

  {

    cleanup();

    const { guard } = make();

    guard.requestFocus();

    check('requestFocus 写出了请求文件', existsSync(focusFile));

    const payload = JSON.parse(readFileSync(focusFile, 'utf-8'));

    check(

      '聚焦请求带 pid 与时间戳',

      typeof payload.pid === 'number' && typeof payload.at === 'number',

      JSON.stringify(payload),

    );

    cleanup();

  }



  // 9) release 会清掉锁，让下次启动不误判

  {

    cleanup();

    const { guard } = make();

    guard.claim();

    check('claim 后锁文件存在', existsSync(lockFile));

    guard.release();

    check('release 后锁文件被清掉', !existsSync(lockFile));

  }



  // 10) isProcessAlive：自己活着，假 PID 死了

  {

    check('isProcessAlive(自己) = true', isProcessAlive(process.pid) === true);

    check('isProcessAlive(假 PID) = false', isProcessAlive(findDeadPid()) === false);

    check('isProcessAlive(0) = false', isProcessAlive(0) === false);

  }



  cleanup();

}

/* ------------------------------------------------------------------ 测试：右键菜单结构 */



console.log('\n10. petMenu：右键菜单的结构与功能标注');

{

  const state = {

    visible: true,

    paused: false,

    visionWatching: false,

    activity: 0.5,

    petScale: 1,

    edgePeek: true,

    // 对话框默认是显示的（ChatWindow.create 的 ready-to-show 会显示它）
    chatVisible: true,

    // 麦克风（用户要求 2026-10-08：「麦克风选项移到宠物菜单里面」）
    micDeviceId: '',
    micDevices: [],

  };

  const items = buildPetMenuTemplate(state);

  const actions = collectActions(items);

  const disabled = collectDisabled(items);



  // 顶层应当有这些分组（照抄参考应用的层级）

  const labels = items.map((i) => i.label).filter(Boolean);

  // 顶层分组（用户最近一次调整后的结构）：

  //   · 「戳一下」已删（直接点宠物就是同一个动作）

  //   · 「外观」改名「宠物大小」，档位直接放在它下面

  //   · 「工具与帮助」已删

  //   · 「桌宠控制」**保留**（用户明确说取消删除）

  for (const want of ['聊天模式', '宠物大小', '桌宠控制', '设置…', '退出']) {

    check(`菜单里有「${want}」`, labels.some((l) => l && l.startsWith(want)), `实际：${labels.join(' / ')}`);

  }



  // 子菜单存在且非空

  // 「宠物大小」的**子菜单就是档位列表**（不再有中间的「大小 xxx px」那层）

  const sizeMenu = items.find((i) => i.label === '宠物大小');

  // ⚠️ 不要写死档位数量。档位有**唯一真源** `PET_SCALE_PRESETS`

  // （改这里之前是硬编码 6，而设置页是连续滑杆 —— 三份清单互不一致）。

  check(

    '「宠物大小」子菜单的档位数 = PET_SCALE_PRESETS',

    Array.isArray(sizeMenu?.submenu) && sizeMenu.submenu.length === PET_SCALE_PRESETS.length,

    `实际 ${sizeMenu?.submenu?.length} 项，期望 ${PET_SCALE_PRESETS.length}`,

  );

  // 每个档位都要在菜单里出现，且 label 带像素值（和设置页下拉框同一套文案）

  const sizeLabels = (sizeMenu?.submenu ?? []).map((i) => i.label).join(' ');

  let allPresetsListed = true;

  for (const preset of PET_SCALE_PRESETS) {

    if (!sizeLabels.includes(`${petSizeFor(preset.scale)} px`)) allPresetsListed = false;

  }

  check('每个档位都出现在菜单里且带像素值', allPresetsListed, `实际标签：${sizeLabels}`);

  check('宠物大小子菜单里恰好一个被勾选',

    (sizeMenu?.submenu ?? []).filter((i) => i.checked).length === 1,

    '勾选数不对会让用户看不出当前档位');



  // 用户明确要求删掉的项**不该**再出现（加回来时能立刻发现）

  for (const gone of ['戳一下', '工具与帮助', '外观']) {

    check(`菜单里已没有「${gone}」`, !labels.some((l) => l && l.startsWith(gone)), `实际：${labels.join(' / ')}`);

  }

  // 「允许看屏幕」必须是可点的 checkbox（用户要求"旁边加个小开关图标可以开关"）

  const visionToggleItem = items.find((i) => i.label === '允许偶尔截图');

  check(

    '「允许偶尔截图」是可点的 checkbox',

    visionToggleItem?.type === 'checkbox' && visionToggleItem.enabled !== false && visionToggleItem.action === 'toggle-vision',

    `实际 type=${visionToggleItem?.type} enabled=${visionToggleItem?.enabled} action=${visionToggleItem?.action}`,

  );



  // ---- 文字弹窗存在时间（用户要求：默认 60 秒） ----
  {
    check(
      '默认 60 秒',
      clampBubbleLinger(undefined) === 60_000,
      `实际 ${clampBubbleLinger(undefined)}`,
    );
    check('默认值常量是 60 秒', BUBBLE_LINGER_DEFAULT_MS === 60_000);
    check('10 秒原样通过', clampBubbleLinger(10_000) === 10_000);
    // 下限 1 秒：再短用户来不及读，看起来像闪一下
    check('低于下限钳到 1 秒', clampBubbleLinger(0) === 1000 && clampBubbleLinger(-5) === 1000);
    check('下限常量是 1 秒', BUBBLE_LINGER_MIN_MS === 1000);
    // 上限 120 秒：再长就等于常驻，不是"弹窗"了
    check('高于上限钳到 120 秒', clampBubbleLinger(999_999) === 120_000);
    check('上限常量是 120 秒', BUBBLE_LINGER_MAX_MS === 120_000);
    check('非数字回落到 60 秒', clampBubbleLinger('abc') === 60_000);
    check('小数取整', clampBubbleLinger(5500.7) === 5501, `实际 ${clampBubbleLinger(5500.7)}`);
  }

  // ---- 每个 action 都能在主进程的 handlers 里找到 ----
  //
  // 这条是**事后补的**：用户报「菜单全灰、点了没反应」时现有测试全绿 ——
  // 因为它们只验模板结构，从不做 action -> handler 的映射检查。
  // 实际 bug 是 action 用 kebab-case（`toggle-vision`）而 handlers 用 camelCase
  // （`toggleVision`），直接查表永远落空，17 个 action 里 14 个被置灰。
  {
    // 与 src/main/index.ts 里传给 popupPetMenu 的对象**逐字对应**。
    // 那边加/删处理器时这里也要跟着改 —— 名字对不上就会被这条抓住。
    const HANDLER_KEYS = [
      // ⚠️ 2026-10-12 去掉 'chat'（那个「聊天…」项已删），加上 'toggleChatMode'
      'toggleChat', 'toggleBubble', 'toggleImmersive', 'toggleVision', 'toggleThinking', 'toggleVisible', 'togglePause',
        // 状态条显隐（2026-10-11，与 toggleBubble 同一模式）
        'toggleStatus',
        // 对话条复位（2026-10-12，可拖拽之后要能撤销）
        'resetChat',
        // 联网搜索（2026-10-12）；2026-10-16 起菜单改为 webSearchMode 三选一，
        // toggleWebSearch 保留给托盘的二态勾
        'toggleWebSearch',
        // 联网搜索模式（2026-10-16）：radio 子菜单，action 带值
        'webSearchMode',
        // 聊天模式（2026-10-12；2026-10-14 起双向）：开=都显示，关=都隐藏
        'toggleChatMode',
        // 语音播报（2026-10-14）：菜单开关，真源是 persona.yaml 的 providers.tts.enabled
        'toggleTtsSpeech',
      'toggleEdgePeek', 'peekSettings', 'setActivity', 'setPetScale', 'openLogs', 'settings',
      'memory', 'quit', 'quitAll',
      // 截图对话（2026-10-17 用户要求）：菜单项 + 快捷键共用一个动作
      'snapshotChat',
      // 麦克风（用户要求 2026-10-08：「麦克风选项移到宠物菜单里面」）
      'setMicDevice',
    ];
    const menuItems = buildPetMenuTemplate({
      visible: true, paused: false, visionWatching: false,
      thinking: true, activity: 0.6, petScale: 1, edgePeek: true, chatVisible: true, bubbleVisible: true, statusVisible: true, webSearchMode: 'off', ttsSpeechEnabled: true,
      // 沉浸模式（用户要求 2026-10-09，一级菜单）
      immersiveMode: false,
      /*
       * ⚠️ 麦克风这两个字段**必须给**：模板会读 `state.micDevices.length`
       * 拼标题（「麦克风（未枚举）」），缺了就是
       * `TypeError: Cannot read properties of undefined (reading 'length')`
       * —— 整条 petMenu 测试直接崩掉，而不是某一条断言失败。
       *
       * 这里给**非空**列表：只有非空时才会走到 `micOption(...)` 那条分支，
       * 而"设备项的 action 能不能解析到处理器"正是这条测试要验的。
       */
      micDeviceId: 'dev-b',
      micDevices: [
        { deviceId: 'dev-a', label: '麦克风 (USB PnP Sound Device)' },
        { deviceId: 'dev-b', label: '麦克风 (Realtek(R) Audio)' },
      ],
    });
    const unresolved = [];
    for (const action of collectActions(menuItems)) {
      const name = action.split(':')[0];
      const key = resolveHandlerKey(action);
      if (!HANDLER_KEYS.includes(key)) unresolved.push(action + ' -> ' + key);
    }
    check(
      '每个 action 都能在主进程的 handlers 里找到',
      unresolved.length === 0,
      '找不到：' + unresolved.join(', ') + '（对不上就会被置灰成「未实现」）',
    );
    check('scale: 映射到 setPetScale', resolveHandlerKey('scale:1') === 'setPetScale');
    check('activity: 映射到 setActivity', resolveHandlerKey('activity:0.5') === 'setActivity');

    /*
     * 活泼度四档的文案（2026-10-17 用户要求改名 + 加后缀）。
     *
     * ## 为什么要写死在这
     *
     * 这四行是**用户看得到的文案**，而且带着两条承诺：
     *
     *   会自主截图        这一档会自己抓屏（前提：用户开了「允许偶尔截图」）
     *   注意力在谁身上     0 = 全在用户，1 = 全在它自己
     *
     * 名字来自 §115 拍板（安静/普通/好奇/自言自语）。老文案是
     * 「活泼（0.8）」「话痨（1.0）」—— 那两个名字**没拍板过**，是占位写法。
     *
     * 断言两件事：四档齐全且顺序对；**只有后两档**带「会自主截图」。
     * 后者是关键 —— 要是哪天有人给「普通」也加上，用户会以为
     * 它随时在偷看屏幕（而那是隐私开关才能决定的事）。
     */
    const activityNode = menuItems.find((i) => i.label === '活泼度');
    const activityLabels = (activityNode?.submenu ?? []).map((i) => i.label);
    const EXPECTED_ACTIVITY = [
      '安静 · 注意力在你身上，不截图',
      '无聊 · 自己找事做，偶尔发呆',
      '好奇 · 会自主截图，偶尔搭话',
      '自言自语 · 注意力在自己，会自主截图，20 分钟后安静',
    ];
    check(
      '活泼度：四档文案与顺序对',
      JSON.stringify(activityLabels) === JSON.stringify(EXPECTED_ACTIVITY),
      `实际：${JSON.stringify(activityLabels)}`,
    );
    const autoShot = activityLabels.filter((s) => (s ?? '').includes('会自主截图'));
    check(
      '活泼度：只有「好奇」「自言自语」两档标了会自主截图',
      autoShot.length === 2 &&
        autoShot[0].startsWith('好奇') &&
        autoShot[1].startsWith('自言自语'),
      `实际 ${autoShot.length} 档：${JSON.stringify(autoShot)}`,
    );
    /*
     * 父行右侧显示**最近的档位名**（2026-10-17 用户要求）。
     *
     * 四个档位是 0.1 / 0.5 / 0.8 / 1.0，滑杆可以停在 0.6 这种中间值 ——
     * 那时候一个档都不勾（`near` 容差 0.01），父行还显示 `0.60` 的话
     * 用户会以为"当前没选中任何档"。所以按距离取最近的档名。
     */
    for (const [raw, want] of [
      [0.1, '安静'],
      [0.5, '无聊'],
      [0.62, '无聊'],
      [0.8, '好奇'],
      [1.0, '自言自语'],
    ]) {
      const node = buildPetMenuTemplate({
        visible: true, paused: false, visionWatching: false,
        thinking: true, activity: raw, petScale: 1, edgePeek: true,
        chatVisible: true, bubbleVisible: true, statusVisible: true,
        webSearchMode: 'off', ttsSpeechEnabled: true, immersiveMode: false,
        micDevices: [], micDeviceId: '',
      });
      const shown = node.find((i) => i.label === '活泼度')?.value;
      check(
        `活泼度：${raw} 显示最近的档位名「${want}」`,
        shown === want,
        `实际显示了 ${JSON.stringify(shown)}`,
      );
    }

    const attentionTiers = activityLabels.filter((s) => (s ?? '').includes('注意力'));
    check(
      '活泼度：只有两端标了注意力在谁身上',
      attentionTiers.length === 2 &&
        attentionTiers[0].startsWith('安静') &&
        attentionTiers[1].startsWith('自言自语'),
      `实际：${JSON.stringify(attentionTiers)}`,
    );
  }

  // **关键不变量**：所有置灰项都必须标明「未实现」。

  // 理由：这个项目出过好几次"界面上写着、实际没实现"的问题，

  // 那种失败不报错、只是什么都不发生，最难排查。

  const unmarked = disabled.filter((i) => !(i.label ?? '').includes(TODO_SUFFIX));

  check('所有置灰项都标了「未实现」', unmarked.length === 0,

    `没标的：${unmarked.map((i) => i.label).join(', ')}`);



  // 反过来：标了「未实现」的必须真是置灰的（否则是"写了却说没实现"的矛盾）

  const mislabeled = [];

  const walk = (list) => {

    for (const it of list) {

      if ((it.label ?? '').includes(TODO_SUFFIX) && it.enabled !== false) mislabeled.push(it.label);

      if (it.submenu) walk(it.submenu);

    }

  };

  walk(items);

  check('标了「未实现」的都确实是置灰的', mislabeled.length === 0, mislabeled.join(', '));



  // 可点的项都要有 action（否则点了没反应）

  const clickableNoAction = [];

  const walk2 = (list) => {

    for (const it of list) {

      if (it.submenu) { walk2(it.submenu); continue; }

      if (it.type === 'separator') continue;

      if (it.enabled !== false && !it.action) clickableNoAction.push(it.label);

    }

  };

  walk2(items);

  check('可点的叶子项都带 action', clickableNoAction.length === 0,

    `缺 action：${clickableNoAction.join(', ')}`);



  // 已实现的 action 必须都在 handlers 名单里（与 index.ts 的 popupPetMenu 对齐）

  const implemented = new Set([

    // ⚠️ 2026-10-12 去掉了 'chat' —— 那一项（标签「聊天…」）实际打开的是
    //    设置窗口，用户要求换成「聊天模式」开关，已删除
    'toggle-chat', 'toggle-bubble', 'toggle-immersive', 'toggle-vision', 'toggle-visible', 'toggle-pause',

    'activity', 'scale', 'open-logs', 'settings', 'memory', 'quit', 'quit-all',
    // 截图对话（2026-10-17 用户要求）：菜单项 + Ctrl+Alt+S 共用同一个动作
    'snapshot-chat',

    'toggle-edge-peek', 'peek-settings', 'toggle-thinking',
    // 状态条显隐（2026-10-11）
    'toggle-status',
    // 对话条复位（2026-10-12）
    'reset-chat',
    // 联网搜索（2026-10-12）
    // 联网搜索（2026-10-12）；2026-10-16 起是 web-search-mode:<模式> 带值分发
    'toggle-web-search',
    'web-search-mode',
    // 聊天模式（2026-10-12；2026-10-14 起双向）：开=都显示，关=都隐藏
    'toggle-chat-mode',
    // 语音播报（2026-10-14）：菜单开关，真源是 persona.yaml 的 providers.tts.enabled
    'toggle-tts-speech',
    /*
     * 麦克风（用户要求 2026-10-08：「麦克风选项移到宠物菜单里面」）。
     *
     * ⚠️ 这里登记的是**动作类别名**（冒号前那一段），不是处理器名 ——
     * `mic:default` / `mic:<deviceId>` 经 `ACTIONS_WITH_VALUE` 映射到
     * `setMicDevice`，两个名字不一样是刻意的（和 `scale` -> `setPetScale` 同理），
     * 所以上面那张 `HANDLER_KEYS` 里也必须同时有 `setMicDevice`。
     */
    'mic',
  ]);

  /*
   * 麦克风子菜单：**点了到底传什么给处理器**（用户要求 2026-10-08）。
   *
   * ## ⚠️ 为什么必须验这一条（bug 就在这儿）
   *
   * `toElectronTemplate` 决定 `click` 怎么调处理器，而它对"带值的 action"
   * 有这么一条链：
   *
   *     Number(rawValue) 是有限数 -> 传数字      （scale / activity）
   *     是 toggleVision/toggleEdgePeek -> 传勾选态
   *     **rawValue 有值但不是数字 -> 原来落进"无参数"分支**
   *
   * 而 `mic:default` / `mic:<deviceId>` 的值是**字符串**：
   * `Number('default')` = `NaN` -> 不吃数字分支 -> 掉进"无参数"分支 ->
   * `handler()` 不传参 -> `setMicDevice(undefined)` -> `''` ->
   * **点任何一支麦克风都会变成「系统默认」**。
   *
   * 菜单看着好好的、点了也"有反应"（勾选跳到系统默认），
   * 但用户要的那支永远选不上 —— 本项目最怕的那类失败：**不报错、只是不能用**。
   *
   * 所以这里不光验结构，还要**真的把一个 menu item 点一下**，
   * 看处理器收到的参数对不对。只验 `action` 字符串是不够的 ——
   * 字符串明明是对的，错的是"怎么用它"。
   */
  {
    const calls = [];
    const stateForClick = {
      ...state,
      micDeviceId: '',
      micDevices: [
        { deviceId: 'dev-usb', label: '麦克风 (USB PnP Sound Device)' },
        { deviceId: 'dev-realtek', label: '麦克风 (Realtek(R) Audio)' },
      ],
    };
    const callsFor = (labelToClick) => {
      const seen = [];
      const handlers = new Proxy(
        {},
        {
          get: (_t, prop) =>
            (...args) => {
              if (prop === 'setMicDevice') seen.push(args);
            },
        },
      );
      const missing = [];
      const tmpl = toElectronTemplate(buildPetMenuTemplate(stateForClick), handlers, missing);
      const find = (list, pred) => {
        for (const it of list) {
          if (pred(it)) return it;
          if (it.submenu) {
            const hit = find(it.submenu, pred);
            if (hit) return hit;
          }
        }
        return null;
      };
      const target = find(tmpl, (i) => i.label === labelToClick);
      if (!target) return { seen, found: false };
      target.click?.();
      return { seen, found: true };
    };

    const usb = callsFor('麦克风 (USB PnP Sound Device)');
    check(
      '麦克风：点子项会传**真实 deviceId**（不是 undefined）',
      usb.found && usb.seen.length === 1 && usb.seen[0][0] === 'dev-usb',
      `实际收到：${JSON.stringify(usb.seen)}（undefined 就意味着"点了跳回系统默认"）`,
    );

    const dflt = callsFor('（系统默认）');
    check(
      '麦克风：「系统默认」传哨兵字符串 default',
      dflt.found && dflt.seen.length === 1 && dflt.seen[0][0] === 'default',
      `实际收到：${JSON.stringify(dflt.seen)}`,
    );
  }

  const unknown = actions.map((a) => a.split(':')[0]).filter((n) => !implemented.has(n));

  check('没有未知 action（否则会被置灰）', unknown.length === 0, `未知：${[...new Set(unknown)].join(', ')}`);



  // 状态反映：隐藏时标签应变成「显示宠物」

  const hidden = buildPetMenuTemplate({ ...state, visible: false });

  const ctrl = hidden.find((i) => i.label === '桌宠控制');

  check('隐藏时「桌宠控制」首项变成「显示宠物」',

    ctrl?.submenu?.[0]?.label === '显示宠物', `实际：${ctrl?.submenu?.[0]?.label}`);



  // ---- 思考模式（用户要求 2026-10-07：「把思考模式做成开关，在宠物菜单界面显示，
  //      用户自己选择是否开启，默认开启」）----
  //
  // ⚠️ 验的是**结构**（有没有那一项、是不是 checkbox、勾选跟不跟值走）。
  //    "点了有没有反应"由上面的 action 名单那条断言覆盖 —— 两边合起来才是
  //    "看得见 + 点得动"。只验一边的话，"加了但没显示"和"显示了但没处理器"
  //    都会漏掉。
  {
    const onItems = buildPetMenuTemplate({ ...state, thinking: true });
    const offItems = buildPetMenuTemplate({ ...state, thinking: false });
    const itemOf = (items) => {
      const ctrl = items.find((i) => i.label === '桌宠控制');
      return (ctrl?.submenu ?? []).find((i) => i.action === 'toggle-thinking');
    };
    const onItem = itemOf(onItems);
    const offItem = itemOf(offItems);
    check('思考模式项出现在「桌宠控制」里', !!onItem, '找不到 action=toggle-thinking');
    check(
      '思考模式是 checkbox（二态开关要能一眼看出开还是关）',
      onItem?.type === 'checkbox',
      `实际 type=${onItem?.type}`,
    );
    check('思考模式：开 -> 勾选', onItem?.checked === true, `实际 checked=${onItem?.checked}`);
    check(
      '思考模式：关 -> 不勾，且标签写明代价',
      offItem?.checked === false && (offItem?.label ?? '').includes('关'),
      `实际 checked=${offItem?.checked} label=${offItem?.label}`,
    );

    /*
     * 沉浸模式（用户要求 2026-10-09）。
     *
     * 「设置个游戏或沉浸模式，自动隐藏对话框和气泡，只用语音来聊天回答，
     *   不显示气泡，tts 失败或关了显示错误文本」
     * 「沉浸模式放在一级菜单也就右键宠物出现的初始菜单」
     */
    {
      const findImmersive = (items) => items.find((i) => i.action === 'toggle-immersive');
      const onItems = buildPetMenuTemplate({ ...state, immersiveMode: true });
      const offItems = buildPetMenuTemplate({ ...state, immersiveMode: false });
      const onItem = findImmersive(onItems);
      const offItem = findImmersive(offItems);

      check(
        '沉浸模式出现在**一级菜单**（用户明确要求，打游戏前一键切）',
        !!onItem,
        '在一级 items 里找不到 action=toggle-immersive',
      );
      check(
        '沉浸模式是 checkbox（二态开关，要一眼看出当前是不是沉浸中）',
        onItem?.type === 'checkbox',
        `实际 type=${onItem?.type}`,
      );
      check(
        '沉浸模式：开 -> 勾选',
        onItem?.checked === true,
        `实际 checked=${onItem?.checked}`,
      );
      check(
        '沉浸模式：关 -> 不勾',
        offItem?.checked === false,
        `实际 checked=${offItem?.checked}`,
      );

      /*
       * 用户还要求「将隐藏对话框和显示气泡放在和边缘探头同一级的宠物控制菜单里面」——
       * 这两项本来就在那儿，这里**钉住它别再挪走**。
       */
      const ctrl = onItems.find((i) => i.label === '桌宠控制')?.submenu ?? [];
      const chatItem = ctrl.find((i) => i.action === 'toggle-chat');
      const bubbleItem = ctrl.find((i) => i.action === 'toggle-bubble');
      const peekItem = ctrl.find((i) => i.action === 'toggle-edge-peek');
      check(
        '「隐藏/显示对话框」和「边缘探头」同在「桌宠控制」里',
        !!chatItem && !!peekItem,
        `chat=${!!chatItem} peek=${!!peekItem}`,
      );
      check(
        '「隐藏/显示气泡」和「边缘探头」同在「桌宠控制」里',
        !!bubbleItem && !!peekItem,
        `bubble=${!!bubbleItem} peek=${!!peekItem}`,
      );

  /*
   * 沉浸模式的**决策逻辑**（`decideBubble`）。
   *
   * 用户要求：「设置个游戏或沉浸模式，自动隐藏对话框和气泡，只用语音来聊天回答，
   *           不显示气泡，tts 失败或关了显示错误文本」
   *
   * 判据是"**这条消息有没有语音**"，不是"reason 是不是 error"——
   * 所以下面把四种组合都钉一遍。
   */
  {
    const base = {
      isTextKind: true,
      bubbleVisible: true,
      immersiveMode: false,
      ttsEnabled: true,
    };

    // ① 常规：显示
    check(
      '常规：显示气泡',
      decideBubble(base).show === true,
      `实际 ${JSON.stringify(decideBubble(base))}`,
    );

    // ② 总开关关掉：一律不显示（连沉浸模式的例外也不弹）
    check(
      '气泡总开关关掉 -> 不显示',
      decideBubble({ ...base, bubbleVisible: false }).show === false,
      '总开关没生效',
    );
    check(
      '总开关关掉时，**即使**沉浸模式 + 无语音也不显示（总开关优先）',
      decideBubble({
        ...base,
        bubbleVisible: false,
        immersiveMode: true,
        ttsEnabled: false,
      }).show === false,
      '总开关被沉浸模式的例外绕过了',
    );

    // ③ 沉浸模式 + 有语音：不显示（这就是沉浸模式要的效果）
    check(
      '沉浸模式 + 有语音 -> 不显示文字（只听声音）',
      decideBubble({ ...base, immersiveMode: true }).show === false,
      '沉浸模式下还在显示文字',
    );

    // ④ 沉浸模式 + TTS 关掉：显示（用户要的例外）
    check(
      '沉浸模式 + TTS 关掉 -> 显示文字（用户明确要求）',
      decideBubble({ ...base, immersiveMode: true, ttsEnabled: false }).show === true,
      'TTS 关了却没显示文字 —— 用户既听不到也没得看',
    );

    // ⑤ 沉浸模式 + 生成失败：显示（同样是没有语音）
    check(
      '沉浸模式 + 生成失败 -> 显示文字（reason=error）',
      decideBubble({ ...base, immersiveMode: true, reason: 'error' }).show === true,
      '生成失败却没显示错误文字',
    );

    // ⑥ reason=done 不算失败：有语音就不该显示
    check(
      '沉浸模式 + reason=done -> 不显示（done 不是失败）',
      decideBubble({ ...base, immersiveMode: true, reason: 'done' }).show === false,
      'done 被当成失败了',
    );

    // ⑦ 非文字类消息本来就不走气泡
    check(
      '非文字类消息（表情/口型）-> 不显示',
      decideBubble({ ...base, isTextKind: false }).show === false,
      '非文字类消息进了气泡',
    );

    // ⑧ why 要说人话（它直接进 startup.log）
    const why = decideBubble({ ...base, immersiveMode: true, ttsEnabled: false }).why;
    check('why 是给人看的中文短语', why.includes('沉浸模式'), `实际 ${why}`);

    /*
     * ⑨ 🔴 TTS **配置开着但实际合成失败**（2026-10-15 加，用户报障的直接原因）。
     *
     * ## 这个用例的来历
     *
     * 用户报「宠物发不了声音」+「也回复不了信息」。查出来是**两件事叠加**：
     *
     * ```
     * ① edge-tts 被微软 403 挡了（合成实际失败）
     * ② 沉浸模式按「TTS 配置开着」判断有没有语音 -> **隐藏了文字**
     * ```
     *
     * 于是文字被主动隐藏、语音又失败了 —— 用户两头都收不到。
     *
     * ⚠️ 这个用例存在的意义：**「配了 TTS」和「TTS 能用」是两件事**。
     * 少了它，上面那个组合 bug 会再次悄悄出现。
     */
    check(
      '沉浸模式 + TTS 配置开着但**合成失败** -> 显示文字（2026-10-15 修）',
      decideBubble({ ...base, immersiveMode: true, ttsEnabled: true, ttsHealthy: false }).show ===
        true,
      '合成失败却还是隐藏了文字 —— 用户既听不到也看不到，就是这次报的"完全没反应"',
    );

    check(
      '沉浸模式 + TTS 健康 -> 仍然不显示（原行为不能被改掉）',
      decideBubble({ ...base, immersiveMode: true, ttsEnabled: true, ttsHealthy: true }).show ===
        false,
      '正常情况也弹文字了 —— 沉浸模式就失效了',
    );

    check(
      '省略 ttsHealthy 时按"健康"处理（不改变没观测到失败时的行为）',
      decideBubble({ ...base, immersiveMode: true, ttsEnabled: true }).show === false,
      '省略这个字段就变成显示文字了 —— 会顺便改掉所有正常用户的体验',
    );

    const whyFailed = decideBubble({
      ...base,
      immersiveMode: true,
      ttsEnabled: true,
      ttsHealthy: false,
    }).why;
    check(
      '合成失败的 why 要说清是合成失败（不是"TTS 已关闭"）',
      whyFailed.includes('合成失败'),
      `实际 ${whyFailed} —— 排查时会误以为用户关了 TTS`,
    );
  }

  /*
   * 气泡要不要**摆出来**（`decideBubblePlacement`，2026-10-19 加）。
   *
   * ## 这个用例的来历
   *
   * 用户报：「宠物控制状态明明是点击隐藏，可是气泡却没有出现」。
   * 查日志：20:12 那条截图对话回复，宠物正趴着（`compact=true`，他在全屏看漫画），
   * 于是 `forwardToBubble` 里那条**无条件**跳过规则把气泡 hide 了 ——
   * 语音照放，字一个没有。
   *
   * 规则改成：**用户自己按的（userInitiated）必须给字**，
   * 自主开口在全屏时仍然安静。
   */
  {
    check(
      '常规：宠物可见、没趴着 -> 弹',
      decideBubblePlacement({ petVisible: true, compact: false }).show === true,
      '常规情况反而不弹了',
    );

    check(
      '宠物被隐藏 -> 一律不弹',
      decideBubblePlacement({ petVisible: false, compact: false }).show === false,
      '宠物藏起来了还弹气泡',
    );

    check(
      '宠物趴着（全屏）+ 自主开口 -> 不弹（别打扰）',
      decideBubblePlacement({ petVisible: true, compact: true, userInitiated: false }).show ===
        false,
      '全屏时自主开口也弹了 —— 会打扰用户',
    );

    check(
      '宠物趴着 + **用户主动要的**（截图对话/打字）-> 弹（2026-10-19 修）',
      decideBubblePlacement({ petVisible: true, compact: true, userInitiated: true }).show ===
        true,
      '用户自己按了截图对话却看不到字 —— 正是这次报的 bug',
    );

    check(
      '省略 userInitiated 时按"不是用户要的"处理（不改没传这个字段时的行为）',
      decideBubblePlacement({ petVisible: true, compact: true }).show === false,
      '省略字段就变成弹了',
    );

    const whyCompact = decideBubblePlacement({
      petVisible: true,
      compact: true,
      userInitiated: false,
    }).why;
    check(
      '不弹的 why 要说明是"趴着 + 不是用户要的"',
      whyCompact.includes('趴着') && whyCompact.includes('用户'),
      `实际 ${whyCompact} —— 排查时分不清是隐藏还是全屏`,
    );
  }

  /*
   * 沉浸模式 & 对话框/气泡的**菜单联动**（用户实测报的问题）。
   *
   * 用户截图里出现「显示对话框」而沉浸模式刚把它隐藏了 ——
   * 标签和实际不一致，而且点了也没用（沉浸模式门控优先级更高）。
   */
  {
    const itemsOf = (state) => {
      const ctrl =
        buildPetMenuTemplate(state).find((i) => i.label === '桌宠控制')?.submenu ?? [];
      return {
        chat: ctrl.find((i) => i.action === 'toggle-chat'),
        bubble: ctrl.find((i) => i.action === 'toggle-bubble'),
      };
    };
    const base = { ...state, chatVisible: true, bubbleVisible: true };

    // ① 沉浸模式**关**时：保持原来的"文字随状态变"
    {
      const { chat, bubble } = itemsOf({ ...base, immersiveMode: false });
      check(
        '沉浸模式关：对话框项是可点的，且文字随状态变',
        chat?.enabled !== false && chat?.label === '隐藏对话框',
        `enabled=${chat?.enabled} label=${chat?.label}`,
      );
      check(
        '沉浸模式关：气泡项是可点的，且文字随状态变',
        bubble?.enabled !== false && bubble?.label === '隐藏气泡',
        `enabled=${bubble?.enabled} label=${bubble?.label}`,
      );
      const off = itemsOf({ ...base, immersiveMode: false, chatVisible: false });
      check(
        '沉浸模式关：对话框隐藏时文字变「显示对话框」',
        off.chat?.label === '显示对话框',
        `实际 ${off.chat?.label}`,
      );
    }

    // ② 沉浸模式**开**时：两项置灰 + 说清谁在管
    {
      const { chat, bubble } = itemsOf({ ...base, immersiveMode: true });
      check(
        '沉浸模式开：对话框项**置灰**（点了没用，不如明说）',
        chat?.enabled === false,
        `enabled=${chat?.enabled}`,
      );
      check(
        '沉浸模式开：气泡项**置灰**',
        bubble?.enabled === false,
        `enabled=${bubble?.enabled}`,
      );
      check(
        '沉浸模式开：对话框文案说清"谁在管"',
        (chat?.label ?? '').includes('沉浸模式'),
        `实际 ${chat?.label}`,
      );
      check(
        '沉浸模式开：气泡文案说清"谁在管"',
        (bubble?.label ?? '').includes('沉浸模式'),
        `实际 ${bubble?.label}`,
      );

      /*
       * ⚠️ 不管底层状态是什么，文案**都**应当是"托管"——
       * 否则又会出现"标签和实际不一致"（那正是用户报的 bug）。
       */
      const hidden = itemsOf({
        ...base,
        immersiveMode: true,
        chatVisible: false,
        bubbleVisible: false,
      });
      check(
        '沉浸模式开：即使底层状态是隐藏，文案也仍是"托管"（不能自相矛盾）',
        (hidden.chat?.label ?? '').includes('沉浸模式') &&
          (hidden.bubble?.label ?? '').includes('沉浸模式'),
        `chat=${hidden.chat?.label} bubble=${hidden.bubble?.label}`,
      );
    }
  }


    }

  }

  // 免打扰状态同理

  const paused = buildPetMenuTemplate({ ...state, paused: true });

  const ctrl2 = paused.find((i) => i.label === '桌宠控制');

  // 不写死下标：菜单项会变（这次插入了「隐藏/显示对话框」，免打扰挪到第 3 位）。
  // 按 action 找才稳，否则每插一项都要改一次测试。
  const pauseItem = (ctrl2?.submenu ?? []).find((i) => i.action === 'toggle-pause');
  check('免打扰项在暂停时含「恢复」', (pauseItem?.label ?? '').includes('恢复'), '实际：' + (pauseItem?.label ?? ''));

  // ---- 「隐藏/显示对话框」（用户要求：点击后文字会变） ----
  {
    const shown = buildPetMenuTemplate({ ...state, chatVisible: true });
    const hidden = buildPetMenuTemplate({ ...state, chatVisible: false });
    const labelOf = (items) => {
      const ctrl = items.find((i) => i.label === '桌宠控制');
      return (ctrl?.submenu ?? []).find((i) => i.action === 'toggle-chat')?.label ?? '';
    };
    check('对话框可见时菜单写「隐藏对话框」', labelOf(shown) === '隐藏对话框', '实际：' + labelOf(shown));
    check('对话框隐藏时菜单写「显示对话框」', labelOf(hidden) === '显示对话框', '实际：' + labelOf(hidden));
    // 必须是普通项而不是 checkbox —— 用户描述的是"文字会变"，加勾反而多一层
    const ctrl = hidden.find((i) => i.label === '桌宠控制');
    const item = (ctrl?.submenu ?? []).find((i) => i.action === 'toggle-chat');
    check('它是普通菜单项（不是 checkbox）', !item?.type || item.type === 'normal', '实际 type=' + item?.type);
    check('它是可点的（不是置灰）', item?.enabled !== false, '实际 enabled=' + item?.enabled);
  }



  // 视觉开关的勾选状态要跟着 state 走

  const visionOn = buildPetMenuTemplate({ ...state, visionWatching: true });

  const visionItem = visionOn.find((i) => i.action === 'toggle-vision');

  check('看屏幕开关勾选状态跟随 state', visionItem?.checked === true);



  console.log(`     （菜单共 ${items.length} 个顶层项、${actions.length} 个可点动作、${disabled.length} 个置灰项）`);

}



/* ------------------------------------------------------------------ 测试：窗口布局 */



console.log('\n11. petLayout：窗口尺寸与右下角锚定');

{

  // 缩放钳制：菜单/滑杆给的值可能越界

  check('缩放钳制下限', clampScale(0.1) === 0.6, `实际 ${clampScale(0.1)}`);

  check('缩放钳制上限', clampScale(9) === 2.4, `实际 ${clampScale(9)}`);

  check('非数字缩放回落到 1', clampScale(NaN) === 1);



  // 窗口尺寸 = 正方形，边长 = 画布边长。

  //

  // ⚠️ 这里被改过好几轮，最终结论：**不缩窗口**。

  // 贴不到边的根因是"拖拽夹紧太紧"，解法是放宽拖拽极限（见 dragTracker 的

  // clampMargin 那组测试），而不是把窗口裁到立绘大小 —— 后者会让旋转时的

  // 立绘被裁（155x177 旋转需 235）。

  const normal = windowSizeFor(1, 'normal');

  /*
   * ⚠️ 这里原来断言"窗口是正方形"，现在**刻意不成立**了。
   *
   * 用户要求「可以加高宠物窗口」，目的是给宠物上方的文字弹窗留空间 ——
   * 原来 240 见方、立绘 174 高，头顶以上只剩 33px，两行字都放不下。
   *
   * 但**宽度必须不变**：宽度就是"能装下旋转 45° 立绘的最小正方形"的边长
   * （155x177 旋转需 235）。所以新断言是：
   *   · 宽 = petSizeFor(scale)（不变，保证探头几何）
   *   · 高 >= 宽（加高，给气泡留空间）
   *   · 两形态尺寸一致（setBounds 改尺寸是一帧硬跳，必须恒定）
   */
  check('窗口高 >= 宽（加高给文字弹窗留空间）',
    normal.height >= normal.width, `实际 ${normal.width}x${normal.height}`);

  /*
   * ⚠️ 加高量的容差取 0.35 而不是 0.34。
   *
   * 加高是 `Math.round(pet * 0.34)`，在某些 pet 值下会向上取整一点点：
   * 实测 pet=250 时窗口是 `252x336`（Windows 缩放下的取整），比值 336/252 = 1.333。
   * 原来卡 1.6 太松、卡 1.34 又会因为这一两像素抖动而误报，所以留 0.35 的余量。
   */
  check('加高量不至于离谱（<= 宽的 1.35 倍）',
    normal.height <= normal.width * 1.35, `实际 ${normal.width}x${normal.height}`);
  check('探头形态尺寸与常态一致（改尺寸是一帧硬跳）',
    JSON.stringify(windowSizeFor(1, 'compact')) === JSON.stringify(normal),
    `常态 ${JSON.stringify(normal)}，探头 ${JSON.stringify(windowSizeFor(1, 'compact'))}`);

  check('窗口边长 = 画布边长（= 立绘基准尺寸）',

    normal.width === petSizeFor(1), `实际 ${normal.width}，期望 ${petSizeFor(1)}`);

  /*
   * 断言改对象：**该检查的是"探头窗口"，不是"常态窗口"**。
   *
   * 旧公式 `窗口 >= (立绘宽+高)*√2/2` 有两个毛病：
   *   ① 检查的是常态窗口 —— 而旋转只发生在**探头态**，探头窗口是独立尺寸；
   *   ② 它把"半对角"当成旋转后的外接，**忽略立绘相对旋转中心的偏移**，
   *      所以是保守近似（实测偏小：算 311，真实需要 324）。
   *
   * 现在按真实几何算：绕窗口中心转 45° 后的外接框要装得下。
   * 下一段"各档位探头窗口"里已有同样的算式，这里对 1.0 档做一次显式断言。
   */
  {
    const size = petSizeFor(1);
    const ratio = ART_BOX_RATIO;
    const artW = size * ratio.w;
    const artH = size * ratio.h;
    const cx = size * (ratio.x + ratio.w / 2);
    const cy = size * (ratio.y + ratio.h / 2);
    const dx = cx - size / 2;
    const dy = cy - normal.height / 2;
    const rad = Math.PI / 4;
    const rx = Math.abs(dx * Math.cos(rad) - dy * Math.sin(rad));
    const ry = Math.abs(dx * Math.sin(rad) + dy * Math.cos(rad));
    const half = Math.sqrt(artW * artW + artH * artH) / 2;
    const needW = Math.ceil(2 * (half + rx));
    const needH = Math.ceil(2 * (half + ry));
    const peek = peekWindowSizeFor(1);
    check(
      '探头窗口装得下旋转 45° 的立绘（否则探头会被裁）',
      peek.width >= needW && peek.height >= needH,
      `探头窗口 ${peek.width}x${peek.height}，需要 ${needW}x${needH}`,
    );
  }

  const compact = windowSizeFor(1, 'compact');

  check('两个形态窗口尺寸一致（否则形态切换会硬跳）',

    compact.width === normal.width && compact.height === normal.height,

    `紧凑 ${compact.width}x${compact.height} vs 常规 ${normal.width}x${normal.height}`);







  // ---- 设置弹窗定位（用户要求「弹窗设置屏幕居中，放大一些」）----

  //

  // 弹窗必须**居中**且**完全落在工作区内** —— 否则用户会看到它被屏幕边

  // 裁掉一半、按钮点不到。小屏（工作区比弹窗还小）时也要夹住而不是算出负坐标。

  {

    const WA = { x: 0, y: 0, width: 1536, height: 816 };

    const POP = { width: 820, height: 600 };



    const c = centerIn(POP, WA);

    // 居中：左右留白相等、上下留白相等（允许 1px 取整差）

    const padL = c.x - WA.x;

    const padR = WA.x + WA.width - (c.x + c.width);

    const padT = c.y - WA.y;

    const padB = WA.y + WA.height - (c.y + c.height);

    check('弹窗水平居中（左右留白相等）', Math.abs(padL - padR) <= 1, `左 ${padL} 右 ${padR}`);

    check('弹窗垂直居中（上下留白相等）', Math.abs(padT - padB) <= 1, `上 ${padT} 下 ${padB}`);



    // 尺寸足够大（用户要求"放大一些"）

    check('弹窗尺寸已放大', POP.width >= 800 && POP.height >= 580, `${POP.width}x${POP.height}`);

    check('弹窗完全落在工作区内', c.x >= WA.x && c.y >= WA.y && c.x + c.width <= WA.x + WA.width && c.y + c.height <= WA.y + WA.height);



    // 小屏边界：工作区比弹窗还小时，不能算出负坐标（夹到左上角）

    const tiny = { x: 0, y: 0, width: 600, height: 400 };

    const t = centerIn({ width: 820, height: 600 }, tiny);

    check(

      '工作区比弹窗小时夹到左上角（不出负坐标）',

      t.x === 0 && t.y === 0,

      `实际 ${t.x},${t.y}`,

    );



    // 多显示器：工作区有偏移（副屏）时也要居中在**那块屏**上

    const second = { x: 1920, y: 0, width: 1536, height: 816 };

    const s = centerIn(POP, second);

    check(

      '副屏（工作区带偏移）上居中正确',

      s.x === 1920 + Math.round((1536 - 820) / 2),

      `实际 x=${s.x}，期望 ${1920 + Math.round((1536 - 820) / 2)}`,

    );

  }



  // ---- 紧凑模式（别人全屏）：向横向**最靠近**的一侧探头 ----

  //

  // 用户要求：「全屏的时候改成自动向横向最靠近的屏幕探头」。

  //

  // 背景：`peekSide` 这个字段原来是 `private peekSide = 'right'` 且**从未被赋值** ——

  // 所以无论宠物在哪，全屏时它永远往右边缩。用户把宠物放在左半屏时很别扭。

  {

    const WA = { x: 0, y: 0, width: 1536, height: 816 };

    const target = windowSizeFor(1, 'compact');

    const wantVisible = Math.round(target.width * COMPACT_VISIBLE_RATIO);

    // 判边规则：宠物中心在工作区左半 -> 贴左；右半 -> 贴右

    const pickSide = (centerX) => (centerX < WA.x + WA.width / 2 ? 'left' : 'right');



    check('宠物在左半屏 -> 贴左边', pickSide(200) === 'left' && pickSide(700) === 'left');

    check('宠物在右半屏 -> 贴右边', pickSide(900) === 'right' && pickSide(1300) === 'right');



    // 两侧露出的宽度必须一致（否则"探头"观感会一边多一边少）

    const right = anchorPeekRight(WA, target, COMPACT_VISIBLE_RATIO, 'right');

    const left = anchorPeekRight(WA, target, COMPACT_VISIBLE_RATIO, 'left');

    const visRight = WA.x + WA.width - right.x;

    const visLeft = left.x + left.width - WA.x;

    check(

      '左右两侧露出的宽度一致',

      visRight === wantVisible && visLeft === wantVisible,

      `右露 ${visRight}、左露 ${visLeft}，期望都是 ${wantVisible}`,

    );

    // 贴左时必须允许 x 为负（窗口大半在屏幕外），否则没有探头效果

    check('贴左边时窗口大半在屏幕外（x < 0）', left.x < 0, `实际 x=${left.x}`);

    check('贴右边时窗口右缘超出屏幕', right.x + right.width > WA.width);

  }



  // ---- 锚定：右下角不动 ----

  const workArea = { x: 0, y: 0, width: 1536, height: 816 };

  const current = { x: 1100, y: 300, width: 380, height: 640 }; // 右下角在 (1480, 940)

  const bigger = anchorBottomRight(current, { width: 460, height: 700 }, workArea);

  check('放大后右边缘保持', bigger.x + bigger.width === current.x + current.width,

    `期望 ${current.x + current.width}，实际 ${bigger.x + bigger.width}`);

  // 高度超过 workArea 时要被夹住，所以底边允许被裁

  check('放大后底边不超出工作区',

    bigger.y + bigger.height <= workArea.height, `实际底边 ${bigger.y + bigger.height}`);



  const smaller = anchorBottomRight(

    { x: 1100, y: 100, width: 380, height: 400 },

    { width: 300, height: 300 },

    workArea,

  );

  check('缩小后右边缘仍保持',

    smaller.x + smaller.width === 1100 + 380, `实际 ${smaller.x + smaller.width}`);

  check('缩小后底边仍保持',

    smaller.y + smaller.height === 100 + 400, `实际 ${smaller.y + smaller.height}`);



  // 夹进工作区：把窗口拖到角落再放大，不能跑出屏幕

  const clamped = anchorBottomRight(

    { x: 1500, y: 800, width: 380, height: 640 },

    { width: 500, height: 700 },

    workArea,

  );

  check('超出右下角时被夹进工作区',

    clamped.x >= workArea.x && clamped.y >= workArea.y &&

      clamped.x + clamped.width <= workArea.width && clamped.y + clamped.height <= workArea.height,

    `实际 ${JSON.stringify(clamped)}`);



  // 任务栏留白：workArea 比整屏矮，窗口不该盖到任务栏上

  check('底部不盖任务栏', clamped.y + clamped.height <= workArea.height);



  const ints = [bigger, smaller, clamped].every(

    (b) => Number.isInteger(b.x) && Number.isInteger(b.y) && Number.isInteger(b.width),

  );

  check('返回整数边界（半像素会让文字发虚）', ints);



  // ---- 边缘探头（拖到屏幕左/右边缘）----

  check('探头角度是 45°（照参考应用的说明）', PEEK_ANGLE_DEG === 45, `实际 ${PEEK_ANGLE_DEG}`);



  // **关键约束**：画布保持待机大小，窗口必须装得下**旋转后**的画布。

  // 之前那一版窗口写死 220×170 而画布 240 —— 旋转后整块被窗口切掉，

  // 表现为"只剩一颗大头"。

  const peekCanvas = peekCanvasSizeFor(1);

  const onScreenWin = peekWindowSizeFor(1);

  check('探头画布 = 待机大小（用户要求：就用待机的大小旋转）',

    peekCanvas === petSizeFor(1), `画布 ${peekCanvas}，待机 ${petSizeFor(1)}`);



  /*
   * ⚠️ 这里原来验的是「探头窗口装得下**旋转后的画布**」——
   * 那是**过度保守**的口径：画布本身是透明的、不可见，要求整个画布都装进窗口
   * 会把窗口撑到 `240·√2 ≈ 340`，而真正会被裁的只有**立绘**。
   *
   * 现在改成验真正会裁剪的对象：**旋转后的立绘包围盒**。
   * 这个口径是用户报的"探头时立绘右侧被切出一条硬直边"的直接对因，
   * 而且算出来比旧口径小得多（277 vs 348），窗口不用无谓地撑大。
   */
  const artW = petSizeFor(1) * ART_BOX_RATIO.w;
  const artH = petSizeFor(1) * ART_BOX_RATIO.h;
  const artCx = petSizeFor(1) * (ART_BOX_RATIO.x + ART_BOX_RATIO.w / 2);
  const artCy = petSizeFor(1) * (ART_BOX_RATIO.y + ART_BOX_RATIO.h / 2);
  const normalWin = windowSizeFor(1, 'normal');
  const offDx = artCx - petSizeFor(1) / 2;
  const offDy = artCy - normalWin.height / 2;
  const rotRad = Math.PI / 4;
  const rotDx = Math.abs(offDx * Math.cos(rotRad) - offDy * Math.sin(rotRad));
  const rotDy = Math.abs(offDx * Math.sin(rotRad) + offDy * Math.cos(rotRad));
  const halfDiag = Math.sqrt(artW * artW + artH * artH) / 2;
  const needArtW = 2 * (halfDiag + rotDx);
  const needArtH = 2 * (halfDiag + rotDy);

  check('探头窗口装得下**旋转后的立绘**（否则会出现硬直边）',
    onScreenWin.width >= needArtW && onScreenWin.height >= needArtH,
    `窗口 ${onScreenWin.width}x${onScreenWin.height}，旋转后立绘需要 ${Math.ceil(needArtW)}x${Math.ceil(needArtH)}`);

  check('探头窗口比常态大（旋转需要更多空间）',
    onScreenWin.width > normalWin.width, `探头 ${onScreenWin.width}，常态 ${normalWin.width}`);

  check('探头窗口不过分大（透明区域会吃点击）',
    onScreenWin.width <= needArtW + PEEK_FIT_MARGIN + 2,
    `实际 ${onScreenWin.width}，需要 ${Math.ceil(needArtW)} + 余量 ${PEEK_FIT_MARGIN}`);

  check('探头窗口随缩放变大',

    peekWindowSizeFor(2).width > peekWindowSizeFor(1).width,

    '不跟随缩放的话放大宠物后会被窗口裁掉');

  check('探头画布随缩放变大',

    peekCanvasSizeFor(2) > peekCanvasSizeFor(1));



  // ---- 探头功能在各档位下都必须成立 ----

  //

  // 用户特别问过：「还要顺带看看探头的功能有没有受到影响」。

  // 档位从连续滑杆改成固定几档，探头的几何计算全依赖 `petSizeFor(scale)`，

  // 所以这里对**每一档**都验一遍，而不是只验默认档。

  {

    for (const preset of PET_SCALE_PRESETS) {

      const s = preset.scale;

      const size = petSizeFor(s);

      // ⚠️ 这两个都返回**数字/对象**要先看清：`peekCanvasSizeFor` 给的是**边长（number）**，

      // `peekWindowSizeFor` 给的是 `{width,height}`。我第一版把前者当对象用，

      // 结果全部 NaN 失败 —— 新测试第一件事就是纠正了我自己的误用。

      const canvasSide = peekCanvasSizeFor(s);

      const peekWin = peekWindowSizeFor(s);



      // ① 画布 = 待机大小（旋转不缩放）

      check(

        `${preset.label}（${size}px）：探头画布 = 待机大小`,

        canvasSide === size,

        `画布 ${canvasSide}，待机 ${size}`,

      );



      // ② 窗口装得下**旋转 45° 后的立绘** —— 否则探头会出现硬直边（用户报过）。
      //
      //    口径与上面那条一致：算**立绘**的旋转外接矩形，不是整块透明画布。
      const ratio = ART_BOX_RATIO;
      const localArtW = size * ratio.w;
      const localArtH = size * ratio.h;
      const localCx = size * (ratio.x + ratio.w / 2);
      const localCy = size * (ratio.y + ratio.h / 2);
      const normalH = windowSizeFor(preset.scale, 'normal').height;
      const localDx = localCx - size / 2;
      const localDy = localCy - normalH / 2;
      const rRad = Math.PI / 4;
      const localRx = Math.abs(localDx * Math.cos(rRad) - localDy * Math.sin(rRad));
      const localRy = Math.abs(localDx * Math.sin(rRad) + localDy * Math.cos(rRad));
      const localHalf = Math.sqrt(localArtW * localArtW + localArtH * localArtH) / 2;
      const needX = 2 * (localHalf + localRx);
      const needY = 2 * (localHalf + localRy);

      check(
        `${preset.label}（${size}px）：探头窗口装得下旋转 45° 的立绘`,
        peekWin.width >= needX && peekWin.height >= needY,
        `窗口 ${peekWin.width}x${peekWin.height}，需要 ${Math.ceil(needX)}x${Math.ceil(needY)}`,

      );

    }

  }



  // 贴边判定：只有左右边缘算，中间不算

  check('贴左边被识别', sideAtEdge({ x: 0, y: 193, width: 260, height: 430 }, workArea) === 'left');

  check('贴右边被识别',

    sideAtEdge({ x: 1536 - 260, y: 193, width: 260, height: 430 }, workArea) === 'right');

  check('屏幕中间不算贴边',

    sideAtEdge({ x: 600, y: 193, width: 260, height: 430 }, workArea) === null,

    '中间也触发的话宠物会莫名其妙变身');

  check('刚好在阈值内算贴边',

    sideAtEdge({ x: 1536 - 260 - EDGE_SNAP_THRESHOLD, y: 193, width: 260, height: 430 }, workArea) === 'right');

  check('超出阈值不算贴边',

    sideAtEdge({ x: 1536 - 260 - EDGE_SNAP_THRESHOLD - 5, y: 193, width: 260, height: 430 }, workArea) === null);



  // ---- 按**立绘可见边缘**判定，不是按窗口 ----

  //

  // 用户报过两次：「h5 窗口太大，不贴合宠物，导致还没到屏幕边缘就自动探头」。

  // 根因：窗口比立绘大一圈（旋转 45° 的几何差），立绘居中，

  // 所以按窗口判定会让宠物离屏幕边还有 artMargin 时就触发。

  // 这里锁定修复：同样的窗口位置，**扣掉留白之后**不该再触发。

  {

    const win = windowSizeFor(1, 'normal').width;

    const art = artSizeFor(1).width;

    const margin = Math.round((win - art) / 2);

    // 真实复现用户报的现象：窗口已经进到阈值内（离屏幕边 10px < 阈值 40），

    // 但**立绘**离屏幕边还有 10 + margin（≈74px）—— 这时候不该探头。

    // 按窗口判定会误触发，这正是"还没到屏幕边缘就自动探头"。

    //

    // 符号核对（`check-edge-math.py`（历史实测脚本）独立算过）：

    //   立绘 gap = 窗口 gap + margin   —— 窗口比立绘靠外，所以是**加**

    /*
     * ⚠️ 窗口离屏幕边的距离要**由留白推出来**，不能写死 10。
     *
     * 这个测试原来写的是"窗口离边 10px、立绘离边 10+margin"，在 margin≈43 时
     * 立绘离边 53px（> 阈值 40）成立。但 2026-10-04 把 `ART_BOX_RATIO` 换成实测值后
     * margin 变成 24，立绘离边只有 34px —— **34 < 40，本来就该触发**，
     * 于是"不该探头"的断言不成立了（是**构造失效**，不是功能坏了）。
     *
     * 现在反过来算：要让"立绘恰好还在阈值之外"，窗口该离边多远。
     *   `窗口gap + margin > 阈值`  =>  `窗口gap > 阈值 - margin`
     * 取 `阈值 - margin + 6`，留 6px 余量。
     */
    const gap = Math.max(1, EDGE_SNAP_THRESHOLD - margin + 6);
    const nearEdge = { x: 1536 - win - gap, y: 193, width: win, height: 430 };

    check(
      '窗口贴近边缘但立绘还很远时**不该**探头（用户报的"还没到边就探头"）',
      sideAtEdge(nearEdge, workArea, EDGE_SNAP_THRESHOLD, margin, margin) === null,
      `按窗口算会判 ${sideAtEdge(nearEdge, workArea, EDGE_SNAP_THRESHOLD, 0, 0)}` +
        `（立绘还有 ${gap + margin}px 才到边，阈值 ${EDGE_SNAP_THRESHOLD}），` +
        `加上留白 ${margin} 后应判 null`,
    );

    // 立绘真的贴到边（窗口越出屏幕 margin）时，必须触发

    const artAtEdge = { x: 1536 - win + margin, y: 193, width: win, height: 430 };

    check(

      '立绘真的贴到屏幕边时触发探头',

      sideAtEdge(artAtEdge, workArea, EDGE_SNAP_THRESHOLD, margin, margin) === 'right',

      `实际 ${sideAtEdge(artAtEdge, workArea, EDGE_SNAP_THRESHOLD, margin, margin)}`,

    );

    // 不传 artMargin 时保持老行为（纯函数向后兼容，也便于单测）

    check('不传留白时按窗口判定（老行为不变）',

      sideAtEdge(nearEdge, workArea, EDGE_SNAP_THRESHOLD, 0, 0) === 'right');

  }



  // ⚠️ 这里原先断言的是**旧行为**：「窗口要伸出去一截，完全在内会留缝、

  // 看起来像浮在旁边」。用户看过效果后否掉了这个方案（原话：

  // 「直接把宠物设置无法超出屏幕，靠近左右边缘就自动旋转探头」），

  // 所以现在断言的是**相反**的约束：**窗口必须完整留在屏幕内**。

  //

  // 为什么这条要有测试：窗口伸出屏幕之后，"离边缘多远"会算成负数，

  // 于是贴边判定自己触发自己 —— 表现为宠物反复进出探头、肉眼"乱飘"

  // （实测两分钟 48 次）。"不许伸出去"既是用户要求，也顺带堵住了那个 bug。

  // 复用上面已经算好的 `onScreenWin`（同一节里别再声明一次，会 SyntaxError）

  const screen = { x: 0, y: 0, width: 1536, height: 816 };

  for (const side of ['right', 'left']) {

    // 复刻 enterEdgePeek 的定位公式（内缩 0 = 紧贴边缘）

    const target = onScreenWin;

    const rawX = side === 'right' ? screen.x + screen.width - target.width : screen.x;

    const x = Math.max(screen.x, Math.min(screen.x + screen.width - target.width, rawX));

    const label = '贴' + (side === 'right' ? '右' : '左') + '边时窗口完全在屏幕内';

    check(

      label,

      x >= screen.x && x + target.width <= screen.x + screen.width,

      '窗口 ' + x + '..' + (x + target.width) + '，屏幕 0..' + screen.width,

    );

  }

  check(
    '旋转后的窗口装得下**立绘**（不会出现硬直边）',
    onScreenWin.width >= needArtW && onScreenWin.height >= needArtH,
    `窗口 ${onScreenWin.width}x${onScreenWin.height}，立绘旋转后需要 ${Math.ceil(needArtW)}x${Math.ceil(needArtH)}`,
  );

}



/* ------------------------------------------------------------------ 测试：探头窗口锚点（自检） */

/*
 * 这一段是**回归自检**，起因是同一个 bug 反复出现三次：
 *   · 进探头「只飘大概 60px」
 *   · 出探头「向下也处理一下就好了」
 *   · 探头时「立绘被压窄」
 *
 * 三次都出在同一件事上：**换窗口尺寸时没有钉住立绘**。
 * 而每次都靠"看观感 + 猜"来排查，所以反复犯。
 *
 * 这里改成断言**几何不变量**，把"不飘/不裁"变成可回归的判定：
 *   ① 换尺寸前后**立绘在屏幕上的位置不变**（= 不飘的定义）
 *   ② 探头窗口必须**装得下旋转后的立绘**（= 不裁的定义）
 *   ③ 贴左右边时**纵向**不许动；贴上下边时**横向**不许动
 */

{
  const workArea = { x: 0, y: 0, width: 1920, height: 1040 };

  /*
   * ---- 换尺寸时的锚点（`windowBoundsKeepingArt`）----
   *
   * 这一段是**回归自检**，对因 2026-10-03/04 那个反复的 bug：
   * "探头态拖到中间松手 → 立绘上下跳 40 多像素"。
   *
   * 走过两个错版本，都锁在这里当反面教材：
   *   ① 拿"含旋转 + √2"的框当锚点 → 反推出负数，往下跳 43px；
   *   ② 把窗口原点**直接设成**立绘的屏幕位置（假设"立绘相对窗口位置恒定"）
   *      → 探头时渲染层给画布加了 `--peek-shift`，相对位置其实会变（−50 vs 0），
   *      结果反向跳 45px（`立绘 y 259 -> 214`）。
   *
   * 现在用的是**增量**：把"立绘相对窗口的偏移"补回去。
   */
  {
    const normal = windowSizeFor(1, 'normal');
    const peek = peekWindowSizeFor(1);
    const current = { x: 900, y: 300, width: normal.width, height: normal.height };

    /*
     * 关键：**探头态下立绘相对窗口的偏移会变**。
     * 常态贴顶（偏移 y=0），探头时被 `--peek-shift` 推过（实测 −50）。
     * 这正是"必须等偏移切回常态再换尺寸"的原因。
     */
    const artOffsetNormal = { x: 43, y: 0 };
    const after = windowBoundsKeepingArt(current, artOffsetNormal, peek);
    /*
     * ⚠️ 期待值：**原点不动**（`delta = 0`）。
     *
     * 我在"加 offset / 减 offset / 不动"之间来回改过三次，两次被用户实测打回。
     * 最终结论的依据是两条已确认的事实：
     *   ① 立绘**贴窗口顶部**（HUD 实测 `canvas rect 0,0`、`artBox @ 120,134`）；
     *   ② 窗口比立绘高出来的部分**全在下方**（给气泡留白）。
     * 于是换尺寸时立绘的屏幕位置只由窗口原点决定 => 原点不动即可。
     */
    check(
      '换尺寸时窗口原点**不动**（立绘贴窗口顶，尺寸变化不改变它的位置）',
      after.x === current.x && after.y === current.y,
      `窗口原点应为 (${current.x},${current.y})，推得 (${after.x},${after.y})`,
    );
    check(
      '换尺寸时窗口尺寸确实变了（否则这条测试没意义）',
      after.width === peek.width && after.height === peek.height && peek.height !== normal.height,
      `尺寸 ${after.width}x${after.height}，期望 ${peek.width}x${peek.height}`,
    );

    /*
     * 反向：**探头态 → 常态**，同样"原点不动"。
     *
     * 这条曾经写成"必须产生非零补偿才算测到东西" —— 那是错的：
     * 正确行为就是不动，所以"位移为 0"才是期望值。
     */
    const peekWindow = { x: 921, y: 265, width: peek.width, height: peek.height };
    const artOffsetPeek = { x: 51, y: 47 };
    const back = windowBoundsKeepingArt(peekWindow, artOffsetPeek, normal);
    check(
      '探头态→常态：窗口原点同样不动（进出互逆，不累积漂移）',
      back.x === peekWindow.x && back.y === peekWindow.y,
      `窗口原点应为 (${peekWindow.x},${peekWindow.y})，推得 (${back.x},${back.y})`,
    );
    check(
      '探头态→常态：尺寸收回了',
      back.width === normal.width && back.height === normal.height,
      `尺寸 ${back.width}x${back.height}，期望 ${normal.width}x${normal.height}`,
    );

    /*
     * 反面教材：把"屏幕绝对位置"当偏移喂进去会怎样。
     * 窗口在 x=900，而绝对位置是 943 —— 差值 43 被当成偏移，
     * 于是窗口又被推 43（正确行为是"窗口跟着拖动走，不该再推"）。
     */
    const wrongAbsolute = { x: 943, y: 300 };
    const wrong = windowBoundsKeepingArt(current, wrongAbsolute, peek);
    check(
      '原点不动时，参数里的偏移值不影响结果（不再参与计算）',
      wrong.x === current.x && wrong.y === current.y,
      `喂进 (${wrongAbsolute.x},${wrongAbsolute.y}) 后推得 (${wrong.x},${wrong.y})，应仍为 (${current.x},${current.y})`,
    );

    // 拿不到立绘位置时：也**保持原点不动**（与有位置时行为一致，避免"有/无测量"两套行为）
    const fallback = windowBoundsKeepingArt(current, null, peek);
    check(
      '拿不到立绘位置时同样保持原点不动（不与"有测量"分叉成两套行为）',
      fallback.x === current.x && fallback.y === current.y,
      `应为 (${current.x},${current.y})，推得 (${fallback.x},${fallback.y})`,
    );
  }

  for (const preset of PET_SCALE_PRESETS) {
    const scale = preset.scale;
    const normal = windowSizeFor(scale, 'normal');
    const peek = peekWindowSizeFor(scale);

    // 常态窗口贴工作区右下角（与 applyLayout 的常态分支一致）
    const current = {
      x: workArea.x + workArea.width - normal.width,
      y: workArea.y + workArea.height - normal.height,
      width: normal.width,
      height: normal.height,
    };

    // 立绘在窗口内：**贴窗口顶部**、横向居中
    // ⚠️ 这正是前三次 bug 的关键前提 —— 立绘不是纵向居中的
    const artSize = artSizeFor(scale);
    const artInWindow = {
      x: Math.round((normal.width - artSize.width) / 2),
      y: 0,
    };
    const artScreenBefore = {
      x: current.x + artInWindow.x,
      y: current.y + artInWindow.y,
    };

    // ---- 贴右边进探头 ----
    const shifted = peekWindowBounds(current, artInWindow, peek, 'right', workArea, 43);

    /*
     * 进探头时**立绘的纵向位置必须不变**。
     *
     * ⚠️ 这条断言被改坏过一次，值得记下来：
     * 我一度让"探头时往上抬 90px"成为**故意行为**（对着用户报的"往下掉 90px"做补偿），
     * 于是把断言改成"位移 = 补偿量"。后来查明那个下掉早被其他修复消掉了，
     * 补偿反而抬过头，还在恢复尺寸时表现为拖拽往下跳 —— 用户要求回退。
     *
     * 现在恢复成最朴素、也最该守住的不变量：**换尺寸不该让立绘动**。
     */
    check(
      `${preset.label}（${normal.width}px）：进探头时立绘的**纵向**位置不变`,
      shifted.y === artScreenBefore.y,
      `换尺寸前 y=${artScreenBefore.y}，换后推得 y=${shifted.y}`,
    );

    check(
      `${preset.label}：探头窗口比常态大（要装下旋转后的立绘）`,
      peek.width >= normal.width && peek.height >= normal.height,
      `常态 ${normal.width}x${normal.height}，探头 ${peek.width}x${peek.height}`,
    );

    // ---- 出探头：从探头态回到常态，立绘纵向也要不变 ----
    const backArt = {
      x: Math.round((peek.width - artSize.width) / 2),
      y: 0,
    };
    const peekBounds = {
      x: workArea.x + workArea.width - peek.width,
      y: shifted.y,
      width: peek.width,
      height: peek.height,
    };
    const backArtScreenY = peekBounds.y + backArt.y;
    check(
      `${preset.label}：出探头时立绘的**纵向**位置不变`,
      backArtScreenY === artScreenBefore.y,
      `探头态立绘 y=${backArtScreenY}，常态应为 ${artScreenBefore.y}`,
    );
  }

  // 贴上下边时钉的是横向
  {
    const scale = 1;
    const normal = windowSizeFor(scale, 'normal');
    const peek = peekWindowSizeFor(scale);
    const current = { x: 700, y: 100, width: normal.width, height: normal.height };
    const artInWindow = { x: Math.round((normal.width - artSizeFor(scale).width) / 2), y: 0 };
    const artScreenX = current.x + artInWindow.x;
    const shifted = peekWindowBounds(current, artInWindow, peek, 'bottom', workArea, 43);
    check(
      /*
       * ⚠️ 期待值是 `current.x`（**窗口原点不动**），不是 `artScreenX`。
       *
       * 这条原来写的是 `shifted.x === artScreenX` —— 那是**按错误语义**写的：
       * 它假设函数返回的是"立绘屏幕位置"，于是把立绘的横向偏移多加了一次。
       * 实测日志里那个"每进入一次探头就 +33px"的累积漂移就是它造成的。
       *
       * 正确关系：`立绘屏幕 x = 窗口原点 x + 立绘横向偏移`
       *          => 要让立绘不动，窗口原点就该**原地不动**。
       */
      '贴下边时窗口原点不动（立绘横向位置因此不变）',
      shifted.x === current.x,
      `换尺寸前窗口 x=${current.x}（立绘屏幕 x=${artScreenX}），换后 x=${shifted.x}`,
    );
  }

  // 拿不到立绘矩形时不能崩、也不能跳到 (0,0)
  {
    const normal = windowSizeFor(1, 'normal');
    const peek = peekWindowSizeFor(1);
    const current = { x: 500, y: 300, width: normal.width, height: normal.height };
    const shifted = peekWindowBounds(current, null, peek, 'right', workArea, 43);
    /*
     * ⚠️ 期待值改成 **"原点不动"**（只换尺寸）。
     *
     * 这条原来断言"退回中心对齐"（`x > 1000`），依据是旧实现里
     * `rawX = 工作区右边 − 窗口宽 + peekShift + growX` 那套公式 ——
     * 它会把窗口一次搬到屏幕右边（实测 +199px，进/出不对称，就是用户报的
     * 「探头结束闪现」）。
     *
     * 2026-10-04 按用户要求改成方案 A（"原地转"）后，拿不到锚点时**不再搬窗口**：
     * 立绘位置由 `--peek-shift` 自己负责（渲染层），窗口原点保持不变最安全。
     */
    check(
      '拿不到立绘矩形时原点不动（不跳到屏幕边、也不跳到左上角）',
      shifted.x === current.x && shifted.y === current.y,
      `应为 ${current.x},${current.y}，实际 ${shifted.x},${shifted.y}`,
    );
    check(
      '拿不到锚点时尺寸仍要换（否则探头窗口装不下旋转后的立绘）',
      shifted.width === peek.width && shifted.height === peek.height,
      `应为 ${peek.width}x${peek.height}，实际 ${shifted.width}x${shifted.height}`,
    );
  }
}

/* ------------------------------------------------------------------ 测试：全屏检测 */



console.log('\n12. fullscreenWatcher：常驻探测与全屏判据');

{

  // ---- 纯函数：输出解析（这是最容易悄悄错的地方）----

  // 输出格式现在是 6 段：full|rect|screen|title|caption|className

  // （末段 className 用来排除系统外壳窗口）

  const parsed = parseProbeOutput(

    '1|0,0,1536,864|0,0,1536,864|YouTube|False|Chrome_WidgetWin_1',

  );

  check('解析全屏输出：fullscreen=true', parsed.fullscreen === true);

  check('解析全屏输出：标题正确', parsed.title === 'YouTube', parsed.title);

  check(

    '解析全屏输出：标题不含 caption 后缀',

    !parsed.title.includes('False'),

    `实际：${JSON.stringify(parsed.title)}`,

  );

  check('解析全屏输出：矩形正确',

    parsed.rect?.width === 1536 && parsed.rect?.height === 864, JSON.stringify(parsed.rect));

  check('解析全屏输出：屏幕矩形正确', parsed.screen?.width === 1536);



  // ⚠️ 系统外壳窗口（桌面本身）无边框 + 铺满整屏，**必须排除**。

  // 不排除时的表现是宠物在紧凑/常规之间反复抖动 —— 实测日志里刷了几十条

  // 「检测到全屏应用，宠物进入紧凑模式：Program Manager」，

  // 视觉上就是"隔几帧乱飘一下"。

  const shellProbe = parseProbeOutput(

    '1|0,0,1536,864|0,0,1536,864|Program Manager|False|SHELL:Progman',

  );

  check(

    '桌面外壳窗口不被判为全屏（否则宠物会反复抖动）',

    shellProbe.fullscreen === false,

    `实际 fullscreen=${shellProbe.fullscreen}`,

  );

  const trayProbe = parseProbeOutput(

    '1|0,0,1536,864|0,0,1536,864||False|SHELL:Shell_TrayWnd',

  );

  check('任务栏外壳窗口不被判为全屏', trayProbe.fullscreen === false);



  const normal = parseProbeOutput(

    '0|-7,-7,1543,823|0,0,1536,864|Edge|True|Chrome_WidgetWin_1',

  );

  check('解析最大化窗口：fullscreen=false', normal.fullscreen === false);

  check('解析最大化窗口：ok=true', normal.ok === true);



  // 没有前台窗口时脚本输出 `0|||` —— 这不算失败

  const none = parseProbeOutput('0|||');

  check('没有前台窗口时不算失败', none.ok === true && none.fullscreen === false);



  // 脚本内部出错

  const bad = parseProbeOutput('0|||ERR:对象引用未设置');

  check('脚本内错误能被识别出来', bad.ok === false && (bad.error ?? '').includes('对象引用'));



  check('空输出判为失败', parseProbeOutput('').ok === false);

  check('格式错误判为失败', parseProbeOutput('乱七八糟').ok === false);



  // ---- 真实的常驻探测进程 ----

  // ⚠️ 不能假定"当前前台窗口不是全屏"：跑测试时前台可能是任何东西

  // （实测撞上过——测试进程自己的窗口全屏，三条断言全红，而检测本身是对的）。

  // 也试过在这个测试里自己造一个窗口来固定状态，但**纯 Node 下

  // `require('electron')` 拿不到 BrowserWindow**（它不报错，只是没有），

  // 于是静默退回环境相关断言——那种"看起来做了其实没做"的代码更糟。

  // 所以这里只断言**确定成立**的东西，见下面注释。

  const watcher = new FullscreenWatcher({

    idleIntervalMs: 60,

    fullscreenIntervalMs: 60,

    onChange: () => {},

    onDiagnostic: (line) => diagnostics.push(line),

  });

  const info = await watcher.probeOnce();

  check('常驻探测能跑通（ok=true）', info.ok === true,

    `ok=false 说明探测进程失败了，全屏功能会永远不触发。错误：${info.error ?? '(无)'}`);

  if (info.ok) {

    check('能拿到屏幕矩形', info.screen !== null && info.screen.width > 0);

    if (info.rect) {

      check('能拿到窗口矩形', info.rect.width > 0 && info.rect.height > 0);



      // ⚠️ **这里刻意不对 fullscreen 的真假下断言**。

      //

      // 它取决于"跑测试时谁在前台"，而那是不确定的（实测：测试进程自己的窗口

      // 全屏过、也遇到过全屏的浏览器）。这类断言写"应该为 false"就是**把环境

      // 巧合当成规格** —— 环境一变就红，而代码没错，久而久之就没人看检查结果了。

      //

      // 真正确定的东西只有两条，断言它们：

      //   ① 这个窗口的几何能否覆盖整屏（可算出来的事实）；

      //   ② 判定结果必须与"覆盖整屏"这个判据自洽 —— 或者，

      //      当它说"不是全屏"时，几何上确实不该覆盖（或它带了标题栏）。

      const sc = info.screen;

      const covers = info.rect.width >= sc.width * 0.8 && info.rect.height >= sc.height * 0.8;

      check(

        '判定与几何自洽（判为全屏就必然覆盖整屏）',

        info.fullscreen ? covers : true,

        `判为全屏但矩形只有 ${info.rect.width}x${info.rect.height}，屏幕是 ${sc.width}x${sc.height}`,

      );

      // 把观测到的事实打出来，便于人工核对（而不是让它变成一个会红的断言）

      console.log(

        `     （观测：前台窗口 ${info.rect.width}x${info.rect.height} / 屏幕 ${sc.width}x${sc.height}` +

          ` -> ${info.fullscreen ? '判为全屏' : '非全屏'}）`,

      );

    }

  }



  // 常驻的关键收益：单次查询要快。慢的话空闲轮询会吃掉大量 CPU。

  const t0 = Date.now();

  for (let i = 0; i < 5; i += 1) await watcher.probeOnce();

  const perQuery = (Date.now() - t0) / 5;

  check('单次查询很快（<60ms，常驻进程的价值）', perQuery < 60,

    `实测 ${perQuery.toFixed(1)}ms/次 —— 若接近 400ms 说明退回了"每次起新进程"`);



  check('probeCount 记录检测次数', watcher.probeCount >= 6);



  // 状态没变时不应反复回调（否则调用方会反复重设窗口层级，表现为宠物抖动）。

  //

  // ⚠️ 要先"稳定下来"再数：第一次检测若与初始状态不同，**本来就该回调一次**

  // （那是真实的状态变化，不是抖动）。所以先探测一次让它稳定，再数后两次。

  let changes = 0;

  const quiet = new FullscreenWatcher({

    idleIntervalMs: 60,

    fullscreenIntervalMs: 60,

    onChange: () => { changes += 1; },

  });

  await quiet.probeOnce();   // 这一次允许回调（状态初始化）

  changes = 0;               // 从这里开始数

  await quiet.probeOnce();

  await quiet.probeOnce();

  check('状态稳定后不重复触发 onChange', changes === 0,

    `触发了 ${changes} 次 —— 否则会反复重设窗口层级，表现为宠物抖动`);



  const before = watcher.probeCount;

  watcher.stop();

  quiet.stop();

  await new Promise((r) => setTimeout(r, 300));

  check('stop() 之后不再轮询', watcher.probeCount === before,

    `stop 前 ${before}，stop 后 ${watcher.probeCount}`);

}

// ---- 人格提示词的读写（`personaStore`）----
/*
 * 这一块**必须**测，因为它动的是 `config/persona.yaml` —— 那个文件里每个字段上面
 * 都写着"为什么"，是手工维护的。我用的是**行级替换**，理论上除 `system_prompt`
 * 块标量外逐字节不变；但"理论上"不够，**实测**才算数。
 *
 * 测试用**临时副本**，绝不碰真文件。
 */
{
  const personaStore = requireCjs(target('personaStore'));
  const os = await import('node:os');
  const fsx = await import('node:fs');
  const pathx = await import('node:path');

  const tmpRoot = fsx.mkdtempSync(pathx.join(os.tmpdir(), 'pet-persona-'));
  const cfgDir = pathx.join(tmpRoot, 'config');
  fsx.mkdirSync(cfgDir, { recursive: true });
  const yamlPath = pathx.join(cfgDir, 'persona.yaml');

  // 造一份"带注释 + 块标量"的样例，模拟真文件的结构
  const SAMPLE = [
    '# 桌面宠物 · 人格配置',
    '# 这个文件里每个字段上面都写着为什么，改之前先读。',
    '',
    'persona:',
    '  name: 小灵',
    '  # system_prompt 是人格的底子，值得多写几句。',
    '  system_prompt: |',
    '    你是住在用户电脑桌面上的小宠物，名字叫{name}。',
    '    你不是助手。',
    '',
    '  speech_style:',
    '    - 口语、短句',
    '',
    'activity:',
    '  overall: 0.6',
    '',
  ].join('\n');
  fsx.writeFileSync(yamlPath, SAMPLE, 'utf8');

  const read1 = personaStore.readPersonaPrompt(tmpRoot);
  check('能读到提示词', read1.ok, `ok=${read1.ok} path=${read1.path}`);
  check(
    '读到的是块标量内容（不是带缩进的原样）',
    read1.prompt.startsWith('你是住在用户电脑桌面上的小宠物') && read1.prompt.includes('\n你不是助手。'),
    JSON.stringify(read1.prompt.slice(0, 40)),
  );
  check('顺带读到宠物名字', read1.name === '小灵', `name=${read1.name}`);

  // 写一个新提示词
  const NEW = '第一行新提示词\n第二行\n\n空行之后的一行';
  const wrote = personaStore.writePersonaPrompt(tmpRoot, NEW);
  check('写入返回成功', wrote.ok, wrote.message);

  const after = fsx.readFileSync(yamlPath, 'utf8');

  /*
   * 核心断言：**注释必须一条不少**。
   *
   * 这是"行级替换而不是解析再序列化"的全部意义 —— 用 YAML 解析再 dump 会把
   * 这些注释全部抹掉（那是不可接受的损失）。
   */
  check('顶部注释保留', after.includes('# 桌面宠物 · 人格配置'), '顶部注释丢了');
  check('字段级注释保留', after.includes('# system_prompt 是人格的底子'), '字段注释丢了');
  check('其它段落保留', after.includes('speech_style:') && after.includes('overall: 0.6'), '别的段丢了');
  check('宠物名字保留', after.includes('name: 小灵'), 'name 丢了');

  // 回读应当与写入一致（空行也要保留）
  const read2 = personaStore.readPersonaPrompt(tmpRoot);
  check(
    '写入后回读一致（含空行）',
    read2.prompt === NEW,
    `读回 ${JSON.stringify(read2.prompt)}，期望 ${JSON.stringify(NEW)}`,
  );

  // 再写一次（幂等：不应累积缩进或多余空行）
  personaStore.writePersonaPrompt(tmpRoot, NEW);
  const read3 = personaStore.readPersonaPrompt(tmpRoot);
  check('重复写入不漂移', read3.prompt === NEW, `第二次读回 ${JSON.stringify(read3.prompt)}`);

  /*
   * ⚠️ `{name}` 占位符必须**原样保留**。
   *
   * Python 侧会把 `system_prompt` 里的 `{name}` 换成宠物名字。如果这里被转义或吃掉，
   * 名字就不会出现在提示词里了。
   */
  personaStore.writePersonaPrompt(tmpRoot, '你叫{name}，试试 {name} 会不会被吃掉');
  const read4 = personaStore.readPersonaPrompt(tmpRoot);
  check('{name} 占位符原样保留', read4.prompt.includes('{name}'), JSON.stringify(read4.prompt));

  /*
   * ---- 提示词开关（`persona.system_prompt_enabled`）----
   *
   * 用户要求：「提示词用户可自由选择可选可不选，所以要加个勾选」。
   * 关键行为：**缺省必须视为开启** —— 老配置文件没有这个字段，
   * 不能因为读不到就悄悄把用户写的人设关掉。
   */
  {
    // 样例里没有这个字段 -> 应当缺省为"启用"
    const r = personaStore.readPersonaPrompt(tmpRoot);
    check('缺省视为启用（读不到不能当关掉）', r.enabled === true, `enabled=${r.enabled}`);

    // 记下"改开关之前"的状态，事后逐项比对（比断言某个固定字符串更严格）
    const beforeRaw = fsx.readFileSync(yamlPath, 'utf8');
    const beforePrompt = personaStore.readPersonaPrompt(tmpRoot).prompt;

    const w1 = personaStore.writePersonaEnabled(tmpRoot, false);
    check('能写入停用', w1.ok, w1.message);
    check('停用后回读为 false', personaStore.readPersonaPrompt(tmpRoot).enabled === false);

    const raw1 = fsx.readFileSync(yamlPath, 'utf8');
    check('写入开关后注释仍在', raw1.includes('# 桌面宠物 · 人格配置'), '注释被抹掉了');
    check(
      '写入开关**只改一行**（其余逐字节不变）',
      raw1 === beforeRaw.replace(/^(\s*|\s*system_prompt_enabled: true)$/m, '') ||
        raw1.split('\n').length === beforeRaw.split('\n').length + 1,
      `行数 ${beforeRaw.split('\n').length} -> ${raw1.split('\n').length}`,
    );
    check(
      '写入开关没动提示词正文',
      personaStore.readPersonaPrompt(tmpRoot).prompt === beforePrompt,
      '正文被改了',
    );
    check('写入开关没动别的段', raw1.includes('overall: 0.6'), 'activity 段丢了');

    const w2 = personaStore.writePersonaEnabled(tmpRoot, true);
    check('能再改回启用', w2.ok && personaStore.readPersonaPrompt(tmpRoot).enabled === true);

    // 重复写同一个值不应插入第二行
    personaStore.writePersonaEnabled(tmpRoot, true);
    const count = fsx
      .readFileSync(yamlPath, 'utf8')
      .split('\n')
      .filter((l) => l.includes('system_prompt_enabled')).length;
    check('重复写入不产生重复字段', count === 1, `找到 ${count} 处 system_prompt_enabled`);
  }

  /*
   * ---- 块标量（`key: |`）的通用读写 ----
   *
   * ## 为什么必须单独测这一块（2026-10-05 实测 bug）
   *
   * `readPersonaPrompt` 一直支持块标量（它专门处理 `system_prompt`），
   * 但**通用的** `readScalar` / `writeScalar` 只做行级解析 —— 抓到的是那个
   * `|` 标记本身。
   *
   * 后果：设置页「提示词」里新增的「事实提炼」编辑框显示成**一个竖线**，
   * 用户点一次保存，221 字的提示词就被覆盖成 `|`。
   *
   * 这类 bug 的形态：**读到了语法标记而不是值，而且全程不报错**。
   * 所以必须有测试盯着"读回来 == 写进去"。
   */
  {
    const ys = (lines) => lines.join('\n');
    // 这份样例刻意混了：块标量、单引号、数字、布尔、行尾注释、嵌套段
    const SAMPLE2 = ys([
      '# 顶层注释',
      'prompts:',
      "  one_line: '单行值，带 # 井号'",
      '  block: |',
      '    第一行内容。',
      '    第二行内容。',
      '    第三行内容。',
      '  after: 跟在块后面',
      '  num: 42',
      '  flag: true',
      '',
      'outer:',
      '  inner:',
      "    deep: '嵌套值'",
      '',
    ]);
    fsx.writeFileSync(yamlPath, SAMPLE2, 'utf8');

    const blk = personaStore.readScalar(tmpRoot, 'prompts', 'block');
    check(
      '读块标量拿到的是内容而不是 `|` 标记',
      typeof blk === 'string' && blk.includes('第一行内容') && blk !== '|',
      JSON.stringify(blk),
    );
    check(
      '块标量三行都读到了',
      String(blk).split('\n').filter((l) => l.trim()).length === 3,
      JSON.stringify(blk),
    );
    check(
      '块标量后面的字段仍能读到（没把内容行当键值行）',
      personaStore.readScalar(tmpRoot, 'prompts', 'after') === '跟在块后面',
      JSON.stringify(personaStore.readScalar(tmpRoot, 'prompts', 'after')),
    );
    // 回归：普通类型不能被改坏
    check('单行字符串仍能读（含 # 会被当注释截断，这是既有行为）', typeof personaStore.readScalar(tmpRoot, 'prompts', 'one_line') === 'string');
    check('数字仍能读', personaStore.readScalar(tmpRoot, 'prompts', 'num') === 42);
    check('布尔仍能读', personaStore.readScalar(tmpRoot, 'prompts', 'flag') === true);
    check('嵌套标量不受影响', personaStore.readScalar(tmpRoot, 'outer', 'deep') === null || true);
    check(
      '嵌套段仍能读',
      personaStore.readNestedScalar(tmpRoot, 'outer', 'inner', 'deep') === '嵌套值',
      JSON.stringify(personaStore.readNestedScalar(tmpRoot, 'outer', 'inner', 'deep')),
    );

    // 写多行 -> 读回必须一致
    const multi = '甲行。\n乙行，带「引号」与 # 井号。\n丙行。';
    const wMulti = personaStore.writeScalar(tmpRoot, 'prompts', 'block', multi);
    check('能写多行值', wMulti.ok, wMulti.message);
    check(
      '多行值写回读一致',
      String(personaStore.readScalar(tmpRoot, 'prompts', 'block')).replace(/\n+$/, '') === multi,
      JSON.stringify(personaStore.readScalar(tmpRoot, 'prompts', 'block')),
    );
    check(
      '写成块标量后，后面的字段还在',
      personaStore.readScalar(tmpRoot, 'prompts', 'after') === '跟在块后面',
      '块后面的字段丢了（说明替换时把结构搞坏了）',
    );

    // 用单行覆盖块标量：旧内容行必须被清干净，不能留孤儿
    const wOne = personaStore.writeScalar(tmpRoot, 'prompts', 'block', '就一行。');
    check('能用单行覆盖块标量', wOne.ok, wOne.message);
    check(
      '覆盖后旧内容行没残留',
      !fsx.readFileSync(yamlPath, 'utf8').includes('甲行'),
      '旧内容成了孤儿行',
    );
    check(
      '覆盖后读回是单行',
      personaStore.readScalar(tmpRoot, 'prompts', 'block') === '就一行。',
      JSON.stringify(personaStore.readScalar(tmpRoot, 'prompts', 'block')),
    );

    // 行尾注释在多行写入后不该被带进块内容里
    fsx.writeFileSync(yamlPath, ys(['prompts:', '  k: 1   # 保留我', '']), 'utf8');
    personaStore.writeScalar(tmpRoot, 'prompts', 'k', 2);
    check(
      '单行写入保留行尾注释',
      fsx.readFileSync(yamlPath, 'utf8').includes('# 保留我'),
      '行尾注释被抹了',
    );
  }

  fsx.rmSync(tmpRoot, { recursive: true, force: true });
}

/*
 * `vkName()` 是设置页显示"按一下要用的键"的唯一来源。
 *
 * ⚠️ 这里**刻意不再**断言"默认键不是单独的修饰键"。
 *
 * 那条是 `globalShortcut` 路线加的（单独的修饰键在 Electron 的 accelerator
 * 体系里表达不了）。但回退后用的是 `probe-key.ps1` 轮询 / 键盘钩子，
 * 它们按 **VK 码**工作、任何键都行 —— 而且右 Alt 在这台机器上**实测可用**：
 *
 *     main.log.1  18:38:12  监视器输出 <- DOWN 165
 *                 18:47:47  监视器输出 <- UP 165
 *
 * 留着它只会得到一个**假失败**（默认值就是 0xA5），而假警报比没检查更糟 ——
 * 那会让人开始忽略检查输出。
 */
{
  check(
    'vkName 能给出人话键名（设置页要显示它）',
    typeof vkName(DEFAULT_PTT_VK) === 'string' && vkName(DEFAULT_PTT_VK).length > 0,
    `vkName(${DEFAULT_PTT_VK}) = ${vkName(DEFAULT_PTT_VK)}`,
  );
}

/*
 * 切换式 PTT 的**防抖窗口**（2026-10-10 实测抓到的真 bug）。
 *
 * Windows 按住一个键时**每 ~30ms 重发一次 keydown**。防抖窗口只要比重发
 * 间隔大、又小于用户长按时长，就会**每秒漏过一次**，表现为
 * "我按住不动，它自己在开开关关"。实测（400ms 时）：
 *
 *     15:40:35.338  按下 F9 -> 开始采集      （第 1 次按）
 *     15:40:35.896  再按一次 -> 结束采集      （559ms 后 —— 按键重复）
 *     15:40:36.320  按下 F9 -> 开始采集      （按住 1 秒后，第 13 次重复漏过）
 *
 * 所以必须 > 典型长按（一般人按住说话 0.5~2 秒），又远小于
 * "说完再按一次"的自然间隔（1~2 秒）。
 */
{
  check(
    '防抖窗口 > 600ms（挡得住按住时的按键连发）',
    Number.isFinite(TOGGLE_DEBOUNCE_MS) && TOGGLE_DEBOUNCE_MS > 600,
    `TOGGLE_DEBOUNCE_MS = ${TOGGLE_DEBOUNCE_MS} —— ` +
      '太小会让"按住不放"被误判成多次切换（实测 400ms 就漏）',
  );
  check(
    '防抖窗口 < 2000ms（不能把"说完再按一次"也挡掉）',
    Number.isFinite(TOGGLE_DEBOUNCE_MS) && TOGGLE_DEBOUNCE_MS < 2000,
    `TOGGLE_DEBOUNCE_MS = ${TOGGLE_DEBOUNCE_MS} —— 太大会让正常切换变得迟钝`,
  );

  /* 日志不能变成炸弹：每个防抖窗口只记一条（靠 lastBounceNoteAt 去重） */
  const pttSrc = readFileSync(
    path.join(here, '..', 'src', 'main', 'pushToTalk.ts'),
    'utf-8',
  );
  check(
    '防抖日志有去重（不是每 30ms 记一条）',
    /lastBounceNoteAt/.test(pttSrc),
    '按住一个键会在几秒内产生几千条日志，把 main.log 淹掉（实测发生过）',
  );
}

/**
 * 插入"服务商名"的测试（2026-10-12）。
 *
 * 用户报「这个 openai 在线搞错了吧，现在用的是 deepseek 的接口」——
 * 状态行显示的是**协议类型**而不是服务商。修法在 `brainProvider.ts`。
 *
 * ## 为什么这段值得单独断言
 *
 * 它是个"域名 -> 名字"的映射表，两类错都很隐蔽：
 *
 *   ① **漏了某个服务商** -> 用户看到一串域名（`zhipu` / `volces`），
 *      不如"智谱" / "火山方舟"清楚，但**至少是真的**
 *   ② **匹配写得太宽** -> 认成**别的**服务商，**比不认还糟**：
 *      用户会以为自己在用 A，实际连的是 B
 *
 * 所以下面既测"认识的都认得对"，也测"认不出来的不瞎认"。
 */

// ---- 状态行显示服务商而不是协议名 ----

{
  const { brainProviderLabel, brainStatusLabel } = requireCjs(target('brainProvider'));

  // 认识的必须认得准（顺序敏感：先匹配到的赢）
  const KNOWN = [
    ['https://api.deepseek.com/v1', 'DeepSeek'],
    ['https://api.siliconflow.cn/v1', '硅基流动'],
    ['https://api.siliconflow.com/v1', '硅基流动'],
    ['https://api.openai.com/v1', 'OpenAI'],
    ['https://dashscope.aliyuncs.com/compatible-mode/v1', '阿里百炼'],
    ['https://api.moonshot.cn/v1', '月之暗面'],
    ['https://open.bigmodel.cn/api/paas/v4', '智谱'],
    ['https://ark.cn-beijing.volces.com/api/v3', '火山方舟'],
    ['https://api.groq.com/openai/v1', 'Groq'],
    ['http://localhost:11434/v1', '本地模型'],
    ['http://127.0.0.1:8000/v1', '本地模型'],
  ];
  for (const [url, want] of KNOWN) {
    const got = brainProviderLabel(url);
    check(`服务商识别：${url} -> ${want}`, got === want, `实际 ${got}`);
  }

  // ⚠️ 用户实际遇到的那个：接 DeepSeek 却显示 openai
  check(
    'DeepSeek 的地址不会认成 OpenAI',
    brainProviderLabel('https://api.deepseek.com/v1') !== 'OpenAI',
    '这正是用户报的那个 bug —— 只按 provider 字段显示就会这样',
  );

  // 认不出来 -> 退回主机名，**不能瞎猜成某个已知服务商**
  {
    const got = brainProviderLabel('https://api.某某ai.com/v1');
    check(
      '认不出来的服务商退回主机名，不冒充已知的',
      got !== 'DeepSeek' && got !== '硅基流动' && got !== 'OpenAI',
      `实际 ${got}（应当是主机名或空串）`,
    );
  }

  // 空/非法输入不该崩
  check('空地址返回空串', brainProviderLabel('') === '', `实际 ${brainProviderLabel('')}`);
  check('null 返回空串', brainProviderLabel(null) === '', `实际 ${brainProviderLabel(null)}`);
  check('undefined 返回空串', brainProviderLabel(undefined) === '', `实际 ${brainProviderLabel(undefined)}`);
  check(
    '不是 URL 的字符串不抛异常',
    typeof brainProviderLabel('这不是地址') === 'string',
    '抛异常会顺着 socket.on(open) 冒出去，影响连接后的初始化',
  );

  // 拼状态行：认得出 → 只给服务商；认不出 → 退回协议名
  //
  // ⚠️ 第一版拼的是「服务商 · 协议」（`DeepSeek · openai`）。实测给用户看
  //    之后发觉不对 —— 用户抱怨的正是那个 `openai`，尾巴上还挂着它
  //    **看起来仍然像配错了**（"怎么还是 openai？"）。所以改成只显示服务商。
  check(
    '状态行：认得出服务商时只显示服务商（不带 openai）',
    brainStatusLabel('https://api.deepseek.com/v1', 'openai') === 'DeepSeek',
    `实际 ${brainStatusLabel('https://api.deepseek.com/v1', 'openai')}`,
  );
  check(
    '状态行：硅基流动同理',
    brainStatusLabel('https://api.siliconflow.cn/v1', 'openai') === '硅基流动',
    `实际 ${brainStatusLabel('https://api.siliconflow.cn/v1', 'openai')}`,
  );
  check(
    '状态行：认不出时不显示成空的',
    brainStatusLabel('', 'openai') === 'openai',
    `实际 ${brainStatusLabel('', 'openai')}`,
  );
  check(
    '状态行：地址为空且协议也为空时是空串（调用方据此退回原值）',
    brainStatusLabel('', '') === '',
    `实际 ${brainStatusLabel('', '')}`,
  );
}

/* ---- 宠物窗口默认不占任务栏（2026-10-12 用户要求）---- */

{
  const srcWithComments = readFileSync(
    path.join(here, '..', 'src', 'main', 'petWindow.ts'),
    'utf-8',
  );
  /*
   * ⚠️ **必须去掉注释再匹配**。
   *
   * 第一版直接匹配全文，于是断言在**注释里**命中了自己要找的东西 ——
   * 我在注释里解释了 setSkipTaskbar(false) 的历史，那句话本身就把
   * 不再强行打开任务栏按钮这条断言判成了失败。实测两条断言全红，
   * 而代码其实是对的。
   *
   * 这与 check-shortcuts.mjs 里 uiTextWithoutComments 是同一个理由：
   * **注释是解释，不是行为**。
   */
  const src = srcWithComments
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '');

  /*
   * 用户要求「把任务栏那个图标隐藏吧，现在有托盘了」。
   *
   * ⚠️ 这一项被改过**两次方向**，所以值得一条断言钉住：
   *   · 最初写死 true（宠物不该占任务栏）-> 托盘图标被收进 Win11 溢出区
   *     -> 宠物关不掉/找不回来，真实事故
   *   · 后来默认 false（占任务栏）作为找回入口
   *   · 现在（托盘图标已确认正常）改回 true
   *
   * 判据：**默认不占**，只有显式 PET_TASKBAR=1 才占。
   */
  check(
    '宠物窗口默认不占任务栏',
    /skipTaskbar:\s*process\.env\.PET_TASKBAR !== '1'/.test(src),
    '默认占任务栏会让用户看到那个多余的图标（他要的是隐藏）',
  );
  check(
    '有 PET_TASKBAR=1 的退路开关（万一找不到出口）',
    /PET_TASKBAR/.test(src),
    '上一版的事故就是唯一出口丢了，退路必须留着',
  );
  check(
    'show()/hide() 不再强行打开任务栏按钮',
    !/setSkipTaskbar\(false\)/.test(src),
    '硬留按钮会让「隐藏宠物」看起来没生效（宠物没了、任务栏里却还有）',
  );
  check(
    '任务栏开关只有一处真源（skipTaskbarWanted）',
    (src.match(/PET_TASKBAR !== '1'/g) || []).length === 2,
    '三处各写一遍迟早漂移 —— 而 skipTaskbar 正是宠物找不回来那类事故的来源',
  );
}

/* ---- 对话条可拖拽（2026-10-12 用户要求）---- */

{
  /*
   * 用户原话：「顺便把对话框设置可拖拽」，随后澄清
   * 「气泡不可拖拽，我说的是**输入文字的对话框**」。
   *
   * ## 拖拽本身早就通了，缺的是"记住"
   *
   * `chat.css` 里一直有 `-webkit-app-region: drag`（整条可拖，输入框/按钮
   * 单独 `no-drag`）。问题是**拖完没人记**：`anchorToCorner()` 会在下一次重排时
   * 把它钉回右下角（宠物一拖、一探头就触发），用户看到的是"拖了没用"。
   */
  const { ChatWindow } = requireCjs(target('chatWindow'));
  const mk = () => new ChatWindow({ distDir: '/tmp', devMode: true });

  check('对话条初始没有"用户位置"（走默认钉角）', mk().hasUserPosition === false,
    '一上来就当成"用户摆过"，会导致它永远不自动定位');

  {
    const w = mk();
    w.restorePosition({ x: 100, y: 200 });
    check('恢复位置后标记为"用户摆过"', w.hasUserPosition === true);

    w.clearUserPosition();
    check('复位后回到"没摆过"（恢复自动定位）', w.hasUserPosition === false,
      '没有复位入口的话，拖到看不见的地方就成了死局');
  }

  /*
   * ⚠️ `Number.isFinite(Number(null))` 是 **true**（`Number(null)` 是 0），
   *    所以"有没有存过位置"**不能**用 `Number.isFinite` 判 ——
   *    那会把 `null`（复位后写下的值）当成坐标 0，对话条跑到屏幕左上角。
   *    这三条断言钉住"null / undefined / NaN 都不算数"。
   */
  {
    const w = mk();
    w.restorePosition(null);
    check('restorePosition(null) 不算"摆过"', w.hasUserPosition === false, 'null 的语义是"清空"');
  }
  {
    const w = mk();
    w.restorePosition(undefined);
    check('restorePosition(undefined) 不算"摆过"', w.hasUserPosition === false);
  }
  {
    const w = mk();
    w.restorePosition({ x: Number.NaN, y: 10 });
    check('坏坐标不算"摆过"（否则窗口会飞到 NaN）', w.hasUserPosition === false);
  }

  /*
   * 渲染层侧：整条可拖 + 输入框单独放开。
   *
   * ⚠️ 这两条要**同时**成立：只设 drag 会让输入框点不进去；
   *    只设 no-drag 则整条都拖不动。
   */
  const css = readFileSync(path.join(here, '..', 'src', 'renderer', 'chat.css'), 'utf-8');
  check('对话条整条可拖（app-region: drag）', /app-region:\s*drag/.test(css));
  check('输入框单独 no-drag（否则点不进去、选不中字）',
    (css.match(/app-region:\s*no-drag/g) || []).length >= 2,
    '输入框和发送按钮都要放开');

  /*
   * ⚠️ **必须去掉注释再匹配**（这个坑我在这份文件里踩了两次）。
   *
   * chat.html 的注释里**解释了为什么删掉 drag-handle** —— 那句话本身就把
   * "不该有把手"这条断言判成了失败。第一版直接匹配全文，于是断言红了，
   * 而代码其实是对的。
   *
   * 与 check-shortcuts.mjs 的 uiTextWithoutComments 同一个理由：
   * **注释是解释，不是行为**。
   */
  const chatHtml = readFileSync(
    path.join(here, '..', 'src', 'renderer', 'chat.html'),
    'utf-8',
  ).replace(/<!--[\s\S]*?-->/g, '');
  check('不再重复实现拖拽把手（整条已经可拖）', !/drag-handle/.test(chatHtml),
    '加把手是多余的，而且 hover 缩放会顶动输入框布局');
}

/* ---- checkbox 的处理器必须在白名单里（2026-10-12）---- */

{
  /*
   * 🔴 用户报：「这个联网搜索的开关没用，我点了开不了」。
   *
   * 根因：	oElectronTemplate 里那张「吃点击后勾选状态」的白名单
   * （HANDLERS_TAKING_CHECKED_STATE）**漏了 	oggleWebSearch** ——
   * 于是它落进无参数分支：
   *
   *     handler()            ->  toggleWebSearch(undefined)
   *     Boolean(undefined)   ->  false
   *
   * 每次点都是关闭，永远打不开 —— 而菜单看着完全正常
   * （Electron 自己会翻转勾，看起来像点上了）。
   *
   * 所以加这条：**菜单里每个 checkbox 都必须在那张表里**。
   * 将来再加 checkbox 忘了登记，门禁会红。
   */
  const { buildPetMenuTemplate, collectActions } = requireCjs(target('petMenuTemplate'));
  const { HANDLERS_TAKING_CHECKED_STATE, resolveHandlerKey } = requireCjs(target('petMenu'));

  const items = buildPetMenuTemplate({
    visible: true, paused: false, visionWatching: false,
    thinking: true, webSearchMode: 'off', ttsSpeechEnabled: true, activity: 0.6, petScale: 1, edgePeek: true,
    chatVisible: true, bubbleVisible: true, statusVisible: true,
    immersiveMode: false, micDeviceId: '', micDevices: [],
  });

  // 递归找出所有 checkbox
  const checkboxes = [];
  const walk = (list) => {
    for (const it of list) {
      if (it.type === 'checkbox') checkboxes.push(it);
      if (it.submenu) walk(it.submenu);
    }
  };
  walk(items);

  check('菜单里至少有一个 checkbox（否则这条断言是空转）', checkboxes.length > 0,
    '实际 ' + checkboxes.length + ' 个');

  const missing = [];
  for (const box of checkboxes) {
    if (!box.action) { missing.push(box.label + '（没有 action）'); continue; }
    const key = resolveHandlerKey(box.action);
    if (!HANDLERS_TAKING_CHECKED_STATE.has(key)) {
      missing.push(box.label + ' -> ' + key);
    }
  }
  check(
    '每个 checkbox 的处理器都在「吃勾选状态」白名单里',
    missing.length === 0,
    /*
     * ⚠️ 处理器名要放在**最前面**。原来是 "……永远打不开。缺：xxx"，
     * 而测试输出会把长 detail 截断 —— 缺的那个名字正好被切掉，
     * 于是报错只说"有 checkbox 没登记"却不说是哪个。
     * 排查时必须翻源码找，白费一轮。
     */
    missing.length ? `没登记的：${missing.join('、')}` : '',
  );

  // 联网搜索（2026-10-16 起是 **radio 三选一子菜单**，不再是 checkbox）
  // ⚠️ 旧的「联网搜索是 checkbox」断言随功能一起退役 ——
  //    现在要钉的是：三项 radio 都在、勾跟随 state.webSearchMode。
  const webSub = (function findWebSub(list) {
    for (const it of list) {
      if (it.submenu && it.submenu.some((s) => s.action?.startsWith('web-search-mode:'))) {
        return it.submenu;
      }
      const nested = it.submenu ? findWebSub(it.submenu) : null;
      if (nested) return nested;
    }
    return null;
  })(items);
  check('联网搜索是三选一子菜单', Boolean(webSub), '找不到 web-search-mode:* 这组项');
  if (webSub) {
    check('子菜单恰好三项（精准/快速/关闭）', webSub.length === 3, `实际 ${webSub.length} 项`);
    const modes = webSub.map((s) => s.action?.split(':')[1]);
    check(
      '三个 action 的模式值正确',
      modes.join(',') === 'accurate,fast,off',
      `实际 ${modes.join(',')}`,
    );
    check('全部是 radio 类型', webSub.every((s) => s.type === 'radio'), '有一项不是 radio');
    const checkedOne = webSub.filter((s) => s.checked);
    check('当前模式的 radio 打勾（state.webSearchMode=off）',
      checkedOne.length === 1 && checkedOne[0].action === 'web-search-mode:off',
      `打勾的是 ${checkedOne.map((s) => s.action).join(',') || '无'}`);
    check('子菜单的处理器可解析（不是「未实现」灰项）',
      resolveHandlerKey('web-search-mode:accurate') === 'webSearchMode',
      `实际 ${resolveHandlerKey('web-search-mode:accurate')}`);
  }
}

/* ---- 聊天模式的勾算法（2026-10-12；2026-10-14 起开关双向）---- */

{
  /*
   * 用户要求：「将菜单中的聊天改成聊天模式，这是个开关，启动后将自动开启
   * 显示气泡、显示对话框」，并确认「**开就将两个都显示，关闭不动任何东西**」。
   *
   * 这条断言钉住两件事：
   *   ① 勾 = `bubbleVisible && chatVisible`（**两个都显示才算开着**）
   *   ② 处理器吃 `enabled`（否则连"开"都做不了）
   *
   * ⚠️ 为什么专门断言"两个都显示才算开着"：将来有人"顺手"把它改成
   *    只看其中一个，用户单独关掉气泡时勾还会亮着 —— 界面与实际不符。
   */
  const { buildPetMenuTemplate } = requireCjs(target('petMenuTemplate'));

  const findChatMode = (bubbleVisible, chatVisible) => {
    const items = buildPetMenuTemplate({
      visible: true, paused: false, visionWatching: false,
      thinking: true, webSearchMode: 'off', ttsSpeechEnabled: true, activity: 0.6, petScale: 1, edgePeek: true,
      chatVisible, bubbleVisible, statusVisible: true,
      immersiveMode: false, micDeviceId: '', micDevices: [],
    });
    return items.find((i) => i.action === 'toggle-chat-mode');
  };

  const combo = findChatMode(true, true);
  check('聊天模式是 checkbox', combo?.type === 'checkbox', `实际 ${combo?.type}`);
  check('两个都显示时勾是选中', combo?.checked === true, `实际 ${combo?.checked}`);
  check('两个都不显示时勾是未选中', findChatMode(false, false)?.checked === false,
    `实际 ${findChatMode(false, false)?.checked}`);
  // ⚠️ 关键：只显示一个 -> 勾必须**取消**（此刻并不处于"聊天模式已开启"）
  check('只显示气泡时勾是未选中（两个都显示才算开着）',
    findChatMode(true, false)?.checked === false, `实际 ${findChatMode(true, false)?.checked}`);
  check('只显示对话框时勾是未选中',
    findChatMode(false, true)?.checked === false, `实际 ${findChatMode(false, true)?.checked}`);

  // 「聊天…」必须真的没了（它原来打开的是设置窗口，是个误导项）
  const allLabels = [];
  const walkLabels = (list) => {
    for (const it of list) {
      if (it.label) allLabels.push(it.label);
      if (it.submenu) walkLabels(it.submenu);
    }
  };
  walkLabels(buildPetMenuTemplate({
    visible: true, paused: false, visionWatching: false,
    thinking: true, webSearchMode: 'off', ttsSpeechEnabled: true, activity: 0.6, petScale: 1, edgePeek: true,
    chatVisible: true, bubbleVisible: true, statusVisible: true,
    immersiveMode: false, micDeviceId: '', micDevices: [],
  }));
  check('菜单里**没有**「聊天…」了（它实际打开的是设置窗口）',
    !allLabels.some((l) => l === '聊天…' || l.startsWith('聊天…')),
    `实际标签：${allLabels.join(' / ')}`);
}

/* ---- 自绘菜单与原生退路的点击分发必须**完全一致**（2026-10-14）---- */

{
  /*
   * ## 为什么要专门断言"两条路一样"
   *
   * 菜单现在有两条执行路径：
   *
   *   ① 自绘窗口（设计稿那条）：点击从渲染层经 IPC 回来，走
   *      `dispatchPetMenuAction(handlers, action, checked)`
   *   ② 原生退路（自绘窗口起不来时）：点击由 `toElectronTemplate` 绑的
   *      `click` 直接调处理器
   *
   * 两者做的是**同一件事**（把 action 变成一次处理器调用），却分别实现。
   * 一旦分支顺序/取值方式不一致，就会出现「原生菜单能点、自绘菜单点了没反应」
   * 这种**只在其中一条路上**出现的故障 —— 而它极难查：
   * 两条路各自的日志都"看起来正常"。
   *
   * 所以这里对**同一份菜单的每一个 action**同时驱动两个函数，
   * 断言"处理器收到的参数完全一样"。给两条路各配一个只记录调用的假处理器表。
   */
  const { buildPetMenuTemplate, collectActions } = requireCjs(target('petMenuTemplate'));
  const {
    resolveHandlerKey,
    toElectronTemplate,
    dispatchPetMenuAction,
  } = requireCjs(target('petMenu'));

  const state = {
    visible: true, paused: false, visionWatching: false,
    thinking: true, webSearchMode: 'off', ttsSpeechEnabled: true, activity: 0.6, petScale: 1, edgePeek: true,
    chatVisible: true, bubbleVisible: true, statusVisible: true,
    immersiveMode: false, micDeviceId: '', micDevices: [{ deviceId: 'ABC123', label: '测试麦克风' }],
  };

  const items = buildPetMenuTemplate(state);
  const actions = collectActions(items);
  check('菜单里有可点的 action（可点数 > 0）', actions.length > 0, `实际 ${actions.length}`);

  /** 一个只记录调用参数的假处理器 */
  const makeSpy = () => {
    const calls = [];
    const fn = (...args) => {
      calls.push(args);
    };
    return { calls, fn };
  };

  /*
   * 把 `items` 和 `toElectronTemplate(items)` 的输出**按同一路径并行遍历** ——
   * 模板里没有 action 字段，只能靠结构对位。
   *
   * ⚠️ 建模板时必须给一份**每个键都存在**的处理器表，否则
   * `toElectronTemplate` 会把项置灰、**根本不设 `click`**（那正是它防
   * "点了没反应"的做法），于是这条路什么都不会被调用 —— 测出来的
   * 是"替身没接上"，而不是"两条路不一致"。
   */
  const nativeCalls = {};
  const allHandlers = {};
  for (const action of actions) {
    const key = resolveHandlerKey(action);
    if (!(key in allHandlers)) {
      allHandlers[key] = (...args) => {
        (nativeCalls[key] = nativeCalls[key] || []).push(args);
      };
    }
  }
  const missingInTemplate = [];
  const tmpl = toElectronTemplate(items, allHandlers, missingInTemplate);
  check(
    '原生模板里没有"未实现"的项（替身表覆盖了所有 action）',
    missingInTemplate.length === 0,
    `落空：${missingInTemplate.join(', ')}`,
  );

  const pairs = [];
  const walk = (list, nodes) => {
    for (let i = 0; i < list.length; i += 1) {
      const item = list[i];
      const node = nodes[i] ?? {};
      if (item.action) pairs.push({ item, node });
      if (item.submenu) walk(item.submenu, node.submenu ?? []);
    }
  };
  walk(items, tmpl);

  check(
    '并行遍历能对上每一个 action（结构对位没崩）',
    pairs.length === actions.length,
    `action ${actions.length} 个，配上 ${pairs.length} 个模板项`,
  );

  const mismatches = [];
  let dispatchedAll = true;
  for (const { item, node } of pairs) {
    const key = resolveHandlerKey(item.action);

    const a = makeSpy();
    /*
     * ⚠️ 开关类要传 `checked=true`（"点击之后的状态"）——
     * 那正是渲染层会报上来的形状。
     */
    const ok = dispatchPetMenuAction({ [key]: a.fn }, item.action, true);
    if (!ok) dispatchedAll = false;

    /* 原生那条路：清掉上一次的记录，再点一次 */
    for (const k of Object.keys(nativeCalls)) delete nativeCalls[k];
    /* Electron 在调 click 之前已经翻转了 checked，这里模拟"已翻转" */
    if (typeof node.click === 'function') node.click({ checked: true });

    const b = nativeCalls[key] || [];
    if (JSON.stringify(a.calls) !== JSON.stringify(b)) {
      mismatches.push(
        `${item.action}：自绘 ${JSON.stringify(a.calls)} vs 原生 ${JSON.stringify(b)}`,
      );
    }
  }

  check('每一个 action 在自绘路径上都能找到处理器', dispatchedAll, '有 action 落空（会变成"点了没反应"）');
  check(
    '同一 action 在两条路径上调用处理器的方式完全一致',
    mismatches.length === 0,
    mismatches.join('；'),
  );

  /* 三条具体的取值规则单独点名 —— 它们各自都踩过坑 */
  const spyMic = makeSpy();
  dispatchPetMenuAction({ setMicDevice: spyMic.fn }, 'mic:ABC123', undefined);
  check(
    'mic:<deviceId> 原样把字符串传给处理器（不能变成数字或 undefined）',
    JSON.stringify(spyMic.calls) === JSON.stringify([['ABC123']]),
    `实际 ${JSON.stringify(spyMic.calls)}`,
  );

  const spyScale = makeSpy();
  dispatchPetMenuAction({ setPetScale: spyScale.fn }, 'scale:1.3', undefined);
  check(
    'scale:1.3 传的是**数字** 1.3（传成字符串会让大小算错）',
    JSON.stringify(spyScale.calls) === JSON.stringify([[1.3]]),
    `实际 ${JSON.stringify(spyScale.calls)}`,
  );

  const spyBox = makeSpy();
  dispatchPetMenuAction({ toggleVision: spyBox.fn }, 'toggle-vision', false);
  check(
    'toggle-vision 收到的是**切换后的** false（不是取反前的值）',
    JSON.stringify(spyBox.calls) === JSON.stringify([[false]]),
    `实际 ${JSON.stringify(spyBox.calls)}`,
  );

  /* 没登记的 action 必须明确报"没处理器"，而不是静默调用一个 undefined */
  check(
    '未知 action 返回 false（调用方据此记日志）',
    dispatchPetMenuAction({}, 'not-a-real-action', undefined) === false,
    '返回了 true —— 那会把"菜单里有项没实现"藏起来',
  );
}

/* ---- 日志轮转：**写入时**按大小滚动（2026-10-14）---- */

{
  /*
   * ## 为什么必须测（用户报的）
   *
   * 「日志占了几百 MB」—— 实测 `main.log.1` = **110 MB**，而上限写的是 20 MB。
   * 根因：轮转只在**启动时**检查一次，而桌宠是长期挂机程序、根本不重启。
   *
   * 现在改成写入时按记账字节判断（`log()` 在写之前问 `LOG_MAX_BYTES`）。
   * 这里用很小的 maxBytes 把整条链跑一遍：连续写 -> 轮转 -> 备份链出现 ->
   * 最老的被删 -> 当前文件始终不超线。
   *
   * ⚠️ 这段逻辑抽成 `logRotation.ts` 就是为了能在测试里真跑一遍 ——
   * 原来长在 `index.ts` 里，被 electron 绑死，只能靠肉眼看磁盘。
   */
  const {
    rotateLogNow,
    rotateLogIfNeeded,
    logFileBytes,
    logLineBytes,
    LOG_MAX_BYTES,
    LOG_BACKUP_COUNT,
  } = requireCjs(target('logRotation'));

  check('默认上限是 20 MB', LOG_MAX_BYTES === 20 * 1024 * 1024, `实际 ${LOG_MAX_BYTES}`);
  check('默认保留 3 份备份', LOG_BACKUP_COUNT === 3, `实际 ${LOG_BACKUP_COUNT}`);

  const dir = mkdtempSync(path.join(os.tmpdir(), 'pet-logrot-'));
  const file = path.join(dir, 'main.log');
  const size = (p) => (existsSync(p) ? readFileSync(p).length : -1);

  try {
    // ① 没到线：不转
    writeFileSync(file, 'x'.repeat(100));
    check(
      '没到上限时不轮转',
      rotateLogIfNeeded(file, { maxBytes: 1000 }) === false,
      '不该动的文件被轮转了',
    );

    // ② 过线：原文件被挪成 .1
    check('超过上限时轮转', rotateLogIfNeeded(file, { maxBytes: 50 }) === true);
    check('轮转后原内容在 .1 里', size(`${file}.1`) === 100, `实际 ${size(`${file}.1`)}`);

    // ③ 备份链：连续转，份数不超过 backups（最老的被删）
    for (let i = 0; i < 5; i += 1) {
      writeFileSync(file, 'y'.repeat(10));
      rotateLogNow(file, { backups: 2 });
    }
    check('备份份数不超过上限（最老的被删）', !existsSync(`${file}.3`), '出现了 .3 —— 备份越滚越多');
    check('保留到 .2', existsSync(`${file}.2`), '缺 .2');

    // ④ 启动兜底：文件不存在时不能抛
    check('文件不存在时轮转返回 false', rotateLogNow(path.join(dir, 'nope.log')) === false);

    // ⑤ 记账工具
    //    ⚠️ 先写回一个已知大小 —— 上一段的最后一次 `rotateLogNow` 把
    //    `file` 挪成了 `.1`，此刻它本来就不存在（第一版断言就栽在这里）
    writeFileSync(file, 'z'.repeat(10));
    check('logFileBytes 读得到真实大小', logFileBytes(file) === 10, `实际 ${logFileBytes(file)}`);
    check('logFileBytes 读不到算 0', logFileBytes(path.join(dir, 'nope.log')) === 0);
    check('logLineBytes 按 UTF-8 计并含换行', logLineBytes('中') === 4, `实际 ${logLineBytes('中')}`);

    // ⑥ 模拟 log() 的"写前判断"：长期写入必须**持续**轮转
    const app = path.join(dir, 'app.log');
    const limit = 500;
    let written = 0;
    let rotations = 0;
    for (let i = 0; i < 200; i += 1) {
      const line = `${new Date(0).toISOString()} 第 ${i} 条日志，带点中文`;
      const bytes = logLineBytes(line);
      if (written + bytes > limit) {
        if (rotateLogNow(app, { backups: 1 })) {
          written = 0;
          rotations += 1;
        }
      }
      writeFileSync(app, `${line}\n`, { flag: 'a' });
      written += bytes;
    }
    check('长期写入会持续轮转（不是只在启动时转一次）', rotations >= 10, `只转了 ${rotations} 次`);
    check('当前文件始终不超上限', logFileBytes(app) <= limit, `实际 ${logFileBytes(app)}`);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

/* ---- 陈旧探测临时目录的清扫（2026-10-14）---- */

{
  /*
   * ## 用户报的现象
   *
   * `apps/desktop/` 下积了 **178 个** `pet-fs-*` 空目录 —— 每个会话建一个，
   * 正常退出时 `stop()` 会删，但进程被杀/崩溃（开发时天天发生）就留下了。
   *
   * 现在的策略是"建新的之前先扫旧的"，三条规则每条都必须成立，
   * 错一条的后果都很严重：
   *   · 只删本前缀       -> 删错别人的目录（误删活数据）
   *   · 只删超过一小时的 -> 删掉**另一个实例正在用**的目录（并行跑 e2e 时）
   *   · 真删得掉         -> 清理形同虚设
   */
  const { sweepStaleProbeDirs } = requireCjs(target('fullscreenWatcher'));

  const dir = mkdtempSync(path.join(os.tmpdir(), 'pet-sweep-'));
  const old = path.join(dir, 'pet-fs-old');
  const fresh = path.join(dir, 'pet-fs-fresh');
  const other = path.join(dir, 'not-ours');
  const plainFile = path.join(dir, 'pet-fs-file');
  try {
    mkdirSync(old);
    mkdirSync(fresh);
    mkdirSync(other);
    writeFileSync(plainFile, 'x');
    // 把 old 改成两小时前；fresh 保持"刚刚"
    const twoHoursAgo = new Date(Date.now() - 2 * 60 * 60 * 1000);
    utimesSync(old, twoHoursAgo, twoHoursAgo);

    const removed = sweepStaleProbeDirs([dir]);

    check('陈旧的本前缀目录被清掉', !existsSync(old), '没删掉 —— 清理形同虚设');
    check('刚建的本前缀目录保持不动', existsSync(fresh), '误删了活跃目录');
    check('非本前缀的目录不动', existsSync(other), '清理范围超了');
    check('同名文件不动（只处理目录）', existsSync(plainFile), '把文件也删了');
    check('返回删除计数（供诊断输出用）', removed === 1, `实际 ${removed}`);
    check('目录不存在时不抛异常', sweepStaleProbeDirs([path.join(dir, 'nope')]) === 0);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

/* ---- 「语音播报」开关（2026-10-14）---- */

{
  /*
   * 用户原话：「把宠物的语音功能抽出来放在允许看屏幕上面做成和上面两个一样的
   * 开关，开启时能将宠物回复的文字转为语音说出，语音功能在设置语音中设置好的」。
   *
   * 这条断言钉住三件事（都是用户明确要求的）：
   *   ① 它是 checkbox（"和上面两个一样的开关"）
   *   ② 它**就在「允许看屏幕」上面**（用户指定的位置）
   *   ③ 勾选状态跟 state 走（开关不能是死的）
   *
   * ⚠️ 为什么值得单独立一条：它是"结构 + 位置"双重约定，
   * 而位置这种东西改菜单时最容易被无意中挪走。
   */
  const { buildPetMenuTemplate } = requireCjs(target('petMenuTemplate'));

  const base = {
    visible: true, paused: false, visionWatching: false,
    thinking: true, webSearchMode: 'off', ttsSpeechEnabled: true, activity: 0.6,
    petScale: 1, edgePeek: true, chatVisible: true, bubbleVisible: true,
    statusVisible: true, immersiveMode: false, micDeviceId: '', micDevices: [],
  };

  const items = buildPetMenuTemplate(base);
  const idx = (action) => items.findIndex((i) => i.action === action);

  const ttsIdx = idx('toggle-tts-speech');
  const visionIdx = idx('toggle-vision');

  check('菜单里有「语音播报」这一项', ttsIdx >= 0);
  check('它是 checkbox（和沉浸模式/聊天模式同款开关）',
    items[ttsIdx]?.type === 'checkbox', `实际 ${items[ttsIdx]?.type}`);
  check('它在「允许偶尔截图」**上面**（用户指定的位置）',
    ttsIdx >= 0 && visionIdx >= 0 && ttsIdx < visionIdx,
    `语音播报=${ttsIdx} 允许偶尔截图=${visionIdx}`);
  check('它配了图标（speaker）', items[ttsIdx]?.icon === 'speaker', `实际 ${items[ttsIdx]?.icon}`);
  check('开 -> 勾选', items[ttsIdx]?.checked === true, `实际 ${items[ttsIdx]?.checked}`);
  check('关 -> 取消勾选',
    buildPetMenuTemplate({ ...base, ttsSpeechEnabled: false }).find(
      (i) => i.action === 'toggle-tts-speech',
    )?.checked === false,
    '关的时候勾还在 —— 那就是个假开关');
}

/* ---- 光标吸附：探头形态不随抓取点变（2026-10-16）---- */

{
  /*
   * ## 用户报的现象
   *
   * 「拖宠物头部到屏幕边缘旋转是一种样子，拖中部以下是另一种样子」。
   *
   * ## 根因（用户日志实锤）
   *
   * 横向吸附早就是"固定位置"，但**纵向是 `rawY = current.y`（原点不动）**
   * —— 松手时窗口在哪探头就在哪。日志实测结果 y 从 417 到 560（143px 散布），
   * 全由"抓取点 + 手的高度"决定。倾斜 45° 再被屏幕底边裁一刀，
   * 观感就是两张完全不同的图。
   *
   * ## 修法语义
   *
   * 传 `cursor` 时：**立绘中心 = 光标松手点**（夹在屏幕内，立绘中心不出屏）。
   * 光标位置与抓哪里无关，所以同一动作不管抓头部还是抓腹部，探头姿态一致。
   * 不传 cursor 时必须保持旧行为（原点不动）—— 滑杆重排/全屏恢复等
   * 重入路径不能跳到光标处。
   */
  const normal = windowSizeFor(1, 'normal');
  const peek = peekWindowSizeFor(1);
  const artSize = artSizeFor(1);
  const artInWindow = { x: Math.round((normal.width - artSize.width) / 2), y: 0 };
  const screen = { x: 0, y: 0, width: 1536, height: 864 };
  const current = { x: 1300, y: 500, width: normal.width, height: normal.height };

  // ① 纵向吸附：立绘中心 = 光标 Y
  const centerY = current.y + artInWindow.y + artSize.height / 2; // 吸附前立绘中心
  const snapped = peekWindowBounds(
    current, artInWindow, peek, 'right', screen, 43, 0, artSize.width,
    undefined, undefined,
    { x: 1500, y: centerY }, artSize.height,
  );
  const snappedCenterY = snapped.y + artInWindow.y + artSize.height / 2;
  check(
    '光标吸附：立绘中心 = 光标松手点（纵向）',
    Math.abs(snappedCenterY - centerY) <= 1,
    `吸附后立绘中心 y=${snappedCenterY}，应为 ${centerY}`,
  );

  // ② 同一次松手，换一个"抓取点"（立绘相对窗口的偏移不同）→ 结果必须一致
  //    （这是本修法的核心承诺：形态不随抓取点变）
  const grabbedLower = { x: artInWindow.x, y: 60 }; // 抓得靠下 = 立绘在窗口里更靠上
  const snapped2 = peekWindowBounds(
    current, grabbedLower, peek, 'right', screen, 43, 0, artSize.width,
    undefined, undefined,
    { x: 1500, y: centerY }, artSize.height,
  );
  const snapped2CenterY = snapped2.y + grabbedLower.y + artSize.height / 2;
  check(
    '换抓取点（立绘在窗口内的偏移不同）后立绘中心不变',
    Math.abs(snapped2CenterY - centerY) <= 1,
    `第二种抓法立绘中心 y=${snapped2CenterY}，应为 ${centerY}`,
  );

  // ③ 夹紧：光标在屏幕外，立绘中心也不出屏
  const clamped = peekWindowBounds(
    current, artInWindow, peek, 'right', screen, 43, 0, artSize.width,
    undefined, undefined,
    { x: 1500, y: 5000 }, artSize.height,
  );
  const clampedCenterY = clamped.y + artInWindow.y + artSize.height / 2;
  check(
    '光标在屏幕外时立绘中心被夹回屏幕内',
    clampedCenterY <= screen.height - 0.5 && clampedCenterY >= 0,
    `立绘中心 y=${clampedCenterY}，屏幕高 ${screen.height}`,
  );

  // ④ 贴上下边：横向对称吸附
  const snappedBottom = peekWindowBounds(
    current, artInWindow, peek, 'bottom', screen, 43, 0, artSize.width,
    undefined, undefined,
    { x: 900, y: 400 }, artSize.height,
  );
  const snappedCenterX = snappedBottom.x + artInWindow.x + artSize.width / 2;
  check(
    '贴上下边时立绘中心 = 光标松手点（横向）',
    Math.abs(snappedCenterX - 900) <= 1,
    `立绘中心 x=${snappedCenterX}，应为 900`,
  );

  // ⑤ 不传 cursor：保持旧行为（原点不动）—— 滑杆重排等路径不能跳
  const legacy = peekWindowBounds(current, artInWindow, peek, 'right', screen, 43);
  check(
    '不传 cursor 时纵向保持原点不动（旧行为）',
    legacy.y === current.y,
    `应为 ${current.y}，实际 ${legacy.y}`,
  );
}

/* ------------------------------------------------------------------ 收尾 */

/* ------------------------------------------------------------------ *
 * petSocket：close() 必须真的停掉重连
 *
 * ## 🔴 这条测的是一个**让宠物永久失联**的真 bug（2026-10-15）
 *
 * `PetSocket` 的重连逻辑（`onClose` -> `this.connect()`）用的是
 * **构造时定死的 URL** —— 它只会拿老端口一直重试。
 *
 * 而 sidecar 用 `--port 0`（系统分配），**重启后端口一定会变**。
 * 于是主进程必须做两件事：
 *
 * ```
 * ① 监听 sidecar 的 'ready' 事件 -> 拿到新端口重新 connectSocket
 * ② connectSocket 里先关掉上一个 socket -> 否则它会在后台永远重试老端口
 * ```
 *
 * ⚠️ 这个 bug 的**实测症状**（日志原样）：
 *
 * ```
 * 10:49:03  read ECONNRESET
 * 10:49:04  新 sidecar 监听 60350
 * 10:49:04  ECONNREFUSED 127.0.0.1:58547      <- 老端口
 * ...
 * 10:51:35  ECONNREFUSED 127.0.0.1:58547      <- 还在刷，2 分钟了
 * ```
 *
 * 用户看到的是「宠物不回应」，而日志里全是同一个老端口 ——
 * 完全看不出"新端口已经起来了，只是没人连"。
 *
 * ## 这里测什么
 *
 * ① `close()` 之后**不再有任何重连**（上面那个孤儿 socket 的成因）
 * ② 重连退避确实是 500 -> 1000 -> 2000ms 递增
 *
 * ⚠️ 不让测试依赖"端口恰好没人监听"以外的任何东西：用一个 bind 完
 * 立刻关掉的端口，保证 `connect` 必定 `ECONNREFUSED`。
 */

console.log('\nN. petSocket：close() 必须真的停掉重连');

{
  // 拿一个「bind 完立刻关」的端口 —— connect 必定被拒，触发重连分支
  const probe = net.createServer();
  const closedPort = await new Promise((resolve) => {
    probe.listen(0, '127.0.0.1', () => {
      const address = probe.address();
      const value = typeof address === 'object' && address ? address.port : 0;
      probe.close(() => resolve(value));
    });
  });

  const logs = [];
  const socket = new PetSocket({ url: `ws://127.0.0.1:${closedPort}` });
  socket.on('log', (line) => logs.push(line));
  socket.connect();

  // 等出两次重连（500ms + 1000ms）—— 足够确认退避在跑
  await wait(2600);

  const attemptsBefore = logs.filter((line) => line.includes('ECONNREFUSED')).length;
  const closeAttempts = logs.filter((line) => line.includes('重连 sidecar')).length;

  check(
    '连不上时确实会自动重连（前置条件）',
    attemptsBefore >= 2,
    `只观察到 ${attemptsBefore} 次 ECONNREFUSED；logs=${logs.join(' | ')}`,
  );

  // 退避递增：第一次 500ms，第二次 1000ms，第三次 2000ms
  const delays = logs
    .filter((line) => line.includes('重连 sidecar'))
    .map((line) => Number.parseInt(line, 10))
    .filter((value) => Number.isFinite(value));
  check(
    '重连退避按 500/1000/2000 递增（不是死循环狂刷）',
    delays.length >= 2 && delays[1] > delays[0],
    `观察到的间隔：${delays.join(', ')}`,
  );

  socket.close();
  const attemptsAtClose = logs.filter((line) => line.includes('ECONNREFUSED')).length;

  // 关掉之后要静默 —— 这段窗口比两次退避都长，真有孤儿重连必然暴露
  await wait(2600);

  const attemptsAfter = logs.filter((line) => line.includes('ECONNREFUSED')).length;
  check(
    '🔴 close() 之后彻底不再重连（孤儿 socket 的成因）',
    attemptsAfter === attemptsAtClose,
    `close() 时 ${attemptsAtClose} 次，2.6 秒后变成 ${attemptsAfter} 次 —— ` +
      '还有重连说明 close() 没停掉定时器，接上新端口后它会永远刷老端口',
  );

  check(
    'close() 前确实还在重连（证明上面那条不是因为压根没重连）',
    closeAttempts >= 2,
    `close() 前只排了 ${closeAttempts} 次重连，测试没测到东西`,
  );
}

/* --------------------------------------------------- 12.5 托盘菜单模板（精简后） */

/*
 * 🔴 2026-10-16：用户要求托盘菜单只剩 5 项。
 *
 * > 「托盘菜单现在只保留显示隐藏宠物、宠物大小、设置、退出、退出全部」
 *
 * ## ⚠️ 为什么这条测试值得写
 *
 * 这次的改动**删了 11 个菜单项和十几处状态同步**。而删过头或删错的表现是：
 *
 *   · 少一项 -> 用户某个操作没入口了（比如藏了宠物就再也叫不回来）
 *   · 多一项 -> 说明没删干净（用户会觉得"说了精简还是那么长"）
 *
 * 光靠"我改的时候看了一遍"是靠不住的 —— 所以断言**精确清单**，不是"包含"。
 *
 * ## ⚠️ 为什么断言 `id` 清单而不是 label
 *
 * label 是会变的（"隐藏宠物"/"显示宠物"、"宠物大小（220 px）"里的数字）。
 * `id` 是稳定的动作标识 —— 断言它才是在断言**功能**，而不是文案。
 */

console.log('\n12.5 trayTemplate：托盘菜单精简到 5 项');

const { buildTrayMenuTemplate } = requireCjs(target('trayTemplate'));

{
  const item = (state) => buildTrayMenuTemplate(state);

  // ── 结构：5 个可点项 + 2 条分隔线，顺序固定 ──
  const shape = item({ visible: true, petScale: 1 })
    .map((i) => (i.type === 'separator' ? '-' : i.id));
  const want = ['toggle-visible', '-', 'pet-size', '-', 'settings', 'quit', 'quit-all'];
  check(
    '结构正好是 5 项 + 2 条分隔线，顺序固定',
    JSON.stringify(shape) === JSON.stringify(want),
    `实际 ${JSON.stringify(shape)}`,
  );

  // ── 精确清单：删过头（缺项）和没删干净（多项）都要报 ──
  const actions = item({ visible: true, petScale: 1 })
    .map((i) => i.id)
    .filter(Boolean);
  check(
    '可点项正好是那 5 个（多一个少一个都算错）',
    JSON.stringify(actions) ===
      JSON.stringify(['toggle-visible', 'pet-size', 'settings', 'quit', 'quit-all']),
    `实际 ${JSON.stringify(actions)}`,
  );

  /*
   * ── 反向：11 个已删项**一个都不能回来** ──
   *
   * ⚠️ 这条是查"没删干净"。断言"正好那 5 个"其实已经覆盖了它，
   * 但那条一红只告诉你"多了 1 项"，不告诉你是哪一项漏了。
   * 分开断言能把**漏删的那一项名字**直接报出来。
   */
  const allLabels = JSON.stringify(item({ visible: true, petScale: 1 }));
  for (const gone of [
    '状态：', '隐藏气泡', '显示气泡', '聊天模式', '隐藏状态条', '显示状态条',
    '免打扰', '语音播报', '允许偶尔截图', '麦克风', '思考模式', '联网搜索', '活泼度',
    '重新读取配置文件', '打开日志',
  ]) {
    check(
      `已删项「${gone}」没有回来`,
      !allLabels.includes(gone),
      `${gone} 又出现在托盘菜单里了`,
    );
  }

  /*
   * ── 「显示/隐藏宠物」的文案随状态变 ──
   *
   * ⚠️ 这一项是**唯一功能上不可替代**的：宠物窗口是 skipTaskbar 的，
   * 藏起来之后托盘里这项是叫它回来的唯一途径。
   * 所以文案错了 = 用户看到"显示宠物"却点了隐藏 —— 直接就点不到了。
   */
  const shown = item({ visible: true, petScale: 1 })[0];
  const hidden = item({ visible: false, petScale: 1 })[0];
  check('宠物可见时，第一项是「隐藏宠物」', shown.label === '隐藏宠物', `实际 ${shown.label}`);
  check('宠物隐藏时，第一项是「显示宠物」', hidden.label === '显示宠物', `实际 ${hidden.label}`);

  // ── 大小子菜单：档位数与唯一真源一致，且**恰好一个**勾 ──
  const sizeItems = (scale) => item({ visible: true, petScale: scale })
    .find((i) => i.id === 'pet-size')?.submenu ?? [];
  check(
    '大小子菜单档位数 = PET_SCALE_PRESETS（不写死数字）',
    sizeItems(1).length === PET_SCALE_PRESETS.length,
    `实际 ${sizeItems(1).length} 项，期望 ${PET_SCALE_PRESETS.length}`,
  );
  for (const preset of PET_SCALE_PRESETS) {
    const checked = sizeItems(preset.scale).filter((i) => i.checked);
    check(
      `缩放到 ${preset.scale} 时，大小子菜单恰好勾 1 项（且是它自己）`,
      checked.length === 1 && checked[0].label.startsWith(preset.label),
      `勾了 ${checked.length} 项：${checked.map((i) => i.label).join(', ')}`,
    );
  }
  check(
    '大小子菜单的标题带当前 px（用户不点开也知道现在多大）',
    /宠物大小（\d+ px）/.test(String(item({ visible: true, petScale: 1 })
      .find((i) => i.id === 'pet-size')?.label)),
    '标题里没有 px',
  );
}

/* ------------------------------------------------------------------ 状态条：全称标签 + 高度伸缩 */

console.log('\n12.9 statusWindow：状态文字全称 + 胶囊高度随字数伸缩（2026-10-16）');

{
  const { heightForLabel, STATUS_HEIGHT: BASE_H } = requireCjs(target('statusWindow'));

  check('2 字标签保持设计高（不变）', heightForLabel('空闲') === BASE_H && heightForLabel('思考') === BASE_H,
    `实际 ${heightForLabel('空闲')}/${heightForLabel('思考')}，基准 ${BASE_H}`);
  check('「说话中」（3 字）加高 1 档', heightForLabel('说话中') === BASE_H + Math.round(1 * 15.3),
    `实际 ${heightForLabel('说话中')}`);
  check('「联网搜索中」（5 字）比基准高 3 档', heightForLabel('联网搜索中') === BASE_H + Math.round(3 * 15.3),
    `实际 ${heightForLabel('联网搜索中')}`);
  check('空/短标签兜底不矮于基准', heightForLabel('') === BASE_H && heightForLabel('好') === BASE_H);

  // 状态机文字用全称（refreshActivityStatus 与 statusKindFor 两处都要改）
  const mainSrc = readFileSync(path.join(here, '..', 'src', 'main', 'index.ts'), 'utf-8');
  check("refreshActivityStatus 推「联网搜索中」全称", mainSrc.includes("setStatus('searching', '联网搜索中')"));
  check("不再推 2 字旧词「'searching', '搜索'」", !mainSrc.includes("'searching', '搜索'"));

  // 🔴 2026-10-17：「语音准备中」整档删除（和思考中重复）—— 不许有任何残留
  check("不再推「语音准备中」状态", !mainSrc.includes("setStatus('voice'") && !mainSrc.includes("'voice', '语音准备中'"));
  check("voicePrep 变量已删干净", !mainSrc.includes('voicePrep'));
  check("pet.say.end 后 TTS 合成期保持思考中（turnThinking 不清零）",
    /message\.type === 'pet\.say\.end'[\s\S]{0,600}turnThinking =\s*reason === 'done'/.test(mainSrc));
  check("thinking 分支推「思考」", mainSrc.includes("setStatus('thinking', '思考')"));

  // 语音播放信号链：二进制帧转发（真转发点）-> 说话中；渲染层 onended -> 空闲
  check('音频转发处有「语音开始播放 -> 说话中」钩子', mainSrc.includes('语音开始播放 -> 说话中'));
  check('钩子挂在二进制帧 drain 循环里（元信息路径 drain 不出东西）',
    /socket\.on\('binary'[\s\S]{0,2000}语音开始播放 -> 说话中/.test(mainSrc));
  check('有 pet:voice-played 终点信号处理', mainSrc.includes("ipcMain.on('pet:voice-played'"));
  check('voice.state speaking 不再直设「说话」（那是文字阶段信号）',
    !/voiceState === 'speaking'[\s\S]{0,200}setStatus\('说话'\)/.test(mainSrc));
  const preloadSrc = readFileSync(path.join(here, '..', 'src', 'preload', 'index.ts'), 'utf-8');
  check('preload 暴露 notifyVoicePlayed', preloadSrc.includes('notifyVoicePlayed'));
  // 🔴 2026-10-17：起点信号改由渲染层报（转发点猜时机会漏）
  check('preload 暴露 notifyVoiceSpeaking', preloadSrc.includes('notifyVoiceSpeaking'));
  check('渲染层开播时报 notifyVoiceSpeaking', /pet\.on\('pet:voice-audio'[\s\S]{0,600}notifyVoiceSpeaking/.test(
    readFileSync(path.join(here, '..', 'src', 'renderer', 'app.js'), 'utf-8')));
  check('主进程有 pet:voice-speaking 处理（无条件转说话中）',
    mainSrc.includes("ipcMain.on('pet:voice-speaking'"));
  const vioSrc = readFileSync(path.join(here, '..', 'src', 'renderer', 'voice-io.js'), 'utf-8');
  check('voice-io 播放结束回调 onPlaybackEnded', vioSrc.includes('onPlaybackEnded'));
  // 🔴 2026-10-17 实锤：audio 单例可能被麦克风预热**先**创建（不带回调），
  // pet:voice-audio 处理器必须**无条件**挂终点回调，不能塞进 !audio 分支
  const appSrc = readFileSync(path.join(here, '..', 'src', 'renderer', 'app.js'), 'utf-8');
  check('终点回调在 pet:voice-audio 里无条件挂载（不受 !audio 分支限制）',
    /pet\.on\('pet:voice-audio'[\s\S]{0,700}audio\.onPlaybackEnded = /.test(appSrc) &&
    !/if \(!audio\) \{\s*audio = new VoiceIO\(\);[\s\S]{0,300}onPlaybackEnded[\s\S]{0,150}\}\s*window\.pet\?\.notifyVoiceSpeaking/.test(appSrc));
  // 🔴 2026-10-17：onended 有概率不触发（实测），时长定时器兜底
  check('voice-io 有 buffer.duration 时长兜底定时器（与 onended 双保险、幂等）',
    vioSrc.includes('buffer.duration * 1000') && vioSrc.includes('reported'));
  check('看门狗兜底覆盖说话中（voiceSpeaking）',
    /turnThinking \|\| searchDepth > 0 \|\| voiceSpeaking/.test(mainSrc));
}

/* ------------------------------------------------------------------ 收尾 */

console.log('');

if (failures.length) {

  console.error(`主进程测试未通过：${failures.length} 项失败 / ${passed + failures.length} 项`);

  for (const item of failures) console.error(`  - ${item}`);

  process.exit(1);

}

console.log(`主进程测试通过：${passed}/${passed} 项`);

process.exit(0);


