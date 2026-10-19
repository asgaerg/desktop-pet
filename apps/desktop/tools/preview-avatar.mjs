/**
 * 生成序列帧预览图（开发辅助）：把贴图里各动画的代表帧裁出来放大拼成一张图。
 * 用法：node tools/preview-avatar.mjs <素材目录> <输出png> [放大倍数]
 *
 * 为什么需要它：4096 宽的贴图直接看会被缩略图糊掉（细线条直接消失，容易误判成空帧），
 * 裁切放大才能验收画得对不对。
 */

import { readFileSync, writeFileSync } from 'node:fs';
import { deflateSync, inflateSync } from 'node:zlib';
import path from 'node:path';

const [, , avatarDir, outFile, scaleArg] = process.argv;
if (!avatarDir || !outFile) {
  console.error('用法：node tools/preview-avatar.mjs <素材目录> <输出png> [放大倍数]');
  process.exit(2);
}
const scale = Number(scaleArg ?? 2);

// ---------------------------------------------------------------- 读 PNG

function decodePng(buf) {
  if (buf.readUInt32BE(0) !== 0x89504e47) throw new Error('不是 PNG');
  let offset = 8;
  let width = 0;
  let height = 0;
  const idat = [];
  while (offset < buf.length) {
    const length = buf.readUInt32BE(offset);
    const type = buf.toString('ascii', offset + 4, offset + 8);
    const data = buf.subarray(offset + 8, offset + 8 + length);
    if (type === 'IHDR') {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
    } else if (type === 'IDAT') {
      idat.push(data);
    }
    offset += 12 + length;
  }
  const raw = inflateSync(Buffer.concat(idat));
  const stride = width * 4 + 1;
  const pixels = Buffer.alloc(width * height * 4);
  for (let y = 0; y < height; y += 1) {
    raw.copy(pixels, y * width * 4, y * stride + 1, y * stride + 1 + width * 4);
  }
  return { width, height, pixels };
}

// ---------------------------------------------------------------- 写 PNG

function crc32(buffer) {
  let crc = ~0;
  for (let i = 0; i < buffer.length; i += 1) {
    crc ^= buffer[i];
    for (let bit = 0; bit < 8; bit += 1) crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
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

function encodePng(width, height, rgba) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8;
  ihdr[9] = 6;
  const raw = Buffer.alloc(height * (width * 4 + 1));
  for (let y = 0; y < height; y += 1) {
    raw[y * (width * 4 + 1)] = 0;
    rgba.copy(raw, y * (width * 4 + 1) + 1, y * width * 4, (y + 1) * width * 4);
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(raw, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

// ---------------------------------------------------------------- 拼图

const manifest = JSON.parse(readFileSync(path.join(avatarDir, 'avatar.json'), 'utf8'));
const sheet = decodePng(readFileSync(path.join(avatarDir, manifest.image)));
const FW = manifest.frameWidth;
const FH = manifest.frameHeight;
const columns = Math.floor(sheet.width / FW);

const anims = Object.entries(manifest.animations);
const maxFrames = Math.max(...anims.map(([, a]) => a.frames.length));
const cellW = FW * scale;
const cellH = FH * scale;
const outW = cellW * maxFrames;
const outH = cellH * anims.length;
const out = Buffer.alloc(outW * outH * 4);

// 棋盘底，方便看清透明区域
for (let y = 0; y < outH; y += 1) {
  for (let x = 0; x < outW; x += 1) {
    const tone = ((x >> 4) + (y >> 4)) % 2 ? 200 : 235;
    const i = (y * outW + x) * 4;
    out[i] = tone;
    out[i + 1] = tone;
    out[i + 2] = tone;
    out[i + 3] = 255;
  }
}

anims.forEach(([, anim], row) => {
  anim.frames.forEach((frame, col) => {
    const sx = (frame % columns) * FW;
    const sy = Math.floor(frame / columns) * FH;
    for (let y = 0; y < FH; y += 1) {
      for (let x = 0; x < FW; x += 1) {
        const src = ((sy + y) * sheet.width + sx + x) * 4;
        const alpha = sheet.pixels[src + 3] / 255;
        if (alpha === 0) continue;
        for (let dy = 0; dy < scale; dy += 1) {
          for (let dx = 0; dx < scale; dx += 1) {
            const px = col * cellW + x * scale + dx;
            const py = row * cellH + y * scale + dy;
            const dst = (py * outW + px) * 4;
            out[dst] = Math.round(out[dst] * (1 - alpha) + sheet.pixels[src] * alpha);
            out[dst + 1] = Math.round(out[dst + 1] * (1 - alpha) + sheet.pixels[src + 1] * alpha);
            out[dst + 2] = Math.round(out[dst + 2] * (1 - alpha) + sheet.pixels[src + 2] * alpha);
          }
        }
      }
    }
  });
  console.log(`行 ${row + 1}：${anims[row][0]}（${anim.frames.length} 帧，fps=${anim.fps ?? manifest.fps}）`);
});

writeFileSync(outFile, encodePng(outW, outH, out));
console.log(`已输出 ${outFile}（${outW}x${outH}，每行一个动画，顺序同上）`);
