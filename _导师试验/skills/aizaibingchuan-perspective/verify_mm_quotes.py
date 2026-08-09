#!/usr/bin/env python3
"""逐条校验 mental-models.md / decision-heuristics.md 里要用的引文片段。

用法：python verify_mm_quotes.py < frags.txt
每行格式：chunk_id<TAB>片段
校验两件事：① chunk_id 存在 ② 片段能在该 chunk 的 text 里 instr 命中
"""
from __future__ import annotations

import sqlite3
import sys

DB = "file:C:/Users/20577/Documents/炒股/知识库/_知识库系统/indexes/knowledge.db?mode=ro"


def main() -> int:
    db = sqlite3.connect(DB, uri=True)
    ok = bad = 0
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cid, _, frag = line.partition("\t")
        cid, frag = cid.strip(), frag.strip()
        if not frag:
            continue
        row = db.execute(
            "SELECT instr(text, ?) FROM chunks WHERE chunk_id=?", (frag, cid)
        ).fetchone()
        if row is None:
            print(f"NO_CHUNK  {cid}  | {frag[:50]}")
            bad += 1
        elif row[0] == 0:
            # 报告在别的块里有没有
            other = db.execute(
                "SELECT chunk_id FROM chunks WHERE source_id='aizaibingchuan' "
                "AND instr(text, ?)>0 LIMIT 3", (frag,)
            ).fetchall()
            print(f"MISS      {cid}  | {frag[:50]}  -> 别处: {[o[0] for o in other]}")
            bad += 1
        else:
            ok += 1
    print(f"\n== OK {ok} / MISS+NO_CHUNK {bad} ==")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
