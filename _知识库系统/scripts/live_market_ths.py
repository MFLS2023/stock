#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
短线专用数据 —— 同花顺涨停数据中心 + 东财盘口异动。

补齐东财股池给不了的四类短线数据：
  1. 涨停归因标签（reason_type，如「算力租赁+英伟达合作+中报预增」）
  2. 归因全文（reason_info，行业原因 + 公司原因，同花顺 AI 生成）
  3. 连板天梯（按板数分层，一眼看到最高标）
  4. 题材板块涨停聚集度（每个题材涨停几只、几只连板、最高标几板）

数据源：
  data.10jqka.com.cn/dataapi/limit_up/*   同花顺，GET，可回溯历史（实测到 2026-06）
  push2ex.eastmoney.com/getAllStockChanges  东财盘口异动，必须带 dpt=wzchanges

命令行：
    python live_market_ths.py zt          涨停池（带归因标签）
    python live_market_ths.py ladder      连板天梯
    python live_market_ths.py theme       题材板块排行
    python live_market_ths.py reason 603629   某只票的涨停归因全文
    python live_market_ths.py chg         盘口异动
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta

import requests

# ---------------- 常量 ----------------

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
THS_BASE = "https://data.10jqka.com.cn/dataapi/limit_up"
THS_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://data.10jqka.com.cn/datacenterph/limitup/limtupInfo.html",
    "X-Requested-With": "XMLHttpRequest",
}
# 实测有效的 field 串。多传无效码只会多出 null 键，不影响返回
THS_FIELD = ("199112,10,9001,330329,330328,133,3475914,9002,9003,9004,"
             "1968584,3541450,9005,9006")
THS_LIMIT_MAX = 200          # 实测硬上限，超过报 "limit must be less than or equal to 200"

EM_CHANGES = "https://push2ex.eastmoney.com/getAllStockChanges"
EM_UT = "7eea3edcaed734bea9cbfc24409ed989"   # 东财网页端公开固定值，不是密钥
EM_DPT = "wzchanges"          # 关键：不带这个参数返回 rc=102 data=null

MIN_INTERVAL = 0.12
_last_call = [0.0]

# 盘口异动 type 码 —— **官方映射**，来自 akshare 源码
# site-packages/akshare/stock_feature/stock_pankou_em.py 的 symbol_map（22 个码）。
#
# 上一版这里是我按 i 字段段数、末值正负、tm 分布"实测归纳"的名字，22 个里错了 15 个
# （只有 4/8/16/64/128/8193/8194 猜对）。教训：先查现成库的源码，别自己猜。
CHANGE_TYPES = {
    4:    "封涨停板",
    8:    "封跌停板",
    16:   "打开涨停板",
    32:   "打开跌停板",
    64:   "有大买盘",
    128:  "有大卖盘",
    8193: "大笔买入",
    8194: "大笔卖出",
    8201: "火箭发射",
    8202: "快速反弹",
    8203: "高台跳水",
    8204: "加速下跌",
    8207: "竞价上涨",
    8208: "竞价下跌",
    8209: "高开5日线",
    8210: "低开5日线",
    8211: "向上缺口",
    8212: "向下缺口",
    8213: "60日新高",
    8214: "60日新低",
    8215: "60日大幅上涨",
    8216: "60日大幅下跌",
}
# 官方 22 个码之外，实测 8217 也有数据但官方表里没有，按未命名处理。
# 256 / 512 实测始终 0 条。32（打开跌停板）0 条是合法的——当日无跌停股即无该异动。
UNOFFICIAL_CODES = {8217: "官方 symbol_map 未收录，实测有数据，含义未知"}

# changes() 默认查这四个码。**东财 type 参数只认第一个码**，所以是逐码请求再合并，
# 每多一个码多约 0.6 秒 —— 默认只放最必要的四个：
# 封涨停板（谁封上了）、打开涨停板（谁炸了）、火箭发射（谁在拉）、高台跳水（谁在砸）。
CHANGES_DEFAULT = ("4", "16", "8201", "8203")


class NoData(Exception):
    """查无数据 —— 合法结果，不是故障。"""


def _as_int(v):
    """类型码转 int，转不了就原样返回（用于查 CHANGE_TYPES）。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False        # 必须：本机代理 127.0.0.1:7897 会掐断请求
    return s


def _throttle() -> None:
    gap = time.time() - _last_call[0]
    if gap < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - gap)
    _last_call[0] = time.time()


def _get(url: str, params: dict, headers: dict, tries: int = 3) -> dict:
    """带节流和退避的 GET，返回解析好的 JSON。"""
    err = None
    for i in range(tries):
        _throttle()
        try:
            r = _session().get(url, params=params, headers=headers, timeout=15)
            if r.status_code != 200:
                err = RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
            else:
                return r.json()
        except Exception as e:                       # noqa: BLE001
            err = e
        time.sleep(0.4 * (2 ** i))
    raise RuntimeError(f"取数失败（{tries} 次）：{type(err).__name__}: {err}")


def _ths(path: str, params: dict) -> dict:
    """同花顺请求，把 status_code != 0 明确区分为拒绝而不是空数据。"""
    j = _get(f"{THS_BASE}/{path}", params, THS_HEADERS)
    code = j.get("status_code")
    if code not in (0, None):
        raise RuntimeError(f"同花顺拒绝：status_code={code} {j.get('status_msg')}")
    return j


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _norm_date(date: str) -> str:
    """接受 YYYYMMDD 或 YYYY-MM-DD，空则今天。"""
    if not date:
        return _today()
    d = str(date).replace("-", "").replace("/", "").strip()
    if len(d) != 8 or not d.isdigit():
        raise ValueError(f"日期格式应为 YYYYMMDD 或 YYYY-MM-DD，收到：{date}")
    return d


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ts(v) -> str | None:
    """同花顺的首封时间是 Unix 秒时间戳字符串。"""
    try:
        return datetime.fromtimestamp(int(v)).strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return None


def _yi(v, div=1e8) -> float | None:
    """转亿元，保留两位。"""
    try:
        return round(float(v) / div, 2)
    except (TypeError, ValueError):
        return None


def _pct(v) -> float | None:
    """同花顺的比率字段是 0-1 小数，转百分数。"""
    try:
        return round(float(v) * 100, 2)
    except (TypeError, ValueError):
        return None


def _empty_note(d: str) -> dict:
    """
    同花顺对休市日/无数据日期一律返回 status=success + 空 info，不报错。

    实测 20260101（元旦）和 20260802（周日）都是 today 全 0、info 0 条，
    而且 limit_up_count.yesterday 仍带数字但**对不上真实的上一交易日**
    （20260802 的 yesterday 给 num=98，而真实上一交易日 20260731 是 75）。
    所以空结果必须显式标注，不能让调用方把 0 当成"当天真的零涨停"。
    """
    return {
        "数据状态": "无数据",
        "可能原因": "该日期是非交易日（周末/节假日），或同花顺尚未生成当日数据",
        "⚠️ 注意": "同花顺对无数据日期返回 success 而非报错，且「上一交易日」"
                 "统计在这种情况下对不上真实交易日，不要引用",
        "怎么确认": f"{d} 是不是交易日，可用 get_daily_kline 看该日有无 K 线",
    }


def _count(c: dict | None) -> dict | None:
    """
    解析 limit_up_count / limit_down_count 的嵌套结构。

    字段含义（2026-08-04 用 5 个交易日 20 组数据校验，两条恒等式 20/20 精确成立）：
      history_num  当日曾涨停总数（含最终没封住的）
      num          收盘仍封板数
      open_num     收盘时**未封住**的只数（≈ 炸板池家数）
      rate         封板成功率 = num / history_num

      恒等式1  num + open_num == history_num      20/20
      恒等式2  rate == num / history_num          20/20（浮点精确相等）

    ⚠️ 这里的 open_num 与**明细里每只票的 open_num 不是一回事**：
       顶层 open_num=16  → 收盘没封住的 16 只（已不在涨停池里）
       明细 open_num>0   → 当天打开过板、但收盘前又回封的 63 只（仍在涨停池里）
    两个数都对，问的是不同问题。别拿来互相印证。

    这是同花顺官方口径的封板成功率，比自己用「涨停数/(涨停数+炸板数)」推更权威。
    """
    if not isinstance(c, dict):
        return None
    out = {}
    for src, label in (("today", "今日"), ("yesterday", "上一交易日")):
        v = c.get(src) or {}
        out[label] = {
            "曾涨停": v.get("history_num"),
            "收盘封板": v.get("num"),
            "炸板": v.get("open_num"),
            "封板成功率%": _pct(v.get("rate")),
        }
    return out


# ==================== 1. 涨停池（带归因标签）====================

def zt_reason(date: str = "", limit: int = 200) -> dict:
    """
    同花顺涨停池 —— **比东财股池多出涨停归因**。

    每只票带：
      reason_type      涨停标签，如「算力租赁+英伟达合作+中报预增」
      limit_up_suc_rate 该票历史封板成功率（0-1）
      high_days        连板表述，如「7天7板」「3天2板」「首板」
      open_num         当天打开过板的次数（回封了才会出现在本池）
      change_tag       FIRST_LIMIT 首次封板 / LIMIT_BACK 炸板后回封

    ⚠️ 「连板」字段与东财 get_zt_pool_detail 的「连板数」**不是同一口径**：
       同花顺 high_days「3天2板」= 最近 3 个交易日里涨停了 2 次（不要求连续）
       东财   连板数 = 1         = 末尾连续涨停 1 天
       两个都对。2026-08-04 用日线逐日核实 5 只票，两种口径各自 5/5 吻合。
       利通电子就是例子：7/31 涨停、8/3 回调 -3.03%、8/4 再涨停 →
       同花顺记「3天2板」，东财记「连板数 1」。这种形态短线里叫反包。
       想知道「是不是连续板」看东财；想知道「近期涨停密度」看同花顺。

    limit 只截断明细，统计与热词始终基于全部涨停股。可回溯历史，实测到 2026-06。
    ⚠️ 同花顺 filter=HS,GEM2STAR 不含北交所（920xxx），东财股池含。
       2026-08-04 实测东财 137 只、同花顺 136 只，差的就是汉鑫科技 920092。
    """
    d = _norm_date(date)
    # 始终按上限取全量：统计（热词、炸板计数）必须基于全部涨停股，
    # 否则 limit 一小统计基数就跟着缩，得出的热词是假的。明细最后再截。
    j = _ths("limit_up_pool", {
        "page": 1, "limit": THS_LIMIT_MAX, "field": THS_FIELD,
        "filter": "HS,GEM2STAR", "order_field": "330329",
        "order_type": "0", "date": d,
    })
    data = j.get("data") or {}
    info = data.get("info") or []
    rows = []
    for x in info:
        rows.append({
            "代码": x.get("code"),
            "名称": x.get("name"),
            "最新价": x.get("latest"),
            "涨幅%": round(x.get("change_rate"), 2) if x.get("change_rate") is not None else None,
            "连板": x.get("high_days"),
            "涨停归因": x.get("reason_type"),
            "封板类型": {"FIRST_LIMIT": "首次封板", "LIMIT_BACK": "炸板后回封"}.get(
                x.get("change_tag"), x.get("change_tag")),
            "炸板次数": x.get("open_num") or 0,
            "历史封板成功率%": _pct(x.get("limit_up_suc_rate")),
            "换手率%": round(x.get("turnover_rate"), 2) if x.get("turnover_rate") is not None else None,
            "流通市值(亿)": _yi(x.get("currency_value")),
            "总市值(亿)": _yi(x.get("sum_market_value")),
            "是否新股": bool(x.get("is_new")),
        })

    # 以下统计全部基于全量 rows，不受 limit 影响
    zb = [r for r in rows if r["炸板次数"] > 0]
    back = [r for r in rows if r["封板类型"] == "炸板后回封"]
    # 归因标签拆词统计 —— 这是短线找主线最直接的读数
    tag_count: dict[str, int] = {}
    for r in rows:
        for tag in str(r["涨停归因"] or "").split("+"):
            tag = tag.strip()
            if tag:
                tag_count[tag] = tag_count.get(tag, 0) + 1
    hot = sorted(tag_count.items(), key=lambda kv: -kv[1])[:15]

    n = max(1, int(limit))
    ts = data.get("trade_status") or {}
    if not rows:
        return {"取数时间": _now(), "数据日期": data.get("date") or d,
                "数据源": "同花顺涨停数据中心", "交易阶段": ts.get("name"),
                "涨停总数": 0, "明细": [], **_empty_note(d)}
    return {
        "取数时间": _now(),
        "数据日期": data.get("date") or d,
        "数据源": "同花顺涨停数据中心",
        "交易阶段": ts.get("name"),
        "涨停统计": _count(data.get("limit_up_count")),
        "跌停统计": _count(data.get("limit_down_count")),
        "涨停总数": len(rows),
        "明细条数": min(n, len(rows)),
        "曾打开过板只数": len(zb),
        "炸板后回封只数": len(back),
        "统计口径": "以上统计基于全部涨停股，不随 limit 变化；limit 只截断明细",
        "归因热词TOP15": [{"标签": k, "只数": v} for k, v in hot],
        "明细": rows[:n],
    }


def stock_reason(code: str, date: str = "") -> dict:
    """
    单只票的涨停归因全文（行业原因 + 公司原因，同花顺 AI 生成）。

    在 block_top 的 stock_list 里找，因为 reason_info 只在题材接口里带全文。
    找不到通常是该票当天没涨停，或不在前 20 大题材内。
    """
    d = _norm_date(date)
    code = str(code).strip()[-6:]
    j = _ths("block_top", {"date": d})
    for b in (j.get("data") or []):
        for s in (b.get("stock_list") or []):
            if s.get("code") == code:
                return {
                    "取数时间": _now(),
                    "数据日期": d,
                    "代码": code,
                    "名称": s.get("name"),
                    "所属题材": b.get("name"),
                    "涨停归因标签": s.get("reason_type"),
                    "连板": s.get("high"),
                    "首次封板": _ts(s.get("first_limit_up_time")),
                    "最后封板": _ts(s.get("last_limit_up_time")),
                    "封板类型": {"FIRST_LIMIT": "首次封板",
                                 "LIMIT_BACK": "炸板后回封"}.get(s.get("change_tag")),
                    "归因全文": s.get("reason_info") or "（同花顺未提供全文）",
                    "说明": "归因由同花顺 AI 汇总公开信息生成，不构成投资建议",
                }
    return {
        "取数时间": _now(), "数据日期": d, "代码": code,
        "数据状态": "未找到",
        "可能原因": "该票当天未涨停，或不在当日前 20 大题材的成分内",
    }


# ==================== 2. 连板天梯 ====================

def ladder(date: str = "") -> dict:
    """
    连板天梯 —— 按板数分层列出所有连板股，一眼看到最高标。

    这是开盘啦式的核心视图：最高板在哪、几只、断层在哪一级。
    注意只含 2 板及以上，首板不在内（首板看 zt_reason）。

    这里的「板数」是**连续涨停天数**（与东财 get_zt_pool_detail 的连板数同口径），
    不是同花顺涨停池里「3天2板」那种密度口径。
    """
    d = _norm_date(date)
    j = _ths("continuous_limit_up", {"date": d})
    levels = []
    for lv in (j.get("data") or []):
        stocks = [{"代码": c.get("code"), "名称": c.get("name"),
                   "连板数": c.get("continue_num")}
                  for c in (lv.get("code_list") or [])]
        levels.append({"板数": lv.get("height"), "只数": lv.get("number"),
                       "个股": stocks})
    levels.sort(key=lambda x: -(x["板数"] or 0))
    top = levels[0] if levels else None
    if not levels:
        return {"取数时间": _now(), "数据日期": d, "数据源": "同花顺连板天梯",
                "最高板": None, "连板总数": 0, "天梯": [], **_empty_note(d)}
    # 断层：相邻层级之间缺失的板数，短线里断层大说明高度不连续、接力意愿弱
    hs = [x["板数"] for x in levels if x["板数"]]
    gaps = [f"{hs[i]}板与{hs[i + 1]}板之间空缺"
            for i in range(len(hs) - 1) if hs[i] - hs[i + 1] > 1]
    return {
        "取数时间": _now(),
        "数据日期": d,
        "数据源": "同花顺连板天梯",
        "最高板": top["板数"] if top else None,
        "最高板个股": [s["名称"] for s in top["个股"]] if top else [],
        "连板总数": sum(x["只数"] or 0 for x in levels),
        "层级数": len(levels),
        "断层": gaps or "无断层，各层连续",
        "说明": "只含 2 板及以上（首板看 zt_reason）；板数为连续涨停天数",
        "天梯": levels,
    }


# ==================== 3. 题材板块 ====================

def theme_top(date: str = "", with_stocks: bool = False) -> dict:
    """
    题材板块涨停聚集度 —— **判断短线主线的直接读数**。

    每个题材带：涨幅、涨停只数、连板只数、最高标几板、题材连续活跃天数。
    涨停聚集度高 + 连续天数长 = 主线；涨停多但连板少 = 一日游风险。

    with_stocks=True 时附带每个题材的涨停成分股（含首封时间、归因标签），
    体积会大很多，默认关闭。
    """
    d = _norm_date(date)
    j = _ths("block_top", {"date": d})
    out = []
    for b in (j.get("data") or []):
        stocks = b.get("stock_list") or []
        row = {
            "题材代码": b.get("code"),
            "题材": b.get("name"),
            "涨幅%": round(b.get("change"), 2) if b.get("change") is not None else None,
            "涨停只数": b.get("limit_up_num"),
            "连板只数": b.get("continuous_plate_num"),
            "最高标": b.get("high"),
            "题材连续活跃天数": b.get("days"),
        }
        if with_stocks:
            row["成分股"] = [{
                "代码": s.get("code"), "名称": s.get("name"),
                "涨幅%": s.get("change_rate"),
                "连板": s.get("high"),
                "首次封板": _ts(s.get("first_limit_up_time")),
                "涨停归因": s.get("reason_type"),
                "封板类型": {"FIRST_LIMIT": "首次封板",
                             "LIMIT_BACK": "炸板后回封"}.get(s.get("change_tag")),
            } for s in stocks]
        else:
            row["成分股只数"] = len(stocks)
        out.append(row)
    if not out:
        return {"取数时间": _now(), "数据日期": d, "数据源": "同花顺题材板块",
                "题材数": 0, "题材排行": [], **_empty_note(d)}
    return {
        "取数时间": _now(),
        "数据日期": d,
        "数据源": "同花顺题材板块",
        "题材数": len(out),
        "判读": "涨停多 + 连板多 + 连续天数长 = 主线；涨停多但连板少 = 一日游风险",
        "题材排行": out,
    }


# ==================== 4. 东财盘口异动 ====================

def _changes_one(code: str, pagesize: int) -> tuple:
    """取单个 type 码的异动。返回 (全市场总数, 行列表)。"""
    j = _get(EM_CHANGES, {"type": code, "ut": EM_UT, "pageindex": 0,
                          "pagesize": pagesize, "dpt": EM_DPT},
             {"User-Agent": UA})
    if j.get("rc") != 0:
        raise RuntimeError(f"东财异动拒绝 type={code}：rc={j.get('rc')}"
                           f"（检查是否漏了 dpt=wzchanges）")
    data = j.get("data") or {}
    rows = []
    for x in (data.get("allstock") or []):
        tm = str(x.get("tm") or "").zfill(6)
        t = x.get("t")
        rows.append({
            "时间": f"{tm[:2]}:{tm[2:4]}:{tm[4:6]}",
            "代码": x.get("c"),
            "名称": x.get("n"),
            "异动类型": CHANGE_TYPES.get(t, f"未收录码({t})"),
            "类型码": t,
            "原始值": x.get("i"),
        })
    return data.get("tc"), rows


def changes(types: str = "", limit: int = 60) -> dict:
    """
    盘口异动 —— 实时逐笔级别的异动推送（封涨停板/打开涨停板/火箭发射/高台跳水等）。

    **只有当日数据，收盘后清空，不能回溯。**
    types 传空取短线常用四码；多个用逗号分隔，每多一个码多约 0.6 秒。
    中文类型名用东财官方映射（见 CHANGE_TYPES）。
    """
    if types:
        want = [t.strip() for t in str(types).split(",") if t.strip()]
    else:
        want = list(CHANGES_DEFAULT)
    n = max(1, min(int(limit), 1000))
    # 每码取回条数 ≥ n，保证合并后按时间倒序截 n 条不会漏掉更晚的异动。
    # 东财返回是时间倒序（最新在前），tc 字段不受 pagesize 影响（实测
    # pagesize=20/100/500/5000 时 tc 恒为 2877），所以统计始终按全市场算。
    per = min(max(n, 30), 1000)
    rows, stat, errs = [], [], []
    for code in want:
        try:
            tc, part = _changes_one(code, per)
        except Exception as e:
            errs.append({"类型码": code, "错误": f"{type(e).__name__}: {e}"})
            continue
        stat.append({"类型码": code,
                     "官方名": CHANGE_TYPES.get(_as_int(code), f"未收录码({code})"),
                     # 0 条时东财不返回 tc，补成 0 而不是留 None
                     "全市场家次": tc if tc is not None else 0,
                     "本次取回": len(part)})
        rows.extend(part)
    if not stat and errs:
        raise RuntimeError(f"全部类型码取数失败：{errs}")
    rows.sort(key=lambda r: r["时间"], reverse=True)
    out = {
        "取数时间": _now(),
        "数据源": "东方财富盘口异动",
        "查询类型码": ",".join(want),
        "各类型全市场统计": stat,
        "合计取回": len(rows),
        "明细条数": min(n, len(rows)),
        "说明": "只有当日数据，收盘后清空，不能回溯。"
                "「全市场家次」是该类型当日触发总次数（同一只票可多次触发），"
                "不受 limit 影响",
        "类型名来源": "东财官方映射（akshare stock_pankou_em.py 的 symbol_map）",
        "明细": rows[:n],
    }
    if errs:
        out["失败的类型码"] = errs
    return out


def change_types() -> dict:
    """列出盘口异动的 type 码对照表（东财官方映射）。"""
    return {
        "说明": "官方映射，来自 akshare 源码 stock_feature/stock_pankou_em.py "
                "的 symbol_map（22 个码）。不是我推测的名字",
        "⚠️ type 参数只认一个码": "东财接口对 type 只解析第一个码，逗号后面的静默忽略"
                              "（实测 type=4,8201 与 type=4 返回完全一致，都是 344 条"
                              "封涨停板）。所以 get_stock_changes 内部是逐码请求再合并",
        "默认查询的四码": [{"码": c, "官方名": CHANGE_TYPES.get(_as_int(c))}
                       for c in CHANGES_DEFAULT],
        "未收录的码": [{"码": k, "备注": v} for k, v in UNOFFICIAL_CODES.items()],
        "实测始终 0 条": [256, 512],
        "可能 0 条但合法": {"32": "打开跌停板 —— 当日无跌停股时自然 0 条"},
        "对照表": [{"码": k, "官方名": v} for k, v in sorted(CHANGE_TYPES.items())],
    }


# ==================== 5. 短线综合看板 ====================

def shortline_board(date: str = "") -> dict:
    """
    短线综合看板 —— 一次取齐主线判定所需的三层数据。

    题材聚集度（找主线）+ 连板天梯（找最高标）+ 涨停归因热词（找共同逻辑）。
    这是「开盘啦式」看盘的最小充分集，约 4KB。
    """
    d = _norm_date(date)
    th = theme_top(d)
    ld = ladder(d)
    zt = zt_reason(d)
    # 休市日三个子接口都会走 _empty_note 分支，缺 断层/天梯/涨停统计 等键，
    # 这里必须先短路，否则下面按键取值直接 KeyError。
    if zt.get("数据状态") == "无数据" and ld.get("数据状态") == "无数据":
        return {"取数时间": _now(), "数据日期": d, **_empty_note(d)}
    return {
        "取数时间": _now(),
        "数据日期": d,
        "一、主线（题材涨停聚集度 TOP8）": th.get("题材排行", [])[:8],
        "二、最高标（连板天梯）": {
            "最高板": ld.get("最高板"),
            "最高板个股": ld.get("最高板个股", []),
            "连板总数": ld.get("连板总数", 0),
            "断层": ld.get("断层", "无数据"),
            "各层分布": [{"板数": x["板数"], "只数": x["只数"],
                          "个股": [s["名称"] for s in x["个股"]]}
                         for x in ld.get("天梯", [])],
        },
        "三、涨停结构": {
            "交易阶段": zt.get("交易阶段"),
            "涨停统计": zt.get("涨停统计"),
            "跌停统计": zt.get("跌停统计"),
            "曾打开过板只数": zt.get("曾打开过板只数", 0),
            "炸板后回封只数": zt.get("炸板后回封只数", 0),
            "归因热词TOP15": zt.get("归因热词TOP15", []),
        },
        "判读提示": (
            "封板成功率是同花顺官方口径（收盘封板/曾涨停），已用两条恒等式校验；"
            "「上一交易日」由接口自带交易日历给出，跨周末自动正确"
        ),
    }


# ==================== CLI ====================

def _dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: list[str]) -> int:
    cmd = (argv[1] if len(argv) > 1 else "board").lower()
    arg = argv[2] if len(argv) > 2 else ""
    try:
        if cmd == "zt":
            _dump(zt_reason(arg))
        elif cmd == "ladder":
            _dump(ladder(arg))
        elif cmd == "theme":
            _dump(theme_top(arg, with_stocks=(len(argv) > 3)))
        elif cmd == "reason":
            if not arg:
                print("用法: python live_market_ths.py reason <代码> [日期]")
                return 2
            _dump(stock_reason(arg, argv[3] if len(argv) > 3 else ""))
        elif cmd == "chg":
            _dump(changes(arg, int(argv[3]) if len(argv) > 3 else 60))
        elif cmd == "types":
            _dump(change_types())
        elif cmd == "board":
            _dump(shortline_board(arg))
        else:
            print(__doc__)
            return 2
    except Exception as e:                            # noqa: BLE001
        print(f"取数失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
