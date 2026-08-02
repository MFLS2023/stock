#!/usr/bin/env python3
"""Import the completed first-pass Fulibei corpus into the standard source library."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "_知识库系统" / "source_libraries" / "fulibei"
UTTERANCE_RE = re.compile(r"^\[第(\d+)页\s+(\d{2}:\d{2}(?::\d{2})?)\s+([^\]]+)\]\s*(.*)$")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def parse_utterances(path: Path) -> list[dict]:
    utterances: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        match = UTTERANCE_RE.match(line)
        if match:
            utterances.append(
                {
                    "page": int(match.group(1)),
                    "timestamp": match.group(2),
                    "speaker": match.group(3).strip(),
                    "text": match.group(4).strip(),
                }
            )
        elif line and utterances and not line.startswith(("原文件：", "页数：", "可解析发言：")):
            utterances[-1]["text"] = f"{utterances[-1]['text']} {line}".strip()
    return [item for item in utterances if item["text"]]


def group_records(records: list[dict], target_chars: int) -> list[list[dict]]:
    groups: list[list[dict]] = []
    current: list[dict] = []
    length = 0
    for item in records:
        item_len = len(item["text"])
        if current and length + item_len > target_chars:
            groups.append(current)
            current = []
            length = 0
        current.append(item)
        length += item_len
    if current:
        groups.append(current)
    return groups


def locator(group: list[dict]) -> str:
    first, last = group[0], group[-1]
    if first["page"] == last["page"]:
        return f"第{first['page']}页 {first['timestamp']}—{last['timestamp']}"
    return f"第{first['page']}页 {first['timestamp']}—第{last['page']}页 {last['timestamp']}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True, help="Existing first-pass work directory")
    parser.add_argument("--deliverables", type=Path, help="Existing Fulibei deliverables directory")
    args = parser.parse_args()

    data_dir = args.workdir / "data"
    extracted_dir = args.workdir / "extracted"
    documents = read_json(data_dir / "documents.json")
    curated_methods = read_json(data_dir / "curated_methods.json")
    conflicts = read_json(data_dir / "conflicts.json")
    summary = read_json(data_dir / "summary.json")

    for name in ("texts", "maps"):
        (LIB / name).mkdir(parents=True, exist_ok=True)

    document_rows: list[dict] = []
    parent_rows: list[dict] = []
    chunk_rows: list[dict] = []
    import_errors: list[dict] = []

    for doc in sorted(documents, key=lambda item: int(item["序号"])):
        number = int(doc["序号"])
        doc_id = f"fulibei-{number:03d}"
        source_text = Path(doc["清洗文本"])
        if not source_text.exists():
            candidate = extracted_dir / source_text.name
            source_text = candidate
        if not source_text.exists():
            import_errors.append({"document_id": doc_id, "error": "missing_clean_text", "path": str(source_text)})
            continue

        target_text = LIB / "texts" / f"{doc_id}.txt"
        shutil.copy2(source_text, target_text)
        utterances = parse_utterances(source_text)
        title = doc["文件名"]
        guest = doc.get("嘉宾") or "未标注"
        topics = [part.strip() for part in (doc.get("主要主题") or "").split("、") if part.strip()]
        document_rows.append(
            {
                "source_id": "fulibei",
                "document_id": doc_id,
                "title": title,
                "date": doc.get("文件日期"),
                "author_or_guest": guest,
                "content_type": "transcript_pdf",
                "pages": doc.get("页数"),
                "characters": doc.get("字符数"),
                "utterances": len(utterances),
                "topics": topics,
                "original_path": doc["原始路径"],
                "normalized_text_path": str(target_text),
                "risk_flags": doc.get("风险标记", ""),
            }
        )

        parent_groups = group_records(utterances, target_chars=3600)
        for parent_number, parent_group in enumerate(parent_groups, start=1):
            parent_id = f"{doc_id}-p{parent_number:03d}"
            parent_text = "\n".join(
                f"[{item['timestamp']} {item['speaker']}] {item['text']}" for item in parent_group
            )
            parent_rows.append(
                {
                    "source_id": "fulibei",
                    "document_id": doc_id,
                    "parent_id": parent_id,
                    "title": title,
                    "date": doc.get("文件日期"),
                    "author_or_guest": guest,
                    "locator": locator(parent_group),
                    "text": parent_text,
                }
            )
            child_groups = group_records(parent_group, target_chars=1200)
            for child_number, child_group in enumerate(child_groups, start=1):
                chunk_id = f"{parent_id}-c{child_number:02d}"
                speakers = sorted({item["speaker"] for item in child_group})
                chunk_rows.append(
                    {
                        "source_id": "fulibei",
                        "source_name": "复利杯",
                        "document_id": doc_id,
                        "parent_id": parent_id,
                        "chunk_id": chunk_id,
                        "chunk_type": "transcript",
                        "title": title,
                        "date": doc.get("文件日期"),
                        "author_or_guest": guest,
                        "speakers": speakers,
                        "topics": topics,
                        "claim_type": "opinion_or_case",
                        "market_regime": "未标注",
                        "locator": locator(child_group),
                        "page_start": child_group[0]["page"],
                        "page_end": child_group[-1]["page"],
                        "time_start": child_group[0]["timestamp"],
                        "time_end": child_group[-1]["timestamp"],
                        "text": "\n".join(
                            f"[{item['timestamp']} {item['speaker']}] {item['text']}" for item in child_group
                        ),
                        "original_path": doc["原始路径"],
                        "confidence": "medium",
                    }
                )

    method_rows = []
    for index, method in enumerate(curated_methods, start=1):
        method_rows.append(
            {
                "source_id": "fulibei",
                "method_id": f"fulibei-method-{index:03d}",
                "topic": method.get("主题", ""),
                "conclusion": method.get("整理结论", ""),
                "checklist": method.get("可执行检查", ""),
                "quote": method.get("原文摘录", ""),
                "title": method.get("来源文件", ""),
                "locator": f"第{method.get('页码', '')}页 {method.get('时间戳', '')}".strip(),
                "evidence_level": method.get("证据层级", ""),
                "conditions": method.get("适用条件", ""),
                "invalidation": method.get("失效条件", ""),
                "risk": method.get("风险提示", ""),
            }
        )

    conflict_rows = []
    for index, conflict in enumerate(conflicts, start=1):
        conflict_rows.append(
            {
                "source_id": "fulibei",
                "conflict_id": f"fulibei-conflict-{index:03d}",
                "topic": conflict.get("争议主题", ""),
                "view_a": conflict.get("观点A", ""),
                "view_b": conflict.get("观点B", ""),
                "assessment": conflict.get("Codex判断", ""),
                "sources": conflict.get("相关来源", ""),
                "verification": conflict.get("验证办法", ""),
            }
        )

    write_jsonl(LIB / "documents.jsonl", document_rows)
    write_jsonl(LIB / "parents.jsonl", parent_rows)
    write_jsonl(LIB / "chunks.jsonl", chunk_rows)
    write_jsonl(LIB / "methods.jsonl", method_rows)
    write_jsonl(LIB / "conflicts.jsonl", conflict_rows)

    if args.deliverables and args.deliverables.exists():
        copy_map = {
            "复利杯文字稿逐稿导览.md": "content_map.md",
            "复利杯游资文字稿学习手册.md": "learning_manual.md",
            "质量审查报告.md": "first_pass_quality.md",
            "复利杯内容地图.xlsx": "content_map.xlsx",
        }
        for source_name, target_name in copy_map.items():
            source = args.deliverables / source_name
            if source.exists():
                shutil.copy2(source, LIB / "maps" / target_name)

    import_summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_id": "fulibei",
        "documents": len(document_rows),
        "parents": len(parent_rows),
        "chunks": len(chunk_rows),
        "curated_methods": len(method_rows),
        "conflicts": len(conflict_rows),
        "errors": import_errors,
        "first_pass_summary": summary,
    }
    (LIB / "source_summary.json").write_text(
        json.dumps(import_summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    quality = [
        "# 复利杯标准化导入质量报告",
        "",
        f"- 文档：{len(document_rows)}",
        f"- 父块：{len(parent_rows)}",
        f"- 检索子块：{len(chunk_rows)}",
        f"- 精选方法：{len(method_rows)}",
        f"- 已记录分歧：{len(conflict_rows)}",
        f"- 导入错误：{len(import_errors)}",
        "",
        "风险：直播转写可能包含同音字、断句和说话人识别误差；所有关键结论应回看原 PDF 上下文。",
    ]
    (LIB / "quality_report.md").write_text("\n".join(quality) + "\n", encoding="utf-8")
    print(json.dumps(import_summary, ensure_ascii=False, indent=2))
    return 1 if import_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
