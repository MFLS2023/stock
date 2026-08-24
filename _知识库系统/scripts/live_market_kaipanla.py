#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
开盘啦数据源封装模块

封装 kaipanla_crawler 的核心接口，统一错误处理和数据格式。
提供与现有 live_market 模块相同的调用风格。

依赖：
    pip install requests pandas urllib3
    需要 kaipanla_crawler.py 在同目录

数据覆盖：
    - 市场情绪：实时温度、涨跌分析、市场指数、情绪历史序列（分位数底座）
    - 涨停板块：连板梯队、涨停天梯、炸板历史
    - 板块分析：排行、强度、分时、成分股
    - 个股数据：分时、大单分时、集合竞价
    - 龙虎榜：列表、明细
    - 其他：百日新高、大幅回撤、ETF排行

代理：本机 HTTP(S)_PROXY 指向 127.0.0.1:7897，该代理常处于未启动状态。
      crawler 里 30 处裸 requests 调用只有 28 处带 proxies=None，漏掉的两处
      会跟着环境变量走死代理并抛 ProxyError。开盘啦/东财/同花顺都是国内域名，
      本来不该走代理，所以本模块在 import 时把这些域名加进 NO_PROXY。
      与 live_market.py（trust_env=False）、live_market_akshare.py（NO_PROXY=*）
      是同一件事的不同实现 —— 那两个能拿到自己的 Session，这里拿不到。

