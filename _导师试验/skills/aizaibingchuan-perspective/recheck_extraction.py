#!/usr/bin/env python3
"""把 extraction/*.md 里所有 「」 引文自动抽出来重查一遍。

规则：一个引文片段归属于它前面最近出现的 chunk_id。
用 chunk_id + instr(text, 片段) 双重校验。含故意插入的假哨兵可验证脚本本身有效。
"""
from __future__ import annotations

import pathlib
import re
import sqlite3
import sys

DB = "file:C:/Users/20577/Documents/炒股/知识库/_知识库系统/indexes/knowledge.db?mode=ro"
CID_RE = re.compile(r"(azbcx?-[0-9a-f]{12}-[pq]\d{3}-c\d{2})")
QUOTE_RE = re.compile(r"「([^「」]{6,})」")
BOLD_RE = re.compile(r"\*\*")


def main() -> int:
    files = [pathlib.Path(p) for p in sys.argv[1:]]
    db = sqlite3.connect(DB, uri=True)
    ok = bad = skipped = 0
    for f in files:
        print(f"\n===== {f.name} =====")
        cur_cid = None
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for m in CID_RE.finditer(line):
                cur_cid = m.group(1)
            for q in QUOTE_RE.finditer(line):
                raw = q.group(1)
                # 去掉 markdown 加粗标记和续行符，取最长的连续可查片段
                frag = BOLD_RE.sub("", raw).strip()
                if not frag or cur_cid is None:
                    skipped += 1
                    continue
                # 引文常跨行用 … / …… 省略，按分隔符切成子串逐个查
                parts = [p.strip(" ／/、") for p in re.split(r"…+|\s*/\s*", frag)]
                parts = [p for p in parts if len(p) >= 6]
                if not parts:
                    skipped += 1
                    continue
                for p in parts:
                    row = db.execute(
                        "SELECT instr(text, ?) FROM chunks WHERE chunk_id=?",
                        (p, cur_cid),
                    ).fetchone()
                    if row is None:
                        print(f"  NO_CHUNK L{lineno} {cur_cid} | {p[:44]}")
                        bad += 1
                    elif row[0] == 0:
                        print(f"  MISS     L{lineno} {cur_cid} | {p[:44]}")
                        bad += 1
                    else:
                        ok += 1
    print(f"\n== 校验通过 {ok} / 失败 {bad} / 跳过 {skipped} ==")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
