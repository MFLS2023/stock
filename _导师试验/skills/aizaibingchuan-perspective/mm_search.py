#!/usr/bin/env python3
"""心智模型提取用的检索工具（只读）。

用法：
    python mm_search.py 关键词1 关键词2 ...        # 在「真应答」的答句里搜
    python mm_search.py --scope all 关键词         # 全体裁正文搜
    python mm_search.py --verify "片段"            # instr 校验某条引文
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys

DB = "file:C:/Users/20577/Documents/炒股/知识库/_知识库系统/indexes/knowledge.db?mode=ro"

PAIR_RE = re.compile(r"问：(.*?)\s*答：(.*?)(?=问：|$)", re.S)
PLACEHOLDER_Q = re.compile(r"^[\s\w川哥神大爱看兄老师，,。\.]*借楼[\s，,。\.！!~]*$")
LOG_A = re.compile(r"^\s*(?:\d{1,2}[.:：]\d{1,2}\s*)?(?:早盘|午盘|尾盘|盘中|竞价)?借楼")


def is_log(q: str, a: str) -> bool:
    return bool(PLACEHOLDER_Q.match(q.strip()) or LOG_A.match(a.strip()))


def conn():
    return sqlite3.connect(DB, uri=True)


def search_qa(kws, limit, window):
    db = conn()
    rows = db.execute(
        "SELECT chunk_id, date, text FROM chunks "
        "WHERE source_id='aizaibingchuan' AND chunk_type='qa_reply' ORDER BY date"
    ).fetchall()
    hits = 0
    for cid, date, text in rows:
        for q, a in PAIR_RE.findall(text):
            if is_log(q, a):
                continue
            if all(k in a for k in kws):
                hits += 1
                if hits > limit:
                    print(f"...(还有更多，已截断于 {limit})")
                    return hits
                i = a.find(kws[0])
                seg = a[max(0, i - window): i + window]
                print(f"[{hits}] {cid} {date}")
                print(f"  问：{q.strip()[:120]}")
                print(f"  答：{seg.strip()}")
                print()
    print(f"== 命中 {hits} 组问答（真应答，答句内） ==")
    return hits


def search_all(kws, limit, window, types=None):
    db = conn()
    sql = ("SELECT chunk_id, date, chunk_type, text FROM chunks "
           "WHERE source_id='aizaibingchuan'")
    if types:
        sql += " AND chunk_type IN (%s)" % ",".join("'%s'" % t for t in types)
    sql += " ORDER BY date"
    hits = 0
    for cid, date, ct, text in db.execute(sql):
        if all(k in text for k in kws):
            hits += 1
            if hits > limit:
                print(f"...(截断于 {limit})")
                break
            i = text.find(kws[0])
            print(f"[{hits}] {cid} {date} {ct}")
            print(f"  {text[max(0,i-window): i+window].strip()}")
            print()
    print(f"== 命中 {hits} 块 ==")
    return hits


def count_by_type(kw):
    db = conn()
    for r in db.execute(
        "SELECT chunk_type, count(*) FROM chunks WHERE source_id='aizaibingchuan' "
        "AND instr(text, ?)>0 GROUP BY 1 ORDER BY 2 DESC", (kw,)
    ):
        print(f"  {r[0]:<15} {r[1]}")
    n = db.execute("SELECT count(*) FROM chunks WHERE source_id='aizaibingchuan' "
                   "AND instr(text, ?)>0", (kw,)).fetchone()[0]
    print(f"  合计 {n}")


def verify(frag):
    db = conn()
    rows = db.execute(
        "SELECT chunk_id, date, chunk_type FROM chunks WHERE source_id='aizaibingchuan' "
        "AND instr(text, ?)>0", (frag,)).fetchall()
    if rows:
        print("OK", len(rows), [r[0] for r in rows[:5]])
    else:
        print("FAIL 库里查不到:", frag[:60])
    return bool(rows)


def show(cid, start=0, length=4000):
    db = conn()
    r = db.execute("SELECT date, chunk_type, title, locator, text FROM chunks "
                   "WHERE chunk_id=?", (cid,)).fetchone()
    if not r:
        print("no such chunk", cid)
        return
    print(f"### {cid} | {r[0]} | {r[1]} | {r[2]} | {r[3]}")
    print(r[4][start:start + length])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("kws", nargs="*")
    p.add_argument("--scope", default="qa", choices=["qa", "all"])
    p.add_argument("--types", default=None)
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--window", type=int, default=260)
    p.add_argument("--verify", default=None)
    p.add_argument("--count", default=None)
    p.add_argument("--show", default=None)
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--len", type=int, default=4000)
    a = p.parse_args()
    if a.verify:
        return 0 if verify(a.verify) else 1
    if a.count:
        count_by_type(a.count)
        return 0
    if a.show:
        show(a.show, a.start, a.len)
        return 0
    types = a.types.split(",") if a.types else None
    if a.scope == "qa":
        search_qa(a.kws, a.limit, a.window)
    else:
        search_all(a.kws, a.limit, a.window, types)
    return 0


if __name__ == "__main__":
    sys.exit(main())
