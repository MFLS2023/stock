#!/usr/bin/env python3
"""波段之门专用适配器。

为什么不能用 generic_mixed：

1. **一篇文章五份拷贝**。公众号导出目录里同一篇文章同时存在 .md/.html/.mhtml/.docx/.pdf，
   通用导入器按文件建文档，723 篇会变成 3600+ 份重复块，检索时同一句话命中五次。
   这里只认 .md 为正文源，其余四种格式登记为同一文档的 alternate_formats，不重复建块。

2. **正文只占 22%**。md 全文 676 万字符里只有 145 万是正文，其余是留言区、微信 UI
   残留（"预览时标签不可点"、"轻点两下取消赞"）和头像图片链接。全量入库等于 78% 噪声。

3. **留言区里有 4985 条作者本人回复**，是他对读者提问的直接答复，价值高于正文
   （正文常说"看图"，回复里反而把话说透）。但混在读者留言里，必须靠"波段之门来自"
   这个署名切出来，单独成块并标 chunk_type=author_reply。

4. **结论在图里**。平均每篇 44 张图，正文大量"看下面这张图"式指代。图片已本地化到
   `图片/<标题>/` 且带序号，这里建立文章到图片的映射，OCR 图内文字（点位、指标名、
   日期常以文字形式印在截图上），使检索能命中图内信息。

图片扩展名与真实格式不一致（大量 PNG 存成 .jpg），OCR 前按文件头判定真实格式，
不依赖扩展名。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml
from docx import Document
from pypdf import PdfReader

from kb_import_utils import (
    CJK,
    ROOT,
    clean_text,
    clean_title,
    extract_date,
    infer_topics,
    merge_short_units,
    natural_key,
    ocr_images,
    split_text,
    text_layer_is_usable,
    write_jsonl,
    write_text_lf,
)

CONFIG = ROOT / "_知识库系统" / "config" / "sources.yaml"
SOURCE_ID = "boduanzhimen"
AUTHOR = "波段之门"
POPPLER = Path(
    r"C:\Users\20577\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\poppler\Library\bin\pdftoppm.exe"
)

# md 正文结束的标志。微信导出在正文后附留言区和一堆 UI 文案，
# 取最早出现的那个作为正文边界。
BODY_END_MARKERS = (
    "预览时标签不可点",
    "精选留言",
    "微信扫一扫",
    "轻点两下取消赞",
)

# md 开头的固定样板：封面图、"原创 波段之门 波段之门"、日期地区行、
# "在小说阅读器读本章"/"去阅读" 等阅读器入口。
HEADER_NOISE = (
    re.compile(r"^!\[cover_image\]\([^)]*\)\s*$"),
    re.compile(r"^原创\s+.*$"),
    re.compile(r"^_\d{4}年\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{2}_.*$"),
    re.compile(r"^在小说阅读器读本章$"),
    re.compile(r"^去阅读$"),
    re.compile(r"^#\s+"),  # 标题行，已单独存为 title
)

# 留言区里作者本人的署名行。读者是"<昵称>来自<地区>"，作者是"波段之门来自"。
AUTHOR_REPLY_MARK = "波段之门来自"
COMMENTER_LINE = re.compile(r"^(.{1,30}?)来自(.{0,10})$")

# 纯 UI 噪声行，出现在留言区之间
UI_NOISE = re.compile(
    r"^(\*+|_+|×\s*分析|阅读|修改于|分享|留言|收藏|听过|使用小程序|"
    r"微信扫一扫可打开此内容，?|使用完整服务|\[\s*(取消|允许)\s*\]\(javascript:void\\?\(0\\?\);\)\s*)*$"
)

IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
TEXT_EXTS = {".md", ".txt"}
ALT_EXTS = {".html", ".mhtml", ".docx", ".pdf"}

# OCR 缓存版本键（2026-08-24 补）。此前本导入器的两类缓存都没有版本字段——
# 南京路/郁金香靠 CACHE_VERSION/OCR_CACHE_VERSION 常量实现「改引擎或预处理即全量
# 重算」，这里没有，导致换了 OCR 引擎旧缓存照样命中、新引擎永远不生效。
# 规则与 import_tulip_garden.py:299 一致：读缓存时校验版本，不符按未命中重算并覆写。
# 改 OCR 引擎 / 预处理 / clean_text(ocr=True) 时 bump 这个数。
OCR_CACHE_VERSION = 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_text_file(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def real_image_suffix(path: Path) -> str:
    """按文件头判定真实图片格式。

    这批图里大量 PNG 存成 .jpg（实测 8 张一组里 7 张如此），按扩展名交给
    图像库会解析失败或静默出错，所以读魔术字节。
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(8)
    except OSError:
        return ""
    if head[:2] == b"\xff\xd8":
        return ".jpg"
    if head[:4] == b"\x89PNG":
        return ".png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head[:4] == b"RIFF":
        return ".webp"
    return ""


def parse_wechat_name(stem: str) -> tuple[str, str]:
    """从 `[2023-03-01-0855]标题` 解出 (日期, 标题)。"""
    match = re.match(r"^\[(\d{4}-\d{2}-\d{2})-(\d{4})\](.+)$", stem)
    if match:
        return match.group(1), match.group(3).strip()
    return "", stem.strip()


def split_body_and_comments(text: str) -> tuple[str, str]:
    """按最早出现的结束标志切开正文与留言区。"""
    cut = len(text)
    for marker in BODY_END_MARKERS:
        index = text.find(marker)
        if index > 0:
            cut = min(cut, index)
    return text[:cut], text[cut:]


