# -*- coding: utf-8 -*-
"""OCR 坏块率汇总：按层统计孤立部首坏块，产出 reports/ocr_quality_report.md。

只读（SQLite 走 mode=ro），不改任何语料。OCR 层重跑或索引重建后手动跑一次：

    python _知识库系统/scripts/report_ocr_quality.py

坏块判据与历次审查一致：clean 后孤立部首（忄亻讠钅纟饣彳氵灬扌宀辶阝卩刂）
出现 >=2 个记坏块。判据偏保守——按邻接判定其实更低，这里报的是难看的值。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "indexes" / "knowledge.db"
OUT = ROOT / "reports" / "ocr_quality_report.json"
OUT_MD = ROOT / "reports" / "ocr_quality_report.md"

RAD_RE = re.compile(r"[忄亻讠钅纟饣彳氵灬扌宀辶阝卩刂]")

# 五个既定 OCR 层：(chunk_type, source_id, 说明)
LAYERS = [
    ("chart_ocr", "boduanzhimen", "K线图 OCR"),
    ("course_ocr", "aizaibingchuan", "课程讲义 OCR"),
    ("screenshot_ocr", "nanjinglu_bian", "南京路截图 OCR"),
    ("image_ocr", "tulip_garden", "郁金香截图 OCR"),
    ("paid_article", "boduanzhimen", "付费扫描页"),
]


def is_bad(text: str) -> bool:
    return len(RAD_RE.findall(text or "")) >= 2


def main() -> int:
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    today = date.today().isoformat()

    rows_out = []
    for ctype, src, label in LAYERS:
        rows = con.execute(
            "SELECT chunk_id, text FROM chunks WHERE chunk_type=? AND source_id=?",
            (ctype, src),
        ).fetchall()
        bad = [r for r in rows if is_bad(r["text"])]
        # 每层抓 3 条最长的坏块当定位样本（chunk_id 可直接回查原图）
        samples = [
            {"chunk_id": r["chunk_id"], "length": len(r["text"] or "")}
            for r in sorted(bad, key=lambda r: -len(r["text"] or ""))[:3]
        ]
        rows_out.append(
            {
                "layer": f"{src}/{ctype}",
                "label": label,
                "total": len(rows),
                "bad": len(bad),
                "rate": round(100.0 * len(bad) / len(rows), 1) if rows else 0.0,
                "samples": samples,
            }
        )

    # 兜底扫一遍其余带 ocr 字样的层，防止新来源建了新层却没人盯
    extra = con.execute(
        "SELECT chunk_type, source_id, COUNT(*) AS n FROM chunks "
        "WHERE chunk_type LIKE '%ocr%' GROUP BY chunk_type, source_id"
    ).fetchall()
    covered = {(ctype, src) for ctype, src, _ in LAYERS}
    unlisted = [
        {"chunk_type": r["chunk_type"], "source_id": r["source_id"], "blocks": r["n"]}
        for r in extra
        if (r["chunk_type"], r["source_id"]) not in covered
    ]

    payload = {"date": today, "layers": rows_out, "unlisted_ocr_layers": unlisted}
    OUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        f"# OCR 坏块率汇总（{today}）",
        "",
        "判据：孤立部首 [忄亻讠钅纟饣彳氵灬扌宀辶阝卩刂] >=2 记坏块（保守口径）。",
        "由 `scripts/report_ocr_quality.py` 生成；OCR 重跑或索引重建后手动执行。",
        "",
        "| 层 | 块数 | 坏块 | 坏块率 |",
        "|---|---:|---:|---:|",
    ]
    for r in rows_out:
        lines.append(
            f"| {r['label']}（{r['layer']}） | {r['total']} | {r['bad']} | {r['rate']}% |"
        )
    lines.append("")
    lines.append("各层最长坏块样本（chunk_id 可回查原图核对）：")
    lines.append("")
    for r in rows_out:
        ids = ", ".join(s["chunk_id"] for s in r["samples"]) or "（无坏块）"
        lines.append(f"- {r['label']}: {ids}")
    if unlisted:
        lines.append("")
        lines.append("未纳入既定五层的 OCR 类块（新层出现时把它加进 LAYERS）：")
        for u in unlisted:
            lines.append(f"- {u['source_id']}/{u['chunk_type']}: {u['blocks']} 块")
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    for r in rows_out:
        print(f"{r['layer']}: {r['bad']}/{r['total']} = {r['rate']}%")
    print(f"已写出 {OUT_MD.name} / {OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
