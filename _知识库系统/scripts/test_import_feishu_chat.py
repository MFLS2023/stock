#!/usr/bin/env python3
"""Tests for the Feishu speaker/time-aware HTML importer."""

from __future__ import annotations

import sys
import tempfile
import unittest
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from import_feishu_chat import (
    SOURCE_ROOT,
    message_locator,
    normalize_message_text,
    parse_export_date,
    parse_html,
    reconstruct_messages,
)


SYNTHETIC_HTML = """<!doctype html><html><body>
<div class="meta-info">导出时间: 2026/8/1 16:34:36 | 共 4 条记录</div>
<div class="msg-group">
  <div class="msg-header"><span class="sender-name">甲</span><span class="msg-time">7月24日 14:44</span></div>
  <div class="msg-text-bubble"><div>第一条\n【潘凤老师】</div></div>
  <div class="consecutive-container">
    <div class="consecutive-item"><div class="consecutive-time">11:25</div><div class="msg-bubble-consecutive"><div>跨到下个交易日</div></div></div>
    <div class="consecutive-item"><div class="consecutive-time">unknown</div><div class="msg-bubble-consecutive"><div>继承时间</div></div></div>
  </div>
</div>
<div class="msg-group">
  <div class="msg-header"><span class="sender-name">乙</span><span class="msg-time">昨天 08:35</span></div>
  <div class="msg-text-bubble"><div>最后一条</div></div>
</div>
</body></html>"""


class FeishuImporterTests(unittest.TestCase):
    def parse_synthetic(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "chat.html"
            path.write_text(SYNTHETIC_HTML, encoding="utf-8")
            groups, meta = parse_html(path)
        return groups, meta

    def test_parser_preserves_folded_messages_and_speaker(self):
        groups, meta = self.parse_synthetic()
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["sender"], "甲")
        self.assertEqual(len(groups[0]["consecutive_texts"]), 2)
        self.assertEqual(parse_export_date(meta["meta_text"]), date(2026, 8, 1))

    def test_date_rollover_skips_weekend_and_unknown_inherits(self):
        groups, meta = self.parse_synthetic()
        messages, stats = reconstruct_messages(groups, parse_export_date(meta["meta_text"]))
        self.assertEqual([item["date"] for item in messages], [
            "2026-07-24", "2026-07-27", "2026-07-27", "2026-07-31"
        ])
        self.assertEqual(messages[2]["time"], "11:25")
        self.assertTrue(messages[2]["time_inherited"])
        self.assertEqual(stats["market_day_rollovers"], 1)
        self.assertEqual(stats["unknown_time_inherited"], 1)

    def test_redundant_signature_is_removed_without_rewriting_content(self):
        text, changed = normalize_message_text("Gμo瓷材料\n【潘凤老师】")
        self.assertTrue(changed)
        self.assertEqual(text, "Gμo瓷材料")

    def test_locator_uses_clock_extremes_when_export_groups_rewind(self):
        items = [
            {"date": "2026-07-28", "time": "16:15", "message_no": 2978},
            {"date": "2026-07-28", "time": "12:26", "message_no": 2979},
        ]
        self.assertEqual(
            message_locator(items),
            "2026-07-28 12:26—16:15｜消息2978—2979",
        )

    def test_actual_export_shape_when_present(self):
        candidates = sorted(SOURCE_ROOT.glob("*.html"))
        if len(candidates) != 1:
            self.skipTest("The registered Feishu export is not present")
        groups, meta = parse_html(candidates[0])
        messages, stats = reconstruct_messages(groups, parse_export_date(meta["meta_text"]))
        self.assertEqual(len(groups), 16)
        self.assertEqual(len(messages), 3445)
        self.assertEqual(Counter(item["sender"] for item in messages), {
            "我有上将潘凤": 3326,
            "5280": 119,
        })
        self.assertEqual((messages[0]["date"], messages[-1]["date"]), ("2026-06-23", "2026-07-31"))
        self.assertEqual(len({item["date"] for item in messages}), 29)
        self.assertEqual(stats["unknown_time_inherited"], 2)
        self.assertEqual(meta["images"], 0)
        self.assertEqual(meta["external_links"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
