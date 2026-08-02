#!/usr/bin/env python3
"""Shared helpers for read-only trading knowledge-base importers."""

from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from PIL import Image, ImageOps


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


def write_jsonl(path: Path, records: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
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


def ocr_images(
    items: Sequence[tuple[str, Path]],
    *,
    language: str = "zh-Hans",
    max_dimension: int = 9000,
    overlap: int = 120,
    batch_size: int = 24,
) -> dict[str, dict]:
    """OCR images in persistent PowerShell batches and return text/error metadata."""
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}
    for batch_start in range(0, len(items), batch_size):
        batch = items[batch_start:batch_start + batch_size]
        work = Path(tempfile.mkdtemp(prefix="ocr-", dir=TEMP_ROOT))
        manifest_rows: list[dict] = []
        tile_map: dict[str, list[Path]] = {}
        try:
            for item_index, (key, image_path) in enumerate(batch, start=1):
                tile_outputs: list[Path] = []
                try:
                    with Image.open(image_path) as opened:
                        image = ImageOps.exif_transpose(opened).convert("RGB")
                        if image.width > max_dimension:
                            ratio = max_dimension / image.width
                            image = image.resize((max_dimension, max(1, int(image.height * ratio))))
                        for tile_index, box in enumerate(
                            _tile_boxes(image.width, image.height, max_dimension, overlap), start=1
                        ):
                            tile_path = work / f"image-{item_index:03d}-tile-{tile_index:03d}.png"
                            output_path = tile_path.with_suffix(".txt")
                            image.crop(box).save(tile_path, format="PNG")
                            tile_outputs.append(output_path)
                            manifest_rows.append(
                                {"key": key, "image_path": str(tile_path), "output_path": str(output_path)}
                            )
                    tile_map[key] = tile_outputs
                except Exception as exc:  # corrupt/unsupported images are recorded, not fatal
                    results[key] = {"text": "", "error": f"prepare_failed: {exc}", "tiles": 0}
            if manifest_rows:
                manifest_path = work / "manifest.json"
                manifest_path.write_text(json.dumps(manifest_rows, ensure_ascii=False), encoding="utf-8")
                command = [
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(BATCH_OCR_SCRIPT), "-ManifestPath", str(manifest_path), "-Language", language,
                ]
                completed = subprocess.run(
                    command, capture_output=True, text=True, encoding="utf-8", errors="replace"
                )
                if completed.returncode != 0:
                    raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
            for key, output_paths in tile_map.items():
                tile_texts = [path.read_text(encoding="utf-8") for path in output_paths if path.exists()]
                if len(tile_texts) != len(output_paths):
                    results[key] = {
                        "text": _merge_tile_texts(tile_texts),
                        "error": f"missing_tile_outputs: {len(output_paths) - len(tile_texts)}",
                        "tiles": len(output_paths),
                    }
                else:
                    results[key] = {"text": _merge_tile_texts(tile_texts), "error": "", "tiles": len(output_paths)}
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return results
