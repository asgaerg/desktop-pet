/**
 * 生成默认序列帧素材（`node tools/make-placeholder-avatar.mjs`）。
 *
 * 为什么要有这个：序列帧播放器如果永远只走"程序化兜底"分支，那条真正要用的
 * 贴图加载路径就从未被验证过 —— 等 P1 拿到美术素材时才会第一次踩坑。
 * 所以这里用 Node 直接画出一套 16 帧的占位贴图 + avatar.json，
 * 让"切图 -> 按帧播放 -> 口型/情绪驱动"整条链路今天就是真的在跑。
 *
 * 零依赖：自己写 PNG 编码（zlib 是 Node 内置的）。
 */

import { deflateSync } from 'node:zlib';
import { writeFileSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const OUT_DIR = path.resolve(here, '..', 'src', 'renderer', 'assets', 'avatar', 'default');

const FRAME = 256;
/** 动画定义（帧号 -> 见下方 frames 数组） */
const LAYOUT = [
  { state: 'idle', count: 4, ears: 0, mouth: 0, eyes: 1 },
  { state: 'blink', count: 2, ears: 0, mouth: 0, eyes: 0.08 },
  { state: 'talk', count: 3, ears: 0, mouth: 0.9, eyes: 1 },
  { state: 'drag', count: 1, ears: 1.4, mouth: 0.35, eyes: 1.25 },
  { state: 'sleep', count: 2, ears: -0.5, mouth: 0.1, eyes: 0.1 },
  { state: 'happy', count: 2, ears: 0.25, mouth: -1, eyes: 0.85 },
  { state: 'curious', count: 2, ears: 0.7, mouth: 0.2, eyes: 1.3 },
];

// --------------------------------------------------------------------------- //
// 极小画布：直接操作 RGBA 像素缓冲，避免为了一个占位素材引入 canvas 依赖
// --------------------------------------------------------------------------- //

class Bitmap {
  constructor(width, height) {
    this.width = width;
    this.height = height;
    this.data = new Uint8Array(width * height * 4);
  }

  blend(x, y, r, g, b, a) {
    if (a <= 0) return;
    const xi = Math.round(x);
    const yi = Math.round(y);
    if (xi < 0 || yi < 0 || xi >= this.width || yi >= this.height) return;
    const i = (yi * this.width + xi) * 4;
    const alpha = Math.min(1, a);
    const inv = 1 - alpha;
    this.data[i] = Math.round(this.data[i] * inv + r * alpha);
    this.data[i + 1] = Math.round(this.data[i + 1] * inv + g * alpha);
    this.data[i + 2] = Math.round(this.data[i + 2] * inv + b * alpha);
    this.data[i + 3] = Math.min(255, Math.round(this.data[i + 3] * inv + 255 * alpha));
  }

  fillEllipse(cx, cy, rx, ry, color) {
    const x0 = Math.max(0, Math.floor(cx - rx - 1));
    const x1 = Math.min(this.width - 1, Math.ceil(cx + rx + 1));
    const y0 = Math.max(0, Math.floor(cy - ry - 1));
    const y1 = Math.min(this.height - 1, Math.ceil(cy + ry + 1));
    for (let y = y0; y <= y1; y += 1) {
      for (let x = x0; x <= x1; x += 1) {
        const dx = (x - cx) / rx;
        const dy = (y - cy) / ry;
        const d = dx * dx + dy * dy;
        if (d > 1.15) continue;
        // 边缘 1.2 像素做抗锯齿，否则缩放后边缘会有台阶
        const alpha = d > 1 ? 1 - (d - 1) / 0.15 : 1;
        this.blend(x, y, color[0], color[1], color[2], alpha * (color[3] ?? 1));
      }
    }
  }

  fillTriangle(a, b, c, color) {
    const minX = Math.max(0, Math.floor(Math.min(a[0], b[0], c[0])));
    const maxX = Math.min(this.width - 1, Math.ceil(Math.max(a[0], b[0], c[0])));
    const minY = Math.max(0, Math.floor(Math.min(a[1], b[1], c[1])));
    const maxY = Math.min(this.height - 1, Math.ceil(Math.max(a[1], b[1], c[1])));
    const area = (b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1]);
    if (area === 0) return;
    for (let y = minY; y <= maxY; y += 1) {
      for (let x = minX; x <= maxX; x += 1) {
        const w0 = ((b[0] - a[0]) * (y - a[1]) - (x - a[0]) * (b[1] - a[1])) / area;
        const w1 = ((c[0] - b[0]) * (y - b[1]) - (x - b[0]) * (c[1] - b[1])) / area;
        const w2 = 1 - w0 - w1;
        if (w0 < 0 || w1 < 0 || w2 < 0) continue;
        this.blend(x, y, color[0], color[1], color[2], color[3] ?? 1);
      }
    }
  }

  strokeLine(x0, y0, x1, y1, width, color) {
    const steps = Math.max(2, Math.ceil(Math.hypot(x1 - x0, y1 - y0) * 2));
    for (let i = 0; i <= steps; i += 1) {
      const t = i / steps;
      this.fillEllipse(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, width / 2, width / 2, color);
    }
  }
}

