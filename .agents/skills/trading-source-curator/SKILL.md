---
name: trading-source-curator
description: Register, inspect, batch-ingest, normalize, update, and validate new trading blogger/source folders containing MD, TXT, PDF, Word, images, transcripts, or ordered screenshots. Use when adding a new source folder, deciding between the generic or specialized adapter, refreshing changed files, rebuilding manifests/indexes, repairing citations, or producing source content maps without modifying originals.
---

# Trading Source Curator

Preserve original source folders as read-only. Write every derivative under `_知识库系统`.

## Workflow

1. Inspect the new folder's extensions, nesting, duplicates, date patterns, image order, and whether speaker or visual relationships matter.
2. Register a conventional source with `register_source.py`; use a stable ASCII `source_id` and keep the source folder read-only.
3. Select the adapter:
   - `generic_mixed`: ordinary MD/TXT/PDF/DOCX/JPG/PNG where one file is one document and simple page/section locators are sufficient.
   - `specialized`: transcripts with speakers/timestamps, ordered screenshot courses, mixed Word-image relationships, or any source where generic extraction would lose meaning.
4. Run `build_manifest.py` before ingestion. For a generic source use `import_source.py --source <id>`; do not silently ignore unsupported formats.
   For PDF extraction: always try the text layer first (`pdfplumber`). If a page yields ≥30 characters from the text layer, use it directly. Only run OCR on pages where the text layer is absent or yields <30 characters. Record `extraction_method: text_layer` or `extraction_method: ocr` per chunk.
5. For specialized sources preserve their defining structure:
   - `transcript_pdf`: preserve speaker, page, timestamp, and surrounding utterances.
   - `dated_article_pdf`: preserve title, date, section hierarchy, and viewpoint evolution.
   - `word_image_course`: preserve Word sections, image order, OCR confidence, and image references.
6. Assign stable `source_id`, `document_id`, `chunk_id`, `parent_id`, and locator fields.
7. Classify content as fact, opinion, rule, case, hypothesis, or calculation. Do not promote author claims to facts.
8. Update only new or changed files determined by the manifest. Never overwrite raw files.
9. Rebuild the index, cross-source coverage map, and validation:

   ```powershell
   python _知识库系统/scripts/build_index.py
   python _知识库系统/scripts/build_cross_source.py
   python _知识库系统/scripts/validate_kb.py
   ```

10. Record unsupported formats, failures, OCR uncertainty, missing pages, unresolved speaker attribution, and any reason a specialized adapter is required.

## Output Contract

Each source library must include `source.yaml`, `documents.jsonl`, `parents.jsonl`, `chunks.jsonl`, a content map, and a quality report. Each searchable chunk must include source, document, locator, date when known, text, original path, confidence, and extraction method.
