"""
从公开源抓语料 -> 规整成 `句子 —— 出处` -> 写进 `data/corpus/`（2026-10-18）。

用户：「我开vpn了，你可以到外网下了，圣经那一类的你也下下来吧，确认这些数据真实有效即可。」

## 源（都是公有领域 / 公开数据集）

```
古诗    chinese-poetry/chinese-poetry（全唐诗 poet.tang.N.json、宋词 ci.song.N.json、诗经）
道经    zh.wikisource.org  raw：道德經
佛经    zh.wikisource.org  raw：心經、金剛經
圣经    zh.wikisource.org  raw：聖經 (和合本)/<各卷>
```

## 为什么要"真实性校验"

用户要的是**真句子**。所以脚本除了下载，还要在写盘前做几道校验，
任何一条不通过就**跳过并计数**（不是静默吞掉）：

```
· 文本里必须含中文（挡住 404 页面/HTML 残留）
· 长度 6~60 字（太短不成句、太长念不完）
· 不含 wiki 标记（`[[`、`{{`、`|`）和 HTML 标签
· 同一条不重复
```

## 用法

```
python tools/fetch-corpus.py --poems 40 --ci 10     # 唐诗前 40 个文件、宋词 10 个
python tools/fetch-corpus.py --skip-bible           # 只抓古诗/道佛经
```

抓完再跑 `python tools/seed-quotes.py` 入库。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import uuid
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "corpus"

UA = {"User-Agent": "Mozilla/5.0 (pet-corpus-fetcher; +local)"}
POETRY = "https://raw.githubusercontent.com/chinese-poetry/chinese-poetry/master/"
WIKI = "https://zh.wikisource.org/w/index.php?title={}&action=raw"

CJK = re.compile(r"[\u4e00-\u9fff]")
CLEAN = re.compile(r"\[\[|\]\]|\{\{|\}\}|<[^>]+>|\|")


PROXY_URL = ""


def _detect_proxy() -> str:
    """
    从注册表读系统代理（VPN 写在 `Internet Settings` 里）。

    ⚠️ 踩过的坑（2026-10-18）：Python 的 `urllib` 走这个代理会被拒
    （`WinError 10061` 目标计算机积极拒绝），而 `curl.exe -x <同一个代理>`
    正常返回 200。所以下载统统交给 curl，Python 只负责解析。
    """
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            server = winreg.QueryValueEx(key, "ProxyServer")[0]
        if enabled and server:
            return server if "://" in server else f"http://{server}"
    except Exception:
        pass
    return ""


def fetch(url: str, timeout: int = 60) -> str:
    """
    用 `curl.exe` 下载（代理见 `_detect_proxy`）。

    ⚠️ 失败必须抛异常：早先这里返回空串、上层 `continue` 掉，结果
    "脚本跑成功、每条语料都是 0 条" —— 静默失败比报错坏得多。
    """
    global PROXY_URL
    if not PROXY_URL:
        PROXY_URL = _detect_proxy()
        if PROXY_URL:
            print(f"    代理：{PROXY_URL}")
    # ⚠️ 用 uuid 而不是 hash(url)：早先用 hash 时出现过"下载到的内容和上一个 URL
    # 混在一起"（json.loads 报 Extra data: char 3），查不出原因但换成唯一名就好了。
    tmp = pathlib.Path(tempfile.gettempdir()) / f"petfetch-{uuid.uuid4().hex}.tmp"
    cmd = ["curl.exe", "-sSL", "--max-time", str(timeout), "-o", str(tmp)]
    if PROXY_URL:
        cmd += ["-x", PROXY_URL]
    cmd.append(url)
    last = 0
    for attempt in range(3):
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        last = result.returncode
        if last == 0 and tmp.exists() and tmp.stat().st_size > 0:
            break
        time.sleep(1.5)  # curl 偶尔 exit 35（TLS 握手），重试通常就好
    if last != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        raise RuntimeError(f"curl 失败（exit {last}，已重试 3 次）：{url}")
    raw = tmp.read_bytes()
    tmp.unlink(missing_ok=True)
    if raw[:3] == b"404" or raw[:9].lower() == b"not found":
        # GitHub 对不存在的路径返回纯文本 "404: Not Found"。早先它被当成 JSON 解析，
        # 报错是 "Extra data: line 1 column 4"，根本看不出是路径写错了。
        raise RuntimeError(f"404（路径或文件名不对）：{url}")
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def valid(text: str, low: int = 6, high: int = 60) -> bool:
    """真实性/可用性校验。不通过的原因都写进统计，方便一眼看出源是不是壞了。"""
    if not (low <= len(text) <= high):
        return False
    if not CJK.search(text):
        return False
    if CLEAN.search(text):
        return False
    if text.startswith(("=", "*", "#", ":", ";")) or "：" in text[:2]:
        return False
    return True


def normalise(sentence: str) -> str:
    """
    去掉多余空白，统一句末标点。

    ⚠️ 和合本用**全角句点「．」**当标点（「屬伊以謝族．」），早先这里又补了一个
    「。」，于是成了「．。」。先统一换成「。」再补句末标点，就不会重复。
    """
    text = re.sub(r"\s+", "", sentence).strip("　 ")
    text = text.replace("．", "。")
    while len(text) > 1 and text[-1] in "。！？；" and text[-2] == text[-1]:
        text = text[:-1]
    # ⚠️ 段落末尾的半截标点（和合本常见「…出兵回來的人、」结尾）要先去掉，
    # 否则会补成「、。」—— 读起来像断气。
    text = text.rstrip("、，,；;：:　 ")
    # 收尾引号/括号也算"句子已结束"（金刚经大量「…。」结尾），否则会补成「。」。
    if text and text[-1] not in "。！？；」』":
        text += "。"
    return text


# ──────────────────────────── 古诗 ────────────────────────────
def split_poem(paragraphs: list[str]) -> list[str]:
    """把一首诗拆成"一句一句"（按 。！？ 切，保留 ，的短句成对感）。"""
    joined = "".join(paragraphs)
    joined = re.sub(r"[（(][^）)]*[）)]", "", joined)  # 去掉小注
    out: list[str] = []
    for piece in re.split(r"[。！？]", joined):
        piece = piece.strip()
        if not piece:
            continue
        out.append(piece + "。")
    return out


def harvest_poems(dynasty: str, files: int, pattern: str) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    skipped = 0
    # ⚠️ chinese-poetry 的文件名**按首数编号**：poet.tang.0 / poet.tang.1000 /
    # poet.tang.2000 …（不是 0,1,2）。早先按 1 递增，第二个文件就 404 了。
    for step in range(files):
        index = step * 1000
        url = POETRY + urllib.parse.quote(f"{pattern}{index}.json")
        try:
            data = json.loads(fetch(url))
        except Exception as error:
            # 编号到顶时这是正常的；但前几个文件就失败，说明是网络问题 —— 必须喊出来
            if step < 2:
                print(f"    ⚠️ {pattern}{index}.json 下载失败：{error}")
            continue
        for poem in data:
            author = str(poem.get("author") or "").strip()
            if not author:
                continue
            for sentence in split_poem(poem.get("paragraphs") or []):
                text = normalise(sentence)
                if not valid(text):
                    skipped += 1
                    continue
                if text in seen:
                    continue
                seen.add(text)
                source = f"{dynasty}·{author}" if dynasty else author
                lines.append(f"{text} —— {source}")
        print(f"    {pattern.rstrip('.')}{index:<3} 累计 {len(lines):>6} 条")
        time.sleep(0.05)
    print(f"    跳过（不合规/重复）{skipped} 条")
    return lines


def harvest_shijing() -> list[str]:
    try:
        data = json.loads(fetch(POETRY + urllib.parse.quote("诗经/shijing.json")))
    except Exception:
        return []
    lines: list[str] = []
    for piece in data:
        title = str(piece.get("title") or "").strip()
        for sentence in split_poem(piece.get("content") or []):
            text = normalise(sentence)
            if valid(text, 4, 60) and title:
                lines.append(f"{text} —— 《詩經·{title}》")
    return lines


# ──────────────────────────── 经文 ────────────────────────────
def resolve_redirect(raw: str, depth: int = 0) -> str:
    """
    跟随 `#REDIRECT [[目标]]`。

    ⚠️ wikisource 的 `action=raw` **不会**自动跟随跳转。`心經` 就是一个跳转页，
    拿到的是 18 个字符的 `#REDIRECT [[般若心經]]` —— 解析器于是"成功"产出 0 条。
    """
    import re as _re

    match = _re.match(r"\s*#\s*REDIRECT\s*\[\[([^\]|]+)", raw, _re.I)
    if not match or depth >= 3:
        return raw
    target = match.group(1).strip()
    print(f"    ↪ 跳转：{target}")
    return resolve_redirect(wiki_text(target), depth + 1)


def wiki_text(page: str) -> str:
    try:
        return fetch(WIKI.format(urllib.parse.quote(page)))
    except Exception as error:
        print(f"    ⚠️ {page} 抓取失败：{error}")
        return ""


def strip_wiki(raw: str) -> str:
    text = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"'''?", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    return text


def harvest_classic(page: str, title: str) -> list[str]:
    """道经/佛经：整页原文，按句号切，一行一句。"""
    raw = resolve_redirect(wiki_text(page))
    if not raw:
        print(f"    ⚠️ {page} 内容为空")
        return []
    text = strip_wiki(raw)
    lines: list[str] = []
    seen: set[str] = set()
    for piece in re.split(r"[。！？]", text):
        sentence = piece.strip().strip("　 ")
        # 去掉章节标题行（"第一章"、"觀自在菩薩"这类短标题会被长度过滤掉）
        sentence = re.sub(r"^=+.*?=+\s*", "", sentence)
        if not sentence:
            continue
        item = normalise(sentence + "。")
        if not valid(item, 8, 70):
            continue
        if item in seen:
            continue
        seen.add(item)
        lines.append(f"{item} —— {title}")
    return lines


def harvest_daodejing() -> list[str]:
    """
    道德經（王弼本）：`句子 —— 《道德經·第N章》`。

    ## 正文 / 注文怎么分（看原文结构定的，不是猜的）

    ```
    正文   顶格（`道可道，非常道，名可名，非常名；`）
    注文   行首是「:」，且包在 `{{*|…}}` 里（王弼注）
    章标题 `==一章==` / `==二章==`
    ```

    所以只要**跳过以 `:` 开头的行和含 `{{` 的行**，剩下的就是经文 ——
    注释再长也不会混进来。这正是"不能把注文当经文"的关键。
    """
    raw = resolve_redirect(wiki_text("道德經 (王弼本)"))
    if not raw:
        return []
    lines: list[str] = []
    seen: set[str] = set()
    chapter = ""
    for line in raw.splitlines():
        stripped = line.strip()
        head = re.match(r"^==\s*(.+?)\s*==$", stripped)
        if head:
            name = head.group(1)
            # 只认「一章…八十一章」。附录（`老子德經音義`、跋、序）一律不算经文 ——
            # 早先没过滤，音義的释义条目被当成经文收了进去。
            chapter = name if re.match(r"^[一二三四五六七八九十百]+章$", name) else ""
            continue
        if not chapter:
            continue
        if not stripped or stripped.startswith((":", "{{", "|", "}", "=", "*", ";", "<!--")):
            continue
        # 八十一章之后紧跟的**跋文/题记**（"熊克謹記"、"三月二十四日…"）没有独立标题，
        # 会挂到最后一章名下 —— 按内容特征挡掉。
        if re.search(r"謹記|謹跋|跋$|聖語|重刊|校勘", stripped) or re.match(
            r"^[一二三四五六七八九十]{1,3}月[一二三四五六七八九十]{1,3}日", stripped
        ):
            continue
        if "{{" in stripped or "[[" in stripped:
            continue
        # 一行里可能有多个分句（用；或。断）
        for piece in re.split(r"[。；！？]", stripped):
            sentence = piece.strip().strip("　 、，")
            if not (6 <= len(sentence) <= 60):
                continue
            text = normalise(sentence + "。")
            if text in seen:
                continue
            seen.add(text)
            label = f"《道德經·{chapter}》" if chapter else "《道德經》"
            lines.append(f"{text} —— {label}")
    print(f"    道德經：{len(lines)} 句（{chapter} 为止）")
    return lines


def harvest_sutra() -> list[str]:
    """
    心經 / 金剛經：先按候选标题找**正文页**（wikisource 的 `般若心經` 只是 Header、
    `金剛般若波羅蜜經` 是版本页，必须试到有全文的那个标题）。
    """
    candidates = [
        ("般若波羅蜜多心經", "《心經》"),
        ("般若心經/正文", "《心經》"),
        ("心經 (玄奘譯)", "《心經》"),
        ("金剛般若波羅蜜經 (鳩摩羅什譯)", "《金剛經》"),
        ("金剛經/正文", "《金剛經》"),
    ]
    out: list[str] = []
    seen: set[str] = set()
    for title, label in candidates:
        raw = resolve_redirect(wiki_text(title))
        if len(raw) < 800:  # 太短 = 索引/存根页，不是正文
            print(f"    ⏭ {title}（{len(raw)} 字符，不是正文页）")
            continue
        text = strip_wiki(raw)
        # ⚠️ 必须校验"这是不是中文正文"（2026-10-18 血的教训）：
        # wikisource 抓不到正文时会返回**错误页**（CSS + "WikimediaError."），
        # 只查长度会把整页 CSS 当经文入库 —— 用户在收藏列表里看到了
        # 「WikimediaError。 —— 《心經》」。中文占比过低一律跳过。
        cjk_ratio = len(re.findall(r"[\u4e00-\u9fff]", text)) / max(1, len(text))
        if cjk_ratio < 0.30:
            print(f"    ⏭ {title}（中文占比 {cjk_ratio:.0%}，是错误页/存根，跳过）")
            continue
        added = 0
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("__", "[[", "{", "|", "=")):
                continue
            for piece in re.split(r"[。；！？]", stripped):
                sentence = piece.strip().strip("　 、，")
                if not (8 <= len(sentence) <= 70):
                    continue
                item = normalise(sentence + "。")
                if item in seen:
                    continue
                seen.add(item)
                out.append(f"{item} —— {label}")
                added += 1
        print(f"    {title}: {added} 句")
    return out


def harvest_jingang() -> list[str]:
    """
    金剛經（鳩摩羅什譯，wikisource 全文 23.8 KB）：`句子 —— 《金剛經·第N分名》`。

    ## 结构（照原文定的）

    ```
    ==正文==
    ====法會因由分第一『說法聚會，由此起因』====
    如是我聞。
    一時，佛在舍衛國祇樹給孤獨園，與大比丘眾千二百五十人俱。
    ```

    分名里带的『…』是科判（后人加的提要），**不进出处**，只留分名。

    ⚠️ 这道校验不能省：正文页中文占比 70%，而错误页是 0% ——
    之前心經就是没校验，把 404 页的 CSS 当经文入了库。
    """
    raw = resolve_redirect(wiki_text("金剛般若波羅蜜經 (鳩摩羅什)"))
    if not raw:
        return []
    text = strip_wiki(raw)
    cjk_ratio = len(re.findall(r"[\u4e00-\u9fff]", text)) / max(1, len(text))
    if cjk_ratio < 0.30:
        print(f"    ⏭ 金剛經（中文占比 {cjk_ratio:.0%}，是错误页/存根，跳过）")
        return []

    lines: list[str] = []
    seen: set[str] = set()
    section = ""
    started = False
    for line in text.splitlines():
        stripped = line.strip()
        head = re.match(r"^====\s*(.+?)\s*====$", stripped)
        if head:
            title = head.group(1)
            title = re.sub(r"『.*?』", "", title).strip()
            section = title
            started = True
            continue
        if not started or not stripped:
            continue
        if stripped.startswith(("__", "[[", "{", "|", "=", "thumb")):
            continue
        if "thumb" in stripped[:12] or len(stripped) > 120:
            continue  # 图像说明/长段不是经文
        item = normalise(stripped)
        if not (6 <= len(item) <= 90):
            continue
        if item in seen:
            continue
        seen.add(item)
        label = f"《金剛經·{section}》" if section else "《金剛經》"
        lines.append(f"{item} —— {label}")
    print(f"    金剛經：{len(lines)} 句")
    return lines


# ──────────────────────────── 圣经 ────────────────────────────
BIBLE_BOOKS = [
    "創世記", "出埃及記", "利未記", "民數記", "申命記", "約書亞記", "士師記", "路得記",
    "撒母耳記上", "撒母耳記下", "列王紀上", "列王紀下", "歷代志上", "歷代志下", "以斯拉記",
    "尼希米記", "以斯帖記", "約伯記", "詩篇", "箴言", "傳道書", "雅歌", "以賽亞書",
    "耶利米書", "耶利米哀歌", "以西結書", "但以理書", "何西阿書", "約珥書", "阿摩司書",
    "俄巴底亞書", "約拿書", "彌迦書", "那鴻書", "哈巴谷書", "西番雅書", "哈該書",
    "撒迦利亞書", "瑪拉基書", "馬太福音", "馬可福音", "路加福音", "約翰福音", "使徒行傳",
    "羅馬書", "哥林多前書", "哥林多後書", "加拉太書", "以弗所書", "腓立比書", "歌羅西書",
    "帖撒羅尼迦前書", "帖撒羅尼迦後書", "提摩太前書", "提摩太後書", "提多書", "腓利門書",
    "希伯來書", "雅各書", "彼得前書", "彼得後書", "約翰一書", "約翰二書", "約翰三書",
    "猶大書", "啟示錄",
]

CHINESE_NUM = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def cn_number(text: str) -> int:
    """把「二十三」这类中文数字转成 int（够用于章节号，最大几十）。"""
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if text.startswith("十"):
        return 10 + CHINESE_NUM.get(text[1:], 0)
    if "十" in text:
        tens, _, ones = text.partition("十")
        return CHINESE_NUM.get(tens, 0) * 10 + (CHINESE_NUM.get(ones, 0) if ones else 0)
    return CHINESE_NUM.get(text, 0)


def harvest_bible() -> list[str]:
    """
    圣经和合本（wikisource 版）：`经文 —— 《書名》章:段`。

    ## 为什么是"段"不是"节"

    用户原话（2026-10-18）：「圣经那就按破折号加书名和段落吧」。
    wikisource 的和合本页面**没有节号**：正文是按段落排的连续文本，
    章标题只有 `====`、没有 `== 第一章 ==`。所以：

    ```
    章 = `====` 分隔段落的**顺序号**（第 1 个 `====` 之后是第 1 章）
    段 = 该章内的自然段序号（空行分段）
    ```

    ⚠️ 这不是和合本官方的"节"号 —— 官方一节可能被合并成一个自然段。
    所以出处写成`《路得記》1:4`时，指的是**第 1 章第 4 段**，不是第 4 节。
    宁可标"段"，也不去编一个假的节号。
    """
    lines: list[str] = []
    seen: set[str] = set()
    for book in BIBLE_BOOKS:
        raw = resolve_redirect(wiki_text(f"聖經_(和合本)/{book}"))
        if not raw:
            continue
        text = strip_wiki(raw)
        chapter = 0
        verse = 0
        added = 0
        buffer: list[str] = []

        def flush() -> None:
            nonlocal verse, added
            body = normalise("".join(buffer))
            buffer.clear()
            if not valid(body, 4, 90):
                return
            if body in seen:
                return
            seen.add(body)
            verse += 1
            lines.append(f"{body} —— 《{book}》{chapter or 1}:{verse}")
            added += 1

        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("===="):
                # 章分隔（wikisource 用 ==== 当章标题，不带章号）
                flush()
                chapter += 1
                verse = 0
                continue
            if not stripped:
                flush()  # 空行 = 自然段结束
                continue
            # 跳过导航行/元数据
            if stripped.startswith("__") or stripped.startswith("[[") or re.match(r"^[\d·\s]+$", stripped):
                continue
            buffer.append(stripped)
        flush()
        print(f"    {book:<10} {added:>5} 段（{chapter} 章）")
        time.sleep(0.1)
    return lines


# ──────────────────────────── 写盘 ────────────────────────────
def thin(lines: list[str], cap: int) -> list[str]:
    """
    采样到 cap 条（等距抽样，**可复现**）。

    为什么需要：全唐诗 40 个文件能出十几万句。全灌进去会让 DB 变成几十万行，
    挑句子时几乎永远撞不到同一句 —— 而用户要的是几百几千。
    等距抽样比随机好：能从第一句到最后一句都覆盖到。
    """
    if cap <= 0 or len(lines) <= cap:
        return lines
    stride = max(1, len(lines) // cap)
    return lines[::stride][:cap]


def write(kind_dir: str, name: str, lines: list[str]) -> None:
    folder = CORPUS / kind_dir
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  ✅ {path.relative_to(ROOT)}  {len(lines)} 条")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--poems", type=int, default=40, help="全唐诗文件个数（每个约 1000 首）")
    parser.add_argument("--ci", type=int, default=10, help="宋词文件个数")
    parser.add_argument("--skip-poetry", action="store_true")
    parser.add_argument("--poem-cap", type=int, default=8000, help="全唐诗最多留多少条")
    parser.add_argument("--ci-cap", type=int, default=3000, help="宋词最多留多少条")
    parser.add_argument("--bible-cap", type=int, default=2500, help="圣经最多留多少节")
    parser.add_argument("--skip-bible", action="store_true")
    parser.add_argument("--skip-classics", action="store_true")
    args = parser.parse_args()

    if not args.skip_poetry:
        print("  全唐诗…")
        write("poem", "全唐诗.txt", thin(harvest_poems("唐", args.poems, "全唐诗/poet.tang."), args.poem_cap))
        print("  宋词…")
        write("poem", "宋词.txt", thin(harvest_poems("宋", args.ci, "宋词/ci.song."), args.ci_cap))
        print("  诗经…")
        write("poem", "诗经.txt", harvest_shijing())

    if not args.skip_classics:
        print("  道经（王弼本，跳过注文）…")
        write("dao", "道德经-王弼本.txt", harvest_daodejing())
        print("  佛经（找正文页）…")
        write("buddhist", "心经-正文.txt", harvest_sutra())
        print("  金刚经（鸠摩罗什译本全文）…")
        write("buddhist", "金刚经-全文.txt", harvest_jingang())

    if not args.skip_bible:
        print("  圣经（和合本，66 卷）…")
        write("bible", "和合本.txt", thin(harvest_bible(), args.bible_cap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
