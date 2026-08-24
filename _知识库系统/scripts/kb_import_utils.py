#!/usr/bin/env python3
"""Shared helpers for read-only trading knowledge-base importers."""

from __future__ import annotations

import difflib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from functools import lru_cache
from pathlib import Path

# Pillow is imported inside ocr_images(), not here. This module also holds the LF writing
# helpers, which the report builders (build_manifest, build_cross_source, validate_kb) and
# register_source import — none of them touch OCR. A module level ``from PIL import ...``
# made those four unimportable on any interpreter without Pillow installed, so a pure
# stdlib task failed on a dependency it never used.


ROOT = Path(__file__).resolve().parents[2]
SYSTEM = ROOT / "_知识库系统"
TEMP_ROOT = SYSTEM / "tmp" / "batch-ocr"
BATCH_OCR_SCRIPT = Path(__file__).with_name("windows_ocr_batch.ps1")

CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
TOPIC_ALIASES = {
    "情绪周期": ("情绪周期", "周期", "退潮", "修复", "冰点", "拐点", "转点", "情绪流"),
    "龙头与核心": ("龙头", "核心票", "人气核心", "空间龙", "核心"),
    "题材与板块": ("主线", "题材", "板块", "催化", "产业趋势"),
    "预期与超预期": ("预期", "超预期", "低于预期", "符合预期"),
    "竞价与盘口": ("竞价", "竟价", "盘口", "承接", "抛压", "封单", "点位"),
    "筹码与量价": ("筹码", "量价", "换手", "断层", "成交量"),
    "打板与接力": ("打板", "接力", "连板", "首板", "一进二"),
    "低吸与半路": ("低吸", "半路", "反包", "弱转强"),
    "趋势与容量": ("趋势", "容量", "大票", "机构", "量化"),
    "仓位与回撤": ("仓位", "分仓", "满仓", "回撤", "风控"),
    "卖点与退出": ("卖点", "止损", "兑现", "退出", "格局", "去弱留强"),
    "复盘与计划": ("复盘", "计划", "推演", "预案", "看盘"),
    "心态与系统": ("心态", "执行力", "模式", "交易系统", "管住手", "体系", "心法"),
}


