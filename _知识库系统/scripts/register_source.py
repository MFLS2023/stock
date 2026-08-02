#!/usr/bin/env python3
"""Register a new read-only source folder and inspect its format mix."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

from kb_import_utils import write_text_lf


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "_知识库系统" / "config" / "sources.yaml"
LIBRARIES = ROOT / "_知识库系统" / "source_libraries"
RESERVED = {"_知识库系统", ".agents", ".Codex"}


def default_source_id(folder_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", folder_name.casefold()).strip("-")
    if slug:
        return slug[:48]
    suffix = hashlib.sha256(folder_name.encode("utf-8")).hexdigest()[:8]
    return f"source-{suffix}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", help="A source folder under the knowledge-base root")
    parser.add_argument("--id", dest="source_id")
    parser.add_argument("--name", dest="display_name")
    parser.add_argument("--author")
    parser.add_argument("--adapter", default="auto", choices=["auto", "generic_mixed", "specialized"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_absolute():
        folder = ROOT / folder
    folder = folder.resolve()
    try:
        relative = folder.relative_to(ROOT)
    except ValueError as exc:
        raise ValueError(f"Source folder must be inside {ROOT}") from exc
    if not folder.is_dir():
        raise FileNotFoundError(folder)
    if relative.parts[0] in RESERVED:
        raise ValueError(f"Reserved project folder cannot be registered as a source: {relative.parts[0]}")

    source_id = args.source_id or default_source_id(folder.name)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,62}", source_id):
        raise ValueError("--id must contain 2-63 lowercase ASCII letters, digits, underscores, or hyphens")
    display_name = args.display_name or folder.name
    files = [path for path in folder.rglob("*") if path.is_file()]
    formats = Counter(path.suffix.lower() or "[no_extension]" for path in files)
    supported = {".md", ".txt", ".pdf", ".docx", ".jpg", ".jpeg", ".png"}
    unsupported = {extension: count for extension, count in formats.items() if extension not in supported}
    complexity_signals: list[str] = []
    pdf_pages = 0
    pdf_low_text_pages = 0
    pdf_paths = [item for item in files if item.suffix.lower() == ".pdf"][:12]
    if pdf_paths:
        # Imported here, not at module level: a folder with no PDFs needs nothing beyond
        # the standard library plus yaml, and the module must stay importable without pypdf.
        from pypdf import PdfReader
    for path in pdf_paths:
        try:
            reader = PdfReader(path)
            for page in reader.pages[:20]:
                pdf_pages += 1
                try:
                    text = page.extract_text() or ""
                except Exception:
                    text = ""
                if len(re.sub(r"\s+", "", text)) < 200:
                    pdf_low_text_pages += 1
        except Exception as exc:
            complexity_signals.append(f"PDF预检失败:{path.name}:{exc}")
    if pdf_pages and pdf_low_text_pages / pdf_pages >= 0.30:
        complexity_signals.append(f"抽样PDF低文本页占比{pdf_low_text_pages}/{pdf_pages}，可能为截图或图文混合")

    docx_media = 0
    docx_max_media = 0
    for path in (item for item in files if item.suffix.lower() == ".docx"):
        try:
            with zipfile.ZipFile(path) as archive:
                media = sum(name.startswith("word/media/") and not name.endswith("/") for name in archive.namelist())
            docx_media += media
            docx_max_media = max(docx_max_media, media)
        except (OSError, zipfile.BadZipFile):
            complexity_signals.append(f"DOCX预检失败:{path.name}")
    if docx_max_media >= 5 or docx_media >= 20:
        complexity_signals.append(f"DOCX内嵌图片较多:总计{docx_media}，单文件最高{docx_max_media}")

    image_files = [item for item in files if item.suffix.lower() in {".jpg", ".jpeg", ".png"}]
    image_parent_counts = Counter(path.parent for path in image_files)
    if image_parent_counts and max(image_parent_counts.values()) >= 4:
        complexity_signals.append(f"同一目录存在连续图片，最高{max(image_parent_counts.values())}张，需确认阅读顺序")

    recommended_adapter = "specialized" if unsupported or complexity_signals else "generic_mixed"
    selected_adapter = recommended_adapter if args.adapter == "auto" else args.adapter
    review_required = bool(unsupported or complexity_signals or selected_adapter == "specialized")

    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    if not args.dry_run and any(item["id"] == source_id for item in config.get("sources", [])):
        raise ValueError(f"Source id already exists: {source_id}")
    if not args.dry_run and any(item["source_path"] == relative.as_posix() for item in config.get("sources", [])):
        raise ValueError(f"Source folder is already registered: {relative.as_posix()}")
    source = {
        "id": source_id,
        "display_name": display_name,
        "source_path": relative.as_posix(),
        "source_type": "mixed_documents",
        "status": "registered",
        "original_policy": "read_only",
        "primary_locator": "page_section_image",
        "pipeline": "generic_format_adapters" if selected_adapter == "generic_mixed" else "specialized_required",
        "adapter": selected_adapter,
        "author": args.author or display_name,
        "format_counts": dict(sorted(formats.items())),
        "review_required": review_required,
        "complexity_signals": complexity_signals,
        "notes": "通用适配器支持 MD/TXT/PDF/DOCX/JPG/PNG；特殊说话人、图文关系或复杂顺序需专用适配器。",
    }
    preview = {
        "source": source,
        "files": len(files),
        "unsupported_formats": unsupported,
        "recommended_adapter": recommended_adapter,
        "selected_adapter": selected_adapter,
        "pdf_preflight": {"sampled_pages": pdf_pages, "low_text_pages": pdf_low_text_pages},
        "docx_preflight": {"embedded_media": docx_media, "max_media_in_one_file": docx_max_media},
        "next_command": (
            f"python _知识库系统/scripts/import_source.py --source {source_id}"
            if selected_adapter == "generic_mixed"
            else "先建立并登记专用适配器，再运行 import_source.py"
        ),
    }
    if args.dry_run:
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0

    config.setdefault("sources", []).append(source)
    temporary = CONFIG.with_suffix(".yaml.tmp")
    write_text_lf(temporary, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    temporary.replace(CONFIG)
    library = LIBRARIES / source_id
    library.mkdir(parents=True, exist_ok=True)
    write_text_lf(library / "source.yaml", yaml.safe_dump(source, allow_unicode=True, sort_keys=False))
    report = [
        f"# {display_name} 接入检查", "", f"- 登记时间：{datetime.now(timezone.utc).isoformat()}",
        f"- 原始目录：{folder}", f"- 文件数：{len(files)}", f"- 格式：{json.dumps(dict(formats), ensure_ascii=False)}",
        f"- 推荐适配器：{recommended_adapter}", f"- 登记适配器：{selected_adapter}",
        f"- 复杂度信号：{json.dumps(complexity_signals, ensure_ascii=False)}",
        f"- 需要人工确认：{'是' if review_required else '否'}", "",
        "## 下一步", "",
        (
            f"运行：`python _知识库系统/scripts/import_source.py --source {source_id}`"
            if selected_adapter == "generic_mixed"
            else "先根据复杂度信号建立专用适配器，禁止直接用通用导入器。"
        ),
    ]
    if unsupported:
        report.extend(["", f"暂不支持的格式：{json.dumps(unsupported, ensure_ascii=False)}。请增加专用适配器，勿静默跳过。"])
    write_text_lf(library / "onboarding_report.md", "\n".join(report) + "\n")
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
