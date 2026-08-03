#!/usr/bin/env python3
"""Tests for the retrieval layer: index shape, short-term search, source coverage.

Run from the project root, so relative paths resolve the same way in every shell.
``-m unittest test_query_kb`` fails to import from the root (scripts is not on
sys.path), so always go through ``discover``:

    export PYTHONIOENCODING=utf-8                       # Git Bash; PowerShell: $env:...
    python -m unittest discover -s _知识库系统/scripts -t _知识库系统/scripts -v

Two subsets, for the rebuild ordering in SPEC 3.2. Step 1 runs before the index is
rebuilt, so it must not touch the database at all — otherwise it reads the *old*
index and reports on data the run is about to replace:

    FIXTURE="-k FixtureShapeTests -k IndexPollutionTests -k ShortTermSearchTests \
             -k SourceCoverageTests -k RetrievalContractTests -k GlobEscapingTests \
             -k AsciiCaseRecallTests -k SubsetMarkerTests -k SourceRegistryTests"
    REAL="-k RealIndexRequirementTests -k RealIndexTests -k RegistryScopedIndexTests"

    python -m unittest discover $S $FIXTURE -v                      # 步骤 1，重建索引前
    KB_REQUIRE_REAL_INDEX=1 python -m unittest discover $S $REAL -v  # 步骤 3，重建之后

``test_the_subset_markers_cover_every_test_class`` keeps those two lists exhaustive:
add a class without listing it and that test fails. And
``test_the_documented_commands_list_every_subset_class`` keeps *these* command lines in
sync with the lists — the earlier revision only guarded the code constants, so both this
docstring and SPEC 3.2 silently dropped ``-k GlobEscapingTests`` and the documented
fixture command ran 54 cases where 64 were intended. The two lists above are written as
``FIXTURE=``/``REAL=`` assignments, in the same shape as SPEC 3.2's Git Bash block, so
that guard can locate and check **each** list on its own instead of pooling every ``-k``
in the file into one set — pooling let one list cover for another's omission.

Two source scopes, deliberately kept apart — see REGRESSION_SAMPLE and source_registry().
Frozen row counts and hit counts are asserted against the sample; everything phrased as
"retrieval must reach it" is asserted against sources.yaml, so a newly approved source is
covered without editing this file.

Nor may a coverage assertion assume what a future source contains. No fixed term list, no
"every source must hold this word", no "every source must have an author": search terms are
either declared per source in sources.yaml (smoke_query) or derived from that source's own
text, author coverage is scoped to sources that carry authorship, and per-term coverage is
computed from which sources actually hold matches. See smoke_query_for().

**本文件已不含任何 ``expectedFailure``。** 阶段 3 摘除了最后一个，全轮四个阶段的
15 个标记全部归零（SPEC 3.0 的生命周期表：阶段 0 留 15、阶段 1 留 12、阶段 2 留 1、
阶段 3 留 0）。历史上这些标记描述的是 SPEC 要求而当时代码不满足的行为，规则是：

1. 修复落地时用例翻成 "unexpected success"，unittest 退出码是 **1** 而不是 0——
   那是红灯不是绿灯。所以装饰器必须与修复在**同一个提交**里删掉，再重跑到普通 PASS。
2. 输出里不得出现 ``unexpected successes`` 或 ``skipped``。前者是"修好了但标记没摘"，
   后者是"这批断言根本没跑"。
3. Set ``KB_REQUIRE_REAL_INDEX=1`` to turn a missing knowledge.db from a silent skip
   into a hard failure — without it, RealIndexTests can skip 20-odd assertions and
   still print "OK".

阶段 3 摘除的那一项是 ``SourceCoverageTests.test_prose_matches_must_outrank_label_only_matches``，
按修复后的事实改名为 ``test_prose_matches_outrank_title_and_author_only_matches``：
阶段 2 把 topics 移出召回之后，压过正文命中的已经不是标签块（那些块根本进不了候选集），
而是仅标题/仅作者含词的块，名字里的 ``label_only`` 随之改掉。同一提交里另有一条阶段 2
的断言被改写——``test_default_limit_no_longer_collapses_to_the_first_source`` 当年成立
靠的正是 author 5.0 / title 3.0 压过正文 1.0，即本阶段要修的那个缺陷，所以权重一改它就
翻红；它同时是一条隐性来源配额（SPEC 阶段 3 明确不检查小 limit 结果的来源数量），
改成钉"前 8 条一条都不在旧候选池里"。

阶段 2 摘除了 11 项，其中 9 项按修复后的事实改写并改名——名字里的 ``must_`` 和
``fallback`` 去掉了，因为 LIKE 兜底路径连同它按 rowid 截断的候选池一起被删除，
``rank == 999.0`` 这个哨兵值不再出现在任何路径上。
"""

from __future__ import annotations

import inspect
import itertools
import json
import os
import random
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from build_index import add_chunk, create_schema
from query_kb import (
    ASCII_FOLD,
    FIELD_WEIGHTS,
    MATCH_MIN_LENGTH,
    UNICODE_FOLD,
    UNICODE_FOLD_TABLE,
    FieldFolds,
    FoldedTerm,
    fold,
    fold_kind_for,
    fold_unicode,
    folded_terms,
    glob_fold_ascii_case,
    glob_literal,
    glob_pattern,
    matched_term_count,
    needs_cased_fold,
    prose_hit,
    rank_and_truncate,
    ranking_key,
    search,
    split_terms_by_length,
    terms_from_query,
)


# infer_topics() labels every chunk with 5-6 topics drawn from taxonomy.yaml. The
# label itself never appears in the prose, so whatever it matches is pure noise.
TOPIC_LABEL = "竞价与盘口"


def with_term(seed: int) -> str:
    """Prose carrying the two-character term and a three-character one.

    Each chunk gets distinct filler so bm25 produces distinct scores. Identical text
    would make every score tie, and a tied ORDER BY falls back to rowid, which would
    fake the very skew these tests are meant to isolate.
    """
    return (
        f"第{seed}节讨论开盘前的竞价，竞价量能决定当天的承接强度，"
        f"这是典型的弱转强结构，编号{seed}的案例可以对照阅读。"
    )


def without_term(seed: int) -> str:
    """Prose without either term, but still tagged with the auto label."""
    return f"第{seed}节记录当天盘面整体偏弱，指数在低位徘徊，没有明显主线可以跟随。"


# A second two-character term, present in tulip_garden's prose and nowhere else. Lets a
# multi-short-term query be checked for OR semantics: on the real corpus 情绪 sits at
# 889 prose matches spread 369/151/369, so a query pairing it with another short term
# must not come back holding only one source's chunks.
# （清洁前是 888、分布 369/150/369，那组是历史值。）
SECOND_TERM = "情绪"


def with_second_term(seed: int) -> str:
    """Prose carrying only SECOND_TERM — no 竞价, no 弱转强.

    Kept disjoint from with_term() on purpose. If these chunks also matched the
    three-character term, a mixed-length query could reach them through the FTS branch
    alone, and dropping the short term would look harmless.
    """
    return (
        f"第{seed}节复盘当天的{SECOND_TERM}温度，{SECOND_TERM}从冰点回暖后承接才敢接，"
        f"编号{seed}的盘面可以对照。"
    )


def chunk(
    source_id: str,
    index: int,
    text: str,
    *,
    topics: str = TOPIC_LABEL,
    title: str = "示例文档",
    author: str = "示例作者",
    chunk_type: str = "transcript",
) -> dict:
    return {
        "chunk_id": f"{source_id}-{index:04d}",
        "source_id": source_id,
        "source_name": source_id,
        "document_id": f"{source_id}-doc",
        "parent_id": f"{source_id}-p001",
        "chunk_type": chunk_type,
        "title": title,
        "date": "2026-01-01",
        "author": author,
        "topics": topics,
        "claim_type": "opinion_or_case",
        "locator": f"第{index}段",
        "text": text,
        "confidence": "medium",
    }


def fts5_folded(text: str) -> str:
    """把整串交给**真** trigram 分词器，从它切出的 gram 序列重建它眼里的折叠形式。

    这是折叠差分测试的基准，必须由 FTS5 现场给出。硬编码期望值等于把当前 SQLite
    版本的行为抄成常量：换个版本，测试会变成"在说谎"而不是"在报警"。

    长度 n 的串产出 n-2 个 trigram，第 i 个恰是 ``folded[i:i+3]``，所以折叠形式
    = 第 0 个 term + 其后每个 term 的末字符。读法与 query_kb.build_fold_table() 相同，
    区别是那边喂码点区间、这边喂任意串——**上下文相关的折叠只在整串里暴露**，
    ``str.lower()`` 对词尾 ``Σ`` 的 Final_Sigma 映射就是逐字符测不出来的那一类。
    """
    if len(text) < 3:
        raise ValueError("trigram 对短于 3 字符的串不产 token，无法反推折叠形式")
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE p USING fts5(t, tokenize='trigram')")
        connection.execute("INSERT INTO p(t) VALUES (?)", (text,))
        connection.execute("CREATE VIRTUAL TABLE v USING fts5vocab(p, 'instance')")
        rows = connection.execute("SELECT offset, term FROM v ORDER BY offset").fetchall()
    finally:
        connection.close()
    if [offset for offset, _ in rows] != list(range(len(text) - 2)):
        raise RuntimeError("FTS5 trigram 的 offset 序列不连续，无法反推折叠形式")
    return rows[0][1] + "".join(term[2] for _, term in rows[1:])


# 三套折叠规则分歧最集中的四组字符，用户复审点名要覆盖的就是这四组。
# 取全部三字排列（27 + 27 + 8 + 64 = 126 例），每例再配一份中文上下文。
FOLD_DIFF_GROUPS = (
    ("sigma", "Σσς"),      # 词尾 Σ：lower() 走 Final_Sigma 折成 ς，FTS5 折成 σ
    ("long s", "Ssſ"),     # ſ：FTS5 折成 s，lower() 原样不动（它本就是小写）
    ("sharp s", "ẞß"),     # ẞ：FTS5 折成 ß，casefold() 展开成 ss，长度都变了
    ("dotted I", "İIiı"),  # İ：FTS5 不折，lower() 拆成 i + U+0307 两个码点
)


# Chunks whose TITLE carries the two-character term while the prose does not. On the real
# corpus this is a large share of legitimate results — 310 chunks for 龙头, 405 for 情绪,
# 58 for 竞价 — because titles are human-written, unlike the topics column. SPEC 2.2
# therefore recalls text/title/author, and these fixture rows are what proves a
# text-only implementation would drop them.
#
# 情绪 是 405 而不是 406：数据清洁（clean_text 删 NUL）之后有一块的正文不再被 NUL 截断，
# 它从"仅标题含词"变成"正文也含词"。406 是清洁前的**历史值**。
TITLE_WITH_TERM = "竞价专题整理"
AUTHOR_WITH_TERM = "竞价老师"

# GLOB 元字符。两字词走 GLOB '*词*'，所以用户输入里的这几个字符会被当成通配语法：
# 查 * 会匹配所有块，查 竞* 会匹配"竞"后面接任意内容，而 [ 单独出现让字符类不闭合，
# 整个模式失效、一条都不匹配。转义前实测（真库 2026-08-02）：* 召回 3362 而字面命中
# 45，? 召回 3362 对 614，竞* 召回 534 对 0，[ 召回 0 对 1864。
#
# fixture 里必须真有含这些字符的块，否则"召回 == 字面并集"两边都是空集，断言空转。
# 每个字符给一块，正文里写死在固定位置。
GLOB_METACHARACTER_TERMS = ("*", "?", "[", "**", "竞*", "A?")


def build_fixture() -> sqlite3.Connection:
    """A miniature knowledge.db that reproduces the real index's skew.

    Chunk counts and insertion order mirror the production database: build_index.py
    walks source_libraries in directory-name order, so fulibei lands at the lowest
    rowids and dominates any query that reads rows without an ORDER BY. fulibei is
    given enough chunks to fill the fallback candidate pool on its own, which is
    exactly why 竞价/龙头/打板/情绪 returned nothing but fulibei on the real corpus.
    """
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    tokenizer = create_schema(connection)

    # fulibei: 300 chunks, and the 20 that mention the term sit at the END of its
    # block. That ordering matters: an unordered LIMIT 240 fills up on fulibei chunks
    # that carry only the auto label, so the pool is both source-skewed and starved
    # of real matches — the same combination measured on the real corpus.
    for index in range(1, 301):
        text = with_term(index) if index > 280 else without_term(index)
        add_chunk(connection, chunk("fulibei", index, text))
    # nanjinglu_bian and tulip_garden sit behind fulibei in rowid order but hold far
    # more on-topic prose, mirroring tulip_garden's 1140 竞价 chunks in production.
    for index in range(1, 41):
        add_chunk(connection, chunk("nanjinglu_bian", index, with_term(1000 + index)))
    # The last 30 tulip_garden chunks also carry SECOND_TERM, so a two-short-term query
    # has a correct answer that differs from either term's answer alone.
    for index in range(1, 121):
        text = with_second_term(2000 + index) if index > 90 else with_term(2000 + index)
        add_chunk(connection, chunk("tulip_garden", index, text))
    # 10 chunks where only the title carries 竞价, and 5 where only the author does. Their
    # prose is deliberately free of both terms, so a text-only recall path misses all 15.
    for index in range(200, 210):
        add_chunk(
            connection,
            chunk("nanjinglu_bian", index, without_term(index), title=TITLE_WITH_TERM),
        )
    for index in range(300, 305):
        add_chunk(
            connection,
            chunk("tulip_garden", index, without_term(index), author=AUTHOR_WITH_TERM),
        )
    connection.execute("INSERT INTO metadata VALUES (?,?)", ("fts_tokenizer", tokenizer))
    connection.commit()
    return connection


def build_glob_fixture() -> sqlite3.Connection:
    """一个专门装 GLOB 元字符的小库，与 build_fixture() 分开。

    没有把这些块加进主 fixture，是因为那边有一批钉死的绝对数字——标题命中 465、
    165/310/475、两词并集 195——多塞几块就全要跟着改，而那些数字守的是别的性质。
    这里只有元字符这一件事，形状可以随它自己的需要长。

    每个元字符词一块，正文里字面写上它。另外三块用来卡住反向情形：
    ``只有一个星号`` 那块让 ``**`` 有机会误命中（转义正确则不命中），
    ``普通块`` 完全不含元字符，``竞价块`` 证明含元字符的查询不影响普通词的召回。
    """
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    tokenizer = create_schema(connection)
    for index, text in enumerate(
        [
            "第1节的记录里出现了 * 这样的符号，属于原始文本的一部分。",
            "第2节写到 ? 这个问号，是作者自己打上去的。",
            "第3节里有 [ 这个方括号，没有配对的右括号。",
            "第4节连着写了 ** 两个星号，用来强调。",
            "第5节提到 竞* 这种缩写形式。",
            "第6节的表格标记是 A? 这样。",
            "第7节只有一个星号 * 单独出现，后面不再跟第二个。",
            "第8节是完全普通的复盘记录，一个特殊符号都没有。",
            "第9节讨论开盘前的竞价，与符号无关。",
        ],
        start=1,
    ):
        add_chunk(connection, chunk("tulip_garden", index, text, topics="盘口"))
    # 标题和作者各一块，证明三列都经过同样的转义——只对 text 转义会让另两列出错。
    add_chunk(
        connection,
        chunk("tulip_garden", 50, "第50节正文不含符号。", title="标题里有 * 星号"),
    )
    add_chunk(
        connection,
        chunk("tulip_garden", 51, "第51节正文不含符号。", author="作者名带 ? 号"),
    )
    connection.execute("INSERT INTO metadata VALUES (?,?)", ("fts_tokenizer", tokenizer))
    connection.commit()
    return connection


# 两字 ASCII 词的大小写。短词走 GLOB，而 GLOB 是大小写敏感的——这是它与 MATCH、LIKE
# 唯一不一致的地方。修复前真库实测（2026-08-02）：AI 召回 144、ai 83、Ai 3，而三列不区分
# 大小写并集是 218，三个查询各自丢 74/135/215 条；而三字以上走 MATCH 的 AI硬件/ai硬件
# 同为 8 块，旧 LIKE 兜底路径也不区分大小写。所以这是短词路径独有的回归。
#
# fixture 里正文、标题、作者三处各放不同的书写形式，任一处漏折叠都会在等值断言上露出来。
CASE_TERM = "AI"
CASE_FORMS = ("AI", "ai", "Ai", "aI")

# 大小写块用独立的 source_id，好把它们与主 fixture 那批钉死的绝对数字隔开。
CASE_SOURCE = "case_fixture"


def build_case_fixture() -> sqlite3.Connection:
    """一个专门装 ASCII 大小写形式的小库，与 build_fixture() 分开。

    分开的理由同 build_glob_fixture()：主 fixture 里有一批钉死的绝对数字（标题命中
    465、165/310/475、两词并集 195），多塞几块就全要跟着改，而那些数字守的是别的性质。

    四种书写形式（AI / ai / Ai / aI）分布在正文、标题、作者三列上，每列至少两种，
    这样"只折叠了 text 列"或"只处理了全大写形式"的实现都会在等值断言上失败。
    另外放两块完全不含该词的，让"召回 == 并集"两边都不等于全表——否则一个直接返回
    全库的实现也能通过。
    """
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    tokenizer = create_schema(connection)
    # 正文里四种形式各一块。
    for index, form in enumerate(CASE_FORMS, start=1):
        add_chunk(
            connection,
            chunk(
                CASE_SOURCE,
                index,
                f"第{index}节讨论 {form} 硬件方向的持续性，属于正文含词。",
                topics="盘口",
            ),
        )
    # 标题含词、正文不含：两种形式，验证 title 列同样折叠。
    for index, form in enumerate(("AI", "ai"), start=10):
        add_chunk(
            connection,
            chunk(
                CASE_SOURCE,
                index,
                f"第{index}节正文只讲大盘节奏，不含那个英文词。",
                title=f"{form}方向专题",
                topics="盘口",
            ),
        )
    # 作者含词、正文和标题都不含：两种形式，验证 author 列同样折叠。
    for index, form in enumerate(("Ai", "aI"), start=20):
        add_chunk(
            connection,
            chunk(
                CASE_SOURCE,
                index,
                f"第{index}节正文只讲仓位管理，不含那个英文词。",
                title="仓位专题",
                author=f"{form}研究员",
                topics="盘口",
            ),
        )
    # 混合大小写的三字以上词，用来对照 MATCH 分支的大小写行为。
    for index, form in enumerate(("AI硬件", "ai硬件"), start=30):
        add_chunk(
            connection,
            chunk(CASE_SOURCE, index, f"第{index}节复盘 {form} 的分歧转一致。", topics="盘口"),
        )
    # 不含该词的对照块，防止"返回全表"也能通过等值断言。
    for index in range(40, 42):
        add_chunk(
            connection,
            chunk(CASE_SOURCE, index, f"第{index}节是完全普通的复盘记录。", topics="盘口"),
        )
    connection.execute("INSERT INTO metadata VALUES (?,?)", ("fts_tokenizer", tokenizer))
    connection.commit()
    return connection


def case_insensitive_ids(connection: sqlite3.Connection, term: str) -> set[str]:
    """不区分大小写的 text/title/author 并集，topics 排除在外。

    这是大小写断言的分母。用 SQLite 的 ``lower()`` + ``instr()`` 而不是 Python 侧
    折叠：口径必须由数据库定义，因为召回也在数据库里算。``lower()`` 只折 ASCII
    （``lower('Ａ')`` 仍是全角 Ａ），而 ``instr`` 是纯子串查找、没有元字符概念——
    正是 GLOB 转义 + ASCII 字符类折叠之后应该等价的语义。
    """
    return {
        row[0]
        for row in connection.execute(
            "SELECT chunk_id FROM chunks WHERE instr(lower(text), lower(?)) > 0 "
            "OR instr(lower(title), lower(?)) > 0 OR instr(lower(author), lower(?)) > 0",
            [term] * 3,
        )
    }


def literal_substring_ids(connection: sqlite3.Connection, term: str) -> set[str]:
    """字面子串口径的 text/title/author 并集，元字符不作通配解释。

    用 ``instr()`` 而不是 ``LIKE``：``LIKE`` 的 ``%``/``_`` 是另一套通配符，拿它当
    "字面"的基准会把 ``_`` 这类输入也算错。``instr`` 是纯子串查找，没有元字符概念，
    正好是 GLOB 转义之后应该等价于的语义。
    """
    return {
        row[0]
        for row in connection.execute(
            "SELECT chunk_id FROM chunks "
            "WHERE instr(text, ?) > 0 OR instr(title, ?) > 0 OR instr(author, ?) > 0",
            [term] * 3,
        )
    }


def sources_of(rows) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["source_id"]] = counts.get(row["source_id"], 0) + 1
    return counts


def fts_hits(connection: sqlite3.Connection, term: str, column: str | None = None) -> int:
    """Phrase MATCH against the FTS table.

    Only meaningful for terms of three characters or more. trigram cannot build a gram
    from two characters, so a two-character MATCH is 0 by construction — which is why
    no acceptance threshold may be expressed through this helper. Use ``recall_ids``
    for that: it measures what the query layer actually returns, whatever path it took.
    """
    target = column or "chunks_fts"
    sql = f'SELECT count(*) FROM chunks_fts WHERE {target} MATCH ?'
    return connection.execute(sql, ['"' + term.replace('"', '""') + '"']).fetchone()[0]


def glob_hits(connection: sqlite3.Connection, term: str, column: str = "text") -> int:
    """Substring match on the FTS table via GLOB, which SPEC 2.2 picks for short terms.

    Measured on the real index: GLOB returns exactly the prose-match count, which is what
    this helper is for. Do not read a performance claim into it — a short GLOB pattern has
    no three consecutive literal characters, so it cannot use the trigram postings and
    degrades to a linear scan. ``EXPLAIN QUERY PLAN`` showing ``INDEX 0:G<n>`` only means
    the virtual table accepted the GLOB constraint. See query_kb.glob_fold_ascii_case()
    for the controlled measurement and SPEC stage 2 for the large-corpus acceptance gate.

    Note this helper takes the term raw (no escaping, no case folding) because callers
    pass known-plain terms; the query layer itself goes through ``glob_pattern``.
    """
    sql = f"SELECT count(*) FROM chunks_fts WHERE {column} GLOB ?"
    return connection.execute(sql, [f"*{term}*"]).fetchone()[0]


def recall_ids(connection: sqlite3.Connection, term: str, limit: int = 5000) -> set[str]:
    """chunk_ids the query layer returns for a term, implementation-agnostic.

    Acceptance thresholds are stated against this rather than against MATCH or GLOB, so
    the same assertion holds whether the fix comes from a tokenizer change, a GLOB
    branch, or something else. A large limit is passed to measure recall, not ranking.
    """
    return {row["chunk_id"] for row in search(connection, term, None, None, limit)}


def prose_ids(connection: sqlite3.Connection, term: str) -> set[str]:
    """chunk_ids whose prose really contains the term."""
    return {
        row[0]
        for row in connection.execute(
            "SELECT chunk_id FROM chunks WHERE text LIKE ?", (f"%{term}%",)
        )
    }


def recall_target_ids(connection: sqlite3.Connection, term: str) -> set[str]:
    """The SPEC 2.2 recall target: text/title/author union, topics excluded.

    This is the denominator for every recall assertion. Prose alone is the wrong one:
    titles are human-written and stage 1 keeps them indexed, so a title-only match is a
    legitimate result, while a topics-only match is auto-generated noise.
    """
    return {
        row[0]
        for row in connection.execute(
            "SELECT chunk_id FROM chunks WHERE text LIKE ? OR title LIKE ? OR author LIKE ?",
            [f"%{term}%"] * 3,
        )
    }


