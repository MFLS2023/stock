#!/usr/bin/env python3
"""Import Tulip Garden Word courses, ordered screenshots, Markdown, and related PDF."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

from kb_import_utils import (
    ROOT,
    clean_text,
    clean_title,
    extract_date,
    infer_topics,
    meaningful_char_count,
    merge_short_units,
    natural_key,
    ocr_images,
    span_locator,
    split_text,
    write_jsonl,
    write_text_lf,
)


SOURCE_ID = "tulip_garden"
SOURCE_NAME = "郁金香花园"
SOURCE_ROOT = ROOT / "郁金香花园付费文章文档版"
LIB = ROOT / "_知识库系统" / "source_libraries" / SOURCE_ID
CONVERTER = Path(__file__).with_name("convert_doc_to_docx.ps1")

# Screenshots below this width are enlarged before OCR. Windows OCR resolves dense
# Chinese glyphs by pixel size, not by how legible the image looks to a human, so a
# narrow screenshot loses strokes that a wider copy of the same picture keeps.
#
# Unlike the nanjinglu source — where this same change moved narrow pages from 57%
# to 89% in-vocabulary characters — the gain here is small, because these images are
# already 1080px wide at the median. Measured on 42 sampled images across three width
# bands: 93.4% -> 94.8% overall, with the widest gain on the <800px band
# (88.2% -> 89.9%, and 812 -> 956 characters read). Kept because the cost is one
# resize per cached image, but it is an optimisation rather than a repair.
OCR_TARGET_WIDTH = 2000
# Bumped when the OCR input changes shape, so existing per-image caches are recomputed
# rather than silently mixing pre- and post-upscale text.
# v3（2026-08-24）：引擎 Windows OCR → RapidOCR。A/B 实测坏块率 17%→0%
# （_bench_engine_ab.py --retulip，生产同款分片），CJK 率持平、界面垃圾更少。
# v4（2026-08-24 同日）：全量对比发现 rapid 在《22截屏》等美图截图上输出
# 「写K不_TT」式假汉字乱码（旧版这些页是通顺正文，六个文档共丢约 4 万汉字）。
# kb_import_utils 兜底判据升级后重跑：失灵图自动回退 Windows 择优。
# v5（2026-08-24 同日）：v4 复测四篇受灾文档保留率仅 21-48%——「半读」躲过了
# 失灵判据。改 engine="dual" 全量双引擎逐图取多者，不再依赖判据。
OCR_CACHE_VERSION = 5

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
}
W = "{" + NS["w"] + "}"
R = "{" + NS["r"] + "}"
A = "{" + NS["a"] + "}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_group_hash(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(SOURCE_ROOT).as_posix().encode("utf-8"))
        digest.update(sha256_file(path).encode("ascii"))
    return digest.hexdigest()


def convert_doc(path: Path, source_hash: str, force: bool) -> tuple[Path | None, str]:
    target = LIB / "converted_docx" / f"{path.stem}.docx"
    sidecar = target.with_suffix(".source.json")
    if not force and target.exists() and sidecar.exists():
        try:
            cache = json.loads(sidecar.read_text(encoding="utf-8"))
            if cache.get("source_hash") == source_hash:
                return target, ""
        except (OSError, json.JSONDecodeError):
            pass
    command = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(CONVERTER),
        "-InputPath", str(path), "-OutputPath", str(target),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0 or not target.exists():
        return None, completed.stderr.strip() or completed.stdout.strip() or "Word conversion failed"
    write_text_lf(
        sidecar,
        json.dumps({"source_hash": source_hash, "source_path": str(path)}, ensure_ascii=False, indent=2)
        + "\n",
    )
    return target, ""


def relationship_map(archive: zipfile.ZipFile) -> dict[str, str]:
    name = "word/_rels/document.xml.rels"
    if name not in archive.namelist():
        return {}
    root = ET.fromstring(archive.read(name))
    return {
        item.attrib.get("Id", ""): item.attrib.get("Target", "")
        for item in root.findall("pr:Relationship", NS)
    }


def node_tokens(node: ET.Element, relationships: dict[str, str]) -> tuple[str, list[str]]:
    tokens: list[str] = []
    images: list[str] = []
    for item in node.iter():
        if item.tag in {W + "t", W + "delText", W + "instrText"} and item.text:
            tokens.append(item.text)
        elif item.tag == W + "tab":
            tokens.append("\t")
        elif item.tag in {W + "br", W + "cr"}:
            tokens.append("\n")
        elif item.tag == A + "blip":
            relation_id = item.attrib.get(R + "embed")
            target = relationships.get(relation_id or "", "")
            if target:
                images.append(target)
                tokens.append(f" [内嵌图片:{PurePosixPath(target).name}] ")
    return clean_text("".join(tokens)), images


def extract_auxiliary_xml(archive: zipfile.ZipFile, pattern: re.Pattern[str], label: str) -> list[dict]:
    units: list[dict] = []
    for name in sorted((item for item in archive.namelist() if pattern.fullmatch(item)), key=natural_key):
        try:
            root = ET.fromstring(archive.read(name))
            text = clean_text("".join((item.text or "") for item in root.iter() if item.tag in {W + "t", W + "delText"}))
        except ET.ParseError:
            continue
        if text:
            units.append({"locator": f"{label}:{PurePosixPath(name).name}", "text": text, "method": "docx_xml", "confidence": "high"})
    return units


def extract_docx(path: Path, doc_id: str) -> tuple[list[dict], list[tuple[str, Path]], list[dict]]:
    units: list[dict] = []
    media_items: list[tuple[str, Path]] = []
    errors: list[dict] = []
    asset_dir = LIB / "assets" / doc_id
    asset_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as archive:
        relationships = relationship_map(archive)
        media_names = [name for name in archive.namelist() if name.startswith("word/media/") and not name.endswith("/")]
        for name in media_names:
            target = asset_dir / PurePosixPath(name).name
            target.write_bytes(archive.read(name))
        try:
            document = ET.fromstring(archive.read("word/document.xml"))
        except (KeyError, ET.ParseError) as exc:
            return [], [], [{"document_id": doc_id, "stage": "docx_xml", "error": str(exc)}]
        body = document.find("w:body", NS)
        paragraph_number = 0
        table_number = 0
        if body is not None:
            for child in body:
                if child.tag == W + "p":
                    paragraph_number += 1
                    text, images = node_tokens(child, relationships)
                    if text and not (text.startswith("[内嵌图片:") and text.endswith("]")):
                        units.append({
                            "locator": f"正文第{paragraph_number}段", "text": text,
                            "method": "docx_xml", "confidence": "high",
                        })
                    for target in images:
                        media_path = asset_dir / PurePosixPath(target).name
                        if media_path.exists():
                            key = f"{doc_id}|{media_path.name}"
                            units.append({
                                "locator": f"正文第{paragraph_number}段内嵌图片:{media_path.name}", "text": "",
                                "method": "ocr", "confidence": "medium", "image_key": key,
                                "image_path": str(media_path),
                            })
                            media_items.append((key, media_path))
                elif child.tag == W + "tbl":
                    table_number += 1
                    rows: list[str] = []
                    table_images: list[str] = []
                    for row in child.findall(".//w:tr", NS):
                        cells: list[str] = []
                        for cell in row.findall("w:tc", NS):
                            cell_text, images = node_tokens(cell, relationships)
                            cells.append(cell_text)
                            table_images.extend(images)
                        rows.append(" | ".join(cells))
                    table_text = clean_text("\n".join(rows))
                    if table_text:
                        units.append({
                            "locator": f"表格{table_number}", "text": table_text,
                            "method": "docx_table", "confidence": "high",
                        })
                    for target in table_images:
                        media_path = asset_dir / PurePosixPath(target).name
                        if media_path.exists():
                            key = f"{doc_id}|{media_path.name}"
                            units.append({
                                "locator": f"表格{table_number}内嵌图片:{media_path.name}", "text": "",
                                "method": "ocr", "confidence": "medium", "image_key": key,
                                "image_path": str(media_path),
                            })
                            media_items.append((key, media_path))
        units.extend(extract_auxiliary_xml(archive, re.compile(r"word/header\d+\.xml"), "页眉"))
        units.extend(extract_auxiliary_xml(archive, re.compile(r"word/footer\d+\.xml"), "页脚"))
        units.extend(extract_auxiliary_xml(archive, re.compile(r"word/(comments|footnotes|endnotes)\.xml"), "附注"))
    # A relationship can appear more than once; OCR each physical image only once.
    deduped = list(dict(media_items).items())
    return units, [(key, path) for key, path in deduped], errors


def trailing_sequence(stem: str) -> tuple[str, int | None]:
    match = re.match(r"^(.*?)[-_ ](\d{1,3})$", stem)
    return (match.group(1).strip(), int(match.group(2))) if match else (stem, None)


def group_external_images() -> list[dict]:
    images = [path for path in SOURCE_ROOT.rglob("*") if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}]
    groups: dict[str, list[Path]] = defaultdict(list)
    titles: dict[str, str] = {}
    old_root = SOURCE_ROOT / "郁金香老号"
    direct_old = [path for path in images if path.parent == old_root]
    candidate_counts: Counter[str] = Counter(trailing_sequence(path.stem)[0] for path in direct_old)
    for path in images:
        if path.parent != old_root:
            key = path.parent.relative_to(SOURCE_ROOT).as_posix()
            title = path.parent.name
        else:
            candidate, sequence = trailing_sequence(path.stem)
            title = candidate if sequence is not None and candidate_counts[candidate] > 1 else path.stem
            key = f"郁金香老号/{title}"
        groups[key].append(path)
        titles[key] = title
    result: list[dict] = []
    for key, paths in groups.items():
        def image_sort(path: Path):
            _, sequence = trailing_sequence(path.stem)
            return (sequence if sequence is not None else 10**9, natural_key(path.name))
        paths.sort(key=image_sort)
        result.append({"key": key, "title": titles[key], "paths": paths})
    return sorted(result, key=lambda item: natural_key(item["key"]))


def upscale_for_ocr(path: Path, work: Path) -> tuple[Path, int]:
    """Return an OCR-ready copy of an image, widened to OCR_TARGET_WIDTH if narrower.

    Returns the original path unchanged when no resize is needed, so wide images cost
    nothing. A failure to decode is not fatal: the original is handed to OCR, which
    records its own error for that image.
    """
    from PIL import Image

    try:
        with Image.open(path) as opened:
            width, height = opened.size
            if width >= OCR_TARGET_WIDTH:
                return path, 0
            ratio = OCR_TARGET_WIDTH / width
            work.mkdir(parents=True, exist_ok=True)
            target = work / f"{sha256_file(path)[:16]}.png"
            if not target.exists():
                opened.convert("RGB").resize(
                    (OCR_TARGET_WIDTH, max(1, round(height * ratio))), Image.LANCZOS
                ).save(target, format="PNG")
            return target, OCR_TARGET_WIDTH
    except Exception:
        return path, 0


def cache_ocr(items: list[tuple[str, Path]], force: bool) -> tuple[dict[str, dict], list[dict]]:
    cache_dir = LIB / "image_ocr_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    work_dir = ROOT / "_知识库系统" / "tmp" / "tulip-upscaled"
    results: dict[str, dict] = {}
    missing: list[tuple[str, Path]] = []
    key_hashes: dict[str, str] = {}
    for key, path in items:
        digest = sha256_file(path)
        key_hashes[key] = digest
        cache_path = cache_dir / f"{digest}.json"
        if not force and cache_path.exists():
            try:
                item = json.loads(cache_path.read_text(encoding="utf-8"))
                # The version check is what makes the upscale take effect: caches
                # written before it carry text OCRed from the un-enlarged image.
                if (
                    item.get("source_hash") == digest
                    and item.get("ocr_cache_version") == OCR_CACHE_VERSION
                ):
                    results[key] = item
                    continue
            except (OSError, json.JSONDecodeError):
                pass
        missing.append((key, path))
    errors: list[dict] = []
    if missing:
        # OCR reads the enlarged copy; the cache still records the original path so the
        # citation and any re-check point at the real asset, not a temporary file.
        prepared: list[tuple[str, Path]] = []
        scaled: dict[str, int] = {}
        for key, path in missing:
            ocr_path, scaled_to = upscale_for_ocr(path, work_dir)
            prepared.append((key, ocr_path))
            scaled[key] = scaled_to
        # dual：全量对比发现 rapid 在部分美图长截图上「半读」（21-48% 真内容丢失），
        # 失灵判据拦不住半读，只能两台引擎都跑逐图取汉字多者（2026-08-24）
        fresh = ocr_images(prepared, batch_size=20, engine="dual")
        for key, path in missing:
            raw = fresh.get(key, {"text": "", "error": "no_result", "tiles": 0})
            item = {
                "source_hash": key_hashes[key], "source_path": str(path),
                "ocr_cache_version": OCR_CACHE_VERSION, "scaled_to": scaled[key],
                "text": clean_text(raw.get("text", ""), ocr=True), "error": raw.get("error", ""),
                "tiles": raw.get("tiles", 0),
            }
            write_text_lf(
                cache_dir / f"{key_hashes[key]}.json",
                json.dumps(item, ensure_ascii=False, indent=2) + "\n",
            )
            results[key] = item
            if item["error"]:
                errors.append({"image": str(path), "stage": "ocr", "error": item["error"]})
    return results, errors


def group_units(units: list[dict], target_chars: int) -> list[list[dict]]:
    groups: list[list[dict]] = []
    current: list[dict] = []
    length = 0
    for unit in units:
        if current and length + len(unit["text"]) > target_chars:
            groups.append(current)
            current, length = [], 0
        current.append(unit)
        length += len(unit["text"])
    if current:
        groups.append(current)
    return groups


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force-ocr", action="store_true")
    parser.add_argument("--force-convert", action="store_true")
    parser.add_argument(
        "--chunk-min-chars", type=int, default=400,
        help="Merge consecutive Word paragraphs until a chunk reaches this length",
    )
    parser.add_argument(
        "--chunk-max-chars", type=int, default=800,
        help="Flush a merged chunk before it exceeds this length",
    )
    args = parser.parse_args()
    for name in ("texts", "assets", "converted_docx", "image_ocr_cache", "maps"):
        (LIB / name).mkdir(parents=True, exist_ok=True)

    errors: list[dict] = []
    specs: list[dict] = []
    all_ocr_items: list[tuple[str, Path]] = []
    word_files = sorted(
        (path for path in SOURCE_ROOT.iterdir() if path.is_file() and path.suffix.lower() in {".doc", ".docx"}),
        key=lambda path: natural_key(path.name),
    )
    old_explanation = SOURCE_ROOT / "郁金香老号" / "郁金香老号说明.docx"
    if old_explanation.exists():
        word_files.append(old_explanation)
    for path in word_files:
        source_hash = sha256_file(path)
        doc_id = f"tulip-{source_hash[:12]}"
        extraction_path = path
        if path.suffix.lower() == ".doc":
            converted, error = convert_doc(path, source_hash, args.force_convert)
            if error or converted is None:
                errors.append({"document_id": doc_id, "stage": "doc_conversion", "error": error})
                specs.append({"doc_id": doc_id, "title": clean_title(path), "paths": [path], "units": [], "content_type": "word_course", "source_hash": source_hash})
                continue
            extraction_path = converted
        try:
            units, media_items, doc_errors = extract_docx(extraction_path, doc_id)
            errors.extend(doc_errors)
            all_ocr_items.extend(media_items)
        except Exception as exc:
            units = []
            errors.append({"document_id": doc_id, "stage": "docx", "error": str(exc)})
        specs.append({
            "doc_id": doc_id, "title": clean_title(path), "paths": [path], "units": units,
            "content_type": "word_course", "source_hash": source_hash,
        })

    md_path = SOURCE_ROOT / "郁金香老号" / "郁金香8.md"
    pdf_path = SOURCE_ROOT / "郁金香老号" / "郁金香8.pdf"
    if md_path.exists():
        source_hash = sha256_file(md_path)
        specs.append({
            "doc_id": f"tulip-{source_hash[:12]}", "title": clean_title(md_path),
            "paths": [md_path] + ([pdf_path] if pdf_path.exists() else []),
            "units": [{"locator": "Markdown正文", "text": clean_text(md_path.read_text(encoding="utf-8", errors="replace")), "method": "markdown", "confidence": "high"}],
            "content_type": "markdown_with_related_pdf", "source_hash": source_hash,
        })

    image_groups = group_external_images()
    for group in image_groups:
        paths = group["paths"]
        group_hash = stable_group_hash(paths)
        doc_id = f"tulip-{group_hash[:12]}"
        units = []
        for index, path in enumerate(paths, start=1):
            key = f"{doc_id}|{index:03d}|{path.name}"
            units.append({
                "locator": f"图片{index}/{len(paths)}:{path.name}", "text": "", "method": "ocr",
                "confidence": "medium", "image_key": key, "image_path": str(path),
            })
            all_ocr_items.append((key, path))
        specs.append({
            "doc_id": doc_id, "title": group["title"], "paths": paths, "units": units,
            "content_type": "ordered_screenshot_article", "source_hash": group_hash,
        })

    # OCR Word-embedded media and external screenshots in one reusable cached pass.
    unique_ocr_items = list(dict(all_ocr_items).items())
    ocr_results, ocr_errors = cache_ocr([(key, path) for key, path in unique_ocr_items], args.force_ocr)
    errors.extend(ocr_errors)

    document_rows: list[dict] = []
    parent_rows: list[dict] = []
    chunk_rows: list[dict] = []
    extraction_counts: Counter[str] = Counter()
    for spec in specs:
        expanded_units: list[dict] = []
        for unit in spec["units"]:
            if unit.get("image_key"):
                result = ocr_results.get(unit["image_key"], {})
                text = clean_text(result.get("text", ""), ocr=True)
                unit = {**unit, "text": text, "confidence": "medium" if text else "low", "ocr_error": result.get("error", "")}
            if not unit.get("text"):
                continue
            extraction_counts[unit.get("method", "unknown")] += 1
            for part in split_text(unit["text"], 1200):
                expanded_units.append({**unit, "text": part})
        # Word stores one sentence per <w:p>, so raw units are far too short to read
        # on their own. Merge consecutive prose units; image OCR units stay standalone
        # because each one is an independent retrieval target with its own image_path.
        expanded_units = merge_short_units(
            expanded_units,
            min_chars=args.chunk_min_chars,
            max_chars=args.chunk_max_chars,
            mergeable=lambda unit: not unit.get("image_key") and unit.get("method") == "docx_xml",
        )
        normalized_text = "\n\n".join(
            f"[{unit['locator']} | {unit.get('method', 'unknown')}]\n{unit['text']}" for unit in expanded_units
        )
        text_path = LIB / "texts" / f"{spec['doc_id']}.txt"
        write_text_lf(text_path, normalized_text + ("\n" if normalized_text else ""))
        path_values = [path.relative_to(SOURCE_ROOT).as_posix() for path in spec["paths"]]
        date = extract_date(spec["title"], *path_values, normalized_text[:1600])
        topics = infer_topics(spec["title"], normalized_text)
        has_ocr = any(unit.get("method") == "ocr" for unit in expanded_units)
        document_rows.append({
            "source_id": SOURCE_ID, "document_id": spec["doc_id"], "title": spec["title"], "date": date,
            "author_or_guest": "郁金香花园", "content_type": spec["content_type"], "topics": topics,
            "characters": meaningful_char_count(normalized_text), "unit_count": len(expanded_units),
            "original_path": str(spec["paths"][0]), "related_original_paths": [str(path) for path in spec["paths"][1:]],
            "normalized_text_path": str(text_path), "sha256": spec["source_hash"],
            "risk_flags": "图片OCR需回看原图" if has_ocr else "",
        })
        for parent_number, parent_group in enumerate(group_units(expanded_units, 4200), start=1):
            parent_id = f"{spec['doc_id']}-p{parent_number:03d}"
            locator = span_locator(parent_group[0]["locator"], parent_group[-1]["locator"])
            parent_rows.append({
                "source_id": SOURCE_ID, "document_id": spec["doc_id"], "parent_id": parent_id,
                "title": spec["title"], "date": date, "author_or_guest": "郁金香花园",
                "locator": locator, "text": "\n".join(unit["text"] for unit in parent_group),
            })
            for child_number, unit in enumerate(parent_group, start=1):
                chunk_rows.append({
                    "source_id": SOURCE_ID, "source_name": SOURCE_NAME, "document_id": spec["doc_id"],
                    "parent_id": parent_id, "chunk_id": f"{parent_id}-c{child_number:02d}",
                    "chunk_type": "course_text" if unit.get("method") != "ocr" else "image_ocr",
                    "title": spec["title"], "date": date, "author_or_guest": "郁金香花园", "topics": topics,
                    "claim_type": "opinion_or_case", "market_regime": "未标注", "locator": unit["locator"],
                    "text": unit["text"], "original_path": str(spec["paths"][0]),
                    "image_path": unit.get("image_path", ""), "confidence": unit.get("confidence", "medium"),
                    "extraction_method": unit.get("method", "unknown"),
                })

    write_jsonl(LIB / "documents.jsonl", document_rows)
    write_jsonl(LIB / "parents.jsonl", parent_rows)
    write_jsonl(LIB / "chunks.jsonl", chunk_rows)
    ordered = sorted(document_rows, key=lambda item: (item.get("date") or "9999", natural_key(item["title"])))
    content_map = ["# 郁金香花园内容地图", "", "| 日期 | 单元 | 类型 | 主题 | 字符数 | 文档ID |", "|---|---|---|---|---:|---|"]
    for item in ordered:
        content_map.append(
            f"| {item.get('date') or '未标注'} | {item['title'].replace('|', '｜')} | {item['content_type']} | "
            f"{'、'.join(item.get('topics') or []) or '未标注'} | {item['characters']} | {item['document_id']} |"
        )
    write_text_lf(LIB / "content_map.md", "\n".join(content_map) + "\n")
    image_groups_map = ["# 郁金香花园图片分组", "", "> 图片按父文件夹、共同文件名前缀和尾部序号恢复顺序。", ""]
    for group in image_groups:
        image_groups_map.append(f"- {group['title']}：{len(group['paths'])} 张")
        for path in group["paths"]:
            image_groups_map.append(f"  - {path.relative_to(SOURCE_ROOT).as_posix()}")
    write_text_lf(LIB / "image_groups.md", "\n".join(image_groups_map) + "\n")
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "source_id": SOURCE_ID,
        "physical_files": sum(1 for path in SOURCE_ROOT.rglob("*") if path.is_file()),
        "documents": len(document_rows), "word_documents": len(word_files), "image_groups": len(image_groups),
        "external_images": sum(len(group["paths"]) for group in image_groups),
        "ocr_images_total": len(unique_ocr_items), "parents": len(parent_rows), "chunks": len(chunk_rows),
        "characters": sum(item["characters"] for item in document_rows),
        "extraction_counts": dict(extraction_counts), "errors": errors,
    }
    write_text_lf(LIB / "source_summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    quality = [
        "# 郁金香花园导入质量报告", "", f"- 原始物理文件：{summary['physical_files']}",
        f"- 结构化课程/文章单元：{len(document_rows)}", f"- Word 文档：{len(word_files)}",
        f"- 外部截图：{summary['external_images']}（分为 {len(image_groups)} 个有序单元）",
        f"- 含 Word 内嵌图的 OCR 图片总数：{summary['ocr_images_total']}", f"- 父块：{len(parent_rows)}",
        f"- 检索子块：{len(chunk_rows)}", f"- 正文字符：{summary['characters']}",
        f"- 提取方式：{json.dumps(dict(extraction_counts), ensure_ascii=False)}", f"- 已记录错误：{len(errors)}", "",
        "风险：图片中的小字号、分时图、盘口数字和表格结构可能被 OCR 误识别；相关块保留图片名和原始路径，关键数字必须回看原图。",
    ]
    write_text_lf(LIB / "quality_report.md", "\n".join(quality) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
