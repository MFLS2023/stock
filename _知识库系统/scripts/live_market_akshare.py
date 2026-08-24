#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
akshare 补充源 —— **只接我自己四个模块拿不到的数据**，不重复已有能力。

为什么单独一个模块、不塞进 live_market_ex：
  akshare 是同步阻塞库，单接口 0.5-8 秒，光 import 就要约 1 秒（实测 0.99s）。
  MCP server 是常驻进程，在模块顶层 import 会让每次启动都白等 1 秒。
  所以这里**懒加载**：调到才 import，不调就是零成本。

必须在 import akshare **之前** pop 掉 proxy 环境变量。
本机代理 127.0.0.1:7897 会掐断东财请求，而 requests.Session.trust_env=False
只管我自己建的 session，**管不到 akshare 内部自建的 session**。顺序反了就全挂。

四个能力（2026-08-04 逐个实测通过）：
    dept_rank()          营业部排行 + 上榜后 1/2/3/5/10 日涨幅与上涨概率
    earnings_forecast()  业绩预告（预增/预减/预亏 + 变动幅度 + 原因）
    suspension()         停复牌
    bonus_plan()         分红送配 / 送转比例排行

**已确认不可用，别再试：**

  `stock_fund_flow_big_deal` 大单追踪 → HTTP 和 HTTPS **都返回 401**。
      不是网络问题：data.10jqka.com.cn 的 80 和 443 端口实测 0.01 秒就连上，
      是同花顺给 /funds/ddzz/ 这个路径加了鉴权。

  `stock_gsrl_gsdt_em` → 能通，但**它不是高送转**，我旧文档标错了。
      akshare 源码 `stock/stock_gsrl_em.py:13` 写的是「股市日历-公司动态」，
      实测 20260804 的 77 行构成是对外担保 48 / 资产重组 14 / 股份质押 13 /
      资产收购 2，**零条送转**。真正的送转在 `stock_fhps_em`（本模块 bonus_plan）。

命令行：
    python live_market_akshare.py dept       # 营业部排行
    python live_market_akshare.py yjyg       # 业绩预告
    python live_market_akshare.py suspend    # 停复牌
    python live_market_akshare.py bonus      # 送转排行
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
from datetime import datetime

_AK = None          # 懒加载的 akshare 模块对象，只 import 一次


def _ak():
    """
    懒加载 akshare。**清代理必须在 import 之前**，见模块头部说明。
    第二次调用直接返回缓存，不重复 import。
    """
    global _AK
    if _AK is None:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                  "ALL_PROXY", "all_proxy"):
            os.environ.pop(k, None)
        os.environ["NO_PROXY"] = "*"
        import akshare as ak      # noqa: E402 - 顺序不能反，pop 完才能 import
        _AK = ak
    return _AK


def _quiet(fn, *a, **kw):
    """
    调 akshare 并吞掉它的 tqdm 进度条。

    akshare 内部用 `get_tqdm()` 往 **stderr** 打进度条（见
    `akshare/utils/tqdm.py`），MCP 走 stdio 时这些字节会混进传输流。
    实测 stock_fhps_em 一次吐 361-453 字节进度条，必须挡掉。
    """
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        return fn(*a, **kw)


