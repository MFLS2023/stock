#!/usr/bin/env python3
"""Query the local trading knowledge base with optional source and author filters."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATABASE = ROOT / "_知识库系统" / "indexes" / "knowledge.db"


def terms_from_query(query: str) -> list[str]:
    terms = [part for part in re.split(r"[\s,，。；;、]+", query.strip()) if part]
    return terms or [query.strip()]


def search(connection: sqlite3.Connection, query: str, source: str | None, author: str | None, limit: int):
    terms = terms_from_query(query)
    filters = []
    params: list[object] = []
    if source:
        filters.append("c.source_id = ?")
        params.append(source)
    if author:
        filters.append("(c.author LIKE ? OR c.title LIKE ?)")
        params.extend([f"%{author}%", f"%{author}%"])
    filter_sql = (" AND " + " AND ".join(filters)) if filters else ""

    fts_terms = [term for term in terms if len(term) >= 3]
    rows = []
    if fts_terms:
        fts_query = " OR ".join('"' + term.replace('"', '""') + '"' for term in fts_terms)
        sql = f"""
            SELECT c.*, bm25(chunks_fts) AS rank
            FROM chunks_fts
            JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
            WHERE chunks_fts MATCH ? {filter_sql}
            ORDER BY rank
            LIMIT ?
        """
        rows = connection.execute(sql, [fts_query, *params, limit]).fetchall()

    if not rows:
        like_clauses = []
        like_params: list[object] = []
        for term in terms:
            like_clauses.append("(c.text LIKE ? OR c.title LIKE ? OR c.author LIKE ? OR c.topics LIKE ?)")
            like_params.extend([f"%{term}%"] * 4)
        candidate_limit = max(limit * 30, 120)
        sql = f"""
            SELECT c.*, 999.0 AS rank
            FROM chunks c
            WHERE ({' OR '.join(like_clauses)}) {filter_sql}
            LIMIT ?
        """
        candidates = connection.execute(sql, [*like_params, *params, candidate_limit]).fetchall()

        def relevance(row) -> float:
            text = (row["text"] or "").lower()
            title = (row["title"] or "").lower()
            row_author = (row["author"] or "").lower()
            topics = (row["topics"] or "").lower()
            score = 0.0
            for term in terms:
                token = term.lower()
                score += min(text.count(token), 8) * 1.0
                score += min(title.count(token), 3) * 3.0
                score += min(row_author.count(token), 2) * 5.0
                score += min(topics.count(token), 3) * 4.0
            if row["chunk_type"] == "curated_method":
                score += 5.0
            elif row["chunk_type"] == "conflict":
                score += 2.0
            return score

        rows = sorted(candidates, key=relevance, reverse=True)[:limit]
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    parser.add_argument("--source")
    parser.add_argument("--author")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--show-parent", action="store_true")
    args = parser.parse_args()

    if not DATABASE.exists():
        raise FileNotFoundError(f"Index not found. Run build_index.py first: {DATABASE}")
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    rows = search(connection, args.query, args.source, args.author, args.limit)
    results = []
    for row in rows:
        item = dict(row)
        if args.show_parent and item.get("parent_id"):
            parent = connection.execute("SELECT text, locator FROM parents WHERE parent_id=?", (item["parent_id"],)).fetchone()
            if parent:
                item["parent_text"] = parent["text"]
                item["parent_locator"] = parent["locator"]
        results.append(item)
    connection.close()

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    if not results:
        print("未找到匹配结果。")
        return 1
    for index, item in enumerate(results, start=1):
        preview = item["text"].replace("\n", " ")[:420]
        citation = f"[{item.get('source_name') or item['source_id']}｜{item.get('author') or '未标注'}｜{item['title']}｜{item.get('date') or '日期未标注'}｜{item['locator']}]"
        print(f"\n#{index} {citation}")
        print(f"类型: {item['chunk_type']} | 主题: {item.get('topics') or '未标注'} | ID: {item['chunk_id']}")
        print(preview)
        if args.show_parent and item.get("parent_text"):
            print("\n父块上下文：")
            print(item["parent_text"][:1800])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