def natural_key(value: str) -> list[object]:
    """Sort numbered filenames naturally, including 2 before 10."""
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def clean_text(text: str, *, ocr: bool = False) -> str:
    """Normalize extracted text while preserving useful paragraph boundaries."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\ufeff", "").replace("\u200b", "").replace("\ufffd", "")
    # SQLite compares GLOB and LIKE with C-string semantics and stops at the first NUL,
    # so one stray NUL hides every character after it in that chunk from retrieval:
    # nanjinglu-92154afd0e2c-p008-c05 carried a NUL at character 5 and its search term at
    # character 44, which made the chunk unfindable. NUL is never meaningful prose here --
    # it arrives only inside the byte soup a broken PDF font extracts from a damaged layer.
    text = text.replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if ocr:
        # Windows OCR often inserts spaces between every Chinese character.
        text = re.sub(fr"(?<=[{CJK}])\s+(?=[{CJK}])", "", text)
        text = re.sub(fr"(?<=[{CJK}])\s+(?=[，。！？；：、）》】])", "", text)
        text = re.sub(fr"(?<=[（《【])\s+(?=[{CJK}])", "", text)
    text = re.sub(r"[\t\v\f]+", " ", text)
    text = re.sub(r"[ \u3000]{2,}", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def meaningful_char_count(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def cjk_ratio(text: str) -> float:
    """Share of CJK characters among non-whitespace characters.

    A PDF text layer with a broken font maps glyphs to arbitrary code points, so it
    extracts as mostly non-CJK bytes (``_|\\笉颫\\x17{y``). Chinese prose scores well
    above 0.5, damaged text layers well below it, which separates the two reliably
    even when the damaged layer reports a healthy character count.
    """
    stripped = re.sub(r"\s+", "", text or "")
    if not stripped:
        return 0.0
    return len(re.findall(f"[{CJK}]", stripped)) / len(stripped)


def subtract_known_text(ocr_text: str, embedded_text: str, *, min_line_chars: int = 6) -> str:
    """Drop OCR lines that merely re-read text already captured by the text layer.

    Rendering a whole PDF page OCRs both the article prose and any screenshots pasted
    into it. The prose is already available — and cleaner — from the text layer, so
    only the screenshot-only lines are worth keeping as a separate low-confidence
    block. Matching is fuzzy because OCR misreads characters (赚 as 賺) that would
    defeat exact comparison.

    Short lines are always kept: they carry too little signal to match reliably, and
    dropping them risks losing real screenshot content.
    """
    if not ocr_text:
        return ""
    if not embedded_text:
        return ocr_text
    # Index the text layer as overlapping character n-grams so a sentence can be
    # recognised regardless of how OCR happened to break lines.
    window = 8
    reference = re.sub(r"\s+", "", embedded_text)
    known = {reference[start:start + window] for start in range(max(1, len(reference) - window + 1))}

    def already_known(sentence: str) -> bool:
        compact = re.sub(r"\s+", "", sentence)
        if len(compact) < window:
            return False
        grams = [compact[start:start + window] for start in range(len(compact) - window + 1)]
        hits = sum(gram in known for gram in grams)
        # OCR misreads a few characters per sentence, which breaks some n-grams but
        # leaves most intact; a clear majority of matches means it is a re-read.
        return hits / len(grams) >= 0.6

    kept: list[str] = []
    for sentence in re.split(r"(?<=[。！？；!?;\n])", ocr_text):
        stripped = sentence.strip()
        if not stripped:
            continue
        if meaningful_char_count(stripped) < min_line_chars or not already_known(stripped):
            kept.append(stripped)
    return clean_text("\n".join(kept), ocr=True)


def extract_page_images(
    page,
    destination: Path,
    prefix: str,
    *,
    min_dimension: int = 100,
    min_pixels: int = 40000,
    target_width: int = 1600,
) -> list[dict]:
    """Write a PDF page's embedded image objects to disk for OCR, skipping icons.

    Why this exists instead of rendering the whole page: these article PDFs paste
    market screenshots, and rendering the page re-samples them twice — once into the
    page raster, once by whatever scale the page uses — while OCRing the objects
    directly re-samples at most once. A second benefit is that prose and screenshots
    arrive pre-separated, so the ``subtract_known_text`` n-gram pass is not needed
    for these pages at all.

    ``target_width`` is the part that took two attempts to get right. Extracting at
    native resolution alone made things *worse* on a third of the pages: a per-page
    comparison over the 103 comparable screenshot pages of this source scored 34
    pages worse against 20 better. The split was purely by image width — pages that
    regressed carried 660-666px screenshots, pages that improved carried 1080px ones.
    A 665px image placed on a page that renders 1157px wide was being enlarged by the
    old whole-page path, and Windows OCR needs that size to resolve dense Chinese
    glyphs. Upscaling narrow images to 1600px recovered it (in-vocabulary character
    rate 57.4% -> 89.5% on the regressed pages) and cost nothing on wide ones
    (86.0% -> 88.2%), so it is applied unconditionally below the threshold.

    Icons are filtered by size: WeChat exports embed 64x64 avatars and decorations
    that only ever yield OCR noise. Both a minimum edge and a minimum area are
    checked, because a legitimate one-line screenshot can be short (1080x120)
    while still being wide.
    """
    from PIL import Image

    destination.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    try:
        embedded = list(page.images)
    except Exception as exc:
        # A damaged image dictionary must not abort the page: the text layer and the
        # whole-page fallback are still usable, so this is recorded and skipped.
        return [{"error": f"page_images_failed: {exc}"}]

    for index, image in enumerate(embedded, start=1):
        name = getattr(image, "name", f"img{index}")
        try:
            data = image.data
        except Exception as exc:
            results.append({"error": f"image_data_failed: {exc}", "name": name})
            continue
        path = destination / f"{prefix}-img{index:03d}.png"
        try:
            with Image.open(io.BytesIO(data)) as opened:
                width, height = opened.size
                if min(width, height) < min_dimension or width * height < min_pixels:
                    continue
                # Normalise to PNG/RGB so the OCR batch gets one predictable format
                # regardless of whether the source object was JPEG, PNG or CMYK.
                image = opened.convert("RGB")
                scaled_to = 0
                if target_width and width < target_width:
                    ratio = target_width / width
                    image = image.resize(
                        (target_width, max(1, round(height * ratio))), Image.LANCZOS
                    )
                    scaled_to = target_width
                image.save(path, format="PNG")
        except Exception as exc:
            results.append({"error": f"image_decode_failed: {exc}", "name": name})
            continue
        results.append({
            "path": path, "name": name, "width": width, "height": height,
            "scaled_to": scaled_to,
        })
    return results


def text_layer_is_usable(text: str, *, min_chars: int = 50, min_cjk_ratio: float = 0.5) -> bool:
    """Whether a PDF text layer can be used directly instead of running OCR.

    Rendering and OCRing a page that already carries good text costs time and
    introduces recognition errors, so OCR is reserved for pages whose text layer is
    empty, too short to be a real page, or corrupted by font-encoding problems.
    """
    return meaningful_char_count(text) >= min_chars and cjk_ratio(text) >= min_cjk_ratio


def infer_topics(title: str, text: str, limit: int = 6) -> list[str]:
    haystack = f"{title}\n{text[:12000]}".casefold()
    scores: list[tuple[int, str]] = []
    for topic, aliases in TOPIC_ALIASES.items():
        score = sum(min(haystack.count(alias.casefold()), 8) for alias in aliases)
        if score:
            scores.append((score, topic))
    return [topic for _, topic in sorted(scores, key=lambda item: (-item[0], item[1]))[:limit]]


def extract_date(*values: str) -> str:
    """Extract the first plausible YYYY-MM-DD date from paths or titles."""
    full_patterns = (
        r"(?<!\d)(20\d{2})\s*[年./_-]\s*(\d{1,2})\s*(?:月|[./_-])?\s*(\d{1,2})\s*日?",
        r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)",
    )
    short_patterns = (
        r"(?<!\d)(\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?",
        r"(?<!\d)(\d{2})[./_-](\d{1,2})[./_-](\d{1,2})(?!\d)",
    )
    # Preserve caller priority: a short date in a filename/title is usually
    # more trustworthy than an OCR date found later in the body.
    for value in (value for value in values if value):
        for pattern in full_patterns:
            match = re.search(pattern, value)
            if match:
                year, month, day = map(int, match.groups())
                if 1 <= month <= 12 and 1 <= day <= 31:
                    return f"{year:04d}-{month:02d}-{day:02d}"
        for pattern in short_patterns:
            match = re.search(pattern, value)
            if match:
                year, month, day = map(int, match.groups())
                year += 2000
                if 1 <= month <= 12 and 1 <= day <= 31:
                    return f"{year:04d}-{month:02d}-{day:02d}"
    return ""


def clean_title(path: Path) -> str:
    title = path.stem.strip(" .-_—")
    title = re.sub(r"^20\d{2}[-_.年]\d{1,2}[-_.月]\d{1,2}日?\s*", "", title)
    title = re.sub(r"^\d{1,2}[-_.月]\d{1,2}日?\s*", "", title)
    return title.strip(" .-_—") or path.stem


def write_text_lf(path: Path, text: str) -> None:
    """Write UTF-8 with LF line endings, whatever platform this runs on.

    ``Path.write_text(..., encoding="utf-8")`` leaves ``newline=None``, which on Windows
    translates every ``\\n`` to ``\\r\\n``. The generated products then carry CRLF while the
    hand-written sources are LF, ``git diff --check`` reports the ``\\r`` as trailing
    whitespace on every changed line, and the diffs are unreadable. Committed products are
    text meant to be diffed, so they are pinned to LF at the point of writing — fixing it
    after the fact would just come back on the next rebuild.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def write_jsonl(path: Path, records: Iterable[dict]) -> None:
    write_text_lf(
        path,
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
    )


def split_text(text: str, target_chars: int) -> list[str]:
    """Split prose at paragraph/sentence boundaries without dropping content."""
    text = clean_text(text)
    if not text:
        return []
    paragraphs = [part.strip() for part in re.split(r"\n+", text) if part.strip()]
    pieces: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= target_chars:
            pieces.append(paragraph)
            continue
        sentences = [part for part in re.split(r"(?<=[。！？；!?;])", paragraph) if part]
        current = ""
        for sentence in sentences:
            if current and len(current) + len(sentence) > target_chars:
                pieces.append(current.strip())
                current = ""
            if len(sentence) > target_chars:
                if current:
                    pieces.append(current.strip())
                    current = ""
                pieces.extend(sentence[index:index + target_chars] for index in range(0, len(sentence), target_chars))
            else:
                current += sentence
        if current.strip():
            pieces.append(current.strip())
    groups: list[str] = []
    current_parts: list[str] = []
    length = 0
    for piece in pieces:
        if current_parts and length + len(piece) > target_chars:
            groups.append("\n".join(current_parts))
            current_parts, length = [], 0
        current_parts.append(piece)
        length += len(piece)
    if current_parts:
        groups.append("\n".join(current_parts))
    return groups


def span_locator(head: str, tail: str) -> str:
    """Join two locators into one range, flattening locators that are already ranges.

    Merged units carry ranges like ``正文第1段—正文第9段``. Naively joining those
    again produces four-part strings such as ``正文第1段—正文第9段—正文第11段—正文第20段``,
    so only the outermost endpoints are kept.
    """
    start = (head or "").split("—")[0]
    end = (tail or "").split("—")[-1]
    if not start:
        return end
    if not end or start == end:
        return start
    return f"{start}—{end}"


def _looks_like_heading(text: str) -> bool:
    """A short standalone line that titles the paragraphs after it."""
    stripped = text.strip()
    if len(stripped) > 30:
        return False
    if re.match(r"^(第[一二三四五六七八九十百\d]+[章节讲部分篇]|[一二三四五六七八九十]+[、.．]|\d+[、.．)）])", stripped):
        return True
    # A short line without sentence-ending punctuation reads as a title.
    return bool(stripped) and not re.search(r"[。！？；!?;，,]$", stripped)


def merge_short_units(
    units: Sequence[dict],
    *,
    min_chars: int = 400,
    max_chars: int = 800,
    text_key: str = "text",
    locator_key: str = "locator",
    mergeable: "Callable[[dict], bool] | None" = None,
    join_with: str = "\n",
) -> list[dict]:
    """Merge consecutive short prose units so each chunk can stand on its own.

    Word documents often store one sentence per ``<w:p>``, which yields chunks too
    short to interpret. Units are accumulated until ``min_chars`` is reached and
    flushed before exceeding ``max_chars``; the merged locator becomes a range so
    citations still point back to the original paragraphs.

    ``mergeable`` marks units that must stay standalone (image OCR units, tables).
    A non-mergeable unit flushes the pending group and passes through untouched, so
    document order is always preserved.

    Headings start a new group only once the pending group has reached ``min_chars``.
    Below that the heading is absorbed rather than allowed to split the group: these
    course exports contain many short lines that merely look like titles, so breaking
    on them eagerly produces fragments instead of readable blocks.
    """
    if min_chars > max_chars:
        raise ValueError(f"min_chars ({min_chars}) must not exceed max_chars ({max_chars})")

    def is_mergeable(unit: dict) -> bool:
        return mergeable(unit) if mergeable else True

    merged: list[dict] = []
    pending: list[dict] = []

    def flush() -> None:
        if not pending:
            return
        if len(pending) == 1:
            merged.append(pending[0])
        else:
            first, last = pending[0], pending[-1]
            combined = {
                **first,
                text_key: join_with.join(unit.get(text_key, "") for unit in pending),
                locator_key: span_locator(first.get(locator_key, ""), last.get(locator_key, "")),
                "merged_unit_count": len(pending),
                "merged_locators": [unit.get(locator_key, "") for unit in pending],
            }
            # Keep the weakest confidence: a merged block is only as good as its
            # least reliable part.
            order = {"low": 0, "medium": 1, "high": 2}
            levels = [unit.get("confidence") for unit in pending if unit.get("confidence") in order]
            if levels:
                combined["confidence"] = min(levels, key=lambda level: order[level])
            merged.append(combined)
        pending.clear()

    for unit in units:
        if not is_mergeable(unit):
            flush()
            merged.append(unit)
            continue
        text = unit.get(text_key, "") or ""
        pending_length = sum(len(item.get(text_key, "") or "") for item in pending)
        # A heading belongs with the text it introduces, so break before it once the
        # pending group already meets the target length. Breaking earlier was measured
        # on tulip_garden and made things worse: a floor of min_chars // 3 cut the
        # median prose chunk from 428 to 174 characters, because most "headings" in
        # these course exports are ordinary short lines, not section titles.
        if pending and pending_length >= min_chars and _looks_like_heading(text):
            flush()
            pending_length = 0
        if pending and pending_length + len(text) > max_chars:
            flush()
        pending.append(unit)
        if sum(len(item.get(text_key, "") or "") for item in pending) >= min_chars:
            flush()
    flush()
    return merged


def _tile_boxes(width: int, height: int, max_dim: int, overlap: int):
    y = 0
    while y < height:
        bottom = min(y + max_dim, height)
        yield (0, y, width, bottom)
        if bottom >= height:
            break
        y = bottom - overlap


def _similar_line(left: str, right: str) -> bool:
    a = re.sub(r"\s+", "", left)
    b = re.sub(r"\s+", "", right)
    if not a or not b:
        return False
    if a == b or (len(a) >= 8 and (a in b or b in a)):
        return True
    return min(len(a), len(b)) >= 8 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.86


def _merge_tile_texts(texts: Sequence[str]) -> str:
    merged: list[str] = []
    for raw in texts:
        lines = [line.strip() for line in clean_text(raw, ocr=True).splitlines() if line.strip()]
        while merged and lines and any(_similar_line(lines[0], prior) for prior in merged[-3:]):
            lines.pop(0)
        merged.extend(lines)
    return clean_text("\n".join(merged), ocr=True)


def cjk_count(text: str) -> int:
    """汉字数（U+4E00–U+9FFF）。双引擎兜底与质量判定的公共口径。"""
    return sum(1 for ch in text if "一" <= ch <= "鿿")


def _prepare_tiles(
    batch: Sequence[tuple[str, Path]],
    work: Path,
    *,
    max_dimension: int,
    overlap: int,
) -> dict[str, list[Path]]:
    """把一批图按生产口径切成 PNG 分片，返回 key → 分片路径列表。

    预处理与引擎无关，两条 OCR 路径共用这一份：EXIF 转正、转 RGB、宽超过
    max_dimension 先等比缩、再竖向分片（_tile_boxes）。2026-08-24 从 ocr_images
    里抽出来——RapidOCR 通道进来时如果各写一份预处理，跑着跑着就会分叉。
    """
    from PIL import Image, ImageOps

    tile_map: dict[str, list[Path]] = {}
    for item_index, (key, image_path) in enumerate(batch, start=1):
        # 文件名必须含 key 的派生量：调用方可能逐条调本函数（item_index 恒为 1），
        # 同一个 work 目录里只靠序号会互相覆盖 —— 2026-08-24 课程讲义全变成同一页
        # 就是这么来的。key 清洗后截断到 48 字符，再拼整个 key 的短哈希兜底——
        # 截断可能撞（长路径同前缀），哈希不会。
        safe_key = re.sub(r"[^\w.-]", "_", str(key))[:48] or "img"
        key_tag = hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:8]
        tile_outputs: list[Path] = []
        with Image.open(image_path) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            if image.width > max_dimension:
                ratio = max_dimension / image.width
                image = image.resize((max_dimension, max(1, int(image.height * ratio))))
            for tile_index, box in enumerate(
                _tile_boxes(image.width, image.height, max_dimension, overlap), start=1
            ):
                tile_path = work / f"{item_index:03d}-{safe_key}-{key_tag}-tile-{tile_index:03d}.png"
                image.crop(box).save(tile_path, format="PNG")
                tile_outputs.append(tile_path)
        tile_map[key] = tile_outputs
    return tile_map


