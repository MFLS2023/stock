#!/usr/bin/env python3
"""把方法卡里的 user_questions 拆成检索词，逐个探库，判断答案在不在库里。

要回答的问题只有一个：用户这 16 条「怎么判断 / 怎么执行」的疑惑，
是**库里没人讲过**（该加来源），还是**讲过但检索不到**（该上语义检索）。

这两种诊断的修法完全不同，不能靠猜。做法：
  1. 从每条疑惑里抽出名词性关键概念（去掉「怎么」「呢」这类疑问词）
  2. 对每个概念跑三档探针：原词、原词的同义扩展、概念的上位词
  3. 召回为 0 或极少 -> 倾向「没讲过」；召回多但读起来不答题 -> 倾向「搜不到」

输出 reports/未解问题探库结果.md，供人工判读。
只读数据库，不写任何数据。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sqlite3
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
METHODS = ROOT / "_知识库系统" / "source_libraries" / "fulibei" / "methods.jsonl"
DB = ROOT / "_知识库系统" / "indexes" / "knowledge.db"
OUT = ROOT / "_知识库系统" / "reports" / "未解问题探库结果.md"

# 疑问词与语气词，抽概念时要剔掉
STOP = (
    "怎么", "如何", "什么", "为什么", "呢", "吗", "的", "了", "又", "还", "是否",
    "知道", "判断", "算", "能不能", "可不可以", "需要", "但是", "其次", "以及",
    "不过", "考虑", "补充", "就是", "我", "你", "他", "这个", "那个", "自己",
    "到底", "确实", "或者", "还是", "如果", "并且", "而且", "所以", "因为",
)

# 手工维护的同义扩展。CLAUDE.md 记录过：同一概念在不同来源用词不同，
# 且有些同义词是死条目（先弱后强、极度低迷全库 0 命中），所以这里只放实测有命中的。
SYNONYMS = {
    "情绪核心": ["人气核心", "核心票", "龙头", "总龙头"],
    "容量核心": ["容量标", "大票", "权重", "趋势股"],
    "拐点": ["转折", "变盘", "反包", "冰点", "启动点"],
    "增量资金": ["增量", "资金承接", "场外资金", "放量"],
    "高位震荡": ["高位分歧", "分歧", "震荡", "第四阶段"],
    "买卖计划": ["预案", "推演", "计划", "预期"],
    "梯队": ["高度", "空间板", "连板梯队"],
    "辨识度": ["人气", "认可度", "前排"],
    "模式内": ["模式", "体系", "纪律"],
    "大小题材": ["大题材", "题材级别", "主线"],
    "边际强化": ["强化", "加速", "扩散"],
    "空仓": ["被动空仓", "等待", "无机会"],
    "周期节点": ["节点", "阶段", "十字路口"],
}


# 每张卡的疑惑对应哪些交易概念 —— 人工指定，不靠自动抽词。
#
# 为什么不自动抽：试过「删停用词 + 正则切 2-5 字中文」，产出的是「谁是情绪核」
# 「有没有增量」「是不是高位」这类被切断的碎片。这些词召回恒为 1（命中的正是
# 卡里那条疑惑本身），证明不了库里有没有答案，是纯噪声。
# 中文没有空格分词，靠删词切不出概念，只能人工列。
CARD_CONCEPTS: dict[str, list[str]] = {
    "fulibei-method-002": ["周期节点", "退潮", "新周期", "试错"],
    "fulibei-method-003": ["情绪核心", "容量核心", "高位股", "大票"],
    "fulibei-method-004": ["拐点", "反包", "冰点", "跟随"],
    "fulibei-method-005": ["增量资金", "承接", "高位震荡", "退潮"],
    "fulibei-method-006": ["大小题材", "题材级别", "主线", "话题"],
    "fulibei-method-007": ["板块阶段", "边际强化", "梯队", "复盘"],
    "fulibei-method-008": ["超预期", "预期差", "竞价", "大单"],
    "fulibei-method-009": ["辨识度", "前排", "人气", "核心"],
    "fulibei-method-010": ["买卖计划", "卖点", "预案", "推演"],
    "fulibei-method-011": ["模式内", "交割单", "复盘", "运气"],
    "fulibei-method-012": ["交割单", "模式验证", "胜率", "统计"],
    "fulibei-method-013": ["空仓", "等待", "买点", "预选"],
    "fulibei-method-015": ["交易系统", "主干", "小规律", "体系"],
    "fulibei-method-018": ["推演", "退出", "止损", "违背"],
    "fulibei-method-019": ["资金体量", "流动性", "滑点", "半路"],
    "fulibei-method-020": ["心态", "认知", "预案", "恐惧"],
}


def concepts(method_id: str) -> list[str]:
    """取这张卡的概念词。没登记的返回空，宁可漏也不产噪声。"""
    return CARD_CONCEPTS.get(method_id, [])


def recall_count(db: sqlite3.Connection, term: str) -> int:
    """三字段并集召回数——与 SPEC 的召回口径一致。"""
    sql = (
        "SELECT COUNT(DISTINCT chunk_id) FROM chunks WHERE "
        "instr(lower(text), lower(?))>0 OR instr(lower(title), lower(?))>0 "
        "OR instr(lower(author), lower(?))>0"
    )
    return db.execute(sql, (term, term, term)).fetchone()[0]


def per_source(db: sqlite3.Connection, term: str) -> dict[str, int]:
    rows = db.execute(
        "SELECT source_id, COUNT(*) FROM chunks WHERE instr(lower(text), lower(?))>0 "
        "GROUP BY source_id ORDER BY 2 DESC",
        (term,),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-hits", type=int, default=5,
                        help="召回低于此数视为「库里几乎没讲」")
    args = parser.parse_args()

    if not DB.exists():
        print(f"索引不存在：{DB}", file=sys.stderr)
        return 1

    cards = [json.loads(l) for l in METHODS.read_text(encoding="utf-8").splitlines() if l.strip()]
    with_q = [c for c in cards if c.get("user_questions")]
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    lines = [
        "# 方法卡未解问题 · 探库结果",
        "",
        "> 由 `scripts/probe_user_questions.py` 生成。",
        "> 目的：判断用户这些「怎么判断 / 怎么执行」的疑惑，是**库里没人讲过**",
        "> （该加来源）还是**讲过但检索不到**（该上语义检索）。两者修法完全不同。",
        "",
        "> 判读：召回数少 = 倾向「没讲过」；召回数多但读原文不答题 = 倾向「搜不到」。",
        f"> 阈值：召回 < {args.min_hits} 标记为 ⚠️ 少。",
        "",
    ]

    thin: list[tuple[str, str, int]] = []
    for card in with_q:
        question = card["user_questions"]
        lines.append("---")
        lines.append("")
        lines.append(f"## {card['method_id']} — {card.get('topic','')}")
        lines.append("")
        lines.append(f"**用户疑惑**：{question}")
        lines.append("")
        lines.append("| 探针词 | 全库召回 | 各来源正文命中 |")
        lines.append("|---|---:|---|")

        probes: list[str] = []
        for concept in concepts(card["method_id"]):
            probes.append(concept)
            probes.extend(SYNONYMS.get(concept, [])[:3])
        # 去重保序
        probes = list(dict.fromkeys(probes))[:12]
        if not probes:
            lines.append("| （该卡未登记概念词，跳过） | — | — |")

        for probe in probes:
            n = recall_count(db, probe)
            dist = per_source(db, probe)
            dist_str = "、".join(f"{k[:6]} {v}" for k, v in list(dist.items())[:4]) or "—"
            flag = " ⚠️少" if n < args.min_hits else ""
            lines.append(f"| {probe} | {n}{flag} | {dist_str} |")
            if n < args.min_hits:
                thin.append((card["method_id"], probe, n))
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 召回过少的探针汇总")
    lines.append("")
    if thin:
        lines.append("| 卡 | 探针词 | 召回 |")
        lines.append("|---|---|---:|")
        for mid, probe, n in thin:
            lines.append(f"| {mid} | {probe} | {n} |")
        lines.append("")
        lines.append("这些词库里几乎没有。两种可能：① 作者不用这个说法（换词再试）；")
        lines.append("② 确实没人讲过这件事（要加来源）。逐条人工判读，不要直接下结论。")
    else:
        lines.append("没有召回过少的探针。")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"探了 {len(with_q)} 条疑惑，召回过少的探针 {len(thin)} 个")
    print(f"结果 -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
