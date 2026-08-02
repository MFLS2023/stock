#!/usr/bin/env python3
"""Tests for the retrieval layer: index shape, short-term search, source coverage.

Run with the runtime that carries the importer dependencies:
    python -m unittest test_query_kb -v

Cases marked ``expectedFailure`` state the behaviour SPEC.md requires but the
current code does not deliver. unittest counts them as expected failures, so the
suite stays green; when a fix lands it reports "unexpected success", which is the
signal to drop the marker.
"""

from __future__ import annotations

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


def chunk(source_id: str, index: int, text: str, *, topics: str = TOPIC_LABEL) -> dict:
    return {
        "chunk_id": f"{source_id}-{index:04d}",
        "source_id": source_id,
        "source_name": source_id,
        "document_id": f"{source_id}-doc",
        "parent_id": f"{source_id}-p001",
        "chunk_type": "transcript",
        "title": "示例文档",
        "date": "2026-01-01",
        "author": "示例作者",
        "topics": topics,
        "claim_type": "opinion_or_case",
        "locator": f"第{index}段",
        "text": text,
        "confidence": "medium",
    }


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
    for index in range(1, 121):
        add_chunk(connection, chunk("tulip_garden", index, with_term(2000 + index)))

    connection.execute("INSERT INTO metadata VALUES (?,?)", ("fts_tokenizer", tokenizer))
    connection.commit()
    return connection


def sources_of(rows) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["source_id"]] = counts.get(row["source_id"], 0) + 1
    return counts


def fts_hits(connection: sqlite3.Connection, term: str, column: str | None = None) -> int:
    target = column or "chunks_fts"
    sql = f'SELECT count(*) FROM chunks_fts WHERE {target} MATCH ?'
    return connection.execute(sql, ['"' + term.replace('"', '""') + '"']).fetchone()[0]


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
        # tulip_garden holds the most on-topic prose, as in production (1140 chunks).
        self.assertEqual(distribution, {"fulibei": 20, "nanjinglu_bian": 40, "tulip_garden": 120})


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
        # 当前：460/460 命中（100% 噪声）。期望：0 命中。
        self.assertEqual(fts_hits(self.connection, TOPIC_LABEL), 0)

    @unittest.expectedFailure
    def test_hits_must_converge_on_prose_matches(self):
        # 当前：正文 180 条，但标签让每个查到 topics 的词都命中 460 条。
        # 期望：短语命中数收敛到正文真含数（标题命中可另计）。
        prose = self.connection.execute(
            "SELECT count(*) FROM chunks WHERE text LIKE ?", (f"%{TOPIC_LABEL}%",)
        ).fetchone()[0]
        self.assertEqual(fts_hits(self.connection, TOPIC_LABEL), prose)

    def test_prose_term_stays_accurate(self):
        # A three-character term already works and must not regress after the fix.
        self.assertEqual(fts_hits(self.connection, "弱转强", column="text"), 180)

    def test_title_column_stays_searchable(self):
        # Titles are human-written text, unlike topics, so they keep their index.
        # On the real corpus 弱转强 matches 31 titles; dropping that would lose recall.
        self.assertEqual(fts_hits(self.connection, "示例文档", column="title"), 460)


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
        # 当前：0 命中，只能靠 LIKE 兜底。期望：FTS 或精确子串路径直接命中正文 180 条。
        # rank 999.0 是 query_kb.py 给 LIKE 回退结果打的固定分，用它识别走了哪条路。
        rows = self.search("竞价")
        self.assertTrue(all(row["rank"] != 999.0 for row in rows))

    @unittest.expectedFailure
    def test_two_character_term_must_reach_full_recall_without_the_fallback(self):
        # 当前：FTS 命中 0，全靠 LIKE 兜底把 180 条捞回来（rank 固定 999.0）。
        # 期望：不走兜底也能达到正文真含数 180 的 90%。
        # 召回量本身兜底能凑够；真正的问题是兜底带着截断和来源偏斜（见缺陷 C），
        # 所以这里同时断言"数量够"和"不是兜底给的"。
        rows = search(self.connection, "竞价", None, None, 500)
        self.assertGreaterEqual(len(rows), 162)
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

    Measured on the real database (2026-08-02), candidate pool = max(limit*30, 120):
        竞价  pool 240 = fulibei 240/0/0,   real 343/132/1140
        龙头  pool 240 = fulibei 240/0/0,   real 1008/412/232
        打板  pool 240 = fulibei 240/0/0,   real 540/319/538
        情绪  pool 240 = fulibei 240/0/0,   real 884/475/767
    Four of five high-traffic terms return nothing but fulibei. This is worse than
    zero results: the answer looks plausible while two sources are silently excluded.
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

    @unittest.expectedFailure
    def test_two_character_term_must_cover_more_than_one_source(self):
        # 当前：8 条结果全是 fulibei，而 tulip_garden 有 120 条正文命中、
        # nanjinglu_bian 有 40 条，一条都进不来。期望：至少覆盖 2 个来源。
        self.assertGreaterEqual(len(sources_of(self.search("竞价"))), 2)

    @unittest.expectedFailure
    def test_richest_source_must_appear_in_results(self):
        # tulip_garden 持有最多正文命中（fixture 120 条，真库 1140 条），
        # 却因为 rowid 排在后面被整体丢弃。
        self.assertIn("tulip_garden", sources_of(self.search("竞价")))

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
        self.assertEqual(sources_of(rows), {"fulibei": 20, "nanjinglu_bian": 40, "tulip_garden": 120})

    @unittest.expectedFailure
    def test_fallback_must_not_leak_label_only_matches_at_large_limits(self):
        # 当前：limit=500 时 LIKE 兜底返回全部 460 块，其中 280 块正文根本没提『竞价』，
        # 只因为 topics 标签就被算作命中。期望：只返回正文真含的 180 块。
        rows = search(self.connection, "竞价", None, None, 500)
        self.assertEqual(len(rows), 180)


