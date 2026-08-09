#!/usr/bin/env python3
"""对账并刷新回归样本基线快照 `indexes/regression_baseline.json`。

## 这个脚本存在的理由

回归基线原来写死在 `test_query_kb.py` 里（1470/525/1181 等十几处）。
问题不是"数字会变"，而是**合法的数据变动只能通过编辑测试代码来修**，
于是「改基线」和「改测试逻辑」混在同一个 diff 里，审阅时分不清哪个是哪个。

外置后：数据变动只改 JSON，测试代码不动，JSON 的 diff 就是一份变更账。

## 用法

    python refresh_regression_baseline.py            # 只对账，列出差异（默认）
    python refresh_regression_baseline.py --write     # 写入新值（需自己补 history 的 why）

⚠️ `--write` 不会替你写 history。**没有 why 的基线更新等于取消这项保护** ——
写完记得手动往 history 追加一条，说明这次变动是什么、为什么是合法的。

用系统 Python（有 yaml，虽然本脚本不需要）或 codex 运行时都可以，只读数据库。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
DATABASE = ROOT / "_知识库系统" / "indexes" / "knowledge.db"
BASELINE = ROOT / "_知识库系统" / "indexes" / "regression_baseline.json"


def measure(db: sqlite3.Connection, sources: list[str]) -> dict:
    """按当前索引测出全部基线值。口径必须与 test_query_kb.py 里的查询完全一致。"""
    placeholders = ",".join("?" for _ in sources)

    row_counts = {}
    for table in ("chunks", "parents", "documents"):
        rows = db.execute(
            f"SELECT source_id, count(*) FROM {table} "
            f"WHERE source_id IN ({placeholders}) GROUP BY 1",
            sources,
        ).fetchall()
        row_counts[table] = {source: count for source, count in rows}

    def prose(term: str) -> int:
        return db.execute(
            f"SELECT count(*) FROM chunks WHERE source_id IN ({placeholders}) AND text LIKE ?",
            (*sources, f"%{term}%"),
        ).fetchone()[0]

    def union_ids(term: str) -> set[str]:
        return {
            row[0]
            for row in db.execute(
                f"SELECT chunk_id FROM chunks WHERE source_id IN ({placeholders}) "
                f"AND (text LIKE ? OR title LIKE ? OR author LIKE ?)",
                (*sources, *[f"%{term}%"] * 3),
            )
        }

    def prose_ids(term: str) -> set[str]:
        return {
            row[0]
            for row in db.execute(
                f"SELECT chunk_id FROM chunks WHERE source_id IN ({placeholders}) AND text LIKE ?",
                (*sources, f"%{term}%"),
            )
        }

    existing = json.loads(BASELINE.read_text(encoding="utf-8"))
    return {
        "row_counts": row_counts,
        "prose_matches": {t: prose(t) for t in existing["prose_matches"]},
        "title_only_extra": {
            t: len(union_ids(t) - prose_ids(t)) for t in existing["title_only_extra"]
        },
        "recall_union": {t: len(union_ids(t)) for t in existing["recall_union"]},
        "topics_filled": db.execute(
            f"SELECT count(*) FROM chunks WHERE source_id IN ({placeholders}) AND topics != ''",
            sources,
        ).fetchone()[0],
    }


def diff_report(old: dict, new: dict) -> list[str]:
    """逐项比对，返回差异描述。空列表表示完全一致。"""
    lines: list[str] = []

    for table, counts in new["row_counts"].items():
        previous = old["row_counts"].get(table, {})
        for source in sorted(set(counts) | set(previous)):
            was, now = previous.get(source), counts.get(source)
            if was != now:
                lines.append(f"  row_counts.{table}.{source}: {was} -> {now}")

    for section in ("prose_matches", "title_only_extra", "recall_union"):
        for term in sorted(set(new[section]) | set(old.get(section, {}))):
            was, now = old.get(section, {}).get(term), new[section].get(term)
            if was != now:
                lines.append(f"  {section}.{term}: {was} -> {now}")

    if old.get("topics_filled") != new["topics_filled"]:
        lines.append(f"  topics_filled: {old.get('topics_filled')} -> {new['topics_filled']}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="把实测值写回快照（history 需自己补）")
    args = parser.parse_args()

    if not DATABASE.exists():
        print(f"索引不存在：{DATABASE}", file=sys.stderr)
        return 1
    if not BASELINE.exists():
        print(f"基线快照不存在：{BASELINE}", file=sys.stderr)
        return 1

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    db = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)
    measured = measure(db, list(baseline["sources"]))

    differences = diff_report(baseline, measured)
    if not differences:
        print(f"✓ 索引与基线快照一致（样本 {'/'.join(baseline['sources'])}）")
        return 0

    print(f"发现 {len(differences)} 处差异：")
    for line in differences:
        print(line)
    print()

    if not args.write:
        print("这是对账模式。确认这些变动合法后，用 --write 写入，")
        print("**并往 history 追加一条说明 why** —— 没有 why 的更新等于取消这项保护。")
        return 1

    baseline.update(measured)
    BASELINE.write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已写入 {BASELINE}")
    print("⚠️ 别忘了往 history 追加一条，写明这次变动是什么、为什么合法。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