def label_only_ids(connection: sqlite3.Connection, term: str) -> set[str]:
    """chunk_ids reachable through the topics column alone — the noise to exclude."""
    return {
        row[0]
        for row in connection.execute(
            "SELECT chunk_id FROM chunks WHERE topics LIKE ? "
            "AND text NOT LIKE ? AND title NOT LIKE ? AND author NOT LIKE ?",
            [f"%{term}%"] * 4,
        )
    }


class FixtureShapeTests(unittest.TestCase):
    """Guards the fixture itself: if these drift, the other tests stop meaning anything."""

    @classmethod
    def setUpClass(cls):
        cls.connection = build_fixture()

    @classmethod
    def tearDownClass(cls):
        cls.connection.close()

    def test_trigram_tokenizer_is_active(self):
        # create_schema silently degrades to unicode61 when trigram is unavailable,
        # which would change every hit count below.
        value = self.connection.execute("SELECT value FROM metadata WHERE key='fts_tokenizer'").fetchone()[0]
        self.assertEqual(value, "trigram")

    def test_fulibei_occupies_the_lowest_rowids(self):
        # Reproduces build_index.py's directory-name ordering, which is what makes an
        # unordered LIMIT return fulibei and nothing else.
        first = [row[0] for row in self.connection.execute("SELECT source_id FROM chunks LIMIT 50")]
        self.assertEqual(set(first), {"fulibei"})

    def test_term_is_spread_across_all_three_sources(self):
        distribution = dict(
            self.connection.execute(
                "SELECT source_id, count(*) FROM chunks WHERE text LIKE '%竞价%' GROUP BY 1"
            )
        )
        # tulip_garden holds the most on-topic prose, as in production (245 text matches
        # for 竞价 against fulibei's 118). 150 total: 30 tulip_garden chunks carry only
        # SECOND_TERM, so they are absent here.
        self.assertEqual(distribution, {"fulibei": 20, "nanjinglu_bian": 40, "tulip_garden": 90})

    def test_second_term_lives_in_exactly_one_source(self):
        # Makes the mixed-length and multi-term assertions meaningful: if the short term
        # is dropped, these 30 chunks are unreachable and the loss is measurable.
        distribution = dict(
            self.connection.execute(
                "SELECT source_id, count(*) FROM chunks WHERE text LIKE ? GROUP BY 1",
                (f"%{SECOND_TERM}%",),
            )
        )
        self.assertEqual(distribution, {"tulip_garden": 30})

    def test_the_two_terms_never_co_occur(self):
        # The short term and the three-character term must stay disjoint, otherwise the
        # FTS branch alone could reach the short term's chunks.
        overlap = self.connection.execute(
            "SELECT count(*) FROM chunks WHERE text LIKE ? AND text LIKE '%弱转强%'",
            (f"%{SECOND_TERM}%",),
        ).fetchone()[0]
        self.assertEqual(overlap, 0)

    def test_title_and_author_only_matches_exist(self):
        # 15 chunks reachable only through title (10) or author (5). Mirrors the real
        # corpus, where 龙头 has 310 title-only chunks and 情绪 405 (406 是数据清洁前的历史值)。
        title_only = self.connection.execute(
            "SELECT count(*) FROM chunks WHERE title LIKE '%竞价%' AND text NOT LIKE '%竞价%'"
        ).fetchone()[0]
        author_only = self.connection.execute(
            "SELECT count(*) FROM chunks WHERE author LIKE '%竞价%' AND text NOT LIKE '%竞价%'"
        ).fetchone()[0]
        self.assertEqual((title_only, author_only), (10, 5))

    def test_recall_target_is_larger_than_prose_alone(self):
        # The gap is exactly the 15 title/author-only chunks. A text-only recall path
        # would miss all of them, which is why the acceptance threshold is the union.
        prose = prose_ids(self.connection, "竞价")
        target = recall_target_ids(self.connection, "竞价")
        self.assertEqual(len(target) - len(prose), 15)
        self.assertTrue(prose < target)

    def test_label_only_chunks_are_disjoint_from_the_recall_target(self):
        # The topics column labels every chunk, so label-only matches are the complement
        # of the recall target: 475 chunks total, 165 in the target, 310 label-only.
        target = recall_target_ids(self.connection, "竞价")
        labels = label_only_ids(self.connection, "竞价")
        self.assertEqual(target & labels, set())
        total = self.connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
        self.assertEqual(len(target) + len(labels), total)
        # Pinned here rather than inside the expectedFailure cases that quote it: an
        # assertion in one of those is absorbed as "expected", so a fixture built to the
        # wrong shape would go unreported and the defect cases would be measuring nothing.
        self.assertEqual((len(target), len(labels), total), (165, 310, 475))


