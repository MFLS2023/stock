#!/usr/bin/env python3
"""Build an atomic SQLite metadata and FTS5 index from integrated source libraries."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
LIBRARIES = ROOT / "_知识库系统" / "source_libraries"
INDEX_DIR = ROOT / "_知识库系统" / "indexes"
DATABASE = INDEX_DIR / "knowledge.db"
SOURCES_YAML = ROOT / "_知识库系统" / "config" / "sources.yaml"

# 只导入这些状态的来源；draft / disabled 跳过
ALLOWED_STATUSES = {"integrated", "integrated_first_pass"}


def read_jsonl(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def join_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "、".join(str(item) for item in value)
    return str(value)


def create_schema(connection: sqlite3.Connection) -> str:
    connection.executescript(
        """
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=FULL;
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE documents (
            document_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            title TEXT NOT NULL,
            date TEXT,
            author TEXT,
            content_type TEXT,
            topics TEXT,
            original_path TEXT NOT NULL,
            normalized_text_path TEXT,
            risk_flags TEXT
        );
        CREATE TABLE parents (
            parent_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            title TEXT NOT NULL,
            date TEXT,
            author TEXT,
            locator TEXT NOT NULL,
            text TEXT NOT NULL
        );
        CREATE TABLE chunks (
            chunk_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            source_name TEXT,
            document_id TEXT,
            parent_id TEXT,
            chunk_type TEXT NOT NULL,
            title TEXT NOT NULL,
            date TEXT,
            author TEXT,
            speakers TEXT,
            topics TEXT,
            claim_type TEXT,
            market_regime TEXT,
            locator TEXT NOT NULL,
            text TEXT NOT NULL,
            original_path TEXT,
            confidence TEXT,
            image_path TEXT
        );
        CREATE INDEX idx_chunks_source ON chunks(source_id);
        CREATE INDEX idx_chunks_author ON chunks(author);
        CREATE INDEX idx_chunks_document ON chunks(document_id);
        """
    )
    # topics 不进 FTS：它由 infer_topics() 按关键词计数自动打，每块 5-6 个，标签文本
    # 本身从不出现在正文里。实测（3176 块回归样本）标签命中量级压过正文命中——情绪周期
    # FTS 1823 / 正文 202（89% 噪声）、龙头与核心 1358 / 正文 0（100% 噪声），bm25 于是
    # 在噪声上排序。移出后全列 MATCH 精确收敛到 text/title/author 三字段并集。
    #
    # title 和 author 保留：那是人写的真实文本，不是自动标签，标题独有命中是合法结果
    # （实测样本：龙头 310 块、情绪 406 块只有标题含词）。topics 仍存在 chunks 表里，
    # 元数据展示和 relevance() 照旧可读，只是不再参与全文匹配。
    #
    # 两条分支的列定义必须一致：trigram 不可用时会静默降级到 unicode61，只改一条分支
    # 会让降级路径继续被污染（SPEC 4.1 风险 3）。
    columns = "chunk_id UNINDEXED, title, author, text"
    try:
        connection.execute(
            f"CREATE VIRTUAL TABLE chunks_fts USING fts5({columns}, tokenize='trigram')"
        )
        return "trigram"
    except sqlite3.OperationalError:
        connection.execute(
            f"CREATE VIRTUAL TABLE chunks_fts USING fts5({columns}, tokenize='unicode61')"
        )
        return "unicode61"


def add_chunk(connection: sqlite3.Connection, item: dict) -> None:
    row = (
        item["chunk_id"],
        item["source_id"],
        item.get("source_name", ""),
        item.get("document_id", ""),
        item.get("parent_id", ""),
        item.get("chunk_type", "text"),
        item.get("title", ""),
        item.get("date", ""),
        item.get("author_or_guest") or item.get("author", ""),
        join_value(item.get("speakers")),
        join_value(item.get("topics")),
        item.get("claim_type", ""),
        item.get("market_regime", ""),
        item.get("locator", ""),
        item.get("text", ""),
        item.get("original_path", ""),
        item.get("confidence", ""),
        # 新列一律追加在末尾：下面的 FTS 插入按下标取 title/author/text
        # （row[6]/row[8]/row[14]），在中间插列会让这三个下标错位。
        join_value(item.get("image_path")),
    )
    connection.execute(
        "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row
    )
    # row[10] 是 topics，故意不写进 FTS——见 create_schema 的说明。
    # image_path 同样不进 FTS：它是文件路径，不是可检索的自然语言。
    connection.execute(
        "INSERT INTO chunks_fts(chunk_id,title,author,text) VALUES (?,?,?,?)",
        (row[0], row[6], row[8], row[14]),
    )


def load_source_registry() -> dict[str, dict]:
    """从 sources.yaml 读取来源登记表，返回 {source_id: {status, display_name, ...}}。"""
    config = yaml.safe_load(SOURCES_YAML.read_text(encoding="utf-8"))
    return {s["id"]: s for s in config.get("sources", [])}


def main() -> int:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    temporary = INDEX_DIR / "knowledge.db.tmp"
    if temporary.exists():
        temporary.unlink()
    connection = sqlite3.connect(temporary)
    tokenizer = create_schema(connection)
    counts = {"documents": 0, "parents": 0, "chunks": 0, "methods": 0, "conflicts": 0, "methods_skipped": 0}

    registry = load_source_registry()
    # display_name 字典，写 source_name 时从这里取，不硬编码
    display_names = {sid: s.get("display_name", sid) for sid, s in registry.items()}

    skipped: list[str] = []
    for library in sorted(path for path in LIBRARIES.iterdir() if path.is_dir()):
        source_id = library.name
        source_info = registry.get(source_id, {})
        source_status = source_info.get("status", "")
        if source_status not in ALLOWED_STATUSES:
            skipped.append(f"{source_id} (status={source_status!r})")
            continue
        for item in read_jsonl(library / "documents.jsonl") or []:
            connection.execute(
                "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    item["document_id"], item["source_id"], item["title"], item.get("date", ""),
                    item.get("author_or_guest", ""), item.get("content_type", ""), join_value(item.get("topics")),
                    item.get("original_path", ""), item.get("normalized_text_path", ""), item.get("risk_flags", ""),
                ),
            )
            counts["documents"] += 1
        for item in read_jsonl(library / "parents.jsonl") or []:
            connection.execute(
                "INSERT INTO parents VALUES (?,?,?,?,?,?,?,?)",
                (
                    item["parent_id"], item["source_id"], item["document_id"], item["title"],
                    item.get("date", ""), item.get("author_or_guest", ""), item["locator"], item["text"],
                ),
            )
            counts["parents"] += 1
        for item in read_jsonl(library / "chunks.jsonl") or []:
            add_chunk(connection, item)
            counts["chunks"] += 1
        # methods：status 过滤——只导入 status=reviewed 的卡；没有 status 字段视为 draft
        for item in read_jsonl(library / "methods.jsonl") or []:
            if item.get("status", "draft") != "reviewed":
                counts["methods_skipped"] += 1
                continue
            parts = [
                item.get("conclusion", ""), item.get("checklist", ""), item.get("quote", ""),
                item.get("conditions", ""), item.get("invalidation", ""), item.get("risk", ""),
            ]
            # 用户审批时写下的未解问题也进正文，带前缀标明来源。
            # 这些疑惑是该卡的已知缺口（卡答「该看什么」，用户问「怎么看出来」），
            # 检索到卡时必须一并看到，否则会把半完备的卡当成可照做的规则。
            if item.get("user_questions"):
                parts.append(f"用户未解问题：{item['user_questions']}")
            text = "\n".join(part for part in parts if part)
            add_chunk(
                connection,
                {
                    "chunk_id": item["method_id"],
                    "source_id": item["source_id"],
                    "source_name": display_names.get(item["source_id"], item["source_id"]),
                    "chunk_type": "curated_method",
                    "title": item.get("title", "精选方法"),
                    "author": "整理方法卡",
                    "topics": [item.get("topic", "")],
                    "claim_type": "rule",
                    "locator": item.get("locator", ""),
                    "text": text,
                    "confidence": "curated",
                },
            )
            counts["methods"] += 1
        for item in read_jsonl(library / "conflicts.jsonl") or []:
            text = "\n".join(
                f"{label}：{item.get(key, '')}" for label, key in [
                    ("观点A", "view_a"), ("观点B", "view_b"), ("整理判断", "assessment"),
                    ("相关来源", "sources"), ("验证办法", "verification")
                ] if item.get(key)
            )
            add_chunk(
                connection,
                {
                    "chunk_id": item["conflict_id"],
                    "source_id": item["source_id"],
                    "source_name": display_names.get(item["source_id"], item["source_id"]),
                    "chunk_type": "conflict",
                    "title": item.get("topic", "来源内分歧"),
                    "author": "分歧整理",
                    "topics": [item.get("topic", "")],
                    "claim_type": "opinion",
                    "locator": item.get("sources", ""),
                    "text": text,
                    "confidence": "curated",
                },
            )
            counts["conflicts"] += 1

    connection.execute("INSERT INTO metadata VALUES (?,?)", ("generated_at", datetime.now(timezone.utc).isoformat()))
    connection.execute("INSERT INTO metadata VALUES (?,?)", ("fts_tokenizer", tokenizer))
    connection.execute("INSERT INTO metadata VALUES (?,?)", ("counts", json.dumps(counts, ensure_ascii=False)))
    if skipped:
        connection.execute("INSERT INTO metadata VALUES (?,?)", ("skipped_sources", json.dumps(skipped, ensure_ascii=False)))
    connection.commit()
    result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    connection.close()
    if result != "ok":
        raise RuntimeError(f"SQLite integrity check failed: {result}")
    os.replace(temporary, DATABASE)
    output = {"database": str(DATABASE), "tokenizer": tokenizer, **counts}
    if skipped:
        output["skipped_sources"] = skipped
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
