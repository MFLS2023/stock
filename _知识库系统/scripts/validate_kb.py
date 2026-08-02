#!/usr/bin/env python3
"""Validate project structure, source metadata, citations, SQLite integrity, and sample retrieval."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

from kb_import_utils import write_text_lf


ROOT = Path(__file__).resolve().parents[2]
SYSTEM = ROOT / "_知识库系统"
DATABASE = SYSTEM / "indexes" / "knowledge.db"
MANIFEST = SYSTEM / "indexes" / "manifest.jsonl"
REPORT = SYSTEM / "reports" / "validation_report.md"
JSON_REPORT = SYSTEM / "reports" / "validation_report.json"
REQUIRED_CHUNK_FIELDS = {"source_id", "document_id", "chunk_id", "title", "locator", "text"}


def check(condition: bool, name: str, detail: str, results: list[dict]) -> None:
    results.append({"name": name, "passed": bool(condition), "detail": detail})


def main() -> int:
    results: list[dict] = []
    config_path = SYSTEM / "config" / "sources.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    check((ROOT / "AGENTS.md").exists(), "根 AGENTS.md", "项目规则可被新对话自动发现", results)
    check(len(config.get("sources", [])) >= 3, "来源登记", f"登记来源数={len(config.get('sources', []))}", results)

    manifest_rows = []
    if MANIFEST.exists():
        manifest_rows = [json.loads(line) for line in MANIFEST.read_text(encoding="utf-8").splitlines() if line.strip()]
    check(bool(manifest_rows), "文件 manifest", f"记录数={len(manifest_rows)}", results)

    expected_counts = {"documents": 0, "parents": 0, "chunks": 0}
    curated_index_rows = 0
    source_stats = {}
    all_chunks = []
    for source in config.get("sources", []):
        library = SYSTEM / "source_libraries" / source["id"]
        stats = {}
        for kind in ("documents", "parents", "chunks"):
            path = library / f"{kind}.jsonl"
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []
            stats[kind] = len(rows)
            expected_counts[kind] += len(rows)
            if kind == "chunks":
                all_chunks.extend(rows)
        source_stats[source["id"]] = stats
        for extra_name in ("methods.jsonl", "conflicts.jsonl"):
            extra_path = library / extra_name
            if extra_path.exists():
                curated_index_rows += sum(1 for line in extra_path.read_text(encoding="utf-8").splitlines() if line.strip())
    expected_counts["chunks"] += curated_index_rows
    missing = [item.get("chunk_id", "unknown") for item in all_chunks if not REQUIRED_CHUNK_FIELDS.issubset(item)]
    check(all(value["documents"] > 0 and value["chunks"] > 0 for value in source_stats.values()),
          "全部来源内容入库", json.dumps(source_stats, ensure_ascii=False), results)
    check(len(all_chunks) > 1000, "统一检索块", f"块数={len(all_chunks)}", results)
    check(not missing, "检索块字段", f"缺字段块数={len(missing)}", results)
    check(all(item.get("locator") for item in all_chunks), "引用定位", "所有来源检索块均有 locator", results)

    integrity = "missing"
    counts = {}
    sample_results = {}
    if DATABASE.exists():
        connection = sqlite3.connect(DATABASE)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        for table in ("documents", "parents", "chunks"):
            counts[table] = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for query in ("情绪周期", "仓位 回撤", "92科比", "筹码", "弱转强"):
            terms = [term for term in query.split() if term]
            clauses = " OR ".join("text LIKE ? OR title LIKE ? OR author LIKE ?" for _ in terms)
            params = [value for term in terms for value in (f"%{term}%", f"%{term}%", f"%{term}%")]
            sample_results[query] = connection.execute(f"SELECT count(*) FROM chunks WHERE {clauses}", params).fetchone()[0]
        connection.close()
    check(integrity == "ok", "SQLite 完整性", integrity, results)
    check(all(counts.get(kind, -1) == expected_counts[kind] for kind in expected_counts),
          "索引记录数一致", f"SQLite={json.dumps(counts, ensure_ascii=False)} JSONL={json.dumps(expected_counts, ensure_ascii=False)}", results)
    check(all(count > 0 for count in sample_results.values()), "样例检索", json.dumps(sample_results, ensure_ascii=False), results)

    skill_root = ROOT / ".agents" / "skills"
    skill_names = [
        "trading-source-curator", "trading-knowledge-tutor", "cross-source-synthesizer",
        "market-evidence-verifier", "trading-journal-reviewer"
    ]
    skill_missing = [name for name in skill_names if not (skill_root / name / "SKILL.md").exists()]
    check(not skill_missing, "项目 Skills", f"缺失={skill_missing}", results)

    passed = sum(item["passed"] for item in results)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "passed": passed,
        "total": len(results),
        "success": passed == len(results),
        "checks": results,
    }
    write_text_lf(JSON_REPORT, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    lines = ["# 知识库验证报告", "", f"- 通过：{passed}/{len(results)}", f"- 总体：{'通过' if report['success'] else '未通过'}", ""]
    for item in results:
        lines.append(f"- {'✅' if item['passed'] else '❌'} {item['name']}：{item['detail']}")
    write_text_lf(REPORT, "\n".join(lines) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