class IndexPollutionTests(unittest.TestCase):
    """SPEC 缺陷 A（阶段 1 已修）：自动生成的 topics 列曾进入全文索引。

    修复前实测（回归样本 3176 块，2026-08-02，**均为历史值**）：
        情绪周期    FTS 1823, 正文 202  -> 89% 噪声
        龙头与核心  FTS 1358, 正文 0    -> 100% 噪声
        竞价与盘口  FTS 1525, 正文 0    -> 100% 噪声

    正文 202 是清洁前的数，当前是 203（clean_text 删 NUL 后多了一块可见）。这里保留
    旧值是因为它与 FTS 1823 属于同一次测量，是缺陷成因的存档；当前基线看
    test_prose_match_baselines_hold。

    阶段 1 把 topics 从 ``chunks_fts`` 的列定义里移出（build_index.py:create_schema），
    修复后同样三个词的样本命中数为 516 / 0 / 0，精确等于 text/title/author 三字段并集。
    title 与 author 保留在索引里：那是人写的真实文本，标题独有命中是合法结果。

    这些用例从 expectedFailure 转为普通断言，此后是防回归的守卫：任何把 topics 重新
    塞回 FTS 的改动（包括只改 trigram 一条分支、漏改 unicode61 降级分支）都会在这里失败。
    """

    @classmethod
    def setUpClass(cls):
        cls.connection = build_fixture()

    @classmethod
    def tearDownClass(cls):
        cls.connection.close()

    def test_topic_label_never_appears_in_prose(self):
        # Establishes that any hit on the label is noise, not a real match.
        prose = self.connection.execute(
            "SELECT count(*) FROM chunks WHERE text LIKE ?", (f"%{TOPIC_LABEL}%",)
        ).fetchone()[0]
        self.assertEqual(prose, 0)

    def test_topic_label_must_not_match_full_text_search(self):
        # 修复前 475/475 命中（100% 噪声），标签文本在正文里一次都没出现过。
        self.assertEqual(fts_hits(self.connection, TOPIC_LABEL), 0)

    def test_the_fts_table_has_no_topics_column(self):
        # 直接钉住列定义，而不是只看命中数。命中数为 0 也可能是别的原因造成的
        # （比如整张表建错了），查一次 topics MATCH 能把"列还在但恰好没命中"区分出来。
        with self.assertRaises(sqlite3.OperationalError):
            self.connection.execute(
                "SELECT count(*) FROM chunks_fts WHERE topics MATCH ?", ["x"]
            )
        sql = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='chunks_fts'"
        ).fetchone()[0]
        self.assertNotIn("topics", sql)
        # 另外三列必须还在——把 topics 连着 title/author 一起删掉不是修复，是砍功能。
        for column in ("title", "author", "text"):
            with self.subTest(column=column):
                self.assertIn(column, sql)

    def test_topics_stays_readable_on_the_chunks_table(self):
        # 移出 FTS 不等于删数据：topics 仍要能被读出来，元数据展示（query_kb.py 的
        # "主题:" 一行）和 relevance() 都依赖它。
        value = self.connection.execute(
            "SELECT topics FROM chunks WHERE topics != '' LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(value)
        self.assertEqual(value[0], TOPIC_LABEL)

    def test_hits_converge_on_the_recall_target(self):
        # 收敛口径是 text/title/author 三字段并集，不是正文单列。正文口径与本节末句
        # "title 保留"直接冲突：真库里 情绪周期 有 313 块只在标题含词，要求收敛到正文
        # 203 就等于要求把标题列也移出 FTS。实测去掉 topics 后，样本命中 516 = 并集 516。
        # （清洁前是 314 / 202，那组是历史值；并集 516 两次都一样，只是归属换了一栏。）
        #
        # fixture 里标签词的三字段并集为 0（标签文本不出现在任何人写字段里），所以这里
        # 同时验证了标签命中归零；TITLE_WITH_TERM 反向验证标题独有命中仍被算进来。
        #
        # 三个词都至少三字：两字词的 MATCH 恒为 0（trigram 建不出 gram），拿它比并集
        # 测的不是阶段 1 的收敛，而是阶段 2 才修的缺陷 B。
        for term in (TOPIC_LABEL, "弱转强", TITLE_WITH_TERM):
            with self.subTest(term=term):
                self.assertEqual(
                    fts_hits(self.connection, term),
                    len(recall_target_ids(self.connection, term)),
                )

    def test_prose_term_stays_accurate(self):
        # A three-character term already works and must not regress after the fix.
        self.assertEqual(fts_hits(self.connection, "弱转强", column="text"), 150)

    def test_title_column_stays_searchable(self):
        # Titles are human-written text, unlike topics, so they keep their index. On the
        # frozen sources, title-only matches are a large share of legitimate results:
        # 310 chunks for 龙头, 405 for 情绪, 58 for 竞价. Dropping the column would lose them.
        # 465 = 475 total minus the 10 chunks retitled to TITLE_WITH_TERM.
        self.assertEqual(fts_hits(self.connection, "示例文档", column="title"), 465)
        self.assertEqual(fts_hits(self.connection, TITLE_WITH_TERM, column="title"), 10)


class ShortTermSearchTests(unittest.TestCase):
    """SPEC 缺陷 B（阶段 2 已修）：短于三字的检索词曾被整个丢弃。

    Measured on the real database (2026-08-02): 竞价 394 prose matches / 0 FTS hits,
    筹码 367/0, 龙头 616/0, 打板 186/0, 情绪 889/0. Most trading vocabulary is
    two characters, so this is the highest-traffic entry point into the corpus.
    （龙头 615、情绪 888 是数据清洁前的历史值。FTS 侧的 0 与清洁无关，永远是 0。）

    MATCH 命中仍然是 0，而且永远是 0：trigram 建不出两字 gram，阶段 2 也没换
    tokenizer。变的是 query_kb.py 不再按长度丢词——两字词改在 chunks_fts 的
    text/title/author 三列上各自 GLOB '*词*' 后取并集。所以这个类里的断言全部挂在
    search() 的返回上，不挂在 MATCH 计数上：换成别的修法验收标准也不变。
    """

    @classmethod
    def setUpClass(cls):
        cls.connection = build_fixture()

    @classmethod
    def tearDownClass(cls):
        cls.connection.close()

    def search(self, query: str, limit: int = 8):
        return search(self.connection, query, None, None, limit)

    def test_two_character_term_finds_nothing_in_fts(self):
        # 根因，保留为通过的事实：trigram 要三个字符才能建一个 gram，所以两字词的
        # MATCH 恒为 0，且只要不换 tokenizer 就一直是 0。阶段 2 保留 trigram，改走
        # GLOB 子串路径，所以这条不是"必须可搜到"的验收线——那种断言永远不会通过。
        self.assertEqual(fts_hits(self.connection, "竞价"), 0)

    def test_two_character_term_returns_results(self):
        # 阶段 2 前这条叫 ..._via_fallback：结果确实回来了，但来自 LIKE 兜底，
        # 掩盖了来源偏斜。兜底已删除，结果现在来自 GLOB 召回路径。
        rows = self.search("竞价")
        self.assertEqual(len(rows), 8)

    def test_two_character_term_does_not_come_from_a_fallback(self):
        # 阶段 2 摘除 expectedFailure。rank 999.0 曾是 LIKE 兜底的固定分，用它识别
        # 结果走了哪条路；兜底连同它按 rowid 截断的候选池一起删掉，这个值不再出现。
        rows = self.search("竞价")
        self.assertTrue(all(row["rank"] != 999.0 for row in rows))

    def test_two_character_term_reaches_full_recall(self):
        # 阶段 2 摘除 expectedFailure。修复前：MATCH 命中 0，全靠 LIKE 兜底把 475 条
        # 捞回来（rank 恒 999.0），其中 310 条只挂了 topics 标签。
        # 修复后：召回精确等于 text/title/author 三字段并集的 165 条。
        # 等值断言同时卡住上下界——多一条说明 topics 漏进召回，少一条说明人写字段被砍。
        expected = recall_target_ids(self.connection, "竞价")
        rows = search(self.connection, "竞价", None, None, 500)
        got = {row["chunk_id"] for row in rows}
        self.assertEqual(got, expected)
        self.assertTrue(all(row["rank"] != 999.0 for row in rows))

    def test_title_and_author_only_matches_are_recalled(self):
        # 阶段 2 摘除 expectedFailure。这 15 条只在标题或作者里含词，正文不含，
        # 所以只对 text 做 GLOB 的实现会全部丢掉——契约要求三列各自 GLOB 再取并集。
        # 真库同类块：龙头 310 条、情绪 405 条、竞价 58 条，全部来自 title。
        # 这 15 条的数量由 test_recall_target_is_larger_than_prose_alone 那个测试守着。
        rows = search(self.connection, "竞价", None, None, 500)
        got = {row["chunk_id"] for row in rows}
        title_or_author_only = recall_target_ids(self.connection, "竞价") - prose_ids(
            self.connection, "竞价"
        )
        self.assertTrue(title_or_author_only <= got)
        self.assertTrue(all(row["rank"] != 999.0 for row in rows))

    def test_three_character_term_uses_fts(self):
        # The working baseline: must not regress when the two-character path lands.
        rows = self.search("弱转强")
        self.assertTrue(all(row["rank"] != 999.0 for row in rows))

    def test_mixed_query_keeps_both_lengths(self):
        # 阶段 2 前这条叫 ..._currently_ignores_the_short_term：'竞价 弱转强' 会被静默
        # 缩成 '弱转强'。现在两个词各自召回后取并集，所以结果必须真的变大——
        # 短词的贡献由 RetrievalContractTests 逐条核对，这里只钉住"不是兜底给的"。
        rows = self.search("竞价 弱转强")
        self.assertTrue(all(row["rank"] != 999.0 for row in rows))
        both = search(self.connection, "竞价 弱转强", None, None, 500)
        long_only = search(self.connection, "弱转强", None, None, 500)
        self.assertGreater(len(both), len(long_only))


class SourceCoverageTests(unittest.TestCase):
    """SPEC 缺陷 C（阶段 2 修召回、阶段 3 修排序）：候选池曾按 rowid 截断且评分方向是反的。

    阶段 2 删掉了 candidate_limit，候选集先取全量再截断，所以下面这张表记的是修复前的
    候选池构成，作为缺陷成因的存档。阶段 3 修的是排序部分（正文命中要排在仅标题/仅作者
    命中之前），本文件最后一个 expectedFailure 就在这个类里，已随修复摘除。

    Measured read-only on 2026-08-02 over the frozen regression sample, as
    fulibei/nanjinglu_bian/tulip_garden. Candidate pool = max(limit*30, 120) = 240:

        term  pool composition   recall target (text/title/author)   four-field OR
        竞价  fulibei 240/0/0    118/31/303                          343/132/1140
        龙头  fulibei 240/0/0    691/161/73                          1008/412/232
        打板  fulibei 240/0/0    150/9/27                            540/319/538
        情绪  fulibei 240/0/0    539/221/534                         884/475/767

    An earlier revision of this docstring listed the four-field OR column as the "real"
    distribution. It is not a distribution of legitimate results: the topics column is
    machine-assigned and its label text never appears in the prose, so most of that count
    is noise. The recall target per SPEC 2.2 is the middle column.

    修复前每个高频词都只返回 fulibei。那比零结果更糟：答案看起来是合理的，而其他来源
    被静默排除了。修复后召回等于三字段并集，来源覆盖成为等值断言的推论。

    What is asserted here is the *pool*, not the shape of the top 8. SPEC 阶段 3 sets no
    per-source quota on results, so requiring "the 8 rows span >= 2 sources" would push
    the implementation toward one. Source coverage is verified at the recall layer with a
    large limit, against sources.yaml rather than against this sample — see
    RegistryScopedIndexTests.
    """

    @classmethod
    def setUpClass(cls):
        cls.connection = build_fixture()

    @classmethod
    def tearDownClass(cls):
        cls.connection.close()

    def search(self, query: str, limit: int = 8):
        return search(self.connection, query, None, None, limit)

    def test_candidate_pool_is_truncated_by_insertion_order(self):
        # 复刻旧实现那条 SQL（LIMIT 无 ORDER BY），作为缺陷成因的存档。
        # 查的是 chunks 表本身，不经过 search()，所以阶段 2 删掉候选池截断后这条依然
        # 成立——它证明的是"当年那种写法必然偏斜"，不是"今天的实现还偏斜"。
        pool = self.connection.execute(
            "SELECT source_id FROM chunks "
            "WHERE (text LIKE ? OR title LIKE ? OR author LIKE ? OR topics LIKE ?) LIMIT ?",
            [*["%竞价%"] * 4, 240],
        ).fetchall()
        self.assertEqual(sources_of(pool), {"fulibei": 240})

    def test_default_limit_is_not_taken_from_the_truncated_pool(self):
        # 阶段 3 改写。阶段 2 这条叫 ..._no_longer_collapses_to_the_first_source，断言
        # ``set(sources_of(rows)) != {"fulibei"}``；再往前叫 ..._currently_returns_one_
        # source_only，钉的是 {"fulibei"}——候选池按 rowid 截满 240 条 fulibei，其余
        # 来源一条进不来。
        #
        # **那条断言当年成立是个巧合，而且巧合的来源正是阶段 3 要修的缺陷。** 阶段 2 的
        # relevance() 给 author 5.0、title 3.0、正文 1.0，于是前 8 条是 5 条 author-only
        # （9.0 分）+ 3 条 title-only（7.0 分），正文块只有 6.0 分排不上去。这 8 条恰好
        # 分布在 tulip_garden 和 nanjinglu_bian 上，所以"不是 fulibei 独占"通过了——
        # 靠的是"标题命中压过正文命中"，也就是同一个类里那条阶段 3 expectedFailure 记录
        # 的缺陷。权重修正之后正文块升到最高档，而 fixture 里 chunk_id 最小的正文块是
        # fulibei-0281..0288，前 8 条重新变成 fulibei 独占，这条断言就翻红了。
        #
        # 而且它本身就是一条**隐性来源配额**：SPEC 阶段 3 明确"小 limit 的结果里不检查
        # 来源数量"，并记录了同类断言 test_two_character_query_must_span_sources 被删除的
        # 原因。"不许是某一个来源独占"和"至少跨 2 个来源"是同一件事的两种说法。
        #
        # 改成钉真正区分修好没修好的性质：前 8 条**一条都不在旧候选池里**。旧池是
        # rowid 最小的 240 条（fulibei 的 1..240，全是只挂标签的块，正文真含 0 条），
        # 旧实现只能从这 240 条里挑，所以交集为空就证明截断确实没了，而这与结果落在
        # 哪个来源无关。第二条断言保留：前 8 条条条在三字段并集内，没拿标签噪声来填。
        rows = self.search("竞价")
        old_pool = {
            row[0]
            for row in self.connection.execute(
                "SELECT chunk_id FROM chunks "
                "WHERE (text LIKE ? OR title LIKE ? OR author LIKE ? OR topics LIKE ?) LIMIT ?",
                [*["%竞价%"] * 4, 240],
            )
        }
        self.assertEqual(len(old_pool), 240, "旧候选池形状变了，这条断言的分母就没了")
        self.assertFalse({row["chunk_id"] for row in rows} & old_pool)
        self.assertTrue(
            {row["chunk_id"] for row in rows} <= recall_target_ids(self.connection, "竞价")
        )

    def fixture_holders(self) -> dict[str, int]:
        """Which fixture sources hold a three-field match for 竞价, and how many each."""
        return dict(
            self.connection.execute(
                "SELECT source_id, count(*) FROM chunks "
                "WHERE text LIKE ? OR title LIKE ? OR author LIKE ? GROUP BY 1",
                ["%竞价%"] * 3,
            )
        )

    def test_the_fixture_spreads_the_term_across_three_sources(self):
        # Was an assertion inside the expectedFailure below, where a broken fixture would
        # have been silently absorbed as "expected failure" — the guard has to be a plain
        # test to be able to report anything.
        self.assertEqual(len(self.fixture_holders()), 3)

    def test_recall_layer_must_reach_every_source_that_holds_matches(self):
        # 阶段 2 摘除 expectedFailure。修复前：兜底候选池按 rowid 截断，tulip_garden 的
        # 95 条命中一条都进不来。修复后：大 limit 下每个真的持有命中的来源都出现在召回
        # 里，且召回等于三字段并集。不涉及排名，也不要求任何来源在前 8 条里出现。
        # fixture 形状的守卫在上面的普通测试里，不放这里。
        expected = self.fixture_holders()
        rows = self.search("竞价", limit=500)
        self.assertTrue(all(row["rank"] != 999.0 for row in rows))
        self.assertEqual({row["chunk_id"] for row in rows}, recall_target_ids(self.connection, "竞价"))
        self.assertEqual(set(sources_of(rows)), set(expected))

    def test_prose_matches_outrank_title_and_author_only_matches(self):
        # 阶段 3 摘除 expectedFailure（本轮最后一个），并按修复后的事实改名。
        #
        # **原名里的"label_only"在阶段 2 之后已经不准确了。** 原注释记的是：relevance()
        # 给 topics 4.0、正文 1.0，前 8 条全是只挂标签的块，正文真含 0/8。阶段 2 把
        # topics 移出召回之后，纯标签块根本进不了候选集，前 8 条的实际构成变成
        # 5 条 author-only（9.0 分）+ 3 条 title-only（7.0 分）——正文块 6.0 分仍然排不
        # 上去，正文真含仍是 0/8，但压过它的已经不是 topics 4.0，而是 author 5.0 和
        # title 3.0。名字随实际缺陷形态改成 title_and_author_only。
        #
        # 修法是 ranking_key() 的第一层：正文命中（prose_hit）先于一切。不是靠把 text
        # 权重调到比 title 高——权重可以被数量抵消，一个标题里重复三次该词的块照样能
        # 盖过只提一次的正文块，而"正文有没有讲这个词"是定性区别，不该被计数抵消。
        #
        # topics 那一侧同时钉住：权重降到 0.0，且纯标签块本来就在召回之外，所以断言
        # 顺带验证前 8 条与纯标签集无交集——两个方向一起守，避免将来把 topics 塞回召回
        # 时这条测试还是绿的。
        rows = self.search("竞价")
        self.assertEqual(len(rows), 8)
        self.assertTrue(all("竞价" in row["text"] for row in rows))
        self.assertFalse({row["chunk_id"] for row in rows} & label_only_ids(self.connection, "竞价"))

    def test_a_prose_match_outranks_a_title_match_even_with_fewer_hits(self):
        # 上一条的加强版，也是"为什么第一层是布尔而不是权重"的直接证据。
        #
        # fixture 里 title-only 块的标题是 TITLE_WITH_TERM（『竞价专题整理』，含词 1 次），
        # 正文块含词 2 次。把 text 权重调成 3.0、title 调成 2.0 就能让 6.0 > 2.0，
        # 这条测试照样过——所以它单独构造一个反例：标题里重复该词、正文只提一次的块，
        # 在任何"text 权重 > title 权重"的加权求和实现里都会翻红，只有布尔分层才拦得住。
        #
        # 用独立的临时连接，不碰类级 fixture 那批钉死的绝对数字（165/310/475）。
        connection = build_fixture()
        try:
            add_chunk(
                connection,
                chunk(
                    "tulip_garden",
                    900,
                    without_term(900),  # 正文不含『竞价』
                    title="竞价竞价竞价竞价专题",  # 标题含 4 次
                ),
            )
            add_chunk(
                connection,
                chunk("tulip_garden", 901, "第901节只提到一次竞价。", title="普通标题"),
            )
            connection.commit()
            order = [row["chunk_id"] for row in search(connection, "竞价", None, None, 500)]
            self.assertLess(
                order.index("tulip_garden-0901"),
                order.index("tulip_garden-0900"),
                "正文命中必须排在纯标题命中之前，哪怕标题命中次数多得多",
            )
        finally:
            connection.close()

    def test_more_matched_terms_outrank_fewer(self):
        # SPEC 2.2 评分层第三条：多词命中数越多排越前。
        #
        # fixture 里 竞价 与 SECOND_TERM 的正文互不重叠（FixtureShapeTests 的
        # test_the_two_terms_never_co_occur 守着），所以造一块同时含两个词的，它必须排在
        # 任何只含单词的块之前——哪怕那些单词块的词频高得多。
        #
        # 这一层同样不能用加权求和替代：单词块把 竞价 刷到 8 次（上限）就能拿满 24.0 分，
        # 而双词块两个词各 1 次只有 6.0 分。所以多词命中必须是独立的一层，排在字段
        # 加权分之前。
        connection = build_fixture()
        try:
            add_chunk(
                connection,
                chunk("tulip_garden", 910, f"第910节同时讲竞价和{SECOND_TERM}，各提一次。"),
            )
            add_chunk(
                connection,
                chunk("tulip_garden", 911, "竞价" * 20 + "，第911节只讲这一个词。"),
            )
            connection.commit()
            order = [
                row["chunk_id"]
                for row in search(connection, f"竞价 {SECOND_TERM}", None, None, 500)
            ]
            self.assertLess(
                order.index("tulip_garden-0910"),
                order.index("tulip_garden-0911"),
                "两个词都命中的块必须排在只命中一个词的高频块之前",
            )
        finally:
            connection.close()

    def test_chunk_id_breaks_ties_so_order_does_not_depend_on_rowid(self):
        # SPEC 2.2 确定性层：排序末尾加 chunk_id 作为决胜字段。
        #
        # fixture 里 150 个正文块的前三层键完全相同（都是 (-1, -1, -6.0)），此前靠 SQLite
        # 返回的 rowid 顺序兜着。rowid 在同一个库里稳定，但重建索引后按目录名和文件顺序
        # 重新分配，所以"两次查询结果相同"在跨重建时并不成立。
        #
        # 验法是把同一批块用**相反的插入顺序**建一个库：rowid 全反了，chunk_id 没变。
        # 有 chunk_id 决胜则两个库的结果逐条相等；没有则完全颠倒。
        forward = build_fixture()
        try:
            tied = [
                row["chunk_id"]
                for row in search(forward, "竞价", None, None, 500)
            ]
        finally:
            forward.close()

        reversed_connection = sqlite3.connect(":memory:")
        reversed_connection.row_factory = sqlite3.Row
        create_schema(reversed_connection)
        try:
            source = build_fixture()
            try:
                items = source.execute("SELECT * FROM chunks ORDER BY rowid DESC").fetchall()
            finally:
                source.close()
            for item in items:
                add_chunk(reversed_connection, dict(item))
            reversed_connection.commit()
            reversed_order = [
                row["chunk_id"]
                for row in search(reversed_connection, "竞价", None, None, 500)
            ]
        finally:
            reversed_connection.close()

        self.assertEqual(tied, reversed_order, "并列顺序不得依赖 rowid（即插入顺序）")
        # 空转保护：这批结果里真的存在前三层并列的块，否则上面那条比较测不到决胜层。
        self.assertGreater(len(tied), 100)

    def test_fts_path_ranks_across_the_whole_table(self):
        # The FTS branch scores every matching row before truncating, so a large limit
        # returns exactly the prose matches and nothing else. This is the behaviour the
        # fallback must match; asserting it here catches a regression in either path.
        rows = search(self.connection, "弱转强", None, None, 500)
        self.assertEqual(sources_of(rows), {"fulibei": 20, "nanjinglu_bian": 40, "tulip_garden": 90})

    def test_recall_does_not_leak_label_only_matches_at_large_limits(self):
        # 阶段 2 摘除 expectedFailure（原名 test_fallback_must_not_leak_...，兜底已删除，
        # 名字里的 fallback 随之去掉）。修复前：limit=500 时 LIKE 兜底返回全部 475 块，
        # 其中 310 块只挂了 topics 标签，正文、标题、作者里都没有『竞价』。
        # 修复后：只返回三字段并集的 165 块。
        # 165/310/475 这三个数由 test_label_only_chunks_are_disjoint_from_the_recall_target
        # 那个测试守着，这里不重复写死。
        rows = search(self.connection, "竞价", None, None, 500)
        self.assertEqual(len(rows), len(recall_target_ids(self.connection, "竞价")))
        self.assertFalse({row["chunk_id"] for row in rows} & label_only_ids(self.connection, "竞价"))


class RetrievalContractTests(unittest.TestCase):
    """SPEC 2.2 检索契约：召回、去重、评分、确定性四层，各自独立可验。

    Split out from the defect classes on purpose. The defect tests say "today's
    behaviour is wrong"; these say "whatever the fix is, these properties must hold".
    A rewrite of the recall branch that broke deduplication or made ordering
    non-deterministic would pass every defect test and still be unusable.
    """

    @classmethod
    def setUpClass(cls):
        cls.connection = build_fixture()

    @classmethod
    def tearDownClass(cls):
        cls.connection.close()

    def search(self, query: str, limit: int = 8, source=None, author=None):
        return search(self.connection, query, source, author, limit)

    # --- 去重 ---

    def test_results_never_repeat_a_chunk(self):
        # The FTS branch joins chunks_fts to chunks; a bad join condition would
        # duplicate rows and quietly halve the useful result count.
        for query in ("竞价", "弱转强", "竞价 弱转强"):
            with self.subTest(query=query):
                ids = [row["chunk_id"] for row in self.search(query, limit=200)]
                self.assertEqual(len(ids), len(set(ids)))

    # --- 确定性 ---

    def test_repeated_identical_queries_return_identical_results(self):
        # SPEC 2.2: same query twice, same rows in the same order. Ties in bm25 or in
        # relevance() currently fall back to rowid, which is stable within one database
        # but not guaranteed across a rebuild — hence the chunk_id tiebreaker in 阶段 3.
        for query in ("竞价", "弱转强", "情绪"):
            with self.subTest(query=query):
                first = [row["chunk_id"] for row in self.search(query)]
                second = [row["chunk_id"] for row in self.search(query)]
                self.assertEqual(first, second)

    def test_limit_prefix_is_stable_as_limit_grows(self):
        # A larger limit must extend the result list, not reshuffle it. Without this,
        # limit=8 and limit=20 can disagree about the top 8 and both look plausible.
        #
        # 阶段 3 把词表从单个 弱转强 扩到三条路径各一个：长词走 MATCH、短词走 GLOB、
        # 混合查询两条都走。阶段 2 时两条路径用两把不同的尺子，这条只测了 MATCH 那把；
        # 统一到 ranking_key() 之后三种查询的前缀稳定性都由同一个实现保证，一起钉住。
        for query in ("弱转强", "竞价", f"竞价 {SECOND_TERM}", "竞价 弱转强"):
            with self.subTest(query=query):
                short = [row["chunk_id"] for row in self.search(query, limit=8)]
                long = [row["chunk_id"] for row in self.search(query, limit=40)]
                self.assertEqual(len(short), 8)
                self.assertEqual(long[:8], short)

    def test_ranking_is_independent_of_the_candidate_arrival_order(self):
        # 确定性的实现层断言：ranking_key() 排序的结果不依赖输入列表的顺序。
        #
        # 上面那两条测的是 search() 的可观察行为，两次调用之间输入顺序恰好相同，所以
        # 即便实现里残留一个"靠 sorted 稳定性兜住并列"的依赖也测不出来。这里直接把同一
        # 批候选行打乱再排，要求结果逐条相同——决胜层缺失时这条必然翻红。
        rows = search(self.connection, "竞价", None, None, 500)
        self.assertGreater(len(rows), 100)
        ordered = [row["chunk_id"] for row in rank_and_truncate(list(rows), ["竞价"], 500)]
        shuffled = list(rows)
        random.Random(20260803).shuffle(shuffled)
        self.assertNotEqual(
            [row["chunk_id"] for row in shuffled], ordered, "打乱之后与有序输入相同，这条测不到东西"
        )
        self.assertEqual(
            [row["chunk_id"] for row in rank_and_truncate(shuffled, ["竞价"], 500)], ordered
        )

    def test_topics_weight_does_not_exceed_the_prose_weight(self):
        # SPEC 3.1 阶段 3 验收表第二行：topics 权重 ≤ 正文权重。阶段 2 的实测值是
        # topics 4.0 对正文 1.0，方向是反的。
        #
        # 直接钉常量而不只是钉行为：行为断言（前 8 条正文真含）在"topics 权重仍然很高但
        # 恰好没有块能靠它上榜"的数据上会假通过，而权重方向是契约本身。
        self.assertLessEqual(FIELD_WEIGHTS["topics"], FIELD_WEIGHTS["text"])
        self.assertLessEqual(FIELD_WEIGHTS["title"], FIELD_WEIGHTS["text"])
        self.assertLess(FIELD_WEIGHTS["topics"], FIELD_WEIGHTS["title"])
        # SPEC 2.2 的字段权重契约是 text ≥ title > topics。author 不在那三者之列，
        # 归到 title 这一档（同为人写的短字段），所以它也不得超过 text。
        self.assertLessEqual(FIELD_WEIGHTS["author"], FIELD_WEIGHTS["text"])

    def test_a_topics_only_chunk_cannot_be_promoted_by_its_label(self):
        # 权重常量的行为侧配对断言。topics 权重为 0 时，往一个块的 topics 里反复塞该词
        # 不能改变它的位次——阶段 2 的 4.0 权重下，塞三次就是 12.0 分，足以越过一批
        # 正文块。
        connection = build_fixture()
        try:
            baseline = [
                row["chunk_id"] for row in search(connection, "竞价", None, None, 500)
            ]
            # 挑一个排在末尾附近的块（tulip_garden-0304 是 author-only 那批的最后一条），
            # 这样"靠标签往前挤"一旦发生，位次变化必然可见。
            target = baseline[-1]
            connection.execute(
                "UPDATE chunks SET topics = ? WHERE chunk_id = ?",
                ("竞价 竞价 竞价 竞价与盘口", target),
            )
            connection.commit()
            after = [row["chunk_id"] for row in search(connection, "竞价", None, None, 500)]
            self.assertEqual(baseline, after)
            self.assertEqual(after[-1], target, "被塞标签的块仍应留在原位，标签不得抬升位次")
        finally:
            connection.close()

    def test_duplicate_terms_do_not_change_results(self):
        """查询词按首次出现顺序去重。

        "竞价 情绪" 和 "竞价 竞价 情绪" 的结果及顺序必须完全相同。
        matched_term_count 只能计算不同检索词。
        """
        results_once = [
            row["chunk_id"] for row in self.search("竞价 情绪", limit=20)
        ]
        results_dup = [
            row["chunk_id"]
            for row in self.search("竞价 竞价 情绪", limit=20)
        ]
        self.assertEqual(
            results_once,
            results_dup,
            "重复检索词不得改变召回和排序",
        )

    def test_case_variants_of_one_term_count_as_one_term(self):
        """去重要按**检索等价关系**，不能按字面。

        ``AI``/``ai``/``Ai``/``aI`` 走同一条短词路径、ASCII 折叠后同为 ``ai``，召回的块
        集合逐条相同。按字面去重会把它们当四个词，于是 matched_term_count 被重复计数
        撑大——同时含 ``AI`` 和 ``情绪`` 的块拿 5 而不是 2，只含 ``AI`` 的块拿 4 而不是
        1，第 2 层排序键失真，顺序与 ``AI 情绪`` 不同。用户看到的是"多打几遍大小写变体，
        结果就换了一批顺序"。

        用 fixture 里真实存在的两字词 ``竞价`` 配一个 ASCII 词，避免这条测试依赖
        大小写 fixture 的块号。``竞价`` 本身无大小写，所以变体只加在 ASCII 词上。
        """
        connection = build_fixture()
        try:
            # 往 fixture 里塞两块含 ASCII 词的：一块正文含 AI + 竞价，一块只含 AI。
            add_chunk(
                connection,
                chunk("tulip_garden", 940, "这块正文讲 AI 硬件方向的竞价承接。"),
            )
            add_chunk(
                connection,
                chunk("tulip_garden", 941, "这块正文只讲 AI 硬件，不提那个两字词。"),
            )
            connection.commit()

            baseline = [
                row["chunk_id"] for row in search(connection, "AI 竞价", None, None, 30)
            ]
            # 四种写法混打，中间还夹一次重复的 竞价——结果必须与上面逐条相同。
            for query in (
                "AI ai Ai aI 竞价",
                "AI 竞价 ai Ai aI",
                "ai AI 竞价 竞价",
            ):
                with self.subTest(query=query):
                    variants = [
                        row["chunk_id"]
                        for row in search(connection, query, None, None, 30)
                    ]
                    self.assertEqual(
                        variants,
                        baseline,
                        "大小写变体属于同一个检索词，不得改变召回或顺序",
                    )

            # 词表层面直接钉住：四种写法折成一个，且保留首次出现的原始写法。
            self.assertEqual(terms_from_query("AI ai Ai aI 竞价"), ["AI", "竞价"])
            self.assertEqual(
                terms_from_query("ai AI 竞价"),
                ["ai", "竞价"],
                "去重键按折叠形式，但返回的必须是用户首次写下的形式",
            )
            # 排序键层面钉住：词表经 terms_from_query() 去重后，940 含两个词、941 含一个。
            # 关键是 prepared 必须由**去重后的词表**构造——这正是 search() 内部的走法。
            # 若跳过去重直接喂五个变体，940 会拿到 5、941 拿到 4，第 2 层排序键就失真了。
            prepared = folded_terms(tuple(terms_from_query("AI ai Ai aI 竞价")))
            self.assertEqual(len(prepared), 2, "去重后只剩两个检索词")
            rows = {
                row["chunk_id"]: row
                for row in search(connection, "AI 竞价", None, None, 500)
            }
            self.assertEqual(matched_term_count(FieldFolds(rows["tulip_garden-0940"]), prepared), 2)
            self.assertEqual(matched_term_count(FieldFolds(rows["tulip_garden-0941"]), prepared), 1)
            # 反向钉住"为什么必须去重"：不去重就是失真的那个数。
            unduped = folded_terms(("AI", "ai", "Ai", "aI", "竞价"))
            self.assertEqual(
                matched_term_count(FieldFolds(rows["tulip_garden-0941"]), unduped),
                4,
                "这条记录的是未去重时的失真值，用来证明上面那条不是恒真",
            )
        finally:
            connection.close()

    def test_short_term_scoring_folds_ascii_only(self):
        """**短词**的评分口径必须与它的召回路径（GLOB）一致：只折 ASCII。

        两字词走三列 ``GLOB '*[Aa][Ii]*'``，而 GLOB 对非 ASCII 一律精确匹配——实测
        ``GLOB '*ΑΑ*'`` 不命中只含 ``αα`` 的块，``SELECT lower('ΑΑ')='αα'`` 也是 0。
        所以评分侧若用 ``str.lower()``，就会给一个根本没被召回的写法记上命中。

        **这条测试此前是空转的。** 原版两个块都不带 ``title``，正文一个含 ``αα``、
        一个含 ``AI``，于是查 ``AI ΑΑ`` 时 920 块两个词都不命中、压根进不了候选集，
        末尾那句排序断言被包在 ``if 920 in ids and 921 in ids`` 里，从来没执行过。
        重写的关键是**让两个块都通过标题里的 AI 进入候选集**，这样两块必然都在结果里，
        断言可以无条件写。
        """
        connection = build_fixture()
        try:
            # 两块的标题都含 AI，所以两块都必定通过短词 AI 的 title 列 GLOB 进入候选集。
            # 差别只在正文：920 只有小写希腊 αα，921 有 ASCII 的 AI。
            add_chunk(
                connection,
                chunk(
                    "tulip_garden",
                    920,
                    "这块正文里只有小写希腊字母 αα 没有大写，也没有那个英文词。",
                    title="AI 方向专题",
                ),
            )
            add_chunk(
                connection,
                chunk(
                    "tulip_garden",
                    921,
                    "这块正文里有 AI 关键词。",
                    title="AI 方向专题",
                ),
            )
            connection.commit()

            # 单查大写希腊 ΑΑ：不得召回仅含小写 αα 的块。
            greek_ids = {
                row["chunk_id"] for row in search(connection, "ΑΑ", None, None, 50)
            }
            self.assertNotIn(
                "tulip_garden-0920",
                greek_ids,
                "短词 ΑΑ 走 GLOB，不折希腊大小写，不得召回仅含 αα 的块",
            )

            rows_mixed = search(connection, "AI ΑΑ", None, None, 500)
            mixed_ids = [row["chunk_id"] for row in rows_mixed]
            # 无条件断言：两块都靠标题里的 AI 进了候选集，缺任何一块都是召回坏了。
            self.assertIn("tulip_garden-0920", mixed_ids, "920 应通过标题里的 AI 进入候选")
            self.assertIn("tulip_garden-0921", mixed_ids, "921 应通过标题里的 AI 进入候选")

            by_id = {row["chunk_id"]: row for row in rows_mixed}
            prepared = folded_terms(tuple(terms_from_query("AI ΑΑ")))
            self.assertEqual(len(prepared), 2, "AI 与 ΑΑ 是两个不同的检索词")
            folds_920 = FieldFolds(by_id["tulip_garden-0920"])
            folds_921 = FieldFolds(by_id["tulip_garden-0921"])

            # 920 的正文只有小写 αα，不得因此拿到大写 ΑΑ 的 prose_hit。
            self.assertEqual(
                prose_hit(folds_920, prepared),
                0,
                "正文只含小写 αα，不得获得大写 ΑΑ 的 prose_hit",
            )
            # 也不得成为第二个 matched term——它只命中 AI（标题），共 1 个。
            self.assertEqual(
                matched_term_count(folds_920, prepared),
                1,
                "小写 αα 不得算作大写 ΑΑ 的命中，920 只命中标题里的 AI",
            )
            # 921 正文真含 AI，拿到 prose_hit=1。
            self.assertEqual(prose_hit(folds_921, prepared), 1)
            self.assertEqual(matched_term_count(folds_921, prepared), 1)
            # 第 1 层（正文命中）决出顺序：921 在前。
            self.assertLess(
                mixed_ids.index("tulip_garden-0921"),
                mixed_ids.index("tulip_garden-0920"),
                "正文真含 AI 的 921 必须排在仅标题命中的 920 之前",
            )
        finally:
            connection.close()

    def test_long_term_scoring_folds_like_fts_match(self):
        """**长词**的评分口径必须与它的召回路径（MATCH）一致：按 Unicode 折叠。

        这是上一条的镜像，两条合起来才说明"口径按词长分流"不是随口一说。三字及以上的词
        走 ``MATCH``，而 trigram 按 Unicode 折叠——实测 ``MATCH '"ΑΑΑ"'`` **确实**同时
        命中含 ``ΑΑΑ`` 和含 ``ααα`` 的块。

        召回既然把 ``ααα`` 块放进来了，评分就必须承认那是正文命中。若长词也只折 ASCII，
        该块的 prose_hit 会是 0，被判成"仅标题命中"、压到真·仅标题块后面——召回宽、
        评分窄，同一次查询里两套口径，结果比两边都窄更难解释。
        """
        connection = build_fixture()
        try:
            # 950：正文含小写 ααα，标题不含。
            add_chunk(
                connection,
                chunk("tulip_garden", 950, "这块正文里有小写希腊 ααα 三连，标题不含。"),
            )
            # 951：正文不含，只有标题含大写 ΑΑΑ。
            add_chunk(
                connection,
                chunk(
                    "tulip_garden",
                    951,
                    "这块正文讲的是别的事情，完全不含那个三连字母。",
                    title="ΑΑΑ 专题整理",
                ),
            )
            connection.commit()

            rows = search(connection, "ΑΑΑ", None, None, 500)
            ids = [row["chunk_id"] for row in rows]
            # 无条件断言：MATCH 按 Unicode 折叠，两块都必须被召回。
            self.assertIn("tulip_garden-0950", ids, "MATCH 折叠 Unicode，应召回正文含 ααα 的块")
            self.assertIn("tulip_garden-0951", ids, "标题含大写 ΑΑΑ 的块同样应被召回")

            by_id = {row["chunk_id"]: row for row in rows}
            prepared = folded_terms(tuple(terms_from_query("ΑΑΑ")))
            folds_950 = FieldFolds(by_id["tulip_garden-0950"])
            folds_951 = FieldFolds(by_id["tulip_garden-0951"])

            self.assertEqual(
                prose_hit(folds_950, prepared),
                1,
                "MATCH 召回了它，评分就必须承认正文含 ααα 是命中",
            )
            self.assertEqual(matched_term_count(folds_950, prepared), 1)
            self.assertEqual(prose_hit(folds_951, prepared), 0, "951 正文不含，只有标题含")
            self.assertEqual(matched_term_count(folds_951, prepared), 1)
            self.assertLess(
                ids.index("tulip_garden-0950"),
                ids.index("tulip_garden-0951"),
                "正文命中（含 ααα）必须排在仅标题命中（含 ΑΑΑ）之前",
            )
        finally:
            connection.close()

    def test_unicode_folding_matches_the_real_tokenizer(self):
        """长词折叠必须与**真** trigram 分词器逐字符相同，基准现场问 FTS5 要。

        这条钉的是 fold_unicode() 的正确性本身，不是它的后果。此前的实现是
        ``str.lower()``，而它与 FTS5 不等价——分歧在四组字符上最集中，用户复审点名
        要覆盖的就是这四组：

        - ``Σ/σ/ς``：``lower()`` 对**词尾** ``Σ`` 走 Final_Sigma 折成 ``ς``，
          所以 ``'ΣΣΣ'.lower()`` 是 ``'σσς'``，而 FTS5 折成 ``'σσσ'``。
          **这是上下文相关的，逐字符测不出来**——单测 ``'Σ'.lower()`` 得到 ``'σ'``，
          看着没问题。所以下面每组都测整串排列，不是单字符。
        - ``S/s/ſ``：``ſ``（long s）FTS5 折成 ``s``，``lower()`` 原样留着（它本就小写）。
        - ``ẞ/ß``：FTS5 把 ``ẞ`` 折成 ``ß``；``casefold()`` 展开成 ``ss``，长度都变了。
          这是"别直接换 casefold" 的原因。
        - ``İ/I/i/ı``：FTS5 不折 ``İ``；``lower()`` 把它拆成 ``i`` + U+0307 两个码点。

        **基准不硬编码。** fts5_folded() 现场向内存 trigram 表要答案，所以换个 SQLite
        版本时这条测试会指出真实差异，而不是拿旧版本的行为当圣经。
        """
        checked = 0
        for name, group in FOLD_DIFF_GROUPS:
            for combination in itertools.product(group, repeat=3):
                word = "".join(combination)
                for probe in (word, f"情绪{word}周期"):
                    with self.subTest(group=name, probe=probe):
                        self.assertEqual(
                            fold_unicode(probe),
                            fts5_folded(probe),
                            f"{name}：折叠结果必须与真 tokenizer 相同",
                        )
                    checked += 1
        self.assertEqual(checked, 252, "四组三字排列 × 两种上下文，覆盖数是钉死的")

        # 反向钉住"为什么必须换掉 lower()/casefold()"：两者在这批例子上都有分歧，
        # 所以上面那批断言不是恒真的。数字是实测值，不是估计。
        divergent_lower = [
            probe
            for name, group in FOLD_DIFF_GROUPS
            for combination in itertools.product(group, repeat=3)
            for probe in ("".join(combination), f"情绪{''.join(combination)}周期")
            if probe.lower() != fts5_folded(probe)
        ]
        divergent_casefold = [
            probe
            for name, group in FOLD_DIFF_GROUPS
            for combination in itertools.product(group, repeat=3)
            for probe in ("".join(combination), f"情绪{''.join(combination)}周期")
            if probe.casefold() != fts5_folded(probe)
        ]
        self.assertEqual(len(divergent_lower), 158, "str.lower() 在这四组上的分歧数")
        self.assertEqual(len(divergent_casefold), 90, "casefold() 在这四组上的分歧数")
        self.assertIn("ΣΣΣ", divergent_lower, "ΣΣΣ 是 lower() 的分歧例（Final_Sigma）")
        self.assertIn("ẞẞẞ", divergent_casefold, "ẞẞẞ 是 casefold() 的分歧例（展开成 ss）")

        # 折叠表的两条性质，评分两侧都折的前提：1:1 单码点（不改变长度）、幂等。
        for source, image in UNICODE_FOLD_TABLE.items():
            with self.subTest(codepoint=f"U+{source:04X}"):
                self.assertEqual(len(image), 1, "折叠必须是 1:1 单码点，否则长度会变")
                self.assertEqual(fold_unicode(image), image, "折叠表的像必须是不动点")

    def test_query_term_folding_survives_context_sensitive_lowercase(self):
        """查询词自身不能被折错：``ΣΣΣ`` 召回的两个正文块必须拿到 prose_hit=1。

        这是 str.lower() 那个缺陷的**端到端形态**，也是用户复审的第 1 点。FTS5 按自己的
        折叠表建 gram，所以 ``MATCH '"ΣΣΣ"'`` 会召回正文写作 ``σσσ`` 和 ``ςςς`` 的块
        （三者折叠后同为 ``σσσ``）。评分侧若用 ``str.lower()``，查询词会被折成 ``σσς``
        （词尾 Σ 走 Final_Sigma），于是两个正文块的 ``prose_hit`` 全是 0，
        被压到"仅标题命中"的块后面——**召回对了，排序反了**。

        比 test_long_term_scoring_folds_like_fts_match 更强的地方在于：那条用的是
        ``Α/α``，``lower()`` 恰好折对，所以它测的是"口径按词长分流"；这条用 sigma，
        专门打 lower() 折错的那一处。
        """
        connection = build_fixture()
        try:
            # 960 / 961：正文分别写作 σσσ 和 ςςς，标题都不含。
            add_chunk(
                connection,
                chunk("tulip_garden", 960, "这块正文里有小写 σσσ 三连，标题不含。"),
            )
            add_chunk(
                connection,
                chunk("tulip_garden", 961, "这块正文里有词尾式 ςςς 三连，标题不含。"),
            )
            # 962：正文与那三个字母无关，只有标题含大写 ΣΣΣ。
            add_chunk(
                connection,
                chunk(
                    "tulip_garden",
                    962,
                    "这块正文讲的是别的事情，完全不含那三个字母。",
                    title="ΣΣΣ 专题整理",
                ),
            )
            connection.commit()

            rows = search(connection, "ΣΣΣ", None, None, 500)
            ids = [row["chunk_id"] for row in rows]
            # 无条件断言：FTS5 折叠了三种写法，三块都必须被召回。
            for index in (960, 961, 962):
                self.assertIn(
                    f"tulip_garden-{index:04d}", ids, f"{index} 必须被 ΣΣΣ 召回"
                )

            by_id = {row["chunk_id"]: row for row in rows}
            prepared = folded_terms(tuple(terms_from_query("ΣΣΣ")))
            for index in (960, 961):
                chunk_id = f"tulip_garden-{index:04d}"
                folds = FieldFolds(by_id[chunk_id])
                with self.subTest(prose_chunk=chunk_id):
                    self.assertEqual(
                        prose_hit(folds, prepared),
                        1,
                        "FTS5 召回了它，评分就必须承认正文命中——lower() 会给 0",
                    )
                    self.assertEqual(matched_term_count(folds, prepared), 1)
                    self.assertLess(
                        ids.index(chunk_id),
                        ids.index("tulip_garden-0962"),
                        "正文命中必须排在仅标题命中之前",
                    )
            title_only = FieldFolds(by_id["tulip_garden-0962"])
            self.assertEqual(prose_hit(title_only, prepared), 0, "962 正文不含")
            self.assertEqual(matched_term_count(title_only, prepared), 1)
        finally:
            connection.close()

    def test_case_variants_of_a_long_term_dedup_to_one(self):
        """``ΣΣΣ σσσ ςςς`` 必须去重成一个词，结果与顺序等同于单查 ``ΣΣΣ``。

        用户复审的第 2 点。dedup_key() 按"折叠后是否相同"判等价，所以这条的成立完全
        依赖 fold_unicode() 折得对：用 ``str.lower()`` 时三者折成 ``σσς``/``σσσ``/``ςςς``
        三个不同的串，被当成 3 个词，matched_term_count 对同一个块给 1 而不该给的 3
        混进第 2 层排序键——用户看到的是"多打了几遍同一个词的变体，结果就换了顺序"。

        这是 ASCII 侧 ``AI ai Ai aI`` 那条等值断言的 Unicode 版本，缺了它，长词路径的
        大小写等价就只有召回被测过、排序没有。
        """
        self.assertEqual(
            terms_from_query("ΣΣΣ σσσ ςςς"),
            ["ΣΣΣ"],
            "三种写法折叠后相同、走同一条召回路径，必须算一个词",
        )
        connection = build_fixture()
        try:
            add_chunk(
                connection,
                chunk("tulip_garden", 960, "这块正文里有小写 σσσ 三连，标题不含。"),
            )
            add_chunk(
                connection,
                chunk("tulip_garden", 961, "这块正文里有词尾式 ςςς 三连，标题不含。"),
            )
            add_chunk(
                connection,
                chunk(
                    "tulip_garden",
                    962,
                    "这块正文讲的是别的事情，完全不含那三个字母。",
                    title="ΣΣΣ 专题整理",
                ),
            )
            connection.commit()

            single = [row["chunk_id"] for row in search(connection, "ΣΣΣ", None, None, 500)]
            for variants in ("ΣΣΣ σσσ ςςς", "σσσ ςςς ΣΣΣ", "ςςς ΣΣΣ σσσ ΣΣΣ"):
                with self.subTest(query=variants):
                    # 词表保留首次出现的原始写法，所以这里只断言结果，不断言词表内容。
                    self.assertEqual(
                        [row["chunk_id"] for row in search(connection, variants, None, None, 500)],
                        single,
                        "变体查询的结果与顺序必须与单查完全一致",
                    )
            # 反向钉住"不去重会失真"：三个词时第 2 层排序键会被撑到 3。
            unduped = folded_terms(("ΣΣΣ", "σσσ", "ςςς"))
            rows = {row["chunk_id"]: row for row in search(connection, "ΣΣΣ", None, None, 500)}
            self.assertEqual(
                matched_term_count(FieldFolds(rows["tulip_garden-0960"]), unduped),
                3,
                "这条记录的是未去重时的失真值，用来证明上面那条不是恒真",
            )
        finally:
            connection.close()

    def test_the_two_case_folding_kinds_are_split_by_term_length(self):
        """折叠口径的分流依据是**词长**，不是"词里有没有非 ASCII 字符"。

        上面两条测的是行为，这条钉实现层的分流规则：同样是希腊字母，两字的 ``ΑΑ`` 走
        ASCII 口径（因为它走 GLOB），三字的 ``ΑΑΑ`` 走 Unicode 口径（因为它走 MATCH）。
        看着像不一致，其实正是一致——口径跟着召回路径走，而路径按长度选。

        阈值必须与 split_terms_by_length() 用的是同一个常量，否则某个长度的词会出现
        "召回走 A 路径、评分用 B 口径"的错配。
        """
        self.assertEqual(fold_kind_for("ΑΑ"), ASCII_FOLD)
        self.assertEqual(fold_kind_for("AI"), ASCII_FOLD)
        self.assertEqual(fold_kind_for("竞价"), ASCII_FOLD)
        self.assertEqual(fold_kind_for("ΑΑΑ"), UNICODE_FOLD)
        self.assertEqual(fold_kind_for("弱转强"), UNICODE_FOLD)
        self.assertEqual(fold_kind_for("AI硬件"), UNICODE_FOLD)
        # 阈值与召回分流同源：恰好 MATCH_MIN_LENGTH 长的词归 Unicode，短一个字符归 ASCII。
        boundary = "Α" * MATCH_MIN_LENGTH
        self.assertEqual(fold_kind_for(boundary), UNICODE_FOLD)
        self.assertEqual(fold_kind_for(boundary[:-1]), ASCII_FOLD)
        long_terms, short_terms = split_terms_by_length([boundary, boundary[:-1]])
        self.assertEqual(long_terms, [boundary], "归 Unicode 口径的词必须正是走 MATCH 的那批")
        self.assertEqual(short_terms, [boundary[:-1]], "归 ASCII 口径的词必须正是走 GLOB 的那批")

    def test_ascii_folding_is_not_skipped_for_already_lowercase_terms(self):
        """跳过字段折叠的判据是"词里有没有带大小写的字符"，不是"词折叠前后是否相同"。

        性能修复引入了一条快捷路径：词在该口径下不含带大小写的字符时（``竞价``、
        ``弱转强`` 这类纯汉字词），字段就不折了，直接在原文上搜。这条路径的判据很容易
        写错成"折叠后与原词相同就跳过"——那样 ``ai``、``Ai`` 折叠前后都含小写、被判成
        不用折，于是搜不到写作 ``AI硬件`` 的块，四写法等值当场破掉。

        所以这里三样都钉：判据本身、它在**评分层**的后果、四种写法结果逐条相同。

        **只断言召回集合是抓不到这个 bug 的。** needs_fold 只作用在 Python 侧评分，
        召回由 SQL 的 ``GLOB '*[Aa][Ii]*'`` 算——四种写法经 glob_pattern() 生成的是
        **同一个模式**，判据写错时召回一条不少，坏掉的是那批块的 prose_hit 和
        matched_term_count，也就是排序。所以主体断言落在评分函数上。
        """
        # 一、判据本身：折叠后的词里有字符是"别的写法的折叠目标"就得折，与"这个写法
        # 要不要改"无关。判据收的是**折叠后的形式**（见 needs_cased_fold()）。
        self.assertTrue(needs_cased_fold("ai", ASCII_FOLD), "ai 含 ASCII 字母，字段必须折")
        self.assertTrue(needs_cased_fold(fold("AI", ASCII_FOLD), ASCII_FOLD))
        self.assertFalse(needs_cased_fold("竞价", ASCII_FOLD), "纯汉字词可以跳过字段折叠")
        self.assertFalse(needs_cased_fold("弱转强", UNICODE_FOLD))
        # 希腊字母在 ASCII 口径下不算"有别的写法"（GLOB 不折它），在 Unicode 口径下算。
        self.assertFalse(needs_cased_fold("ΑΑ", ASCII_FOLD))
        self.assertTrue(needs_cased_fold(fold("ΑΑΑ", UNICODE_FOLD), UNICODE_FOLD))
        # 判据必须收折叠后的形式，不能收原词。大写 Σ 不是任何字符的折叠目标，
        # 拿原词查像集合会把 ΣΣΣ 判成"不用折"，于是正文写作 ςςς 的块搜不到。
        self.assertTrue(
            needs_cased_fold(fold("ΣΣΣ", UNICODE_FOLD), UNICODE_FOLD),
            "ΣΣΣ 折叠后是 σσσ，σ 是 Σ/ς 的折叠目标，字段必须折",
        )
        self.assertTrue(FoldedTerm("ΣΣΣ").needs_fold, "FoldedTerm 必须传折叠后的形式给判据")

        connection = build_case_fixture()
        try:
            # 二、评分层：拿**全小写**的 ai 去评分，正文写作 AI / Ai / aI 的块也必须算
            # 正文命中。判据错写成"折叠前后是否相同"时，ai 被判成不用折，这几条全变 0。
            lowercase = folded_terms(("ai",))
            rows = {
                row["chunk_id"]: row
                for row in search(connection, "ai", None, None, 500)
            }
            for index, form in enumerate(CASE_FORMS, start=1):
                chunk_id = f"{CASE_SOURCE}-{index:04d}"
                with self.subTest(prose_written_as=form):
                    self.assertIn(chunk_id, rows, f"正文写作 {form} 的块必须被 ai 召回")
                    folds = FieldFolds(rows[chunk_id])
                    self.assertEqual(
                        prose_hit(folds, lowercase), 1, f"小写 ai 必须认出正文里的 {form}"
                    )
                    self.assertEqual(matched_term_count(folds, lowercase), 1)
            # 折叠对 title / author 列同样生效，不只是 text。
            for chunk_id, column, written in (
                (f"{CASE_SOURCE}-0010", "title", "AI方向专题"),
                (f"{CASE_SOURCE}-0020", "author", "Ai研究员"),
            ):
                with self.subTest(column=column):
                    folds = FieldFolds(rows[chunk_id])
                    self.assertEqual(prose_hit(folds, lowercase), 0, "这两块正文都不含该词")
                    self.assertEqual(
                        matched_term_count(folds, lowercase),
                        1,
                        f"小写 ai 必须认出 {column} 列里的 {written}",
                    )
            # 三、可观察后果：四种写法的结果逐条相同，顺序也相同。
            ordering = {
                form: [row["chunk_id"] for row in search(connection, form, None, None, 500)]
                for form in CASE_FORMS
            }
            self.assertGreater(
                len(ordering[CASE_FORMS[0]]), 1, "候选必须多于一条，否则顺序断言是空转的"
            )
            for form in CASE_FORMS[1:]:
                with self.subTest(form=form):
                    self.assertEqual(
                        ordering[form],
                        ordering[CASE_FORMS[0]],
                        "四种写法必须返回逐条相同、顺序相同的结果",
                    )
        finally:
            connection.close()

    def test_chunk_type_bonus_is_documented_as_a_ranking_factor(self):
        """CHUNK_TYPE_BONUS 不能隐藏在"字段命中分"里。

        curated_method +5、conflict +2 是实际排序因素。
        给出一个字段命中相同、chunk_type 不同的反例测试。
        """
        connection = build_fixture()
        try:
            # 两块正文完全相同，字段命中分相同，只有 chunk_type 不同
            add_chunk(
                connection,
                chunk(
                    "tulip_garden",
                    930,
                    "正文只提一次竞价。",
                    chunk_type="article",
                ),
            )
            add_chunk(
                connection,
                chunk(
                    "tulip_garden",
                    931,
                    "正文只提一次竞价。",
                    chunk_type="curated_method",
                ),
            )
            connection.commit()

            order = [row["chunk_id"] for row in search(connection, "竞价", None, None, 500)]
            self.assertLess(
                order.index("tulip_garden-0931"),
                order.index("tulip_garden-0930"),
                "curated_method 必须排在 article 之前（+5.0 加成）",
            )
        finally:
            connection.close()

    # --- 混合与多词查询 ---

    def test_mixed_length_query_recalls_both_lengths(self):
        # 阶段 2 前这条叫 ..._uses_the_fts_branch：'竞价 弱转强' 只留下 弱转强，所以
        # 结果里每一条正文都真含其中一个词。现在短词也参与召回，而两字词的合法命中
        # 包含"仅标题或仅作者含词"的块（fixture 里 10 + 5 条），它们的正文不含任何词。
        # 所以口径从"正文必含"改成"三字段并集内必含"——正文口径会要求实现把标题命中
        # 丢掉，与 SPEC 2.2 的召回字段定义直接冲突。
        rows = self.search("竞价 弱转强", limit=50)
        self.assertTrue(rows)
        self.assertTrue(all(row["rank"] != 999.0 for row in rows))
        target = recall_target_ids(self.connection, "竞价") | recall_target_ids(
            self.connection, "弱转强"
        )
        for row in rows:
            with self.subTest(chunk=row["chunk_id"]):
                self.assertIn(row["chunk_id"], target)

    def test_short_term_contributes_in_a_mixed_length_query(self):
        # 阶段 2 摘除 expectedFailure。修复前：两字词被整个丢掉，'情绪 弱转强' 与单查
        # '弱转强' 返回完全一样的结果——短词贡献为零，而且用户看不出来。
        # fixture 里 SECOND_TERM 只在 30 个 tulip_garden 块里，且这 30 块不含 弱转强，
        # 而 弱转强 覆盖另外 150 块，所以短词生效后结果集必须比单查 弱转强 更大。
        mixed = {row["chunk_id"] for row in self.search(f"{SECOND_TERM} 弱转强", limit=500)}
        long_only = {row["chunk_id"] for row in self.search("弱转强", limit=500)}
        self.assertTrue(prose_ids(self.connection, SECOND_TERM) <= mixed)
        self.assertGreater(len(mixed), len(long_only))

    def test_multiple_two_character_terms_reach_the_whole_recall_target(self):
        # Recall side of OR semantics: with a limit large enough to defeat truncation, both
        # terms' three-field matches come back. 竞价 covers 165 chunks (150 prose + 15
        # title/author-only), SECOND_TERM another 30 disjoint from those, so the union is 195.
        found = {row["chunk_id"] for row in self.search(f"竞价 {SECOND_TERM}", limit=500)}
        expected = recall_target_ids(self.connection, "竞价") | recall_target_ids(
            self.connection, SECOND_TERM
        )
        self.assertEqual(len(expected), 195)
        self.assertTrue(expected <= found)

    def test_multiple_two_character_terms_do_not_return_label_only_chunks(self):
        # 阶段 2 摘除 expectedFailure。修复前：limit=500 时兜底把全部 475 块返回，其中
        # 280 块 text/title/author 三个字段里都没有这两个词，纯靠 topics 自动标签进来
        # （缺陷 A + 缺陷 C 的组合）。修复后：只返回三字段并集的块。
        target = recall_target_ids(self.connection, "竞价") | recall_target_ids(
            self.connection, SECOND_TERM
        )
        got = {row["chunk_id"] for row in self.search(f"竞价 {SECOND_TERM}", limit=500)}
        self.assertEqual(got, target)

    def test_multiple_two_character_terms_both_contribute(self):
        # 阶段 2 摘除 expectedFailure。修复前：默认 limit=8 时候选池 240 条全被 fulibei
        # 的纯标签块占满，只含『情绪』的 30 个 tulip_garden 块一条都进不来。
        #
        # 断言口径从 limit=8 改成大 limit，这不是放宽标准，是把它挪到该管的那一层。
        # 原来那两条断言——"前 8 条里必须出现 SECOND_TERM"、"前 8 条正文必须含词"——
        # 都是对前 8 条顺序的要求，而 SECOND_TERM 在 fixture 里只存在于 tulip_garden，
        # 等于要求小 limit 结果跨来源。SPEC 2.3 把这条线划给阶段 3（阶段 2 用大 limit
        # 查召回不看顺序），SPEC 2.2 也明确小 limit 结果不检查来源数量。
        #
        # 这里改查"两个词各自独有的命中都真的进了召回"：竞价独有 165 条、情绪独有
        # 30 条、交集 0，所以任一词被丢掉都会让对应那一侧变成空集。排序那一半由
        # SourceCoverageTests 里阶段 3 的普通排序断言负责。
        rows = self.search(f"竞价 {SECOND_TERM}", limit=500)
        got = {row["chunk_id"] for row in rows}
        first_only = recall_target_ids(self.connection, "竞价") - recall_target_ids(
            self.connection, SECOND_TERM
        )
        second_only = recall_target_ids(self.connection, SECOND_TERM) - recall_target_ids(
            self.connection, "竞价"
        )
        self.assertTrue(first_only and second_only, "两个词的独有命中不能为空，否则断言空转")
        self.assertTrue(first_only <= got)
        self.assertTrue(second_only <= got)

    # --- 过滤器 ---

    def test_source_filter_narrows_both_paths(self):
        # --source must hold on the FTS branch and the fallback branch alike. 弱转强
        # goes through FTS, 竞价 through the fallback, so this covers both.
        for query in ("弱转强", "竞价"):
            with self.subTest(query=query):
                rows = self.search(query, limit=50, source="tulip_garden")
                self.assertTrue(rows)
                self.assertEqual(sources_of(rows), {"tulip_garden": len(rows)})

    def test_unknown_source_filter_returns_nothing(self):
        # A typo in --source must yield an empty result, not silently fall through to
        # an unfiltered search. The fallback branch reuses filter_sql, so if it were
        # dropped there, this would come back full.
        self.assertEqual(self.search("竞价", limit=50, source="no_such_source"), [])

    def test_author_filter_matches_author_or_title(self):
        # query_kb.py:30 deliberately widens --author to the title column; the fixture's
        # author is 示例作者 and its title 示例文档, so both spellings must hit.
        for value in ("示例作者", "示例文档"):
            with self.subTest(author=value):
                self.assertTrue(self.search("弱转强", limit=10, author=value))

    def test_unknown_author_filter_returns_nothing(self):
        self.assertEqual(self.search("弱转强", limit=50, author="不存在的作者"), [])

    # --- 退化输入 ---

    def test_query_with_no_match_returns_empty(self):
        # Must return nothing rather than falling back to "here are some chunks".
        self.assertEqual(self.search("这个词库里根本没有", limit=8), [])

    def test_blank_query_does_not_crash(self):
        # terms_from_query('') yields [''], and LIKE '%%' matches every row. Today that
        # means a blank query returns arbitrary chunks. Not asserting which behaviour is
        # right — only that it neither raises nor is treated as a real search.
        for query in ("", "   ", "，、；"):
            with self.subTest(query=repr(query)):
                rows = self.search(query, limit=5)
                self.assertIsInstance(rows, list)

    def test_quote_in_query_does_not_break_fts_syntax(self):
        # query_kb.py:37 doubles embedded quotes before building the MATCH expression.
        # An unescaped quote would raise sqlite3.OperationalError instead of returning.
        for query in ('弱转强"', '"弱转强"', "弱转强'"):
            with self.subTest(query=query):
                self.assertIsInstance(self.search(query, limit=5), list)

    def test_limit_zero_and_one_are_honoured(self):
        self.assertEqual(self.search("弱转强", limit=0), [])
        self.assertEqual(len(self.search("弱转强", limit=1)), 1)


class GlobEscapingTests(unittest.TestCase):
    """两字词走 GLOB，所以用户输入里的 GLOB 元字符必须先转义成字面量。

    转义前实测（真库 2026-08-02，召回 vs 字面子串并集）：
    ``*`` 3362/45、``?`` 3362/614、``**`` 3362/4、``竞*`` 534/0、``A?`` 1678/0，
    ``[`` 0/1864。前五个是把无关内容当成命中（``*`` 直接返回全库），最后一个方向相反：
    未闭合的字符类让整个模式失效，真实命中全丢。

    SQLite 的 GLOB 没有 ESCAPE 子句（那是 LIKE 才有的），反斜杠也不是转义符，唯一办法
    是把元字符包进字符类。要转的是 ``*``、``?``、``[`` 三个；``]`` 只在紧跟 ``[`` 时特殊，
    单独出现就是字面量，不用转。

    断言写成"召回集合严格等于字面子串并集"，不是"不抛异常"：不抛异常这条在转义之前
    就已经成立了（``*`` 老老实实返回了 3362 块），根本测不出这个缺陷。
    """

    @classmethod
    def setUpClass(cls):
        cls.connection = build_glob_fixture()

    @classmethod
    def tearDownClass(cls):
        cls.connection.close()

    def test_escaping_wraps_each_metacharacter_in_a_character_class(self):
        # 转义函数本身。顺序在这里就能看出来：`[` 必须先替换，否则先转 `*` 得到的
        # `[*]` 里那个 `[` 会被第二遍再转一次，变成 `[[]*]`。
        cases = {
            "*": "[*]",
            "?": "[?]",
            "[": "[[]",
            "**": "[*][*]",
            "竞*": "竞[*]",
            "A?": "A[?]",
            "*?[": "[*][?][[]",  # 三个一起出现，验证顺序
            "]": "]",            # 单独的右括号不是元字符
            "筹码": "筹码",       # 不含元字符时原样返回
        }
        for raw, expected in cases.items():
            with self.subTest(term=raw):
                self.assertEqual(glob_literal(raw), expected)

    def test_pattern_adds_wildcards_after_escaping_not_before(self):
        # 顺序错了的话，自己加的那两个星号会被一起转掉，模式变成只匹配字面 `*x*`。
        self.assertEqual(glob_pattern("*"), "*[*]*")
        self.assertEqual(glob_pattern("筹码"), "*筹码*")

    def test_metacharacter_terms_recall_exactly_the_literal_matches(self):
        # 核心断言：每个元字符词的召回集合严格等于字面子串并集。
        # 严格相等同时卡住两头——多一条是通配符还在生效，少一条是转义把模式弄坏了。
        for term in GLOB_METACHARACTER_TERMS:
            with self.subTest(term=term):
                expected = literal_substring_ids(self.connection, term)
                self.assertTrue(expected, f"fixture 里没有含 {term!r} 的块，断言会空转")
                got = {row["chunk_id"] for row in search(self.connection, term, None, None, 500)}
                self.assertEqual(got, expected)

    def test_a_single_star_does_not_satisfy_a_double_star_query(self):
        # 反向情形：查 `**` 不该命中只有一个星号的块。转义失效时 `**` 会匹配一切，
        # 这条就会失败——上面那条等值断言已经覆盖，这里单独写出来是因为它最容易
        # 被一个"看起来能过"的实现蒙过去（比如只转第一个元字符）。
        single = {
            row["chunk_id"]
            for row in self.connection.execute(
                "SELECT chunk_id FROM chunks WHERE instr(text, ?) > 0 AND instr(text, ?) = 0",
                ("*", "**"),
            )
        }
        self.assertTrue(single, "fixture 里需要一块只含单个星号的")
        got = {row["chunk_id"] for row in search(self.connection, "**", None, None, 500)}
        self.assertFalse(got & single)

    def test_star_query_does_not_return_the_whole_table(self):
        # 转义前 `*` 在真库返回全部 3362 块。这条把那个具体故障钉死：结果必须真的
        # 少于全表，否则通配符还在生效。
        total = self.connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
        got = search(self.connection, "*", None, None, 500)
        self.assertLess(len(got), total)
        self.assertTrue(got, "至少有几块真的含星号，不该是空的")

    def test_unclosed_bracket_still_matches_its_literal(self):
        # `[` 的故障方向与其他几个相反：转义前整个模式失效，返回 0 条。
        got = {row["chunk_id"] for row in search(self.connection, "[", None, None, 500)}
        self.assertEqual(got, literal_substring_ids(self.connection, "["))
        self.assertTrue(got, "fixture 里有含 `[` 的块，不该一条都不返回")

    def test_escaping_covers_title_and_author_columns(self):
        # 三列都要转义。只对 text 转的实现会在这两条上露出来。
        for term, column in (("*", "title"), ("?", "author")):
            with self.subTest(column=column):
                expected = {
                    row["chunk_id"]
                    for row in self.connection.execute(
                        f"SELECT chunk_id FROM chunks WHERE instr({column}, ?) > 0", (term,)
                    )
                }
                self.assertTrue(expected)
                got = {row["chunk_id"] for row in search(self.connection, term, None, None, 500)}
                self.assertTrue(expected <= got)

    def test_metacharacters_do_not_break_ordinary_terms(self):
        # 转义不该影响不含元字符的查询，也不该让含元字符的块整体变得不可检索。
        got = {row["chunk_id"] for row in search(self.connection, "竞价", None, None, 500)}
        self.assertEqual(got, literal_substring_ids(self.connection, "竞价"))

    def test_mixed_query_with_a_metacharacter_term_keeps_or_semantics(self):
        # 元字符词与普通词混在一起时，仍是 OR，且两边都按字面口径召回。
        got = {row["chunk_id"] for row in search(self.connection, "竞价 *", None, None, 500)}
        expected = literal_substring_ids(self.connection, "竞价") | literal_substring_ids(
            self.connection, "*"
        )
        self.assertEqual(got, expected)

    def test_metacharacter_query_does_not_raise(self):
        # 附带的健壮性检查。故意放在等值断言之后，且不单独作为验收依据——
        # 转义之前它就已经通过了，测不出任何东西。
        for term in (*GLOB_METACHARACTER_TERMS, "]", "[]", "[a-z]", "***", "?*[", "a**b"):
            with self.subTest(term=term):
                self.assertIsInstance(search(self.connection, term, None, None, 5), list)


class AsciiCaseRecallTests(unittest.TestCase):
    """短词的 ASCII 大小写折叠。GLOB 大小写敏感，MATCH 和 LIKE 都不敏感。

    修复前实测（真库 2026-08-02）：``AI`` 召回 144 块、``ai`` 83 块、``Ai`` 3 块、
    ``aI`` 0 块，而 text/title/author 三列不区分大小写的并集是 218 块——四个查询分别
    丢 74/135/215/218 条。同一个词换个大小写就换一批结果，而用户看不出差别。

    这是短词路径**独有**的回归，不是全局行为：三字以上走 MATCH，``AI硬件`` 与
    ``ai硬件`` 实测同为 8 块；被删掉的 LIKE 兜底路径也不区分大小写。所以阶段 2 把短词
    改走 GLOB 时，恰好在这一条上比旧实现更弱了。

    修法是在 glob_pattern() 里把 ASCII 字母折成 ``[Aa]`` 字符类。折叠范围严格限定
    ASCII，与 SQLite 的 ``lower()`` 口径对齐——用 Python 的 ``isalpha()`` 会把全角
    字母和希腊字母一起折进去，召回比基准多出一批，等值断言反而失败。

    断言挂在 search() 的返回上，不挂在 GLOB 模式的字符串形态上（那种断言只有一条，
    见 test_pattern_folds_ascii_letters_into_character_classes），所以换成 LIKE 路径
    或换 tokenizer，验收标准都不变。
    """

    @classmethod
    def setUpClass(cls):
        cls.connection = build_case_fixture()

    @classmethod
    def tearDownClass(cls):
        cls.connection.close()

    def recall(self, term: str) -> set[str]:
        return {
            row["chunk_id"] for row in search(self.connection, term, None, None, 5000)
        }

    def test_the_fixture_really_holds_every_case_form(self):
        # 防空转。下面每条断言都是"召回 == 不区分大小写并集"，如果 fixture 里某种形式
        # 根本不存在，那一侧就是空集，等值断言会假通过。
        for form in CASE_FORMS:
            with self.subTest(form=form):
                count = self.connection.execute(
                    "SELECT count(*) FROM chunks WHERE instr(text, ?) > 0 "
                    "OR instr(title, ?) > 0 OR instr(author, ?) > 0",
                    [form] * 3,
                ).fetchone()[0]
                self.assertGreater(count, 0, f"fixture 里没有含 {form!r} 的块")

    def test_the_fixture_spreads_case_forms_across_all_three_columns(self):
        # 三列各自都要有大小写变体。只折叠 text 列的实现要靠 title/author 上的变体
        # 才能暴露，所以这里把 fixture 的这个形状钉住。
        for column in ("text", "title", "author"):
            with self.subTest(column=column):
                forms = {
                    row[0]
                    for row in self.connection.execute(
                        f"SELECT DISTINCT substr({column}, instr(lower({column}), 'ai'), 2) "
                        f"FROM chunks WHERE instr(lower({column}), 'ai') > 0"
                    )
                }
                self.assertGreaterEqual(
                    len(forms), 2, f"{column} 列只有 {forms} 一种写法，折叠断言在这列上空转"
                )

    def test_the_fixture_is_not_entirely_made_of_matches(self):
        # 另一头的防空转：必须有块不含该词，否则"召回 == 并集"在一个直接返回全表的
        # 实现下也成立。
        total = self.connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
        self.assertLess(len(case_insensitive_ids(self.connection, CASE_TERM)), total)

    def test_every_case_form_recalls_the_same_chunk_ids(self):
        # 核心断言之一：AI / ai / Ai / aI 必须返回**同一个** chunk_id 集合。
        # 修复前四者分别是 144/83/3/0 块（真库口径），互不相等。
        recalls = {form: self.recall(form) for form in CASE_FORMS}
        first = recalls[CASE_FORMS[0]]
        self.assertTrue(first, "fixture 里该词一条都召不回，断言会空转")
        for form, got in recalls.items():
            with self.subTest(form=form):
                self.assertEqual(got, first, f"{form!r} 的召回集合与 {CASE_FORMS[0]!r} 不同")

    def test_case_folded_recall_equals_the_case_insensitive_union(self):
        # 核心断言之二：集合必须**等于** text/title/author 的不区分大小写并集。
        # 只做"四者相等"不够——四个都返回空集也相等。等值同时卡住两头：少一条是折叠
        # 没覆盖到某列或某种形式，多一条是把 topics 也放进了召回。
        expected = case_insensitive_ids(self.connection, CASE_TERM)
        self.assertTrue(expected)
        for form in CASE_FORMS:
            with self.subTest(form=form):
                self.assertEqual(self.recall(form), expected)

    def test_case_folding_does_not_recall_topics_only_chunks(self):
        # 折叠不得顺带把 topics 拉回召回（阶段 1 已把它移出 FTS 可匹配列）。
        # 这个 fixture 的 topics 一律是"盘口"，不含 ai，所以这里换个词构造：
        # 给一块打上含词的 topics，正文标题作者都不含，它必须召不到。
        connection = build_case_fixture()
        try:
            add_chunk(
                connection,
                chunk(
                    CASE_SOURCE,
                    90,
                    "第90节正文完全不含那个英文词。",
                    title="纯净标题",
                    author="纯净作者",
                    topics="AI与算力",
                ),
            )
            connection.commit()
            label_only = f"{CASE_SOURCE}-0090"
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM chunks WHERE chunk_id = ? "
                    "AND instr(lower(topics), 'ai') > 0 AND instr(lower(text), 'ai') = 0 "
                    "AND instr(lower(title), 'ai') = 0 AND instr(lower(author), 'ai') = 0"
                    , (label_only,)
                ).fetchone()[0],
                1,
                "这一块必须只在 topics 含词，否则测不出污染",
            )
            for form in CASE_FORMS:
                with self.subTest(form=form):
                    got = {
                        row["chunk_id"]
                        for row in search(connection, form, None, None, 5000)
                    }
                    self.assertNotIn(label_only, got)
                    self.assertEqual(got, case_insensitive_ids(connection, CASE_TERM))
        finally:
            connection.close()

    def test_long_terms_keep_their_case_insensitive_match_behaviour(self):
        # 对照组：三字以上走 MATCH，本来就不区分大小写。这条守住"修短词没把长词
        # 改坏"，也说明为什么短词以前的行为是回归而不是设计。
        self.assertEqual(self.recall("AI硬件"), self.recall("ai硬件"))
        self.assertEqual(self.recall("AI硬件"), case_insensitive_ids(self.connection, "AI硬件"))

    def test_mixed_length_query_folds_the_short_term_too(self):
        # 混合长短词时短词仍要折叠。OR 语义下长词的命中会掩盖短词的贡献，所以断言
        # 写成"两种大小写的混合查询结果相同"，而不是只看总数不为空。
        self.assertEqual(self.recall("AI 弱转强"), self.recall("ai 弱转强"))
        self.assertEqual(
            self.recall("ai 弱转强"),
            case_insensitive_ids(self.connection, CASE_TERM)
            | recall_target_ids(self.connection, "弱转强"),
        )

    def test_case_folding_preserves_metacharacter_literals(self):
        # 折叠与转义的顺序：先转义再折叠。反过来的话 glob_literal 会把折叠产生的
        # `[Aa]` 改成 `[[]Aa]`，模式全错。`A?` 是这两件事同时发生的最小例子。
        self.assertEqual(glob_pattern("A?"), "*[Aa][?]*")
        self.assertEqual(glob_pattern("a*b"), "*[Aa][*][Bb]*")
        self.assertEqual(glob_pattern("["), "*[[]*")

    def test_pattern_folds_ascii_letters_into_character_classes(self):
        # 唯一一条挂在模式形态上的断言，因为字符类的**写法**本身有个容易错的点：
        # `]` 出现在字母后面时（`a]` → `[Aa]]`）不能再包一层，那样会让类不闭合。
        cases = {
            "AI": "*[Aa][Ii]*",
            "ai": "*[Aa][Ii]*",
            "A股": "*[Aa]股*",
            "5G": "*5[Gg]*",
            "a]": "*[Aa]]*",
            "竞价": "*竞价*",       # 中日韩字符不折叠
            "Ａ": "*Ａ*",           # 全角字母不折叠：SQLite 的 lower() 也不折它
            "Α": "*Α*",            # 希腊字母同理
        }
        for raw, expected in cases.items():
            with self.subTest(term=raw):
                self.assertEqual(glob_pattern(raw), expected)

    def test_non_ascii_case_is_left_alone_on_purpose(self):
        # 折叠范围的边界，写成断言而不是只写在注释里。验收口径是"等于 SQLite
        # lower() 定义的并集"，而 lower() 只折 ASCII。把全角字母也折进去会让召回
        # 超出基准——那是"更强"而不是"一致"，本轮不做。
        self.assertEqual(glob_fold_ascii_case("Ａ"), "Ａ")
        self.assertEqual(glob_fold_ascii_case("Α"), "Α")
        self.assertEqual(glob_fold_ascii_case("ß"), "ß")
        self.assertEqual(
            self.connection.execute("SELECT lower('ＡΑß')").fetchone()[0], "ＡΑß"
        )

    def test_source_and_author_filters_still_apply_to_folded_queries(self):
        # 过滤器不能因为折叠而失效。--author 走的是 LIKE（本来不区分大小写），
        # 这里两种大小写都查一遍，确认过滤后的集合仍然一致。
        for form in CASE_FORMS:
            with self.subTest(form=form):
                scoped = search(self.connection, form, CASE_SOURCE, None, 5000)
                self.assertTrue(scoped)
                self.assertEqual({row["source_id"] for row in scoped}, {CASE_SOURCE})
                self.assertEqual(
                    len(search(self.connection, form, "不存在的来源", None, 5000)), 0
                )
        by_author = {
            row["chunk_id"] for row in search(self.connection, "AI", None, "研究员", 5000)
        }
        self.assertTrue(by_author)
        self.assertEqual(
            by_author,
            {row["chunk_id"] for row in search(self.connection, "ai", None, "研究员", 5000)},
        )

    def test_recalled_chunk_ids_stay_unique_after_folding(self):
        # 一块可能同时被多种形式、多列命中（正文 AI + 标题 ai），去重必须仍然成立。
        for form in CASE_FORMS:
            with self.subTest(form=form):
                rows = search(self.connection, form, None, None, 5000)
                ids = [row["chunk_id"] for row in rows]
                self.assertEqual(len(ids), len(set(ids)))