测试：27/27 MCP 工具接口通过（2026-08-05）
"""
import contextlib
import io
import os
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

# 必须在 import kaipanla_crawler 之前设好 —— requests 读环境变量的时机是
# 每次请求，但写在这里最不容易被后续代码覆盖
_KPL_HOSTS = (
    "apphis.longhuvip.com", "apphwhq.longhuvip.com",
    "applhb.longhuvip.com", "apparticle.longhuvip.com",
    "longhuvip.com",
    "push2.eastmoney.com", "quote.eastmoney.com", "eastmoney.com",
    "eq.10jqka.com.cn", "10jqka.com.cn",
)
for _var in ("NO_PROXY", "no_proxy"):
    _cur = os.environ.get(_var, "")
    _have = {h.strip() for h in _cur.split(",") if h.strip()}
    _add = [h for h in _KPL_HOSTS if h not in _have]
    if _add:
        os.environ[_var] = (_cur + "," if _cur else "") + ",".join(_add)

try:
    from kaipanla_crawler import KaipanlaCrawler
    _CRAWLER = KaipanlaCrawler()
except ImportError as e:
    _CRAWLER = None
    _IMPORT_ERROR = str(e)


def _ensure_crawler():
    """确保爬虫已加载"""
    if _CRAWLER is None:
        raise RuntimeError(f"kaipanla_crawler 未安装或加载失败: {_IMPORT_ERROR}")
    return _CRAWLER


# crawler 上一次调用往 stdout 打的内容，供 wrapper 解释「为什么是空」
_LAST_NOISE = ""


def _handle_error(fn, *args, **kwargs):
    """
    统一错误处理，确保返回 dict。

    **必须挡住 stdout**：kaipanla_crawler 里有 247 处 `print(`，写的是 stdout，
    而 MCP 走 stdio —— stdout 就是 JSON-RPC 传输通道本身，这些字节会直接
    污染协议流（同 live_market_akshare.py 挡 tqdm 那件事，只是那边是 stderr）。

    顺手把 print 出来的内容存进 _LAST_NOISE：crawler 把 API 错误码
    （1020 板块不存在 / 1130 无此数据）只 print 不 raise，那是解释
    「为什么返回空」的唯一线索，捞出来当诊断用。
    """
    global _LAST_NOISE
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            result = fn(*args, **kwargs)
        _LAST_NOISE = buf.getvalue().strip()
        # 字符串/基础类型包装成字典
        if isinstance(result, str):
            return {"数据": result}
        elif isinstance(result, (int, float, bool)):
            return {"值": result}
        return result
    except Exception as e:
        _LAST_NOISE = buf.getvalue().strip()
        return {"错误": str(e), "接口": fn.__name__}


def _noise_hint():
    """把 crawler print 的错误码翻译成人话；没线索返回 None"""
    n = _LAST_NOISE
    if not n:
        return None
    tail = n.splitlines()[-1][:160]
    if "1020" in n:
        return f"开盘啦返回错误码 1020（该板块/股票代码不存在或已下线）：{tail}"
    if "1130" in n:
        return f"开盘啦返回错误码 1130（该维度无数据）：{tail}"
    if "Proxy" in n or "代理" in n or "10061" in n:
        return f"网络/代理失败：{tail}"
    return f"crawler 输出：{tail}"


def _to_dict(data):
    """统一数据格式，DataFrame/Series 转字典"""
    if hasattr(data, 'to_dict'):
        # Series 转字典
        if hasattr(data, 'index'):
            return data.to_dict()
        # DataFrame 转记录列表
        return data.to_dict('records')
    return data


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _last_trade_day() -> str:
    """
    最近一个已收盘的交易日（YYYY-MM-DD）。用于盘后才发布的数据（龙虎榜）。

    与 live_market_ex.py 的同名函数同口径：15:00 前算「今天还没收盘」，
    往前找上一个工作日。**不含节假日日历**，遇长假可能给出休市日，
    此时接口返回空，调用方会看到「尚未发布」而不是假数据。
    """
    d = datetime.now()
    if d.hour < 15:
        d -= timedelta(days=1)
    while d.weekday() >= 5:             # 5=周六 6=周日
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _pctile(series, value):
    """
    value 在 series 里的分位数（0-100），用中位秩定义。
    series 里的 None 会被剔除；空序列或 value 为 None 时返回 None。
    """
    xs = [x for x in series if x is not None]
    if not xs or value is None:
        return None
    below = sum(1 for x in xs if x < value)
    equal = sum(1 for x in xs if x == value)
    return round((below + equal / 2.0) / len(xs) * 100, 1)


def _mean(series, k):
    """近 k 项均值（跳过 None）"""
    xs = [x for x in series[:k] if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


# ==================== 市场整体 ====================

def market_sentiment(date: str = "") -> dict:
    """
    市场情绪指标（涨停/跌停/涨跌家数）。

    参数：
        date: 日期 YYYY-MM-DD，空则今天

    口径说明：
        开盘啦有两套端点，当日只有实时端点有数据：
          历史端点 HisZhangFuDetail —— 当日无行，且**每个字段默认填 0**
          实时端点 MoodNumCount    —— 当日有效，但不含实际涨停(去一字板)

        本函数当日走实时端点，历史日期走历史端点。
        历史端点返回空行时**明确报缺数**，绝不返回 0 冒充真实值。
    """
    c = _ensure_crawler()
    d = date or _today()

    # 当日：走实时端点
    if d == _today():
        mood = _handle_error(c.get_realtime_market_mood)
        if isinstance(mood, dict) and "错误" in mood:
            return mood
        if not mood:
            return {"日期": d, "数据状态": "实时端点无返回", "数据源": "开盘啦MoodNumCount"}
        up, down = mood.get("上涨家数"), mood.get("下跌家数")
        return {
            "日期": d,
            "数据源": "开盘啦MoodNumCount(实时)",
            "涨停数": mood.get("涨停家数"),
            "跌停数": mood.get("跌停家数"),
            "上涨家数": up,
            "下跌家数": down,
            # 自己算，不用它的「涨跌比」字段：08-04 上涨3642/下跌1747=2.08，
            # 而该字段给 10.82，两者不是一个东西，含义未明
            "涨跌家数比": round(up / down, 2) if up and down else None,
            "原始涨跌比字段": mood.get("涨跌比"),
            "原始涨跌比说明": "开盘啦自带字段，实测 != 上涨/下跌，含义未明，勿用",
            "实际涨停": None,
            "实际跌停": None,
            "说明": "实时端点不提供实际涨停(去一字板)；需要请查历史日期或用 kpl_actual_limit_up_down",
        }

    # 历史日期：走历史端点
    data = _handle_error(c.get_market_sentiment, d)
    if isinstance(data, dict) and "错误" in data:
        return data
    if hasattr(data, "empty") and data.empty:
        return {"日期": d, "数据状态": "该日期历史库无数据（非交易日或尚未入库）",
                "数据源": "开盘啦HisZhangFuDetail"}
    if hasattr(data, 'to_dict'):
        rows = data.to_dict('records')
        if not rows:
            return {"日期": d, "数据状态": "该日期历史库无数据",
                    "数据源": "开盘啦HisZhangFuDetail"}
        r = dict(rows[0])
        up, down = r.get("上涨家数"), r.get("下跌家数")
        # 历史端点全 0 说明这一行是默认值填出来的，不是真实盘面
        if not any((r.get("涨停数"), r.get("跌停数"), up, down)):
            return {"日期": d, "数据状态": "历史库返回全 0（该日无有效行，字段是默认值）",
                    "数据源": "开盘啦HisZhangFuDetail"}
        r["数据源"] = "开盘啦HisZhangFuDetail(历史)"
        r["涨跌家数比"] = round(up / down, 2) if up and down else None
        return r
    return data


def realtime_market_mood() -> dict:
    """
    实时市场情绪。

    返回：
        当前市场情绪温度、赚钱效应等实时指标
    """
    c = _ensure_crawler()
    return _handle_error(c.get_realtime_market_mood)


def rise_fall_analysis() -> dict:
    """
    涨跌分析（原始返回，字段标签不可信，见 sentiment_history）。

    ⚠️ crawler 给这个接口的字段名是错的：
       它把 raw_data[3] 当炸板数、raw_data[5] 当「昨日涨停今表现」，
       实测 08-04 得到 炸板数=0 而炸板率=9.80%，自相矛盾。
       正确字段序见 sentiment_history() 的文档字符串。

    要拿情绪历史请用 sentiment_history()，不要用这个函数的 blown_limit_up_count。
    """
    c = _ensure_crawler()
    return _handle_error(c.get_realtime_rise_fall_analysis)


# ==================== 情绪历史（分位数判断的底座）====================

# raw_data 每行的真实字段序（2026-08-04 反推并三重验证，crawler 注释是错的）
#
#   [0] 开盘啦口径「涨停数」—— 口径未完全对齐，**不要当权威涨停数用**。
#       多数日子等于其历史库 ZT（08-03=83、07-31=101、07-30=56、07-27=121 都对上），
#       但 06-30 历史库 ZT=168 而 [0]=138，07-03 ZT=159 而 [0]=104，对不上。
#       要权威涨停数用「收盘封板」（下面反算出来的那个，6/6 天等于天梯求和，
#       且 08-04 = 138 与东财涨停池逐位相同）。
#   [1] 跌停数
#   [2] 含义未定 —— 不要当首板数用。试过 [0]-[2] 与同花顺连板总数比对：
#       08-04 得 7 而同花顺 19；08-03 得 12 而同花顺 17。也试过
#       「一板+二板」假设，5 天里只对上 3 天（07-31 差 1、07-03 差 3）。不使用。
#   [3] 含义未定（07-21 出现 215，与任何已知口径都不匹配，不使用）
#   [4] 炸板率% = [5] / 曾涨停数
#   [5] 炸板数
#   [6] 日期 YYYY-MM-DD
#
# 连板数与市场高度**不要从这里推**，用同花顺 ladder（可回溯任意历史日期）。
#
# 三条独立证据：
#   1. [5] 逐位命中权威源：08-04 = 15（同花顺炸板 15、东财炸板 15），08-03 = 16（同花顺 16）
#   2. 由 [5]/[4] 反算曾涨停，250 个交易日里 244 天得到整数（误差<0.006，2.4% 例外）
#      08-03 反算 曾涨停 91 / 收盘封板 75 / 成功率 82.42%，与同花顺官方逐字相同
#   3. [4] 与另一端点 ZhangTingExpression 的「今日涨停破板率」逐位相同
#      （08-03 17.58 / 07-31 51.94 / 07-30 26.76 / 07-03 33.33 / 06-30 14.81）
#      且该端点 一板+二板+三板+高度板 之和 == 反算的收盘封板数（6/6 天精确相等）
_RD = {"涨停": 0, "跌停": 1, "炸板率": 4, "炸板": 5, "日期": 6}


def sentiment_history(n: int = 60, anchor: str = "") -> dict:
    """
    市场情绪历史序列 —— 一次调用拿到约 250 个交易日的涨停/跌停/炸板率，
    并反算出曾涨停、收盘封板、封板成功率。

    **这是分位数判断的底座**：南京路彼岸体系要求用相对阈值
    （与近5日/10日均值对比、看在近10日的分位数位置），而不是绝对值。

    参数：
        n:      返回最近 n 个交易日的明细
        anchor: 锚定日期 YYYY-MM-DD。均值与分位数从这一天往前算，
                「基准日」也取这一天。空则取序列最新一行。

        ⚠️ 盘中调用时序列最新一行是**上一交易日收盘**，不是今天 ——
           开盘啦这个端点当日盘中不入库。所以做当日复盘时要么盘后再调，
           要么明确知道自己拿的是上一交易日的值。anchor 传今天而序列里
           没有今天时，返回「锚定状态」说明这件事，不会静默错位。

    返回：
        基准日 / 近5日均值 / 近10日均值 / 近10日分位数 / 近60日分位数 / 明细序列

    反算公式（已在 250 天上验证）：
        曾涨停 = 炸板数 / 炸板率 * 100
        收盘封板 = 曾涨停 - 炸板数
        封板成功率% = 100 - 炸板率
    """
    c = _ensure_crawler()
    raw = _handle_error(c.get_realtime_rise_fall_analysis)
    if isinstance(raw, dict) and "错误" in raw:
        return raw
    rows = (raw or {}).get("raw_data") or []
    if not rows:
        return {"数据状态": "开盘啦 RiseFallAnalysis 未返回 raw_data",
                "数据源": "开盘啦RiseFallAnalysis"}

    series = []
    for r in rows:
        if len(r) < 7:
            continue
        zt = r[_RD["涨停"]]
        zb = r[_RD["炸板"]]
        rate = r[_RD["炸板率"]]
        dt = r[_RD["跌停"]]
        # 反算曾涨停：炸板率为 0 时无法反算，明确置 None 而不是猜
        ceng = round(zb / rate * 100) if rate else None
        fengban = (ceng - zb) if ceng is not None else None
        series.append({
            "日期": str(r[_RD["日期"]]),
            "涨停数": zt,
            "跌停数": dt,
            "涨停跌停比": round(zt / dt, 2) if (zt and dt) else None,
            "曾涨停": ceng,
            "收盘封板": fengban,
            "炸板数": zb,
            "炸板率%": round(rate, 2) if rate is not None else None,
            "封板成功率%": round(100 - rate, 2) if rate else None,
        })

    # 锚定：默认序列最新一行；传了 anchor 就从那天往前切
    #（不这么做的话，盘中调用会把上一交易日的值当成今天，静默错位）
    idx, anchor_note = 0, None
    if anchor:
        hit = next((i for i, x in enumerate(series) if x["日期"] == anchor), None)
        if hit is None:
            anchor_note = (f"序列里没有 {anchor} —— 开盘啦该端点当日盘中不入库，"
                           f"最新一行是 {series[0]['日期']}。以下均值与分位数基于 "
                           f"{series[0]['日期']}，不是 {anchor}。")
        else:
            idx = hit
    win = series[idx:]

    def col(key):
        return [x[key] for x in win]

    base = win[0] if win else {}
    out = {
        "取数时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "数据源": "开盘啦RiseFallAnalysis(单次调用含全部历史)",
        "样本交易日数": len(series),
        "基准日": base.get("日期"),
        "基准日数据": base,
    }
    if anchor_note:
        out["锚定状态"] = anchor_note

    # 「收盘封板」必须在列 —— 它是权威涨停数（08-04 反算 138 与东财涨停池逐位相同），
    # 调用方拿它给指标 1 配均值与分位数。原始 [0]「涨停数」口径对不齐，只作参考列。
    metrics = ("涨停数", "收盘封板", "跌停数", "曾涨停", "炸板数", "炸板率%", "封板成功率%")

    # 均值要跟「基准日之前」的历史比，所以正常剔掉基准日自身。
    # 但 anchor 落在序列之外时（盘中查今天，序列最新只到上一交易日），
    # 基准日相对查询日已经是历史，必须计入 —— 否则近5日均值会整体错位一天。
    off = 0 if anchor_note else 1
    out["近5日均值"] = {k: _mean(col(k)[off:], 5) for k in metrics}
    out["近10日均值"] = {k: _mean(col(k)[off:], 10) for k in metrics}

    # 分位数：基准日自身在窗口内，给出「基准日排第几」
    out["近10日分位数"] = {k: _pctile(col(k)[:10], base.get(k)) for k in metrics}
    out["近60日分位数"] = {k: _pctile(col(k)[:60], base.get(k)) for k in metrics}
    out["分位数归属"] = f"以上分位数是 {base.get('日期')} 这一天的排名"

    # 供调用方给「别的来源拿到的当日值」现算分位数（比如盘中用同花顺的实时值）。
    # 不这么做的话，调用方会把基准日的分位数套到今天的值上，那是错的。
    out["序列窗口"] = {"近10日": {k: col(k)[:10] for k in metrics},
                      "近60日": {k: col(k)[:60] for k in metrics}}
    out["窗口口径"] = ("均值窗口" + ("含" if off == 0 else "不含") + "基准日自身"
                      + "；分位数窗口含基准日自身；序列窗口按日期从近到远")
    out["分位数判读"] = ">80分位 = 情绪高点区域；<20分位 = 情绪低点区域（南京路彼岸口径）"
    out["明细"] = win[:max(1, n)]
    out["字段说明"] = ("曾涨停/收盘封板/封板成功率 由 炸板数 与 炸板率 反算，"
                       "公式已在 250 个交易日上验证（244 天整除，08-03 与同花顺官方逐字相同）")
    out["不含"] = "连板数与市场高度不在此序列里（原始字段不可信），请用 get_ladder（同花顺）"
    return out


def market_index() -> dict:
    """
    市场指数。

    ⚠️ 实测该端点返回空 DataFrame（2026-08-05 盘中，crawler 无任何错误输出）。
       指数行情请用 get_market_temperature 或 kpl_market_index 的替代源。
    """
    c = _ensure_crawler()
    data = _handle_error(c.get_market_index)
    if isinstance(data, dict):
        return data
    lst = _to_dict(data)
    # 空 DataFrame 直接标缺数 —— 原先返回 {"指数列表": []} 会被当成「有数据」
    if not lst:
        r = {"日期": _today(),
             "数据状态": "开盘啦未返回指数行情（实测返回空 DataFrame）",
             "数据源": "开盘啦市场指数",
             "建议": "指数点位用 get_market_temperature（东财，含五大指数）"}
        h = _noise_hint()
        if h:
            r["诊断"] = h
        return r
    return {"指数列表": lst}


def index_list() -> dict:
    """
    实时指数列表。

    返回：
        所有可查询的指数代码和名称
    """
    c = _ensure_crawler()
    return _handle_error(c.get_realtime_index_list)


def index_intraday(index_code: str) -> dict:
    """
    指数分时数据（当日每分钟价格/成交量）。

    参数：
        index_code: 指数代码，如 SH000001(上证) / SZ399001(深证) / SZ399006(创业板)

    ⚠️ **该端点区分大小写**：SH000001 返回 121 个分时点，sh000001 报 errcode 1017。
       本函数自动把前缀转成大写，两种写法都能用。

    ⚠️ 别传裸代码：`000001` 不报错，但返回的是平安银行（开盘 11.41），不是上证指数。
       没有 SH/SZ 前缀时本函数拒绝调用，不让个股数据冒充指数。
    """
    c = _ensure_crawler()
    code = (index_code or "").strip()
    up = code.upper()
    if not up.startswith(("SH", "SZ", "BJ")):
        return {"指数代码": index_code,
                "数据状态": "代码缺少市场前缀 —— 该端点会把裸代码当个股返回",
                "数据源": "开盘啦指数分时",
                "建议": "上证传 SH000001，深证 SZ399001，创业板 SZ399006"}
    data = _handle_error(c.get_index_intraday, up)
    if isinstance(data, dict) and "错误" in data:
        return data
    # crawler 在错误码上返回空 dict 不抛异常 —— 明确报缺数
    if not data:
        r = {"指数代码": up, "日期": _today(),
             "数据状态": "开盘啦未返回指数分时",
             "数据源": "开盘啦指数分时",
             "建议": "指数分时可改用 kpl_index_intraday 的其他指数代码，或 get_intraday（东财）"}
        h = _noise_hint()
        if h:
            r["诊断"] = h
        return r
    if isinstance(data, dict):
        if "data" in data and hasattr(data["data"], 'to_dict'):
            data["data"] = data["data"].to_dict('records')
    return data


# ==================== 涨停板块 ====================

def consecutive_limit_up(date: str = "") -> dict:
    """
    连板梯队。

    ⚠️ 这个端点的连板数不可信，两处实测矛盾：
       - 当日（08-04）返回 max_consecutive=0，而真实最高板是 7（传智教育）
       - 历史日期 07-31 与 07-30 都返回 5，而同花顺是 9 板与 8 板（爱丽家居连续）
         同一端点的 get_market_limit_up_ladder 同两天却返回 10 板，自相矛盾

    **市场高度请用 get_ladder（同花顺）**，那个源可回溯任意历史日期且与个股名对得上。
    本函数返回 0 时明确报缺数。
    """
    c = _ensure_crawler()
    d = date or _today()
    data = _handle_error(c.get_consecutive_limit_up, d) if date else \
        _handle_error(c.get_consecutive_limit_up)
    if isinstance(data, dict) and "错误" in data:
        return data
    if isinstance(data, dict):
        if not data.get("max_consecutive"):
            return {"日期": data.get("date", d),
                    "数据状态": "端点返回 max_consecutive=0（不可用，不是真的没有连板股）",
                    "数据源": "开盘啦连板梯队",
                    "建议": "市场高度请用 get_ladder（同花顺），可回溯任意历史日期"}
        data["数据源"] = "开盘啦连板梯队"
        data["口径警告"] = "该端点连板数与同花顺不一致（07-31 它给 5，同花顺给 9），仅供参考"
    return data


def limit_up_ladder(date: str = "") -> dict:
    """
    涨停天梯（一板/二板/三板/高度板 + 连板率 + 破板率 + 市场评价）。

    参数：
        date: 日期 YYYY-MM-DD，空则今天

    ⚠️ 当日盘中该端点不入库（返回空），要盘后或查历史日期。

    这个端点的「今日涨停破板率」经验证与 sentiment_history 的炸板率逐位相同
    （08-03 17.58 / 07-31 51.94 / 07-30 26.76 / 07-03 33.33 / 06-30 14.81），
    且 一板+二板+三板+高度板 之和 == 反算的收盘封板数（6/6 天精确相等）。

    注意「高度板」是该端点自己的口径，与同花顺最高板不是一回事
    （06-30 它给 0，07-03 给 1）。市场高度请用 get_ladder（同花顺）。
    """
    c = _ensure_crawler()
    d = date or _today()
    data = _handle_error(c.get_limit_up_ladder, d)
    if isinstance(data, dict) and "错误" in data:
        return data
    # crawler 返回 DataFrame，原先直接漏出去，MCP 层无法序列化
    if hasattr(data, "empty"):
        if data.empty:
            return {"日期": d,
                    "数据状态": "该日期无天梯数据（当日盘中不入库，或非交易日）",
                    "数据源": "开盘啦ZhangTingExpression"}
        rows = data.to_dict("records")
        r = dict(rows[0])
        r["数据源"] = "开盘啦ZhangTingExpression"
        r["口径说明"] = "「高度板」是该端点自有口径，与同花顺最高板不同；市场高度用 get_ladder"
        return r
    if isinstance(data, dict) and "ladder" in data:
        data["ladder"] = _to_dict(data["ladder"])
    if not data:
        return {"日期": d, "数据状态": "该日期无天梯数据",
                "数据源": "开盘啦ZhangTingExpression"}
    return data


def market_limit_up_ladder(date: str = "") -> dict:
    """
    市场涨停天梯（快速版）。

    当日实时值可用（08-04 层级 {7,4,3,2,1}，最高板 7 = 传智教育，与同花顺一致）。

    ⚠️ 历史日期不可信：07-31 与 07-30 都返回最高板 10，而同花顺是 9 板与 8 板；
       同源的 get_consecutive_limit_up 同两天却返回 5 板。历史市场高度用 get_ladder。
    """
    c = _ensure_crawler()
    d = date or _today()
    data = _handle_error(c.get_market_limit_up_ladder, d) if date else \
        _handle_error(c.get_market_limit_up_ladder)
    if isinstance(data, dict) and "错误" in data:
        return data
    if isinstance(data, dict):
        lad = data.get("ladder") or {}
        levels = sorted((int(k) for k in lad), reverse=True)
        if not levels:
            return {"日期": data.get("date", d),
                    "数据状态": "端点未返回天梯层级（历史日期常见 errcode 1020）",
                    "数据源": "开盘啦市场涨停天梯",
                    "建议": "市场高度请用 get_ladder（同花顺）"}
        data["最高板"] = levels[0]
        data["各层只数"] = {str(k): len(lad.get(str(k)) or []) for k in levels}
        data["数据源"] = "开盘啦市场涨停天梯"
        if not data.get("is_realtime"):
            data["口径警告"] = "历史日期该端点最高板与同花顺不一致，建议用 get_ladder 复核"
    return data


def sector_limit_up_ladder(sector_code: str) -> dict:
    """
    板块涨停天梯 —— 板块内部的涨停梯队分布。

    参数：
        sector_code: 板块代码，如 801346

    ⚠️ 实测 801346 与板块排行里的真实代码（801660 等）都返回 errcode 1130，
       该维度当前拿不到数据。空结果会明确报缺数并带上错误码诊断。
    """
    c = _ensure_crawler()
    data = _handle_error(c.get_sector_limit_up_ladder, sector_code)
    if isinstance(data, dict) and "错误" in data:
        return data
    # crawler 返回的 date 字段里装的是板块代码，这里不透传它的错误结构
    secs = (data or {}).get("sectors") if isinstance(data, dict) else None
    if not secs:
        r = {"板块代码": sector_code, "日期": _today(),
             "数据状态": "开盘啦未返回该板块的涨停梯队",
             "数据源": "开盘啦板块涨停天梯",
             "建议": "板块内涨停成分用 get_theme_top(with_stocks=True)（同花顺）"}
        h = _noise_hint()
        if h:
            r["诊断"] = h
        return r
    return {"板块代码": sector_code, "日期": _today(),
            "数据源": "开盘啦板块涨停天梯",
            "梯队": secs, "总数": len(secs)}


def actual_limit_up_down() -> dict:
    """
    实际涨跌停（去一字板）。

    ⚠️ 实测这个端点在交易日**返回全 0**（08-04 四个字段都是 0，
       而真实涨停 138 家）。返回全 0 时本函数明确报缺数，不冒充真实值。
       要实际涨停(去一字板)请查历史日期的 market_sentiment(date)。
    """
    c = _ensure_crawler()
    data = _handle_error(c.get_realtime_actual_limit_up_down)
    if isinstance(data, dict) and "错误" in data:
        return data
    if isinstance(data, dict):
        vals = (data.get("actual_limit_up"), data.get("actual_limit_down"),
                data.get("limit_up"), data.get("limit_down"))
        if not any(vals):
            return {"日期": _today(),
                    "数据状态": "端点返回全 0（实测该端点当日不可用，不是真的没有涨停股）",
                    "数据源": "开盘啦实际涨跌停",
                    "建议": "涨停/跌停数用 kpl_market_sentiment 或 get_market_temperature"}
        return {
            "日期": _today(),
            "数据源": "开盘啦实际涨跌停",
            "实际涨停": data.get("actual_limit_up"),
            "实际跌停": data.get("actual_limit_down"),
            "涨停": data.get("limit_up"),
            "跌停": data.get("limit_down"),
        }
    return data


def broken_limit_up(date: str = "") -> dict:
    """
    历史炸板股（曾涨停但收盘没封住）。可回溯历史日期。

    参数：
        date: 日期 YYYY-MM-DD，空则今天

    实测已通（走 NO_PROXY 绕开死代理后）：
        08-04 得 15 只、08-03 得 16 只、07-30 得 19 只，
        与同花顺炸板数、东财炸板池逐位相同。

    ⚠️ 当日盘中该端点不入库，返回空。空列表**不代表当天没有炸板股**，
       所以这里区分「周末/节假日」「当日盘中」「原因不明」三种情况，
       不让空列表冒充「0 只」。
    """
    c = _ensure_crawler()
    d = date or _today()
    data = _handle_error(c.get_historical_broken_limit_up, d)
    if isinstance(data, dict) and "错误" in data:
        return data
    if hasattr(data, "empty"):
        data = [] if data.empty else data.to_dict("records")
    if isinstance(data, list):
        if not data:
            # 周末能零成本判断；其余不臆断原因（crawler 把网络异常也吞成空列表）
            try:
                wd = datetime.strptime(d, "%Y-%m-%d").weekday()
            except ValueError:
                wd = None
            if wd in (5, 6):
                why = f"{d} 是{'周六' if wd == 5 else '周日'}，非交易日"
                cause = "非交易日，无炸板数据（这是正常结果，不是故障）"
            elif d >= _today():
                why = "当日盘中该端点不入库"
                cause = "盘中请用 get_zb_pool（东财实时炸板池）"
            else:
                why = "返回空 —— 无法区分「当天没炸板股」和「取数失败」"
                cause = "可能是节假日 / 网络失败（crawler 吞掉异常返回空）"
            return {"日期": d, "数据状态": why, "可能原因": cause,
                    "数据源": "开盘啦历史炸板",
                    "建议": "炸板请用 get_zb_pool（东财），炸板数也可看 kpl_sentiment_history"}
        return {"日期": d, "数据源": "开盘啦历史炸板",
                "总数": len(data), "炸板股": data}
    return data


# ==================== 板块分析 ====================

def sector_ranking(date: str = "") -> dict:
    """
    板块排行。

    参数：
        date: 日期 YYYY-MM-DD，空则今天

    返回：
        板块涨跌幅排行，含成交额、主力资金
    """
    c = _ensure_crawler()
    if date:
        data = _handle_error(c.get_sector_ranking, date)
    else:
        data = _handle_error(c.get_sector_ranking)

    if isinstance(data, dict) and "sectors" in data:
        return data
    return {"板块列表": _to_dict(data)} if not isinstance(data, dict) else data


def multiple_sectors_strength(sector_codes: List[str]) -> dict:
    """
    批量查询板块强度。

    参数：
        sector_codes: 板块代码列表，如 ["801346", "801001"]

    返回：
        每个板块的强度值
    """
    c = _ensure_crawler()
    return _handle_error(c.get_multiple_sectors_strength, sector_codes)


def sector_intraday(sector_code: str, date: str = "") -> dict:
    """
    板块分时数据。

    参数：
        sector_code: 板块代码
        date: 日期 YYYY-MM-DD，空则今天

    返回：
        开盘、收盘、最高、最低、分时明细
    """
    c = _ensure_crawler()
    if date:
        data = _handle_error(c.get_sector_intraday, sector_code, date)
    else:
        data = _handle_error(c.get_sector_intraday, sector_code)

    if isinstance(data, dict) and "data" in data:
        if hasattr(data["data"], 'to_dict'):
            data["data"] = data["data"].to_dict('records')
    return data


def sector_constituent_stocks(sector_code: str) -> dict:
    """
    板块成分股（仅代码和名称）。

    参数：
        sector_code: 板块代码

    ⚠️ 实测 errcode 1020 —— 测试代码 801346 与板块排行里的真实代码 801660
       都取不到。空结果明确报缺数，不返回 total_count=0 冒充「该板块没有股票」。
    """
    c = _ensure_crawler()
    data = _handle_error(c.get_sector_constituent_stocks, sector_code)
    if isinstance(data, dict) and "错误" in data:
        return data
    stocks = (data or {}).get("stocks") if isinstance(data, dict) else None
    if not stocks:
        r = {"板块代码": sector_code, "日期": _today(),
             "数据状态": "开盘啦未返回该板块成分股",
             "数据源": "开盘啦板块成分股",
             "建议": "成分股用 get_stock_board_cons（东财，行业/概念都支持）"}
        h = _noise_hint()
        if h:
            r["诊断"] = h
        return r
    return data


def sector_all_stocks(sector_code: str) -> dict:
    """
    板块所有股票（含实时行情）。

    参数：
        sector_code: 板块代码

    ⚠️ 实测 errcode 1020，同 sector_constituent_stocks。
    """
    c = _ensure_crawler()
    data = _handle_error(c.get_sector_all_stocks, sector_code)
    if isinstance(data, dict) and "错误" in data:
        return data
    if isinstance(data, dict) and not (data.get("all_stocks") or data.get("total_count")):
        r = {"板块代码": sector_code, "日期": _today(),
             "数据状态": "开盘啦未返回该板块股票",
             "数据源": "开盘啦板块所有股票",
             "建议": "成分股+行情用 get_stock_board_cons（东财）"}
        h = _noise_hint()
        if h:
            r["诊断"] = h
        return r
    return data


def sector_bidding_anomaly(sector_code: str) -> dict:
    """
    板块集合竞价异动 —— 9:15-9:25 竞价阶段的异动股票。现有数据源不提供。

    参数：
        sector_code: 板块代码

    ⚠️ 实测 errcode 1130；且竞价数据只在开盘那 10 分钟有效，盘后必然为空。
    """
    c = _ensure_crawler()
    data = _handle_error(c.get_sector_bidding_anomaly, sector_code)
    if isinstance(data, dict) and "错误" in data:
        return data
    if isinstance(data, dict):
        n = sum(len(data.get(k) or []) for k in ("list1", "list2", "list3"))
        if not n:
            r = {"板块代码": sector_code, "日期": _today(),
                 "数据状态": "未返回竞价异动",
                 "可能原因": "竞价数据只在 9:25 后当日有效，盘后查为空；或该板块无此维度",
                 "数据源": "开盘啦板块竞价异动",
                 "建议": "要用这个接口须在 9:25-9:30 之间调"}
            h = _noise_hint()
            if h:
                r["诊断"] = h
            return r
        # crawler 的 date 字段里装的是板块代码，这里改回正确语义
        data = dict(data)
        data["板块代码"] = sector_code
        data["日期"] = _today()
        data.pop("date", None)
    return data


# ==================== 个股数据 ====================

def stock_intraday(stock_code: str, date: str = "") -> dict:
    """
    个股分时数据（每分钟价格/均价/成交量/主力净流入）。

    参数：
        stock_code: 股票代码，如 000001
        date: 日期 YYYY-MM-DD，空则今天

    ⚠️ **当日不可用，历史日期正常**：08-05 盘中无论传不传日期都报 errcode 1020；
       08-04 返回 241 个分时点。六种代码写法（000001 / SZ000001 / sz000001 /
       600519 …）结果一致，所以跟代码格式无关，是该端点当日不入库。
       盘中要分时用 get_intraday（东财/腾讯）。
    """
    c = _ensure_crawler()
    if date:
        data = _handle_error(c.get_stock_intraday, stock_code, date)
    else:
        data = _handle_error(c.get_stock_intraday, stock_code)

    if isinstance(data, dict) and "错误" in data:
        return data
    # crawler 在错误码上返回空 dict，不抛异常 —— 这里要明确报缺数
    if not data:
        d = date or _today()
        r = {"股票代码": stock_code, "日期": d,
             "数据状态": ("该端点当日不入库（实测 08-05 报 1020、08-04 正常）"
                        if d >= _today() else "开盘啦未返回该日期的分时数据"),
             "数据源": "开盘啦个股分时",
             "建议": "盘中分时用 get_intraday（东财/腾讯）；历史分时也可用 get_day_bars_cached"}
        h = _noise_hint()
        if h:
            r["诊断"] = h
        return r
    if isinstance(data, dict) and "data" in data:
        if hasattr(data["data"], 'to_dict'):
            data["data"] = data["data"].to_dict('records')
    return data


def stock_big_order_intraday(stock_code: str) -> dict:
    """
    个股大单分时（每分钟大单净额与笔数）。现有数据源不提供。

    参数：
        stock_code: 股票代码

    ⚠️ crawler 给的三个汇总字段 big_order_buy_total / sell_total / net_total
       **恒为 0**（实测 000001 明细里 big_order_net 有 73 行非零，汇总却是 0），
       所以这里从明细自己求和，并把它那三个字段标成不可用。

    ⚠️ 明细里 unknown1~unknown7 是未解出含义的原始列，原样保留不做解释。
    """
    c = _ensure_crawler()
    data = _handle_error(c.get_stock_big_order_intraday, stock_code)
    if isinstance(data, dict) and "错误" in data:
        return data
    if not data:
        r = {"股票代码": stock_code, "日期": _today(),
             "数据状态": "开盘啦未返回大单分时数据",
             "数据源": "开盘啦个股大单分时",
             "建议": "主力资金分钟级用 get_fund_flow_min（东财，五档齐全）"}
        h = _noise_hint()
        if h:
            r["诊断"] = h
        return r
    if not isinstance(data, dict):
        return data

    recs = data.get("data")
    if hasattr(recs, "to_dict"):
        recs = recs.to_dict("records")
    if not recs:
        return {"股票代码": stock_code, "日期": data.get("date") or _today(),
                "数据状态": "返回了外层结构但明细为空",
                "数据源": "开盘啦个股大单分时",
                "建议": "主力资金分钟级用 get_fund_flow_min（东财）"}

    def _sum(key):
        return round(sum(float(x.get(key) or 0) for x in recs), 2)

    buy, sell = _sum("intraday_buy"), _sum("intraday_sell")
    out = {
        "股票代码": stock_code,
        "日期": data.get("date") or _today(),
        "数据源": "开盘啦个股大单分时",
        "分钟数": len(recs),
        "大单净额合计": _sum("big_order_net"),
        "大单买入合计": buy,
        "大单卖出合计": sell,          # 原始值本身是负数，不取绝对值
        "买入笔数合计": int(_sum("buy_orders")),
        "卖出笔数合计": int(_sum("sell_orders")),
        "合计口径": "由明细逐行求和得出（端点自带的三个 total 字段恒为 0，不可用）",
        "字段说明": "unknown1~unknown7 为未解出含义的原始列，原样保留",
        "明细": recs,
    }
    return out


def stock_call_auction(stock_code: str) -> dict:
    """
    个股集合竞价（新增）。

    参数：
        stock_code: 股票代码

    返回：
        9:15-9:25 竞价时段的逐笔数据
    """
    c = _ensure_crawler()
    data = _handle_error(c.get_stock_call_auction_tick, stock_code)
    if isinstance(data, dict) and "错误" in data:
        return data
    # 这个接口内部打东财 push2 的逐笔明细，crawler 吞掉异常返回空 dict
    if not data:
        return {"股票代码": stock_code, "日期": _today(),
                "数据状态": "未返回竞价时段数据",
                "可能原因": ("东财逐笔明细只保留当日，且 9:25 后竞价那 10 分钟的 tick "
                            "会被盘中成交挤出窗口 —— 盘后查基本都是空的"),
                "数据源": "开盘啦个股集合竞价",
                "建议": "要用这个接口须在 9:25-9:30 之间调；盘后无替代源"}
    if isinstance(data, dict) and "data" in data:
        if hasattr(data["data"], 'to_dict'):
            data["data"] = data["data"].to_dict('records')
    return data


# ==================== 龙虎榜 ====================

def longhubang_list() -> dict:
    """
    龙虎榜股票列表。

    ⚠️ **龙虎榜盘后才发布**。盘中调用该端点返回空列表，
       空列表不等于「今天没人上榜」，所以这里明确报「尚未发布」并给出
       建议日期，与 live_market_ex.lhb() 同口径。

    发布时点实测（2026-08-05 两轮取数夹出来的区间，不是精确时刻）：
       13:00 前 → 空；17:56 → 已有 66 只。
       所以此前注释里写的「约 18:00」偏保守，实际更早，但具体几点没测。
    """
    c = _ensure_crawler()
    data = _handle_error(c.get_longhubang_stock_list)
    if isinstance(data, dict) and "错误" in data:
        return data
    if isinstance(data, dict):
        data = data.get("stocks") if "stocks" in data else data
    if isinstance(data, list):
        if not data:
            r = {"日期": _today(),
                 "数据状态": "尚未发布（龙虎榜盘后才出，盘中查当天为空是正常的；"
                             "实测 13:00 前为空、17:56 已发布，具体时点未测）",
                 "数据源": "开盘啦龙虎榜",
                 "建议日期": _last_trade_day(),
                 "建议": "查上一交易日，或用 get_lhb（东财，带上榜后 1/2/5/10 日涨跌幅）"}
            h = _noise_hint()
            if h:
                r["诊断"] = h
            return r
        return {"日期": _today(), "数据源": "开盘啦龙虎榜",
                "股票列表": data, "总数": len(data)}
    return data


def longhubang_dataframe() -> dict:
    """
    龙虎榜DataFrame格式。

    返回：
        表格形式的龙虎榜数据
    """
    c = _ensure_crawler()
    data = _handle_error(c.get_longhubang_dataframe)
    return {"龙虎榜": _to_dict(data)} if not isinstance(data, dict) else data


def longhubang_detail(stock_code: str) -> dict:
    """
    龙虎榜个股明细（买入/卖出席位与金额）。

    参数：
        stock_code: 股票代码

    ⚠️ 该票当天没上榜时，端点返回的是**空壳**而不是报错：
       stock_name='' / buy_sell_data=[] / on_list_count=0，但金额字段都是 0。
       直接透传会让 0 看起来像「上榜净买 0 元」，所以这里以 buy_sell_data
       是否有席位来判定，没席位就报缺数。

       端点自带的 on_time_list 是该票**历史上榜日期**，即使当天没上榜也有值，
       所以它有内容不代表今天上了榜 —— 缺数时把它单独列出来当参考。
    """
    c = _ensure_crawler()
    data = _handle_error(c.get_longhubang_stock_detail, stock_code)
    if isinstance(data, dict) and "错误" in data:
        return data
    if isinstance(data, dict) and not data.get("buy_sell_data"):
        hist = data.get("on_time_list") or []
        r = {"股票代码": stock_code, "日期": data.get("date") or _today(),
             "数据状态": "该票当天未上榜或龙虎榜尚未发布（端点返回空壳，金额字段是默认 0）",
             "数据源": "开盘啦龙虎榜明细",
             "建议日期": _last_trade_day(),
             "建议": "查上一交易日，或用 get_lhb_dept（东财，带席位性质与3日胜率）"}
        if hist:
            r["该票历史上榜日期"] = hist[:10]
            r["历史日期说明"] = "这是过往上榜记录，不代表今天上榜"
        h = _noise_hint()
        if h:
            r["诊断"] = h
        return r
    return data


# ==================== 其他 ====================

def new_high(date: str = "") -> dict:
    """
    百日新高（每日新增创百日新高的家数）。现有数据源不提供，突破信号。

    参数：
        date: 日期 YYYY-MM-DD，空则今天

    该端点一次返回全部可回溯日期（实测 360 个交易日，2025-02-13 起），
    所以本函数取全序列后自己查这一天：查得到给真值，查不到明确报缺数
    并区分「晚于最新可用日」和「范围内不存在（非交易日）」两种情况，
    **绝不返回 0** —— 0 和「没这天」是两件事，混在一起会让复盘误判成冰点。

    ⚠️ 当日盘中该端点不入库，最新一行是上一交易日收盘。
    """
    c = _ensure_crawler()
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    # 拿全序列（该端点忽略请求里的日期,始终返回全部,crawler 在本地按范围筛）
    data = _handle_error(c.get_new_high_data, "2099-01-01", "2000-01-01")
    if isinstance(data, dict) and "错误" in data:
        return data

    # 期望是 pd.Series(index=日期, value=今日新增)
    idx = getattr(data, "index", None)
    if idx is None or len(idx) == 0:
        return {"日期": date, "数据状态": "开盘啦 StockNewHigh 返回空序列（取数失败或端点变更）",
                "数据源": "开盘啦StockNewHigh"}

    ser = {str(k): v for k, v in zip(idx, list(data.values))}
    dates = sorted(ser.keys(), reverse=True)

    if date in ser:
        v = ser[date]
        return {"日期": date, "百日新高数": int(v),
                "数据源": "开盘啦StockNewHigh",
                "口径": "当日新增创百日新高的家数（不是累计新高总数）",
                "可回溯范围": f"{dates[-1]} ~ {dates[0]}（共 {len(dates)} 个交易日）"}

    # 区分三种「查不到」：晚于最新可用日 / 早于最早可用日 / 落在范围内但不存在
    if date > dates[0]:
        why = (f"晚于最新可用日 {dates[0]} —— 该端点当日盘中不入库，"
               f"或 {date} 尚未收盘/不是交易日")
    elif date < dates[-1]:
        why = f"早于最早可用日 {dates[-1]} —— 超出该端点可回溯范围"
    else:
        why = "落在可回溯范围内但库里没有这一行 —— 基本可判定为非交易日（周末/节假日）"

    return {"日期": date,
            "数据状态": f"该日期无数据：{why}",
            "数据源": "开盘啦StockNewHigh",
            "可回溯范围": f"{dates[-1]} ~ {dates[0]}（共 {len(dates)} 个交易日）",
            "最近可用日期": dates[0],
            "建议": "换成范围内的交易日再查；不要把这里当成 0"}


def sharp_withdrawal(date: str = "") -> dict:
    """
    大幅回撤（冲高回落）—— 可回溯历史日期。

    参数：
        date: 日期 YYYY-MM-DD，空则今天

    ⚠️ 走历史端点 HisHomeDingPan，**当日与上一交易日都可能还没入库**
       （实测 08-05 盘中空、08-04 也空，而 08-03 得 3 只、07-30 得 3 只）。
       盘中冲高回落用 kpl_realtime_sharp_withdrawal 或 get_intraday_shape。

    ⚠️ 该端点自带的「总数」字段与实际返回条数不一致（08-03 总数说 9 只，
       实际只给 3 条明细），所以两个数都保留并标明口径，不替它圆。
    """
    c = _ensure_crawler()
    d = date or _today()
    data = _handle_error(c.get_sharp_withdrawal, d)
    if isinstance(data, dict) and "错误" in data:
        return data
    # crawler 返回 DataFrame，统一成 dict（原先直接漏出 DataFrame，MCP 层无法序列化）
    recs = None
    if hasattr(data, "empty"):
        recs = [] if data.empty else data.to_dict("records")
    elif isinstance(data, list):
        recs = data

    if recs:
        claimed = recs[0].get("总数")
        r = {"日期": d, "数据源": "开盘啦大幅回撤(HisHomeDingPan)",
             "明细条数": len(recs), "股票列表": recs}
        if claimed is not None and int(claimed) != len(recs):
            r["端点自称总数"] = int(claimed)
            r["口径警告"] = (f"端点说当天有 {int(claimed)} 只，但只给了 {len(recs)} 条明细。"
                            "两个数都列出，不确定哪个是全量，别直接当家数用")
        return r

    if recs == [] or not data:
        try:
            wd = datetime.strptime(d, "%Y-%m-%d").weekday()
        except ValueError:
            wd = None
        if wd in (5, 6):
            why = f"{d} 是{'周六' if wd == 5 else '周日'}，非交易日"
        elif d >= _today():
            why = "该历史端点当日不入库"
        else:
            why = "该日期无数据 —— 实测最近两个交易日常为空，入库有延迟"
        r = {"日期": d, "数据状态": why,
             "数据源": "开盘啦大幅回撤(HisHomeDingPan)",
             "建议": "盘中用 kpl_realtime_sharp_withdrawal；单只票的冲高回落用 get_intraday_shape"}
        h = _noise_hint()
        if h:
            r["诊断"] = h
        return r
    return data


def realtime_sharp_withdrawal() -> dict:
    """
    实时大幅回撤。

    返回：
        当日盘中大幅回撤的股票
    """
    c = _ensure_crawler()
    return _handle_error(c.get_realtime_sharp_withdrawal)


def ths_hot_rank() -> dict:
    """
    同花顺热榜。

    返回：
        同花顺人气排行榜
    """
    c = _ensure_crawler()
    data = _handle_error(c.get_ths_hot_rank)

    if isinstance(data, dict) and "错误" in data:
        return data

    # DataFrame/Series/list 统一处理
    converted = _to_dict(data)
    if isinstance(converted, list):
        return {"热榜": converted}
    elif isinstance(converted, dict):
        return {"热榜": converted}
    else:
        return {"热榜": str(data)}


def etf_ranking() -> dict:
    """
    ETF排行。

    返回：
        ETF涨跌幅排行
    """
    c = _ensure_crawler()
    data = _handle_error(c.get_etf_ranking)

    if isinstance(data, dict) and "错误" in data:
        return data

    converted = _to_dict(data)
    if isinstance(converted, list):
        return {"ETF排行": converted}
    elif isinstance(converted, dict):
        return {"ETF排行": converted}
    else:
        return {"数据": str(data)}


def all_etf_ranking() -> dict:
    """
    所有ETF排行。

    返回：
        全市场ETF排行
    """
    c = _ensure_crawler()
    data = _handle_error(c.get_all_etf_ranking)

    if isinstance(data, dict) and "错误" in data:
        return data

    converted = _to_dict(data)
    if isinstance(converted, list):
        return {"ETF排行": converted}
    elif isinstance(converted, dict):
        return {"ETF排行": converted}
    else:
        return {"数据": str(data)}


def plate_news(sector_code: str) -> dict:
    """
    板块新闻。

    参数：
        sector_code: 板块代码

    返回：
        该板块相关的最新新闻
    """
    c = _ensure_crawler()
    data = _handle_error(c.get_plate_news, sector_code)
    if isinstance(data, list):
        return {"板块代码": sector_code, "新闻列表": data, "总数": len(data)}
    return data


# ==================== 便捷函数 ====================

def test_connection() -> dict:
    """测试连接是否正常"""
    try:
        c = _ensure_crawler()
        data = c.get_realtime_market_mood()
        return {"状态": "正常", "测试接口": "实时市场情绪", "返回键": list(data.keys())[:5]}
    except Exception as e:
        return {"状态": "失败", "错误": str(e)}


if __name__ == "__main__":
    print("开盘啦数据源模块")
    print("测试连接...")
    result = test_connection()
    print(result)
