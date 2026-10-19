/**
 * 端到端联调（`npm run test:e2e`）—— 真实 sidecar + 真实客户端。
 *
 * 这是本仓库里**唯一一条真正跨进程的测试**：拉起真实的 Python sidecar 进程，
 * 用真实的 `petSocket.ts` 客户端连上去，再用真实的协议解析函数校验每一条消息。
 *
 * 为什么它不可替代：其余测试都是"某一端 + 打桩的另一端"，
 * 因此两侧**各自自洽但彼此不匹配**这类问题永远测不出来。而联调会把
 * 真实的字节流拿回来，逐条检查 Python 发的东西能不能被 TS 侧接受 ——
 * 这是在没有 GUI 的环境里能做到的最强验证。
 *
 * 依赖：Python 解释器（PET_PYTHON / DSH_PYTHON / .venv / PATH），
 * 以及 `npm run build:testtargets` 打好的产物。
 */

import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const nodeMajor = Number(process.versions.node.split('.')[0]);
if (Number.isFinite(nodeMajor) && nodeMajor < 20) {
  console.error(`需要 Node 20+，当前是 ${process.versions.node}。请用较新的 Node 运行。`);
  process.exit(1);
}

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopDir = path.join(here, '..');
const root = path.join(desktopDir, '..', '..');
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
  const file = path.join(buildDir, `${name}.cjs`);
  if (!existsSync(file)) {
    console.error(`缺少 ${name}.cjs，请先运行：npm run build:testtargets`);
    process.exit(1);
  }
  return file;
};

const requireCjs = createRequire(import.meta.url);
const { PetSocket } = requireCjs(target('petSocket'));

// 直接试各种位置找解释器（与 sidecar.ts 的策略一致，但这里是测试，要能报清楚）
function findPython() {
  const candidates = [
    process.env.PET_PYTHON,
    process.env.DSH_PYTHON,
    process.env.PYTHON,
    path.join(root, '.venv', 'Scripts', 'python.exe'),
    path.join(root, '.venv', 'bin', 'python3'),
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (candidate.includes(path.sep) && existsSync(candidate)) return candidate;
    if (!candidate.includes(path.sep)) return candidate;
  }
  return process.platform === 'win32' ? 'python.exe' : 'python3';
}

/* ------------------------------------------------------------------ 启动 sidecar */

function startSidecar(python) {
  const brainDir = path.join(root, 'apps', 'brain');
  const env = { ...process.env, PET_MOCK_STREAM_DELAY: '0', PYTHONUNBUFFERED: '1' };
  // 宿主可能注入 ELECTRON_RUN_AS_NODE，与 Python 无关但一并清理更干净
  delete env.ELECTRON_RUN_AS_NODE;

  const child = spawn(python, ['-m', 'pet', '--port', '0'], {
    cwd: brainDir,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });

  return new Promise((resolve, reject) => {
    let buffered = '';
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error('sidecar 启动超时（15s 内未就绪）'));
    }, 15_000);

    child.stdout.setEncoding('utf-8');
    child.stdout.on('data', (chunk) => {
      buffered += chunk;
      const match = /PET_READY (\{.*\})/.exec(buffered);
      if (!match) return;
      clearTimeout(timer);
      try {
        resolve({ child, info: JSON.parse(match[1]) });
      } catch (error) {
        reject(new Error(`无法解析 PET_READY：${error.message}`));
      }
    });
    child.stderr.setEncoding('utf-8');
    child.stderr.on('data', (chunk) => {
      if (process.env.PET_TEST_VERBOSE) process.stdout.write(`  [sidecar] ${chunk}`);
    });
    child.on('error', (error) => {
      clearTimeout(timer);
      reject(new Error(`无法启动 Python：${error.message}`));
    });
    child.on('exit', (code) => {
      if (buffered.includes('PET_READY')) return;
      clearTimeout(timer);
      reject(new Error(`sidecar 在就绪前退出（code=${code}）`));
    });
  });
}

/* ------------------------------------------------------------------ 主流程 */

const python = findPython();
console.log('端到端联调（真实 sidecar + 真实客户端）\n');

let sidecar = null;
let child = null;
let info = null;

/**
 * 支持两种模式，因为运行环境的能力不同：
 *   · 默认：自己拉起 sidecar 进程（普通终端里就是这条）；
 *   · `PET_E2E_PORT=8931`：连一个**已经在跑**的 sidecar。
 * 受限环境禁止 Node 起带管道的子进程（`spawn EPERM`），此时只能走后者 ——
 * 由外部先把 sidecar 跑起来，测试专心做协议往返的校验。
 */