DATABASE = Path(__file__).resolve().parents[2] / "_知识库系统" / "indexes" / "knowledge.db"
SOURCES_CONFIG = Path(__file__).resolve().parents[2] / "_知识库系统" / "config" / "sources.yaml"

# Two different source scopes, for two different jobs. Conflating them is what made the
# earlier revision of this file wrong.
#
# REGRESSION_SAMPLE — the three sources whose row counts and hit counts SPEC freezes as a
# fixed historical sample (1470/525/1181). Frozen so that adding or re-importing any other
# source cannot invalidate a baseline number. It is a *sample*, not the project's scope.
#
# registered_sources() — every source in sources.yaml. Recall and functional acceptance
# must cover all of them, because a query the user types is not scoped to the sample.
# Read from the config rather than hardcoded: a fifth source must not require editing
# this file for the assertions to keep covering everything.
REGRESSION_SAMPLE = ("fulibei", "nanjinglu_bian", "tulip_garden")
SAMPLE_PLACEHOLDERS = ",".join("?" for _ in REGRESSION_SAMPLE)

# The two-character terms that expose the recall defect (SPEC 阶段 2). Named once so that
# the two expectedFailure cases — sample scope and registry scope — and the plain guards
# that keep them from going vacuous all iterate over exactly the same list.
RECALL_DEFECT_TERMS = ("竞价", "龙头", "打板", "情绪")


