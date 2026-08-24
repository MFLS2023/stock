#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数据源自检 + 跨源交叉校验 —— 专门防「静默错数据」。

多源拼接最怕的不是接口挂掉（挂掉会抛异常，看得见），而是这两种：
  1. 接口还通，但上游悄悄改了字段名 → 我这边取成 None，数字变空却不报错
  2. 两个源给同一指标不同的数 → 拿哪个都可能错，而且没有任何提示

health()      逐个探测全部数据源，报通/坏/字段是否还在，带耗时
cross_check() 同一指标多源比对，不一致就报差异和已知原因

命令行：
    python live_market_health.py health
    python live_market_health.py cross
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_market as lm          # noqa: E402
import live_market_ex as ex       # noqa: E402
import live_market_ths as ths     # noqa: E402

# 探测样本票：流动性好、不会停牌的大票。用小票会把「今天没数据」误判成「接口坏」。
PROBE_SH = "600519"          # 贵州茅台，沪市
PROBE_SZ = "000001"          # 平安银行，深市

OK = "✅ 通"
NODATA = "⚠️ 通但无数据"
FIELD = "⚠️ 字段变化"
BAD = "❌ 坏"


def _need_vals(obj, keys: list) -> list:
    """
    检查我自己包装后的返回：键一定在（我建的 dict），要看**值是不是 None**。
    上游改字段名时表现就是值变 None，键还在。
    """
    if isinstance(obj, list):
        if not obj:
            return ["<空列表>"]
        obj = obj[0]
    if not isinstance(obj, dict):
        return [f"<不是 dict，是 {type(obj).__name__}>"]
    return [k for k in keys if obj.get(k) is None]


def _probe(name: str, source: str, fn, keys: list | None = None,
           empty_ok: bool = False) -> dict:
    """
    跑一个探测。三种结果分得清楚，不混成一个「失败」：

      通           → 有数据且关键字段都不是 None
      通但无数据   → 接口正常响应但返回空（休市日、盘后数据未发布），合法
      字段变化     → 有数据但关键字段取成 None，**上游很可能改了字段名**
      坏           → 抛异常

    empty_ok=True 时空结果算合法（跌停池、龙虎榜盘中查当天都可能是真的空）。
    """
    t0 = time.time()
    try:
        r = fn()
    except Exception as e:  # noqa: BLE001 - 自检就是要抓住所有异常
        return {"能力": name, "源": source, "状态": BAD,
                "耗时": round(time.time() - t0, 2),
                "错误": f"{type(e).__name__}: {e}"[:200]}
    cost = round(time.time() - t0, 2)

    # 我的模块对无数据/未发布会显式打标，先认这个标
    if isinstance(r, dict):
        mark = r.get("数据状态")
        if mark and mark != "已发布":
            return {"能力": name, "源": source, "状态": NODATA,
                    "耗时": cost, "说明": str(mark)}

    empty = (not r) or (isinstance(r, list) and not r)
    if empty:
        return {"能力": name, "源": source,
                "状态": NODATA if empty_ok else FIELD,
                "耗时": cost,
                "说明": "返回空" + ("（该接口空是合法的）" if empty_ok else "，但这个接口不该为空")}

    miss = _need_vals(r, keys or [])
    if miss:
        return {"能力": name, "源": source, "状态": FIELD, "耗时": cost,
                "取成None的字段": miss,
                "⚠️ 含义": "接口通但字段拿不到值，上游可能改了字段名，"
                          "**这种情况最危险，数字会静默变空**"}
    return {"能力": name, "源": source, "状态": OK, "耗时": cost}


# ---------- 1. 全域自检 ----------

