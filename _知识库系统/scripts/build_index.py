#!/usr/bin/env python3
"""Build an atomic SQLite metadata and FTS5 index from integrated source libraries."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LIBRARIES = ROOT / "_知识库系统" / "source_libraries"
INDEX_DIR = ROOT / "_知识库系统" / "indexes"
DATABASE = INDEX_DIR / "knowledge.db"


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
            confidence TEXT
        );
        CREATE INDEX idx_chunks_source ON chunks(source_id);
        CREATE INDEX idx_chunks_author ON chunks(author);
        CREATE INDEX idx_chunks_document ON chunks(document_id);
        """
    )
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE chunks_fts USING fts5(chunk_id UNINDEXED, title, author, topics, text, tokenize='trigram')"
        )
        return "trigram"
    except sqlite3.OperationalError:
        connection.execute(
            "CREATE VIRTUAL TABLE chunks_fts USING fts5(chunk_id UNINDEXED, title, author, topics, text, tokenize='unicode61')"
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
    )
    connection.execute(
        "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row
    )
    connection.execute(
        "INSERT INTO chunks_fts(chunk_id,title,author,topics,text) VALUES (?,?,?,?,?)",
        (row[0], row[6], row[8], row[10], row[14]),
    )


def main() -> int:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    temporary = INDEX_DIR / "knowledge.db.tmp"
    if temporary.exists():
        temporary.unlink()
    connection = sqlite3.connect(temporary)
    tokenizer = create_schema(connection)
    counts = {"documents": 0, "parents": 0, "chunks": 0, "methods": 0, "conflicts": 0}

    for library in sorted(path for path in LIBRARIES.iterdir() if path.is_dir()):
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
        for item in read_jsonl(library / "methods.jsonl") or []:
            text = "\n".join(
                part for part in [item.get("conclusion", ""), item.get("checklist", ""), item.get("quote", ""),
                                  item.get("conditions", ""), item.get("invalidation", ""), item.get("risk", "")] if part
            )
            add_chunk(
                connection,
                {
                    "chunk_id": item["method_id"], "source_id": item["source_id"], "source_name": "复利杯",
                    "chunk_type": "curated_method", "title": item.get("title", "精选方法"),
                    "author": "整理方法卡", "topics": [item.get("topic", "")], "claim_type": "rule",
                    "locator": item.get("locator", ""), "text": text, "confidence": "curated",
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
                    "chunk_id": item["conflict_id"], "source_id": item["source_id"], "source_name": "复利杯",
                    "chunk_type": "conflict", "title": item.get("topic", "来源内分歧"), "author": "分歧整理",
                    "topics": [item.get("topic", "")], "claim_type": "opinion", "locator": item.get("sources", ""),
                    "text": text, "confidence": "curated",
                },
            )
            counts["conflicts"] += 1

    connection.execute("INSERT INTO metadata VALUES (?,?)", ("generated_at", datetime.now(timezone.utc).isoformat()))
    connection.execute("INSERT INTO metadata VALUES (?,?)", ("fts_tokenizer", tokenizer))
    connection.execute("INSERT INTO metadata VALUES (?,?)", ("counts", json.dumps(counts, ensure_ascii=False)))
    connection.commit()
    result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    connection.close()
    if result != "ok":
        raise RuntimeError(f"SQLite integrity check failed: {result}")
    os.replace(temporary, DATABASE)
    print(json.dumps({"database": str(DATABASE), "tokenizer": tokenizer, **counts}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