def _run_windows_tiles(
    tile_map: dict[str, list[Path]],
    work: Path,
    *,
    language: str,
) -> None:
    """调 windows_ocr_batch.ps1 把每个分片识别成同名 .txt。"""
    manifest_rows: list[dict] = []
    for key, tile_outputs in tile_map.items():
        for tile_path in tile_outputs:
            manifest_rows.append({
                "key": key,
                "image_path": str(tile_path),
                "output_path": str(tile_path.with_suffix(".txt")),
            })
    if not manifest_rows:
        return
    manifest_path = work / "manifest.json"
    write_text_lf(manifest_path, json.dumps(manifest_rows, ensure_ascii=False))
    command = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        str(BATCH_OCR_SCRIPT), "-ManifestPath", str(manifest_path), "-Language", language,
    ]
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())


def _run_rapid_tiles(tile_map: dict[str, list[Path]], *, language: str) -> None:
    """用 RapidOCR 就地识别每个分片，结果写成分片同名 .txt（与 Windows 路径同构）。

    RapidOCR 模型随包自带、纯 CPU 推理、不联网。``language`` 参数仅为保持与
    Windows 路径相同的调用形状，RapidOCR 的中英混模型不区分语言。
    """
    import numpy as np
    from PIL import Image

    engine = _rapid_engine()
    for tile_outputs in tile_map.values():
        for tile_path in tile_outputs:
            try:
                with Image.open(tile_path) as tile_image:
                    array = np.asarray(tile_image.convert("RGB"))
                result, _ = engine(array)
                lines = [line[1] for line in (result or [])]
                write_text_lf(tile_path.with_suffix(".txt"), "\n".join(lines))
            except Exception as exc:
                # 单分片失败不致命：留空 txt 让合并继续，错误打到 stderr 留痕。
                # （2026-08-24 审计 B 项：此前单片异常会炸掉整批导入）
                print(f"rapid 分片识别失败 {tile_path.name}: {exc}", file=sys.stderr)
                write_text_lf(tile_path.with_suffix(".txt"), "")


