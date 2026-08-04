#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
实时行情扩展模块 —— 补齐 live_market.py 拿不到的四类数据。

全部免费、无需注册、无需 API Key。接口来自 2026-08-04 的 37 接口全域探测
（29 个可用），每个函数的 docstring 记录了实测校验过程，别凭猜改。

四类新能力：
  1. 东财股池（push2ex.eastmoney.com）—— 涨停/炸板/跌停/强势/昨涨停/次新
     直接给首次封板时间、炸板次数、连板数、封单额，clist 接口推不出这些
  2. 分钟 K 线 —— 腾讯 ifzq 主源 + 新浪兜底，可回溯约 3 个月
  3. 分钟级资金流 —— 东财 fflow，主力/超大单/大单/中单/小单
  4. 盘后数据（datacenter-web.eastmoney.com）—— 龙虎榜、营业部、大宗、北向、两融

用法：
    python live_market_ex.py zt              # 涨停池（带首封时间/炸板/连板）
    python live_market_ex.py zb              # 炸板池
    python live_market_ex.py yzt             # 昨涨停今表现（弱转强证据）
    python live_market_ex.py qs              # 强势池
    python live_market_ex.py mk 300308 5     # 5分钟K线
    python live_market_ex.py ff 300308       # 分钟资金流
    python live_market_ex.py lhb 2026-08-03  # 龙虎榜
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime

import live_market as lm

UA = lm.UA

# ---------- 东财股池专用接口 ----------

PUSH2EX = "https://push2ex.eastmoney.com"
# 这个 ut 是东财网页端的公开固定值，不是密钥，页面 JS 里硬编码
EM_UT = "7eea3edcaed734bea9cbfc24409ed989"

def _hhmmss(v) -> str | None:
    """
    东财股池的时间字段是整数 HHMMSS，如 93130 → 09:31:30。
    实测校验（2026-08-04）：利通电子 fbt=93130，分时数据显示 09:32 那根首次
    触及涨停价 102.41，两者吻合（分时是分钟收盘，股池是秒级成交）。
    """
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return f"{v // 10000:02d}:{v // 100 % 100:02d}:{v % 100:02d}"


def _px(v, div=1000):
    """股池的价格字段是放大 1000 倍的整数。p=102410 → 102.41。"""
    return None if v is None else round(v / div, 2)


def _pool(api: str, sort: str, date: str = "", pagesize: int = 300) -> list[dict]:
    """
    请求东财股池接口。返回原始 pool 列表。

    date 传空则用今天。注意这个接口 **只有 push2ex 域名**，
    和行情用的 push2delay 不是一套，实测 push2ex 本机可达（0.8s）。
    """
    date = date or datetime.now().strftime("%Y%m%d")
    sess = lm._shared_session()
    last = None
    for attempt in range(3):
        try:
            lm._throttle()
            r = sess.get(f"{PUSH2EX}/{api}", headers=UA, timeout=12, params={
                "ut": EM_UT, "dpt": "wz.ztzt", "Pageindex": 0,
                "pagesize": pagesize, "sort": sort, "date": date,
            })
            r.raise_for_status()
            d = r.json().get("data")
            # pool 为空是合法结果（比如今天没有跌停股），不重试
            return (d or {}).get("pool") or []
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            if attempt < 2:
                time.sleep(0.4 * (2 ** attempt))
    raise RuntimeError(f"东财股池 {api} 取数失败：{last}")


