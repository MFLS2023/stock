# -*- coding: utf-8 -*-
"""逐条核对 boduanzhimen-perspective/SKILL.md 里的可验证数字。

留档文件明确要求「恢复工作时第一步必须重测，不要直接引用文档里的数字」，
所以这个脚本是对账工具：SKILL.md 写什么、库里实测什么、一致与否。
"""
import pathlib
import re
import sqlite3
import sys

ROOT = pathlib.Path(r"C:\Users\20577\Documents\炒股\知识库\_知识库系统")
CJK_DIR = pathlib.Path(r"C:\Users\20577\Neverflandre\wechatDownload4.6\下载\波段之门")
db = sqlite3.connect(ROOT / "indexes" / "knowledge.db")
WHERE = "source_id='boduanzhimen'"


def one(sql: str, *args) -> int:
    return db.execute(sql, args).fetchone()[0]


def blocks(word: str, chunk_type: str | None = None) -> int:
    if chunk_type:
        return one(f"SELECT COUNT(*) FROM chunks WHERE {WHERE} AND chunk_type=? "
                   "AND text LIKE ?", chunk_type, f"%{word}%")
    return one(f"SELECT COUNT(*) FROM chunks WHERE {WHERE} AND text LIKE ?", f"%{word}%")


rows: list[tuple[str, int, int]] = []       # (项目, SKILL 写的, 实测)

# 块类型分布
rows.append(("总块数", 8082, one(f"SELECT COUNT(*) FROM chunks WHERE {WHERE}")))
for name, expected in [("author_reply", 4984), ("article_body", 1909),
                       ("paid_article", 542), ("chart_ocr", 628),
                       ("indicator_formula", 19)]:
    rows.append((f"chunk_type={name}", expected,
                 one(f"SELECT COUNT(*) FROM chunks WHERE {WHERE} AND chunk_type=?", name)))

# author_reply 三种形态（审查报告 P1-6 修复后的分布）。
# 前缀是块文本的开头，SKILL.md 那张表直接引用这三个数。
for prefix, expected in [("读者问", 4337), ("作者续言", 641), ("作者留言", 6)]:
    rows.append((f"author_reply·{prefix}", expected,
                 one(f"SELECT COUNT(*) FROM chunks WHERE {WHERE} "
                     "AND chunk_type='author_reply' AND text LIKE ?", f"{prefix}%")))

# 文档数
rows.append(("登记文档", 849, one(f"SELECT COUNT(*) FROM documents WHERE {WHERE}")))
rows.append(("有块的文档", 845,
             one(f"SELECT COUNT(DISTINCT document_id) FROM chunks WHERE {WHERE}")))

# 模型2 多周期（正文块口径 / 全部块口径）
for word, body_expected, all_expected in [("日线", 240, 431), ("周线", 163, 288),
                                          ("月线", 70, 139), ("季线", 10, 18),
                                          ("分时", 92, 142)]:
    rows.append((f"{word}（正文块）", body_expected, blocks(word, "article_body")))
    rows.append((f"{word}（全部块）", all_expected, blocks(word)))

# 模型4 仓位 / 打折使用 板块
rows.append(("仓位（正文）", 291, blocks("仓位", "article_body")))
rows.append(("仓位（回复）", 136, blocks("仓位", "author_reply")))
rows.append(("仓位（付费）", 53, blocks("仓位", "paid_article")))
rows.append(("仓位（图内）", 24, blocks("仓位", "chart_ocr")))
rows.append(("仓位（全部）", 504, blocks("仓位")))
rows.append(("板块（正文）", 456, blocks("板块", "article_body")))
rows.append(("板块（全部）", 857, blocks("板块")))

# 模型3 相对强度
rows.append(("INDEXC（公式文件）", 4, blocks("INDEXC", "indicator_formula")))
rows.append(("INDEXC（全部块）", 16, blocks("INDEXC")))
rows.append(("INDEXC（涉及文档）", 11,
             one(f"SELECT COUNT(DISTINCT document_id) FROM chunks WHERE {WHERE} "
                 "AND text LIKE '%INDEXC%'")))

# 瑞的那篇混入文章
paid = "boduanzhimen-paid-8b768011e090"
rows.append(("瑞文块数", 9, one("SELECT COUNT(*) FROM chunks WHERE document_id=?", paid)))
rows.append(("分时承接（全库）", 6, blocks("分时承接")))
rows.append(("分时承接（在瑞文）", 6,
             one("SELECT COUNT(*) FROM chunks WHERE document_id=? AND text LIKE '%分时承接%'",
                 paid)))
rows.append(("周线级别（在瑞文）", 0,
             one("SELECT COUNT(*) FROM chunks WHERE document_id=? AND text LIKE '%周线级别%'",
                 paid)))

# 误解节词频：口径是原始 md 全文的篇数
if CJK_DIR.exists():
    texts = []
    for path in sorted(CJK_DIR.rglob("*.md")):
        try:
            texts.append(path.read_text(encoding="utf-8"))
        except OSError:
            pass
    rows.append(("原始 md 篇数", 723, len(texts)))
    for word, expected in [("底部", 286), ("顶部", 205), ("周期", 201), ("结构", 180),
                           ("分时", 129), ("均线", 110), ("波浪", 47), ("艾略特", 8),
                           ("推动浪", 0)]:
        rows.append((f"md 全文·{word}", expected, sum(1 for t in texts if word in t)))

bad = 0
print(f"{'项目':<24s}{'SKILL':>8s}{'实测':>8s}  ")
for name, expected, actual in rows:
    mark = "OK" if expected == actual else "DIFF"
    if expected != actual:
        bad += 1
    print(f"{name:<24s}{expected:>8d}{actual:>8d}  {mark}")

print(f"\n{len(rows) - bad}/{len(rows)} 项一致")
sys.exit(1 if bad else 0)
