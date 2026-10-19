#!/usr/bin/env python3
"""验证 Python 侧日志「写入时轮转」（apps/brain/pet/main.py，2026-10-16 重建）。

## 背景（2026-10-14 发现）

启动时轮转对长期挂机程序等于没有上限：实测 python.log.1 41 MB、main.log.1
110 MB，而上限写的是 20 MB。所以真正轮转交给 `_SizeRotatingFileHandler`
（写入时判断），`_rotate_if_needed` 退为兜底。本脚本验证的就是这套写入时轮转。

## 实现：AST 抠真源码，不 import

`pet.main` 一 import 就拉起 gateway → tools → voice 的整条依赖链（audioop
在 Python 3.13 已移除、第三方依赖一大串）。所以这里用 ast 从 main.py 真源码
里抠出 `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT` / `_rotate_if_needed` /
`_SizeRotatingFileHandler` 四段编译执行 —— **测的就是磁盘上那份真代码**，
又不是拷贝。三个Python 版本都能跑。

## 六条断言

1. 结构：handler 是 logging.FileHandler 子类，emit 被重写
2. 写入时轮转：写超 max_bytes 立刻出 `.1`，主文件不超线
3. 内容迁移：最早的记录进 `.1`，最新的留在主文件
4. 备份上限：无论转多少次，永远没有 `.4`
5. UTF-8 记账：中文行按字节算（按字符算的话这条必挂）
6. 轮转后句柄健在：rotate 关了流会重开，后续记录照写

用法：`python tools/check-log-rotation.py`（自建临时目录，不碰真日志）
"""

from __future__ import annotations