def clean_body(body: str) -> str:
    """去掉头部样板、图片链接行和空白，保留正文段落。"""
    # markdown 链接常被微信导出成跨行的形式：
    #     [ 可以参考这一年
    #     ](http://mp.weixin.qq.com/s?__biz=...)
    # 下面是逐行处理，跨行的正则匹配不到，所以先在整体上把链接压平。
    # 实测不做这一步会残留 428 个 URL（占正文 3% 字符且无检索价值），
    # 还会让块尾看起来像被截断（末尾非句末标点的比例 13.4% → 53.6%）。
    body = re.sub(r"\[\s*([^\]]*?)\s*\]\s*\(\s*(https?://[^)]*)\s*\)", r"《\1》", body, flags=re.S)

    kept: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(pattern.match(stripped) for pattern in HEADER_NOISE):
            continue
        # 图片引用行：正文里的图另有映射，这里只留占位标记以保持段落顺序感
        if re.fullmatch(r"!\[[^\]]*\]\([^)]*\)", stripped):
            kept.append("〔图〕")
            continue
        # 行内混着图片和文字的，去掉图片语法留文字
        stripped = re.sub(r"!\[[^\]]*\]\([^)]*\)", "〔图〕", stripped)
        stripped = re.sub(r"\[([^\]]*)\]\(javascript:void\\?\(0\\?\);\)", r"\1", stripped)
        # 站内文章链接：保留标题文本，丢掉 URL。
        # 他文章里常用「[ 2019上半年展望 ](http://mp.weixin.qq.com/s?__biz=…)」
        # 回链自己的旧文，标题有检索价值（读者问"你哪篇讲过X"能命中），
        # 但那串 200 字符的 URL 没有 —— 实测 170 个链接占正文 23367 字符（3.1%），
        # 按 450 字符切块相当于白占了 50 多个块的空间，还让块尾看起来像被截断。
        stripped = re.sub(r"\[\s*([^\]]*?)\s*\]\(https?://[^)]*\)", r"《\1》", stripped)
        stripped = stripped.strip()
        if not stripped or stripped == "〔图〕":
            if kept and kept[-1] == "〔图〕":
                continue
            kept.append("〔图〕")
            continue
        if UI_NOISE.fullmatch(stripped):
            continue
        kept.append(stripped)
    # 折叠连续的图占位
    folded: list[str] = []
    for item in kept:
        if item == "〔图〕" and folded and folded[-1] == "〔图〕":
            continue
        folded.append(item)
    return clean_text("\n".join(folded))


def extract_author_replies(comments: str) -> list[dict]:
    """从留言区切出作者本人的回复，并带上它回应的那条读者留言。

    留言区结构是 `头像图 / <昵称>来自<地区> / 正文`，作者的署名固定是
    "波段之门来自"（地区为空）。一条作者回复单独看常常没有主语
    （"是的，写错了"），所以把上一条读者留言作为 question 一并留下，
    否则块本身不可读。
    """
    lines = [line.strip() for line in comments.splitlines()]
    entries: list[dict] = []
    speaker = ""
    buffer: list[str] = []

    def flush() -> None:
        if speaker and buffer:
            body = clean_text("\n".join(buffer))
            if body:
                entries.append({"speaker": speaker, "text": body})
        buffer.clear()

    for line in lines:
        if not line:
            continue
        if re.fullmatch(r"!\[[^\]]*\]\([^)]*\)", line):
            continue
        match = COMMENTER_LINE.match(re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line).strip())
        if match:
            flush()
            speaker = match.group(1).strip()
            continue
        cleaned = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line).strip()
        cleaned = re.sub(r"\[([^\]]*)\]\(javascript:void\\?\(0\\?\);\)", r"\1", cleaned).strip()
        if not cleaned or UI_NOISE.fullmatch(cleaned):
            continue
        buffer.append(cleaned)
    flush()

    replies: list[dict] = []
    # 只有「作者回复紧跟在读者留言之后」才配对（审查报告 P1-6）。
    #
    # 原来是无条件往前找第一个非作者发言，而他在留言区经常连发多条独立发言，
    # 于是连发的第 2、3… 条全部被挂到同一条读者留言上 ——
    # 实测 637 条 (12.8%) 的 question 是错的，涉及 243 篇；
    # 最严重一篇 `boduanzhimen-wx-48ad5f2a6c14` 里一条留言被 12 条回复共用，其中 11 条无关。
    # 后果是按提问召回时拿到答非所问的内容。
    #
    # 连发内部**不是**互不相干：抽样看到他会连续汇报自己的动作
    #   「我没有开仓。」→「2689时,还是0.14」→「我计划是接近0或者复数时才分批建仓。」
    # 所以第 2 条起不能简单丢掉上下文，改为记 run_index（这是连发里的第几条）
    # 与 run_head（这串连发的开头那条回复），让下游能把一串重新拼起来。
    #
    # 连发长度分布（4343 段）：1 条 3958 段、2 条 273 段、3 条 55 段…最长 12 条。
    # 也就是说 91% 的配对本来就是对的，问题集中在剩下那 9%。
    run_index = 0          # 当前连发串里的序号，1 表示紧跟读者留言的那条
    run_first_order = 0    # 当前连发串首条的 order，供下游回溯上下文
    for index, entry in enumerate(entries):
        if entry["speaker"] != AUTHOR:
            # 读者发言打断连发，重新计数
            run_index = 0
            run_first_order = 0
            continue
        run_index += 1
        order = len(replies) + 1
        question = ""
        if run_index == 1:
            # 紧邻的前一条若是读者留言才算提问；留言区以作者发言开头时前面没有读者
            if index > 0 and entries[index - 1]["speaker"] != AUTHOR:
                prior = entries[index - 1]
                question = f"{prior['speaker']}：{prior['text']}"
            run_first_order = order
        replies.append({
            "question": question,
            "answer": entry["text"],
            "order": order,
            "run_index": run_index,
            # 记首条的**序号**而不是正文：正文塞进每条会被 FTS 重复索引，
            # 实测前缀占续言块内容 33.2%，`板块` 虚增 15 块、`龙头` 一半命中只来自前缀。
            # 首条本来就在库里且 order 相邻，用序号引用就能回溯，不必复制内容。
            "run_first_order": run_first_order if run_index > 1 else 0,
        })
    return replies


