#!/usr/bin/env python3
"""把审批清单里的判定与疑惑写回 methods.jsonl。

用户在 reports/方法卡审批清单.md 每张卡末尾填了 `认/改/拒` 和自己的疑惑。
本脚本解析那份清单，更新 methods.jsonl 的两个字段：

    status          draft -> reviewed / revised / rejected
    user_questions  用户对这张卡的未解问题（原样保留，不改写）

为什么要存 user_questions：这些疑惑不是噪音，是这张卡的已知缺口。
卡回答「该看什么」，用户问的全是「怎么看出来」——后者是方法卡的固有边界，
不该由 AI 编操作细则去填（项目里已有前例：郁金香 SKILL v2.0 那 14 条阈值
是 AI 推的，原文一个数字都没给）。把缺口显式记下来，检索到这张卡时能一并
看到「用户还没解决什么」，避免把半完备的卡当成可照做的规则。

只读清单、只写 methods.jsonl。用 --dry-run 先看会改什么。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
REVIEW = ROOT / "_知识库系统" / "reports" / "方法卡审批清单.md"
METHODS = ROOT / "_知识库系统" / "source_libraries" / "fulibei" / "methods.jsonl"

# 判定词 -> status。清单里用户写的是中文单字，机器识别按首个出现的判定词为准。
VERDICT_MAP = {"认": "reviewed", "改": "revised", "拒": "rejected"}

CARD_RE = re.compile(r"^## (fulibei-method-\d+)", re.M)
# 判定行形如：**你的判定**：`认 / 改 / 拒` → 认，但是还需要补充的就是……
VERDICT_LINE_RE = re.compile(r"\*\*你的判定\*\*：.*?→\s*(.*)$", re.M)


def parse_review(text: str) -> dict[str, dict]:
    """解析清单，返回 {method_id: {"status":…, "questions":…}}。"""
    # 按卡切段，段内找判定行
    marks = list(CARD_RE.finditer(text))
    result: dict[str, dict] = {}
    for index, mark in enumerate(marks):
        method_id = mark.group(1)
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        section = text[mark.start():end]
        found = VERDICT_LINE_RE.search(section)
        if not found:
            continue
        raw = found.group(1).strip()
        if not raw:
            continue

        # 判定词必须出现在开头附近，否则疑惑正文里的「认」字会误命中
        head = raw[:6]
        verdict = next((v for v in VERDICT_MAP if v in head), None)
        if verdict is None:
            continue

        # 判定词之后的内容即疑惑；去掉紧跟的标点和「（有保留）」这类修饰
        rest = raw[raw.index(verdict) + len(verdict):]
        rest = re.sub(r"^[，,。、\s（）()有保留]*", "", rest).strip()
        result[method_id] = {"status": VERDICT_MAP[verdict], "questions": rest}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只打印将要改什么，不落盘")
    args = parser.parse_args()

    if not REVIEW.exists():
        print(f"找不到审批清单：{REVIEW}", file=sys.stderr)
        return 1

    verdicts = parse_review(REVIEW.read_text(encoding="utf-8"))
    cards = [json.loads(l) for l in METHODS.read_text(encoding="utf-8").splitlines() if l.strip()]

    print(f"清单解析出 {len(verdicts)} 条判定，methods.jsonl 有 {len(cards)} 张卡")
    missing = [c["method_id"] for c in cards if c["method_id"] not in verdicts]
    if missing:
        print(f"⚠️ 未在清单里找到判定的卡（保持 draft）：{missing}")

    counts: dict[str, int] = {}
    with_questions = 0
    for card in cards:
        hit = verdicts.get(card["method_id"])
        if not hit:
            continue
        card["status"] = hit["status"]
        counts[hit["status"]] = counts.get(hit["status"], 0) + 1
        if hit["questions"]:
            card["user_questions"] = hit["questions"]
            with_questions += 1
        else:
            card.pop("user_questions", None)

    print(f"判定分布：{counts}")
    print(f"带用户疑惑的卡：{with_questions} 张")

    if args.dry_run:
        print("\n--dry-run，未写盘。逐条预览：")
        for card in cards:
            q = card.get("user_questions", "")
            print(f"  {card['method_id']}  {card['status']:<9} {q[:56]}")
        return 0

    lines = [json.dumps(c, ensure_ascii=False) for c in cards]
    METHODS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n已写入 {METHODS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
