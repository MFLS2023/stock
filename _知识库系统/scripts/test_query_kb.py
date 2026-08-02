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

    ... discover ... -k FixtureShapeTests -k IndexPollutionTests \
                     -k ShortTermSearchTests -k SourceCoverageTests \
                     -k RetrievalContractTests -k SubsetMarkerTests \
                     -k SourceRegistryTests
    ... discover ... -k RealIndexRequirementTests -k RealIndexTests \
                     -k RegistryScopedIndexTests                 # after rebuild

``test_the_subset_markers_cover_every_test_class`` keeps those two lists exhaustive:
add a class without listing it and that test fails.

Two source scopes, deliberately kept apart — see REGRESSION_SAMPLE and source_registry().
Frozen row counts and hit counts are asserted against the sample; everything phrased as
"retrieval must reach it" is asserted against sources.yaml, so a newly approved source is
covered without editing this file.

Nor may a coverage assertion assume what a future source contains. No fixed term list, no
"every source must hold this word", no "every source must have an author": search terms are
either declared per source in sources.yaml (smoke_query) or derived from that source's own
text, author coverage is scoped to sources that carry authorship, and per-term coverage is
computed from which sources actually hold matches. See smoke_query_for().

Cases marked ``expectedFailure`` state the behaviour SPEC.md requires but the
current code does not deliver. Two rules, per SPEC 3.0:

1. When a fix lands, the case flips to "unexpected success" and unittest exits
   with code **1**, not 0. That is a red build, not a green one. The marker must be
   deleted in the same commit as the fix, and the suite re-run to a plain PASS.
2. A phase is only accepted when the run reports no expected failures, no
   unexpected successes and no skips. Set ``KB_REQUIRE_REAL_INDEX=1`` to turn a
   missing knowledge.db from a silent skip into a hard failure — without it,
   RealIndexTests can skip 20-odd assertions and still print "OK".
"""

from __future__ import annotations

import inspect
import os
import re
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from build_index import add_chunk, create_schema
from query_kb import search


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
# 888 prose matches spread 369/150/369, so a query pairing it with another short term
# must not come back holding only one source's chunks.
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
) -> dict:
    return {
        "chunk_id": f"{source_id}-{index:04d}",
        "source_id": source_id,
        "source_name": source_id,
        "document_id": f"{source_id}-doc",
        "parent_id": f"{source_id}-p001",
        "chunk_type": "transcript",
        "title": title,
        "date": "2026-01-01",
        "author": author,
        "topics": topics,
        "claim_type": "opinion_or_case",
        "locator": f"第{index}段",
        "text": text,
        "confidence": "medium",
    }


# Chunks whose TITLE carries the two-character term while the prose does not. On the real
# corpus this is a large share of legitimate results — 310 chunks for 龙头, 406 for 情绪,
# 58 for 竞价 — because titles are human-written, unlike the topics column. SPEC 2.2
# therefore recalls text/title/author, and these fixture rows are what proves a
# text-only implementation would drop them.
TITLE_WITH_TERM = "竞价专题整理"
AUTHOR_WITH_TERM = "竞价老师"


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

    Measured on the real index: GLOB uses the trigram index (VIRTUAL TABLE INDEX 0:G4,
    18.2ms) and returns exactly the prose-match count, while LIKE falls back to a scan
    (187.4ms) because SQLite's LIKE is case-insensitive by default.
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
        # corpus, where 龙头 has 310 title-only chunks and 情绪 406.
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
    """SPEC 缺陷 A: the auto-generated topics column is indexed for full-text search.

    Measured on the real database (3176 chunks, 2026-08-02):
        情绪周期    FTS 1823, prose 202  -> 89% noise
        龙头与核心  FTS 1358, prose 0    -> 100% noise
        竞价与盘口  FTS 1525, prose 0    -> 100% noise
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

    @unittest.expectedFailure
    def test_topic_label_must_not_match_full_text_search(self):
        # 当前：475/475 命中（100% 噪声）。期望：0 命中。
        self.assertEqual(fts_hits(self.connection, TOPIC_LABEL), 0)

    @unittest.expectedFailure
    def test_hits_must_converge_on_prose_matches(self):
        # 当前：正文 0 条含标签，但标签让每个查到 topics 的词都命中 475 条。
        # 期望：短语命中数收敛到正文真含数（标题命中可另计）。
        prose = self.connection.execute(
            "SELECT count(*) FROM chunks WHERE text LIKE ?", (f"%{TOPIC_LABEL}%",)
        ).fetchone()[0]
        self.assertEqual(fts_hits(self.connection, TOPIC_LABEL), prose)

    def test_prose_term_stays_accurate(self):
        # A three-character term already works and must not regress after the fix.
        self.assertEqual(fts_hits(self.connection, "弱转强", column="text"), 150)

    def test_title_column_stays_searchable(self):
        # Titles are human-written text, unlike topics, so they keep their index. On the
        # frozen sources, title-only matches are a large share of legitimate results:
        # 310 chunks for 龙头, 406 for 情绪, 58 for 竞价. Dropping the column would lose them.
        # 465 = 475 total minus the 10 chunks retitled to TITLE_WITH_TERM.
        self.assertEqual(fts_hits(self.connection, "示例文档", column="title"), 465)
        self.assertEqual(fts_hits(self.connection, TITLE_WITH_TERM, column="title"), 10)


