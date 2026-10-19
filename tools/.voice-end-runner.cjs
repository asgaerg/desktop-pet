// 由 test-voice-end.mjs 生成，勿手改。
const { app, BrowserWindow } = require('electron');
const http = require('http');
const fs = require('fs');
const path = require('path');

app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required');
app.disableHardwareAcceleration();

const VOICE_IO = "F:\\ai项目\\apps\\desktop\\src\\renderer\\voice-io.js";
const PAGE = "<!doctype html>\n<html><body>\n<script type=\"module\">\nimport { VoiceIO } from './voice-io.js';\n\nconst t0 = performance.now();\nconst events = [];\nconst result = { events, pass: false };\n\nfunction log(kind, detail) {\n  events.push({ kind, detail, at: Math.round(performance.now() - t0) });\n}\n\n// ── 场景 1：warmup 复现 —— 不带回调创建（bug 根因） ──\nwindow.audio = new VoiceIO();\nlog('warmup-created', '无回调实例（复现 app.js 预热路径）');\n\nconst sleep = (ms) => new Promise((r) => setTimeout(r, ms));\n\n// 生成 1 秒 440Hz 的 Int16 PCM\nfunction pcmBytes(seconds) {\n  const rate = 16000;\n  const n = rate * seconds;\n  const buf = new ArrayBuffer(n * 2);\n  const view = new Int16Array(buf);\n  for (let i = 0; i < n; i += 1) {\n    view[i] = Math.round(Math.sin((2 * Math.PI * 440 * i) / rate) * 12000);\n  }\n  return buf;\n}\n\ntry {\n  // ── 场景 2：修复后的模式 —— 每次播之前都（重新）挂回调 ──\n  window.audio.onPlaybackEnded = () => log('ended', '播放自然结束');\n\n  await window.audio.play(pcmBytes(1), 'pcm');\n  log('play1-started', '1 秒 PCM 开始播');\n\n  // 最多等 3 秒\n  const deadline = performance.now() + 3000;\n  while (!events.some((e) => e.kind === 'ended') && performance.now() < deadline) {\n    await sleep(50);\n  }\n  const e1 = events.filter((e) => e.kind === 'ended').length;\n  result.end1Count = e1;\n  result.end1Elapsed = events.find((e) => e.kind === 'ended')?.at ?? -1;\n\n  // ── 场景 3：打断 —— 2 秒音频，300ms 后 stop ──\n  let ended2 = 0;\n  const before = events.filter((e) => e.kind === 'ended').length;\n  window.audio.onPlaybackEnded = () => { ended2 += 1; log('ended', 'stop 打断结束'); };\n  await window.audio.play(pcmBytes(2), 'pcm');\n  await sleep(300);\n  await window.audio.stopPlayback();\n  await sleep(1200);\n  result.interruptFired = ended2 > 0;\n\n  // ── 场景 4：连播 —— play 自带打断（旧段 stop）+ 新段播完 ──\n  let ended3 = 0;\n  window.audio.onPlaybackEnded = () => { ended3 += 1; log('ended', '连播结束'); };\n  await window.audio.play(pcmBytes(2), 'pcm');\n  await sleep(300);\n  await window.audio.play(pcmBytes(2), 'pcm');\n  await sleep(3000);\n  result.chainEndedCount = ended3;\n\n  result.pass =\n    result.end1Count === 1 &&\n    result.end1Elapsed > 0 && result.end1Elapsed < 2000 &&\n    result.interruptFired === true &&\n    result.chainEndedCount >= 2;\n} catch (err) {\n  result.error = String(err && err.stack || err);\n}\n\nawait fetch('/result', { method: 'POST', body: JSON.stringify(result) });\n</script>\n</body></html>";

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