def zt_pool(date: str = "") -> dict:
    """
    涨停池。**比 live_market.limit_up_pool() 强的地方**：直接给出
    首次封板时间、最后封板时间、炸板次数、连板数、封单额，
    这些是 clist 行情接口推不出来的。

    字段实测校验（2026-08-04）：
      fund（封单额）与腾讯买一挂单逐一吻合 —— 利通电子东财 7.69 亿 vs
      腾讯 102.41×75073 手 = 7.69 亿；行云 5.21 vs 5.21；宏景 3.16 vs 3.16。
      所以 fund 就是封单额，不是成交额（成交额是 amount）。

      zbc（炸板次数）：行云科技 zbc=3，与分时还原的 09:56–10:08 脱离涨停
      13 分钟一致（期间反复打开 3 次）。
    """
    rows = _pool("getTopicZTPool", "fbt:asc", date)
    out = []
    for x in rows:
        amt = x.get("amount") or 0
        fund = x.get("fund") or 0
        tj = x.get("zttj") or {}
        out.append({
            "代码": x.get("c"),
            "名称": x.get("n"),
            "最新价": _px(x.get("p")),
            "涨跌幅": round(x.get("zdp") or 0, 2),
            "首次封板": _hhmmss(x.get("fbt")),
            "最后封板": _hhmmss(x.get("lbt")),
            "炸板次数": x.get("zbc"),
            "连板数": x.get("lbc"),
            "N天M板": f"{tj.get('days')}天{tj.get('ct')}板" if tj else None,
            "封单(亿)": round(fund / 1e8, 2),
            "成交额(亿)": round(amt / 1e8, 2),
            "封成比%": round(fund / amt * 100, 1) if amt else None,
            "换手率": round(x.get("hs") or 0, 2),
            "流通市值(亿)": round((x.get("ltsz") or 0) / 1e8, 1),
            "行业": x.get("hybk"),
        })
    return {
        "取数时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "交易日": date or datetime.now().strftime("%Y%m%d"),
        "涨停家数": len(out),
        "行业分布": lm._top_industries(out, 12),
        "曾炸板只数": sum(1 for x in out if (x["炸板次数"] or 0) > 0),
        "明细": out,
    }


def zb_pool(date: str = "") -> dict:
    """
    炸板池 —— 曾涨停但当前已打开的票。**情绪转弱的直接读数**。
    炸板池家数 / (涨停家数 + 炸板家数) 就是市场的封板失败率。
    """
    rows = _pool("getTopicZBPool", "fbt:asc", date)
    out = []
    for x in rows:
        out.append({
            "代码": x.get("c"), "名称": x.get("n"),
            "最新价": _px(x.get("p")), "涨停价": _px(x.get("ztp")),
            "涨跌幅": round(x.get("zdp") or 0, 2),
            "首次封板": _hhmmss(x.get("fbt")),
            "炸板次数": x.get("zbc"),
            "振幅": round(x.get("zf") or 0, 2),
            "涨速": round(x.get("zs") or 0, 2),
            "成交额(亿)": round((x.get("amount") or 0) / 1e8, 2),
            "换手率": round(x.get("hs") or 0, 2),
            "行业": x.get("hybk"),
        })
    return {
        "取数时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "炸板家数": len(out),
        "说明": "曾涨停但当前打开。炸板率 = 炸板数/(涨停数+炸板数)，是情绪转弱的直接读数",
        "明细": out,
    }


def yesterday_zt(date: str = "") -> dict:
    """
    昨日涨停股的今日表现 —— **弱转强 / 反包的直接证据**，
    库内「弱转强」概念要落地验证就靠这个接口。

    zs 字段是「昨涨停今日的溢价率」，yfbt 是昨日首封时间，ylbc 是昨日连板数。
    """
    rows = _pool("getYesterdayZTPool", "zs:desc", date)
    out = []
    up = flat = down = 0
    for x in rows:
        pct = round(x.get("zdp") or 0, 2)
        if pct > 0.5:
            up += 1
        elif pct < -0.5:
            down += 1
        else:
            flat += 1
        tj = x.get("zttj") or {}
        out.append({
            "代码": x.get("c"), "名称": x.get("n"),
            "最新价": _px(x.get("p")), "涨停价": _px(x.get("ztp")),
            "今日涨跌幅": pct,
            "溢价率": round(x.get("zs") or 0, 2),
            "振幅": round(x.get("zf") or 0, 2),
            "昨日首封": _hhmmss(x.get("yfbt")),
            "昨日连板数": x.get("ylbc"),
            "N天M板": f"{tj.get('days')}天{tj.get('ct')}板" if tj else None,
            "成交额(亿)": round((x.get("amount") or 0) / 1e8, 2),
            "换手率": round(x.get("hs") or 0, 2),
            "行业": x.get("hybk"),
        })
    n = len(out)
    return {
        "取数时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "昨涨停只数": n,
        "今日红盘": up, "今日平盘": flat, "今日绿盘": down,
        "昨涨停今日赚钱效应": f"{up}/{n} ({up / n * 100:.0f}%)" if n else None,
        "说明": "昨日涨停股的今日表现。红盘率是情绪延续性的核心读数，弱转强/反包看这里",
        "明细": out,
    }


