#!/usr/bin/env python3
"""把 qa_reply 里的「真应答」和「借楼日志」分开，避免把盘中日志当成对话。

背景：qa_reply 2376 块是本项目的主力素材（读者提出具体质疑时作者必须解释判断依据，
比写文章更能暴露思维方式）。但实测发现 963 块含「借楼」，其中大量是这种形态：

    问：川哥借楼
    答：午盘借楼再加一条，下午没有新热点的时候，老妖股也会是市场关注的目标…

这里的「答」不是在回答读者，是作者借读者的楼层发**盘中日志**。
把它当对话素材提取，会得出「他答非所问」的错误结论。

分类规则（按优先级）：
  1. 问句本身就是占位（「借楼」「XX借楼」）        -> 日志
  2. 答句以时间戳或借楼开头（「午盘借楼」「14.04借楼」） -> 日志
  3. 其余                                    -> 真应答

只读数据库，输出统计与样本，不改任何数据。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
DB = ROOT / "_知识库系统" / "indexes" / "knowledge.db"

PAIR_RE = re.compile(r"问：(.*?)\s*答：(.*?)(?=问：|$)", re.S)

# 问句是纯占位：只有「借楼」及少量修饰
PLACEHOLDER_Q = re.compile(r"^[\s\w川哥神大爱看兄老师，,。\.]*借楼[\s，,。\.！!~]*$")
# 答句开头是借楼或时间戳（14.04借楼 / 午盘借楼 / 10.06借楼）
LOG_A = re.compile(r"^\s*(?:\d{1,2}[.:：]\d{1,2}\s*)?(?:早盘|午盘|尾盘|盘中)?借楼")


def classify(question: str, answer: str) -> str:
    if PLACEHOLDER_Q.match(question.strip()):
        return "日志"
    if LOG_A.match(answer.strip()):
        return "日志"
    return "应答"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=3, help="每类打印几个样本")
    args = parser.parse_args()

    if not DB.exists():
        print(f"索引不存在：{DB}", file=sys.stderr)
        return 1
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    counts = {"应答": 0, "日志": 0}
    samples: dict[str, list[tuple[str, str, str]]] = {"应答": [], "日志": []}
    blocks_with_log = 0
    total_blocks = 0

    rows = db.execute(
        "SELECT chunk_id, text FROM chunks "
        "WHERE source_id='aizaibingchuan' AND chunk_type='qa_reply'"
    ).fetchall()

    for chunk_id, text in rows:
        total_blocks += 1
        has_log = False
        for question, answer in PAIR_RE.findall(text):
            kind = classify(question, answer)
            counts[kind] += 1
            if kind == "日志":
                has_log = True
            if len(samples[kind]) < args.samples:
                samples[kind].append((chunk_id, question.strip()[:40], answer.strip()[:90]))
        if has_log:
            blocks_with_log += 1

    total_pairs = sum(counts.values())
    print(f"qa_reply 块数 {total_blocks}，解析出问答对 {total_pairs}")
    print()
    print("【分类结果】")
    for kind in ("应答", "日志"):
        n = counts[kind]
        pct = n / total_pairs * 100 if total_pairs else 0
        print(f"    {kind}  {n:>6} 对  {pct:>5.1f}%")
    print()
    print(f"含至少一条借楼日志的块：{blocks_with_log} / {total_blocks} "
          f"({blocks_with_log / total_blocks * 100:.1f}%)")
    print()

    for kind in ("应答", "日志"):
        print(f"【{kind} 样本】")
        for chunk_id, question, answer in samples[kind]:
            print(f"  {chunk_id}")
            print(f"     问：{question}")
            print(f"     答：{answer}")
        print()

    print("提取对话维度素材时只用「应答」类；「日志」类归到决策记录维度"
          "（它是盘中实时想法，有独立价值，但不是对话）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