def find_article_images(image_root: Path, title: str) -> list[Path]:
    """定位一篇文章的本地图片，按文件名里的序号自然排序。

    映射规则：`图片/<标题>/` 目录名与 md 标题逐字相等（实测 710/710 命中）。
    文件名尾部的 `_<N>` 是该图在原始 HTML 里的 img 下标，不连续
    （未下载的头像/表情留空号），所以只用来排序，不用来对齐正文第几张图。
    """
    directory = image_root / title
    if not directory.is_dir():
        return []
    files = [path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTS]
    return sorted(files, key=lambda path: natural_key(path.name))


def is_avatar(path: Path) -> bool:
    """头像和表情图：留言区里每条留言前一张，占了图片总数的大头，OCR 无意义。

    判定靠尺寸而非路径：头像是 64x64/240x240 的小方图，K 线截图是 1080 宽的长图。
    """
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
    except Exception:
        return False
    if width <= 260 and height <= 260:
        return True
    # 极端细长的分隔线图
    return min(width, height) <= 40


def chart_ocr_is_useful(text: str, *, min_chars: int = 40, min_cjk: float = 0.45) -> tuple[bool, str]:
    """判断一张 K 线截图的 OCR 结果值不值得入库。

    实测 40 张抽样：只有 12 张（30%）识别出的是有意义的中文，其余是行情软件
    界面被逐字读出的乱码 —— "券交未登录上指訕 1 4 : 55 1 4 : 55 1 4 : 55…"
    （时间刻度）、"A 股成交 B 股成交国债成交基金成交权证成交"（面板标题）。
    这类文本进了库只会污染检索：搜"成交"会命中几百张图的面板标题。

    有价值的是他截图里的文字段落，正文往往没有，例如
    "我只能把第二目标给你们：2772点"、"最理想的走势是周初反弹一个小时"。

    两个门槛都是从抽样分布里定的，不是拍的：
    - 中文字符占比 < 0.45 的几乎全是数字刻度和板块代码（噪声组中位数约 0.25）
    - 少于 40 字的多是水印残留（"公众号·波段之门"、"分时线"）

    返回 (是否保留, 不保留的原因)。
    """
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return False, "empty"
    # 40 字门槛会误杀他写在图上的短判断（审查报告 P2-1 实测 33 条语句通顺的被丢），
    # 例如「我们每天的底部是稍微有些移动的,明天的30分底部在3137点。」只有 31 字。
    # 判据同下面的 watermark_only：有句读说明是完整句子而不是面板逐字读出的乱码。
    # 门槛取 min_chars 的六成（40→24），低于这个长度的确实只是水印或零星数字。
    if len(compact) < min_chars:
        looks_like_prose = len(re.findall(r"[。！？；，,、?]", compact)) >= 2
        if not (looks_like_prose and len(compact) >= min_chars * 0.6):
            return False, f"too_short({len(compact)})"
    cjk_share = len(re.findall(f"[{CJK}]", compact)) / len(compact)
    if cjk_share < min_cjk:
        return False, f"low_cjk({cjk_share:.2f})"
    # 水印本身不算内容：去掉水印后仍要够长。
    #
    # 但门槛不能直接沿用 min_chars（审查报告 P1-7）：水印只占 6~8 字，
    # 「38 字正文 + 6 字水印 = 44 字」过了上面第一道 min_chars，去掉水印剩 38 字，
    # 38 < 40 就被判成"纯水印"丢掉 —— 实测命中的 12 条里 8 条是他的真实判断，
    # 包括带点位的「短线的底,大概率会破掉3356,抄底不宜过早」。
    #
    # 放宽的方式是看**有没有句读**，不是单纯降数字：实测这 12 条全部落在 32~39 字，
    # 任何低于 32 的门槛都会把噪声一起收进来。而两者的分界线很干净 ——
    #   他写在图上的话是完整句子，标点 3~7 个
    #   商品标签和行情面板（「方解石Calcite中国.内蒙古自治区…」）标点 0 个
    without_mark = re.sub(r"公众号[·．.]?波段之门|波段之门", "", compact)
    if len(without_mark) < min_chars:
        has_sentence_punct = len(re.findall(r"[。！？；，,、?]", without_mark)) >= 2
        # 句读够多说明是他写的话，此时只要求达到 min_chars 的六成
        if not (has_sentence_punct and len(without_mark) >= min_chars * 0.6):
            return False, "watermark_only"
    # 行情软件面板：中文比很高（每个字都是汉字）却没有语义，光靠 cjk_share 拦不住。
    # 典型是"A股成交B股成交国债成交基金成交权证成交最新指数今日开盘昨日收盘"这种
    # 字段名连排。命中两个以上面板词且没有句读，就是面板而不是他写的话。
    panel_hits = len(re.findall(
        r"股成交|国债成交|基金成交|权证成交|债券成交|最新指数|今日开盘|昨日收盘|"
        r"指数振幅|总成交量|总成交额|最高指|最低指|量比|涨跌幅|平均涨幅|主力净|"
        r"上证换手|家数|总市值|资产分析|总账户|本月收益|近半年|证券代码|证券名称|成交日期",
        compact,
    ))
    if panel_hits >= 2 and not re.search(r"[。！？；]", compact):
        return False, f"quote_panel({panel_hits})"
    return True, ""