DATABASE = Path(__file__).resolve().parents[2] / "_知识库系统" / "indexes" / "knowledge.db"


@unittest.skipUnless(DATABASE.exists(), f"索引不存在，先跑 build_index.py：{DATABASE}")
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
            "SELECT count(*) FROM chunks WHERE text LIKE ?", (f"%{term}%",)
        ).fetchone()[0]

    def test_baseline_row_counts(self):
        # SPEC 3.1: the retrieval fixes must not change how much content is indexed.
        counts = {
            table: self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("documents", "parents", "chunks")
        }
        self.assertEqual(counts, {"documents": 194, "parents": 676, "chunks": 3176})

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

    @unittest.expectedFailure
    def test_two_character_terms_must_be_searchable(self):
        # 当前全为 0（缺陷 B）。期望：≥ 正文真含数的 90%。
        for term in ("竞价", "筹码", "龙头", "打板", "情绪"):
            with self.subTest(term=term):
                self.assertGreaterEqual(fts_hits(self.connection, term), self.prose_matches(term) * 0.9)

    @unittest.expectedFailure
    def test_topic_labels_must_not_dominate_full_text_search(self):
        # 当前：情绪周期 FTS 1823 / 正文 202，龙头与核心 1358 / 正文 0，
        # 竞价与盘口 1525 / 正文 0。期望：命中数不超过正文真含数 + 标题命中。
        for term in ("情绪周期", "龙头与核心", "竞价与盘口"):
            with self.subTest(term=term):
                titles = fts_hits(self.connection, term, column="title")
                self.assertLessEqual(fts_hits(self.connection, term), self.prose_matches(term) + titles)

    @unittest.expectedFailure
    def test_two_character_query_must_span_sources(self):
        # 当前：竞价/龙头/打板/情绪 的结果 100% 是 fulibei（缺陷 C）。
        for term in ("竞价", "龙头", "打板", "情绪"):
            with self.subTest(term=term):
                rows = search(self.connection, term, None, None, 8)
                self.assertGreaterEqual(len(sources_of(rows)), 2)

    def test_three_character_query_already_spans_sources(self):
        # The working baseline on real data: 弱转强 returns fulibei + nanjinglu_bian,
        # 情绪周期 returns nanjinglu_bian + fulibei. Must not regress.
        for term in ("弱转强", "情绪周期"):
            with self.subTest(term=term):
                rows = search(self.connection, term, None, None, 8)
                self.assertGreaterEqual(len(sources_of(rows)), 2)

    def test_explicit_source_filter_still_works(self):
        # --source must keep narrowing to one source even after the skew is fixed.
        rows = search(self.connection, "竞价", "tulip_garden", None, 8)
        self.assertEqual(sources_of(rows), {"tulip_garden": 8})
