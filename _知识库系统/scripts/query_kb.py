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

# trigram 分词器要三个字符才能建一个 gram，所以两字词的 MATCH 恒为 0（SPEC 缺陷 B）。
# 短于这个长度的词改走 GLOB 子串路径。不换 tokenizer：换了要重建索引且会重排全库
# 召回特性，改动不可逆（SPEC 2.3）。
MATCH_MIN_LENGTH = 3

# 召回字段：人写的三列。topics 由 infer_topics() 按关键词计数自动打，标签文本本身
# 从不出现在正文里，阶段 1 已把它移出 FTS 可匹配列，召回同样不带它（SPEC 2.2）。
# 顺序无所谓——三列各自 GLOB 后取并集。
RECALL_COLUMNS = ("text", "title", "author")

# GLOB 子串路径不产生 bm25 分数，排序由 relevance() 在 Python 侧完成，所以这条路径
# 的 rank 只是个占位值。
#
# 旧实现在这里放的是 999.0，标记"这批结果来自 LIKE 兜底"。兜底连同它的
# candidate_limit 一起删掉了——那是缺陷 C 的成因（按 rowid 截断候选池，结果系统性
# 偏向插入顺序最靠前的来源），所以 999.0 这个值不再出现。
#
# 换成 0.0 而不是继续沿用 999.0，是因为一批验收断言的写法是 rank != 999.0，语义为
# "不是靠兜底给的"。阶段 3 统一 bm25 与 relevance 的标尺时再定义 rank 的数值含义；
# 阶段 2 不碰权重，只保证这两把尺子在一次查询里不混用。
SUBSTRING_RANK = 0.0


def glob_literal(term: str) -> str:
    """把用户输入转成只匹配自身的 GLOB 模式片段。

    SQLite 的 GLOB 没有 ESCAPE 子句（LIKE 才有），反斜杠也不是转义符，唯一的办法是
    把元字符包进字符类：``[*]`` 只匹配一个字面星号。三个元字符要转——``*``（任意长）、
    ``?``（任意一字）、``[``（字符类开始）。``]`` 不用转：在 GLOB 里它只有紧跟在
    ``[`` 后面才特殊，单独出现就是字面量。

    ``[`` 必须第一个替换。反过来的话，先转 ``*`` 得到的 ``[*]`` 里那个 ``[`` 会被第二遍
    再转一次，变成 ``[[]*]``，匹配的东西完全不对。

    不转义的后果实测（真库，2026-08-02）：查 ``*`` 返回全部 3362 块而字面命中只有 45 块，
    查 ``?`` 返回 3362 对 614，查 ``竞*`` 返回 534 而字面 0 块；查 ``[`` 更糟，未闭合的
    字符类让整个模式失效，返回 0 块而字面命中有 1864 块。前者是把无关内容当成命中，
    后者是把真实命中全丢掉，两个方向都错。
    """
    return term.replace("[", "[[]").replace("*", "[*]").replace("?", "[?]")


def glob_pattern(term: str) -> str:
    """``*字面词*``：转义之后才在两侧加通配符，否则自己加的星号也会被转掉。"""
    return f"*{glob_literal(term)}*"


def terms_from_query(query: str) -> list[str]:
    terms = [part for part in re.split(r"[\s,，。；;、]+", query.strip()) if part]
    return terms or [query.strip()]


def split_terms_by_length(terms: list[str]) -> tuple[list[str], list[str]]:
    """把检索词分成"能进 MATCH 的"和"要走 GLOB 的"两组。

    每个词独立分流。旧实现是 ``[t for t in terms if len(t) >= 3]``，短词被整个丢掉：
    查 ``竞价 弱转强`` 与单查 ``弱转强`` 返回完全一样的结果，而用户看不出短词的
    贡献是零（SPEC 缺陷 B）。分流之后两组各自召回、结果取并集，长词在场不再让短词
    被静默吞掉。
    """
    long_terms = [term for term in terms if len(term) >= MATCH_MIN_LENGTH]
    short_terms = [term for term in terms if len(term) < MATCH_MIN_LENGTH]
    return long_terms, short_terms