def health(quick: bool = False) -> dict:
    """
    逐个探测全部数据源。quick=True 跳过慢的（两融约 8 秒、长历史分钟 K）。

    探测覆盖 6 个域名：push2delay（实时）、push2ex（股池）、
    push2ex异动、datacenter-web（盘后）、ifzq.gtimg.cn（腾讯K线）、
    money.finance.sina.com.cn（新浪）、data.10jqka.com.cn（同花顺）。
    """
    probes = [
        # ---- 东财 push2delay：实时行情 ----
        ("个股实时", "东财 push2delay", lambda: lm.quote_em(PROBE_SH),
         ["最新价", "涨跌幅", "量比", "换手率", "涨停价"], False),
        ("大盘温度", "东财 push2delay+ulist", lambda: lm.market_temp(with_dt=False),
         ["指数", "沪深上涨家数", "两市成交额(亿)", "赚钱效应"], False),
        ("板块资金流", "东财 push2delay", lambda: lm.sector_flow("industry", 5),
         ["板块", "主力净流入(亿)"], False),
        ("量比榜", "东财 push2delay", lambda: lm.strong_pool(5),
         ["代码", "量比"], False),
        # ---- 腾讯 / 新浪：K 线 ----
        ("个股实时", "腾讯 qt.gtimg.cn", lambda: lm.quote_tx(PROBE_SH),
         ["最新价", "涨跌幅", "买五档"], False),
        ("个股实时", "新浪 hq.sinajs.cn", lambda: lm.quote_sina(PROBE_SH),
         ["最新价", "涨跌幅"], False),
        ("日线", "腾讯 web.ifzq.gtimg.cn", lambda: lm.daily(PROBE_SH, 5),
         ["日期", "开", "收", "量(手)"], False),
        ("分钟K(短)", "腾讯 ifzq.gtimg.cn", lambda: ex.minute_kline(PROBE_SH, 5, 30),
         ["时间", "开", "收", "量(手)"], False),
        # ---- 东财 push2ex：股池 ----
        ("涨停池详细", "东财 push2ex", lambda: ex.zt_pool(),
         ["涨停家数", "明细"], False),
        ("炸板池", "东财 push2ex", lambda: ex.zb_pool(), ["炸板家数"], True),
        ("昨涨停今表现", "东财 push2ex", lambda: ex.yesterday_zt(),
         ["昨涨停只数", "昨涨停今日赚钱效应"], True),
        ("强势池", "东财 push2ex", lambda: ex.qs_pool(), ["强势股数"], True),
        ("跌停池", "东财 push2ex", lambda: ex.dt_pool(), ["跌停家数"], True),
        ("次新池", "东财 push2ex", lambda: ex.cx_pool(), ["次新股数"], True),
        # ---- 东财 push2ex 异动 ----
        ("盘口异动", "东财 push2ex异动", lambda: ths.changes("4", 5),
         ["各类型全市场统计", "明细"], True),
        # ---- 同花顺 ----
        ("涨停归因", "同花顺 data.10jqka", lambda: ths.zt_reason("", 5),
         ["涨停统计", "归因热词TOP15", "明细"], True),
        ("连板天梯", "同花顺 data.10jqka", lambda: ths.ladder(),
         ["最高板", "天梯"], True),
        ("题材聚集度", "同花顺 data.10jqka", lambda: ths.theme_top(),
         ["题材数", "题材排行"], True),
        # ---- 东财 datacenter-web：盘后 ----
        ("龙虎榜", "东财 datacenter-web", lambda: ex.lhb("", 5), ["明细"], True),
        ("龙虎榜席位", "东财 datacenter-web", lambda: ex.lhb_dept("", "", 5), ["明细"], True),
        ("大宗交易", "东财 datacenter-web", lambda: ex.block_trade("", 5), ["明细"], True),
    ]
    if not quick:
        probes += [
            ("分钟K(长历史)", "新浪 money.finance.sina", lambda: ex.minute_kline(PROBE_SZ, 5, 400),
             ["时间", "开", "收"], False),
            ("分钟资金流", "东财 push2delay", lambda: ex.fund_flow_min(PROBE_SH, 5),
             ["当前主力净流入(亿)", "恒等式校验"], False),
            ("两融明细", "东财 datacenter-web(慢~8s)", lambda: ex.margin_detail(5),
             ["明细"], True),
        ]

    t0 = time.time()
    rows = [_probe(*p) for p in probes]
    cnt = {}
    for r in rows:
        cnt[r["状态"]] = cnt.get(r["状态"], 0) + 1

    bad = [r for r in rows if r["状态"] == BAD]
    fld = [r for r in rows if r["状态"] == FIELD]
    # 按源汇总：一个域名整体挂掉和单个接口挂掉，处理方式完全不同
    by_src = {}
    for r in rows:
        s = by_src.setdefault(r["源"], {"通": 0, "坏": 0, "其他": 0})
        if r["状态"] == OK:
            s["通"] += 1
        elif r["状态"] == BAD:
            s["坏"] += 1
        else:
            s["其他"] += 1
    dead = [k for k, v in by_src.items() if v["通"] == 0 and v["坏"] > 0]

    if fld:
        concl = (f"⚠️ {len(fld)} 项字段取不到值 —— 这是最危险的情况，"
                 f"接口通但数字会静默变空，先查这几项再用数据")
    elif dead:
        concl = f"❌ 整源不可用：{'、'.join(dead)} —— 该域名下所有接口都失败，先查网络/代理"
    elif bad:
        concl = f"❌ {len(bad)} 个接口坏，但同源其他接口还通，属单接口问题"
    else:
        concl = "✅ 全部数据源可用，字段完整"

    return {
        "自检时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "模式": "快速（跳过慢接口）" if quick else "完整",
        "总耗时": f"{round(time.time() - t0, 1)}s",
        "结论": concl,
        "汇总": {"探测项": len(rows), **cnt},
        "按源汇总": by_src,
        "需要处理": (fld + bad) or "无",
        "明细": rows,
    }


