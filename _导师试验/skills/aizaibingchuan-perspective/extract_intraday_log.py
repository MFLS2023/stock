#!/usr/bin/env python3
"""把爱在冰川的「借楼日志」抽成结构化决策记录。

这批素材的独特性：它是**盘中实时**发的，不是盘后复盘。
盘后复盘会被结果影响（知道涨了才说"我看好"），盘中日志是当时的真实判断。
南京路的 PDF 文章、郁金香的课程都没有这个粒度。

识别方式见 classify_qa_reply.py：问句是纯占位（`问：川哥借楼`）或答句以
借楼/时间戳开头（`答：午盘借楼再加一条…`）。实测 13621 个问答对里 1513 对是日志。

抽取三元组：
    标的     文中提到的股票名（用 _symbol_map.json 的 5885 个真实股票名匹配）
    理由     为什么看好/看空（中报、题材、板块联动…）
    退出     什么时候走（"明天冲高就走"这类）

⚠️ 不做的事：不判断这个决策后来对不对。早期标的无法用 MCP 核实
（实测 get_zt_reason 查两年前直接报 date参数不合法），硬核实就是编。

只读数据库，输出 JSONL + 统计。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
DB = ROOT / "_知识库系统" / "indexes" / "knowledge.db"
SYMBOL_MAP = ROOT / "_知识库系统" / "data" / "_symbol_map.json"
OUT = pathlib.Path(__file__).resolve().parent / "references" / "intraday_log.jsonl"

# 借楼判定的定义在 classify_qa_reply.py，从那里 import，不在这里复制第二份。
# 复制过的后果：2026-08-09 两边正则不同步（一边有「竞价」一边没有），计数差 261 条。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from classify_qa_reply import (  # noqa: E402
    LOG_A,
    LOG_PREFIX,
    PAIR_RE,
    PLACEHOLDER_Q,
)

# 时间标记：14.04借楼 / 10点借楼 / 午盘借楼
TIME_RE = re.compile(r"(\d{1,2}[.:：]\d{1,2}|\d{1,2}\s*点|早盘|午盘|尾盘|竞价)")

# 退出/持有意图
EXIT_RE = re.compile(
    r"(明天冲高就走|冲高就走|明天走|就走|兑现|减仓|清仓|止损|割肉|"
    r"持股不动|拿住|留一半|做T|短差|波段)"
)

# 日志类型。⚠️ 这一层是 2026-08-09 补的，因为最初我以为这 1774 条都是决策记录，
# 实测发现多数不是：抽样看到「不收小弟不收徒弟」「晚上十点公众号发红包」
# 「谢谢各位留言」「昨晚做了个梦」。标的识别率只有 10.3% 不是匹配失败，
# 是多数借楼贴本来就没有标的。先分类再谈价值，别拿精选样本以偏概全。
CHORE_RE = re.compile(
    r"(不收|徒弟|小弟|收费|荐股|加好友|公众号|红包|二维码|关注|谢谢|感谢|留言太多|"
    r"抱歉|明儿再聊|通知|广告|打赏|赞赏)"
)
MARKET_RE = re.compile(r"(大盘|指数|盘面|沪指|创业板|两市|量能|情绪|行情)")

# 理由关键词（分类用，不是穷举）
REASON_PATTERNS = {
    "业绩": r"(中报|年报|季报|业绩|预增|增长|扭亏)",
    "题材": r"(概念|题材|板块|方向|风口)",
    "政策消息": r"(政策|会议|国改|发文|批复|签约|中标|订单)",
    "技术形态": r"(均线|支撑|压力|突破|回踩|形态|缺口|放量|缩量)",
    "资金": r"(主力|资金|封单|承接|爆量|大单|北向)",
    "板块联动": r"(带动|联动|跟随|龙头|回流|补涨)",
}


def load_symbols() -> set[str]:
    """真实股票名，用于从正文里认出标的。"""
    try:
        data = json.loads(SYMBOL_MAP.read_text(encoding="utf-8"))
        return {n for n in data.get("by_name", {}) if len(n) >= 3}
    except Exception:
        return set()


def find_symbols(text: str, names: set[str]) -> list[str]:
    """精确匹配股票名。只用精确匹配，不做模糊。

    映射表是当前快照，只有现名没有旧名（小康股份->赛力斯、韦尔股份->豪威集团
    都不在表里）。而语料跨 2017-2026，早期文章写的就是旧名。
    模糊匹配会把一个正确的旧名"纠正"成另一家真实公司，那是伪造。
    """
    return [n for n in names if n in text]


def classify_reason(text: str) -> list[str]:
    return [tag for tag, pattern in REASON_PATTERNS.items() if re.search(pattern, text)]


def classify_kind(text: str, symbols: list[str]) -> str:
    """日志类型。优先级：杂务 > 有标的的操作 > 大盘判断 > 其他。

    杂务放最前是因为它最容易误判成别的（「谢谢大家关注，今天大盘…」既含
    杂务词也含大盘词，但它主要是公告）。
    """
    if CHORE_RE.search(text) and not symbols:
        return "杂务公告"
    if symbols:
        return "个股操作"
    if MARKET_RE.search(text):
        return "大盘判断"
    return "其他"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-chars", type=int, default=20,
                        help="答句短于此字数的日志跳过（多是「借楼」占位无内容）")
    args = parser.parse_args()

    if not DB.exists():
        print(f"索引不存在：{DB}", file=sys.stderr)
        return 1
    names = load_symbols()
    print(f"股票名映射表 {len(names)} 个（≥3 字）")

    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = db.execute(
        "SELECT chunk_id, date, title, text FROM chunks "
        "WHERE source_id='aizaibingchuan' AND chunk_type='qa_reply' ORDER BY date"
    ).fetchall()

    records: list[dict] = []
    skipped_short = 0
    for chunk_id, date, title, text in rows:
        for question, answer in PAIR_RE.findall(text):
            q, a = question.strip(), answer.strip()
            is_log = PLACEHOLDER_Q.match(q) or LOG_A.match(a)
            if not is_log:
                continue
            body = LOG_PREFIX.sub("", a).strip()
            if len(re.findall(r"[一-鿿]", body)) < args.min_chars:
                skipped_short += 1
                continue
            time_mark = TIME_RE.search(a)
            symbols = find_symbols(body, names)
            records.append({
                "chunk_id": chunk_id,
                "date": date,
                "title": title,
                "time": time_mark.group(1) if time_mark else "",
                "kind": classify_kind(body, symbols),
                "symbols": symbols,
                "reasons": classify_reason(body),
                "exit": sorted({m for m in EXIT_RE.findall(body)}),
                "text": body[:600],
            })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"抽出日志 {len(records)} 条（跳过无实质内容 {skipped_short} 条）")
    print(f"-> {OUT}")
    print()

    import collections

    print("【日志类型构成】—— 这批不全是决策记录")
    kinds = collections.Counter(r["kind"] for r in records)
    for kind, n in kinds.most_common():
        print(f"  {kind:<10}{n:>5} 条 ({n/len(records)*100:>5.1f}%)")
    print()

    stock_logs = [r for r in records if r["kind"] == "个股操作"]
    with_exit = [r for r in stock_logs if r["exit"]]
    print("【个股操作类的结构完整度】（分母是个股操作，不是全部）")
    print(f"  含退出意图    {len(with_exit):>5} / {len(stock_logs)} "
          f"({len(with_exit)/len(stock_logs)*100:.1f}%)" if stock_logs else "  无")
    full = [r for r in stock_logs if r["reasons"] and r["exit"]]
    print(f"  理由+退出齐全 {len(full):>5} / {len(stock_logs)} "
          f"({len(full)/len(stock_logs)*100:.1f}%)" if stock_logs else "")
    print()
    print("【理由分布】")
    tally = collections.Counter(t for r in records for t in r["reasons"])
    for tag, n in tally.most_common():
        print(f"  {tag:<8}{n:>5}")
    print()
    print("【时段分布】")
    slot = collections.Counter(r["time"] or "无标记" for r in records)
    for k, n in slot.most_common(8):
        print(f"  {k:<8}{n:>5}")
    print()
    print("【三元组齐全的样例】")
    for r in full[:3]:
        print(f"  {r['chunk_id']} [{r['date']} {r['time']}]")
        print(f"     标的 {'、'.join(r['symbols'][:4])}")
        print(f"     理由 {'、'.join(r['reasons'])}   退出 {'、'.join(r['exit'])}")
        print(f"     原文 {r['text'][:90]}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