def source_registry() -> dict[str, dict[str, str]]:
    """Every source in sources.yaml as ``id -> {scalar field: value}``.

    Parsed with a minimal reader instead of importing yaml, so the fixture-only subset of
    this module stays free of third-party imports. Scoped to the ``sources:`` block and
    stopped at the next top-level key, so an unrelated list of ``- id:`` entries elsewhere
    in the file cannot leak in. SourceRegistryTests checks the parse, and
    RegistryScopedIndexTests cross-checks the result against the index.

    Only scalar ``key: value`` fields are kept. A nested block (``format_counts:``,
    ``complexity_signals:``) is recorded with an empty value and its children skipped by
    indentation — without that, ``.html: 1`` under ``format_counts:`` was read as a field of
    the source itself. Nothing here needs the nested values, so they are dropped rather
    than modelled.

    Fields are read, not just ids, so a per-source declaration can drive a test instead of
    the test assuming every source looks like the ones present today — ``author`` and
    ``smoke_query`` are used that way below.
    """
    registry: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    inside = False
    nested_above: int | None = None  # indent of the block whose children we are skipping
    for raw in SOURCES_CONFIG.read_text(encoding="utf-8").splitlines():
        if not raw.startswith((" ", "\t", "#")) and raw.strip():
            inside = raw.split(":", 1)[0].strip() == "sources"
            current = None
            nested_above = None
            continue
        line = raw.strip()
        if not inside or not line or line.startswith("#"):
            continue
        indent = len(raw.expandtabs(4)) - len(raw.expandtabs(4).lstrip())
        if nested_above is not None:
            if indent > nested_above:
                continue
            nested_above = None
        if line.startswith("- "):
            key, _, value = line[2:].partition(":")
            if key.strip() != "id":
                continue
            current = {}
            registry[value.strip().strip("'\"")] = current
            continue
        if current is None or ":" not in line:
            continue
        key, _, value = line.partition(":")
        current[key.strip()] = value.strip().strip("'\"")
        if not value.strip():
            nested_above = indent
    return registry


def registered_sources() -> tuple[str, ...]:
    """Every registered source id, sorted for stable test output."""
    return tuple(sorted(source_registry()))


# SPEC 3.0 requires zero skips at acceptance. Without this switch a missing index
# silently skips every real-corpus assertion and unittest still prints "OK", so the
# acceptance run sets KB_REQUIRE_REAL_INDEX=1 and turns that into a failure instead.
REQUIRE_REAL_INDEX = os.environ.get("KB_REQUIRE_REAL_INDEX") == "1"


class RealIndexRequirementTests(unittest.TestCase):
    """Fails loudly when the acceptance run is missing the index it claims to verify."""

    def test_index_exists_when_required(self):
        if not REQUIRE_REAL_INDEX:
            self.assertTrue(True, "未要求真库；RealIndexTests 允许 skip")
            return
        self.assertTrue(
            DATABASE.exists(),
            f"KB_REQUIRE_REAL_INDEX=1 但索引不存在，先跑 build_index.py：{DATABASE}",
        )