# ---------- 2. 跨源交叉校验 ----------

def _cmp(item: str, vals: dict, tol: float = 0, known: str = "") -> dict:
    """
    比对同一指标的多源取值。

    tol 是容许差（绝对值）。差在容许范围内算一致；超出就报出来，
    **不自动挑一个当"正确答案"** —— 两个源都可能错，选哪个得看口径。
    known 填已知的口径差异原因，有 known 时超差算「已知差异」而不是「异常」。
    """
    got = {k: v for k, v in vals.items() if isinstance(v, (int, float))}
    if len(got) < 2:
        return {"指标": item, "状态": "⚠️ 无法比对",
                "原因": "可比的源不足 2 个（有源取数失败或该指标为空）",
                "各源取值": vals}
    lo, hi = min(got.values()), max(got.values())
    diff = round(hi - lo, 4)
    if diff <= tol:
        return {"指标": item, "状态": "✅ 一致", "值": lo if lo == hi else got,
                "最大差": diff, "容许差": tol}
    out = {"指标": item,
           "状态": "⚠️ 已知口径差异" if known else "❌ 不一致",
           "各源取值": got, "差值": diff, "容许差": tol}
    if known:
        out["已知原因"] = known
    else:
        out["⚠️ 怎么处理"] = "两个源都可能对，先确认口径再引用；不要默认取其中一个"
    return out


def _is_bj(code: str) -> bool:
    """北交所：920xxx 新规则，8xxxx 老规则。同花顺股池不含北交所。"""
    c = str(code or "")
    return c.startswith("920") or c.startswith("8")


def _attrib_zt(em_detail: list, lup, zbp, ths_n, bj_n: int) -> dict:
    """
    涨停家数校验 —— **逐只归因，不用容许差**。

    为什么不用容许差：家数差 4 只，可能 4 只都有正当理由，也可能 3 只有理由
    + 1 只是我算错了。只比数字这两种情况长得一模一样，必须比代码集合。

    2026-08-04 实测三条口径的确切关系（逐只核对，差异 100% 闭合）：
      行情列表 141 = 官方池 138 + 真封板 ST 2 只 + 炸板回封 1 只（高争民爆）
      官方池有而行情列表没有的：0 只

    归因规则：
      我有官方没有 → 是 ST（官方池系统性不含 ST）或在炸板池（官方归炸板）→ 可解释
      官方有我没有 → **一律算未解释**，这方向的漏票是我扫描逻辑的漏洞
    """
    em_codes = {x.get("代码") for x in em_detail}
    em_n = len(em_detail) or None
    if not isinstance(lup, list):
        # 查历史日期时 limit_up_pool 不可用（它只有实时），退化成两源数字比对
        return _cmp("涨停家数", {"东财push2ex": em_n, "同花顺": ths_n},
                    tol=max(bj_n, 2),
                    known=f"同花顺 filter=HS,GEM2STAR 不含北交所，东财池里有 {bj_n} 只")

    zt = [r for r in lup if r.get("板制") != "新股"]
    mine = {r.get("代码"): r for r in zt}
    zb_codes = {x.get("代码") for x in ((zbp or {}).get("明细") or [])}

    extra, unexplained = [], []
    for c, r in mine.items():
        if c in em_codes:
            continue
        nm = r.get("名称")
        if lm._is_st(nm):
            why = "ST：东财官方股池系统性不含 ST"
        elif c in zb_codes:
            why = "在官方炸板池：收盘封在涨停价但盘中炸过板，官方归炸板池"
        else:
            why = "❌ 未解释"
            unexplained.append(f"{c} {nm}（我有官方无）")
        extra.append({"代码": c, "名称": nm, "涨跌幅": r.get("涨跌幅"),
                      "板制": r.get("板制"), "归因": why})
    missing = [{"代码": x.get("代码"), "名称": x.get("名称"),
                "归因": "❌ 未解释：官方池有我没有，是我扫描逻辑漏了"}
               for x in em_detail if x.get("代码") not in mine]
    unexplained += [f"{m['代码']} {m['名称']}（官方有我无）" for m in missing]

    lm_n = len(zt)
    out = {
        "指标": "涨停家数",
        "各源取值": {"东财push2ex": em_n, "同花顺": ths_n, "东财行情列表": lm_n},
        "验算": f"行情列表 {lm_n} = 官方池 {em_n} + 我多 {len(extra)} - 我少 {len(missing)}",
        "验算成立": em_n is not None and lm_n == em_n + len(extra) - len(missing),
        "同花顺少的原因": f"filter=HS,GEM2STAR 不含北交所，东财池里北交所 {bj_n} 只",
    }
    if unexplained:
        out["状态"] = "❌ 不一致"
        out["未解释的票"] = unexplained
        out["⚠️ 怎么处理"] = ("这些票在一个源里是涨停、另一个源里不是，且不符合任何"
                            "已知口径差异 —— 很可能是判定逻辑有 bug，先查再用")
    else:
        out["状态"] = "✅ 一致（差异逐只可解释）"
    if extra:
        out["我多出的票"] = extra
    if missing:
        out["我漏掉的票"] = missing
    return out


