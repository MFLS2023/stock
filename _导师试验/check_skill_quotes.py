#!/usr/bin/env python3
"""核对导师 SKILL.md 里的引文能否在知识库里回溯到原文。

为什么需要：导师 SKILL 的定位是「用某位作者的体系回答我」，用户看到引号里的
内容会当成作者原话去用。抽查发现郁金香 SKILL 里 10 条引文有 9 条在库内找不到，
且写法是裸引用（无页码/文件名/chunk_id）。必须全量核对并标注。

比对方式（三档，逐档放宽）：
  1. 原样子串
  2. 去掉全部空白后子串 —— 郁金香的 image_ocr 块字间带空格
  3. 取引文里最长的汉字片段做探针

找不到 ≠ 编造：可能来自未导入知识库的素材（郁金香 15 个 docx 只导入了部分）。
所以输出区分「已回溯」「库内无」，由人判断后者是库外素材还是失实。

只读，不改任何文件。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = ROOT / "_知识库系统" / "indexes" / "knowledge.db"
SKILLS = {
    "yujinxiang": (ROOT / "_导师试验/skills/yujinxiang-perspective/SKILL.md", "tulip_garden"),
    "nanjinglu": (ROOT / "_导师试验/skills/nanjinglu-bian-perspective/SKILL.md", "nanjinglu_bian"),
}

CJK = re.compile(r"[一-鿿]")
WS = re.compile(r"\s+")

# 引文候选：只认成对的中文引号「」和成对的直/弯双引号，且必须在同一行内。
#
# 第一版写成 `[「"“]([^「」"“”]{12,80})[」"”]`，把开闭引号混在一个字符类里，
# 于是 markdown 正文里的普通括号、跨行文本都被抓成「引文」——90 条候选里
# 混进大量 `),\n不是那天的池子` 这类碎片，"85 条库内无"的结论因此不可信。
# 改成逐种引号各配一条规则，并用 [^\n] 限制不跨行。
QUOTE_PATTERNS = (
    re.compile(r"「([^「」\n]{10,80})」"),
    re.compile(r'"([^"\n]{10,80})"'),
    re.compile(r"“([^“”\n]{10,80})”"),
)


def extract_quotes(text: str) -> list[tuple[int, str]]:
    """逐行抽引文，返回 [(行号, 引文)]。只收含 ≥8 个汉字的片段。"""
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for lineno, line in enumerate(text.splitlines(), 1):
        # markdown 表格行里的引号多是示例值，不当引文
        if line.count("|") >= 3:
            continue
        for pattern in QUOTE_PATTERNS:
            for match in pattern.finditer(line):
                quote = match.group(1).strip()
                if len(CJK.findall(quote)) < 8:
                    continue
                if quote in seen:
                    continue
                seen.add(quote)
                found.append((lineno, quote))
    return found


def flatten(text: str) -> str:
    return WS.sub("", text)


def load_source(db: sqlite3.Connection, source_id: str) -> list[tuple[str, str, str]]:
    """返回 [(chunk_id, locator, 去空白正文)]。"""
    rows = db.execute(
        "SELECT chunk_id, locator, text FROM chunks WHERE source_id=?", (source_id,)
    ).fetchall()
    return [(r[0], r[1], flatten(r[2])) for r in rows]


def probe_of(quote: str) -> str:
    """取引文里最长的连续汉字片段，最多 14 字。"""
    runs = CJK.findall(flatten(quote))
    joined = "".join(runs)
    return joined[:14]


def check(quote: str, corpus: list[tuple[str, str, str]]) -> tuple[str, str, str]:
    """返回 (判定, chunk_id, locator)。"""
    flat = flatten(quote)
    for cid, loc, body in corpus:
        if flat and flat in body:
            return "已回溯", cid, loc
    probe = probe_of(quote)
    if len(probe) >= 8:
        for cid, loc, body in corpus:
            if probe in body:
                return "片段回溯", cid, loc
    # 再退一档：前 8 字
    if len(probe) >= 8:
        short = probe[:8]
        for cid, loc, body in corpus:
            if short in body:
                return "短片段回溯", cid, loc
    return "库内无", "", ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", choices=sorted(SKILLS) + ["all"], default="all")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not DB.exists():
        print(f"索引不存在：{DB}", file=sys.stderr)
        return 1
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    targets = sorted(SKILLS) if args.skill == "all" else [args.skill]
    report: dict[str, dict] = {}

    for name in targets:
        path, source_id = SKILLS[name]
        if not path.exists():
            print(f"跳过 {name}：{path} 不存在")
            continue
        text = path.read_text(encoding="utf-8")
        corpus = load_source(db, source_id)
        quotes = extract_quotes(text)

        verdicts: list[dict] = []
        for line, quote in quotes:
            verdict, cid, loc = check(quote, corpus)
            verdicts.append(
                {"line": line, "quote": quote, "verdict": verdict,
                 "chunk_id": cid, "locator": loc}
            )

        counts: dict[str, int] = {}
        for v in verdicts:
            counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
        report[name] = {"source_id": source_id, "corpus_chunks": len(corpus),
                        "quotes": len(quotes), "counts": counts, "detail": verdicts}

        if not args.json:
            print("=" * 66)
            print(f"{name}  （对照来源 {source_id}，{len(corpus)} 块）")
            print(f"引文候选 {len(quotes)} 条 -> {counts}")
            print()
            for v in verdicts:
                if v["verdict"] == "库内无":
                    print(f"  ✗ L{v['line']:<5} {v['quote'][:52]}")
            print()
            for v in verdicts:
                if v["verdict"] != "库内无":
                    print(f"  ✓ L{v['line']:<5} {v['quote'][:34]} -> {v['chunk_id']} {v['locator'][:22]}")
            print()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