# The condition deliberately ignores REQUIRE_REAL_INDEX. An earlier version read
# ``DATABASE.exists() or REQUIRE_REAL_INDEX`` so that the flag would force these to run,
# but a missing file then made setUpClass raise ``unable to open database file`` and the
# acceptance run reported ``failures=1, errors=1`` — the error being pure noise on top of
# the one real diagnosis. Enforcement belongs to RealIndexRequirementTests above: with
# the flag set and no index, that single test fails and the exit code is 1 regardless of
# how many cases here skip.
@unittest.skipUnless(
    DATABASE.exists(),
    f"索引不存在，先跑 build_index.py：{DATABASE}",
)
class RealIndexTests(unittest.TestCase):
    """Cross-checks the fixture's conclusions against the real index, read-only.

    An in-memory table cannot reproduce bm25 score distribution or LIMIT behaviour on
    48M of data, so the numbers SPEC.md commits to are verified here as well. These
    read the live database and never write to it.
    """

    @classmethod
    def setUpClass(cls):
        cls.connection = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)
        cls.connection.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.connection.close()

    def prose_matches(self, term: str) -> int:
        return self.connection.execute(
            f"SELECT count(*) FROM chunks WHERE source_id IN ({SAMPLE_PLACEHOLDERS}) AND text LIKE ?",
            (*REGRESSION_SAMPLE, f"%{term}%"),
        ).fetchone()[0]

    def prose_ids(self, term: str) -> set[str]:
        """chunk_ids whose prose contains the term, restricted to the regression sample."""
        return {
            row[0]
            for row in self.connection.execute(
                f"SELECT chunk_id FROM chunks "
                f"WHERE source_id IN ({SAMPLE_PLACEHOLDERS}) AND text LIKE ?",
                (*REGRESSION_SAMPLE, f"%{term}%"),
            )
        }

    def recall_target_ids(self, term: str) -> set[str]:
        """The SPEC 2.2 recall target: text/title/author union, topics excluded.

        Asserting against prose alone would demand that the fix DROP legitimate results:
        measured on the frozen sources, 龙头 has 310 chunks and 情绪 405 whose title
        carries the term while the prose does not. Those titles are human-written, unlike
        the auto-generated topics column, and stage 1 keeps them in the FTS index.
        """
        return {
            row[0]
            for row in self.connection.execute(
                f"SELECT chunk_id FROM chunks WHERE source_id IN ({SAMPLE_PLACEHOLDERS}) "
                f"AND (text LIKE ? OR title LIKE ? OR author LIKE ?)",
                (*REGRESSION_SAMPLE, *[f"%{term}%"] * 3),
            )
        }

    def label_only_ids(self, term: str) -> set[str]:
        """chunk_ids matched by topics alone — the noise the recall path must exclude."""
        return {
            row[0]
            for row in self.connection.execute(
                f"SELECT chunk_id FROM chunks WHERE source_id IN ({SAMPLE_PLACEHOLDERS}) "
                f"AND topics LIKE ? AND text NOT LIKE ? AND title NOT LIKE ? AND author NOT LIKE ?",
                (*REGRESSION_SAMPLE, *[f"%{term}%"] * 4),
            )
        }

    def scoped_fts_hits(self, term: str, column: str | None = None) -> int:
        """Phrase MATCH count restricted to the regression sample.

        chunks_fts has no source_id column, so the bare fts_hits() helper counts every
        registered source: 情绪周期 is 1850 whole-table against 1823 within the sample.
        Any assertion that compares an FTS count against a chunks-table count has to go
        through this, or it compares the whole corpus against the sample.
        """
        target = column or "chunks_fts"
        return self.connection.execute(
            f"SELECT count(*) FROM chunks_fts f JOIN chunks c ON c.chunk_id = f.chunk_id "
            f"WHERE {target} MATCH ? AND c.source_id IN ({SAMPLE_PLACEHOLDERS})",
            ['"' + term.replace('"', '""') + '"', *REGRESSION_SAMPLE],
        ).fetchone()[0]

    def scoped_glob_hits(self, term: str, column: str = "text") -> int:
        """Substring count on chunks_fts, restricted to the regression sample."""
        return self.connection.execute(
            f"SELECT count(*) FROM chunks_fts f JOIN chunks c ON c.chunk_id = f.chunk_id "
            f"WHERE f.{column} GLOB ? AND c.source_id IN ({SAMPLE_PLACEHOLDERS})",
            [f"*{term}*", *REGRESSION_SAMPLE],
        ).fetchone()[0]

    def in_scope(self, rows) -> list:
        """Drops rows outside the regression sample, without filtering inside search().

        Passing --source would exercise a different code path; the point here is to see
        what an unfiltered query returns and then measure it against the frozen numbers.
        Whole-corpus behaviour is a separate concern and is asserted in
        RegistryScopedIndexTests below, against sources.yaml.
        """
        return [row for row in rows if row["source_id"] in REGRESSION_SAMPLE]

    def test_baseline_row_counts_per_source(self):
        # SPEC 3.1: the retrieval fixes must not change how much content is indexed.
        #
        # Asserted per source, not as a sum. A sum of 3176 also holds for, say,
        # 1000/995/1181 — content could shift between sources and the total would not
        # move. The frozen baseline is the three numbers, so the three numbers are what
        # gets checked.
        expected = {
            "chunks": {"fulibei": 1470, "nanjinglu_bian": 525, "tulip_garden": 1181},
            "parents": {"fulibei": 441, "nanjinglu_bian": 80, "tulip_garden": 155},
            "documents": {"fulibei": 110, "nanjinglu_bian": 42, "tulip_garden": 42},
        }
        for table, per_source in expected.items():
            with self.subTest(table=table):
                actual = dict(
                    self.connection.execute(
                        f"SELECT source_id, count(*) FROM {table} "
                        f"WHERE source_id IN ({SAMPLE_PLACEHOLDERS}) GROUP BY 1",
                        REGRESSION_SAMPLE,
                    )
                )
                self.assertEqual(actual, per_source)

    def test_tokenizer_stays_trigram(self):
        value = self.connection.execute(
            "SELECT value FROM metadata WHERE key='fts_tokenizer'"
        ).fetchone()[0]
        self.assertEqual(value, "trigram")

    def test_prose_match_baselines_hold(self):
        # The denominators every SPEC acceptance threshold is expressed against.
        #
        # 龙头 615→616 和 情绪 888→889 是数据清洁提交带来的：clean_text 现在删掉 NUL
        # 字节，原先被 NUL 截断的正文重新可见，所以 prose_matches 用的 GLOB 能看到它们了。
        # 这两条不是阈值放宽，是分母变准——之前那两块的正文里确实写着这个词，只是
        # SQLite 按 C 字符串在 NUL 处停止比较，检索层看不到。
        for term, expected in [("竞价", 394), ("筹码", 367), ("龙头", 616), ("打板", 186), ("情绪", 889)]:
            with self.subTest(term=term):
                self.assertEqual(self.prose_matches(term), expected)

    def test_two_character_terms_are_invisible_to_match(self):
        # Root cause, recorded as a passing fact rather than an acceptance threshold:
        # trigram needs three characters, so MATCH on a two-character term is 0 and
        # stays 0 unless the tokenizer changes. SPEC 2.2 keeps trigram and routes short
        # terms through GLOB instead, so this measurement must NOT be phrased as
        # "must be searchable" — that assertion could never pass.
        for term in ("竞价", "筹码", "龙头", "打板", "情绪"):
            with self.subTest(term=term):
                self.assertEqual(self.scoped_fts_hits(term), 0)

    def test_glob_on_the_fts_table_matches_the_chunks_table(self):
        # Feasibility evidence for SPEC 2.2: GLOB over chunks_fts finds the same rows as
        # a scan of chunks, with no tokenizer change, so two-character terms are reachable
        # while trigram stays.
        #
        # Compared against a live count rather than a literal. The literals used before
        # (395/374/617/194/897) were whole-table figures covering every registered source,
        # so they broke as soon as one more source was imported. The invariant being tested
        # — GLOB finds what a chunks scan finds — holds at any corpus size, so it is stated
        # as an equality between two live counts instead of a frozen pair of numbers.
        for term in ("竞价", "筹码", "龙头", "打板", "情绪"):
            with self.subTest(term=term):
                self.assertEqual(self.scoped_glob_hits(term), self.prose_matches(term))

    def test_glob_covers_title_and_author_columns_too(self):
        # SPEC 2.2 recalls text/title/author. The title column carries a large share of
        # legitimate hits — 310 chunks for 龙头, 405 for 情绪 — so GLOB has to work there
        # as well, otherwise stage 2 would have to drop them.
        for term in ("竞价", "龙头", "情绪"):
            with self.subTest(term=term):
                union = self.scoped_glob_hits(term, "text") + self.scoped_glob_hits(term, "title")
                self.assertGreater(self.scoped_glob_hits(term, "title"), 0)
                self.assertGreaterEqual(union, len(self.recall_target_ids(term)))

    def test_recall_target_exceeds_prose_for_high_traffic_terms(self):
        # Pins the reason the acceptance threshold is the three-field union: measuring
        # against prose alone would require the fix to DISCARD these title-only chunks.
        #
        # 情绪 406→405：数据清洁后有一块的正文不再被 NUL 截断，它从"仅标题含词"变成
        # "正文也含词"，于是标题独有数少 1。并集本身没变（1294），只是归属换了一栏。
        # 龙头的 310 不变——它那一块清洁前后都是正文含词。
        for term, extra in [("竞价", 58), ("筹码", 48), ("龙头", 310), ("情绪", 405)]:
            with self.subTest(term=term):
                self.assertEqual(
                    len(self.recall_target_ids(term)) - self.prose_matches(term), extra
                )

    def test_recall_target_stays_well_below_the_four_field_or(self):
        # Upper bound: topics must stay out of recall. The gap is the noise being excluded
        # — 1163 chunks for 竞价, 832 for 情绪 — so a fix that let topics back in would
        # overshoot the union count by a wide margin and this assertion would catch it.
        for term in ("竞价", "筹码", "龙头", "打板", "情绪"):
            with self.subTest(term=term):
                or4 = self.connection.execute(
                    f"SELECT count(*) FROM chunks WHERE source_id IN ({SAMPLE_PLACEHOLDERS}) "
                    f"AND (text LIKE ? OR title LIKE ? OR author LIKE ? OR topics LIKE ?)",
                    (*REGRESSION_SAMPLE, *[f"%{term}%"] * 4),
                ).fetchone()[0]
                union = len(self.recall_target_ids(term))
                self.assertLess(union, or4)
                self.assertEqual(or4 - union, len(self.label_only_ids(term)))

    def test_large_limit_recall_is_exactly_the_union_with_no_label_leak(self):
        # 阶段 2 前这条叫 test_large_limit_masks_the_recall_defect_but_not_the_precision_one，
        # 记录的是修复前 limit=5000 的两半现象：召回看着够（candidate_limit 变成 150000，
        # 比表还大，兜底把所有命中都返回了），但精度不行——兜底把 topics 也 OR 进去，
        # 只挂了自动标签的块同样返回。实测那时 rank 恒为 999.0。
        #
        # 修复后两半都成立，所以断言从"漏标签块"翻成"一条标签块都不漏"，并把召回从
        # 子集关系收紧成等值：多一条说明 topics 又漏进召回，少一条说明人写字段被砍。
        for term in ("竞价", "筹码", "龙头", "打板", "情绪"):
            with self.subTest(term=term):
                rows = self.in_scope(search(self.connection, term, None, None, 5000))
                got = {row["chunk_id"] for row in rows}
                self.assertTrue(all(row["rank"] != 999.0 for row in rows))
                self.assertEqual(got, self.recall_target_ids(term))
                self.assertFalse(self.label_only_ids(term) & got)

    def test_two_character_terms_are_recalled_without_a_fallback(self):
        # 阶段 2 摘除 expectedFailure。修复前：两字词进不了 MATCH，只能靠 LIKE 兜底
        # （rank 恒为 999.0），而兜底带着候选池截断和来源偏斜（缺陷 B + C）。
        # 修复后：召回等于 text/title/author 三字段并集，且不是兜底给的。
        #
        # 口径是并集而不是正文：真库里 龙头 有 310 块、情绪 有 405 块只在标题里含词，
        # 那是人写的真实标题，属于合法结果。用正文口径会要求实现把它们丢掉。
        # 等值断言同时卡住上界（不得把 topics 放进召回）。
        # 断言挂在 search() 的返回上，不挂在 MATCH 上——修法可以是 GLOB、可以是
        # 换 tokenizer，验收标准都不变（这是 Codex 意见 2 要求的解耦）。
        for term in ("竞价", "筹码", "龙头", "打板", "情绪"):
            with self.subTest(term=term):
                rows = self.in_scope(search(self.connection, term, None, None, 5000))
                self.assertTrue(all(row["rank"] != 999.0 for row in rows))
                self.assertEqual({row["chunk_id"] for row in rows}, self.recall_target_ids(term))

    def test_default_limit_already_returns_prose_matches(self):
        # Measured, and it corrects an earlier note in SPEC.md: on the REAL corpus the
        # top 8 are all genuine prose matches (8/8 for 竞价/龙头/情绪/筹码, 7/7 for 打板).
        # The 0/8 figure belongs to the fixture, which deliberately starves the candidate
        # pool; the real pool holds 79-131 prose matches out of 240, and relevance() ranks
        # them up because text.count contributes up to 8 points.
        #
        # So 缺陷 C on real data is source skew alone, not label-only results. Keeping this
        # as a passing baseline stops a later fix from trading precision for coverage.
        for term in ("竞价", "龙头", "打板", "情绪", "筹码"):
            with self.subTest(term=term):
                rows = self.in_scope(search(self.connection, term, None, None, 8))
                self.assertTrue(rows)
                self.assertTrue(all(term in row["text"] for row in rows))

    def test_default_limit_results_no_longer_come_from_a_fallback(self):
        # 阶段 2 前这条叫 ..._come_from_the_fallback，断言 rank 恒为 999.0，用来交代
        # 上一条那些数字的来路。兜底已删除，999.0 这个哨兵值不再出现在任何路径上。
        #
        # 精度仍然是 relevance() 的属性，只是它现在跑在全量候选上而不是被截断的 240 条
        # 上；权重本身阶段 2 一个都没动（那是阶段 3 的活）。
        for term in ("竞价", "龙头", "打板", "情绪", "筹码"):
            with self.subTest(term=term):
                rows = search(self.connection, term, None, None, 8)
                self.assertTrue(rows)
                self.assertTrue(all(row["rank"] != 999.0 for row in rows))

    def test_topic_labels_do_not_dominate_full_text_search(self):
        # 阶段 1 前：情绪周期 FTS 1823 / 正文 202（**清洁前的历史值**，当前正文是 203），
        # 龙头与核心 1358 / 正文 0，竞价与盘口 1525 / 正文 0。阶段 1 后实测 516 / 0 / 0。
        # 数字取回归样本范围内——全库口径下 情绪周期 修复前是 1850，多算了样本外来源的 27 条。
        for term in ("情绪周期", "龙头与核心", "竞价与盘口"):
            with self.subTest(term=term):
                self.assertLessEqual(
                    self.scoped_fts_hits(term), len(self.recall_target_ids(term))
                )

    def test_phase_one_acceptance_hit_counts(self):
        # SPEC 3.1 阶段 1 验收表，逐项钉死在真库上。收敛口径是三字段并集而不是正文单列：
        # 同节末句要求 title/author 保留在 FTS 中，而 情绪周期 有 313 块只在标题含词，
        # 两条不能同时成立。阶段 2 的表格（正文 203 + 标题作者独有 313 = 并集 516）和本
        # 阶段对应的旧 expectedFailure（断言"不超过并集"）都指向并集口径。
        # （清洁前是 202 + 314，那组是历史值；并集 516 前后一致，只是归属换了一栏。）
        #
        # 断言写成"等于并集"而不是写死 516：并集本身由 test_prose_match_baselines_hold
        # 和 test_recall_target_exceeds_prose_for_high_traffic_terms 钉住，这里要证明的是
        # FTS 命中与并集严格相等——多一条说明 topics 漏进来了，少一条说明人写字段被砍了。
        expected = {"情绪周期": 516, "龙头与核心": 0, "弱转强": 111, "筹码断层": 52}
        for term, count in expected.items():
            with self.subTest(term=term):
                union = len(self.recall_target_ids(term))
                self.assertEqual(union, count)
                self.assertEqual(self.scoped_fts_hits(term), count)

    def test_the_real_fts_table_has_no_topics_column(self):
        # 与 fixture 侧同名断言配对：fixture 走 create_schema() 的内存路径，这里查的是
        # build_index.py 真跑一遍落盘的结果，两者可能不一致（比如库没重建）。
        with self.assertRaises(sqlite3.OperationalError):
            self.connection.execute(
                "SELECT count(*) FROM chunks_fts WHERE topics MATCH ?", ["x"]
            )
        sql = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='chunks_fts'"
        ).fetchone()[0]
        self.assertNotIn("topics", sql)
        for column in ("title", "author", "text"):
            with self.subTest(column=column):
                self.assertIn(column, sql)

    def test_topics_survives_on_the_chunks_table(self):
        # 阶段 1 只动 FTS 的可匹配列，不动数据。topics 仍要有值：relevance() 读它，
        # 检索结果的"主题:"一行也显示它。
        filled = self.connection.execute(
            f"SELECT count(*) FROM chunks WHERE source_id IN ({SAMPLE_PLACEHOLDERS}) "
            f"AND topics != ''",
            REGRESSION_SAMPLE,
        ).fetchone()[0]
        self.assertEqual(filled, 3176)

    def test_integrity_check_passes(self):
        # SPEC 3.1 阶段 1 验收表最后两行之一。重建索引本身会跑一次，但那是构建期的库；
        # 这里查的是落盘后被测试真正读到的这一个文件。
        self.assertEqual(
            self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
        )

    def test_default_limit_rows_all_come_from_the_recall_target(self):
        # 阶段 2 前这条叫 test_current_default_limit_drops_all_but_one_sample_source，
        # 钉的是"样本内四个词都只返回 fulibei"，即缺陷 C 的实测现状。候选池全量化后
        # 实测已变化——『竞价』的前 8 条来自 tulip_garden，『龙头』仍是 fulibei。
        #
        # 替换后的断言不检查来源数量，也不要求任何来源出现：那是配额，SPEC 阶段 3
        # 明确不设，而且这里正是那个陷阱的出处——修复前『打板』不加过滤时会带回一条
        # panfeng，"跨 >= 2 个来源"对它已经通过，而 nanjinglu_bian 和 tulip_garden
        # 两个都不在。来源覆盖在召回层用等值断言证明。
        #
        # 这里只钉小 limit 的可信度：前 8 条必须条条落在三字段并集内，即截断之后也没有
        # 拿标签噪声来填。顺序对不对归阶段 3。
        for term in ("竞价", "龙头", "打板", "情绪"):
            with self.subTest(term=term):
                rows = self.in_scope(search(self.connection, term, None, None, 8))
                self.assertTrue(rows)
                self.assertTrue({row["chunk_id"] for row in rows} <= self.recall_target_ids(term))

    def test_prose_matches_outrank_title_only_matches_on_the_real_corpus(self):
        # SPEC 3.1 阶段 3 验收表第一行的真库版，也是 fixture 侧同名断言的对照。
        # 那一行是**防退化项**：阶段 2 实测前 8 条已经全是正文真含（样本口径 8/8，
        # 『打板』7/7，第 8 条来自样本外的 panfeng），阶段 3 把 topics 权重降下去、
        # 把正文命中提到第一层，都有可能反过来伤到精度，所以必须每次复测。
        #
        # 阶段 3 实测：五个词在样本口径下全部 8/8（『打板』由 7/7 变 8/8——正文优先层
        # 把原先那条样本外的 title 命中挤了下去）。断言写成"全部正文真含"而不是写死
        # 比例，比例会随语料变。
        for term in RECALL_DEFECT_TERMS + ("筹码",):
            with self.subTest(term=term):
                rows = self.in_scope(search(self.connection, term, None, None, 8))
                self.assertEqual(len(rows), 8)
                self.assertTrue(all(term in row["text"] for row in rows))

    def test_repeated_queries_are_identical_on_the_real_corpus(self):
        # SPEC 3.1 阶段 3 验收表第三行：同一查询连续两次结果逐条完全相等。
        # 真库上单独测一遍而不只靠 fixture：内存表复现不了 bm25 的分数分布，而阶段 3
        # 恰好把 bm25 从排序里摘掉了，所以"顺序是否仍然稳定"要在真数据上验。
        for term in ("竞价", "弱转强", "情绪周期", f"竞价 {SECOND_TERM}"):
            with self.subTest(term=term):
                first = [row["chunk_id"] for row in search(self.connection, term, None, None, 8)]
                second = [row["chunk_id"] for row in search(self.connection, term, None, None, 8)]
                self.assertEqual(len(first), 8)
                self.assertEqual(first, second)

    def test_limit_prefix_is_stable_on_the_real_corpus(self):
        # SPEC 3.1 阶段 3 验收表第四行：--limit 8 与 --limit 40 的前 8 条完全相同。
        # 覆盖三条路径：长词走 MATCH、短词走 GLOB、混合查询两条都走。
        for term in ("竞价", "弱转强", "竞价 弱转强"):
            with self.subTest(term=term):
                short = [row["chunk_id"] for row in search(self.connection, term, None, None, 8)]
                long = [row["chunk_id"] for row in search(self.connection, term, None, None, 40)]
                self.assertEqual(len(short), 8)
                self.assertEqual(long[:8], short)

    def test_source_filter_still_narrows_the_ranked_results(self):
        # SPEC 3.1 阶段 3 验收表第五行：--source tulip_garden 仍只返回该来源。
        # 排序层改动不该碰过滤器，但两者都在 search() 里，所以一起复测。
        for term in ("竞价", "筹码断层"):
            with self.subTest(term=term):
                rows = search(self.connection, term, "tulip_garden", None, 8)
                self.assertTrue(rows)
                self.assertEqual(set(sources_of(rows)), {"tulip_garden"})

    def test_ties_are_broken_by_chunk_id_on_the_real_corpus(self):
        # SPEC 2.2 确定性层的真库断言：前三层键相等的块之间按 chunk_id 升序。
        # fixture 侧用反序重建库来证明不依赖 rowid；真库是只读的，改为直接检查输出——
        # 相邻两条只要前三层键相同，chunk_id 就必须递增。
        found_tie = False
        for term in ("竞价", "情绪", "弱转强"):
            rows = search(self.connection, term, None, None, 200)
            for previous, current in zip(rows, rows[1:]):
                previous_key = ranking_key(previous, [term])
                current_key = ranking_key(current, [term])
                if previous_key[:3] == current_key[:3]:
                    found_tie = True
                    with self.subTest(term=term, pair=(previous["chunk_id"], current["chunk_id"])):
                        self.assertLess(previous["chunk_id"], current["chunk_id"])
        self.assertTrue(found_tie, "真库结果里没有并列，这条断言测不到决胜层")

    def sample_holders_of(self, term: str) -> dict[str, int]:
        """Which regression-sample sources hold a three-field match, and how many each."""
        return dict(
            self.connection.execute(
                f"SELECT source_id, count(*) FROM chunks "
                f"WHERE source_id IN ({SAMPLE_PLACEHOLDERS}) "
                f"AND (text LIKE ? OR title LIKE ? OR author LIKE ?) GROUP BY 1",
                (*REGRESSION_SAMPLE, *[f"%{term}%"] * 3),
            )
        )

    def test_the_two_character_recall_terms_still_have_sample_holders(self):
        # Same reasoning as the registry-scoped guard: a vacuity check must live outside the
        # expectedFailure method, because failures inside one are swallowed as expected.
        for term in RECALL_DEFECT_TERMS:
            with self.subTest(term=term):
                self.assertTrue(
                    self.sample_holders_of(term),
                    f"{term} 在回归样本里已无任何命中，RECALL_DEFECT_TERMS 需要换一组词",
                )

    def test_recall_layer_must_reach_every_source_that_holds_matches(self):
        # 阶段 2 摘除 expectedFailure。修复前：兜底候选池按 rowid 截断，郁金香的 245 条
        # 『竞价』正文命中一条都进不来。修复后：大 limit 下每个真的持有命中的来源都出现
        # 在召回里。
        #
        # SPEC 阶段 3 明确不设来源配额，所以这条只查召回层（大 limit），不查前 8 条。
        # 防空转的检查在上面的普通测试里——写在这个方法内部会被当成预期失败吞掉。
        for term in RECALL_DEFECT_TERMS:
            with self.subTest(term=term):
                expected = self.sample_holders_of(term)
                rows = self.in_scope(search(self.connection, term, None, None, 5000))
                got = {row["chunk_id"] for row in rows}
                self.assertTrue(all(row["rank"] != 999.0 for row in rows))
                self.assertEqual(got, self.recall_target_ids(term))
                self.assertEqual(set(sources_of(rows)), set(expected))

    def test_three_character_query_reaches_the_full_recall_target(self):
        # The working baseline on real data: the FTS branch scores the whole table before
        # truncating, so a large limit covers every three-field match. Must not regress.
        for term in ("弱转强", "情绪周期", "筹码断层"):
            with self.subTest(term=term):
                rows = self.in_scope(search(self.connection, term, None, None, 5000))
                self.assertTrue(self.recall_target_ids(term) <= {row["chunk_id"] for row in rows})

    def test_explicit_source_filter_still_works(self):
        # --source must keep narrowing to one source even after the skew is fixed.
        rows = search(self.connection, "竞价", "tulip_garden", None, 8)
        self.assertEqual(sources_of(rows), {"tulip_garden": 8})

    def test_glob_metacharacters_are_escaped_on_the_real_corpus(self):
        # fixture 侧的 GlobEscapingTests 用 11 块的小库证明语义，这里在真库上复核——
        # 转义前的故障数字全部是在这个 48M 的库上测出来的：`*` 召回 3362（= 全表）
        # 而字面命中只有 45 块，`?` 3362 对 614，`竞*` 534 对 0，`[` 0 对 1864。
        #
        # 口径用 instr，与 fixture 侧的 literal_substring_ids 完全一致：instr 是纯子串
        # 查找，没有元字符概念，正是转义之后应该等价于的语义。曾经这里退让成 LIKE，
        # 因为真库有 41 块正文带 NUL 字节，GLOB 和 LIKE 都按 C 字符串在 NUL 处停止
        # 比较、instr 按 blob 看完整内容，于是 instr 口径下多出一条差异。那条差异是
        # 真实的召回遗漏（NUL 后的正文永远搜不到），LIKE 只是把它一起藏了起来。NUL
        # 现在由 clean_text 在导入时删除，两个口径重新一致，所以基准回到 instr。
        total = self.connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
        for term in ("*", "?", "[", "**", "竞*", "A?"):
            with self.subTest(term=term):
                expected = {
                    row[0]
                    for row in self.connection.execute(
                        "SELECT chunk_id FROM chunks "
                        "WHERE instr(text, ?) > 0 OR instr(title, ?) > 0 OR instr(author, ?) > 0",
                        [term] * 3,
                    )
                }
                got = {
                    row["chunk_id"] for row in search(self.connection, term, None, None, total + 1)
                }
                self.assertEqual(got, expected)
                self.assertLess(len(got), total, f"{term!r} 返回了全表，通配符仍在生效")

    def test_ascii_case_forms_recall_the_same_chunks_on_the_real_corpus(self):
        # fixture 侧的 AsciiCaseRecallTests 用 12 块的小库证明语义，这里在真库上复核——
        # 修复前的数字全部是在这个 48M 的库上测出来的：AI 召回 144、ai 83、Ai 3、aI 0，
        # 而 text/title/author 三列不区分大小写的并集是 218，四者分别丢 74/135/215/218 条。
        #
        # 口径用 lower() + instr：折叠范围限定 ASCII，而 SQLite 的 lower() 恰好只折
        # ASCII，两边定义一致。用 Python 侧折叠当基准会把全角字母也算进去，基准本身
        # 就比实现"更宽"，等值断言测的就不是同一件事了。
        expected = {
            row[0]
            for row in self.connection.execute(
                "SELECT chunk_id FROM chunks WHERE instr(lower(text), 'ai') > 0 "
                "OR instr(lower(title), 'ai') > 0 OR instr(lower(author), 'ai') > 0"
            )
        }
        self.assertTrue(expected, "真库里没有含 ai 的块，这条断言会空转")
        recalls = {}
        for form in ("AI", "ai", "Ai", "aI"):
            with self.subTest(form=form):
                got = {
                    row["chunk_id"] for row in search(self.connection, form, None, None, 100000)
                }
                recalls[form] = got
                self.assertEqual(got, expected)
        self.assertEqual(len({frozenset(ids) for ids in recalls.values()}), 1)

    def test_case_folding_does_not_pull_topics_into_recall_on_the_real_corpus(self):
        # 折叠不得顺带把 topics 拉回召回。真库里 topics 是 infer_topics() 自动打的标签，
        # 阶段 1 已把它移出 FTS 可匹配列；这里按结果反查：仅 topics 含词的块必须一条
        # 都不在召回里。实测这个差集当前为 0 块（taxonomy 里没有含 ai 的标签），所以
        # 断言写成"召回与 topics-only 集合不相交"而不是"差集非空"——后者会因内容变化
        # 误报，而不相交在任何内容下都必须成立。
        topics_only = {
            row[0]
            for row in self.connection.execute(
                "SELECT chunk_id FROM chunks WHERE instr(lower(topics), 'ai') > 0 "
                "AND instr(lower(text), 'ai') = 0 AND instr(lower(title), 'ai') = 0 "
                "AND instr(lower(author), 'ai') = 0"
            )
        }
        for form in ("AI", "ai"):
            with self.subTest(form=form):
                got = {
                    row["chunk_id"] for row in search(self.connection, form, None, None, 100000)
                }
                self.assertFalse(got & topics_only)

    def test_long_terms_stay_case_insensitive_on_the_real_corpus(self):
        # 对照组，说明短词以前的行为是回归而不是设计：三字以上走 MATCH，本来就不区分
        # 大小写。实测 AI硬件 与 ai硬件 同为 8 块，修复前后都是。
        self.assertEqual(
            {row["chunk_id"] for row in search(self.connection, "AI硬件", None, None, 100000)},
            {row["chunk_id"] for row in search(self.connection, "ai硬件", None, None, 100000)},
        )

    def test_the_real_index_carries_no_nul_bytes(self):
        """NUL 硬断言：一个 NUL 会让它后面的正文对 GLOB 和 LIKE 永久不可见。

        两者都按 C 字符串语义比较，遇到第一个 NUL 就停止，所以这不是排序或权重能
        补救的问题——带 NUL 的块在检索层根本不存在。实测 nanjinglu_bian 曾有 41 块
        正文带 NUL，其中 nanjinglu-92154afd0e2c-p008-c05 的 NUL 在第 5 个字符、
        "龙头"在第 42 个字符，这块因此搜不到，龙头的真实召回少了 1 条。
        """
        for table, columns in (
            ("chunks", ("text", "title", "author", "topics")),
            ("parents", ("text", "title", "author")),
            ("chunks_fts", ("text", "title", "author")),
        ):
            for column in columns:
                with self.subTest(table=table, column=column):
                    count = self.connection.execute(
                        f"SELECT count(*) FROM {table} WHERE instr({column}, char(0)) > 0"
                    ).fetchone()[0]
                    self.assertEqual(count, 0, f"{table}.{column} 有 {count} 行含 NUL")

    def test_glob_and_instr_agree_on_the_whole_corpus(self):
        # NUL 是 GLOB/LIKE 与 instr 唯一的分歧来源，所以两个口径在验收词上逐一相等，
        # 等价于"没有任何一块的正文被 NUL 截断"。上一条按列查 NUL，这条按检索结果
        # 查后果：即便将来有新的 C 字符串陷阱，这里也会先失败。
        for term in ("竞价", "筹码", "龙头", "打板", "情绪", "弱转强", "情绪周期", "筹码断层"):
            with self.subTest(term=term):
                by_instr = {
                    row[0]
                    for row in self.connection.execute(
                        "SELECT chunk_id FROM chunks "
                        "WHERE instr(text, ?) > 0 OR instr(title, ?) > 0 OR instr(author, ?) > 0",
                        [term] * 3,
                    )
                }
                by_like = {
                    row[0]
                    for row in self.connection.execute(
                        "SELECT chunk_id FROM chunks "
                        "WHERE text LIKE ? OR title LIKE ? OR author LIKE ?",
                        [f"%{term}%"] * 3,
                    )
                }
                self.assertTrue(by_instr)
                self.assertEqual(by_instr, by_like)


