#!/usr/bin/env python3
"""Import Nanjinglu Bian dated PDF/JPG articles into the standard library."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader

from kb_import_utils import (
    ROOT,
    cjk_ratio,
    clean_text,
    clean_title,
    extract_date,
    infer_topics,
    meaningful_char_count,
    natural_key,
    ocr_images,
    split_text,
    subtract_known_text,
    text_layer_is_usable,
    write_jsonl,
    write_text_lf,
)


SOURCE_ID = "nanjinglu_bian"
SOURCE_NAME = "南京路彼岸"
SOURCE_ROOT = ROOT / "南京路彼岸"
LIB = ROOT / "_知识库系统" / "source_libraries" / SOURCE_ID
POPPLER = Path(
    r"C:\Users\20577\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\poppler\Library\bin\pdftoppm.exe"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def render_pdf_page(pdf: Path, page_number: int, output: Path, dpi: int) -> Path:
    # Prefer the real bundled executable. The PATH shim in this Windows runtime
    # is a .cmd wrapper with an invalid relocated path.
    executable = str(POPPLER) if POPPLER.exists() else (shutil.which("pdftoppm") or "")
    if not executable:
        raise FileNotFoundError("pdftoppm was not found in the bundled runtime or PATH")
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix = output.with_suffix("")
    command = [
        executable, "-png", "-r", str(dpi), "-f", str(page_number), "-l", str(page_number),
        "-singlefile", str(pdf), str(prefix),
    ]
    completed = subprocess.run(
        command, cwd=str(Path(executable).parent), capture_output=True, text=True,
        encoding="utf-8", errors="replace"
    )
    if completed.returncode != 0 or not output.exists():
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"render failed: {pdf}")
    return output


def page_locator(page_start: int, page_end: int) -> str:
    return f"第{page_start}页" if page_start == page_end else f"第{page_start}—{page_end}页"


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


def load_cache(path: Path, source_hash: str, dpi: int) -> dict | None:
    if not path.exists():
        return None
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        item.get("source_hash") == source_hash
        and item.get("dpi") == dpi
        and not (item.get("method") == "embedded_fallback" and item.get("ocr_error"))
        # Caches written before dual-layer extraction lack ocr_residue and would
        # silently drop the screenshot content, so treat them as stale.
        and "ocr_residue" in item
    ):
        return item
    return None


def save_cache(path: Path, item: dict) -> None:
    write_text_lf(path, json.dumps(item, ensure_ascii=False, indent=2) + "\n")


def extract_pdf(
    path: Path,
    doc_id: str,
    source_hash: str,
    *,
    dpi: int,
    threshold: int,
    cjk_threshold: float,
    force: bool,
) -> tuple[list[dict], list[dict]]:
    reader = PdfReader(path)
    pages: list[dict | None] = [None] * len(reader.pages)
    to_ocr: list[tuple[str, Path]] = []
    render_work = Path(tempfile.mkdtemp(prefix=f"{doc_id}-", dir=ROOT / "_知识库系统" / "tmp"))
    errors: list[dict] = []
    try:
        for index, page in enumerate(reader.pages, start=1):
            cache_path = LIB / "page_texts" / f"{doc_id}-page-{index:03d}.json"
            cached = None if force else load_cache(cache_path, source_hash, dpi)
            if cached:
                pages[index - 1] = cached
                continue
            try:
                embedded = clean_text(page.extract_text() or "")
            except Exception as exc:
                embedded = ""
                errors.append({"document_id": doc_id, "page": index, "stage": "embedded", "error": str(exc)})
            # These PDFs mix article prose (carried by the text layer) with pasted
            # market screenshots (only reachable through OCR), so a page is not an
            # either/or choice. Every page is rendered and OCRed; a usable text layer
            # is kept alongside as the high-confidence copy of the prose.
            usable = text_layer_is_usable(embedded, min_chars=threshold, min_cjk_ratio=cjk_threshold)
            try:
                image_path = render_work / f"page-{index:03d}.png"
                render_pdf_page(path, index, image_path, dpi)
                key = str(index)
                to_ocr.append((key, image_path))
                pages[index - 1] = {
                    "source_hash": source_hash, "dpi": dpi, "page": index, "method": "pending_ocr",
                    "confidence": "low", "text": embedded, "embedded_chars": meaningful_char_count(embedded),
                    "ocr_chars": 0, "ocr_error": "", "embedded_usable": usable,
                    "embedded_cjk_ratio": round(cjk_ratio(embedded), 3),
                }
            except Exception as exc:
                errors.append({"document_id": doc_id, "page": index, "stage": "render", "error": str(exc)})
                item = pages[index - 1] or {
                    "source_hash": source_hash, "dpi": dpi, "page": index, "method": "embedded_fallback",
                    "confidence": "low", "text": embedded, "embedded_chars": meaningful_char_count(embedded),
                    "ocr_chars": 0, "ocr_error": str(exc),
                }
                save_cache(cache_path, item)
                pages[index - 1] = item

        if to_ocr:
            ocr_result = ocr_images(to_ocr)
            for key, _ in to_ocr:
                index = int(key)
                pending = pages[index - 1] or {}
                ocr_item = ocr_result.get(key, {"text": "", "error": "no_result", "tiles": 0})
                embedded = pending.get("text", "")
                ocr_text = clean_text(ocr_item.get("text", ""), ocr=True)
                embedded_count = meaningful_char_count(embedded)
                ocr_count = meaningful_char_count(ocr_text)
                embedded_usable = bool(pending.get("embedded_usable"))
                if embedded_usable:
                    # Keep the clean text layer for the prose and only the OCR lines it
                    # does not already cover, which is the screenshot content.
                    residue = subtract_known_text(ocr_text, embedded)
                    method, confidence, primary = "embedded", "high", embedded
                else:
                    # Nothing trustworthy in the text layer, so OCR carries the page.
                    residue = ""
                    method, confidence, primary = "ocr", "medium", ocr_text
                    if not ocr_text:
                        method, confidence, primary = "embedded_fallback", "low", embedded
                item = {
                    "source_hash": source_hash,
                    "dpi": dpi,
                    "page": index,
                    "method": method,
                    "confidence": confidence,
                    "text": primary,
                    "ocr_residue": residue,
                    "ocr_residue_chars": meaningful_char_count(residue),
                    "embedded_chars": embedded_count,
                    "ocr_chars": ocr_count,
                    "embedded_cjk_ratio": round(cjk_ratio(embedded), 3),
                    "ocr_cjk_ratio": round(cjk_ratio(ocr_text), 3),
                    "ocr_error": ocr_item.get("error", ""),
                    "ocr_tiles": ocr_item.get("tiles", 0),
                }
                if ocr_item.get("error"):
                    errors.append({
                        "document_id": doc_id, "page": index, "stage": "ocr", "error": ocr_item["error"]
                    })
                save_cache(LIB / "page_texts" / f"{doc_id}-page-{index:03d}.json", item)
                pages[index - 1] = item
    finally:
        shutil.rmtree(render_work, ignore_errors=True)
    return [item for item in pages if item is not None], errors


def extract_image(path: Path, doc_id: str, source_hash: str, *, force: bool) -> tuple[list[dict], list[dict]]:
    cache_path = LIB / "page_texts" / f"{doc_id}-image-001.json"
    cached = None if force else load_cache(cache_path, source_hash, 0)
    if cached:
        return [cached], []
    result = ocr_images([("1", path)]).get("1", {"text": "", "error": "no_result", "tiles": 0})
    text = clean_text(result.get("text", ""), ocr=True)
    item = {
        "source_hash": source_hash, "dpi": 0, "page": 1, "method": "ocr",
        "confidence": "medium" if text else "low", "text": text, "embedded_chars": 0,
        "ocr_chars": meaningful_char_count(text), "ocr_error": result.get("error", ""),
        "ocr_tiles": result.get("tiles", 0), "image_name": path.name,
    }
    save_cache(cache_path, item)
    errors = []
    if result.get("error"):
        errors.append({"document_id": doc_id, "page": 1, "stage": "ocr", "error": result["error"]})
    return [item], errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dpi", type=int, default=140)
    parser.add_argument(
        "--embedded-threshold", type=int, default=50,
        help="Minimum text-layer characters for a page to skip OCR (was 500, which "
             "forced OCR on pages averaging 385 characters of good native text)",
    )
    parser.add_argument(
        "--embedded-cjk-ratio", type=float, default=0.5,
        help="Minimum CJK share of the text layer; below this the layer is treated as "
             "font-encoding garbage and the page is sent to OCR",
    )
    parser.add_argument("--force", action="store_true", help="Ignore persistent page OCR caches")
    args = parser.parse_args()

    for name in ("texts", "page_texts", "maps"):
        (LIB / name).mkdir(parents=True, exist_ok=True)
    files = sorted(
        (path for path in SOURCE_ROOT.rglob("*") if path.is_file() and path.suffix.lower() in {".pdf", ".jpg", ".jpeg", ".png"}),
        key=lambda path: natural_key(path.relative_to(SOURCE_ROOT).as_posix()),
    )
    hashes = {path: sha256_file(path) for path in files}
    hash_groups: dict[str, list[Path]] = {}
    for path, digest in hashes.items():
        hash_groups.setdefault(digest, []).append(path)
    canonicals = {digest: min(paths, key=lambda path: (len(path.relative_to(SOURCE_ROOT).parts), len(str(path)))) for digest, paths in hash_groups.items()}

    document_rows: list[dict] = []
    parent_rows: list[dict] = []
    chunk_rows: list[dict] = []
    errors: list[dict] = []
    extraction_counts: Counter[str] = Counter()
    used_ids: Counter[str] = Counter()

    for path in files:
        digest = hashes[path]
        base_id = f"nanjinglu-{digest[:12]}"
        used_ids[base_id] += 1
        doc_id = base_id if used_ids[base_id] == 1 else f"{base_id}-dup{used_ids[base_id]}"
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        title = clean_title(path)
        date = extract_date(path.name, path.parent.name, relative)
        duplicate_of = ""
        canonical = canonicals[digest]
        if path != canonical:
            duplicate_of = f"nanjinglu-{digest[:12]}"
            pages = []
        elif path.suffix.lower() == ".pdf":
            try:
                pages, page_errors = extract_pdf(
                    path, doc_id, digest, dpi=args.dpi, threshold=args.embedded_threshold,
                    cjk_threshold=args.embedded_cjk_ratio, force=args.force,
                )
                errors.extend(page_errors)
            except Exception as exc:
                pages = []
                errors.append({"document_id": doc_id, "stage": "pdf", "error": str(exc)})
        else:
            pages, page_errors = extract_image(path, doc_id, digest, force=args.force)
            errors.extend(page_errors)

        normalized_parts: list[str] = []
        units: list[dict] = []
        for page in pages:
            page_number = int(page.get("page", 1))
            page_method = page.get("method", "unknown")
            # A page contributes up to two layers: the primary text (clean prose from
            # the text layer, or OCR when there is no usable layer) and, for mixed
            # pages, the OCR-only screenshot content kept at lower confidence.
            layers = [(page_method, page.get("confidence", "low"), page.get("text", ""))]
            if page.get("ocr_residue"):
                layers.append(("ocr_screenshot", "low", page["ocr_residue"]))
            for method, confidence, raw_text in layers:
                page_text = clean_text(raw_text, ocr=method.startswith("ocr"))
                if not page_text:
                    continue
                extraction_counts[method] += 1
                normalized_parts.append(f"[第{page_number}页 | {method}]\n{page_text}")
                for segment in split_text(page_text, 1200):
                    units.append(
                        {
                            "page_start": page_number, "page_end": page_number, "text": segment,
                            "method": method, "confidence": confidence,
                        }
                    )

        normalized_text = "\n\n".join(normalized_parts).strip()
        text_path = LIB / "texts" / f"{doc_id}.txt"
        write_text_lf(text_path, normalized_text + ("\n" if normalized_text else ""))
        content_date = extract_date(normalized_text[:400])
        topics = infer_topics(title, normalized_text)
        risk_flags = []
        if any(page.get("method") == "ocr" for page in pages):
            risk_flags.append("OCR内容需回看原页")
        if date and content_date and date != content_date:
            risk_flags.append(f"文件日期与正文识别日期不一致:{date}/{content_date}")
        document_rows.append(
            {
                "source_id": SOURCE_ID, "document_id": doc_id, "title": title, "date": date,
                "author_or_guest": "南京路彼岸", "content_type": "dated_article_pdf" if path.suffix.lower() == ".pdf" else "dated_article_image",
                "pages": len(pages), "characters": meaningful_char_count(normalized_text), "topics": topics,
                "original_path": str(path), "normalized_text_path": str(text_path), "sha256": digest,
                "content_detected_date": content_date, "duplicate_of": duplicate_of,
                "risk_flags": "；".join(risk_flags),
            }
        )
        if duplicate_of:
            continue

        for parent_number, parent_group in enumerate(group_units(units, 4200), start=1):
            parent_id = f"{doc_id}-p{parent_number:03d}"
            parent_start = parent_group[0]["page_start"]
            parent_end = parent_group[-1]["page_end"]
            parent_rows.append(
                {
                    "source_id": SOURCE_ID, "document_id": doc_id, "parent_id": parent_id, "title": title,
                    "date": date, "author_or_guest": "南京路彼岸", "locator": page_locator(parent_start, parent_end),
                    "text": "\n".join(unit["text"] for unit in parent_group),
                }
            )
            for child_number, unit in enumerate(parent_group, start=1):
                chunk_rows.append(
                    {
                        "source_id": SOURCE_ID, "source_name": SOURCE_NAME, "document_id": doc_id,
                        "parent_id": parent_id, "chunk_id": f"{parent_id}-c{child_number:02d}",
                        "chunk_type": "screenshot_ocr" if unit["method"] == "ocr_screenshot" else "article",
                        "title": title, "date": date, "author_or_guest": "南京路彼岸",
                        "topics": topics, "claim_type": "opinion_or_case", "market_regime": "未标注",
                        "locator": page_locator(unit["page_start"], unit["page_end"]),
                        "page_start": unit["page_start"], "page_end": unit["page_end"], "text": unit["text"],
                        "original_path": str(path), "confidence": unit["confidence"],
                        "extraction_method": unit["method"],
                    }
                )

    write_jsonl(LIB / "documents.jsonl", document_rows)
    write_jsonl(LIB / "parents.jsonl", parent_rows)
    write_jsonl(LIB / "chunks.jsonl", chunk_rows)

    chronology_rows = sorted(document_rows, key=lambda item: (item.get("date") or "9999", item["title"]))
    chronology = ["# 南京路彼岸文章时间线", "", "> 日期来自文件名或目录名；未能可靠判断的文件列在末尾。", ""]
    for item in chronology_rows:
        suffix = f"（重复文件，正文沿用 {item['duplicate_of']}）" if item.get("duplicate_of") else ""
        chronology.append(f"- {item.get('date') or '日期未标注'}｜{item['title']}｜{item['document_id']} {suffix}".rstrip())
    write_text_lf(LIB / "chronology.md", "\n".join(chronology) + "\n")

    content_map = ["# 南京路彼岸内容地图", "", "| 日期 | 标题 | 主题 | 字符数 | 文档ID |", "|---|---|---|---:|---|"]
    for item in chronology_rows:
        content_map.append(
            f"| {item.get('date') or '未标注'} | {item['title'].replace('|', '｜')} | "
            f"{'、'.join(item.get('topics') or []) or '未标注'} | {item.get('characters', 0)} | {item['document_id']} |"
        )
    write_text_lf(LIB / "content_map.md", "\n".join(content_map) + "\n")

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "source_id": SOURCE_ID,
        "files": len(files), "documents": len(document_rows), "unique_documents": sum(not item.get("duplicate_of") for item in document_rows),
        "duplicates": sum(bool(item.get("duplicate_of")) for item in document_rows), "parents": len(parent_rows),
        "chunks": len(chunk_rows), "characters": sum(item.get("characters", 0) for item in document_rows),
        "extraction_counts": dict(extraction_counts), "errors": errors,
    }
    write_text_lf(LIB / "source_summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    quality = [
        "# 南京路彼岸导入质量报告", "", f"- 原始文件：{len(files)}", f"- 唯一内容文档：{summary['unique_documents']}",
        f"- 重复文件：{summary['duplicates']}", f"- 父块：{len(parent_rows)}", f"- 检索子块：{len(chunk_rows)}",
        f"- 正文字符：{summary['characters']}", f"- 提取方式：{json.dumps(dict(extraction_counts), ensure_ascii=False)}",
        f"- 已记录错误：{len(errors)}", "",
        "风险：截图页经 Windows OCR 识别，可能存在同音字、标点和小字号误差；引用已保留页码和原始路径，关键结论应回看原页。",
    ]
    write_text_lf(LIB / "quality_report.md", "\n".join(quality) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