def render_pdf_page(pdf: Path, page: int, output: Path, dpi: int = 150, max_pixels: int = 6_000_000) -> None:
    """把 PDF 的一页渲染成 PNG，供 OCR 用。

    用 -scale-to 而不是只给 -r：这批 PDF 里有把整篇长图塞进一页的（做T指南第1页
    按 150dpi 渲出来是 2.1 亿像素），既撞 Pillow 的 decompression bomb 上限，
    OCR 也会慢到实际不可用。限制长边像素后，一页几秒内可完成。
    """
    executable = str(POPPLER) if POPPLER.exists() else (shutil.which("pdftoppm") or "")
    if not executable:
        raise FileNotFoundError("pdftoppm not found")
    output.parent.mkdir(parents=True, exist_ok=True)
    # 长边上限：按 max_pixels 反推一个正方形边长，长图会被等比缩到这个长边
    side_limit = int(max_pixels ** 0.5) * 2
    completed = subprocess.run(
        [
            executable, "-png", "-r", str(dpi), "-scale-to", str(side_limit),
            "-f", str(page), "-l", str(page),
            "-singlefile", str(pdf), str(output.with_suffix("")),
        ],
        cwd=str(Path(executable).parent),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if completed.returncode != 0 or not output.exists():
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"pdftoppm rc={completed.returncode} out_exists={output.exists()} detail={detail!r}")


def extract_pdf_pages(path: Path, *, ocr_scanned: bool = False, cache_dir: Path | None = None,
                      dpi: int = 150, errors: list[dict] | None = None,
                      stats: Counter | None = None) -> list[dict]:
    """抽取 PDF 正文；无文字层的扫描页可选走 OCR。

    这批付费文章 101 个 PDF 里有 65 个是纯扫描图（一个字符的文字层都没有），
    而且恰好是 2022-2023 年的核心技术文。不 OCR 等于这部分内容不进库。
    """
    reader = PdfReader(path)
    units: list[dict] = []
    pending: list[tuple[str, Path]] = []
    work: Path | None = None
    try:
        for index, page in enumerate(reader.pages, start=1):
            try:
                text = clean_text(page.extract_text() or "")
            except Exception:
                text = ""
            if text_layer_is_usable(text):
                units.append({"locator": f"第{index}页", "text": text, "method": "embedded", "confidence": "high"})
                continue
            if not ocr_scanned:
                units.append({"locator": f"第{index}页", "text": text, "method": "low_text", "confidence": "low"})
                continue
            # 版本号拼进摘要串：bump OCR_CACHE_VERSION 即换一批缓存文件名，
            # 旧文件自然失效（留在目录里不碍事，想清理可删 pdf-*.json）。
            digest = hashlib.sha256(
                f"{path}|{index}|{dpi}|ocrv{OCR_CACHE_VERSION}".encode("utf-8")
            ).hexdigest()
            cache = (cache_dir / f"pdf-{digest[:16]}.json") if cache_dir else None
            if cache is not None and cache.exists():
                try:
                    cached = json.loads(cache.read_text(encoding="utf-8"))
                    units.append({
                        "locator": f"第{index}页", "text": cached.get("text", ""),
                        "method": "pdf_ocr", "confidence": "medium" if cached.get("text") else "low",
                    })
                    if stats is not None:
                        stats["pdf_ocr_cached"] += 1
                    continue
                except (OSError, json.JSONDecodeError):
                    pass
            if work is None:
                work = Path(tempfile.mkdtemp(prefix="bdzm-pdf-", dir=ROOT / "_知识库系统" / "tmp"))
            image = work / f"page-{index:03d}.png"
            try:
                render_pdf_page(path, index, image, dpi)
            except Exception as exc:
                if errors is not None:
                    errors.append({"stage": "pdf_render", "path": str(path), "page": index, "error": str(exc)})
                units.append({"locator": f"第{index}页", "text": text, "method": "low_text", "confidence": "low"})
                continue
            key = f"{index}"
            pending.append((key, image))
            units.append({"locator": f"第{index}页", "text": "", "method": "pdf_ocr", "confidence": "low", "_ocr_key": key,
                          "_cache": str(cache) if cache else ""})
        if pending:
            results = ocr_images(pending)
            for unit in units:
                key = unit.pop("_ocr_key", None)
                cache_path = unit.pop("_cache", "")
                if key is None:
                    continue
                result = results.get(key, {"text": "", "error": "no_result"})
                text = clean_text(result.get("text", ""), ocr=True)
                unit["text"] = text
                unit["confidence"] = "medium" if text else "low"
                if cache_path:
                    write_text_lf(
                        Path(cache_path),
                        json.dumps({"text": text, "error": result.get("error", "")}, ensure_ascii=False, indent=2) + "\n",
                    )
                if result.get("error") and errors is not None:
                    errors.append({"stage": "pdf_ocr", "path": str(path), "page": key, "error": result["error"]})
                if stats is not None:
                    stats["pdf_ocr_done"] += 1
    finally:
        # 未消费的临时键要清掉，避免残留在 chunk 里
        for unit in units:
            unit.pop("_ocr_key", None)
            unit.pop("_cache", None)
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
    return units


def extract_docx_units(path: Path) -> list[dict]:
    document = Document(path)
    units: list[dict] = []
    for index, paragraph in enumerate(document.paragraphs, start=1):
        text = clean_text(paragraph.text)
        if text:
            units.append({"locator": f"正文第{index}段", "text": text, "method": "docx", "confidence": "high"})
    for table_index, table in enumerate(document.tables, start=1):
        rows = [" | ".join(clean_text(cell.text) for cell in row.cells) for row in table.rows]
        text = clean_text("\n".join(rows))
        if text:
            units.append({"locator": f"表格{table_index}", "text": text, "method": "docx_table", "confidence": "high"})
    return units