CJK_RUN = re.compile(r"[一-鿿]+")
SMOKE_MIN_HITS = 3
SMOKE_SAMPLE_CHUNKS = 40

# 会进索引的结构化产物。build_index.py 读的就是这几个文件，所以 NUL 检查的范围以它们
# 为准，而不是"source_libraries 下所有 JSON"。methods/conflicts 只有部分来源有，按存在
# 与否跳过，不要求每个来源都齐。
#
# 刻意不含 page_texts/*.json（PDF 文本层与 OCR 的原始提取缓存）和 image_ocr_cache：
# 那些是导入器的输入，读出来之后还要过 clean_text() 才落进产物。也不含 messages.jsonl
# ——它是 panfeng 的中间产物（导入器自己的解析记录），chunks/parents 才是索引读的。
INDEXABLE_JSONL = (
    "documents.jsonl",
    "parents.jsonl",
    "chunks.jsonl",
    "methods.jsonl",
    "conflicts.jsonl",
)


def json_string_values(value):
    """递归产出一个 JSON 值里所有的字符串（含 dict 的键）。

    必须递归：NUL 可能藏在嵌套结构里（``merged_locators`` 是列表、``provenance`` 是
    嵌套 dict），只看顶层字段会漏。键也要看——键名理论上也能带 NUL，代价是零。
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, nested in value.items():
            if isinstance(key, str):
                yield key
            yield from json_string_values(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from json_string_values(nested)


def json_nul_count(value) -> int:
    """一个 JSON 值里所有字符串（含嵌套、含键）的 NUL 总数。"""
    return sum(text.count("\x00") for text in json_string_values(value))


def jsonl_nul_offenders(path: Path) -> list[str]:
    """逐行解析 JSONL，返回 ``第N行 M 个`` 形式的问题清单，干净则是空列表。

    真库检查和故障注入测试**共用这一个函数**，故意的。两边各写一份检测逻辑的话，
    真库那份退化成按原始字节 grep 也照样绿——注入测试用自己那份正确逻辑通过了，
    真库那份的缺陷无人看守。共用之后，退化会同时让两条失败。

    为什么必须解析 JSON：JSON 把 NUL 序列化成六个 ASCII 字符 ``\\u0000``，
    原始字节里根本没有 0x00。见 test_the_nul_check_actually_detects_an_escaped_nul。
    """
    offenders: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        count = json_nul_count(json.loads(line))
        if count:
            offenders.append(f"第{number}行 {count} 个")
    return offenders


def json_file_nul_count(path: Path) -> int:
    """整份 JSON 文件（非 JSONL）里的 NUL 总数。page_texts 缓存是这个形状。"""
    return json_nul_count(json.loads(path.read_text(encoding="utf-8")))


def text_file_nul_count(path: Path) -> int:
    """纯文本文件解码后的 NUL 数。

    ``errors="surrogateescape"`` 是为了让非法字节也能读进来而不抛异常——目的是查 NUL，
    不是校验编码，读不进去就查不到才是真问题。
    """
    return path.read_text(encoding="utf-8", errors="surrogateescape").count("\x00")


def frequent_trigrams(texts, limit: int = 200) -> list[str]:
    """CJK trigrams ranked by how many of ``texts`` contain them.

    Three characters because that is the shortest string the trigram tokenizer can build a
    gram from (see fts_hits), so a smoke query must be at least that long to exercise the
    FTS path at all. Ranked by document frequency rather than raw count so one long
    repetitive chunk cannot dominate the pick, and tie-broken alphabetically so the chosen
    query is the same on every run — a smoke test that queries something different each
    time turns a real regression into an intermittent one.
    """
    frequency: dict[str, int] = {}
    for text in texts:
        present: set[str] = set()
        for run in CJK_RUN.findall(text or ""):
            for start in range(len(run) - 2):
                present.add(run[start : start + 3])
        for gram in present:
            frequency[gram] = frequency.get(gram, 0) + 1
    ranked = sorted(frequency.items(), key=lambda item: (-item[1], item[0]))
    return [gram for gram, _ in ranked[:limit]]


@unittest.skipUnless(
    DATABASE.exists(),
    f"索引不存在，先跑 build_index.py：{DATABASE}",
)
class RegistryScopedIndexTests(unittest.TestCase):
    """Whole-corpus behaviour, scoped by sources.yaml rather than by a fixed tuple.

    RealIndexTests above measures the frozen regression sample, which by design ignores
    everything imported after it was frozen. That is the wrong scope for acceptance: a
    query the user types is not restricted to three sources. So every "the retrieval layer
    must reach it" assertion lives here, and reads its scope from registered_sources().

    Nothing in this class names a source id except the panfeng import pin, and no assertion
    names a search term: every term is either declared by the source in sources.yaml or
    derived from that source's own text. Registering a fifth source therefore widens the
    coverage automatically, whatever vocabulary it happens to use.
    """

    @classmethod
    def setUpClass(cls):
        cls.connection = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)
        cls.connection.row_factory = sqlite3.Row
        cls.registry = source_registry()
        cls.registered = tuple(sorted(cls.registry))

    @classmethod
    def tearDownClass(cls):
        cls.connection.close()

    def hits_in(self, source: str, term: str, author: str | None = None) -> int:
        sql = (
            "SELECT count(*) FROM chunks WHERE source_id = ? "
            "AND (text LIKE ? OR title LIKE ? OR author LIKE ?)"
        )
        params = [source, *[f"%{term}%"] * 3]
        if author is not None:
            sql += " AND author LIKE ?"
            params.append(f"%{author}%")
        return self.connection.execute(sql, params).fetchone()[0]

    def smoke_query_for(self, source: str, author: str | None = None) -> str:
        """A term demonstrably held by this source — or by this author within it.

        No fixed vocabulary list. An earlier revision tried
        ``("情绪", "龙头", "仓位", "半路", "复盘", "打板")`` in order, which happens to
        cover the four sources registered today and says nothing about a fifth: a source
        about options, bonds or macro could hold none of them and the test would report a
        broken import where there is none.

        Two ways to get one, in order:

        1. Declared in the source's sources.yaml entry — ``smoke_query:`` for the source
           scope, ``author_smoke_query:`` for the author scope. Two separate keys on
           purpose: the source-level term is picked to be representative of the whole
           source, so it may well sit in chunks written by somebody else. Reusing it under
           ``--author`` would then return nothing and fail on correct data. Whichever scope
           is asked for reads only its own key and never falls back to the other.
        2. Derived from the chunks that are actually in scope — this source's, or this
           author's within it — then verified to hold at least SMOKE_MIN_HITS rows. Because
           it is measured against the very rows it was derived from, this cannot fail for
           vocabulary reasons; only near-absent CJK prose in scope makes it fail, which is a
           genuine import problem.
        """
        key = "author_smoke_query" if author is not None else "smoke_query"
        declared = (self.registry.get(source) or {}).get(key, "").strip()
        if declared:
            self.assertGreaterEqual(
                self.hits_in(source, declared, author),
                1,
                f"sources.yaml 为 {source} 声明的 {key} {declared!r} 在索引里没有命中"
                + (f"（作者过滤={author!r}）" if author is not None else ""),
            )
            return declared

        sql = "SELECT text FROM chunks WHERE source_id = ?"
        params: list[object] = [source]
        if author is not None:
            sql += " AND author LIKE ?"
            params.append(f"%{author}%")
        # chunk_id as the secondary key: length alone leaves ties, and a tie broken by
        # rowid is only stable within one build. A rebuild that reorders equal-length
        # chunks would silently change which term the smoke test queries.
        sql += " ORDER BY length(text) DESC, chunk_id LIMIT ?"
        params.append(SMOKE_SAMPLE_CHUNKS)
        texts = [row[0] for row in self.connection.execute(sql, params)]
        for gram in frequent_trigrams(texts):
            if self.hits_in(source, gram, author) >= SMOKE_MIN_HITS:
                return gram
        self.fail(
            f"来源 {source} 的正文里找不到任何出现 >= {SMOKE_MIN_HITS} 次的三字片段"
            f"（作者过滤={author!r}），导入可能有问题；"
            f"确定内容如此，就在 sources.yaml 里给它写 {key}"
        )

    def test_registered_sources_match_what_is_indexed(self):
        # Both directions matter. A source in sources.yaml with nothing in the index means
        # build_index.py never picked it up; a source in the index that is not registered
        # means content arrived without going through the approval step CLAUDE.md requires
        # — which is exactly how panfeng first entered the database unnoticed, because
        # build_index.py:137 scans source_libraries/ without consulting this config.
        indexed = tuple(
            row[0]
            for row in self.connection.execute(
                "SELECT DISTINCT source_id FROM chunks ORDER BY 1"
            )
        )
        self.assertEqual(indexed, self.registered)

    def test_every_registered_source_has_rows_in_all_three_tables(self):
        for table in ("documents", "parents", "chunks"):
            counts = dict(
                self.connection.execute(f"SELECT source_id, count(*) FROM {table} GROUP BY 1")
            )
            for source in self.registered:
                with self.subTest(table=table, source=source):
                    self.assertGreater(counts.get(source, 0), 0)

    def test_panfeng_import_row_counts(self):
        # An import-integrity pin for the source added on 2026-08-02, measured after the
        # rename from feishu_panfeng_chat: 29 trading days, 60 parents, 186 chunks out of
        # 3445 chat messages. The originals are read-only, so these cannot drift unless a
        # rebuild loses data — which is the failure this catches.
        #
        # Named explicitly on purpose, and a fifth source does NOT need a line here: the
        # message-level counts of each source belong to its own importer test (for panfeng,
        # test_import_feishu_chat.py). Coverage assertions are the dynamic ones above.
        actual = {
            table: self.connection.execute(
                f"SELECT count(*) FROM {table} WHERE source_id = 'panfeng'"
            ).fetchone()[0]
            for table in ("documents", "parents", "chunks")
        }
        self.assertEqual(actual, {"documents": 29, "parents": 60, "chunks": 186})

    def test_every_registered_source_can_be_queried_on_its_own(self):
        # --source is how a peer source stays usable while 缺陷 C is still open: even when
        # the unfiltered top 8 is dominated by one source, an explicit filter must return
        # that source's own material and nothing else.
        for source in self.registered:
            query = self.smoke_query_for(source)
            with self.subTest(source=source, query=query):
                rows = search(self.connection, query, source, None, 5)
                self.assertTrue(rows)
                self.assertEqual(set(sources_of(rows)), {source})

    def test_sources_that_carry_authors_are_reachable_by_author(self):
        # The --author path, used by the cross-source report to attribute a view to a person.
        #
        # Scoped to sources that actually carry authorship, not to every registered source.
        # Authorship is a property of the material: a source could be anonymous posts, an
        # unattributed course handout or a data export with no byline, and demanding an
        # author for it would fail on correct data. A source is covered when either
        #   - its chunks carry a non-empty author, or
        #   - its sources.yaml entry declares ``author:``,
        # and a source that declares one while carrying none is a real inconsistency, so
        # that combination fails instead of being skipped.
        #
        # The author column is not a source label either. Measured 2026-08-02: fulibei is an
        # anthology whose authors are the original posters (41 distinct values, the largest
        # being 未从文件名明确识别 at 618 chunks), and panfeng carries the chat participants
        # (我有上将潘凤 175, 5280、我有上将潘凤 5, ...). So the author is read from the data
        # per source, and the query is derived from that author's *own* chunks, not reused
        # from the source-level smoke query: the source-level pick is representative of the
        # whole source and may live in another author's material, and 未从文件名明确识别
        # holds only 618 of fulibei's 1470 chunks. Reusing it would fail on correct data.
        # A related earlier bug: taking the first author row rather than the largest drew
        # 5280、我有上将潘凤 (5 chunks) and then failed on an empty result that was right.
        covered = []
        for source in self.registered:
            declared = (self.registry.get(source) or {}).get("author", "").strip()
            if declared:
                # The declared name itself must be present, not merely *some* author. An
                # earlier revision only checked that the source had a non-empty author
                # column somewhere, so a typo'd or renamed declaration passed silently as
                # long as any other name was there — exactly the drift this should catch.
                # Substring rather than equality because a chunk with several participants
                # stores them joined (measured: 我有上将潘凤 exact 175, LIKE 183 —— the extra
                # 8 are 5280、我有上将潘凤 and 我有上将潘凤、5280), and the declared author
                # genuinely did speak in those.
                self.assertGreater(
                    self.connection.execute(
                        "SELECT count(*) FROM chunks WHERE source_id = ? AND author LIKE ?",
                        (source, f"%{declared}%"),
                    ).fetchone()[0],
                    0,
                    f"sources.yaml 给 {source} 声明了 author: {declared}，"
                    f"但它的 chunks 里没有任何块的 author 含这个名字",
                )
            author = self.connection.execute(
                "SELECT author FROM chunks WHERE source_id = ? AND author != '' "
                "GROUP BY 1 ORDER BY count(*) DESC, author LIMIT 1",
                (source,),
            ).fetchone()
            if author is None:
                # Nothing declared and nothing in the data: this source is simply out of
                # scope for the author path. (The declared-but-absent case already failed
                # above, so reaching here with a declaration is impossible.)
                continue
            name = author[0]
            query = self.smoke_query_for(source, author=name)
            covered.append(source)
            with self.subTest(source=source, author=name, query=query):
                holders = {
                    row[0]
                    for row in self.connection.execute(
                        "SELECT DISTINCT source_id FROM chunks "
                        "WHERE author LIKE ? OR title LIKE ?",
                        (f"%{name}%", f"%{name}%"),
                    )
                }
                rows = search(self.connection, query, None, name, 20)
                self.assertTrue(rows)
                self.assertTrue(all(name in row["author"] or name in row["title"] for row in rows))
                # Subset, not equality against {source}: a name that appears in more than one
                # source is legitimate (that is what the cross-source report is for), so the
                # invariant is only that --author never leaks a source lacking that name.
                self.assertTrue(set(sources_of(rows)) <= holders)
        # Without this the test would pass vacuously if authorship vanished everywhere,
        # which is the failure mode narrowing the scope introduces.
        self.assertTrue(covered, "没有任何来源带作者数据，--author 这条路完全没被测到")

    def test_three_character_recall_already_covers_every_source_holding_matches(self):
        # Today's working case, kept as a regression guard: the FTS branch scores the whole
        # table before truncating, so for a three-character term every source that holds a
        # match already comes back. Measured 2026-08-02 — 弱转强 returns 43/44/24 with no
        # panfeng row because panfeng genuinely has none, and 筹码断层 returns tulip_garden
        # only. So the expectation is per-term "every source that holds matches", never
        # "every registered source", which no single term could satisfy.
        for term in ("弱转强", "情绪周期", "筹码断层"):
            with self.subTest(term=term):
                expected = dict(
                    self.connection.execute(
                        "SELECT source_id, count(*) FROM chunks "
                        "WHERE (text LIKE ? OR title LIKE ? OR author LIKE ?) GROUP BY 1",
                        [f"%{term}%"] * 3,
                    )
                )
                rows = search(self.connection, term, None, None, 5000)
                self.assertTrue(recall_target_ids(self.connection, term) <= {r["chunk_id"] for r in rows})
                self.assertTrue(set(expected) <= set(sources_of(rows)))

    def holders_of(self, term: str) -> dict[str, int]:
        """Which sources hold a three-field match for ``term``, and how many each."""
        return dict(
            self.connection.execute(
                "SELECT source_id, count(*) FROM chunks "
                "WHERE (text LIKE ? OR title LIKE ? OR author LIKE ?) GROUP BY 1",
                [f"%{term}%"] * 3,
            )
        )

    def test_the_two_character_recall_terms_still_have_holders(self):
        # A plain test, deliberately NOT part of the expectedFailure below, even though it
        # exists to protect it. Inside an expectedFailure method any assertion failure is
        # swallowed as "expected", so a guard placed there reports nothing when it trips:
        # if these four terms ever fell to zero hits, both sides of that method's equality
        # assertions would be empty, it would pass, and unittest would report the flip as
        # "unexpected success" — a red build blamed on a fix that never happened. Out here
        # the same condition fails loudly and names the real cause.
        for term in RECALL_DEFECT_TERMS:
            with self.subTest(term=term):
                self.assertTrue(
                    self.holders_of(term),
                    f"{term} 在全库已无任何命中，"
                    f"RECALL_DEFECT_TERMS 需要换一组词，否则召回缺陷的断言会空转",
                )

    def test_recall_layer_must_reach_every_registered_source_that_holds_matches(self):
        # 阶段 2 摘除 expectedFailure。修复前（2026-08-02 实测，全库口径）：两字词全部走
        # LIKE 兜底，rank 恒为 999.0，召回集合和 text/title/author 并集不一致——竞价
        # 目标 453 条，兜底返回 1620 条，多出来的是 topics 标签命中；同时候选池按 rowid
        # 截断，各来源比例也不对。
        # 修复后：召回等于三字段并集，且每个真的持有命中的来源都出现，不靠兜底。
        #
        # 范围取 sources.yaml 全部登记来源，不是三来源回归样本：验收面向用户真实查询。
        # 但覆盖面逐词按"该词在哪些来源有命中"动态计算，绝不要求全部登记来源都出现。
        #
        # 这里曾经写着 assertEqual(set(expected), set(self.registered))，意思是"这四个词
        # 必须每个来源都有"。那是把当前语料的巧合当成了契约：潘凤只有 186 块口语记录，
        # 弱转强 一条都没有；任何一个来源的选材偏好变一变，或者新来源讲的是别的品种，
        # 这条断言就会失败——而检索层其实完全正确。命中面是内容属性，不是检索层的责任。
        #
        # 防空转的检查在上面那个普通测试里，不放这个方法内部：这里的失败会被当成
        # "预期失败"吞掉，守卫写在这儿等于没写。
        for term in RECALL_DEFECT_TERMS:
            with self.subTest(term=term):
                expected = self.holders_of(term)
                rows = search(self.connection, term, None, None, 5000)
                self.assertTrue(all(row["rank"] != 999.0 for row in rows))
                self.assertEqual(
                    {row["chunk_id"] for row in rows}, recall_target_ids(self.connection, term)
                )
                self.assertEqual(set(sources_of(rows)), set(expected))

    def test_the_indexable_products_carry_no_nul_bytes(self):
        """可索引产物的 NUL 硬断言，按文件查，不只按数据库查。

        为什么不能只查数据库：数据库是**当前一次**构建的产物，而 JSONL 是构建的输入。
        产物里混进 NUL 的话，重建索引会把它带进库，而此刻数据库是干净的——只查库的
        断言在那个时间窗里通不报。这条按输入查，所以它在重建之前就会失败。

        为什么必须解析 JSON 而不是扫原始字节：JSON 把 NUL 序列化成六个 ASCII 字符
        ``\\u0000``，原始字节里根本没有 0x00。实测 page_texts 缓存就是这样——312 个
        缓存文件原始字节 NUL 计数全为 0，而其中 41 个解码后共有 651 个（2026-08-02）。
        按字节 grep 会报"全部干净"，那正是这条断言存在的理由。检测逻辑走共用的
        ``jsonl_nul_offenders()``，故障注入测试调的是同一个函数，退化会同时暴露。

        **page_texts 明确排除在外**，因为它是 PDF 文本层/OCR 的原始提取缓存，不是
        可索引产物：``import_nanjinglu.py`` 读缓存**之后**才过 ``clean_text()``
        （见 clean_text 落点的说明），NUL 在那一步被删掉，所以缓存里留着转义后的 NUL
        既无害也不需要重新 OCR。当前实测：page_texts 41 个文件、651 个 NUL，来自 4 个
        文档（nanjinglu-554254de485c / 92154afd0e2c / 92f3ed9047ee / c0bf0cd2860c）。
        这个豁免是按目录名精确排除的，不是按"某来源整体豁免"——同一个来源下的
        chunks.jsonl 照查。

        覆盖面从 source_libraries 的目录动态取，接入新来源自动纳入，不用改这里。
        """
        library = DATABASE.parents[1] / "source_libraries"
        self.assertTrue(library.is_dir(), f"source_libraries 不存在：{library}")
        sources = sorted(path for path in library.iterdir() if path.is_dir())
        self.assertTrue(sources, "source_libraries 下一个来源目录都没有，断言会空转")

        checked_files = 0
        for source in sources:
            # 1. texts/ 下的文本：纯文本文件，按解码后的字符查。
            texts_dir = source / "texts"
            if texts_dir.is_dir():
                for path in sorted(texts_dir.rglob("*")):
                    if not path.is_file():
                        continue
                    checked_files += 1
                    with self.subTest(source=source.name, file=path.name):
                        count = text_file_nul_count(path)
                        self.assertEqual(count, 0, f"{path} 有 {count} 个 NUL")

            # 2. 结构化产物：递归检查每个 JSON 字符串值。methods/conflicts 只有部分来源有。
            #    走共用的 jsonl_nul_offenders()，与故障注入测试是同一份检测逻辑。
            for name in INDEXABLE_JSONL:
                path = source / name
                if not path.exists():
                    continue
                checked_files += 1
                with self.subTest(source=source.name, file=name):
                    offenders = jsonl_nul_offenders(path)
                    self.assertEqual(offenders, [], f"{path} 含 NUL：{offenders[:5]}")

        self.assertGreater(checked_files, 0, "一个文件都没查到，断言在空转")

    def test_the_nul_check_actually_detects_an_escaped_nul(self):
        """守住上一条的检测能力：造一个转义 NUL 的 JSONL，必须被抓出来。

        不是多余的自测。上一条的关键在于"解析 JSON 之后再查"，而一个改成按原始字节
        grep 的实现会在真库上照样通过（page_texts 就是活证据：原始字节 0、解码后 651）。

        关键是这条调用的是**上一条真正用的那个函数** ``jsonl_nul_offenders()``，
        不是在这里另写一份检测。本轮修订之前这里复制了一份逻辑，于是真库那份退化成
        字节扫描时，这条仍然会绿——注入测试只证明了自己那份复制品是对的。现在共用，
        真库那份一退化，这条立刻失败。

        除了调共用函数，还独立钉住"字节看不见、解码看得见"这个前提本身：
        原始字节 0 个 0x00，但含 ``\\u0000`` 字面，解码后 3 个（``text`` 里 1 个、
        嵌套列表里 1 个、键名里 1 个——键也要覆盖）。
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chunks.jsonl"
            payload = {"text": "a\x00b", "nested": {"t": ["c\x00"]}, "k\x00ey": "干净"}
            path.write_bytes(
                (json.dumps(payload, ensure_ascii=False) + "\n{}\n").encode("utf-8")
            )

            raw = path.read_bytes()
            self.assertEqual(raw.count(b"\x00"), 0, "JSON 应把 NUL 转义，原始字节里没有 0x00")
            self.assertIn(b"\\u0000", raw, "转义后的 NUL 应以六个 ASCII 字符出现")

            # 真库检查用的同一个函数：命中第 1 行 3 个，第 2 行的 {} 不报。
            self.assertEqual(jsonl_nul_offenders(path), ["第1行 3 个"])

            # 干净文件必须返回空列表，否则这条只证明了"总是报错"。
            clean = Path(directory) / "clean.jsonl"
            clean.write_bytes((json.dumps({"text": "干净"}, ensure_ascii=False) + "\n").encode())
            self.assertEqual(jsonl_nul_offenders(clean), [])

    def test_the_page_texts_cache_is_excluded_on_purpose(self):
        """豁免范围本身也要有断言，否则"排除了什么"只活在注释里。

        两头都钉：page_texts 里当前**确实**还有转义 NUL（所以豁免不是空话，去掉豁免
        上一条就会失败），而同一个来源的可索引 JSONL **必须**干净（所以豁免没有顺带
        放过整个来源）。实测 2026-08-02：page_texts 651 个 NUL / 41 个文件 / 4 个文档，
        nanjinglu_bian 的 chunks/parents/documents 三个 JSONL 为 0。

        缓存里的 NUL 数量不写死：那是 OCR 输出的属性，重跑 OCR 会变。断言只要求
        "仍然存在"，一旦将来清零，这条会失败并提示可以撤掉豁免——那时豁免才是死代码。

        一个 page_texts 目录都找不到时**明确失败，不 skipTest**。本轮修订改的：
        skip 与"验收零 skip"的口径冲突，而且"跳过"这个结果本身没有信息——豁免究竟
        还需不需要，读输出的人看不出来。没有缓存目录意味着 INDEXABLE_JSONL 那条排除
        规则和它的注释成了无人核验的声明，该删，所以失败并把话说清楚是对的处理。
        """
        library = DATABASE.parents[1] / "source_libraries"
        caches = sorted(library.glob("*/page_texts"))
        self.assertTrue(
            caches,
            f"{library} 下没有任何 page_texts 缓存目录。豁免已成死代码："
            "把 INDEXABLE_JSONL 注释里的 page_texts 排除说明和这条测试一起删掉",
        )
        total = 0
        for cache in caches:
            for path in sorted(cache.rglob("*.json")):
                total += json_file_nul_count(path)
            # 同一个来源的可索引产物必须干净：豁免只针对 page_texts 这个目录。
            for name in INDEXABLE_JSONL:
                path = cache.parent / name
                if not path.exists():
                    continue
                with self.subTest(source=cache.parent.name, file=name):
                    self.assertEqual(
                        jsonl_nul_offenders(path),
                        [],
                        f"{path} 含 NUL——豁免只针对 page_texts，不放过同来源的产物",
                    )
        self.assertGreater(
            total, 0, "page_texts 已经没有 NUL 了，可以撤掉豁免并把它一起纳入检查"
        )