def _num(v):
    """akshare 的数值列常混 str/NaN，统一成 float 或 None。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else round(f, 4)      # f != f 判 NaN


def _rows(df, cols: dict, limit: int) -> list[dict]:
    """
    DataFrame 转 dict 列表。cols 是 {原列名: 输出名}。
    **原列名缺失时跳过而不是抛异常** —— akshare 上游改列名的概率不低，
    宁可少一个字段也不要整个接口报错。
    """
    out = []
    have = set(df.columns)
    for _, r in df.head(limit).iterrows():
        d = {}
        for src, dst in cols.items():
            if src not in have:
                continue
            v = r[src]
            # pandas 的缺失值有多种字面量：float 的 nan、日期列的 NaT、
            # 对象列的 None。全部归一成 None，否则会输出 "NaT" 这种假字符串
            d[dst] = _num(v) if isinstance(v, (int, float)) else (
                None if v is None or str(v) in ("nan", "NaT", "None", "", "-")
                else str(v))
        out.append(d)
    return out


def _stamp(t0: float) -> dict:
    return {"取数时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "耗时": f"{round(time.time() - t0, 2)}s", "数据源": "akshare"}


# ---------- 1. 营业部排行（我原来完全没有的能力） ----------

DEPT_COLS = {
    "营业部名称": "营业部",
    "上榜后1天-买入次数": "1日买入次数",
    "上榜后1天-平均涨幅": "1日平均涨幅%",
    "上榜后1天-上涨概率": "1日上涨概率%",
    "上榜后2天-平均涨幅": "2日平均涨幅%",
    "上榜后2天-上涨概率": "2日上涨概率%",
    "上榜后3天-平均涨幅": "3日平均涨幅%",
    "上榜后3天-上涨概率": "3日上涨概率%",
    "上榜后5天-平均涨幅": "5日平均涨幅%",
    "上榜后5天-上涨概率": "5日上涨概率%",
    "上榜后10天-平均涨幅": "10日平均涨幅%",
    "上榜后10天-上涨概率": "10日上涨概率%",
}


def dept_rank(period: str = "近一月", limit: int = 30,
              min_times: int = 5) -> dict:
    """
    营业部排行 + **上榜后 1/2/3/5/10 日涨幅与上涨概率**。

    这是我自己四个模块拿不到的：`get_lhb_dept` 只能给「某只票的上榜席位」
    和该席位 3 日胜率，拿不到「全市场营业部按胜率排」。跟龙虎榜席位配合用：
    先 get_lhb_dept 看哪个席位买了，再来这里查那个席位的历史胜率。

    period: 近一月 / 近三月 / 近六月 / 近一年
    min_times: 买入次数低于这个数的席位剔掉 —— 上榜 1 次赢 1 次是 100%
               概率，那不是胜率是噪声。默认 5。

    ⚠️ 「上涨概率」是历史统计，**不是预测**。席位换手、营业部改名、
    同一营业部多个交易单元都会让统计失真。
    """
    t0 = time.time()
    ok = ("近一月", "近三月", "近六月", "近一年")
    if period not in ok:
        return {"错误": f"period 只能是 {ok} 之一，收到 {period!r}"}
    df = _quiet(_ak().stock_lhb_yybph_em, symbol=period)
    total = len(df)
    col = "上榜后1天-买入次数"
    if col in df.columns and min_times > 0:
        import pandas as pd
        df = df[pd.to_numeric(df[col], errors="coerce").fillna(0) >= min_times]
    # 按 10 日上涨概率降序 —— 1 日概率噪声大，10 日更能反映席位风格
    key = "上榜后10天-上涨概率"
    if key in df.columns:
        import pandas as pd
        df = df.assign(_k=pd.to_numeric(df[key], errors="coerce")) \
               .sort_values("_k", ascending=False)
    return {"统计周期": period, "全市场营业部数": total,
            f"买入次数>={min_times}": len(df), "返回条数": min(limit, len(df)),
            "排序": "按上榜后10日上涨概率降序",
            "明细": _rows(df, DEPT_COLS, limit),
            "⚠️ 口径": "上涨概率是历史统计不是预测；买入次数少的席位概率不可信，"
                      f"已过滤 <{min_times} 次",
            **_stamp(t0)}


# ---------- 2. 业绩预告 ----------

YJYG_COLS = {
    "股票代码": "代码", "股票简称": "名称", "预告类型": "预告类型",
    "业绩变动幅度": "变动幅度%", "预测指标": "预测指标",
    "业绩变动": "业绩变动", "预测数值": "预测数值",
    "上年同期值": "上年同期", "业绩变动原因": "变动原因",
    "公告日期": "公告日期",
}


def earnings_forecast(period: str = "", limit: int = 30,
                      kind: str = "") -> dict:
    """
    业绩预告。短线用途很直接：业绩预增是涨停归因里的高频标签，
    「中报预增 + 题材」是常见的涨停理由组合。

    period: 报告期 YYYYMMDD，必须是季末（0331/0630/0930/1231）。
            传空自动取最近一个已过去的季末。
    kind: 按预告类型过滤，如「预增」「预减」「略增」「首亏」。传空不过滤。

    ⚠️ 预告是公司自己披露的**预计值**，不是已审计的真实业绩。
    """
    t0 = time.time()
    if not period:
        n = datetime.now()
        ends = [(n.year, 3, 31), (n.year, 6, 30), (n.year, 9, 30),
                (n.year - 1, 12, 31)]
        past = [e for e in ends if datetime(*e) <= n]
        y, m, d = max(past, key=lambda e: datetime(*e)) if past \
            else (n.year - 1, 12, 31)
        period = f"{y}{m:02d}{d:02d}"
    if period[4:] not in ("0331", "0630", "0930", "1231"):
        return {"错误": f"period 必须是季末日期（YYYY0331/0630/0930/1231），"
                        f"收到 {period!r}"}
    df = _quiet(_ak().stock_yjyg_em, date=period)
    total = len(df)
    if df is None or not total:
        return {"报告期": period, "数据状态": "无数据",
                "可能原因": "该报告期预告还没开始披露，或报告期填错",
                **_stamp(t0)}
    kinds = {}
    if "预告类型" in df.columns:
        kinds = {str(k): int(v) for k, v in
                 df["预告类型"].value_counts().head(10).items()}
        if kind:
            df = df[df["预告类型"].astype(str).str.contains(kind, na=False)]
    # 按变动幅度降序，预增幅度大的排前面
    if "业绩变动幅度" in df.columns:
        import pandas as pd
        df = df.assign(_k=pd.to_numeric(df["业绩变动幅度"], errors="coerce")) \
               .sort_values("_k", ascending=False)
    return {"报告期": period, "全部预告数": total,
            "预告类型分布": kinds or "无此字段",
            "过滤": kind or "不过滤", "返回条数": min(limit, len(df)),
            "排序": "按业绩变动幅度降序",
            "明细": _rows(df, YJYG_COLS, limit),
            "⚠️ 口径": "预告是公司自己披露的预计值，不是已审计业绩",
            **_stamp(t0)}


# ---------- 3. 停复牌 ----------

SUSPEND_COLS = {
    "股票代码": "代码", "股票简称": "名称", "交易所": "交易所",
    "停牌时间": "停牌时间", "复牌时间": "复牌时间",
    "停牌事项说明": "停牌原因",
}


def suspension(date: str = "", limit: int = 50) -> dict:
    """
    停复牌。盘前扫一眼，避免把停牌股算进池子里。
    date 传空取今天，格式 YYYYMMDD。
    """
    t0 = time.time()
    d = date or datetime.now().strftime("%Y%m%d")
    if len(d) != 8 or not d.isdigit():
        return {"错误": f"date 格式应为 YYYYMMDD，收到 {date!r}"}
    df = _quiet(_ak().news_trade_notify_suspend_baidu, date=d)
    n = 0 if df is None else len(df)
    if not n:
        return {"日期": d, "停复牌只数": 0,
                "数据状态": "无数据（当日无停复牌，或该日期非交易日）",
                **_stamp(t0)}
    return {"日期": d, "停复牌只数": n, "返回条数": min(limit, n),
            "明细": _rows(df, SUSPEND_COLS, limit),
            "数据源说明": "百度股市通停复牌公告（经 akshare）",
            **_stamp(t0)}


# ---------- 4. 分红送配 / 送转 ----------

BONUS_COLS = {
    "代码": "代码", "名称": "名称",
    "送转股份-送转总比例": "送转总比例(股/10股)",
    "送转股份-送转比例": "送股比例",
    "送转股份-转股比例": "转股比例",
    "现金分红-现金分红比例": "现金分红(元/10股)",
    "现金分红-股息率": "股息率%",
    "方案进度": "方案进度", "预案公告日": "预案公告日",
    "股权登记日": "股权登记日", "除权除息日": "除权除息日",
    "净利润同比增长": "净利润同比增长%",
}


def bonus_plan(period: str = "", limit: int = 30,
               min_ratio: float = 0) -> dict:
    """
    分红送配 / 送转比例排行。**这才是真正的高送转接口**
    （`stock_fhps_em`），我旧文档写的 `stock_gsrl_gsdt_em` 是「公司动态」，
    实测里一条送转都没有，标错了。

    period: 报告期 YYYYMMDD，季末。传空取最近一个已过去的季末。
    min_ratio: 送转总比例下限（单位 股/10股），0 表示只要有送转方案就算。

    ⚠️ **不要写死「≥5 算高送转」这个阈值。** 2026-08-04 实测历年上限：

        2017年报  597 个方案  最大 36.00  ≥5 的 294 只
        2018年报  455 个方案  最大 37.38  ≥5 的 163 只
        2024年报  347 个方案  最大 20.00  ≥5 的   3 只
        2025年报  402 个方案  最大  4.90  ≥5 的   0 只

    2025 年报 402 个方案最大值卡在 4.9，一个 5.0 都没有。这个截断很整齐，
    不像是数据问题，但**我没查到出处**（搜了五轮监管原文全是无关结果），
    所以只记现象不解释原因。实际后果：拿固定阈值筛近年数据会得到 0 只，
    该看的是**当期排行的相对高低**，不是绝对数字。
    """
    t0 = time.time()
    if not period:
        n = datetime.now()
        ends = [(n.year, 3, 31), (n.year, 6, 30), (n.year, 9, 30),
                (n.year - 1, 12, 31)]
        past = [e for e in ends if datetime(*e) <= n]
        y, m, d = max(past, key=lambda e: datetime(*e)) if past \
            else (n.year - 1, 12, 31)
        period = f"{y}{m:02d}{d:02d}"
    if period[4:] not in ("0331", "0630", "0930", "1231"):
        return {"错误": f"period 必须是季末日期，收到 {period!r}"}
    df = _quiet(_ak().stock_fhps_em, date=period)
    total = 0 if df is None else len(df)
    if not total:
        return {"报告期": period, "数据状态": "无数据", **_stamp(t0)}
    import pandas as pd
    col = "送转股份-送转总比例"
    if col not in df.columns:
        return {"报告期": period, "错误": f"上游缺列 {col!r}，可能改了字段名",
                "现有列": list(df.columns)[:20], **_stamp(t0)}
    s = pd.to_numeric(df[col], errors="coerce")
    has = int((s > 0).sum())
    mx = float(s.max()) if s.notna().any() else None
    sub = df[s > max(min_ratio, 0)] if min_ratio > 0 else df[s > 0]
    sub = sub.assign(_k=pd.to_numeric(sub[col], errors="coerce")) \
             .sort_values("_k", ascending=False)
    return {"报告期": period, "全部方案数": total, "有送转方案": has,
            "本期送转总比例最大值": round(mx, 2) if mx is not None else None,
            "筛选下限": min_ratio or "只要有送转",
            "符合条数": len(sub), "返回条数": min(limit, len(sub)),
            "排序": "按送转总比例降序",
            "明细": _rows(sub, BONUS_COLS, limit),
            "⚠️ 阈值口径": "不要写死「≥5 算高送转」。实测 2017 年报最大 36、"
                          "2025 年报最大 4.9，同一阈值在近年会筛出 0 只。"
                          "看当期排行的相对高低，不看绝对数字",
            **_stamp(t0)}


# ---------- CLI ----------

def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    cmd = argv[1] if len(argv) > 1 else "dept"
    a2 = argv[2] if len(argv) > 2 else ""
    p = lambda o: print(json.dumps(o, ensure_ascii=False, indent=2))  # noqa: E731
    if cmd == "dept":
        r = dept_rank(a2 or "近一月", 8)
    elif cmd == "yjyg":
        r = earnings_forecast(a2, 8)
    elif cmd == "suspend":
        r = suspension(a2, 20)
    elif cmd == "bonus":
        r = bonus_plan(a2, 8)
    else:
        print(__doc__)
        print("用法：dept [近一月|近三月|近六月|近一年] | yjyg [YYYYMMDD] "
              "| suspend [YYYYMMDD] | bonus [YYYYMMDD]")
        return 1
    p(r)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
