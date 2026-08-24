# -*- coding: utf-8 -*-
"""复利杯闲聊标记（2026-08-24）。

规则候选 25 条、人工逐条核对后放行 22 条（豁免 3 条误伤）。被标记的块仍可检索，
只在排序层降权（CHUNK_TYPE_BONUS['chitchat']=-3.0）。幂等：重复跑结果一致。
"""
import json
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "source_libraries" / "fulibei"

# 规则=零词汇命中+≥2 个强闲聊标记词；或首时间戳≤14 分钟且词命中≤1。
# 豁免（规则命中但人工判定为有效内容）：
#   -092-p001-c01 开场即入正题的行情/思想感言
#   -103-p003-c01 含「保持敏感度、反复看」的交易习惯讨论
#   -107-p001-c01 实际在陈述当日卖出动作
APPROVED = [
    "fulibei-020-p009-c01",  # 榜一大哥救场、直播间卡顿
    "fulibei-021-p001-c01",  # 开场寒暄、个人经历铺垫
    "fulibei-031-p001-c01",  # 微博假号实名八卦
    "fulibei-050-p001-c01",  # 试麦调音量
    "fulibei-050-p002-c01",  # 捏肩膀、和平饭店、掼蛋
    "fulibei-050-p002-c03",  # LV 手表年份、抖音八卦
    "fulibei-059-p001-c01",  # 嘉宾介绍吹捧
    "fulibei-069-p002-c02",  # 高考、喝茶闲谈
    "fulibei-077-p002-c03",  # 推荐算法、美女跳舞
    "fulibei-077-p002-c04",  # 睡觉、点名上线
    "fulibei-089-p001-c01",  # 睡醒、洗衣服
    "fulibei-093-p001-c01",  # 星星、狗相亲、绯闻玩笑
    "fulibei-094-p001-c01",  # 绯闻玩笑
    "fulibei-094-p003-c01",  # 直播拉人上线
    "fulibei-094-p004-c03",  # 宵夜沙拉
    "fulibei-098-p009-c04",  # 跳舞、表妹、直播间
    "fulibei-102-p002-c02",  # 阴阳师、嫂子调侃
    "fulibei-102-p002-c03",  # 女主播、清心寡欲
    "fulibei-102-p003-c01",  # 开车、LOL 八卦
    "fulibei-103-p002-c02",  # 车展车模
    "fulibei-108-p001-c01",  # 瑞鹤仙去向八卦（人八卦不是方法，检索「瑞鹤仙」仍可命中）
    "fulibei-108-p001-c02",  # 抖音等级、小号刷礼物
]


def main() -> int:
    path = LIB / "chunks.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    changed = 0
    seen = set()
    for row in rows:
        seen.add(row["chunk_id"])
        if row["chunk_id"] in APPROVED:
            if row["chunk_type"] != "chitchat":
                row["chunk_type"] = "chitchat"
                changed += 1
    missing = [cid for cid in APPROVED if cid not in seen]
    if missing:
        raise SystemExit(f"清单里有库里不存在的 chunk_id：{missing}")
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    tmp.replace(path)
    print(f"标记 {changed} 条为 chitchat（清单 {len(APPROVED)} 条）；重建索引后生效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