def qs_pool(date: str = "") -> dict:
    """
    强势池 —— 涨幅靠前但未必涨停的票。
    nh=1 表示创新高，cc 是连续上涨天数，lb 是量比。
    """
    rows = _pool("getTopicQSPool", "zdp:desc", date, pagesize=200)
    out = []
    for x in rows:
        out.append({
            "代码": x.get("c"), "名称": x.get("n"),
            "最新价": _px(x.get("p")), "涨停价": _px(x.get("ztp")),
            "涨跌幅": round(x.get("zdp") or 0, 2),
            "是否新高": "是" if x.get("nh") else "否",
            "连涨天数": x.get("cc"),
            "量比": round(x.get("lb") or 0, 2),
            "涨速": round(x.get("zs") or 0, 2),
            "成交额(亿)": round((x.get("amount") or 0) / 1e8, 2),
            "换手率": round(x.get("hs") or 0, 2),
            "行业": x.get("hybk"),
        })
    return {
        "取数时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "强势股数": len(out),
        "创新高只数": sum(1 for x in out if x["是否新高"] == "是"),
        "明细": out,
    }


def dt_pool(date: str = "") -> dict:
    """跌停池。空列表是合法结果（今天没跌停股就返回 0 只）。"""
    rows = _pool("getTopicDTPool", "fund:asc", date)
    out = [{
        "代码": x.get("c"), "名称": x.get("n"),
        "最新价": _px(x.get("p")), "涨跌幅": round(x.get("zdp") or 0, 2),
        "封单(亿)": round((x.get("fund") or 0) / 1e8, 2),
        "成交额(亿)": round((x.get("amount") or 0) / 1e8, 2),
        "换手率": round(x.get("hs") or 0, 2),
        "连续跌停": x.get("lbc"), "行业": x.get("hybk"),
    } for x in rows]
    return {
        "取数时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "跌停家数": len(out), "明细": out,
    }


def cx_pool(date: str = "") -> dict:
    """次新股池。ods 是上市天数，od/ipod 是上市日期。"""
    rows = _pool("getTopicCXPool", "zdp:desc", date, pagesize=150)
    out = [{
        "代码": x.get("c"), "名称": x.get("n"),
        "最新价": _px(x.get("p")), "涨跌幅": round(x.get("zdp") or 0, 2),
        "上市天数": x.get("ods"), "上市日": x.get("ipod"),
        "是否新高": "是" if x.get("nh") else "否",
        "成交额(亿)": round((x.get("amount") or 0) / 1e8, 2),
        "换手率": round(x.get("hs") or 0, 2), "行业": x.get("hybk"),
    } for x in rows]
    return {
        "取数时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "次新股数": len(out), "明细": out,
    }


# ---------- 分钟 K 线（补历史分时的洞）----------

TX_MK = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
SINA_K = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php"
          "/CN_MarketData.getKLineData")
VALID_MIN = (1, 5, 15, 30, 60)
TX_MAX = 320   # 腾讯 mkline 单次硬上限，实测请求 3000 只回 320


def _sym(code: str) -> str:
    return ("sh" if code.startswith(("60", "68", "51", "58", "11")) else "sz") + code


def _mk_tx(sym: str, period: int, n: int) -> list[dict]:
    """腾讯 mkline。返回 [时间, 开, 收, 高, 低, 量]，时间格式 202608041030。"""
    r = lm._shared_session().get(TX_MK, params={"param": f"{sym},m{period},,{n}"},
                                 headers=UA, timeout=10)
    out = []
    for p in r.json()["data"][sym][f"m{period}"]:
        t = p[0]
        out.append({
            "时间": f"{t[:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}",
            "开": float(p[1]), "收": float(p[2]),
            "高": float(p[3]), "低": float(p[4]),
            "量(手)": int(float(p[5])),
        })
    return out