const externalPort = Number(process.env.PET_E2E_PORT ?? 0);
if (externalPort > 0) {
  console.log(`Python：（使用外部 sidecar，端口 ${externalPort}）\n`);
  info = { port: externalPort, protocolVersion: 1, brain: '(external)' };
} else {
  console.log(`Python：${python}\n`);
  try {
    sidecar = await startSidecar(python);
    child = sidecar.child;
    info = sidecar.info;
  } catch (error) {
    console.error(`无法启动 sidecar：${error.message}`);
    console.error(
      '可改用外部 sidecar：先在另一个终端跑 `python -m pet --port 8931`，\n' +
        '然后设置 PET_E2E_PORT=8931 再运行本测试。',
    );
    process.exit(1);
  }
}
console.log(`1. sidecar 就绪`);
check('拿到可用端口', Number.isInteger(info.port) && info.port > 0, JSON.stringify(info));
if (externalPort > 0) {
  console.log('  （外部 sidecar 模式：跳过 READY 字段校验）');
} else {
  check('协议版本为 1', info.protocolVersion === 1, String(info.protocolVersion));
  check('报告了 brain 提供者', typeof info.brain === 'string' && info.brain.length > 0, info.brain);
}

/* ------------------------------------------------------------------ 连接与握手 */

console.log('\n2. 真实握手（手写的 WebSocket 客户端）');
const socket = new PetSocket({ url: `ws://127.0.0.1:${info.port}` });
const messages = [];
const logs = [];
socket.on('message', (m) => messages.push(m));
socket.on('log', (line) => logs.push(line));

const opened = new Promise((resolve) => socket.on('open', resolve));
socket.connect();
await Promise.race([opened, wait(5000)]);
check('与真实 sidecar 握手成功', socket.connected, logs.join(' | '));

socket.send({
  type: 'hello',
  protocolVersion: 1,
  client: 'e2e',
});
await wait(400);
const ready = messages.find((m) => m.type === 'ready');
check('收到 ready（说明 hello 被接受、协议版本匹配）', Boolean(ready), JSON.stringify(messages.map((m) => m.type)));
check('ready 带人格名', typeof ready?.persona === 'string' && ready.persona.length > 0);
check('ready 带活泼度', typeof ready?.activity?.overall === 'number', JSON.stringify(ready?.activity));
check('ready 带调度器状态', typeof ready?.autonomy?.threshold === 'number');
check('ready 带语音状态', typeof ready?.voice?.state === 'string', JSON.stringify(ready?.voice));
check('ready 带记忆状态', typeof ready?.memory?.items === 'number', JSON.stringify(ready?.memory));

/* ------------------------------------------------------------------ 打字对话 */

console.log('\n3. 真实流式对话（端到端字节往返）');
messages.length = 0;
socket.send({ type: 'user.text', text: '你好呀，你是谁' });
await wait(1500);

const kinds = messages.map((m) => m.type);
check('收到思考态', kinds.includes('pet.thinking'), kinds.join(','));
check('收到发言开始', kinds.includes('pet.say.start'), kinds.join(','));
const deltas = messages.filter((m) => m.type === 'pet.say.delta');
check('收到流式增量（真流式而非一次吐完）', deltas.length >= 1, `增量 ${deltas.length} 条`);
const end = messages.find((m) => m.type === 'pet.say.end');
check('收到发言结束', Boolean(end), kinds.join(','));
check('结束原因正常', end?.reason === 'done', String(end?.reason));
check(
  '**流式增量拼起来与最终文本一致**（这是前端显示正确性的基础）',
  deltas.map((m) => m.text).join('') === end?.text,
  `拼出 ${JSON.stringify(deltas.map((m) => m.text).join(''))} vs 最终 ${JSON.stringify(end?.text)}`,
);
check('收到情绪状态', kinds.includes('pet.emotion'), kinds.join(','));
check(
  '同一条发言的 id 始终一致',
  new Set(messages.filter((m) => m.id).map((m) => m.id)).size === 1,
  JSON.stringify([...new Set(messages.filter((m) => m.id).map((m) => m.id))]),
);

/* ------------------------------------------------------------------ 配置热更新 */

console.log('\n4. 真实配置热更新');
messages.length = 0;
socket.send({ type: 'config.update', patch: { 'activity.overall': 0.9 } });
await wait(500);
const ack = messages.find((m) => m.type === 'ready');
check('热更新被回报', Boolean(ack), messages.map((m) => m.type).join(','));
check('设定值生效', Math.abs((ack?.activity?.overall ?? 0) - 0.9) < 1e-6, String(ack?.activity?.overall));

messages.length = 0;
socket.send({ type: 'config.update', patch: { 'activity.overall': 42 } });
await wait(500);
const clamped = messages.find((m) => m.type === 'ready');
check('越界值被夹取后回报', clamped?.activity?.overall === 1.0, String(clamped?.activity?.overall));

/* ------------------------------------------------------------------ 非法输入 */

console.log('\n5. 非法输入被拒且连接不断');
messages.length = 0;
socket.send({ type: 'user.text', text: '   ' });
await wait(400);
const err = messages.find((m) => m.type === 'system.error');
check('非法消息得到可读错误', Boolean(err), messages.map((m) => m.type).join(','));
check('错误信息说明了原因', typeof err?.message === 'string' && err.message.length > 0, err?.message);

