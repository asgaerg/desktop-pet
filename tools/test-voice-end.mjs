#!/usr/bin/env node
/**
 * 语音终点信号自测（2026-10-17）：用**真实 Electron 离屏窗口**验证
 * VoiceIO 的 onPlaybackEnded 会不会触发 —— 复现用户报的
 * 「语音播完了状态条还挂说话中」。
 *
 * 场景（模拟 app.js 的真实使用方式）：
 *   1. warmup 复现：先像麦克风预热那样**不带回调**创建 VoiceIO
 *      （这就是 bug 的根因：回调永远挂不上）；
 *   2. 按修复后的模式补挂回调，播 1 秒 PCM -> 终点信号必须在 ~1.5s 内到；
 *   3. 打断复现：播 2 秒、300ms 后 stop -> 终点信号必须立刻到；
 *   4. 连播复现：播 A（2 秒），300ms 后播 B -> A 的终点（stop 触发）+
 *      B 的终点都要到。
 *
 * 用法：node tools/test-voice-end.mjs   （退出码 0 = 全过）
 *
 * ⚠️ 需要 Electron 能跑（离屏 + --no-sandbox --disable-gpu，无需 GUI）。
 */

import { spawnSync, spawn } from 'node:child_process';
import { writeFileSync, existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopDir = path.resolve(here, '..', 'apps', 'desktop');
const requireCjs = createRequire(import.meta.url);

function findElectron() {
  for (const base of [
    path.join(desktopDir, 'node_modules', 'electron'),
    path.resolve(here, '..', 'node_modules', 'electron'),
  ]) {
    const exe = path.join(base, 'dist', 'electron.exe');
    if (existsSync(exe)) return exe;
  }
  throw new Error('找不到 electron.exe');
}

const RUNNER = path.join(here, '.voice-end-runner.cjs');

// 页面：在 http 源里 import **真文件**（file:// 的 module 有 CORS 坑，所以起 http）
const TEST_PAGE = `<!doctype html>
<html><body>
<script type="module">
import { VoiceIO } from './voice-io.js';

const t0 = performance.now();
const events = [];
const result = { events, pass: false };

function log(kind, detail) {
  events.push({ kind, detail, at: Math.round(performance.now() - t0) });
}

// ── 场景 1：warmup 复现 —— 不带回调创建（bug 根因） ──
window.audio = new VoiceIO();
log('warmup-created', '无回调实例（复现 app.js 预热路径）');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 生成 1 秒 440Hz 的 Int16 PCM
function pcmBytes(seconds) {
  const rate = 16000;
  const n = rate * seconds;
  const buf = new ArrayBuffer(n * 2);
  const view = new Int16Array(buf);
  for (let i = 0; i < n; i += 1) {
    view[i] = Math.round(Math.sin((2 * Math.PI * 440 * i) / rate) * 12000);
  }
  return buf;
}

try {
  // ── 场景 2：修复后的模式 —— 每次播之前都（重新）挂回调 ──
  window.audio.onPlaybackEnded = () => log('ended', '播放自然结束');

  await window.audio.play(pcmBytes(1), 'pcm');
  log('play1-started', '1 秒 PCM 开始播');

  // 最多等 3 秒
  const deadline = performance.now() + 3000;
  while (!events.some((e) => e.kind === 'ended') && performance.now() < deadline) {
    await sleep(50);
  }
  const e1 = events.filter((e) => e.kind === 'ended').length;
  result.end1Count = e1;
  result.end1Elapsed = events.find((e) => e.kind === 'ended')?.at ?? -1;

  // ── 场景 3：打断 —— 2 秒音频，300ms 后 stop ──
  let ended2 = 0;
  const before = events.filter((e) => e.kind === 'ended').length;
  window.audio.onPlaybackEnded = () => { ended2 += 1; log('ended', 'stop 打断结束'); };
  await window.audio.play(pcmBytes(2), 'pcm');
  await sleep(300);
  await window.audio.stopPlayback();
  await sleep(1200);
  result.interruptFired = ended2 > 0;

  // ── 场景 4：连播 —— play 自带打断（旧段 stop）+ 新段播完 ──
  let ended3 = 0;
  window.audio.onPlaybackEnded = () => { ended3 += 1; log('ended', '连播结束'); };
  await window.audio.play(pcmBytes(2), 'pcm');
  await sleep(300);
  await window.audio.play(pcmBytes(2), 'pcm');
  await sleep(3000);
  result.chainEndedCount = ended3;

  result.pass =
    result.end1Count === 1 &&
    result.end1Elapsed > 0 && result.end1Elapsed < 2000 &&
    result.interruptFired === true &&
    result.chainEndedCount >= 2;
} catch (err) {
  result.error = String(err && err.stack || err);
}

await fetch('/result', { method: 'POST', body: JSON.stringify(result) });
</script>
</body></html>`;

// runner：起 http 伺服 voice-io.js + 测试页，收集 /result
const runnerJs = `// 由 test-voice-end.mjs 生成，勿手改。
const { app, BrowserWindow } = require('electron');
const http = require('http');
const fs = require('fs');
const path = require('path');

app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required');
app.disableHardwareAcceleration();

const VOICE_IO = ${JSON.stringify(path.join(desktopDir, 'src', 'renderer', 'voice-io.js'))};
const PAGE = ${JSON.stringify(TEST_PAGE)};

app.whenReady().then(async () => {
  let resolveResult;
  const resultPromise = new Promise((r) => { resolveResult = r; });

  const server = http.createServer((req, res) => {
    if (req.url === '/voice-io.js') {
      res.writeHead(200, { 'content-type': 'text/javascript; charset=utf-8' });
      res.end(fs.readFileSync(VOICE_IO));
      return;
    }
    if (req.url === '/result') {
      let body = '';
      req.on('data', (c) => { body += c; });
      req.on('end', () => {
        res.writeHead(204); res.end();
        try { resolveResult(JSON.parse(body)); } catch (e) { resolveResult({ pass: false, error: 'bad result: ' + body }); }
      });
      return;
    }
    // /test 页面
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    res.end(PAGE);
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const port = server.address().port;

  const win = new BrowserWindow({
    width: 200, height: 120, show: false,
    webPreferences: { offscreen: false, contextIsolation: true, nodeIntegration: false },
  });
  await win.loadURL('http://127.0.0.1:' + port + '/test');

  const timeout = setTimeout(() => {
    console.log('RESULT ' + JSON.stringify({ pass: false, error: '总超时（15 秒没拿到结果）' }));
    app.exit(1);
  }, 15000);

  resultPromise.then((result) => {
    clearTimeout(timeout);
    console.log('RESULT ' + JSON.stringify(result));
    app.exit(result.pass ? 0 : 1);
  });
});
`;

function main() {
  const electron = findElectron();
  writeFileSync(RUNNER, runnerJs, 'utf-8');

  // ⚠️ 沙箱/终端可能带 ELECTRON_RUN_AS_NODE=1（electron 退化成纯 Node）
  const env = { ...process.env };
  delete env.ELECTRON_RUN_AS_NODE;

  const proc = spawnSync(
    electron,
    ['--disable-gpu', '--no-sandbox', RUNNER],
    { cwd: desktopDir, timeout: 30000, encoding: 'utf-8', env },
  );
  const out = (proc.stdout || '') + (proc.stderr || '');
  const line = out.split('\n').find((l) => l.startsWith('RESULT '));
  if (!line) {
    console.error('❌ 测试没产出结果（exit=' + proc.status + '）');
    console.error(out.slice(-1500));
    process.exit(2);
  }
  const result = JSON.parse(line.slice(7));
  console.log('事件时间线（ms）:');
  for (const e of result.events || []) console.log(`  ${String(e.at).padStart(5)}  ${e.kind}  ${e.detail}`);
  if (result.error) console.error('  error:', result.error);
  console.log(`场景1 播完终点: count=${result.end1Count} @${result.end1Elapsed}ms`);
  console.log(`场景2 打断终点: ${result.interruptFired ? '触发 ✓' : '未触发 ✗'}`);
  console.log(`场景3 连播终点: ${result.chainEndedCount} 次`);
  if (proc.status === 0 && result.pass) {
    console.log('✅ 三条场景全过 —— VoiceIO 终点信号在真实 Chromium 里工作正常');
    process.exit(0);
  }
  console.error('❌ 有场景没过');
  process.exit(1);
}

main();