def _mk_sina(sym: str, period: int, n: int) -> list[dict]:
    """新浪分钟K。字段 day/open/high/low/close/volume，volume 是股不是手。"""
    r = lm._shared_session().get(SINA_K, headers=UA, timeout=15, params={
        "symbol": sym, "scale": period, "ma": "no", "datalen": n,
    })
    return [{
        "时间": x["day"][:16], "开": float(x["open"]), "收": float(x["close"]),
        "高": float(x["high"]), "低": float(x["low"]),
        "量(手)": int(x["volume"]) // 100,
    } for x in r.json()]


def minute_kline(code: str, period: int = 5, n: int = 320) -> list[dict]:
    """
    分钟 K 线，可回溯历史。这补上了 live_market.minutes() 只能取当日的洞。

    **源的选择由 n 决定，不是简单的主备降级**：
      n <= 320  → 腾讯优先（响应快 0.1s），失败降新浪
      n >  320  → 新浪优先（腾讯硬上限 320 根，请求 3000 也只回 320），失败降腾讯

    实测（2026-08-04）：
      - 域名必须是 `ifzq.gtimg.cn`，**不能带 web. 前缀**。`web.ifzq.gtimg.cn`
        会被解析到 web3.ifzq.gtimg.cn 然后报 SSLError: UNEXPECTED_EOF_WHILE_READING。
        去掉 web. 后连打 5 次全成功
      - 新浪无硬上限：datalen=1000/1500/3000 分别回溯到 07-07 / 06-22 / 05-07
      - 腾讯请求 3000 实测只回 320 根，所以长历史必须走新浪

    period 只支持 1/5/15/30/60 分钟。
    """
    if period not in VALID_MIN:
        raise ValueError(f"period 只支持 {VALID_MIN}，收到 {period}")
    sym = _sym(code)
    # n 超过腾讯上限时反转优先级，否则长历史请求会被腾讯截断到 320 根
    order = (_mk_sina, _mk_tx) if n > TX_MAX else (_mk_tx, _mk_sina)
    errs = []
    for fn in order:
        try:
            out = fn(sym, period, n)
            if out:
                return out[-n:]
            errs.append(f"{fn.__name__} 返回空")
        except Exception as e:  # noqa: BLE001 - 逐源降级
            errs.append(f"{fn.__name__}: {type(e).__name__} {e}")
    raise RuntimeError(f"{code} {period}分钟K线取数失败：{' | '.join(errs)}")


def intraday_shape(code: str, date: str = "") -> dict:
    """
    某一天的日内形态量化 —— 冲高回落、破均价、最大回撤全部算成数字。

    用 5 分钟 K 线还原（东财分时接口只给当日，历史日内只能靠分钟 K）。
    date 传空则取数据里最后一个交易日；格式 YYYY-MM-DD。

    输出的「收距最高%」就是冲高回落幅度：接近 0 说明收在最高，
    -5% 以下就是明显的冲高回落。
    """
    rows = minute_kline(code, 5, 3000)
    days: dict[str, list[dict]] = {}
    for k in rows:
        days.setdefault(k["时间"][:10], []).append(k)
    if not days:
        raise RuntimeError(f"{code}: 分钟K线为空")
    day = date or sorted(days)[-1]
    if day not in days:
        raise RuntimeError(f"{code}: {day} 无数据，可选范围 {sorted(days)[0]} ~ {sorted(days)[-1]}")
    r = days[day]

    op = r[0]["开"]
    cl = r[-1]["收"]
    hi = max(r, key=lambda x: x["高"])
    lo = min(r, key=lambda x: x["低"])
    # 均价线（VWAP 近似：分钟收盘按成交量加权）
    vol = sum(x["量(手)"] for x in r) or 1
    vwap = sum(x["收"] * x["量(手)"] for x in r) / vol
    above = sum(1 for x in r if x["收"] >= vwap)
    # 逐根最大回撤
    peak = -1.0
    mdd = 0.0
    mdd_t = ""
    for x in r:
        if x["高"] > peak:
            peak = x["高"]
        dd = (x["低"] - peak) / peak * 100
        if dd < mdd:
            mdd, mdd_t = dd, x["时间"][11:]
    idx = r.index(hi)
    return {
        "代码": code, "交易日": day, "K线根数": len(r),
        "首根开盘": op, "末根收盘": cl,
        "最高": hi["高"], "最高时间": hi["时间"][11:],
        "最低": lo["低"], "最低时间": lo["时间"][11:],
        "收距最高%": round((cl - hi["高"]) / hi["高"] * 100, 2),
        "收距最低%": round((cl - lo["低"]) / lo["低"] * 100, 2),
        "均价线": round(vwap, 2),
        "收盘距均价%": round((cl - vwap) / vwap * 100, 2),
        "在均价上方占比": f"{above}/{len(r)} ({above / len(r) * 100:.0f}%)",
        "日内最大回撤%": round(mdd, 2), "最大回撤时间": mdd_t,
        "最高点位置": f"第{idx + 1}/{len(r)}根（{'前半场' if idx < len(r) / 2 else '后半场'}）",
        "说明": "收距最高% 接近 0 = 收在最高；低于 -5% = 明显冲高回落",
    }