messages.length = 0;
socket.send({ type: 'user.text', text: '还在吗' });
await wait(1200);
check(
  '被拒之后连接仍可用（宠物照常回答）',
  messages.some((m) => m.type === 'pet.say.end'),
  messages.map((m) => m.type).join(','),
);

/* ------------------------------------------------------------------ 手动触发 */

console.log('\n6. 手动触发自主发言');
messages.length = 0;
socket.send({ type: 'user.poke' });
await wait(1200);
const pokeEnd = messages.find((m) => m.type === 'pet.say.end');
check('戳一下会让它主动说话', Boolean(pokeEnd), messages.map((m) => m.type).join(','));
check('自主发言内容非空', (pokeEnd?.text ?? '').length > 0, JSON.stringify(pokeEnd?.text));
const pokeStart = messages.find((m) => m.type === 'pet.say.start');
check('自主发言标记为低优先级（不抢占用户对话）', pokeStart?.priority === 'low', String(pokeStart?.priority));

/* ------------------------------------------------------------------ 免打扰 */

console.log('\n7. 免打扰真的拦得住');
messages.length = 0;
socket.send({ type: 'config.update', patch: { autonomy: { muted: true } } });
await wait(400);
const mutedAck = messages.find((m) => m.type === 'ready');
check('免打扰在 sidecar 生效', mutedAck?.autonomy?.muted === true, JSON.stringify(mutedAck?.autonomy));

messages.length = 0;
socket.send({ type: 'user.poke' });
await wait(900);
check(
  '免打扰期间戳它也不说话',
  !messages.some((m) => m.type === 'pet.say.start'),
  messages.map((m) => m.type).join(','),
);

socket.send({ type: 'config.update', patch: { autonomy: { muted: false } } });
await wait(400);
messages.length = 0;
socket.send({ type: 'user.poke' });
await wait(1200);
check(
  '解除免打扰后恢复',
  messages.some((m) => m.type === 'pet.say.end'),
  messages.map((m) => m.type).join(','),
);

/* ------------------------------------------------------------------ 记忆 */

console.log('\n8. 记忆清空（隐私底线能力）');
messages.length = 0;
socket.send({ type: 'memory.clear' });
await wait(400);
const cleared = messages.find((m) => m.type === 'memory.cleared');
check('收到清空回执', Boolean(cleared), messages.map((m) => m.type).join(','));
check('回执里记忆归零', cleared?.memory?.items === 0, JSON.stringify(cleared?.memory));

/* ------------------------------------------------------------------ 语音控制 */

console.log('\n9. 语音开关与二进制通道');
messages.length = 0;
socket.send({ type: 'voice.control', enabled: true });
await wait(400);
const voiceState = messages.find((m) => m.type === 'voice.state');
check('语音状态被回报', Boolean(voiceState), messages.map((m) => m.type).join(','));
check('状态为 listening（在听）', voiceState?.state === 'listening', String(voiceState?.state));

// 推一帧静音 PCM：不该崩，也不该产生识别结果
const silence = Buffer.alloc(640); // 20ms @16kHz 单声道 16bit
for (let i = 0; i < 20; i += 1) socket.sendBinary(silence);
await wait(400);
check(
  '推入静音音频不会触发误识别（环境噪声不该让宠物自言自语）',
  !messages.some((m) => m.type === 'user.voice'),
  messages.map((m) => m.type).join(','),
);

// 推一段"说话"音频（正弦波），应当被识别并触发对话
const speech = Buffer.alloc(32000); // 1s
for (let i = 0; i < 16000; i += 1) {
  speech.writeInt16LE(Math.round(Math.sin((2 * Math.PI * 200 * i) / 16000) * 12000), i * 2);
}
for (let i = 0; i < 50; i += 1) socket.sendBinary(speech.subarray(i * 640, (i + 1) * 640));
// 之后接静音，让端点检测判定"说完了"
for (let i = 0; i < 40; i += 1) socket.sendBinary(silence);
await wait(1500);
check(
  '说话音频会触发识别与回复（整条语音链路真的通了）',
  messages.some((m) => m.type === 'user.voice') || messages.some((m) => m.type === 'pet.say.start'),
  messages.map((m) => m.type).join(','),
);

socket.send({ type: 'voice.control', enabled: false });
await wait(300);

/* ------------------------------------------------------------------ 收尾 */

socket.close();
await wait(200);
if (child !== null) {
  child.kill('SIGTERM');
  await wait(300);
  if (child.exitCode === null) child.kill('SIGKILL');
}

console.log('');
if (failures.length) {
  console.error(`端到端联调未通过：${failures.length} 项失败 / ${passed + failures.length} 项`);
  for (const item of failures) console.error(`  - ${item}`);
  process.exit(1);
}
console.log(`端到端联调通过：${passed}/${passed} 项`);
process.exit(0);
