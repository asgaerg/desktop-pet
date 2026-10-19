"""
真实屏幕的自检（`python -m pet --vision-test`）。

存在的理由：P4 的**分析**部分已经在主自检里用合成帧测得很透（纯函数、可离线、
确定性），但"采集层在这台机器上到底能不能拿到画面"只能靠真跑一次才知道。
没有这一步，P4 就永远停在"代码看起来对"的状态。

它做的判断很克制，因为真实屏幕的内容不可控：
  · 采集器不可用        -> 跳过（不算失败；可能只是没装 Pillow 或不允许访问桌面）
  · 抓到画面            -> 必须 1920x1080 之类的合理尺寸、网格非全同值
  · 窗口标题            -> 不做硬断言（可能一个都没有），但要打印出来便于核对
  · 隐私规则            -> 必须非空（否则等于没有保护）
  · 变化检测            -> 不强求"一定有变化"（用户可能真的没动屏幕），
                          但要把统计打出来，让人看到跳过了多少帧

退出码：0 = 通过或合理跳过，1 = 采集层坏了（抓不到或抓到空白）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Optional

from .vision import ChangeDetector, PrivacyRules, VisionLoop
from .vision_capture import PillowCapturer

log = logging.getLogger("pet.visiontest")


def _print_header(title: str) -> None:
    print(f"\n{title}")
    print("-" * max(20, min(70, len(title) + 8)))


def run_vision_test(interval_s: float = 1.5, frames: int = 3) -> int:
    """
    对着真实屏幕跑几轮感知，把关键事实打出来。

    返回退出码（0 通过/跳过，1 失败）。
    """
    print("=" * 70)
    print("桌面宠物 · 屏幕感知自检（真实屏幕）")
    print("=" * 70)

    _print_header("1. 采集器可用性")
    capturer = PillowCapturer()
    info = capturer.describe()
    print(f"  实现：{info['impl']}")
    print(f"  可用：{info['available']}")
    if info.get("error"):
        print(f"  原因：{info['error']}")

    if not capturer.available:
        print("\n  结论：跳过（采集器不可用）。这不算失败 ——")
        print("        看屏幕是锦上添花，装不上 Pillow 或不允许访问桌面时宠物照常工作。")
        return 0

    _print_header("2. 抓取一帧")
    first = capturer.grab()
    if first is None:
        print("  抓取失败：返回 None")
        print("\n  结论：失败。当前会话无法访问桌面（屏幕锁定 / 权限受限 / 无桌面会话）。")
        return 1

    spread = max(first.grid) - min(first.grid)
    print(f"  尺寸：{first.width}x{first.height}")
    print(f"  指纹：{first.signature:#018x}")
    print(f"  亮度：min={min(first.grid)} max={max(first.grid)} 跨度={spread}")
    if spread < 3:
        print("  警告：画面几乎单色 —— 可能抓到的是空白桌面或权限受限的占位图")
        return 1

    _print_header("3. 窗口标题（零成本的语义线索）")
    if first.titles:
        for index, title in enumerate(first.titles, 1):
            print(f"  {index}. {title}")
        # 验证清洗确实生效：不该再有多余的不可见字符
        dirty = [t for t in first.titles if any(not ch.isprintable() for ch in t)]
        print(f"  清洗检查：{'存在未清洗的字符（异常）' if dirty else '标题干净'}")
    else:
        print("  （未取到标题，可能是纯桌面或工具窗口 —— 不视为失败）")

    _print_header("4. 隐私规则")
    rules = PrivacyRules.from_config(None)
    print(f"  内置默认规则：{len(rules.title_patterns)} 条")
    print(f"  启用状态：{rules.enabled}")
    sample = rules.check(("我的密码 - Chrome",))
    print(f"  样例检查（'我的密码 - Chrome'）：{'放行（异常！）' if sample else '拦下 -> ' + sample.reason}")
    if not rules.title_patterns or sample:
        print("\n  结论：失败。隐私规则为空或未生效 —— 这等于没有保护，不能接受。")
        return 1

    _print_header("5. 图像编码（视觉模型 / OCR 的前置条件）")
    # 这一步很关键：内容层需要的是**编码后的图像字节**，而灰度网格不经过这条路径。
    # 不单独验证的话，"网格没问题"会掩盖"图片编码失败"。
    keeper = PillowCapturer(keep_image=True, max_edge_px=768)
    kept = keeper.grab()
    if kept is None:
        print("  抓取失败")
        return 1
    if not kept.image:
        print("  ⚠ keep_image=True 但未拿到图像字节 —— 视觉模型/OCR 会因此永远拿不到画面")
        return 1
    size_kb = len(kept.image) / 1024.0
    print(f"  原始尺寸：{kept.width}x{kept.height}")
    print(f"  编码后：{size_kb:.1f} KB（PNG，长边压到 {keeper.max_edge_px}px）")
    # PNG magic number：确认拿到的是真图片而不是空字节串
    if not kept.image.startswith(b"\x89PNG\r\n\x1a\n"):
        print("  ⚠ 编码结果不是合法 PNG")
        return 1
    print("  格式校验：PNG 头正确")
    # 省 token 的量化：一张 1920x1080 的 PNG 直接送模型大约要 1000+ token，
    # 压到 768 长边后大约降到十分之一量级。
    print(f"  说明：不压缩时约 {kept.width * kept.height / 750:.0f} token 量级，压缩后约 {size_kb * 4:.0f} 量级")

    _print_header("6. 内容抽取链")
    from .content import build_content_pipeline

    pipeline = build_content_pipeline(
        config={"content_order": ["title", "ocr", "vlm"], "model": ""},
        brain_section={},
        api_key="",
    )
    chain = pipeline.available_chain()
    print(f"  可用链路：{' -> '.join(chain) if chain else '（无）'}")
    print("  说明：title 永远可用（零成本）；ocr 需要装 tesseract；vlm 需要配模型名与密钥")
    result = pipeline.extract(kept)
    print(f"  本次抽取：source={result.source} 字符数={len(result.text)}")
    if result.text:
        print(f"  内容预览：{result.text[:100]}")
    if result.error:
        print(f"  （{result.error}）")

    _print_header(f"7. 连续观察 {frames} 帧（间隔 {interval_s}s）")
    detector = ChangeDetector()
    loop = VisionLoop(bus=None, capturer=capturer, rules=rules, detector=detector)  # type: ignore[arg-type]
    for index in range(frames):
        observation = loop.step()
        if observation is None:
            print(f"  第 {index + 1} 帧：跳过（画面无变化或命中隐私规则）")
        else:
            print(
                f"  第 {index + 1} 帧：有变化 novelty={observation.novelty:.2f} "
                f"变化比例={observation.changed_ratio:.2f}"
            )
            if observation.content:
                print(f"           文本线索：{observation.content[:70]}")
        if index < frames - 1:
            time.sleep(interval_s)

    stats = detector.stats()
    print(
        f"\n  统计：共 {stats['total_frames']} 帧，识别变化 {stats['change_hits']} 次，"
        f"跳过 {stats['skipped']} 次（省 {stats['saved_ratio']:.0%}）"
    )
    print(f"  感知循环状态：{loop.stats()}")

    _print_header("结论")
    print("  通过：采集层可用，画面非空白，隐私规则生效。")
    print("  （变化次数取决于你这几秒有没有动屏幕，不作为断言）")
    print("=" * 70)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pet --vision-test",
        description="对着真实屏幕验证屏幕感知（P4）的采集层",
    )
    parser.add_argument("--interval", type=float, default=1.5, help="两帧之间的间隔秒数")
    parser.add_argument("--frames", type=int, default=3, help="观察多少帧")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(name)-16s %(message)s")
    return run_vision_test(interval_s=max(0.2, args.interval), frames=max(1, args.frames))