# ---------- 分钟级资金流（补资金流的洞）----------

def fund_flow_min(code: str, tail: int = 60) -> dict:
    """
    分钟级资金流。东财 fflow 接口，**这是累计值不是增量值**。

    字段顺序实测校验（2026-08-04 中际旭创）：
      f51=时间, f52=主力, f53=小单, f54=中单, f55=大单, f56=超大单
      校验1：大单 + 超大单 = 主力，差 0.0（4.37 + 44.91 = 49.28）
      校验2：主力 + 小单 + 中单 = 0.0
    两条恒等式都严格成立，字段顺序确认无误。

    注意 klt=1 只有当日数据；日频资金流 daykline 接口在 push2delay 返回空，
    push2his 本机不可达，所以历史日频资金流走 stock_data MCP 的 stock_fund_flow。
    """
    d = lm._em_get("/api/qt/stock/fflow/kline/get", {
        "lmt": 0, "klt": 1, "secid": lm._secid(code),
        "fields1": "f1,f2,f3,f7", "fields2": "f51,f52,f53,f54,f55,f56",
    })
    rows = []
    for line in d.get("klines") or []:
        p = line.split(",")
        try:
            v = [float(x) for x in p[1:6]]
        except ValueError:
            continue
        rows.append({
            "时间": p[0][11:] if len(p[0]) > 11 else p[0],
            "主力净流入(亿)": round(v[0] / 1e8, 2),
            "小单(亿)": round(v[1] / 1e8, 2),
            "中单(亿)": round(v[2] / 1e8, 2),
            "大单(亿)": round(v[3] / 1e8, 2),
            "超大单(亿)": round(v[4] / 1e8, 2),
        })
    if not rows:
        raise RuntimeError(f"{code}: 分钟资金流为空（可能是非交易时段或代码错误）")

    last = rows[-1]
    # 每 30 分钟采样一次，看主力资金的进出节奏
    pace = [r for i, r in enumerate(rows) if i % 30 == 0 or i == len(rows) - 1]
    return {
        "代码": code, "名称": d.get("name"),
        "取数时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "口径": "累计值（当日开盘至该分钟的累计净流入），不是每分钟增量",
        "当前主力净流入(亿)": last["主力净流入(亿)"],
        "当前超大单(亿)": last["超大单(亿)"],
        "当前大单(亿)": last["大单(亿)"],
        "恒等式校验": "大单+超大单=主力，主力+小单+中单=0",
        "30分钟节奏": pace,
        "明细": rows[-tail:],
    }


# ---------- 盘后数据中心 ----------

DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"


class NoData(Exception):
    """查无数据 —— 是合法结果，不是故障。比如龙虎榜盘中查当天。"""