K_NAME = re.compile(r"-k[\s,\"']+([A-Za-z_][A-Za-z0-9_]*)")


def assignment_k_names(text: str, variable: str) -> set[str] | None:
    """抽出 ``variable=`` 这一个赋值块里的 ``-k`` 类名，找不到该赋值则返回 None。

    用途见 SubsetMarkerTests.test_the_documented_commands_list_every_subset_class：
    必须按赋值块隔离，把整篇文档的 ``-k`` 汇成一个集合会让一条命令的遗漏被另一条掩盖。

    赋值起点认 ``FIXTURE=``（bash）和 ``$FIXTURE =``（PowerShell）两种写法，行首起、
    允许缩进。终点是"空行"或"下一行以另一个 ``名字=`` 开头"——bash 的行尾续行 ``\\``
    和 PowerShell 的括号换行都不产生空行，所以多行赋值能完整取到，而下一条赋值
    （``REAL=``）不会被吞进来。

    返回 set 而不是 list：``-k`` 的语义是 OR，同一个类名写两遍与写一遍等价，顺序也不
    影响收集到哪些用例。是否重复不是本守卫要管的事。
    """
    start = re.search(rf"^[ \t]*\$?{re.escape(variable)}[ \t]*=", text, re.MULTILINE)
    if start is None:
        return None
    body: list[str] = []
    for line in text[start.end():].splitlines():
        if body and (not line.strip() or re.match(r"^[ \t]*\$?[A-Za-z_][A-Za-z0-9_]*[ \t]*=", line)):
            break
        body.append(line)
    return set(K_NAME.findall("\n".join(body)))


# The -k names SPEC 3.2 uses to split the suite. Kept next to the check that enforces
# them so the two never drift apart.
FIXTURE_TEST_CLASSES = (
    "FixtureShapeTests",
    "IndexPollutionTests",
    "ShortTermSearchTests",
    "SourceCoverageTests",
    "RetrievalContractTests",
    "GlobEscapingTests",
    "AsciiCaseRecallTests",
    "SubsetMarkerTests",
    "SourceRegistryTests",
)
REAL_INDEX_TEST_CLASSES = (
    "RealIndexRequirementTests",
    "RealIndexTests",
    "RegistryScopedIndexTests",
)


class SubsetMarkerTests(unittest.TestCase):
    """Guards the two -k lists in the module docstring and SPEC 3.2.

    Splitting the suite by -k is only safe while the lists are exhaustive. A class added
    later and left out of both would silently run in neither subset, and the split
    command would report OK while skipping it.
    """

    def test_the_subset_markers_cover_every_test_class(self):
        defined = {
            name
            for name, value in globals().items()
            if isinstance(value, type)
            and issubclass(value, unittest.TestCase)
            and value is not unittest.TestCase
        }
        listed = set(FIXTURE_TEST_CLASSES) | set(REAL_INDEX_TEST_CLASSES)
        self.assertEqual(
            defined - listed,
            set(),
            "新增的测试类没有列进 FIXTURE_TEST_CLASSES 或 REAL_INDEX_TEST_CLASSES，"
            "SPEC 3.2 的分开跑命令会漏掉它",
        )
        self.assertEqual(listed - defined, set(), "列表里有已删除的类名")

    def test_the_two_subsets_do_not_overlap(self):
        self.assertEqual(set(FIXTURE_TEST_CLASSES) & set(REAL_INDEX_TEST_CLASSES), set())

    def test_no_class_name_is_a_substring_of_another(self):
        # ``-k X`` matches by substring, not by exact name, so disjoint lists are not
        # enough: a real-index class whose name contains a fixture class name gets pulled
        # into step 1 of SPEC 3.2 and reads the pre-rebuild index.
        #
        # Measured, not hypothetical. RegistryScopedIndexTests was first called
        # RegisteredSourceCoverageTests, which contains SourceCoverageTests, and the
        # fixture subset silently collected 55 cases where 48 were intended — the 7 extra
        # being real-index ones. Renaming fixed it; this assertion is what would have
        # caught it.
        names = list(FIXTURE_TEST_CLASSES) + list(REAL_INDEX_TEST_CLASSES)
        for name in names:
            for other in names:
                if name != other and name in other:
                    self.fail(f"类名 {name} 是 {other} 的子串，-k {name} 会把两个都收进来")

    def test_the_documented_commands_list_every_subset_class(self):
        """每一条文档命令的 `-k` 清单**各自**必须等于对应常量。

        这是本轮修订的守卫。第一版把整个 SPEC.md 里所有 ``-k`` 汇成一个集合再比对，
        于是**一条命令的遗漏能被另一条掩盖**：从 Git Bash 的 FIXTURE 里删掉
        ``-k AsciiCaseRecallTests``，因为 PowerShell 的 $FIXTURE 里还有，守卫照样通过；
        两条官方命令都删掉的类，也可能被 SPEC 里其它举例用的 ``-k`` 顶上。
        照文档实跑会少跑一整个类，而守卫是绿的。

        改为逐块解析四份清单，每份单独断言：

        1. 本模块 docstring 的 ``FIXTURE=`` / ``REAL=``
        2. SPEC 3.2 Git Bash 的 ``FIXTURE=`` / ``REAL=``
        3. SPEC 3.2 PowerShell 的 ``$FIXTURE =`` / ``$REAL =``

        （PowerShell 的 ``$S``/``$PY`` 之类不参与——只找这两个名字。）

        每份 fixture 清单单独等于 FIXTURE_TEST_CLASSES，real 清单单独等于
        REAL_INDEX_TEST_CLASSES。断言的是**集合相等**而不是"包含"：多写一个不存在的
        类名，``-k`` 不会报错、只是白跑，同样是文档与代码脱节。

        赋值块的边界靠"下一个空行或下一行行首的 ``名字=``"判定，行尾续行 ``\\``
        因此能跨行；``-k`` 后面的标识符两侧的引号、逗号不参与匹配，所以 bash 的
        ``-k X`` 和 PowerShell 的 ``"-k","X"`` 用同一个正则。

        SPEC.md 缺失时只查 docstring 那两份，不 skipTest：这个仓库里 SPEC.md 一定在，
        但测试文件应当能在只拷了 scripts 目录的环境里跑起来，而"零 skip"是验收口径。
        docstring 的两份在任何环境下都在，够构成一次有效断言。
        """
        expected = {
            "FIXTURE": set(FIXTURE_TEST_CLASSES),
            "REAL": set(REAL_INDEX_TEST_CLASSES),
        }

        # 文档侧的每一段命令各算一份，不合并。SPEC.md 按 Markdown 围栏代码块切开——
        # 不能用 text.find("PowerShell") 之类的关键词切：SPEC 里 "PowerShell" 这个词
        # 早在 3.2 之前就出现过（第 296 行讲两个 shell 的差异），切出来的前半段不含
        # 3.2 的 Git Bash 命令，那份就悄悄没被检查。围栏是唯一可靠的块边界。
        # (标签, 语言, 正文)。语言单独存，不从标签里再切回来。
        documents = [("test_query_kb.py 的模块 docstring", "docstring", __doc__ or "")]
        spec = Path(__file__).resolve().parents[2] / "SPEC.md"
        if spec.exists():
            text = spec.read_text(encoding="utf-8")
            for match in re.finditer(r"```(\w*)\n(.*?)```", text, re.DOTALL):
                language, body = match.group(1) or "无语言标注", match.group(2)
                # 入选条件是"真的有 FIXTURE 赋值"，不是"正文里出现 FIXTURE/REAL 字样"：
                # 后者会把 3.1 那段只出现 KB_REQUIRE_REAL_INDEX 的命令块也拉进来。
                if assignment_k_names(body, "FIXTURE") is not None:
                    line = text.count("\n", 0, match.start()) + 1
                    documents.append((f"SPEC.md 第 {line} 行的 {language} 块", language, body))

        blocks: dict[str, set[str]] = {}
        for label, _language, body in documents:
            for variable in expected:
                found = assignment_k_names(body, variable)
                if found is not None:
                    blocks[f"{label} → {variable}"] = found

        # 每一段命令都必须同时给出 FIXTURE 和 REAL，缺一份就是那份没被检查——属于
        # "文档结构变了"，不是"文档没问题"。
        for label, _language, _body in documents:
            for variable in expected:
                self.assertIn(
                    f"{label} → {variable}",
                    blocks,
                    f"{label} 里没解析出 {variable} 赋值，正则或文档结构变了："
                    f"已解析 {sorted(blocks)}",
                )

        # SPEC 3.2 承诺两个 shell 各给一版，两版都要被检查到。
        if spec.exists():
            covered = {language for _label, language, _body in documents}
            for language in ("bash", "powershell"):
                self.assertIn(
                    language,
                    covered,
                    f"SPEC.md 里没找到带 FIXTURE/REAL 的 {language} 块，"
                    f"3.2 的命令没了或围栏语言标注丢了：已解析 {sorted(blocks)}",
                )

        for key, names in blocks.items():
            variable = key.rsplit(" ", 1)[-1]
            with self.subTest(command=key):
                self.assertEqual(
                    names,
                    expected[variable],
                    f"{key} 的 -k 清单与常量不一致，照文档实跑会少跑或多跑："
                    f"漏了 {sorted(expected[variable] - names)}，"
                    f"多了 {sorted(names - expected[variable])}",
                )

    def test_fixture_classes_do_not_reference_the_real_database(self):
        # Step 1 of SPEC 3.2 runs before the rebuild. If a fixture class read the live
        # index path it would be reporting on data that step 2 is about to overwrite.
        #
        # inspect.getsource(cls), not text slicing between "class X(" markers: the module
        # level defines that path between the last fixture class and the first real-index
        # one, so a slice up to the next class name swallows it and reports a false hit.
        #
        # This class excludes itself: it is a static source check that opens no connection,
        # yet its own body has to name the symbol it is grepping for, so including itself
        # made the check fail on itself. maxDiff=None is off deliberately — the default
        # truncation keeps the message readable when a class body is long.
        needle = "DATA" + "BASE"
        for name in FIXTURE_TEST_CLASSES:
            if name == type(self).__name__:
                continue
            with self.subTest(cls=name):
                offenders = [
                    line.strip()
                    for line in inspect.getsource(globals()[name]).splitlines()
                    if needle in line
                ]
                self.assertEqual(
                    offenders, [], f"{name} 读了真库路径，会在重建前测到旧索引"
                )


class SourceRegistryTests(unittest.TestCase):
    """Guards source_registry(), which decides the scope of every coverage assertion.

    A fixture-subset class: sources.yaml is hand-authored config, not a build product, so
    reading it before the rebuild is safe. Kept separate from the index checks precisely
    because a wrong parse here would silently shrink the corpus the acceptance run covers.
    """

    def test_the_registry_reads_scalar_fields_per_source(self):
        # The coverage tests read per-source declarations (author, smoke_query) to avoid
        # assuming every source resembles the ones registered today, so the field-level
        # parse is now load-bearing, not just the id list.
        registry = source_registry()
        self.assertIn("display_name", registry["panfeng"])
        self.assertEqual(registry["panfeng"]["display_name"], "我有上将潘凤")
        self.assertEqual(registry["panfeng"]["source_path"], "飞书聊天记录_潘凤")
        # A nested block under a source must not swallow the fields that follow it, and its
        # children must not be mistaken for fields of the source.
        self.assertEqual(registry["panfeng"].get("format_counts", ""), "")
        self.assertNotIn(".html", registry["panfeng"])
        self.assertEqual(registry["panfeng"]["review_required"], "false")
        for source, fields in registry.items():
            with self.subTest(source=source):
                self.assertIn("display_name", fields, f"{source} 没有 display_name")
                self.assertIn("status", fields, f"{source} 没有 status")

    def test_declared_smoke_queries_are_long_enough_for_the_tokenizer(self):
        # Both keys, not just the source-level one: they are read by the same code path in
        # smoke_query_for and a two-character value in either sends the query down the LIKE
        # fallback, so the test would pass while measuring something other than FTS. Both
        # fields are optional, so today this iterates over nothing; it exists so that the
        # first time someone writes one, the length is checked here rather than never.
        for source, fields in source_registry().items():
            for key in ("smoke_query", "author_smoke_query"):
                declared = fields.get(key, "").strip()
                if not declared:
                    continue
                with self.subTest(source=source, key=key, query=declared):
                    self.assertGreaterEqual(
                        len(declared),
                        3,
                        f"{key} 至少三个字符，否则 trigram 建不出 gram，测的就不是 FTS 路径",
                    )

    def test_the_registry_parses_and_holds_the_approved_sources(self):
        registered = registered_sources()
        self.assertEqual(len(registered), len(set(registered)), "sources.yaml 有重复 id")
        self.assertEqual(registered, tuple(sorted(registered)))
        # The frozen sample is a subset of the registry, never the whole of it. An earlier
        # revision of this file treated the three of them AS the project's scope; that is
        # the mistake this assertion is here to prevent from coming back.
        self.assertTrue(set(REGRESSION_SAMPLE) < set(registered))
        self.assertIn("panfeng", registered)

    def test_the_registry_ignores_id_lines_outside_the_sources_block(self):
        # The parser is line-based, so it has to be pinned against a decoy. Written as a
        # temp file rather than by patching a constant, so it exercises the real reader.
        import tempfile

        decoy = (
            "version: 1\n"
            "sources:\n"
            "  - id: real_one\n"
            "    display_name: x\n"
            "    smoke_query: 情绪周期\n"
            "    format_counts:\n"
            "      .md: 3\n"
            "    status: integrated\n"
            "  - id: 'quoted_two'\n"
            "retired_sources:\n"
            "  - id: should_be_ignored\n"
            "    smoke_query: 不该被读到\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.yaml"
            path.write_text(decoy, encoding="utf-8")
            global SOURCES_CONFIG
            original, SOURCES_CONFIG = SOURCES_CONFIG, path
            try:
                self.assertEqual(registered_sources(), ("quoted_two", "real_one"))
                registry = source_registry()
                # A nested block in the middle must not stop the fields after it being read.
                self.assertEqual(registry["real_one"]["smoke_query"], "情绪周期")
                self.assertEqual(registry["real_one"]["status"], "integrated")
                self.assertNotIn(".md", registry["real_one"])
                self.assertEqual(registry["quoted_two"], {})
                self.assertNotIn("should_be_ignored", registry)
            finally:
                SOURCES_CONFIG = original

    def test_every_registered_source_has_a_structured_library_directory(self):
        # build_index.py scans source_libraries/ instead of reading this config, so the two
        # drifting apart is what let an unapproved source into the index once already. The
        # directory name must equal the id — that is the coupling the rename to panfeng/
        # restored.
        libraries = SOURCES_CONFIG.parents[1] / "source_libraries"
        present = {path.name for path in libraries.iterdir() if path.is_dir()}
        self.assertEqual(set(registered_sources()), present)