def update_last_import_summary(summary: dict) -> None:
    """只改写本来源那一段的 last_import_summary 三行，不动 sources.yaml 的其余部分。

    原来是 `yaml.safe_dump(config)` 整文件重写，后果（CLAUDE.md 记为 P0）：
      1. **22 行注释全部消失** —— 那是 AGENTS.md 引用的项目规范，不是可选装饰；
         `generic_supported_extensions` 也会从行内数组变成多行列表。
      2. **并行会话时互相抹掉对方的登记** —— 实测有人恢复格式时又抹掉了别人刚加的
         `kongkonglong` 整段和 `nanjinglu_bian` 的 4 个新字段。

    做法是文本级替换：定位 `- id: boduanzhimen` 那一段里的 `last_import_summary:`，
    只替换紧随其后的缩进子行。找不到锚点就抛错，不静默跳过 ——
    静默跳过会让 sources.yaml 里的数字长期停在旧值上，后续脚本会被假数字骗到。
    """
    original = CONFIG.read_text(encoding="utf-8")
    lines = original.splitlines()

    # 1) 找到本来源块的行区间
    start = None
    for index, line in enumerate(lines):
        if line.strip() == f"- id: {SOURCE_ID}":
            start = index
            break
    if start is None:
        raise SystemExit(f"sources.yaml 里找不到 `- id: {SOURCE_ID}`，拒绝写入")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].lstrip().startswith("- id:"):
            end = index
            break

    # 2) 在该区间内找 last_import_summary，连同它的缩进子行一起替换
    key_line = None
    for index in range(start, end):
        if lines[index].strip() == "last_import_summary:":
            key_line = index
            break
    if key_line is None:
        raise SystemExit(f"{SOURCE_ID} 段里找不到 last_import_summary，拒绝写入")
    key_indent = len(lines[key_line]) - len(lines[key_line].lstrip())
    tail = key_line + 1
    while tail < end:
        stripped = lines[tail].strip()
        # 空行或缩进比 key 更深的行都属于这个映射
        if stripped and (len(lines[tail]) - len(lines[tail].lstrip())) <= key_indent:
            break
        tail += 1

    child_indent = " " * (key_indent + 2)
    replacement = [lines[key_line]]
    replacement += [f"{child_indent}{name}: {summary[name]}"
                    for name in ("documents", "chunks", "errors")]
    updated = lines[:key_line] + replacement + lines[tail:]

    text = "\n".join(updated) + "\n"
    # 校验：改完必须仍是合法 YAML，且除这三个数字外没有别的字段被动到
    reparsed = yaml.safe_load(text)
    entry = next(item for item in reparsed["sources"] if item["id"] == SOURCE_ID)
    if entry["last_import_summary"] != summary:
        raise SystemExit(f"写入校验失败：期望 {summary}，实际 {entry['last_import_summary']}")
    before = yaml.safe_load(original)
    for item_before, item_after in zip(before["sources"], reparsed["sources"]):
        keys_before = set(item_before) - {"last_import_summary"}
        keys_after = set(item_after) - {"last_import_summary"}
        if keys_before != keys_after:
            raise SystemExit(f"写入校验失败：{item_before.get('id')} 的字段集变了")

    temporary = CONFIG.with_suffix(".yaml.tmp")
    write_text_lf(temporary, text)
    temporary.replace(CONFIG)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 篇公众号文章，0 为全部（调试用）")
    parser.add_argument("--ocr-images", action="store_true", help="OCR 文章配图（慢，默认关闭）")
    parser.add_argument("--ocr-limit", type=int, default=0, help="限制 OCR 图片张数，0 为不限")
    parser.add_argument("--skip-paid", action="store_true", help="跳过付费文章 PDF")
    parser.add_argument("--ocr-pdf", action="store_true", help="OCR 无文字层的扫描版 PDF（65个付费PDF需要）")
    parser.add_argument(
        "--out-dir",
        default="",
        help="把产物写到别处（调试用）。配合 --limit 试切块参数时必须加这个，"
        "否则 chunks.jsonl 会被只含前 N 篇的残缺版覆盖；同时也不会改写 sources.yaml",
    )
    args = parser.parse_args()

    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    source = next(item for item in config["sources"] if item["id"] == SOURCE_ID)
    source_root = ROOT / source["source_path"]
    wechat_root = source_root / "公众号文章"
    paid_root = source_root / "付费文章"
    image_root = wechat_root / "图片"
    lib = ROOT / "_知识库系统" / "source_libraries" / SOURCE_ID
    # --out-dir 只改写出位置，OCR 缓存仍读写正式目录（缓存是纯派生数据，共享才不浪费算力）
    ocr_cache_dir = lib / "image_ocr_cache"
    ocr_cache_dir.mkdir(parents=True, exist_ok=True)
    if args.out_dir:
        lib = Path(args.out_dir).resolve()
    for name in ("texts", "maps"):
        (lib / name).mkdir(parents=True, exist_ok=True)

    documents: list[dict] = []
    parents: list[dict] = []
    chunks: list[dict] = []
    errors: list[dict] = []
    stats: Counter[str] = Counter()
    image_map: list[dict] = []

    # ---------- 第一部分：公众号文章（md 为正文源） ----------
    md_files = sorted(
        (path for path in wechat_root.iterdir() if path.is_file() and path.suffix.lower() == ".md"),
        key=lambda path: natural_key(path.name),
    )
    if args.limit:
        md_files = md_files[: args.limit]

    ocr_queue: list[tuple[str, Path]] = []
    ocr_owner: dict[str, dict] = {}
    # 内容完全相同的 md 会算出同一个 sha256。实测有 6 篇同日重复下载的文件
    # （[2026-07-14-1731]一片涨停 等 4 篇内容一字不差），若直接用 hash 前12位
    # 做 document_id，建索引时会撞 UNIQUE 约束。这里给重复的加序号后缀，
    # 并记下它是哪一份的副本，保留"这些文件确实存在"这个事实而不静默丢弃。
    wechat_id_seen: Counter[str] = Counter()

    for md_path in md_files:
        digest = sha256_file(md_path)
        base_id = f"{SOURCE_ID}-wx-{digest[:12]}"
        wechat_id_seen[base_id] += 1
        occurrence = wechat_id_seen[base_id]
        doc_id = base_id if occurrence == 1 else f"{base_id}-dup{occurrence}"
        duplicate_of = "" if occurrence == 1 else base_id
        date, title = parse_wechat_name(md_path.stem)
        if not date:
            date = extract_date(md_path.name, md_path.stem)
        raw = read_text_file(md_path)
        body_raw, comments_raw = split_body_and_comments(raw)
        body = clean_body(body_raw)
        replies = extract_author_replies(comments_raw)

        images = find_article_images(image_root, title)
        charts = [path for path in images if not is_avatar(path)]
        stats["images_total"] += len(images)
        stats["images_charts"] += len(charts)

        # 同名的其它格式登记为备用格式，不重复建块
        alternates = [
            str(candidate)
            for suffix in sorted(ALT_EXTS)
            if (candidate := md_path.with_suffix(suffix)).exists()
        ]

        units: list[dict] = []
        # 正文切块目标 450 字，不是 1000。
        # 原因：他的正文中位数只有 781 字（大量内容画在图里，文字本来就短），
        # 目标定 1000 时 61% 的文章整篇只切出 1 块 —— 检索命中的是"整篇文章"
        # 而不是"讲这件事的那一段"，用户要的答案埋在 900 多字里。
        # 450 是实测选出来的：他的段落本身很短，split_text 只在段落/句子边界切，
        # 目标再往下压会把一个完整段落拆成两块，语义断裂。
        for index, piece in enumerate(split_text(body, 450), start=1):
            units.append(
                {
                    "locator": f"正文第{index}段",
                    "text": piece,
                    "method": "md",
                    "confidence": "high",
                    "chunk_type": "article_body",
                }
            )
        for reply in replies:
            # 三种形态，对应留言区的三种真实情形（审查报告 P1-6）：
            #   1. 紧跟读者留言 → 读者问 / 作者答
            #   2. 连发的第 2 条起 → 作者续言，带上这串开头供还原上下文，
            #      **不冒充问答** —— 它回答的不是那条读者留言
            #   3. 留言区以他自己开头 → 作者留言
            if reply["question"]:
                text = f"读者问：{reply['question']}\n作者答：{reply['answer']}"
            elif reply.get("run_first_order"):
                # 只写首条的序号，不复制它的正文 —— 复制会被 FTS 重复索引
                # （实测前缀占续言块内容 33.2%，`板块` 虚增 15 块）。
                # ⚠️ 括号必须是半角 ( )：库里现存 641 条前缀就是半角（2026-08-24 实测），
                # 这里曾误写全角（），重导一次就会让 641 条前缀整体翻转、FTS 词频漂移。
                text = (f"作者续言(承接本篇第{reply['run_first_order']}条，"
                        f"同一串第{reply['run_index']}条)：{reply['answer']}")
            else:
                text = f"作者留言：{reply['answer']}"
            units.append(
                {
                    "locator": f"留言区第{reply['order']}条作者回复",
                    "text": clean_text(text),
                    "method": "md_comment",
                    "confidence": "high",
                    "chunk_type": "author_reply",
                }
            )
        stats["author_replies"] += len(replies)

        # 图片 OCR 单元先占位，实际文字稍后回填
        if args.ocr_images:
            for order, path in enumerate(charts, start=1):
                if args.ocr_limit and stats["ocr_queued"] >= args.ocr_limit:
                    break
                key = f"{doc_id}-img{order:03d}"
                cache = ocr_cache_dir / f"{sha256_file(path)}.json"
                unit = {
                    "locator": f"配图{order}:{path.name}",
                    "text": "",
                    "method": "ocr",
                    "confidence": "medium",
                    "chunk_type": "chart_ocr",
                    "image_path": str(path),
                }
                units.append(unit)
                if cache.exists():
                    try:
                        cached = json.loads(cache.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        cached = None
                    # 版本不符按未命中处理：排队重算，识别结果会带新版本号覆写回同一文件。
                    if isinstance(cached, dict) and int(cached.get("cache_version") or 0) == OCR_CACHE_VERSION:
                        # 缓存存的是原始识别结果，质量门槛在这里重新判一次：
                        # 门槛调整后旧缓存不必重跑 OCR，也不会绕过过滤。
                        cached_text = cached.get("text", "")
                        keep, reason = chart_ocr_is_useful(cached_text)
                        unit["text"] = cached_text if keep else ""
                        unit["confidence"] = "medium" if keep else "low"
                        stats["ocr_cached"] += 1
                        if keep:
                            stats["ocr_kept"] += 1
                        else:
                            stats[f"ocr_dropped_{reason.split('(')[0]}"] += 1
                        continue
                    stats["ocr_cache_stale"] += 1
                ocr_queue.append((key, path))
                ocr_owner[key] = {"unit": unit, "cache": cache}
                stats["ocr_queued"] += 1

        for order, path in enumerate(images, start=1):
            image_map.append(
                {
                    "document_id": doc_id,
                    "title": title,
                    "date": date,
                    "order": order,
                    "file": str(path),
                    "real_format": real_image_suffix(path),
                    "kind": "avatar_or_ui" if is_avatar(path) else "chart",
                }
            )

        documents.append(
            {
                "source_id": SOURCE_ID,
                "document_id": doc_id,
                "title": title,
                "date": date,
                "author_or_guest": AUTHOR,
                "content_type": "wechat_article",
                "topics": infer_topics(title, body),
                "characters": len(re.sub(r"\s+", "", body)),
                "original_path": str(md_path),
                "normalized_text_path": str(lib / "texts" / f"{doc_id}.txt"),
                "sha256": digest,
                "duplicate_of": duplicate_of,
                "alternate_formats": alternates,
                "image_count": len(images),
                "chart_count": len(charts),
                "author_reply_count": len(replies),
                "risk_flags": "正文含〔图〕占位，结论可能在图中" if "〔图〕" in body else "",
            }
        )
        write_text_lf(
            lib / "texts" / f"{doc_id}.txt",
            "\n\n".join(f"[{unit['locator']} | {unit['method']}]\n{unit['text']}" for unit in units if unit["text"]) + "\n",
        )
        ocr_owner.setdefault("_pending_docs", {"unit": None, "cache": None})
        # 暂存，等 OCR 回填后再建块。副本登记为文档但不建块：
        # 同一段文字入库两次会让检索同一句命中多条，且父块内容重复。
        documents[-1]["_units"] = [] if duplicate_of else units
        if duplicate_of:
            stats["wechat_duplicate"] += 1

    # ---------- OCR 回填 ----------
    if ocr_queue:
        print(f"OCR 队列 {len(ocr_queue)} 张，开始识别…", flush=True)
        results = ocr_images(ocr_queue)
        for key, payload in ocr_owner.items():
            if key == "_pending_docs" or payload["unit"] is None:
                continue
            result = results.get(key, {"text": "", "error": "no_result"})
            text = clean_text(result.get("text", ""), ocr=True)
            keep, reason = chart_ocr_is_useful(text)
            # 缓存里存原始识别结果和判定，方便回查为什么某张图没进库；
            # 但只有通过质量门槛的才写进 unit（空 text 的 unit 后面会被过滤掉）。
            payload["unit"]["text"] = text if keep else ""
            payload["unit"]["confidence"] = "medium" if keep else "low"
            write_text_lf(
                payload["cache"],
                json.dumps(
                    {"cache_version": OCR_CACHE_VERSION,
                     "text": text, "kept": keep, "drop_reason": reason, "error": result.get("error", "")},
                    ensure_ascii=False, indent=2,
                ) + "\n",
            )
            if result.get("error"):
                errors.append({"stage": "ocr", "key": key, "error": result["error"]})
            stats["ocr_done"] += 1
            if keep:
                stats["ocr_kept"] += 1
            else:
                stats[f"ocr_dropped_{reason.split('(')[0]}"] += 1

    # ---------- 第二部分：付费文章 / 指标源码 ----------
    if not args.skip_paid:
        paid_files = sorted(
            (path for path in paid_root.rglob("*") if path.is_file()),
            key=lambda path: natural_key(path.relative_to(paid_root).as_posix()),
        )
        seen_hashes: dict[str, str] = {}
        for path in paid_files:
            suffix = path.suffix.lower()
            if suffix not in (TEXT_EXTS | ALT_EXTS | IMAGE_EXTS):
                stats["paid_skipped_format"] += 1
                continue
            if suffix in {".html", ".mhtml"}:
                stats["paid_skipped_format"] += 1
                continue
            digest = sha256_file(path)
            doc_id = f"{SOURCE_ID}-paid-{digest[:12]}"
            if digest in seen_hashes:
                stats["paid_duplicate"] += 1
                continue
            seen_hashes[digest] = doc_id
            relative = path.relative_to(paid_root).as_posix()
            title = clean_title(path)
            date = extract_date(path.name, relative)
            units: list[dict] = []
            try:
                if suffix == ".pdf":
                    pdf_units = extract_pdf_pages(
                        path,
                        ocr_scanned=args.ocr_pdf,
                        cache_dir=ocr_cache_dir,
                        errors=errors,
                        stats=stats,
                    )
                    for unit in pdf_units:
                        for piece in split_text(unit["text"], 1000):
                            units.append({**unit, "text": piece, "chunk_type": "paid_article"})
                elif suffix == ".docx":
                    raw_units = extract_docx_units(path)
                    for unit in merge_short_units(raw_units, min_chars=400, max_chars=1000):
                        units.append({**unit, "chunk_type": "paid_article"})
                elif suffix in TEXT_EXTS:
                    text = clean_text(read_text_file(path))
                    kind = "indicator_formula" if "公式" in path.name or "指标" in path.name else "paid_article"
                    for index, piece in enumerate(split_text(text, 1200), start=1):
                        units.append(
                            {
                                "locator": f"全文第{index}段",
                                "text": piece,
                                "method": suffix[1:],
                                "confidence": "high",
                                "chunk_type": kind,
                            }
                        )
                    if kind == "indicator_formula":
                        stats["indicator_files"] += 1
                else:
                    stats["paid_images"] += 1
                    continue
            except Exception as exc:
                errors.append({"stage": "paid_extract", "path": str(path), "error": str(exc)})
                continue
            units = [unit for unit in units if unit["text"]]
            if not units:
                stats["paid_empty"] += 1
                continue
            documents.append(
                {
                    "source_id": SOURCE_ID,
                    "document_id": doc_id,
                    "title": title,
                    "date": date,
                    "author_or_guest": AUTHOR,
                    "content_type": f"paid_{suffix.lstrip('.')}",
                    "topics": infer_topics(title, "\n".join(unit["text"] for unit in units)),
                    "characters": sum(len(re.sub(r"\s+", "", unit["text"])) for unit in units),
                    "original_path": str(path),
                    "normalized_text_path": str(lib / "texts" / f"{doc_id}.txt"),
                    "sha256": digest,
                    "duplicate_of": "",
                    "alternate_formats": [],
                    "risk_flags": "低文本PDF页，可能为截图" if any(unit["method"] == "low_text" for unit in units) else "",
                    "_units": units,
                }
            )
            write_text_lf(
                lib / "texts" / f"{doc_id}.txt",
                "\n\n".join(f"[{unit['locator']} | {unit['method']}]\n{unit['text']}" for unit in units) + "\n",
            )

    # ---------- 建 parents / chunks ----------
    for document in documents:
        units = document.pop("_units", [])
        units = [unit for unit in units if unit.get("text")]
        if not units:
            continue
        groups: list[list[dict]] = []
        current: list[dict] = []
        length = 0
        for unit in units:
            # 不同 chunk_type 不混进同一个父块：正文、作者回复、图 OCR
            # 是三种可信度和用途不同的内容，混在一起会让 --show-parent 读出串味的上下文
            if current and (length + len(unit["text"]) > 4000 or current[-1]["chunk_type"] != unit["chunk_type"]):
                groups.append(current)
                current, length = [], 0
            current.append(unit)
            length += len(unit["text"])
        if current:
            groups.append(current)
        for parent_number, group in enumerate(groups, start=1):
            parent_id = f"{document['document_id']}-p{parent_number:03d}"
            locator = group[0]["locator"] if len(group) == 1 else f"{group[0]['locator']}—{group[-1]['locator']}"
            parents.append(
                {
                    "source_id": SOURCE_ID,
                    "document_id": document["document_id"],
                    "parent_id": parent_id,
                    "title": document["title"],
                    "date": document["date"],
                    "author_or_guest": AUTHOR,
                    "locator": locator,
                    "text": "\n".join(unit["text"] for unit in group),
                }
            )
            for child_number, unit in enumerate(group, start=1):
                chunks.append(
                    {
                        "source_id": SOURCE_ID,
                        "source_name": source["display_name"],
                        "document_id": document["document_id"],
                        "parent_id": parent_id,
                        "chunk_id": f"{parent_id}-c{child_number:02d}",
                        "chunk_type": unit["chunk_type"],
                        "title": document["title"],
                        "date": document["date"],
                        "author_or_guest": AUTHOR,
                        "topics": document["topics"],
                        "claim_type": "opinion_or_case",
                        "market_regime": "未标注",
                        "locator": unit["locator"],
                        "text": unit["text"],
                        "original_path": document["original_path"],
                        "confidence": unit["confidence"],
                        "extraction_method": unit["method"],
                        "image_path": unit.get("image_path", ""),
                    }
                )
                stats[f"chunk_{unit['chunk_type']}"] += 1

    write_jsonl(lib / "documents.jsonl", documents)
    write_jsonl(lib / "parents.jsonl", parents)
    write_jsonl(lib / "chunks.jsonl", chunks)
    write_jsonl(lib / "maps" / "image_map.jsonl", image_map)

    content_map = [
        f"# {source['display_name']} 内容地图",
        "",
        "| 日期 | 标题 | 类型 | 主题 | 字符 | 配图 | 作者回复 |",
        "|---|---|---|---|---:|---:|---:|",
    ]
    for item in sorted(documents, key=lambda row: (row.get("date") or "9999", row["title"])):
        content_map.append(
            f"| {item.get('date') or '未标注'} | {item['title'].replace('|', '｜')} | {item['content_type']} | "
            f"{'、'.join(item.get('topics') or []) or '未标注'} | {item['characters']} | "
            f"{item.get('chart_count', 0)} | {item.get('author_reply_count', 0)} |"
        )
    write_text_lf(lib / "content_map.md", "\n".join(content_map) + "\n")

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_id": SOURCE_ID,
        "documents": len(documents),
        "parents": len(parents),
        "chunks": len(chunks),
        "stats": dict(stats),
        "errors": errors[:50],
        "error_count": len(errors),
    }
    write_text_lf(lib / "source_summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    write_text_lf(
        lib / "quality_report.md",
        "\n".join(
            [
                f"# {source['display_name']} 导入质量报告",
                "",
                f"- 文档：{len(documents)}",
                f"- 父块：{len(parents)}",
                f"- 检索块：{len(chunks)}",
                f"- 作者留言回复：{stats['author_replies']} 条",
                f"- 配图总数：{stats['images_total']}，其中K线等实质图 {stats['images_charts']}",
                f"- OCR：已识别 {stats['ocr_done']}，命中缓存 {stats['ocr_cached']}",
                f"- 指标公式文件：{stats['indicator_files']}",
                f"- 错误：{len(errors)}",
                "",
                "## 已知边界",
                "",
                "- 一篇文章的 .html/.mhtml/.docx/.pdf 视为 .md 的同源冗余，仅登记在 alternate_formats，不重复建块。",
                "- 正文里的 〔图〕 是图片占位符：该处原文是一张图，结论可能只存在于图中，需回看 image_map。",
                "- 图片扩展名与真实格式不一致（大量 PNG 命名为 .jpg），image_map 的 real_format 记录按文件头判定的真实格式。",
                "- 留言区读者发言未入库，只保留作者本人回复及其对应的提问。",
                "- .tn6（通达信指标二进制）与 .mp4 未解析。",
            ]
        )
        + "\n",
    )

    source["status"] = "integrated" if not errors else "integrated_with_warnings"
    source["adapter"] = "specialized_boduanzhimen"
    source["pipeline"] = "wechat_md_plus_paid_pdf"
    source["primary_locator"] = "date_section_image"
    source["last_import_summary"] = {
        "documents": len(documents),
        "chunks": len(chunks),
        "errors": len(errors),
    }
    source.pop("review_required", None)
    source.pop("complexity_signals", None)
    source.pop("format_counts", None)
    # 调试跑（--out-dir）不回写全局配置：那份 last_import_summary 会记成只导了 N 篇，
    # 后续任何读 sources.yaml 的脚本都会被这个假数字骗到
    if args.out_dir:
        print(f"[--out-dir] 产物写到 {lib}，未改写 {CONFIG.name}")
    else:
        update_last_import_summary(source["last_import_summary"])
    write_text_lf(lib / "source.yaml", yaml.safe_dump(source, allow_unicode=True, sort_keys=False))

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