def _dc(report: str, extra: dict, retries: int = 3) -> list[dict]:
    """
    请求东财数据中心。

    **返回码语义（实测 2026-08-04，必须区分，否则会把"没数据"当成"接口坏"）：**
      code=0    success=True   → 正常有数据
      code=9201 success=False  → "返回数据为空"，查无数据，是合法结果
      其他       success=False  → 真故障。如 reportName 写错返回"报表配置不存在"
                                （`RPT_BILLBOARD_TRADEDETAIL` 就是错的，
                                 正确的是 `RPT_DAILYBILLBOARD_DETAILSNEW`）

    9201 抛 NoData，由调用方决定是返回空列表还是提示换日期。
    """
    sess = lm._shared_session()
    params = {"reportName": report, "columns": "ALL", "pageNumber": 1,
              "pageSize": 50, "source": "WEB", "client": "WEB"}
    params.update(extra)
    last = None
    for attempt in range(retries):
        try:
            lm._throttle()
            r = sess.get(DC, params=params, headers=UA, timeout=15)
            r.raise_for_status()
            j = r.json()
            if not j.get("success"):
                if j.get("code") == 9201:
                    raise NoData(str(j.get("message") or "返回数据为空"))
                raise RuntimeError(f"东财数据中心拒绝：code={j.get('code')} {j.get('message')}")
            return ((j.get("result") or {}) or {}).get("data") or []
        except NoData:
            raise                       # 查无数据，重试无意义
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            if attempt < retries - 1:
                time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError(f"东财数据中心 {report} 取数失败：{last}")


def _last_trade_day() -> str:
    """
    最近一个已收盘的交易日（YYYY-MM-DD）。用于盘后数据的默认日期。

    判定规则：15:00 前算「今天还没收盘」，往前找上一个工作日。
    **不含节假日日历**，遇长假可能给出休市日，此时接口返回 NoData，
    调用方会提示换日期，不会返回假数据。
    """
    from datetime import timedelta
    d = datetime.now()
    if d.hour < 15:
        d -= timedelta(days=1)
    while d.weekday() >= 5:             # 5=周六 6=周日
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def lhb(date: str = "", limit: int = 40) -> dict:
    """
    龙虎榜个股明细。date 格式 YYYY-MM-DD，传空自动取最近已收盘交易日。

    **龙虎榜盘后才发布**，盘中查当天东财返回 code=9201「返回数据为空」，
    此时函数返回 `数据状态: 尚未发布` 而不是抛错，并给出建议日期。
    实测（2026-08-04 14:43 盘中）：查 08-04 → 9201；查 08-03 → 58 只。

    D1/D2/D5/D10_CLOSE_ADJCHRATE 是上榜后 1/2/5/10 日涨跌幅，
    可以直接验证「上龙虎榜之后怎么走」。
    """
    date = date or _last_trade_day()
    try:
        rows = _dc("RPT_DAILYBILLBOARD_DETAILSNEW", {
            "filter": f"(TRADE_DATE='{date}')", "pageSize": max(limit, 50),
            "sortColumns": "BILLBOARD_NET_AMT", "sortTypes": -1,
        })
    except NoData as e:
        return {"交易日": date, "上榜只数": 0, "数据状态": f"尚未发布（{e}）",
                "说明": "龙虎榜盘后才发布，盘中查当天无数据是正常的",
                "建议日期": _last_trade_day(), "明细": []}
    out = []
    for x in rows[:limit]:
        out.append({
            "代码": x.get("SECURITY_CODE"), "名称": x.get("SECURITY_NAME_ABBR"),
            "收盘价": x.get("CLOSE_PRICE"), "涨跌幅": x.get("CHANGE_RATE"),
            "换手率": x.get("TURNOVERRATE"),
            "上榜净买(万)": round(x["BILLBOARD_NET_AMT"] / 1e4, 1)
                            if x.get("BILLBOARD_NET_AMT") is not None else None,
            "上榜成交额(万)": round(x["BILLBOARD_DEAL_AMT"] / 1e4, 1)
                              if x.get("BILLBOARD_DEAL_AMT") is not None else None,
            "占总成交比%": x.get("DEAL_AMOUNT_RATIO"),
            "上榜原因": x.get("EXPLAIN"),
            "次日涨跌%": x.get("D1_CLOSE_ADJCHRATE"),
            "5日涨跌%": x.get("D5_CLOSE_ADJCHRATE"),
            "10日涨跌%": x.get("D10_CLOSE_ADJCHRATE"),
        })
    return {
        "交易日": date, "上榜只数": len(rows), "数据状态": "已发布",
        "明细": out,
    }


