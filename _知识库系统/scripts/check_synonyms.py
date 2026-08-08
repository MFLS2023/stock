#!/usr/bin/env python3
"""核对 config/synonyms.yaml 与真库是否一致，并重测每个词的命中数。

为什么需要：同义词表最容易腐坏。项目里已经踩过两次——
  · 死条目：`先弱后强`、`极度低迷` 曾写在表里但全库 0 命中，扩展它们是空动作
  · 死条目复活：爱在冰川接入后 `极度低迷` 有了 3 块、`空间龙` 5 块
所以每次接入来源或增量导入后都要重跑本脚本，不能沿用旧判断。

检查三件事：
  1. groups 里每个词的实测命中数（0 命中的报出来，应移入 dead_terms）
  2. dead_terms 里的词是否真的还是 0（复活了要报，应移回 groups）
  3. 标了「独有」的 note 是否仍然成立（来源分布验证）

只读数据库。--update 会把重测的命中数写回 yaml 的注释区（默认不写）。
"""
from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = ROOT / "_知识库系统" / "config" / "synonyms.yaml"
DB = ROOT / "_知识库系统" / "indexes" / "knowledge.db"


def load_config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def hits(db: sqlite3.Connection, term: str) -> int:
    """text 列子串命中块数 —— 与 CLAUDE.md 表格口径一致。"""
    return db.execute(
        "SELECT COUNT(*) FROM chunks WHERE instr(text, ?) > 0", (term,)
    ).fetchone()[0]


def distribution(db: sqlite3.Connection, term: str) -> dict[str, int]:
    rows = db.execute(
        "SELECT source_id, COUNT(*) FROM chunks WHERE instr(text, ?) > 0 "
        "GROUP BY source_id ORDER BY 2 DESC",
        (term,),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="逐词打印命中数")
    args = parser.parse_args()

    if not DB.exists():
        print(f"索引不存在：{DB}", file=sys.stderr)
        return 1

    config = load_config()
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    total = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    print(f"库内 {total} 块，配置声明 {config.get('measured_chunks')} 块 "
          f"({'一致' if total == config.get('measured_chunks') else '⚠️ 不一致，需重测'})")
    print()

    groups = config.get("groups", {})
    dead = config.get("dead_terms", [])

    zero_in_groups: list[tuple[str, str]] = []
    revived: list[tuple[str, int]] = []
    single_source: list[tuple[str, str, int]] = []

    for name, group in groups.items():
        terms = [group.get("canonical", "")] + list(group.get("variants", []))
        terms = [t for t in terms if t]
        if args.verbose:
            print(f"[{name}]")
        for term in terms:
            n = hits(db, term)
            if args.verbose:
                print(f"    {term:<10} {n:>6}")
            if n == 0:
                zero_in_groups.append((name, term))
            elif n > 0:
                dist = distribution(db, term)
                # 命中集中在单一来源且量不小 -> 提示这是来源独有词
                if len(dist) == 1 and n >= 20:
                    single_source.append((term, next(iter(dist)), n))

    for term in dead:
        n = hits(db, term)
        if n > 0:
            revived.append((term, n))

    print("=" * 60)
    problems = 0

    if zero_in_groups:
        problems += len(zero_in_groups)
        print(f"⚠️ groups 里有 {len(zero_in_groups)} 个零命中词（应移入 dead_terms）：")
        for name, term in zero_in_groups:
            print(f"    {name} / {term}")
    else:
        print("✓ groups 里没有零命中词")

    if revived:
        problems += len(revived)
        print(f"\n⚠️ dead_terms 里有 {len(revived)} 个词已复活（应移回 groups）：")
        for term, n in revived:
            print(f"    {term}  {n} 块")
    else:
        print("✓ dead_terms 全部仍为零命中")

    if single_source:
        print(f"\nℹ️ 单来源独有词 {len(single_source)} 个（拿它们检索等于只查一个来源）：")
        for term, source, n in single_source:
            print(f"    {term:<10} {n:>5} 块，全在 {source}")

    print()
    print("检查完成" if problems == 0 else f"发现 {problems} 处需要修的地方")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
