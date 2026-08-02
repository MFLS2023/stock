#!/usr/bin/env python3
"""Build a provenance-preserving cross-source topic coverage map."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from kb_import_utils import write_text_lf


ROOT = Path(__file__).resolve().parents[2]
LIBRARIES = ROOT / "_知识库系统" / "source_libraries"
OUTPUT = ROOT / "_知识库系统" / "cross_source"
CONFIG = ROOT / "_知识库系统" / "config" / "sources.yaml"


def read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def topic_values(value) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").replace("、", ",").split(",") if item.strip()]


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    source_names = {item["id"]: item["display_name"] for item in config.get("sources", [])}
    topic_chunk_counts: dict[str, Counter] = defaultdict(Counter)
    topic_doc_ids: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    topic_documents: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    source_summary = {}
    for source_id, source_name in source_names.items():
        library = LIBRARIES / source_id
        documents = read_jsonl(library / "documents.jsonl")
        chunks = read_jsonl(library / "chunks.jsonl")
        valid_dates = [item.get("date") for item in documents if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(item.get("date") or ""))]
        source_summary[source_id] = {
            "source_name": source_name, "documents": len(documents), "chunks": len(chunks),
            "date_start": min(valid_dates, default=""), "date_end": max(valid_dates, default=""),
        }
        for chunk in chunks:
            for topic in topic_values(chunk.get("topics")):
                topic_chunk_counts[topic][source_id] += 1
                topic_doc_ids[topic][source_id].add(chunk.get("document_id", ""))
                topic_documents[topic][source_id][chunk.get("title", "未命名")] += 1

    topics = sorted(
        topic_chunk_counts,
        key=lambda topic: (-sum(len(topic_doc_ids[topic][source]) for source in source_names), topic),
    )
    source_headers = " | ".join(source_names[source_id] for source_id in source_names)
    separator = "|---|" + "---:|" * len(source_names) + "---|"
    lines = [
        "# 跨来源主题覆盖地图", "",
        "> 这里显示各来源已入库材料的覆盖量，不代表观点一致。真正比较时仍须逐来源检索并保留引用。", "",
        f"| 主题 | {source_headers} | 高覆盖文档示例 |", separator,
    ]
    for topic in topics:
        examples = []
        for source_id in source_names:
            if topic_documents[topic].get(source_id):
                title = topic_documents[topic][source_id].most_common(1)[0][0]
                examples.append(f"{source_names[source_id]}：{title}")
        counts = {source_id: len(topic_doc_ids[topic][source_id]) for source_id in source_names}
        count_cells = " | ".join(str(counts[source_id]) for source_id in source_names)
        lines.append(f"| {topic} | {count_cells} | {'；'.join(examples).replace('|', '｜')} |")
    write_text_lf(OUTPUT / "topic_coverage.md", "\n".join(lines) + "\n")
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": source_summary,
        "topics": {
            topic: {
                "document_counts": {source: len(topic_doc_ids[topic][source]) for source in source_names},
                "chunk_counts": dict(topic_chunk_counts[topic]),
            }
            for topic in topics
        },
        "interpretation": "coverage_only_not_consensus",
    }
    write_text_lf(
        OUTPUT / "source_overview.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