def lhb_dept(date: str = "", code: str = "", limit: int = 40) -> dict:
    """
    龙虎榜营业部明细 —— 席位性质是判断游资 / 机构的直接依据。
    OPERATEDEPT_NAME 里含「机构专用」就是机构席位，其余是营业部（游资）。

    date 传空自动取最近已收盘交易日。code 传空为全市场。
    """
    date = date or _last_trade_day()
    flt = f"(TRADE_DATE='{date}')"
    if code:
        flt += f"(SECURITY_CODE=\"{code}\")"
    try:
        rows = _dc("RPT_BILLBOARD_DAILYDETAILSBUY", {
            "filter": flt, "pageSize": max(limit, 50),
            "sortColumns": "NET", "sortTypes": -1,
        })
    except NoData as e:
        return {"交易日": date, "代码": code or "全市场", "席位数": 0,
                "数据状态": f"尚未发布或该票未上榜（{e}）",
                "建议日期": _last_trade_day(), "明细": []}
    out = []
    inst = 0
    for x in rows[:limit]:
        name = x.get("OPERATEDEPT_NAME") or ""
        is_inst = "机构专用" in name
        inst += is_inst
        out.append({
            "代码": x.get("SECURITY_CODE"), "营业部": name,
            "席位性质": "机构" if is_inst else "营业部(游资)",
            "买入(万)": round(x["BUY"] / 1e4, 1) if x.get("BUY") is not None else None,
            "卖出(万)": round(x["SELL"] / 1e4, 1) if x.get("SELL") is not None else None,
            "净额(万)": round(x["NET"] / 1e4, 1) if x.get("NET") is not None else None,
            "上榜原因": x.get("EXPLANATION"),
            "该席位3日胜率": x.get("RISE_PROBABILITY_3DAY"),
        })
    return {
        "交易日": date, "代码": code or "全市场",
        "席位数": len(rows), "其中机构席位": inst,
        "明细": out,
    }


def block_trade(date: str = "", limit: int = 40) -> dict:
    """
    大宗交易。折溢价率反映大额筹码的转手意愿。
    date 传空自动取最近已收盘交易日，格式 YYYY-MM-DD。
    """
    date = date or _last_trade_day()
    try:
        rows = _dc("RPT_DATA_BLOCKTRADE", {
            "filter": f"(TRADE_DATE='{date}')", "pageSize": max(limit, 50),
        })
    except NoData as e:
        return {"交易日": date, "笔数": 0, "数据状态": f"尚未发布（{e}）",
                "建议日期": _last_trade_day(), "明细": []}
    out = [{
        "代码": x.get("SECURITY_CODE"), "名称": x.get("SECURITY_NAME_ABBR"),
        "成交价": x.get("DEAL_PRICE"), "收盘价": x.get("CLOSE_PRICE"),
        "折溢价率%": x.get("PREMIUM_RATIO"),
        "成交量(万股)": round(x["DEAL_VOLUME"] / 1e4, 1)
                        if x.get("DEAL_VOLUME") is not None else None,
        "成交额(万)": round(x["DEAL_AMT"] / 1e4, 1)
                      if x.get("DEAL_AMT") is not None else None,
        "买方": x.get("BUYER_NAME"), "卖方": x.get("SELLER_NAME"),
    } for x in rows[:limit]]
    return {"交易日": date, "笔数": len(rows), "数据状态": "已发布", "明细": out}