const BODY = [255, 179, 92];
const BODY_DARK = [232, 148, 66];
const BELLY = [255, 226, 186];
const EYE = [58, 42, 26];
const CHEEK = [255, 138, 138, 0.55];
const MOUTH = [140, 59, 59];
const OUTLINE = [150, 100, 40, 0.5];

/** 画一帧。phase 用于让 idle 有呼吸起伏，避免所有帧一模一样。 */
function drawFrame({ ears, mouth, eyes }, phase) {
  const bmp = new Bitmap(FRAME, FRAME);
  const cx = FRAME / 2;
  const cy = FRAME * 0.62 + Math.sin(phase) * 3;
  const r = FRAME * 0.27;

  // 影子
  bmp.fillEllipse(cx, cy + r * 1.06, r * 0.82, r * 0.15, [0, 0, 0, 0.16]);

  // 耳朵
  for (const side of [-1, 1]) {
    const lift = ears;
    bmp.fillTriangle(
      [cx + side * r * 0.52, cy - r * 0.72],
      [cx + side * (r * 0.74 + lift * 4), cy - r * (1.34 + lift * 0.12)],
      [cx + side * r * 0.2, cy - r * 0.96],
      BODY_DARK,
    );
  }

  // 身体
  bmp.fillEllipse(cx, cy, r * 0.92, r, BODY);
  bmp.fillEllipse(cx, cy + r * 0.34, r * 0.5, r * 0.42, BELLY);

  // 眼睛
  const eyeY = cy - r * 0.18;
  const eyeDx = r * 0.34;
  const eyeR = r * 0.135;
  for (const side of [-1, 1]) {
    const ex = cx + side * eyeDx;
    bmp.fillEllipse(ex, eyeY, eyeR, eyeR * eyes, EYE);
    if (eyes > 0.5) {
      bmp.fillEllipse(ex - eyeR * 0.32, eyeY - eyeR * 0.32, eyeR * 0.3, eyeR * 0.3, [255, 255, 255, 0.92]);
    }
  }

  // 腮红
  for (const side of [-1, 1]) {
    bmp.fillEllipse(cx + side * r * 0.58, cy + r * 0.14, r * 0.17, r * 0.1, CHEEK);
  }

  // 嘴
  const mouthY = cy + r * 0.3;
  if (mouth > 0.05) {
    bmp.fillEllipse(cx, mouthY + mouth * r * 0.05, r * (0.14 + mouth * 0.08), r * (0.05 + mouth * 0.2), MOUTH);
  } else if (mouth < -0.3) {
    // 笑：用两段弧线近似
    for (let i = -8; i <= 8; i += 1) {
      const t = i / 8;
      bmp.fillEllipse(cx + t * r * 0.22, mouthY - Math.cos(t * 1.4) * r * 0.05, 2.2, 2.2, EYE);
    }
  } else {
    bmp.strokeLine(cx - r * 0.12, mouthY, cx + r * 0.12, mouthY, 3, EYE);
  }

  // 描边（后画，压在内容之上形成轮廓感）
  for (let a = 0; a < Math.PI * 2; a += 0.02) {
    bmp.blend(cx + Math.cos(a) * r * 0.92, cy + Math.sin(a) * r, OUTLINE[0], OUTLINE[1], OUTLINE[2], 0.18);
  }

  return bmp;
}

