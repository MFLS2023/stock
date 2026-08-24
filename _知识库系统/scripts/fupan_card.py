#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
每日复盘卡生成器 —— 机器能填的它填，你只填机器填不了的。

解决三个问题：
  1. 「这里缺一点那里缺一点」→ 数据由机器统一取，缺就明确标缺数，不会漏项
  2. 「潜意识觉得累」      → 你只需要回答 5 个问题，其余全自动
  3. 「过两天又忘记了」    → 每天一个文件按日期存，自带「上次说的话」对照区

用法：
    python fupan_card.py              # 生成今天的卡
    python fupan_card.py 2026-08-07   # 生成指定日期的卡
    python fupan_card.py --check      # 只看昨天写的预判对不对（不生成新卡）

输出：_知识库系统/personal/fupan/YYYY-MM-DD.md
"""
from __future__ import annotations

import io
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Windows 控制台编码
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

OUT_DIR = HERE.parent / "personal" / "fupan"

# ---------- 取数（每一项都可能失败，失败就标缺数，绝不填 0 冒充） ----------


def _safe(fn, *a, retries: int = 2, **kw):
    """
    跑一个取数，失败返回 (None, 错误说明)。
    开盘啦/同花顺偶发连接超时，重试一次多数能过，所以默认试 2 轮。
    """
    last = ""
    for i in range(retries):
        try:
            return fn(*a, **kw), None
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {str(e)[:120]}"
            if i + 1 < retries:
                import time

                time.sleep(1.5)
    return None, last


def _unwrap(tool):
    """
    MCP 的 @mcp.tool() 在不同版本里包装方式不同：
    有的把原函数挂在 .fn，有的挂在 .func，有的根本不包装。
    这里三种都兼容，取不到就原样返回。
    """
    for attr in ("fn", "func", "__wrapped__"):
        inner = getattr(tool, attr, None)
        if callable(inner):
            return inner
    return tool


def fetch_all(date: str) -> dict:
    """一次拿齐生成复盘卡需要的全部数据。"""
    import live_market_mcp as M

    bag: dict = {"缺数": []}

    def put(key, tool, *a, **kw):
        val, err = _safe(_unwrap(tool), *a, **kw)
        bag[key] = val
        if err:
            bag["缺数"].append(f"{key}: {err}")

    # 7 指标是主干，其余是解释这 7 个数字为什么长这样
    put("sentiment", M.get_sentiment_7indicators, date)
    put("board", M.get_shortline_board, date)
    put("ladder", M.get_ladder, date)
    put("theme", M.get_theme_top, date)
    put("yesterday_zt", M.get_yesterday_zt, date)
    put("zt_reason", M.get_zt_reason, date, 40)
    return bag


# ---------- 读数（把嵌套 dict 里的数字挖出来，挖不到就说挖不到） ----------


def dig(obj, *path, default="缺数"):
    """按路径取值，任何一层不存在就返回 default。"""
    cur = obj
    for p in path:
        if cur is None:
            return default
        if isinstance(cur, dict):
            if p not in cur:
                return default
            cur = cur[p]
        elif isinstance(cur, (list, tuple)):
            if not isinstance(p, int) or p >= len(cur):
                return default
            cur = cur[p]
        else:
            return default
    return default if cur is None else cur


def pctile_tag(v) -> str:
    """分位数转成一句人话。南京路的相对阈值：>80 高点区，<20 低点区。"""
    if not isinstance(v, (int, float)):
        return ""
    if v >= 80:
        return "偏高（>80分位=情绪高点区域）"
    if v <= 20:
        return "偏低（<20分位=情绪低点区域）"
    if v >= 60:
        return "中性偏高"
    if v <= 40:
        return "中性偏低"
    return "中性"


def trend_arrow(today, avg5) -> str:
    """今日 vs 近5日均值，给个方向。"""
    if not isinstance(today, (int, float)) or not isinstance(avg5, (int, float)):
        return ""
    if avg5 == 0:
        return ""
    d = (today - avg5) / avg5 * 100
    if d > 15:
        return f"高于5日均值 {d:+.0f}%"
    if d < -15:
        return f"低于5日均值 {d:+.0f}%"
    return f"与5日均值持平（{d:+.0f}%）"


# ---------- 周期位置：只给候选和依据，不替用户拍板 ----------


def stage_hint(s: dict) -> list[str]:
    """
    按 SKILL 模型1 的进入条件，列出「今天像哪个阶段」的依据。
    故意不输出唯一结论 —— 阶段判断要用户自己下，机器只摆证据。
    """
    zt = dig(s, "1_涨停数", "今日", default=None)
    dt = dig(s, "2_跌停数", "今日", default=None)
    lb = dig(s, "3_连板数", "今日", default=None)
    fb = dig(s, "4_封板率", "今日", default=None)
    hi = dig(s, "6_市场高度", "今日", default=None)
    zt_p = dig(s, "1_涨停数", "近10日分位数", default=None)

    lines = []
    num = lambda x: isinstance(x, (int, float))  # noqa: E731

    # 缺数必须先报出来。缺数 ≠ 中性 —— 拿不到跌停数就断言「没触发否决权」
    # 等于用一个安慰性结论盖住了风险，这比不给结论更糟。
    missing = [
        name
        for name, v in (
            ("涨停数", zt), ("跌停数", dt), ("连板数", lb),
            ("封板率", fb), ("市场高度", hi),
        )
        if not num(v)
    ]
    if missing:
        lines.append(
            f"⚠ **缺数：{'、'.join(missing)}** —— 下面的判断不完整，"
            "缺的这几项不能当成「正常」。补数后再定阶段。"
        )

    if num(zt) and num(dt):
        if zt < 30 and dt >= 7:
            lines.append("✓ 混沌期特征：涨停<30 且 跌停≥7")
        if zt < 30 and dt < 7:
            lines.append("· 涨停<30 但跌停不多 —— 情绪冷但没恐慌，偏震荡")
    if num(fb) and num(hi):
        if fb > 80 and hi >= 7:
            lines.append("✓ 主升期特征：封板率>80% 且 高度≥7板")
        if fb < 60:
            lines.append("⚠ 封板率<60% —— 启发式2：不做接力（单条否决权）")
    if num(dt) and dt >= 7:
        lines.append(f"⚠ 跌停 {dt} 家 ≥7 —— 启发式3：管住手看戏（单条否决权）")
    if num(zt_p) and zt_p >= 80:
        lines.append("⚠ 涨停数在近10日>80分位 —— 启发式4：防范情绪高点")
    if num(lb) and num(hi):
        if lb <= 5 and hi <= 3:
            lines.append("· 连板少且高度≤3板 —— 无强势票，接力环境差")

    if not lines:
        lines.append("· 各项都在中性区间，没有触发任何单条否决权")
    elif missing and len(lines) == 1:
        # 只有缺数警告、没有任何实质判断时，别让它看起来像「今天没事」
        lines.append("· 除缺数项外没有可判断的数据，今天的阶段判断请手工核对。")
    return lines


# ---------- 昨日预判回查：解决「过两天又忘记了」 ----------


def load_prev_card(date: str) -> tuple[str | None, str]:
    """
    找到 date 之前最近的一张卡，抽出它的「明日只看这一件事」。
    返回 (那天的日期, 抽出的内容)。找不到就返回 (None, "")。
    """
    if not OUT_DIR.exists():
        return None, ""
    cards = sorted(p for p in OUT_DIR.glob("*.md") if p.stem < date)
    if not cards:
        return None, ""
    prev = cards[-1]
    text = prev.read_text(encoding="utf-8", errors="replace")

    # 抽 A5 那一节的正文。A5 是最后一节，所以除了下一个 ### 标题，
    # 还要在 --- 分隔线和「## 取数说明」处停下，否则会把页脚一起抓进来。
    grab: list[str] = []
    hit = False
    for line in text.splitlines():
        if line.startswith("### A5"):
            hit = True
            continue
        if not hit:
            continue
        if line.startswith("### ") or line.startswith("## ") or line.strip() == "---":
            break
        grab.append(line)

    body = "\n".join(grab).strip()
    # 去掉「按这个句式填…」这句提示语，只留用户真正写的内容
    body = "\n".join(
        ln for ln in body.splitlines() if not ln.startswith("按这个句式填")
    ).strip()
    return prev.stem, body


# ---------- 渲染 ----------

FILL = "________"


def render(date: str, bag: dict) -> str:
    s = bag.get("sentiment") or {}
    board = bag.get("board") or {}
    ladder = bag.get("ladder") or {}
    theme = bag.get("theme") or {}
    yzt = bag.get("yesterday_zt") or {}

    L: list[str] = []
    add = L.append

    add(f"# 复盘卡 {date}")
    add("")
    add("> 上半部分（M 区）机器已填，你**只读不写**。")
    add("> 下半部分（A 区）只有 5 个空，**必须手写** —— 手写才记得住，这是利弗莫尔的原话。")
    add("")

    # ============ 昨日回查 ============
    prev_date, prev_body = load_prev_card(date)
    add("## 第 0 步：先看上次自己说了什么（30 秒）")
    add("")
    if prev_date:
        add(f"上一张卡是 **{prev_date}**，你当时写的「明日只看这一件事」是：")
        add("")
        if prev_body:
            for ln in prev_body.splitlines():
                # 上一张卡里已经是 > 开头的引用，别再套一层变成 >>
                add(ln if ln.startswith(">") else (f"> {ln}" if ln.strip() else ">"))
        else:
            add("> （那天 A5 是空的 —— 这就是为什么会忘）")
        add("")
        add("**回答一句：应验了没有？** → " + FILL)
        add("")
        add("（写「应验/没应验/根本没发生」三选一即可，不要写分析。")
        add("连续记 20 天，你会发现自己错的地方是重复的。）")
    else:
        add("这是第一张卡，没有上次记录。从明天起这一栏会自动填上你昨天的判断。")
    add("")

    # ============ M 区：机器填 ============
    add("---")
    add("")
    add("## M 区 · 机器已填（只读）")
    add("")
    add("### M1 七指标")
    add("")
    add("| 指标 | 今日 | 近5日均值 | 近10日分位 | 读数 |")
    add("|---|---|---|---|---|")

    rows = [
        ("涨停数", "1_涨停数"),
        ("跌停数", "2_跌停数"),
        ("连板数", "3_连板数"),
        ("封板率%", "4_封板率"),
        ("涨跌家数比", "5_涨跌家数比"),
        ("市场高度", "6_市场高度"),
        ("RPS三线红池数", "7_RPS三线红池数"),
    ]
    for label, key in rows:
        today = dig(s, key, "今日")
        avg5 = dig(s, key, "近5日均值", default="—")
        p10 = dig(s, key, "近10日分位数", default="—")
        if today == "缺数":
            # 缺数就明说缺数，不要显示成 — 混在正常值里当成「正常」
            add(f"| {label} | ⚠ 缺数 | — | — | **没取到，不是 0** |")
            continue
        tag = pctile_tag(p10) or trend_arrow(today, avg5) or "—"
        add(f"| {label} | **{today}** | {avg5} | {p10} | {tag} |")
    add("")

    # 高度断层是判断情绪高度的核心视图
    hi_stocks = dig(s, "6_市场高度", "最高板个股", default=[])
    if isinstance(hi_stocks, list) and hi_stocks:
        add(f"最高板个股：{'、'.join(str(x) for x in hi_stocks[:8])}")
    fault = dig(s, "6_市场高度", "断层", default="")
    if fault:
        add(f"天梯断层：{fault}")
    add("")

    # 第 7 项的名单层读法比池数更稳
    diff = dig(s, "7_RPS三线红池数", "日间进出", default=None)
    if isinstance(diff, dict):
        add(
            f"三线红名单变动：新进 {diff.get('新进榜', '—')} 只，"
            f"掉出 {diff.get('掉出榜', '—')} 只，"
            f"连续在榜 {diff.get('连续在榜', '—')} 只"
            f"（对比 {diff.get('对比基准日', '—')}）"
        )
        add("")

    # ---- M2 触发了哪些硬规则 ----
    add("### M2 今天触发了哪些规则")
    add("")
    for ln in stage_hint(s):
        add(f"- {ln}")
    add("")
    add("⚠ 带「单条否决权」的规则不参与加减分 —— 不许用别的好信号去对冲它。")
    add("")

    # ---- M3 连板天梯（字段：天梯 -> [{板数, 只数, 个股:[{名称}]}]）----
    add("### M3 连板天梯")
    add("")
    tiers = dig(ladder, "天梯", default=None)
    if isinstance(tiers, list) and tiers:
        for tier in tiers[:10]:
            if not isinstance(tier, dict):
                add(f"- {tier}")
                continue
            n = tier.get("板数", "?")
            cnt = tier.get("只数", "?")
            stocks = tier.get("个股") or []
            names = "、".join(
                str(x.get("名称", x)) if isinstance(x, dict) else str(x) for x in stocks[:10]
            )
            more = f" 等{cnt}只" if isinstance(cnt, int) and cnt > 10 else ""
            add(f"- **{n}板**（{cnt}只）：{names}{more}")
        add("")
        add(f"最高板 {dig(ladder, '最高板')}｜连板总数 {dig(ladder, '连板总数')}")
    else:
        add("（天梯数据缺失，见文末缺数说明）")
    add("")

    # ---- M4 题材聚集度：找主线用这个（字段：题材排行）----
    add("### M4 题材涨停聚集度（找主线看这里）")
    add("")
    themes = dig(theme, "题材排行", default=None)
    if isinstance(themes, list) and themes:
        add("| 题材 | 涨停只数 | 连板只数 | 最高标 | 连续活跃天数 |")
        add("|---|---|---|---|---|")
        for t in themes[:8]:
            if not isinstance(t, dict):
                continue
            add(
                f"| {t.get('题材', '—')} | {t.get('涨停只数', '—')} | "
                f"{t.get('连板只数', '—')} | {t.get('最高标', '—')} | "
                f"{t.get('题材连续活跃天数', '—')} |"
            )
        add("")
        add("判读：涨停多 + 连板多 + 连续天数长 = 主线；涨停多但连板少 = 一日游风险。")
    else:
        add("（题材数据缺失，见文末缺数说明）")
    add("")

    # ---- M5 昨涨停今日表现（红盘率要自己算，官方那个字段常为 None）----
    add("### M5 昨涨停今日表现（情绪延续性）")
    add("")
    total = dig(yzt, "昨涨停只数", default=None)
    red = dig(yzt, "今日红盘", default=None)
    green = dig(yzt, "今日绿盘", default=None)
    if isinstance(total, int) and isinstance(red, int) and total > 0:
        rate = red / total * 100
        verdict = (
            "接力资金还在（>60%）" if rate > 60
            else "昨天的涨停偏虚（<40%，看A做B的信号源特征）" if rate < 40
            else "中性（40-60%）"
        )
        add(f"昨涨停 {total} 只 → 今日红盘 {red} 只、绿盘 {green} 只")
        add("")
        add(f"**红盘率 {rate:.1f}%** —— {verdict}")
    else:
        add("（昨涨停数据缺失，见文末缺数说明）")
    add("")

    # ---- M6 涨停归因热词（字段：归因热词TOP15 -> [{标签, 只数}]）----
    zt_reason = bag.get("zt_reason") or {}
    hot = dig(zt_reason, "归因热词TOP15", default=None)
    add("### M6 涨停归因热词（今天资金在买什么逻辑）")
    add("")
    if isinstance(hot, list) and hot:
        parts = []
        for h in hot[:15]:
            if isinstance(h, dict):
                parts.append(f"{h.get('标签', '')} {h.get('只数', '')}只")
            else:
                parts.append(str(h))
        add("｜".join(parts))
        add("")
        add("这是找「今天资金在买什么逻辑」最快的读数，A2 直接抄这里。")
    else:
        add("（归因热词缺失，见文末缺数说明）")
    add("")

    # ============ A 区：只有 5 个空 ============
    add("---")
    add("")
    add("## A 区 · 你手写（只有 5 个空，写完就收工）")
    add("")
    add("> 规则：**每空最多两行**。写不出来就写「不知道」——「不知道」是有效答案，")
    add("> 空着不是。空着等于这一天没复盘。")
    add("")

    add("### A1 今天属于哪个阶段")
    add("")
    add("混沌 / 破局 / 主升 / 补涨 / 退潮 → " + FILL)
    add("")
    add("依据（抄 M2 里的一条就行）：" + FILL)
    add("")

    add("### A2 今天最强的一条逻辑是什么")
    add("")
    add("看 M4 和 M6，用一句话说清今天资金在买什么：" + FILL)
    add("")
    add("这条逻辑是第几天了：" + FILL)
    add("")

    add("### A3 今天我做了什么（没交易就写空仓）")
    add("")
    add("操作：" + FILL)
    add("")
    add("是计划内还是临时起意：" + FILL)
    add("")

    add("### A4 今天我错在哪一步")
    add("")
    add("从这六个里挑一个，只挑一个：**方向 / 节点 / 节奏 / 成本 / 仓位 / 卖点**")
    add("")
    add("→ " + FILL)
    add("")
    add("（「今天亏了多少」不是答案，那是结果。要写错在哪个动作上。）")
    add("")

    add("### A5 明日只看这一件事")
    add("")
    add("按这个句式填，明天开卡第一眼就会看到它：")
    add("")
    add(f"> 只有当 {FILL} 出现，")
    add(f"> 并且 {FILL} 没有失效时，")
    add(f"> 我才关注 {FILL}；")
    add("> 否则保持空仓 / 只处理持仓。")
    add("")

    # ============ 缺数说明 ============
    add("---")
    add("")
    add("## 取数说明")
    add("")
    add(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    add("")
    if bag.get("缺数"):
        add("**本次缺数（这些项机器没取到，不是 0）：**")
        add("")
        for m in bag["缺数"]:
            add(f"- {m}")
        add("")
        add("缺数不影响复盘 —— 按 M2 里能拿到的规则判断即可。")
    else:
        add("全部取数成功，无缺项。")
    add("")

    return "\n".join(L)


# ---------- 连续性检查：A5 有没有在写 ----------


def check_streak() -> str:
    """看最近 10 张卡里，A5 有几张真的填了。"""
    if not OUT_DIR.exists():
        return "还没有任何复盘卡。"
    cards = sorted(OUT_DIR.glob("*.md"))[-10:]
    if not cards:
        return "还没有任何复盘卡。"

    lines = [f"最近 {len(cards)} 张卡的填写情况：", ""]
    filled = 0
    for p in cards:
        text = p.read_text(encoding="utf-8", errors="replace")
        # A5 那节里还剩几个下划线占位符 = 没填
        seg = text.split("### A5")
        body = seg[1].split("### ")[0] if len(seg) > 1 else ""
        blanks = body.count(FILL)
        if blanks == 0 and body.strip():
            lines.append(f"  ☑ {p.stem}  已填")
            filled += 1
        else:
            lines.append(f"  ☐ {p.stem}  还剩 {blanks} 个空")
    lines += ["", f"填写率 {filled}/{len(cards)}"]
    if filled < len(cards) * 0.6:
        lines.append("→ 填写率不到六成。先把 A5 一个空填满，别管其他的。")
    return "\n".join(lines)


def main() -> int:
    args = [a for a in sys.argv[1:] if a]

    if "--check" in args:
        print(check_streak())
        return 0

    date = next((a for a in args if not a.startswith("-")), "")
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    date = date.replace("/", "-")
    if len(date) == 8 and date.isdigit():
        date = f"{date[:4]}-{date[4:6]}-{date[6:]}"

    print(f"正在取数（{date}）…")
    bag = fetch_all(date)

    text = render(date, bag)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{date}.md"
    if out.exists():
        # 已经写过就不覆盖，避免把手写内容冲掉
        print(f"⚠ {out} 已存在，不覆盖。")
        print("  想重新生成先把它改名或删掉。")
        return 1
    out.write_text(text, encoding="utf-8")

    print(f"✓ 已生成 {out}")
    if bag["缺数"]:
        print(f"  有 {len(bag['缺数'])} 项缺数，已在文件末尾标明")
    print()
    print("现在做两件事：")
    print("  1. 读 M 区（1 分钟，只读不写）")
    print("  2. 填 A 区 5 个空（5 分钟，手写）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