def margin_detail(limit: int = 30) -> dict:
    """
    两融个股明细，按日期降序。融资余额变化是杠杆资金意愿的读数。

    不传日期，直接拉最新可得的那一天（两融数据 T+1 发布，
    所以最新一条通常是上一个交易日）。实测这个接口较慢，约 8 秒。
    """
    try:
        rows = _dc("RPTA_WEB_RZRQ_GGMX", {
            "pageSize": max(limit, 50), "sortColumns": "date", "sortTypes": -1,
        })
    except NoData as e:
        return {"取数时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "条数": 0, "数据状态": f"无数据（{e}）", "明细": []}
    out = [{
        "日期": str(x.get("DATE"))[:10], "代码": x.get("SCODE"),
        "名称": x.get("SECNAME"), "市场": x.get("MARKET"),
        "收盘价": x.get("SPJ"), "涨跌幅": x.get("ZDF"),
        "融资余额(万)": round(x["RZYE"] / 1e4, 1) if x.get("RZYE") is not None else None,
        "融资买入(万)": round(x["RZMRE"] / 1e4, 1) if x.get("RZMRE") is not None else None,
        "融券余量(万股)": round(x["RQYL"] / 1e4, 1) if x.get("RQYL") is not None else None,
    } for x in rows[:limit]]
    return {"取数时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "条数": len(out), "明细": out}


# ---------- 组合分析：盘后复盘一次取齐 ----------

def after_close(date: str = "") -> dict:
    """
    盘后复盘全套。一次取齐涨停池、炸板池、昨涨停今表现、跌停、强势池，
    自动算出封板失败率和情绪延续性。做每日复盘用这个。
    """
    zt = zt_pool(date)
    zb = zb_pool(date)
    yzt = yesterday_zt(date)
    dt = dt_pool(date)
    n_zt, n_zb = zt["涨停家数"], zb["炸板家数"]
    tot = n_zt + n_zb
    return {
        "交易日": zt["交易日"],
        "取数时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "情绪读数": {
            "涨停家数": n_zt,
            "炸板家数": n_zb,
            "跌停家数": dt["跌停家数"],
            "封板成功率": f"{n_zt}/{tot} ({n_zt / tot * 100:.0f}%)" if tot else None,
            "炸板率": f"{n_zb}/{tot} ({n_zb / tot * 100:.0f}%)" if tot else None,
            "涨停股中曾炸板": zt["曾炸板只数"],
            "昨涨停今日赚钱效应": yzt["昨涨停今日赚钱效应"],
            "涨停行业分布": zt["行业分布"],
        },
        "涨停池": zt["明细"],
        "炸板池": zb["明细"],
        "昨涨停今表现": yzt["明细"],
        "跌停池": dt["明细"],
    }


# ---------- CLI ----------

def _p(o):
    print(json.dumps(o, ensure_ascii=False, indent=2))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd, a = argv[1], argv[2:]
    try:
        if cmd == "zt":
            _p(zt_pool(a[0] if a else ""))
        elif cmd == "zb":
            _p(zb_pool(a[0] if a else ""))
        elif cmd == "yzt":
            _p(yesterday_zt(a[0] if a else ""))
        elif cmd == "qs":
            _p(qs_pool(a[0] if a else ""))
        elif cmd == "dt":
            _p(dt_pool(a[0] if a else ""))
        elif cmd == "cx":
            _p(cx_pool(a[0] if a else ""))
        elif cmd == "mk":
            _p(minute_kline(a[0], int(a[1]) if len(a) > 1 else 5,
                            int(a[2]) if len(a) > 2 else 320))
        elif cmd == "shape":
            _p(intraday_shape(a[0], a[1] if len(a) > 1 else ""))
        elif cmd == "ff":
            _p(fund_flow_min(a[0]))
        elif cmd == "lhb":
            _p(lhb(a[0]))
        elif cmd == "dept":
            _p(lhb_dept(a[0], a[1] if len(a) > 1 else ""))
        elif cmd == "bt":
            _p(block_trade(a[0]))
        elif cmd == "margin":
            _p(margin_detail())
        elif cmd == "close":
            _p(after_close(a[0] if a else ""))
        else:
            print(f"未知命令 {cmd}")
            print(__doc__)
            return 1
    except Exception as e:  # noqa: BLE001 - CLI 边界，明确报错不返回假数据
        print(f"取数失败: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