// --------------------------------------------------------------------------- //
// PNG 编码（RGBA8、无滤波），够用且零依赖
// --------------------------------------------------------------------------- //

function crc32(buffer) {
  let crc = ~0;
  for (let i = 0; i < buffer.length; i += 1) {
    crc ^= buffer[i];
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
    }
  }
  return ~crc >>> 0;
}

function chunk(type, data) {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length, 0);
  const typeAndData = Buffer.concat([Buffer.from(type, 'ascii'), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(typeAndData), 0);
  return Buffer.concat([length, typeAndData, crc]);
}

function encodePng(bitmap) {
  const { width, height, data } = bitmap;
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; // 位深
  ihdr[9] = 6; // 颜色类型：RGBA
  ihdr[10] = 0;
  ihdr[11] = 0;
  ihdr[12] = 0;

  // 每行前加一个滤波类型字节（0 = None）
  const raw = Buffer.alloc(height * (width * 4 + 1));
  for (let y = 0; y < height; y += 1) {
    raw[y * (width * 4 + 1)] = 0;
    Buffer.from(data.buffer, y * width * 4, width * 4).copy(raw, y * (width * 4 + 1) + 1);
  }

  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(raw, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

// --------------------------------------------------------------------------- //
// 组装横向贴图 + manifest
// --------------------------------------------------------------------------- //

const animations = {};
let frameIndex = 0;
const allFrames = [];

for (const group of LAYOUT) {
  const frames = [];
  for (let i = 0; i < group.count; i += 1) {
    const phase = (i / group.count) * Math.PI * 2;
    allFrames.push(drawFrame(group, phase));
    frames.push(frameIndex);
    frameIndex += 1;
  }
  animations[group.state] = {
    frames,
    fps: group.state === 'sleep' ? 2.2 : group.state === 'talk' ? 13 : 8,
    loop: group.state !== 'blink',
  };
}

const sheet = new Bitmap(FRAME * allFrames.length, FRAME);
for (let i = 0; i < allFrames.length; i += 1) {
  const frame = allFrames[i];
  for (let y = 0; y < FRAME; y += 1) {
    for (let x = 0; x < FRAME; x += 1) {
      const src = (y * FRAME + x) * 4;
      const alpha = frame.data[src + 3];
      if (!alpha) continue;
      const dst = (y * sheet.width + i * FRAME + x) * 4;
      sheet.data[dst] = frame.data[src];
      sheet.data[dst + 1] = frame.data[src + 1];
      sheet.data[dst + 2] = frame.data[src + 2];
      sheet.data[dst + 3] = alpha;
    }
  }
}

mkdirSync(OUT_DIR, { recursive: true });
writeFileSync(path.join(OUT_DIR, 'sheet.png'), encodePng(sheet));
writeFileSync(
  path.join(OUT_DIR, 'avatar.json'),
  JSON.stringify(
    {
      $comment:
        '占位序列帧素材，由 apps/desktop/tools/make-placeholder-avatar.mjs 生成。换成真美术时保持同样的结构即可，播放器无需改动。',
      image: 'sheet.png',
      frameWidth: FRAME,
      frameHeight: FRAME,
      fps: 8,
      palette: { body: '#ffb35c', belly: '#ffe0b8', eye: '#3a2a1a', cheek: '#ff8a8a' },
      animations,
    },
    null,
    2,
  ) + '\n',
  'utf-8',
);

console.log(
  `已生成 ${allFrames.length} 帧占位素材 -> ${OUT_DIR}\n` +
    `  sheet.png  ${sheet.width}x${sheet.height}\n` +
    `  动画：${Object.entries(animations)
      .map(([name, spec]) => `${name}(${spec.frames.length}帧)`)
      .join(' ')}`,
);
