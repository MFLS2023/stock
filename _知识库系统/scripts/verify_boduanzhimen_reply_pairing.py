# -*- coding: utf-8 -*-
"""用审查报告 P1-6 的原始复现脚本验证隔离产物：共用同一提问的条数应清零。

判据与审查报告完全一致（同文档内多条回复共用同一 question），
只是数据源换成隔离目录的产物。
"""
import json
import pathlib
import re
import sys
from collections import Counter, defaultdict

ROOT = pathlib.Path(r"C:\Users\20577\Documents\炒股\知识库\_知识库系统")
PAT = re.compile(r"^读者问[:：](.+?)\n作者答[:：](.+)$", re.S)


def audit(chunks_path: pathlib.Path, label: str) -> None:
    rows = [json.loads(l) for l in chunks_path.open(encoding="utf-8")]
    replies = [r for r in rows if r["chunk_type"] == "author_reply"]
    by_doc = defaultdict(list)
    with_q = 0
    for item in replies:
        match = PAT.match(item["text"])
        if match:
            by_doc[item["document_id"]].append(match.group(1).strip())
            with_q += 1
    dup = sum(sum(v - 1 for v in Counter(qs).values() if v > 1) for qs in by_doc.values())
    docs = sum(1 for qs in by_doc.values() if any(v > 1 for v in Counter(qs).values()))
    pct = dup / with_q * 100 if with_q else 0
    print(f"{label}")
    print(f"  author_reply {len(replies)} 条，其中带提问 {with_q} 条")
    print(f"  共用同一提问：{dup} 条 ({pct:.1f}%)，涉及 {docs} 篇")
    if by_doc:
        worst_doc, questions = max(by_doc.items(),
                                   key=lambda kv: max(Counter(kv[1]).values(), default=0))
        top = Counter(questions).most_common(1)[0]
        print(f"  最严重：{worst_doc} 一条留言被 {top[1]} 条回复共用")
    return dup


# 正式目录已经是修复后的产物，所以「修复前」必须读备份，
# 否则两边读同一份文件，输出会退化成 0 → 0 看不出对照。
BACKUP = pathlib.Path(r"C:\Users\20577\AppData\Local\Temp\bdzm_jsonl_bak\chunks.jsonl")
old = audit(BACKUP, "【修复前｜备份产物】") if BACKUP.exists() else None
if old is None:
    print(f"缺备份 {BACKUP}，只验证当前状态")
print()
new = audit(ROOT / "source_libraries" / "boduanzhimen" / "chunks.jsonl", "【修复后｜正式目录】")
print()
print(f"错配 {old if old is not None else '?'} → {new}")
sys.exit(0 if new == 0 else 1)
