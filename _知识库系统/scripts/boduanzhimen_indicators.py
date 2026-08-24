#!/usr/bin/env python3
"""波段之门指标公式的 Python 实现。

来源：`波段之门/付费文章/**/*.txt` 共 20 个通达信公式文件，去重后 12 个独立公式。
每个函数的 docstring 里附原始通达信代码和它出自哪篇文章，便于回查。

翻译时按通达信语义实现，不做"改进"：
- `MA(X,N)` 简单移动平均，前 N-1 根为 NaN
- `SMA(X,N,M)` 通达信的加权递推：Y = (M*X + (N-M)*Y') / N，与 pandas 的 ewm 不同，
  必须手写递推。KDJ 的 K/D 用的就是这个，用 ewm 算出来的值和通达信不一致。
- `REF(X,N)` 向前引用 N 根
- `HHV/LLV(X,N)` N 根内最高/最低
- `COUNT(cond,N)` N 根内条件成立的次数
- `EVERY(cond,N)` N 根内条件全部成立
- `BARSLAST(cond)` 距上次条件成立过了几根
- `BARSLASTCOUNT(cond)` 条件连续成立的根数

注意 `INDEXC`（大盘收盘价）在通达信里是自动对齐的同期指数，这里必须由调用方
显式传入指数序列，否则强度指标无法计算——这是原公式里唯一依赖外部数据的部分。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "ma", "sma_tdx", "ema", "ref", "hhv", "llv", "count", "every",
    "barslast", "barslastcount", "std_tdx", "cross",
    "kdj", "macd", "boll",
    "strength_lines", "do_t_ma5_trend", "ma_slope_color",
    # 原来这里写了 double_signal，但模块里没有这个函数，导致
    # `from boduanzhimen_indicators import *` 直接 AttributeError。已删。
    "position_line", "kd_dull", "kdj_battle",
    "select_doubling", "select_shock_market", "select_11_stocks",
    "select_ma_climb", "select_sector_strong", "boll_top_risk",
    "volume_price_rule", "macd_zero_cross", "sell_next_day",
    "eleven_sequence", "REGISTRY",
]


# ---------- 通达信基础函数 ----------

def ma(series: pd.Series, n: int) -> pd.Series:
    """MA(X,N)：简单移动平均。"""
    return series.rolling(n, min_periods=n).mean()


def sma_tdx(series: pd.Series, n: int, m: int) -> pd.Series:
    """SMA(X,N,M)：通达信加权递推均值，Y = (M*X + (N-M)*Y') / N。

    不能用 pandas 的 ewm 代替：ewm 的 alpha 定义和这里的 M/N 不等价，
    且首值处理不同，算出的 KDJ 与通达信显示值有可见偏差。
    """
    values = series.to_numpy(dtype="float64")
    out = np.full(values.shape, np.nan)
    previous = np.nan
    for index, value in enumerate(values):
        if np.isnan(value):
            continue
        if np.isnan(previous):
            previous = value  # 通达信以首个有效值作为递推起点
        else:
            previous = (m * value + (n - m) * previous) / n
        out[index] = previous
    return pd.Series(out, index=series.index)


def ema(series: pd.Series, n: int) -> pd.Series:
    """EMA(X,N)：指数移动平均，等价于 SMA(X,N+1,2)。"""
    return series.ewm(span=n, adjust=False).mean()


def ref(series: pd.Series, n: int) -> pd.Series:
    """REF(X,N)：N 根之前的值。"""
    return series.shift(n)


def hhv(series: pd.Series, n: int) -> pd.Series:
    """HHV(X,N)：N 根内最高值。"""
    return series.rolling(n, min_periods=1).max()


def llv(series: pd.Series, n: int) -> pd.Series:
    """LLV(X,N)：N 根内最低值。"""
    return series.rolling(n, min_periods=1).min()


def count(condition: pd.Series, n: int) -> pd.Series:
    """COUNT(cond,N)：N 根内条件成立的次数。"""
    return condition.astype("float64").rolling(n, min_periods=1).sum()


def every(condition: pd.Series, n: int) -> pd.Series:
    """EVERY(cond,N)：N 根内条件全部成立。"""
    return condition.astype("float64").rolling(n, min_periods=n).min().astype("boolean").fillna(False)


def std_tdx(series: pd.Series, n: int) -> pd.Series:
    """STD(X,N)：通达信用的是样本标准差（ddof=1），布林带宽度依赖这一点。"""
    return series.rolling(n, min_periods=n).std(ddof=1)


def barslast(condition: pd.Series) -> pd.Series:
    """BARSLAST(cond)：距上一次条件成立过了几根；从未成立为 NaN。"""
    values = condition.fillna(False).to_numpy(dtype=bool)
    out = np.full(values.shape, np.nan)
    last = -1
    for index, flag in enumerate(values):
        if flag:
            last = index
        if last >= 0:
            out[index] = index - last
    return pd.Series(out, index=condition.index)


def barslastcount(condition: pd.Series) -> pd.Series:
    """BARSLASTCOUNT(cond)：条件连续成立到当前的根数，不成立时为 0。"""
    values = condition.fillna(False).to_numpy(dtype=bool)
    out = np.zeros(values.shape, dtype="float64")
    run = 0
    for index, flag in enumerate(values):
        run = run + 1 if flag else 0
        out[index] = run
    return pd.Series(out, index=condition.index)


def cross(left: pd.Series, right: pd.Series) -> pd.Series:
    """CROSS(A,B)：A 上穿 B。上一根 A<=B 且这一根 A>B。

    通达信的 CROSS 要求"之前在下方、现在在上方"，只判 `left > right` 会把
    一直在上方的每一根都算成金叉。NaN 视为未成立（避免序列开头误报）。
    """
    return ((ref(left, 1) <= ref(right, 1)) & (left > right)).fillna(False)


# ---------- 常用组合指标 ----------

def kdj(frame: pd.DataFrame, n: int = 9, k_period: int = 3, d_period: int = 3) -> pd.DataFrame:
    """标准 KDJ，波段之门多个公式的基础。

    RSV:=(CLOSE-LLV(LOW,9))/(HHV(HIGH,9)-LLV(LOW,9))*100;
    K:=SMA(RSV,3,1);  D:=SMA(K,3,1);  J:=3*K-2*D;
    """
    low_n = llv(frame["low"], n)
    high_n = hhv(frame["high"], n)
    span = (high_n - low_n).replace(0, np.nan)
    rsv = (frame["close"] - low_n) / span * 100
    k = sma_tdx(rsv, k_period, 1)
    d = sma_tdx(k, d_period, 1)
    return pd.DataFrame({"rsv": rsv, "k": k, "d": d, "j": 3 * k - 2 * d}, index=frame.index)


def macd(frame: pd.DataFrame, short: int = 12, long: int = 26, mid: int = 9) -> pd.DataFrame:
    """MACD：DIF=EMA12-EMA26, DEA=EMA(DIF,9), MACD=(DIF-DEA)*2。"""
    dif = ema(frame["close"], short) - ema(frame["close"], long)
    dea = ema(dif, mid)
    return pd.DataFrame({"dif": dif, "dea": dea, "macd": (dif - dea) * 2}, index=frame.index)


def boll(frame: pd.DataFrame, n: int = 20, width: float = 2.0) -> pd.DataFrame:
    """布林带：中轨 MA20，上下轨 ±2 倍标准差。"""
    mid = ma(frame["close"], n)
    deviation = std_tdx(frame["close"], n)
    return pd.DataFrame({"mid": mid, "upper": mid + width * deviation, "lower": mid - width * deviation}, index=frame.index)


# ---------- 波段之门的具体公式 ----------

def strength_lines(frame: pd.DataFrame, index_close: pd.Series) -> pd.DataFrame:
    """强度指标（他自称"选股的必备工具"，20 个文件里出现 4 次，是最核心的一个）。

    原文（《关于平安交易的一点解析》《我做短线的一些方法（上）》《11只票的选股思路》
    《如何在震荡市选股附文的说明》四篇一致）：

        A:=(C/INDEXC)*10000;
        M13:MA(A,13);
        M34:MA(A,34),COLORLIBLUE,LINETHICK2;
        M55:MA(A,55),COLORRED,LINETHICK2;
        M89:MA(A,89),COLORYELLOW,DOTLINE;

    含义是个股对大盘的相对强度（比价）再取多周期均线。他在图上标 1/2/3/4 四个数字，
    分别是 M13 上穿 M34、M34 上穿 M55 的时点和 M55/M89 的位置——即"强度的多头排列"。

    index_close 必须是与 frame 同期对齐的大盘收盘价（通达信的 INDEXC 自动对齐，
    这里要调用方保证），否则算出来的比价没有意义。
    """
    aligned = index_close.reindex(frame.index)
    if aligned.isna().all():
        raise ValueError("index_close 与 frame 的索引完全不重叠，无法计算相对强度")
    strength = (frame["close"] / aligned) * 10000
    result = pd.DataFrame({"strength": strength}, index=frame.index)
    for period in (13, 34, 55, 89):
        result[f"m{period}"] = ma(strength, period)
    # 强度多头排列：他图上的 1/2 标记本质是这两个上穿
    result["cross_13_34"] = (result["m13"] > result["m34"]) & (ref(result["m13"], 1) <= ref(result["m34"], 1))
    result["cross_34_55"] = (result["m34"] > result["m55"]) & (ref(result["m34"], 1) <= ref(result["m55"], 1))
    result["bull_stack"] = (result["m13"] > result["m34"]) & (result["m34"] > result["m55"]) & (result["m55"] > result["m89"])
    return result


def ma_slope_color(frame: pd.DataFrame, n: int) -> pd.DataFrame:
    """均线三色（涨/平/跌），他用来看长周期均线的拐头。

    原文（《250321 做T指南》n=5；《熊市的确定性反弹与最终归宿》n=1597；
    《什么样的下跌可以做反弹，为什么8月不是大底》n=25）：

        A:MA(CLOSE,N);
        涨A:IF(A>REF(A,1),A,DRAWNULL),COLORRED;
        平A:IF(A=REF(A,1),A,DRAWNULL),COLOR00FFFF;
        跌A:IF(A<REF(A,1),A,DRAWNULL),COLORGREEN;

    1597 是斐波那契数，他用它当"熊市最终归宿"的参照线（约 6.5 年均线）。
    """
    line = ma(frame["close"], n)
    previous = ref(line, 1)
    return pd.DataFrame(
        {
            "line": line,
            "rising": line > previous,
            "flat": line == previous,
            "falling": line < previous,
        },
        index=frame.index,
    )


def do_t_ma5_trend(frame: pd.DataFrame) -> pd.DataFrame:
    """做T指南用的 5 日线方向。原文 A:MA(CLOSE,5) 加三色，见 ma_slope_color。"""
    return ma_slope_color(frame, 5)


def position_line(frame: pd.DataFrame, n: int = 6) -> pd.DataFrame:
    """「位置」指标 —— 他判断中国平安底部用的那个（本质是 RSI 的变体）。

    原文《为什么那里是中国平安的底部》：

        A:=REF(CLOSE,1);
        位置:SMA(MAX(C-A,0),6,1)/SMA(ABS(CLOSE-A),6,1)*100;
        牛市周线:80;  逃顶线:80;  抄底:16;  91;  97.8;

    他给了 5 条水平参照线：16 抄底、80 牛市/逃顶、91 和 97.8 两条极值线。
    公式形态就是 6 周期 RSI（SMA 递推版），但他用在周线上，且阈值是自己定的。
    """
    previous = ref(frame["close"], 1)
    gain = (frame["close"] - previous).clip(lower=0)
    change = (frame["close"] - previous).abs()
    line = sma_tdx(gain, n, 1) / sma_tdx(change, n, 1).replace(0, np.nan) * 100
    return pd.DataFrame(
        {
            "position": line,
            "buy_zone": line < 16,
            "bull_or_top": line > 80,
            "extreme_91": line > 91,
            "extreme_978": line > 97.8,
        },
        index=frame.index,
    )


def kd_dull(frame: pd.DataFrame) -> pd.DataFrame:
    """KD 钝化 —— 他算 3219 点目标位用的条件（《3219点的目标位是怎么来的》
    与《KD钝化指标》两文公式相同）。

        RSV:=(CLOSE-LLV(LOW,9))/(HHV(HIGH,9)-LLV(LOW,9))*100;
        K:=SMA(RSV,3,1); D:=SMA(K,3,1);
        COUNT(K>80,5)=5 AND C>MA(C,55);

    含义：K 连续 5 根都在 80 上方（钝化）且股价站在 55 日线上——强势钝化，
    他视为趋势健康而非超买。
    """
    values = kdj(frame)
    signal = (count(values["k"] > 80, 5) == 5) & (frame["close"] > ma(frame["close"], 55))
    return pd.DataFrame({"k": values["k"], "d": values["d"], "dull_signal": signal.fillna(False)}, index=frame.index)


def kdj_battle(frame: pd.DataFrame) -> pd.DataFrame:
    """KDJ 战法的全套信号（《kdj战法-指标》，他标了「回调/危险/七分危险/九分危险」等）。

        日K:K-D>15.5;                                   → 回调
        M1:J>110 AND D>75;
        M3:BARSLASTCOUNT(J-K>0 AND J-D>0)>12;           → 七分危险
        AL:J-K>28.3 AND D>40;                           → 过应(原文如此)
        MX1/MX2:BARSLASTCOUNT(J>K AND K>D AND J>D)<6 AND K>78
                AND C<MA(C,233) AND ...>1 AND C与MA55/MA89的关系  → 九分危险
        A1:=K/D; AY:A1>1.37 AND C>REF(C,1);             → 跌1
        M4:BARSLASTCOUNT(D>80)>6 AND J>90;              → 危险

    MX1 与 MX2 只差股价在 55/89 日线上方还是下方，标签同为「九分危险」，
    这里保留两个字段以还原原公式，不合并。
    """
    values = kdj(frame)
    k, d, j = values["k"], values["d"], values["j"]
    close = frame["close"]
    stack = (j > k) & (k > d) & (j > d)
    stack_run = barslastcount(stack)
    ma55, ma89, ma233 = ma(close, 55), ma(close, 89), ma(close, 233)
    base = (stack_run < 6) & (k > 78) & (close < ma233) & (stack_run > 1)
    return pd.DataFrame(
        {
            "k": k, "d": d, "j": j,
            "pullback": (k - d > 15.5).fillna(False),                      # 日K → 回调
            "m1": ((j > 110) & (d > 75)).fillna(False),
            "seven_danger": (barslastcount((j - k > 0) & (j - d > 0)) > 12),  # 七分危险
            "overreact": ((j - k > 28.3) & (d > 40)).fillna(False),           # 过应
            "nine_danger_above": (base & (close > ma55) & (close > ma89)).fillna(False),
            "nine_danger_below": (base & (close < ma55) & (close < ma89)).fillna(False),
            "fall_1": ((k / d.replace(0, np.nan) > 1.37) & (close > ref(close, 1))).fillna(False),
            "danger": ((barslastcount(d > 80) > 6) & (j > 90)).fillna(False),
        },
        index=frame.index,
    )


def select_doubling(frame: pd.DataFrame) -> pd.Series:
    """翻倍选股（《翻倍选股方法》）。

        T1:=REF(C,2)/REF(C,3)>1.099 AND C>O;
        T2:=REF(V,1)>2*REF(V,2) AND REF(C,1)<REF(O,1);
        T3:=C>REF(C,1) AND L=O;
        T:T1 AND T2 AND T3;

    形态：前天涨停(涨幅>9.9%)、昨天放量收阴、今天收阳且最低价等于开盘价
    （开盘即最低，全天没跌破开盘）。他强调买点在收盘前 5 分钟，因为下影线可能后出现。
    """
    close, open_, low, volume = frame["close"], frame["open"], frame["low"], frame["volume"]
    t1 = (ref(close, 2) / ref(close, 3) > 1.099) & (close > open_)
    t2 = (ref(volume, 1) > 2 * ref(volume, 2)) & (ref(close, 1) < ref(open_, 1))
    t3 = (close > ref(close, 1)) & (low == open_)
    return (t1 & t2 & t3).fillna(False)


def select_shock_market(frame: pd.DataFrame, index_close: pd.Series) -> pd.Series:
    """震荡市选股（《如何在震荡市选股附文的说明》第 2 段公式）。

        A:=(C/INDEXC)*10000; M13:=MA(A,13); M21:=MA(A,21);
        M1:=MA(C,34); M2:=MA(C,89);
        XG:M13>M21 AND M21>M1 AND M13/M1>1.15 AND C>MA(C,21)
           AND C>MA(C,55) AND HHV(H,104)/LLV(L,104)>2;

    注意原公式里 M21 与 M1 量纲不同（M21 是强度均线，M1 是价格均线），
    `M21>M1` 与 `M13/M1>1.15` 是拿比价均线和价格均线直接比较。这在通达信里
    语法合法但含义可疑，按原样实现并在此标注，不替他改。
    """
    aligned = index_close.reindex(frame.index)
    strength = (frame["close"] / aligned) * 10000
    m13, m21 = ma(strength, 13), ma(strength, 21)
    m1 = ma(frame["close"], 34)
    condition = (
        (m13 > m21)
        & (m21 > m1)
        & (m13 / m1.replace(0, np.nan) > 1.15)
        & (frame["close"] > ma(frame["close"], 21))
        & (frame["close"] > ma(frame["close"], 55))
        & (hhv(frame["high"], 104) / llv(frame["low"], 104).replace(0, np.nan) > 2)
    )
    return condition.fillna(False)


def select_11_stocks(frame: pd.DataFrame, index_close: pd.Series, code: str = "") -> pd.Series:
    """11只票的选股思路（《11只票的选股思路》第 2 段）。

        A:=(C/INDEXC)*10000; M55:=MA(A,55); M89:=MA(A,89);
        A1:=REF(HHV(H,189),1)/REF(LLV(L,189),1)<1.44
            and HHV(H,699)/LLV(L,699)>2.05;
        XG:A1 AND H>REF(HHV(H,144),1) AND C>MA(C,13)
           AND IF(CODELIKE('83'),0,1) AND M55>M89;

    形态：近 189 根振幅被压缩到 1.44 倍以内（长期横盘），但 699 根内曾有 2.05 倍
    以上波动（有过大行情），今天创 144 日新高。CODELIKE('83') 是排除北交所老代码段。
    """
    aligned = index_close.reindex(frame.index)
    strength = (frame["close"] / aligned) * 10000
    high, low, close = frame["high"], frame["low"], frame["close"]
    squeezed = ref(hhv(high, 189), 1) / ref(llv(low, 189), 1).replace(0, np.nan) < 1.44
    had_range = hhv(high, 699) / llv(low, 699).replace(0, np.nan) > 2.05
    excluded = code.startswith("83")
    condition = (
        squeezed
        & had_range
        & (high > ref(hhv(high, 144), 1))
        & (close > ma(close, 13))
        & (ma(strength, 55) > ma(strength, 89))
    )
    if excluded:
        return pd.Series(False, index=frame.index)
    return condition.fillna(False)


def select_ma_climb(frame: pd.DataFrame, code: str = "") -> pd.Series:
    """沿着均线爬升加强版（《板块炒作规律与策略》《这波行情的规律》两文相同）。

        A:=MA(C,5)>REF(MA(C,5),1);
        XG:COUNT(A,14)>=12 AND C/LLV(L,14)>1.18
           AND IF(CODELIKE('8'),0,1) AND IF(CODELIKE('4'),0,1);

    形态：近 14 根里 5 日线有 12 根以上在上行（稳步爬升），且已从 14 日低点涨超 18%。
    排除 8/4 开头（北交所、新三板）。
    """
    ma5 = ma(frame["close"], 5)
    rising = ma5 > ref(ma5, 1)
    if code.startswith(("8", "4")):
        return pd.Series(False, index=frame.index)
    condition = (count(rising, 14) >= 12) & (frame["close"] / llv(frame["low"], 14).replace(0, np.nan) > 1.18)
    return condition.fillna(False)


def select_sector_strong(
    frame: pd.DataFrame,
    ref_date_close: float | None = None,
    *,
    allow_missing_ref: bool = False,
) -> pd.Series:
    """板块内强势股（《如何挑选某一板块里的强势股（医药为例）》）。

        M55:=MA(C,55);
        A:=REFDATE(C,1241008);
        XG:=HHV(H,77);
        C>M55 AND C>A AND MA(C,13)>MA(C,55) AND C>XG*0.8;

    REFDATE(C,1241008) 取的是 2024-10-08 那天的收盘价（通达信的 REFDATE 用
    YYMMDD 或带前缀的日期数；1241008 即 2024-10-08）——那是他反复提到的一个
    关键高点日。

    ⚠️ 缺基准价不再静默降级（2026-08-24 修，《待处理》P1-3）：ref_date_close
    不传时本函数直接抛 ValueError——没有那个条件就不是他的公式，实测信号会
    多约 4 倍。此前退化只写进返回值的 attrs，而 attrs 在 Series 被 bool() 或
    进 DataFrame 后就丢了，等于无声换公式。确要跑无基准版必须显式传
    allow_missing_ref=True，attrs 里仍会标注。
    """
    if ref_date_close is None and not allow_missing_ref:
        raise ValueError(
            "select_sector_strong 需要基准日收盘价：原公式硬编码 "
            "REFDATE(C,1241008)=2024-10-08 收盘价，请传 ref_date_close=<该日收盘>。"
            "若明确要跑无基准的弱化版（不是他的公式，信号约多 4 倍），"
            "传 allow_missing_ref=True。"
        )
    close, high = frame["close"], frame["high"]
    ma55, ma13 = ma(close, 55), ma(close, 13)
    condition = (close > ma55) & (ma13 > ma55) & (close > hhv(high, 77) * 0.8)
    applied = ref_date_close is not None
    if applied:
        condition = condition & (close > ref_date_close)
    result = condition.fillna(False)
    result.attrs["ref_date_applied"] = applied
    result.attrs["ref_date_note"] = "原公式硬编码 REFDATE(C,1241008)=2024-10-08 收盘价"
    return result


def boll_top_risk(frame: pd.DataFrame) -> pd.Series:
    """布林带见顶风险（《这种情况，上证出现了24次，结果无二例外》）。

        BOLL:=MA(CLOSE,20); UB:=BOLL+2*STD(CLOSE,20); LB:=BOLL-2*STD(CLOSE,20);
        A:H/UB>0.998 AND C<MA(C,89) AND BOLL<REF(BOLL,1);

    形态：摸到布林上轨、但收盘在 89 日线下方、且中轨在下行——反弹到上轨却仍在
    下降趋势里，他统计上证出现 24 次。
    """
    bands = boll(frame)
    condition = (
        (frame["high"] / bands["upper"].replace(0, np.nan) > 0.998)
        & (frame["close"] < ma(frame["close"], 89))
        & (bands["mid"] < ref(bands["mid"], 1))
    )
    return condition.fillna(False)


def volume_price_rule(frame: pd.DataFrame) -> pd.DataFrame:
    """大盘量价关系（《大盘的部分量价关系规律》）。

        T1:EVERY(AMO>REF(AMO,1),3) AND EVERY(C>REF(C,1),4);   → 标'跌'
        T2:EVERY(AMO<REF(AMO,1),3) AND EVERY(C<REF(C,1),4);   → 标'底'

    连续 3 天放量 + 连续 4 天上涨 = 见顶（他标"跌"）；
    连续 3 天缩量 + 连续 4 天下跌 = 见底（他标"底"）。
    AMO 是成交额；若数据只有成交量，用 volume 近似并在 attrs 标注。
    """
    amount = frame["amount"] if "amount" in frame else frame["volume"]
    close = frame["close"]
    top = every(amount > ref(amount, 1), 3) & every(close > ref(close, 1), 4)
    bottom = every(amount < ref(amount, 1), 3) & every(close < ref(close, 1), 4)
    result = pd.DataFrame({"top_warning": top, "bottom_signal": bottom}, index=frame.index)
    result.attrs["amount_proxy"] = "amount" not in frame
    return result


def macd_zero_cross(frame: pd.DataFrame) -> pd.Series:
    """DIF-DEA 上穿零轴（《从另一个指标，看大盘6月到底有没有风险》）。

        DIF:=EMA(CLOSE,12)-EMA(CLOSE,26); DEA:=EMA(DIF,9);
        A:=DIF-DEA;  B:REF(A,1)<=0 AND A>0;

    即 MACD 柱由负转正的那一根（金叉当日）。
    """
    values = macd(frame)
    difference = values["dif"] - values["dea"]
    return ((ref(difference, 1) <= 0) & (difference > 0)).fillna(False)


def sell_next_day(frame: pd.DataFrame) -> pd.Series:
    """年后第一天卖出的形态（《年后第一天为什么卖出》）。

        A:C<REF(C,1) AND H>MA(C,5) AND L<MA(C,5) AND C>MA(C,13);

    形态：收阴、最高价在 5 日线上方而最低价在下方（5 日线被穿刺）、但仍站 13 日线上。
    他视为短线转弱的第一信号。
    """
    close, high, low = frame["close"], frame["high"], frame["low"]
    ma5, ma13 = ma(close, 5), ma(close, 13)
    condition = (close < ref(close, 1)) & (high > ma5) & (low < ma5) & (close > ma13)
    return condition.fillna(False)


def eleven_sequence(frame: pd.DataFrame, start_date: str) -> pd.Series:
    """起始于某日期的 11 序列（《分析工具1--起始于某个日期的11序列》）。

        AA1:=YEAR=年 AND MONTH=月 AND DAY=日;
        AA2:=BARSLAST(AA1);
        AA3:=MOD(AA2,11)+1;
        DRAWTEXT(AA2>=0,L,AA3);

    从指定日期起，每根K线标 1..11 循环。他用 11 这个数当变盘节奏的计数器，
    数到 11 附近关注转折。start_date 格式 'YYYY-MM-DD'。
    """
    marks = pd.Series(frame.index.astype(str) == start_date, index=frame.index)
    if not marks.any():
        raise ValueError(f"起始日期 {start_date} 不在数据范围内，无法计数")
    bars = barslast(marks)
    return (bars % 11 + 1).where(bars.notna())


# 公式名 → (函数, 出处文章, 是否需要额外参数)
REGISTRY = {
    "强度指标": (strength_lines, "关于平安交易的一点解析 / 我做短线的一些方法（上） / 11只票的选股思路", "需 index_close"),
    "均线三色": (ma_slope_color, "250321做T指南(5) / 熊市的确定性反弹(1597) / 什么样的下跌可以做反弹(25)", "需 n"),
    "位置指标": (position_line, "为什么那里是中国平安的底部", ""),
    "KD钝化": (kd_dull, "3219点的目标位是怎么来的 / KD钝化指标", ""),
    "KDJ战法": (kdj_battle, "kdj战法-指标", ""),
    "翻倍选股": (select_doubling, "翻倍选股方法", ""),
    "震荡市选股": (select_shock_market, "如何在震荡市选股附文的说明", "需 index_close"),
    "11只票选股": (select_11_stocks, "11只票的选股思路", "需 index_close"),
    "沿均线爬升": (select_ma_climb, "板块炒作规律与策略 / 这波行情的规律", ""),
    "板块强势股": (select_sector_strong, "如何挑选某一板块里的强势股（医药为例）", "含硬编码日期"),
    "布林见顶": (boll_top_risk, "这种情况，上证出现了24次", ""),
    "量价规律": (volume_price_rule, "大盘的部分量价关系规律", ""),
    "MACD零轴": (macd_zero_cross, "从另一个指标，看大盘6月到底有没有风险", ""),
    "卖出形态": (sell_next_day, "年后第一天为什么卖出", ""),
    "11序列": (eleven_sequence, "分析工具1--起始于某个日期的11序列", "需 start_date"),
}