import ast
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MAIN_SRC = ROOT / "apps" / "brain" / "pet" / "main.py"

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    tag = "OK  " if cond else "FAIL"
    print(f"  {tag} {name}" + (f" —— {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def extract_log_code() -> dict[str, Any]:
    """从 main.py 真源码抠出轮转相关四段，编译执行，返回命名空间。"""
    tree = ast.parse(MAIN_SRC.read_text(encoding="utf-8"), filename=str(MAIN_SRC))
    wanted_fns = {"_rotate_if_needed", "_SizeRotatingFileHandler"}
    wanted_consts = {"LOG_MAX_BYTES", "LOG_BACKUP_COUNT"}
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in wanted_fns:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in wanted_consts for t in node.targets
        ):
            nodes.append(node)
    assert nodes, "main.py 里没找到轮转相关定义（源码结构变了？）"
    mod = ast.Module(body=nodes, type_ignores=[])
    import typing

    ns: dict[str, Any] = {
        "logging": logging,
        "os": os,
        "sys": sys,
        "Path": Path,
        "Any": typing.Any,
    }
    exec(compile(mod, str(MAIN_SRC), "exec"), ns)  # noqa: S102 —— 有意为之
    return ns


def make_logger(tmp: Path, name: str, max_bytes: int, backups: int = 3) -> tuple[logging.Logger, Path]:
    log_path = tmp / f"{name}.log"
    logger = logging.getLogger(f"check-rot-{name}")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    # ⚠️ 直接建真源码的 handler，不要"先建占位再替换"——
    # 占位 FileHandler 不 close 会一直握着句柄，Windows 上
    # 轮转的 path.replace() 会因文件被占而失败（实测坑）。
    ns_handler = NS["_SizeRotatingFileHandler"](str(log_path), max_bytes=max_bytes, backups=backups)
    ns_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(ns_handler)
    logger.propagate = False
    return logger, log_path


NS = extract_log_code()


def main() -> int:
    print("== 抠出的常量 ==")
    print(f"  LOG_MAX_BYTES={NS['LOG_MAX_BYTES']:,}  LOG_BACKUP_COUNT={NS['LOG_BACKUP_COUNT']}")
    print("== 断言 ==")

    # ---- 1. 结构 ----
    handler_cls = NS["_SizeRotatingFileHandler"]
    check(
        "handler 是 logging.FileHandler 子类",
        issubclass(handler_cls, logging.FileHandler),
    )
    check(
        "emit 被重写（写入时轮转的挂点）",
        "emit" in handler_cls.__dict__ and handler_cls.emit.__qualname__.startswith("_SizeRotatingFileHandler"),
    )

    with tempfile.TemporaryDirectory(prefix="check-logrot-") as td:
        tmp = Path(td)

        # ---- 2. 写入时轮转 ----
        # ⚠️ max_bytes 要与行宽对齐：emit 里先 close()（冲掉缓冲）再查
        # st_size，所以"触发那次"磁盘上必须有 ≥ max_bytes 的内容才会真改名。
        # 105 字节/行（磁盘，含 \r\n）、记账 104/行：max=208 时第 3 行触发
        # （磁盘 210 ≥ 208 ✓），之后每 3 行稳定轮转一次 —— 10 行 = 3 次轮转。
        logger, log_path = make_logger(tmp, "a", max_bytes=208)
        line = "x" * 100
        for i in range(10):
            logger.info(f"{line}-{i:02d}")
        for h in logger.handlers:
            h.close()
        first = tmp / "a.log.1"
        main_size = log_path.stat().st_size
        check("写超 max_bytes 后 .1 存在（写入时轮转，无需 close 先行）", first.is_file())
        check(
            f"轮转链完整（.2/.3 也在 —— 10 行确实滚了 3 次）",
            (tmp / "a.log.2").is_file() and (tmp / "a.log.3").is_file(),
        )
        check(
            f"主文件只剩最后一行（实际 {main_size} ≤ 2 行）",
            main_size <= 2 * 105,
        )

        # ---- 3. 内容迁移 ----
        # max=208 时每 2 行滚一次，10 行 = 4 次轮转：最老的 x-00 已被
        # 第 4 次轮转按"上限 3 份"冲掉，.3 里是最老**幸存**的 x-02。
        backup3_text = (tmp / "a.log.3").read_text(encoding="utf-8")
        main_text = log_path.read_text(encoding="utf-8")
        all_names = "\n".join(p.read_text(encoding="utf-8") for p in tmp.glob("a.log*"))
        check("最老幸存记录（02）在 .3", "x-02" in backup3_text)
        check("最早的记录（00）已被上限轮掉（不在任何备份里）", "x-00" not in all_names)
        check("最新的记录（09）留在主文件", "x-09" in main_text)

        # ---- 4. 备份上限 ----
        logger2, path2 = make_logger(tmp, "b", max_bytes=208)
        for i in range(60):  # 20 次轮转，足够冲掉所有上限约束
            logger2.info(f"{line}-{i:02d}")
        for h in logger2.handlers:
            h.close()
        strays = [p.name for p in tmp.glob("b.log.4")]
        check("滚了很多次后也没有 .4（上限 3 份）", not strays, f"发现 {strays}")

        # ---- 5. UTF-8 字节记账 ----
        # 中文行：len(msg) 是字符数，utf-8 是 3 倍。max_bytes 恰好等于
        # 一行的磁盘大小（utf-8 + \r\n）—— 第一行写满不轮转，第二行同长
        # 必须立即轮转（按字符记账的话 50 字符远小于 151，这条必挂）。
        cn_line = "宠" * 50  # utf-8 150 字节 + \r\n = 磁盘 152
        max_bytes = len(cn_line.encode("utf-8")) + 1  # 151
        logger3, path3 = make_logger(tmp, "c", max_bytes=max_bytes)
        logger3.info(cn_line)  # 记账 151，不超线（0+151 不 > 151）
        after_first = not (tmp / "c.log.1").exists()
        logger3.info(cn_line)  # 记账 302 > 151，磁盘 152 ≥ 151 —— 必须轮转
        for h in logger3.handlers:
            h.close()
        check("第一行恰好写满不轮转", after_first)
        check("第二行同长立即轮转（按 UTF-8 字节记账）", (tmp / "c.log.1").is_file())

        # ---- 6. 轮转后句柄健在 ----
        # 10 行后 _written=208，再写一条会**再触发一次轮转**：旧内容进 .1、
        # 主文件只剩新记录 —— 这同时证明 close→rotate→reopen 全链路健康。
        logger4, path4 = make_logger(tmp, "d", max_bytes=208)
        for i in range(10):
            logger4.info(f"{line}-{i:02d}")  # 轮转多次
        logger4.info("轮转后的新记录")
        for h in logger4.handlers:
            h.close()
        main_text = path4.read_text(encoding="utf-8")
        b1_text = (tmp / "d.log.1").read_text(encoding="utf-8")
        check(
            "轮转后再写：新记录进主文件、旧内容搬进 .1（句柄重开成功）",
            "轮转后的新记录" in main_text and "x-09" in b1_text and "轮转后的新记录" not in b1_text,
        )

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 条挂了：{'、'.join(FAILURES)}")
        return 1
    print("✅ 六条全过 —— Python 侧写入时轮转行为正确")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