def match_expression(terms: list[str]) -> str:
    """OR 连接的 FTS5 短语表达式。多词查询语义为 OR，任一词命中即入候选。"""
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def build_filters(source: str | None, author: str | None) -> tuple[str, list[object]]:
    """``--source`` / ``--author`` 的 SQL 片段，供每一条召回路径共用。

    只生成一次并在最外层套用，而不是每条路径各写一遍：漏在任何一条上，过滤器就会
    在那条路径上静默失效，而结果看起来仍然像是过滤过的。
    """
    clauses: list[str] = []
    params: list[object] = []
    if source:
        clauses.append("c.source_id = ?")
        params.append(source)
    if author:
        # 故意放宽到 title：作者名常常只写在标题里。
        clauses.append("(c.author LIKE ? OR c.title LIKE ?)")
        params.extend([f"%{author}%", f"%{author}%"])
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def relevance(row: sqlite3.Row, terms: list[str]) -> float:
    """GLOB 路径的排序分。阶段 2 一个权重都没动。

    字段权重方向是反的——topics 4.0 高于正文 1.0，而 topics 是自动标签。这是缺陷 C
    的排序部分，归阶段 3（SPEC 2.2 评分层）。阶段 2 只负责召回和去重，改这里会让
    阶段 3 的验收失去基线。
    """
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


def search(connection: sqlite3.Connection, query: str, source: str | None, author: str | None, limit: int):
    """SPEC 2.2 的召回层：找全、不重复，然后才截断。

    两条路径，永不混用标尺：

    * 全部检索词都 ≥3 字 → 一次 ``MATCH``，bm25 排序（与阶段 1 之前一致）。
    * 出现短词 → 候选集是 ``MATCH`` 命中与三列 ``GLOB`` 命中的并集（模式先经
      glob_pattern() 转义，用户输入的 ``*``/``?``/``[`` 只匹配自身），
      由 relevance() 排序。这一路没有 bm25 值可用，而 bm25 是负值、尺度与
      relevance() 不同，混着排等于凭空发明一个换算规则——那是阶段 3 的事。

    两条路径都先拿到完整候选再在 Python 侧切片。SQL 里不留任何 rowid 预截断：
    旧实现的 ``LIMIT max(limit*30, 120)`` 没有 ORDER BY，按插入顺序截断，
    郁金香的命中一条都进不来（SPEC 缺陷 C）。全库 3176 块，全量候选代价可接受。
    """
    terms = [term for term in terms_from_query(query) if term]
    if not terms or limit <= 0:
        # 空查询、纯标点查询走到这里：返回空列表，不抛异常，也不当成一次真的检索
        # （LIKE '%%' 会匹配全表，那才是最坏的结果——看起来像检索到了东西）。
        return []

    filter_sql, filter_params = build_filters(source, author)
    long_terms, short_terms = split_terms_by_length(terms)

    if not short_terms:
        sql = f"""
            SELECT c.*, bm25(chunks_fts) AS rank
            FROM chunks_fts
            JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
            WHERE chunks_fts MATCH ? {filter_sql}
            ORDER BY rank
        """
        rows = connection.execute(sql, [match_expression(long_terms), *filter_params]).fetchall()
        return rows[:limit]

    # 子查询逐条只查 chunks_fts，不 JOIN chunks；UNION 去重后由外层一次套用过滤器。
    # 这样过滤器不可能漏在某条路径上，chunk_id 也天然唯一——一个块被多个词、多个
    # 字段、两条路径同时命中，仍然只返回一份（SPEC 2.2 去重层）。
    #
    # 写成 IN + UNION 而不是"先在 Python 里取 id 集合再 IN (?,?,...)"：真库里
    # 『情绪』的并集是 1303 条，绑定变量个数会撞上 SQLite 的上限。
    clauses: list[str] = []
    params: list[object] = []
    if long_terms:
        clauses.append("SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ?")
        params.append(match_expression(long_terms))
    for term in short_terms:
        for column in RECALL_COLUMNS:
            # GLOB 是精确子串且大小写敏感，实测在 trigram 表上走索引
            # （VIRTUAL TABLE INDEX 0:G4，18.2ms vs chunks 全表扫 191.4ms）。
            # 模式经 glob_pattern() 转义，用户输入里的 * ? [ 只匹配自身。
            clauses.append(f"SELECT chunk_id FROM chunks_fts WHERE {column} GLOB ?")
            params.append(glob_pattern(term))

    sql = f"""
        SELECT c.*, {SUBSTRING_RANK} AS rank
        FROM chunks c
        WHERE c.chunk_id IN ({' UNION '.join(clauses)}) {filter_sql}
        ORDER BY c.rowid
    """
    candidates = connection.execute(sql, [*params, *filter_params]).fetchall()
    # ORDER BY c.rowid 只是把旧实现"无 ORDER BY 时按 rowid 返回"的隐含行为写明，
    # 好让下面这次排序有个确定的输入顺序（sorted 是稳定排序，并列仍按 rowid）。
    # 不是新增决胜规则——按 chunk_id 决胜归阶段 3。
    candidates.sort(key=lambda row: relevance(row, terms), reverse=True)
    return candidates[:limit]


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
