#!/usr/bin/env python3
"""Tests for the chunk-merging helpers shared by the importers.

Run with the runtime that carries the importer dependencies:
    python -m unittest test_kb_import_utils -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from kb_import_utils import merge_short_units, span_locator


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