@lru_cache(maxsize=1)
def _rapid_engine():
    """进程内共享一个 RapidOCR 实例（模型加载约 1 秒，不能每个分片重建）。"""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:  # pragma: no cover - 环境缺包时的兜底提示
        raise SystemExit(
            "KB_OCR_ENGINE=rapid 但 rapidocr_onnxruntime 未安装。\n"
            "安装：pip install rapidocr_onnxruntime；或回退 Windows 引擎：set KB_OCR_ENGINE=windows"
        ) from exc
    return RapidOCR()


# OCR 引擎选择（2026-08-24 起）。默认 rapid：A/B 实测全面胜出 ——
#   南京路文本层页还原率 92.4% vs Windows 40.2%（_bench_ocr.py，同页同 DPI）
#   生产素材坏块率（孤立部首≥2）：K 线 3%→0%、郁金香截图 17%→0%、讲义 75%→0%
# 且对 DPI 不敏感。回退方式：环境变量 KB_OCR_ENGINE=windows。
OCR_ENGINE = os.environ.get("KB_OCR_ENGINE", "rapid").strip().lower()

# 双引擎兜底阈值（2026-08-24）。RapidOCR 在个别材质上会整页失灵——实测两种病：
#   ① 南京路《去弱留强》：6241 字正文读成字母数字乱汤（几乎无汉字）；
#   ② 郁金香《22截屏》美图截图：输出「写K不_TT / $7.eX生EX」式假汉字乱码，
#     汉字计数不低但拉丁/数字占比畸高，单看字数拦不住。
# 判据（满足任一即视为失灵）：合并文本汉字数 < FLOOR，或非空白字符里
# ASCII 字母数字占比 > LATinish 上限。失灵时用 Windows 引擎重跑同一组分片，
# 谁认出的汉字多留谁。代价只在失灵图上发生。缓存存择优结果，来源不再区分。
DUAL_ENGINE_FLOOR_CJK = 25
DUAL_ENGINE_MAX_LATINISH = 0.40


