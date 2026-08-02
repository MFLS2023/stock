#!/usr/bin/env python3
"""Tests for the chunk-merging helpers shared by the importers.

Run from the project root, same command as the rest of the suite:

    export PYTHONIOENCODING=utf-8              # Git Bash; PowerShell: $env:PYTHONIOENCODING="utf-8"
    python -m unittest discover -s _知识库系统/scripts -t _知识库系统/scripts -v

Running ``python -m unittest test_kb_import_utils`` from the project root fails with
an import error: sys.path lacks the scripts directory, so the module cannot be found.

These are pure fixture tests — no database, no filesystem outside tmp — so they belong
to step 1 of SPEC 3.2 and are safe to run before the index is rebuilt. To count them on
their own, add ``-p "test_kb_import_utils.py"``; the unqualified discover collects every
test module in the directory, so its total is not this file's count.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from kb_import_utils import (
    cjk_ratio,
    clean_text,
    merge_short_units,
    span_locator,
    subtract_known_text,
    text_layer_is_usable,
)


def unit(locator: str, text: str, **extra) -> dict:
    return {"locator": locator, "text": text, "method": "docx_xml", "confidence": "high", **extra}


class SpanLocatorTests(unittest.TestCase):
    def test_joins_two_points(self):
        self.assertEqual(span_locator("正文第1段", "正文第9段"), "正文第1段—正文第9段")

    def test_flattens_existing_ranges(self):
        # Without flattening this would become a four-part locator.
        self.assertEqual(
            span_locator("正文第1段—正文第9段", "正文第11段—正文第20段"),
            "正文第1段—正文第20段",
        )

    def test_identical_endpoints_collapse(self):
        self.assertEqual(span_locator("正文第5段", "正文第5段"), "正文第5段")

    def test_missing_endpoint(self):
        self.assertEqual(span_locator("正文第1段", ""), "正文第1段")
        self.assertEqual(span_locator("", "正文第9段"), "正文第9段")
        self.assertEqual(span_locator("", ""), "")


class MergeShortUnitsTests(unittest.TestCase):
    def setUp(self):
        # 20 paragraphs of ~20 characters: the Word-export shape that caused
        # a median chunk length of 19 characters in tulip_garden.
        self.short_units = [
            unit(f"正文第{index}段", "这是一个大约二十字左右的短小段落内容。")
            for index in range(1, 21)
        ]

    def test_merges_short_paragraphs(self):
        merged = merge_short_units(self.short_units, min_chars=400, max_chars=800)
        self.assertLess(len(merged), len(self.short_units))
        self.assertEqual(merged[0]["locator"], "正文第1段—正文第20段")

    def test_preserves_content_exactly(self):
        merged = merge_short_units(self.short_units, min_chars=400, max_chars=800)
        self.assertEqual(
            "".join(item["text"] for item in merged).replace("\n", ""),
            "".join(item["text"] for item in self.short_units),
        )

    def test_records_source_locators(self):
        merged = merge_short_units(self.short_units, min_chars=400, max_chars=800)
        self.assertEqual(merged[0]["merged_unit_count"], len(merged[0]["merged_locators"]))

    def test_respects_max_chars(self):
        merged = merge_short_units(self.short_units, min_chars=100, max_chars=200)
        self.assertTrue(all(len(item["text"]) <= 200 for item in merged))

    def test_non_mergeable_units_stay_standalone(self):
        units = [
            unit("正文第1段", "短段一。"),
            unit("正文第2段内嵌图片:a.png", "图" * 40, image_key="k1", method="ocr"),
            unit("正文第3段", "短段二。"),
        ]
        merged = merge_short_units(units, mergeable=lambda item: not item.get("image_key"))
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[1]["image_key"], "k1")
        self.assertEqual([item["locator"] for item in merged], [item["locator"] for item in units])

    def test_long_unit_passes_through_untouched(self):
        long_unit = unit("正文第1段", "很长的段落。" * 200)
        merged = merge_short_units([long_unit, unit("正文第2段", "短。")])
        self.assertEqual(merged[0]["text"], long_unit["text"])

    def test_confidence_degrades_to_weakest(self):
        merged = merge_short_units(
            [
                unit("正文第1段", "短。" * 30, confidence="high"),
                unit("正文第2段", "短。" * 30, confidence="low"),
            ],
            min_chars=100,
        )
        self.assertEqual(merged[0]["confidence"], "low")

    def test_empty_and_single_input(self):
        self.assertEqual(merge_short_units([]), [])
        single = unit("正文第1段", "短。")
        self.assertEqual(merge_short_units([single]), [single])

    def test_rejects_inverted_bounds(self):
        with self.assertRaises(ValueError):
            merge_short_units([unit("正文第1段", "短。")], min_chars=900, max_chars=800)

    def test_heading_starts_new_group_once_target_is_met(self):
        merged = merge_short_units(
            [
                unit("正文第1段", "正文。" * 180),          # 540 chars, past min_chars
                unit("正文第2段", "第二章 情绪周期"),
                unit("正文第3段", "情绪周期正文。" * 60),
            ],
            min_chars=400,
            max_chars=800,
        )
        self.assertNotIn("第二章 情绪周期", merged[0]["text"])

    def test_heading_absorbed_before_target_is_met(self):
        # Measured trade-off on tulip_garden: breaking on every heading-looking line
        # cut the median prose chunk from 428 to 174 characters, because most short
        # lines in these exports are list items, not section titles.
        merged = merge_short_units(
            [
                unit("正文第1段", "正文。" * 90),           # 270 chars, below min_chars
                unit("正文第2段", "第二章 情绪周期"),
                unit("正文第3段", "情绪周期正文。" * 60),
            ],
            min_chars=400,
            max_chars=800,
        )
        self.assertIn("第二章 情绪周期", merged[0]["text"])

    def test_two_pass_merge_keeps_locator_flat(self):
        merged = merge_short_units(
            merge_short_units(self.short_units, min_chars=100, max_chars=200),
            min_chars=400,
            max_chars=800,
        )
        self.assertEqual(merged[0]["locator"].count("—"), 1)


class TextLayerQualityTests(unittest.TestCase):
    # Real samples taken from nanjinglu_bian PDFs: a broken embedded font extracts
    # as arbitrary code points while reporting a plausible character count.
    GARBLED = "成飞周期弹性的表现:\n_|\\笉颫\x17{y\x00+\x00V\x00\n\x00c\x00x\x00b\x001\x003\x000\x004"
    PROSE = "虽然知道这是短线相当强的信号，但这种方法当然就不适合去追涨，那为什么这里是关键。"

    def test_cjk_ratio_separates_prose_from_garbage(self):
        self.assertGreater(cjk_ratio(self.PROSE), 0.8)
        self.assertLess(cjk_ratio(self.GARBLED), 0.5)

    def test_cjk_ratio_on_empty_input(self):
        self.assertEqual(cjk_ratio(""), 0.0)
        self.assertEqual(cjk_ratio("   \n\t "), 0.0)

    def test_usable_prose_skips_ocr(self):
        self.assertTrue(text_layer_is_usable(self.PROSE * 2, min_chars=50))

    def test_garbled_layer_falls_through_to_ocr(self):
        self.assertFalse(text_layer_is_usable(self.GARBLED * 4, min_chars=50))

    def test_blank_and_short_pages_fall_through_to_ocr(self):
        self.assertFalse(text_layer_is_usable("", min_chars=50))
        self.assertFalse(text_layer_is_usable("只有几个字。", min_chars=50))

    def test_old_default_would_have_rejected_good_pages(self):
        # 385 characters was the measured average for pages that the previous
        # threshold of 500 pushed into OCR.
        page = "这是一段正常的中文正文内容。" * 28  # ~392 chars
        self.assertFalse(text_layer_is_usable(page, min_chars=500))
        self.assertTrue(text_layer_is_usable(page, min_chars=50))


class CleanTextNulTests(unittest.TestCase):
    """A NUL anywhere in a chunk hides the rest of it from SQLite GLOB and LIKE.

    Both compare with C-string semantics and stop at the first NUL byte, so a chunk
    whose prose sits past one is unreachable no matter how the query is written —
    the retrieval layer cannot fix this, only the import layer can. The measured
    case: ``nanjinglu-92154afd0e2c-p008-c05`` carried a NUL at character 5 and its
    龙头 at character 44, which made the whole chunk invisible to 龙头.
    """

    # Real sample: a broken embedded font extracts as NUL-separated byte soup, and the
    # page's actual prose follows it.
    GARBLED_THEN_PROSE = "_|\\笉颫\x17{y\x00+\x00V\x00\n\x00c\x00x\x00b\x00\n龙头是在高位的"

    def test_removes_nul(self):
        self.assertEqual(clean_text("king \x00 来自浙江"), "king 来自浙江")

    def test_removes_every_nul_not_just_the_first(self):
        self.assertNotIn("\x00", clean_text("a\x00b\x00c\x00d"))
        self.assertEqual(clean_text("a\x00b\x00c\x00d"), "abcd")

    def test_prose_after_a_nul_survives(self):
        cleaned = clean_text(self.GARBLED_THEN_PROSE)
        self.assertNotIn("\x00", cleaned)
        self.assertIn("龙头是在高位的", cleaned)

    def test_nul_is_removed_on_the_ocr_path_too(self):
        # ocr=True takes a different branch through the CJK space collapsing, so it
        # needs its own case rather than trusting the shared tail.
        self.assertNotIn("\x00", clean_text("龙 头\x00复 盘", ocr=True))
        self.assertEqual(clean_text("龙 头\x00复 盘", ocr=True), "龙头复盘")

    def test_nul_only_input_collapses_to_empty(self):
        self.assertEqual(clean_text("\x00"), "")
        self.assertEqual(clean_text("\x00\x00\x00"), "")

    def test_nul_does_not_glue_words_across_a_line_break(self):
        # Deleting the NUL must not swallow the newline beside it, or two paragraphs
        # would merge into one sentence.
        self.assertEqual(clean_text("第一段\x00\n第二段"), "第一段\n第二段")

    def test_ordinary_text_is_unchanged(self):
        prose = "竞价是情绪的第一个观察点。\n\n龙头才是主线。"
        self.assertEqual(clean_text(prose), prose)


class SubtractKnownTextTests(unittest.TestCase):
    PROSE = (
        "这次首次提出的“算电协同”新基建，作为新的基础设施投资，"
        "东数西算的进一步延伸，统筹就地建设利用绿电的数据中心完成消纳。"
    )
    SCREENSHOT = "《关于促进人工智能与能源双向赋能的行动方案》。其中指出，统筹优化能源资源与算力布局。"

    def test_drops_lines_the_text_layer_already_covers(self):
        residue = subtract_known_text(self.PROSE, self.PROSE)
        self.assertEqual(residue, "")

    def test_keeps_screenshot_only_content(self):
        residue = subtract_known_text(f"{self.PROSE}\n{self.SCREENSHOT}", self.PROSE)
        self.assertIn("行动方案", residue)
        self.assertNotIn("东数西算", residue)

    def test_tolerates_ocr_misreads_in_repeated_prose(self):
        # OCR corrupts a few characters but the sentence is still a re-read.
        garbled = self.PROSE.replace("统筹", "统箅").replace("消纳", "消細")
        self.assertEqual(subtract_known_text(garbled, self.PROSE), "")

    def test_empty_inputs(self):
        self.assertEqual(subtract_known_text("", self.PROSE), "")
        self.assertEqual(subtract_known_text(self.SCREENSHOT, ""), self.SCREENSHOT)

    def test_short_lines_are_always_kept(self):
        # Too little signal to match reliably; dropping them risks losing real content.
        residue = subtract_known_text(f"{self.PROSE}\n5板", self.PROSE)
        self.assertIn("5板", residue)


if __name__ == "__main__":
    unittest.main(verbosity=2)