class ShortTermSearchTests(unittest.TestCase):
    """SPEC 缺陷 B: query_kb.py drops terms shorter than three characters.

    Measured on the real database (2026-08-02): 竞价 394 prose matches / 0 FTS hits,
    筹码 367/0, 龙头 615/0, 打板 186/0, 情绪 888/0. Most trading vocabulary is
    two characters, so this is the highest-traffic entry point into the corpus.
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
        # Documents the root cause: trigram needs three characters to build a gram,
        # and query_kb.py:34 filters the term out before it reaches MATCH.
        self.assertEqual(fts_hits(self.connection, "竞价"), 0)

    def test_two_character_term_still_returns_results_via_fallback(self):
        # The LIKE fallback hides the defect: results come back, so the failure is
        # invisible until you check which sources they came from.
        rows = self.search("竞价")
        self.assertEqual(len(rows), 8)

    @unittest.expectedFailure
    def test_two_character_term_must_not_need_the_fallback(self):
        # 当前：0 命中，只能靠 LIKE 兜底。期望：FTS 或精确子串路径直接命中正文 150 条。
        # rank 999.0 是 query_kb.py 给 LIKE 回退结果打的固定分，用它识别走了哪条路。
        rows = self.search("竞价")
        self.assertTrue(all(row["rank"] != 999.0 for row in rows))

    @unittest.expectedFailure
    def test_two_character_term_must_reach_full_recall_without_the_fallback(self):
        # 当前：FTS 命中 0，全靠 LIKE 兜底把 475 条全捞回来（rank 固定 999.0），
        # 其中 310 条只挂了 topics 标签。
        # 期望：召回等于 text/title/author 三字段并集的 165 条，且不是兜底给的。
        expected = recall_target_ids(self.connection, "竞价")
        rows = search(self.connection, "竞价", None, None, 500)
        got = {row["chunk_id"] for row in rows}
        self.assertEqual(got, expected)
        self.assertTrue(all(row["rank"] != 999.0 for row in rows))

    @unittest.expectedFailure
    def test_title_and_author_only_matches_must_be_recalled(self):
        # 当前：两字词进不了 FTS，兜底虽然四字段 OR 能碰到这 15 条，但混在 310 条
        # 标签噪声里，且带着候选池截断。期望：这 15 条必须在召回结果里。
        # 真库同类块：龙头 310 条、情绪 406 条、竞价 58 条，全部来自 title。
        # 这 15 条的数量由 test_recall_target_is_larger_than_prose_alone 那个普通测试守着，
        # 不在这里重复断言——写在 expectedFailure 内部的守卫失败会被吞掉，等于没写。
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

    def test_mixed_query_currently_ignores_the_short_term(self):
        # '竞价 情绪周期' silently becomes '情绪周期': the short term is dropped and the
        # FTS branch succeeds, so no fallback ever runs to recover it.
        rows = self.search("竞价 弱转强")
        self.assertTrue(all(row["rank"] != 999.0 for row in rows))


class SourceCoverageTests(unittest.TestCase):
    """SPEC 缺陷 C: the LIKE fallback truncates by rowid, so results skew to one source.

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

    Every one of these high-traffic terms returns nothing but fulibei. That is worse than
    zero results: the answer looks plausible while the other sources are silently excluded.

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
        # Replays query_kb.py:54-61 verbatim: LIMIT without ORDER BY.
        pool = self.connection.execute(
            "SELECT source_id FROM chunks "
            "WHERE (text LIKE ? OR title LIKE ? OR author LIKE ? OR topics LIKE ?) LIMIT ?",
            [*["%竞价%"] * 4, 240],
        ).fetchall()
        self.assertEqual(sources_of(pool), {"fulibei": 240})

    def test_default_limit_currently_returns_one_source_only(self):
        # Records today's skew as a baseline rather than demanding a quota. Both of the
        # assertions that used to live here — "the 8 rows span >= 2 sources" and
        # "tulip_garden must appear in the 8" — were requirements on the top 8, which
        # SPEC 阶段 3 explicitly does not impose. Coverage is a recall-layer property; see
        # test_recall_layer_must_reach_every_source_that_holds_matches below.
        self.assertEqual(set(sources_of(self.search("竞价"))), {"fulibei"})

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

    @unittest.expectedFailure
    def test_recall_layer_must_reach_every_source_that_holds_matches(self):
        # 当前：兜底候选池按 rowid 截断，tulip_garden 的 95 条命中一条都进不来。
        # 期望：大 limit 下，每个真的持有命中的来源都出现在召回里，且召回等于三字段并集。
        # 不涉及排名，也不要求任何来源在前 8 条里出现。
        # fixture 形状的守卫在上面的普通测试里，不放这里——会被当成预期失败吞掉。
        expected = self.fixture_holders()
        rows = self.search("竞价", limit=500)
        self.assertTrue(all(row["rank"] != 999.0 for row in rows))
        self.assertEqual({row["chunk_id"] for row in rows}, recall_target_ids(self.connection, "竞价"))
        self.assertEqual(set(sources_of(rows)), set(expected))

    @unittest.expectedFailure
    def test_prose_matches_must_outrank_label_only_matches(self):
        # relevance() 给 topics 权重 4.0、正文 1.0，而 topics 是自动标签。
        # 实测 fixture：前 8 条正文真含 0/8，全是只挂了标签的块。
        # 真库里『竞价』有 1221 块是"仅 topics 含、正文不含"，同样排在真讲竞价的块前面。
        rows = self.search("竞价")
        self.assertTrue(all("竞价" in row["text"] for row in rows))

    def test_fts_path_ranks_across_the_whole_table(self):
        # The FTS branch scores every matching row before truncating, so a large limit
        # returns exactly the prose matches and nothing else. This is the behaviour the
        # fallback must match; asserting it here catches a regression in either path.
        rows = search(self.connection, "弱转强", None, None, 500)
        self.assertEqual(sources_of(rows), {"fulibei": 20, "nanjinglu_bian": 40, "tulip_garden": 90})

    @unittest.expectedFailure
    def test_fallback_must_not_leak_label_only_matches_at_large_limits(self):
        # 当前：limit=500 时 LIKE 兜底返回全部 475 块，其中 310 块只挂了 topics 标签，
        # 正文、标题、作者里都没有『竞价』。期望：只返回三字段并集的 165 块。
        # 165/310/475 这三个数由 test_label_only_chunks_are_disjoint_from_the_recall_target
        # 那个普通测试守着，这里不重复写死——写在 expectedFailure 内部会被吞掉。
        rows = search(self.connection, "竞价", None, None, 500)
        self.assertEqual(len(rows), len(recall_target_ids(self.connection, "竞价")))


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
        short = [row["chunk_id"] for row in self.search("弱转强", limit=8)]
        long = [row["chunk_id"] for row in self.search("弱转强", limit=40)]
        self.assertEqual(long[:8], short)

    # --- 混合与多词查询 ---

    def test_mixed_length_query_uses_the_fts_branch(self):
        # '竞价 弱转强' keeps only 弱转强 (>=3 chars), so fts_terms is non-empty and the
        # fallback never runs. Baseline: the results are real prose matches, which must
        # not regress when the short-term path lands.
        rows = self.search("竞价 弱转强", limit=50)
        self.assertTrue(rows)
        self.assertTrue(all(row["rank"] != 999.0 for row in rows))
        for row in rows:
            with self.subTest(chunk=row["chunk_id"]):
                self.assertTrue("竞价" in row["text"] or "弱转强" in row["text"])

    @unittest.expectedFailure
    def test_short_term_must_contribute_in_a_mixed_length_query(self):
        # 当前：query_kb.py:34 把两字词整个丢掉，'情绪 弱转强' 与单查 '弱转强' 返回
        # 完全一样的结果——短词贡献为零，而且用户看不出来。
        # fixture 里 SECOND_TERM 只在 30 个 tulip_garden 块里，且这 30 块不含 弱转强，
        # 而 弱转强 覆盖另外 150 块，所以短词一旦生效，结果集必须比单查 弱转强 更大。
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

    @unittest.expectedFailure
    def test_multiple_two_character_terms_must_not_return_label_only_chunks(self):
        # 当前：limit=500 时兜底把全部 475 块返回，其中 280 块 text/title/author 三个
        # 字段里都没有这两个词，纯靠 topics 自动标签进来（缺陷 A + 缺陷 C 的组合）。
        # 期望：只返回三字段并集的块。
        target = recall_target_ids(self.connection, "竞价") | recall_target_ids(
            self.connection, SECOND_TERM
        )
        got = {row["chunk_id"] for row in self.search(f"竞价 {SECOND_TERM}", limit=500)}
        self.assertEqual(got, target)

    @unittest.expectedFailure
    def test_multiple_two_character_terms_must_both_contribute(self):
        # 当前：默认 limit=8 时候选池 240 条全被 fulibei 的纯标签块占满，
        # 只含『情绪』的 30 个 tulip_garden 块一条都进不来。
        # 期望：两个词各自的命中都能进结果，且结果正文真含至少一个词。
        rows = self.search(f"竞价 {SECOND_TERM}")
        self.assertTrue(any(SECOND_TERM in row["text"] for row in rows))
        for row in rows:
            with self.subTest(chunk=row["chunk_id"]):
                self.assertTrue("竞价" in row["text"] or SECOND_TERM in row["text"])

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
        measured on the frozen sources, 龙头 has 310 chunks and 情绪 406 whose title
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
        for term, expected in [("竞价", 394), ("筹码", 367), ("龙头", 615), ("打板", 186), ("情绪", 888)]:
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
        # legitimate hits — 310 chunks for 龙头, 406 for 情绪 — so GLOB has to work there
        # as well, otherwise stage 2 would have to drop them.
        for term in ("竞价", "龙头", "情绪"):
            with self.subTest(term=term):
                union = self.scoped_glob_hits(term, "text") + self.scoped_glob_hits(term, "title")
                self.assertGreater(self.scoped_glob_hits(term, "title"), 0)
                self.assertGreaterEqual(union, len(self.recall_target_ids(term)))

    def test_recall_target_exceeds_prose_for_high_traffic_terms(self):
        # Pins the reason the acceptance threshold is the three-field union: measuring
        # against prose alone would require the fix to DISCARD these title-only chunks.
        for term, extra in [("竞价", 58), ("筹码", 48), ("龙头", 310), ("情绪", 406)]:
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

    def test_large_limit_masks_the_recall_defect_but_not_the_precision_one(self):
        # Today's baseline, in two halves.
        #
        # Recall looks fine at limit=5000: candidate_limit becomes 150000, larger than the
        # table, so the fallback returns every match. That is why the acceptance assertion
        # also requires rank != 999.0 — coverage alone can be faked by a big limit.
        #
        # Precision does not look fine: the fallback ORs in the topics column, so it also
        # returns chunks matched by the auto label alone. Those are counted here so the
        # figure is on record rather than assumed away.
        for term in ("竞价", "筹码", "龙头", "打板", "情绪"):
            with self.subTest(term=term):
                rows = self.in_scope(search(self.connection, term, None, None, 5000))
                got = {row["chunk_id"] for row in rows}
                self.assertTrue(all(row["rank"] == 999.0 for row in rows))
                self.assertTrue(self.recall_target_ids(term) <= got)
                # Label-only chunks come back too, which is the precision half of 缺陷 A.
                self.assertTrue(self.label_only_ids(term) & got)

    @unittest.expectedFailure
    def test_two_character_terms_must_be_recalled_without_the_fallback(self):
        # 当前：两字词进不了 MATCH，只能靠 LIKE 兜底（rank 恒为 999.0），
        # 而兜底带着候选池截断和来源偏斜（缺陷 B + C）。
        # 期望：召回等于 text/title/author 三字段并集，且不是兜底给的。
        #
        # 口径是并集而不是正文：真库里 龙头 有 310 块、情绪 有 406 块只在标题里含词，
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

    def test_default_limit_results_come_from_the_fallback(self):
        # Provenance for the numbers above: rank 999.0 means query_kb.py:56 produced them,
        # so precision today is a property of relevance() over a truncated pool, and any
        # change to either has to be re-measured rather than assumed.
        for term in ("竞价", "龙头", "打板", "情绪", "筹码"):
            with self.subTest(term=term):
                rows = search(self.connection, term, None, None, 8)
                self.assertTrue(all(row["rank"] == 999.0 for row in rows))

    @unittest.expectedFailure
    def test_topic_labels_must_not_dominate_full_text_search(self):
        # 当前：情绪周期 FTS 1823 / 正文 202，龙头与核心 1358 / 正文 0，
        # 竞价与盘口 1525 / 正文 0。期望：命中数不超过 text/title/author 并集。
        # 数字取回归样本范围内——全库口径下 情绪周期 是 1850，多算了样本外来源的 27 条。
        for term in ("情绪周期", "龙头与核心", "竞价与盘口"):
            with self.subTest(term=term):
                self.assertLessEqual(
                    self.scoped_fts_hits(term), len(self.recall_target_ids(term))
                )

    def test_current_default_limit_drops_all_but_one_sample_source(self):
        # Today's baseline for 缺陷 C, stated as a measurement rather than a target.
        # Within the sample all four terms return fulibei only. Unfiltered, 打板 also
        # returns one panfeng row, which is exactly the trap that makes a "spans >= 2
        # sources" assertion useless: it would already pass for 打板 while
        # nanjinglu_bian and tulip_garden are both absent. Coverage is asserted at the
        # recall layer instead, as an equality that means nothing was dropped.
        for term in ("竞价", "龙头", "打板", "情绪"):
            with self.subTest(term=term):
                rows = self.in_scope(search(self.connection, term, None, None, 8))
                self.assertEqual(set(sources_of(rows)), {"fulibei"})

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

    @unittest.expectedFailure
    def test_recall_layer_must_reach_every_source_that_holds_matches(self):
        # 当前：兜底候选池按 rowid 截断，郁金香的 245 条『竞价』正文命中一条都进不来。
        # 期望：大 limit 下每个真的持有命中的来源都出现在召回里。
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


CJK_RUN = re.compile(r"[一-鿿]+")
SMOKE_MIN_HITS = 3
SMOKE_SAMPLE_CHUNKS = 40


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

    @unittest.expectedFailure
    def test_recall_layer_must_reach_every_registered_source_that_holds_matches(self):
        # 当前（2026-08-02 实测，全库口径）：两字词全部走 LIKE 兜底，rank 恒为 999.0，
        # 召回集合和 text/title/author 并集不一致——竞价 目标 453 条，兜底返回 1620 条，
        # 多出来的是 topics 标签命中；同时候选池按 rowid 截断，各来源比例也不对。
        # 期望：召回等于三字段并集，且每个真的持有命中的来源都出现，不靠兜底。
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


# The -k names SPEC 3.2 uses to split the suite. Kept next to the check that enforces
# them so the two never drift apart.
FIXTURE_TEST_CLASSES = (
    "FixtureShapeTests",
    "IndexPollutionTests",
    "ShortTermSearchTests",
    "SourceCoverageTests",
    "RetrievalContractTests",
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