def _rapid_degenerate(text: str) -> bool:
    non_space = [c for c in text if not c.isspace()]
    if not non_space:
        return True
    if cjk_count(text) < DUAL_ENGINE_FLOOR_CJK:
        return True
    latinish = sum(1 for c in non_space if c.isascii() and c.isalnum())
    return latinish / len(non_space) > DUAL_ENGINE_MAX_LATINISH


def ocr_images(
    items: Sequence[tuple[str, Path]],
    *,
    language: str = "zh-Hans",
    max_dimension: int = 9000,
    overlap: int = 120,
    batch_size: int = 24,
    engine: str | None = None,
) -> dict[str, dict]:
    """OCR images and return text/error metadata（入口按引擎分流）。

    ``engine`` 缺省用全局 OCR_ENGINE；显式传 "dual" 时两台引擎都跑、逐图取
    汉字多者 —— 为「半读」而生：rapid 有时只认出页面一小角（几百字躲过一切
    失灵判据），实测南京路《题材是否抬头》18 个可信片段 0% 存留、郁金香四篇
    21-48%。代价是每张图都付两次识别，只给小体量、正文密集的来源用。

    预处理（分片）两引擎共用 _prepare_tiles；识别各自走 _run_windows_tiles /
    _run_rapid_tiles，输出统一为「每分片一个 .txt」再 _merge_tile_texts 合并，
    保证两条路径的可比性与可替换性。
    """
    chosen_engine = (engine or OCR_ENGINE).strip().lower()
    if chosen_engine not in ("rapid", "windows", "dual"):
        raise ValueError(f"未知 OCR 引擎：{chosen_engine}")
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}
    for batch_start in range(0, len(items), batch_size):
        batch = items[batch_start:batch_start + batch_size]
        work = Path(tempfile.mkdtemp(prefix="ocr-", dir=TEMP_ROOT))
        try:
            prepared: dict[str, list[Path]] = {}
            for key, image_path in batch:
                try:
                    prepared[key] = _prepare_tiles(
                        [(key, image_path)], work,
                        max_dimension=max_dimension, overlap=overlap,
                    )[key]
                except Exception as exc:  # corrupt/unsupported images are recorded, not fatal
                    results[key] = {"text": "", "error": f"prepare_failed: {exc}", "tiles": 0}
            tile_map = {k: v for k, v in prepared.items() if v is not None}

            def read_key(key: str) -> tuple[str, int]:
                """读回该图全部分片文本并合并；返回 (合并文本, 缺失分片数)。"""
                texts, missing = [], 0
                for p in tile_map[key]:
                    txt_path = p.with_suffix(".txt")
                    if txt_path.exists():
                        texts.append(txt_path.read_text(encoding="utf-8"))
                    else:
                        missing += 1
                return _merge_tile_texts(texts), missing

            if chosen_engine == "windows":
                _run_windows_tiles(tile_map, work, language=language)
                for key in tile_map:
                    text, missing = read_key(key)
                    results[key] = {
                        "text": text,
                        "error": f"missing_tile_outputs: {missing}" if missing else "",
                        "tiles": len(tile_map[key]),
                    }
                continue

            _run_rapid_tiles(tile_map, language=language)
            rapid_merged = {key: read_key(key) for key in tile_map}

            if chosen_engine == "rapid":
                # 兜底：rapid 失灵（没字/拉丁汤）的图用 Windows 重跑——整批攒齐后
                # 一次 powershell 调用，不是每图一次（2026-08-24 审计 C 项）。
                retry_keys = {
                    key for key, (text, missing) in rapid_merged.items()
                    if not missing and _rapid_degenerate(text)
                }
                if retry_keys:
                    _run_windows_tiles(
                        {k: tile_map[k] for k in retry_keys}, work, language=language
                    )
                for key in tile_map:
                    merged, missing = rapid_merged[key]
                    if key in retry_keys:
                        alt, _ = read_key(key)
                        if cjk_count(alt) > cjk_count(merged):
                            merged = alt
                    results[key] = {
                        "text": merged,
                        "error": f"missing_tile_outputs: {missing}" if missing else "",
                        "tiles": len(tile_map[key]),
                    }
                continue

            # dual：Windows 再跑一遍全部图，逐图取汉字多者
            _run_windows_tiles(tile_map, work, language=language)
            for key in tile_map:
                win_text, missing = read_key(key)
                rap_text, _ = rapid_merged[key]
                merged = win_text if cjk_count(win_text) > cjk_count(rap_text) else rap_text
                results[key] = {
                    "text": merged,
                    "error": f"missing_tile_outputs: {missing}" if missing else "",
                    "tiles": len(tile_map[key]),
                }
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return results
