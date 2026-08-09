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
        """
    )
    # chunks 表由 CHUNK_COLUMNS 生成，不在这里手写列名 —— 手写会与 add_chunk 的
    # 插入语句各自漂移，而漂移的后果（FTS 索引错列）不报错。
    connection.execute(
        "CREATE TABLE chunks (%s)"
        % ", ".join(f"{name} {definition}" for name, definition in CHUNK_COLUMNS)
    )
    connection.executescript(
        """
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


# chunks 表的列顺序，**唯一事实来源**。create_schema 的建表语句和 add_chunk 的插入
# 都从这里推导，两处不可能再不一致。
#
# 这张表原先是「建表 SQL 写一遍列名 + 插入时写一个 18 元素元组 + FTS 按 row[6]/row[8]/
# row[14] 取下标」三处各自维护，靠一条注释提醒「新列一律追加在末尾，否则下标错位」。
# 那是靠人记的约束，不是代码保证的约束 —— 加列时只要手滑插在中间，FTS 就会把
# locator 当成 text 索引，而且不报错。现在 FTS 按列名取值，插在哪里都不会错。
CHUNK_COLUMNS: tuple[tuple[str, str], ...] = (
    ("chunk_id", "TEXT PRIMARY KEY"),
    ("source_id", "TEXT NOT NULL"),
    ("source_name", "TEXT"),
    ("document_id", "TEXT"),
    ("parent_id", "TEXT"),
    ("chunk_type", "TEXT NOT NULL"),
    ("title", "TEXT NOT NULL"),
    ("date", "TEXT"),
    ("author", "TEXT"),
    ("speakers", "TEXT"),
    ("topics", "TEXT"),
    ("claim_type", "TEXT"),
    ("market_regime", "TEXT"),
    ("locator", "TEXT NOT NULL"),
    ("text", "TEXT NOT NULL"),
    ("original_path", "TEXT"),
    ("confidence", "TEXT"),
    ("image_path", "TEXT"),
    # ↓ 2026-08-09 新增。两个字段此前只存在于 chunks.jsonl，检索侧取不到 ——
    # 文档却写着「日期敏感的结论要先看 date_precision」，那句话在 query_kb.py 里
    # 是做不到的。
    #
    # extraction_method：6 个来源都有值（仅 fulibei 缺），是全库覆盖最广的
    #   可信度维度 —— 能区分「文本层抽的」和「OCR 认的」，后者数字不可信。
    # date_precision：目前只有 kongkonglong 有值，其余为空字符串。空值是合法的，
    #   表示「该来源的导入器没有评估日期精度」，不等于日期可信。
    ("extraction_method", "TEXT"),
    ("date_precision", "TEXT"),
)

# 从 jsonl 取值的方式：多数列同名直取，少数需要兼容旧键名或做列表拼接。
CHUNK_VALUE_GETTERS = {
    "author": lambda item: item.get("author_or_guest") or item.get("author", ""),
    "speakers": lambda item: join_value(item.get("speakers")),
    "topics": lambda item: join_value(item.get("topics")),
    "image_path": lambda item: join_value(item.get("image_path")),
    "chunk_type": lambda item: item.get("chunk_type", "text"),
}

# 进 FTS 的列。topics 故意不进（自动标签会压过正文命中，见 create_schema）；
# image_path / extraction_method / date_precision 也不进 —— 它们是路径和枚举值，
# 不是可检索的自然语言，进了 FTS 只会让「ocr」这类词命中几千块。
FTS_COLUMNS = ("title", "author", "text")


def add_chunk(connection: sqlite3.Connection, item: dict) -> None:
    values = {
        name: CHUNK_VALUE_GETTERS.get(name, lambda item, name=name: item.get(name, ""))(item)
        for name, _ in CHUNK_COLUMNS
    }
    # chunk_id 与 source_id 是必需的，缺了要炸而不是写空字符串
    values["chunk_id"] = item["chunk_id"]
    values["source_id"] = item["source_id"]
    placeholders = ",".join("?" for _ in CHUNK_COLUMNS)
    connection.execute(
        f"INSERT INTO chunks VALUES ({placeholders})",
        tuple(values[name] for name, _ in CHUNK_COLUMNS),
    )
    connection.execute(
        f"INSERT INTO chunks_fts(chunk_id,{','.join(FTS_COLUMNS)}) "
        f"VALUES (?,{','.join('?' for _ in FTS_COLUMNS)})",
        (values["chunk_id"], *(values[name] for name in FTS_COLUMNS)),
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
    try:
        os.replace(temporary, DATABASE)
    except PermissionError:
        # Windows 上常驻进程（MCP 服务、编辑器索引器）会以
        # FILE_SHARE_READ|FILE_SHARE_WRITE 但**不带 FILE_SHARE_DELETE** 的模式
        # 持有 knowledge.db。这种句柄下文件「可写但不可改名/删除」，
        # 所以 os.replace 必然抛 WinError 5 —— 实测连续 8 次全失败，不是瞬时冲突，
        # 重试没有意义。
        #
        # 退路是用 sqlite 的 backup API 把新库内容**原地灌进**已存在的文件：
        # 它只需要写权限，不需要删除权限，也不会打断那些进程的连接。
        # 2026-08-09 实测：灌入后 integrity_check=ok、记录数与 tmp 完全一致。
        source = sqlite3.connect(str(temporary))
        target = sqlite3.connect(str(DATABASE))
        try:
            source.backup(target)
            target.commit()
            swapped = target.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            source.close()
            target.close()
        if swapped != "ok":
            raise RuntimeError(
                f"原地灌入后完整性检查失败：{swapped}。新库仍在 {temporary}，请手工处理"
            )
        temporary.unlink(missing_ok=True)
        print("注：knowledge.db 被其他进程持有，已改用 sqlite backup 原地覆盖（内容一致）")
    output = {"database": str(DATABASE), "tokenizer": tokenizer, **counts}
    if skipped:
        output["skipped_sources"] = skipped
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