def cross_check(code: str = PROBE_SH, date: str = "") -> dict:
    """
    跨源交叉校验 —— 同一个指标两个源都能拿到时，自动比对。

    校验 5 组：
      1. 个股最新价与涨跌幅（东财 / 腾讯 / 新浪三源）
      2. 涨停家数（东财 push2ex 股池 / 同花顺 / 东财行情列表自判）
      3. 炸板家数（东财炸板池 / 同花顺顶层 open_num）
      4. 封板成功率（我自算 / 同花顺官方 rate）
      5. 最高板（同花顺天梯 / 东财涨停池连板数最大值）

    **已知口径差异会标成「已知」而不是「异常」**，比如同花顺股池不含北交所。
    """
    t0 = time.time()
    checks, errs = [], []

    def grab(label, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - 单源失败不影响其余比对
            errs.append({"取数": label, "错误": f"{type(e).__name__}: {e}"[:160]})
            return None

    # ---- 1. 个股价格三源 ----
    qem = grab("东财个股", lambda: lm.quote_em(code))
    qtx = grab("腾讯个股", lambda: lm.quote_tx(code))
    qsn = grab("新浪个股", lambda: lm.quote_sina(code))
    g = lambda q, k: (q or {}).get(k)  # noqa: E731
    checks.append(_cmp(f"{code} 最新价",
                       {"东财": g(qem, "最新价"), "腾讯": g(qtx, "最新价"),
                        "新浪": g(qsn, "最新价")},
                       tol=0.02,
                       known=""))
    checks.append(_cmp(f"{code} 涨跌幅%",
                       {"东财": g(qem, "涨跌幅"), "腾讯": g(qtx, "涨跌幅"),
                        "新浪": g(qsn, "涨跌幅")},
                       tol=0.05))
    checks.append(_cmp(f"{code} 成交量(手)",
                       {"东财": g(qem, "成交量(手)"), "腾讯": g(qtx, "成交量(手)"),
                        "新浪": g(qsn, "成交量(手)")},
                       # 盘中三源采样时点不同，成交量会差几秒的量
                       tol=max(1000, (g(qem, "成交量(手)") or 0) * 0.01),
                       known="盘中三源采样时点差 1-3 秒，成交量本就会差一点"))

    # ---- 2. 涨停家数三源 ----
    ztp = grab("东财涨停池", lambda: ex.zt_pool(date))
    ztr = grab("同花顺涨停池", lambda: ths.zt_reason(date, 1))
    lup = grab("东财行情列表", lambda: lm.limit_up_pool(300)) if not date else None

    em_n = (ztp or {}).get("涨停家数")
    em_detail = (ztp or {}).get("明细") or []
    bj_n = sum(1 for x in em_detail if _is_bj(x.get("代码")))
    ths_stat = ((ztr or {}).get("涨停统计") or {}).get("今日") or {}
    ths_n = ths_stat.get("收盘封板")
    lm_n = (sum(1 for r in lup if r.get("板制") != "新股")
            if isinstance(lup, list) else None)

    # 炸板池在「涨停家数」和「炸板家数」两处都要用，取一次就够，别重复打接口
    zbp = grab("东财炸板池", lambda: ex.zb_pool(date))
    zb_n = (zbp or {}).get("炸板家数")

    # 涨停家数**不用容许差，做逐只归因**。
    # 家数差 4 只可能是 4 只都能解释，也可能是 3 只能解释 + 1 只我算错了 ——
    # 只比数字看不出区别，必须比代码集合。
    checks.append(_attrib_zt(em_detail, lup, zbp, ths_n, bj_n))

    # ---- 3. 炸板家数两源 ----
    checks.append(_cmp("炸板家数",
                       {"东财炸板池": (zbp or {}).get("炸板家数"),
                        "同花顺顶层open_num": ths_stat.get("炸板")},
                       tol=3,
                       known="同花顺顶层 open_num 是「收盘未封住」，东财炸板池是"
                             "「曾涨停现已打开」，同一件事两种统计时点，差几只正常"))

    # ---- 4. 封板成功率：自算 vs 官方 ----
    mine = (round(em_n / (em_n + zb_n) * 100, 1)
            if isinstance(em_n, int) and isinstance(zb_n, int) and (em_n + zb_n) else None)
    checks.append(_cmp("封板成功率%",
                       {"自算(东财涨停/(涨停+炸板))": mine,
                        "同花顺官方rate": ths_stat.get("封板成功率%")},
                       tol=5,
                       known="分母口径不同：我用东财池家数，同花顺用它自己的"
                             "history_num（曾涨停）。差 5 个点内属正常"))

    # ---- 5. 最高板两源 ----
    lad = grab("同花顺天梯", lambda: ths.ladder(date))
    lbs = [x.get("连板数") for x in em_detail
           if isinstance(x.get("连板数"), int)]
    checks.append(_cmp("最高板",
                       {"同花顺天梯": (lad or {}).get("最高板"),
                        "东财涨停池连板数max": max(lbs) if lbs else None},
                       tol=0,
                       known=""))

    # ---- 6. 同花顺自身恒等式 ----
    idt = None
    if ths_stat.get("曾涨停") is not None:
        a, b, c = (ths_stat.get("收盘封板"), ths_stat.get("炸板"),
                   ths_stat.get("曾涨停"))
        if all(isinstance(x, int) for x in (a, b, c)):
            idt = {"恒等式": "收盘封板 + 炸板 == 曾涨停",
                   "算式": f"{a} + {b} = {a + b} vs 曾涨停 {c}",
                   "状态": "✅ 成立" if a + b == c else "❌ 不成立（同花顺自身数据矛盾）"}

    bad = [c for c in checks if c["状态"].startswith("❌")]
    unk = [c for c in checks if c["状态"].startswith("⚠️ 无法")]
    if bad:
        concl = (f"❌ {len(bad)} 项跨源不一致且无已知口径原因 —— "
                 f"引用这些数字前必须先确认哪个源对："
                 + "、".join(c["指标"] for c in bad))
    elif unk:
        concl = f"⚠️ {len(unk)} 项无法比对（有源取数失败或休市无数据），其余一致"
    else:
        concl = "✅ 全部指标跨源一致，或差异有已知口径原因"

    return {
        "校验时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "样本票": code,
        "交易日": date or "今天",
        "耗时": f"{round(time.time() - t0, 1)}s",
        "结论": concl,
        "同花顺恒等式自检": idt or "无数据（休市或未取到）",
        "取数失败": errs or "无",
        "明细": checks,
        "说明": "不一致时不自动选源。两个源可能问的不是同一个问题（如连板数口径），"
                "也可能真有一个错了，得看『已知原因』有没有给出解释",
    }


# ---------- CLI ----------

def _p(o) -> None:
    print(json.dumps(o, ensure_ascii=False, indent=2))


def main(argv: list[str]) -> int:
    # Windows 控制台默认 GBK，输出 ✅/⚠️ 会 UnicodeEncodeError。
    # **只在 CLI 路径改**，不放模块顶层 —— MCP server 会 import 本模块，
    # 在导入时动 sys.stdout 会干扰 stdio JSON-RPC 传输。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 - 老 Python 或被重定向时忽略
        pass
    cmd = argv[1] if len(argv) > 1 else "health"
    arg = argv[2] if len(argv) > 2 else ""
    if cmd == "health":
        _p(health(quick=(arg == "quick")))
    elif cmd == "cross":
        _p(cross_check(arg or PROBE_SH))
    else:
        print(__doc__)
        print("用法：health [quick] | cross [股票代码]")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
