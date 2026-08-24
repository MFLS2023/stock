#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
实时行情 MCP Server —— 把 live_market.py 的能力暴露给 Claude 等 MCP 客户端。

数据源：东方财富 push2delay + 腾讯 + 新浪，全部免费、无需注册、无需 API Key。
三源自动降级，取数失败明确报错，不返回假数据。

启动（stdio 模式，供 MCP 客户端调用）：
    python live_market_mcp.py

MCP 客户端配置（.mcp.json 或客户端设置）：
    {
      "mcpServers": {
        "live-market": {
          "command": "python",
          "args": ["C:\\\\Users\\\\20577\\\\Documents\\\\炒股\\\\知识库\\\\_知识库系统\\\\scripts\\\\live_market_mcp.py"]
        }
      }
    }
"""
from __future__ import annotations

import sys
from pathlib import Path

# 保证能 import 同目录的 live_market
sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_market as lm  # noqa: E402
import live_market_ex as ex  # noqa: E402
import live_market_ths as ths  # noqa: E402
import live_market_health as health  # noqa: E402
import live_market_cache as cache  # noqa: E402
import live_market_akshare as ak  # noqa: E402
import live_market_kaipanla as kpl  # noqa: E402
import rps_log  # noqa: E402  第7指标（RPS三线红池数）的每日计数流水
import rps_pool  # noqa: E402  第7指标的名单层：通达信导出解析 + 跨日统计 + 日间对比
import rps_local  # noqa: E402  第7指标的本机算路：直读通达信 extdata + lday 算三线红
from mcp.server.mcpserver import MCPServer  # noqa: E402

mcp = MCPServer(
    name="live-market",
    instructions=(
        "A 股实时行情 + 盘后复盘工具，数据来自东方财富/腾讯/新浪免费接口，多源自动降级。\n"
        "所有返回值都带取数时间戳，盘中数据延迟约 1-3 秒。\n"
        "用于把当前真实盘面喂给分析流程，替代模型记忆里的过时行情。\n"
        "\n"
        "选工具的顺序：\n"
        "  1. 短线看盘先 get_shortline_board（题材主线 + 连板天梯 + 归因热词，约 2KB）\n"
        "  2. 再 get_market_temperature 定位情绪周期位置\n"
        "  3. 盘中看涨停结构用 get_zt_pool_detail（带首封时间/炸板次数/封单额）\n"
        "  4. 想知道某只票「为什么涨停」用 get_stock_reason（同花顺归因全文）\n"
        "  5. 验证弱转强/反包用 get_yesterday_zt（昨涨停今日表现）\n"
        "  6. 分析某天日内形态用 get_intraday_shape（冲高回落量化，可回溯约 60 个交易日）\n"
        "  7. 盘后复盘一次取齐用 get_after_close\n"
        "\n"
        "两套涨停数据的分工（别重复调）：\n"
        "  东财 get_zt_pool_detail  → 封单额、封成比、首末封板时间（钱的维度）\n"
        "  同花顺 get_zt_reason     → 涨停归因标签、官方封板成功率（逻辑的维度）\n"
        "\n"
        "注意：这些是当前市场事实，与知识库里作者的历史观点必须分开陈述。\n"
        "取数失败会明确报错，不返回假数据。"
    ),
)


@mcp.tool()
def get_market_temperature() -> dict:
    """
    大盘情绪温度计。判断情绪周期位置的最小充分集，做任何盘面分析都应先调这个。

    返回：五大指数点位涨跌、沪深上涨/下跌家数、赚钱效应百分比、
    两市成交额、实时涨停数、跌停数、涨停股的行业聚集分布。
    """
    return lm.market_temp()


@mcp.tool()
def get_quote(codes: str) -> list[dict]:
    """
    个股实时行情。多个代码用逗号分隔，如 "002827,600519"。

    返回：最新价、涨跌幅、量比、换手率、振幅、市盈率、市值、涨跌停价、行情时间戳。
    量比和换手率是判断承接与情绪强度的关键读数。
    """
    out = []
    for c in [x.strip() for x in codes.split(",") if x.strip()]:
        try:
            out.append(lm.quote(c))
        except Exception as e:  # noqa: BLE001 - 单只失败不影响其余
            out.append({"代码": c, "错误": f"{type(e).__name__}: {e}"})
    return out


@mcp.tool()
def get_limit_up_pool(limit: int = 120) -> dict:
    """
    实时涨停池。按各板块自己的涨停幅度判定（主板10%/创业科创20%/北交30%/ST5%），
    新股单独标出不混入涨停家数。

    每只带：板制、量比、换手率、成交额、主力净流入、所属行业。
    行业聚集度直接反映主线强度。
    """
    rows = lm.limit_up_pool(limit)
    zt = [r for r in rows if r["板制"] != "新股"]
    return {
        "取数时间": lm.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "涨停家数": len(zt),
        "行业分布": lm._top_industries(zt, 10),
        "明细": rows,
    }


@mcp.tool()
def get_strong_stocks(limit: int = 30) -> dict:
    """
    量比榜。按量比降序，量比是当日资金关注度相对历史的倍数，
    高量比配合上涨是资金进场的直接证据。
    """
    return {
        "取数时间": lm.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "说明": "按量比降序。量比 = 当日均每分钟成交量 / 过去5日均每分钟成交量",
        "明细": lm.strong_pool(limit),
    }


@mcp.tool()
def get_sector_flow(kind: str = "industry", limit: int = 20) -> dict:
    """
    板块实时主力资金流向。kind: "industry" 行业板块 / "concept" 概念板块。

    返回每个板块的涨跌幅、主力净流入（亿）、主力净占比、超大单净流入、领涨股。
    主力净占比比绝对值更有意义，它排除了板块体量差异。
    """
    return {
        "取数时间": lm.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "类型": "行业板块" if kind == "industry" else "概念板块",
        "明细": lm.sector_flow(kind, limit),
    }


@mcp.tool()
def get_daily_kline(code: str, n: int = 30, adjust: str = "qfq") -> dict:
    """
    日线。adjust: qfq 前复权 / hfq 后复权 / 空串不复权。
    最后一根是当日实时值，盘中可用于判断当日形态。
    """
    return {
        "代码": code,
        "复权": adjust or "不复权",
        "说明": "最后一根为当日实时值，盘中未收盘",
        "K线": lm.daily(code, n, adjust),
    }


@mcp.tool()
def get_intraday(code: str, days: int = 1) -> dict:
    """
    分时数据（1 分钟粒度）。days=1 为当日。
    含每分钟的开高低收、成交量、成交额、均价线。
    均价线与股价的关系是判断日内强弱的常用依据。
    """
    rows = lm.minutes(code, days)
    return {
        "代码": code,
        "条数": len(rows),
        "分时": rows[-240:],   # 最多返回 240 根，控制上下文体积
    }


@mcp.tool()
def get_full_snapshot(code: str = "") -> dict:
    """
    一次取回完整盘面快照：大盘温度 + 板块资金流 + 涨停池 + （可选）指定个股。
    做盘中综合分析时用这个，一次调用拿齐全部上下文，约 8KB。

    code 留空则只返回大盘层面数据。
    """
    res = {
        "大盘": lm.market_temp(),
        "板块资金流TOP10": lm.sector_flow("industry", 10),
        "涨停池": lm.limit_up_pool(40),
    }
    if code:
        try:
            res["个股"] = lm.quote(code)
        except Exception as e:  # noqa: BLE001
            res["个股"] = {"代码": code, "错误": str(e)}
    return res


# ==================== 以下为扩展工具（live_market_ex）====================
# 补齐四类原本拿不到的数据：股池细节、历史分钟K线、分钟资金流、盘后数据。
# 接口来自 2026-08-04 的 37 接口全域探测（29 可用），回归 18/18 通过。


@mcp.tool()
def get_zt_pool_detail(date: str = "") -> dict:
    """
    涨停池详细版。**比 get_limit_up_pool 多给四个关键字段**：
    首次封板时间、最后封板时间、炸板次数、连板数（N天M板）、封单额、封成比。

    这些是判断封板质量的核心读数，行情列表接口推不出来：
      - 首次封板时间早 = 资金抢筹果断
      - 炸板次数 > 0 = 当天封板不牢，反复打开过
      - 封成比 = 封单额 / 成交额，越高说明卖压越轻

    date 传空为今天，格式 YYYYMMDD（如 20260804）。
    """
    return ex.zt_pool(date)


@mcp.tool()
def get_zb_pool(date: str = "") -> dict:
    """
    炸板池 —— 曾涨停但当前已打开的票。情绪转弱的直接读数。
    配合涨停池可算封板成功率：涨停数 / (涨停数 + 炸板数)。
    """
    return ex.zb_pool(date)


@mcp.tool()
def get_yesterday_zt(date: str = "") -> dict:
    """
    昨日涨停股的今日表现 —— **验证「弱转强」「反包」的直接证据**。

    返回每只昨涨停票的今日涨跌幅、溢价率、昨日首封时间、昨日连板数，
    并汇总「昨涨停今日红盘率」，这是情绪延续性的核心指标。

    知识库里的弱转强概念要落到当前市场验证，就查这个。
    """
    return ex.yesterday_zt(date)


@mcp.tool()
def get_qs_pool(date: str = "") -> dict:
    """
    强势池 —— 涨幅靠前但未必涨停的票。
    带「是否创新高」「连续上涨天数」「量比」，找主线扩散标的用这个。
    """
    return ex.qs_pool(date)


@mcp.tool()
def get_dt_pool(date: str = "") -> dict:
    """跌停池。返回 0 只是合法结果（当天没有跌停股），不是接口故障。"""
    return ex.dt_pool(date)


@mcp.tool()
def get_cx_pool(date: str = "") -> dict:
    """次新股池，带上市天数和上市日期。次新板块单独看，不混入涨停统计。"""
    return ex.cx_pool(date)


@mcp.tool()
def get_minute_kline(code: str, period: int = 5, n: int = 320) -> dict:
    """
    分钟 K 线，**可回溯历史**（当日分时看 get_intraday）。
    period 支持 1/5/15/30/60 分钟。

    n 的取值决定能回溯多久（实测 5 分钟粒度）：
      n=320  → 约 7 个交易日（走腾讯，快）
      n=1000 → 约 21 个交易日
      n=3000 → 约 63 个交易日（走新浪）
    n 超过 320 会自动切新浪源，因为腾讯单次硬上限是 320 根。
    """
    rows = ex.minute_kline(code, period, n)
    return {
        "代码": code, "周期": f"{period}分钟", "根数": len(rows),
        "时间范围": f"{rows[0]['时间']} ~ {rows[-1]['时间']}" if rows else None,
        "K线": rows,
    }


@mcp.tool()
def get_intraday_shape(code: str, date: str = "") -> dict:
    """
    某个交易日的日内形态量化 —— **把「冲高回落」算成数字**。

    返回：收距最高%、收距最低%、均价线、在均价上方的时间占比、
    日内最大回撤及发生时间、最高点出现在前半场还是后半场。

    判读：
      收距最高% 接近 0    = 收在最高，没有冲高回落
      收距最高% 低于 -5%  = 明显冲高回落
      在均价上方占比 > 80% = 全天强势，均价线支撑有效

    date 传空为最后一个交易日，格式 YYYY-MM-DD。可回溯约 60 个交易日，
    这是对比「今天和前几天的日内结构差异」的工具。
    """
    return ex.intraday_shape(code, date)


@mcp.tool()
def get_fund_flow_min(code: str, tail: int = 60) -> dict:
    """
    个股分钟级资金流（当日）。主力/超大单/大单/中单/小单五档。

    **是累计值不是增量值** —— 每个时间点的数字是当日开盘至该分钟的累计净流入。
    带 30 分钟采样的节奏视图，看主力是全天持续买还是某个时段突击。

    字段已用两条恒等式校验：大单+超大单=主力，主力+小单+中单=0。
    历史日频资金流走 stock_data MCP 的 stock_fund_flow。
    """
    return ex.fund_flow_min(code, tail)


@mcp.tool()
def get_lhb(date: str = "", limit: int = 40) -> dict:
    """
    龙虎榜个股明细。date 格式 YYYY-MM-DD，**传空自动取最近已收盘交易日**。

    龙虎榜盘后才发布。盘中查当天会返回 `数据状态: 尚未发布` 和建议日期，
    不是接口故障。带上榜后 1/2/5/10 日涨跌幅，可直接验证「上榜之后怎么走」。
    """
    return ex.lhb(date, limit)


@mcp.tool()
def get_lhb_dept(date: str = "", code: str = "", limit: int = 40) -> dict:
    """
    龙虎榜营业部明细 —— **判断游资还是机构的直接依据**。

    席位名含「机构专用」标为机构，其余为营业部（游资），带该席位 3 日胜率。
    date 传空自动取最近已收盘交易日；code 传空为全市场，传代码查单只票。
    """
    return ex.lhb_dept(date, code, limit)


@mcp.tool()
def get_block_trade(date: str = "", limit: int = 40) -> dict:
    """
    大宗交易。折溢价率反映大额筹码的转手意愿。
    date 格式 YYYY-MM-DD，传空自动取最近已收盘交易日。
    """
    return ex.block_trade(date, limit)


@mcp.tool()
def get_margin_detail(limit: int = 30) -> dict:
    """两融个股明细，按日期降序。融资余额变化是杠杆资金意愿的读数。"""
    return ex.margin_detail(limit)


@mcp.tool()
def get_after_close(date: str = "") -> dict:
    """
    盘后复盘全套，一次取齐。**每日复盘的入口工具**。

    包含涨停池、炸板池、昨涨停今表现、跌停池，并自动算出：
    封板成功率、炸板率、涨停股中曾炸板的只数、昨涨停今日赚钱效应、涨停行业分布。

    date 传空为今天，格式 YYYYMMDD。
    """
    return ex.after_close(date)


# ============ 以下为短线专用工具（live_market_ths，同花顺 + 东财异动）============
# 补齐东财股池给不了的：涨停归因、连板天梯、题材涨停聚集度。
# 数据源 data.10jqka.com.cn，实测可回溯历史（试到 2026-06）。


@mcp.tool()
def get_shortline_board(date: str = "") -> dict:
    """
    短线综合看板 —— **短线看盘的第一个工具**，约 2KB 拿齐主线判定的三层数据。

    一、题材涨停聚集度 TOP8：每个题材涨停几只、几只连板、最高标几板、连续活跃几天
    二、连板天梯：按板数分层，一眼看到最高标和断层位置
    三、涨停结构：官方封板成功率（今日 + 上一交易日对比）+ 涨停归因热词 TOP15

    判读：涨停多 + 连板多 + 连续天数长 = 主线；涨停多但连板少 = 一日游风险。
    归因热词是找「今天资金在买什么逻辑」最快的读数。

    date 传空为今天，支持 YYYYMMDD 或 YYYY-MM-DD，可回溯历史。
    """
    return ths.shortline_board(date)


@mcp.tool()
def get_zt_reason(date: str = "", limit: int = 60) -> dict:
    """
    同花顺涨停池 —— **带涨停归因标签**，这是东财股池没有的。

    每只票带：涨停归因（如「算力租赁+英伟达合作+中报预增」）、连板表述（「7天7板」）、
    该票历史封板成功率、炸板次数、封板类型（首次封板 / 炸板后回封）。

    还给出同花顺官方口径的市场封板成功率 = 收盘封板数 / 曾涨停数，
    已用两条恒等式在 5 个交易日 20 组数据上校验通过。

    limit 只截断明细条数，统计和归因热词始终基于全部涨停股，不会因 limit 变小而失真。
    要封单额、封成比请用 get_zt_pool_detail（东财），两者互补。
    """
    return ths.zt_reason(date, limit)


@mcp.tool()
def get_stock_reason(code: str, date: str = "") -> dict:
    """
    单只票「为什么涨停」—— 同花顺 AI 汇总的归因全文。

    返回行业原因 + 公司原因的完整文字（含公告日期、互动易问答、业绩预告要点），
    以及涨停标签、所属题材、首末封板时间。

    这是回答「这票今天涨停是什么逻辑」最直接的数据，比看行情猜要准。
    仅涵盖当日前 20 大题材的成分股；不在其中会返回「未找到」而不是编造。
    """
    return ths.stock_reason(code, date)


@mcp.tool()
def get_ladder(date: str = "") -> dict:
    """
    连板天梯 —— 按板数分层列出所有连板股。

    最高板在哪、几只、断层在哪一级，这是判断情绪高度的核心视图。
    只含 2 板及以上；首板看 get_zt_reason。可回溯历史。
    """
    return ths.ladder(date)


@mcp.tool()
def get_theme_top(date: str = "", with_stocks: bool = False) -> dict:
    """
    题材板块涨停聚集度 —— **判断短线主线的直接读数**。

    每个题材带：涨幅、涨停只数、连板只数、最高标几板、题材连续活跃天数。
    与 get_sector_flow（板块资金流）的区别：这个数涨停家数，那个数资金额。
    找主线看这个，找资金去向看那个。

    with_stocks=True 附带每个题材的涨停成分股（含首封时间、归因标签），
    体积从约 2KB 涨到约 70KB，只在需要看成分时开。
    """
    return ths.theme_top(date, with_stocks)


@mcp.tool()
def get_stock_changes(types: str = "", limit: int = 60) -> dict:
    """
    盘口异动 —— 实时逐笔级别的异动推送（封板、打开涨停、火箭发射、高台跳水等）。

    **只有当日数据，收盘后清空，不能回溯。**
    types 传空取短线常用码；调 get_change_types 看对照表。
    中文类型名用东财官方映射，可直接引用。
    """
    return ths.changes(types, limit)


@mcp.tool()
def get_change_types() -> dict:
    """
    盘口异动 type 码对照表（东财官方映射，22 个码）。

    256 / 512 实测始终返回空；32「打开跌停板」在当日无跌停股时返回 0 条是合法的。
    另有 8217 实测有数据但官方表未收录，含义未知。
    """
    return ths.change_types()


# ========== 数据层基础设施 ==========

@mcp.tool()
def get_source_health(quick: bool = True) -> dict:
    """
    接口自检 —— 一次跑完全部数据源，报哪些通、哪些坏、字段有没有变。

    quick=True 跑 21 项（约 2 秒），False 跑完整 24 项（约 15 秒含两融）。
    返回按源汇总，标出整源死亡（DNS 挂、IP 封、域名过期）vs 单个接口挂。
    每次取数前扫一眼，避免拿坏数据还不知道。
    """
    return health.health(quick)


@mcp.tool()
def cross_check(code: str = "000001", date: str = "") -> dict:
    """
    交叉校验 —— 同一指标两个源都能拿的自动比对，不一致就报出来。

    6 组校验：价格/涨跌幅/成交量 三源比对，涨停家数（含逐只归因），炸板家数，
    封板成功率（自算 vs 同花顺官方），最高板，同花顺恒等式（收盘封板+炸板=曾涨停）。

    date 传空取最近交易日。回溯历史日期时，涨停家数归因降级为数值容差比对
    （官方池数据只保留 5 天）。
    """
    return health.cross_check(code, date)


@mcp.tool()
def get_daily_kline_cached(code: str, n: int = 30, adjust: str = "qfq",
                           force: bool = False) -> dict:
    """
    日线（缓存版）—— 查过的存本地，重复查读缓存。首次约 0.5s，命中约 0.002s。

    adjust: qfq 前复权 / hfq 后复权 / 不传不复权，三者独立缓存互不干扰。
    force=True 强制回源（用于怀疑缓存脏时）。

    返回带 数据来源/缓存命中/网络请求/回源原因/已写入缓存 五个字段，
    看得见每次是命中还是回源。
    """
    return cache.daily(code, n, adjust, force)


@mcp.tool()
def get_minute_kline_cached(code: str, period: int = 5, n: int = 48,
                            force: bool = False) -> dict:
    """
    分钟线（缓存版）—— 查过的存本地，但**盘中今天必然回源**（这不是 bug，
    是保证实时性）。真正节省 token 的用法是 get_day_bars_cached。

    period: 1 / 5 / 15 / 30 / 60，单位分钟。
    返回从最新往回数 n 根，盘中最后一根是不完整的当前 bar。

    回源逻辑：取 meta 记的「上次网络最新 bar 时间」，若当前时间晚于它就回源。
    交易日盘中这个条件始终成立，所以一定回源；盘后和非交易日不回源。
    """
    return cache.minute(code, period, n, force)


@mcp.tool()
def get_day_bars_cached(code: str, date: str, period: str = "5",
                        fetch_n: int = 3000) -> dict:
    """
    指定日期的全天分钟线（缓存版）—— **这个才是真•缓存**，历史日期二次查秒回。

    拒绝当日和未来日期（当日盘中未结算，未来日期是时钟错误）。
    首次 miss 时拉 3000 根顺带缓存约 63 个交易日（5 分钟周期），后续查这段全命中。

    非交易日（周末/节假日）返回 数据状态: 该日期无分钟线，不是报错。
    """
    return cache.day_bars(code, date, period, fetch_n)


# ========== akshare 补充源 ==========

@mcp.tool()
def get_dept_rank(period: str = "近一月", limit: int = 30,
                  min_times: int = 5) -> dict:
    """
    营业部排行 + 上榜后 1/2/3/5/10 日涨幅与上涨概率。

    这是我自己四个模块拿不到的：get_lhb_dept 只能给「某只票的上榜席位」
    和该席位 3 日胜率，拿不到「全市场营业部按胜率排」。跟龙虎榜席位配合用：
    先 get_lhb_dept 看哪个席位买了，再来这里查那个席位的历史胜率。

    period: 近一月 / 近三月 / 近六月 / 近一年
    min_times: 买入次数低于此数的席位剔掉（上榜 1 次赢 1 次是 100% 那是噪声）

    ⚠️ 上涨概率是历史统计不是预测；席位换手、营业部改名会让统计失真。
    """
    return ak.dept_rank(period, limit, min_times)


@mcp.tool()
def get_earnings_forecast(period: str = "", limit: int = 30,
                          kind: str = "") -> dict:
    """
    业绩预告。短线用途：业绩预增是涨停归因里的高频标签，
    「中报预增 + 题材」是常见的涨停理由组合。

    period: 报告期 YYYYMMDD（季末 0331/0630/0930/1231），传空自动取最近季末。
    kind: 按预告类型过滤，如「预增」「预减」「略增」「首亏」，传空不过滤。

    ⚠️ 预告是公司披露的预计值，不是已审计业绩。
    """
    return ak.earnings_forecast(period, limit, kind)


@mcp.tool()
def get_suspension(date: str = "", limit: int = 50) -> dict:
    """
    停复牌。盘前扫一眼，避免把停牌股算进池子里。
    date 传空取今天，格式 YYYYMMDD。
    """
    return ak.suspension(date, limit)


@mcp.tool()
def get_bonus_plan(period: str = "", limit: int = 30,
                   min_ratio: float = 0) -> dict:
    """
    分红送配 / 送转比例排行。

    period: 报告期 YYYYMMDD（季末），传空取最近季末。
    min_ratio: 送转总比例下限（单位 股/10股），0 表示只要有送转方案就算。

    ⚠️ 不要写死「≥5 算高送转」这个阈值。实测历年上限：
        2017年报 最大 36.00，2025年报 最大 4.90（402 个方案无一 ≥5）。
    看当期排行的相对高低，不看绝对数字。
    """
    return ak.bonus_plan(period, limit, min_ratio)


# ==================== 以下为开盘啦数据源（live_market_kaipanla）====================
# 开盘啦 app 数据接口，覆盖：市场情绪、涨停板块、个股分时、龙虎榜等。
# 29/35 接口可用（2026-08-04 测试）。部分接口返回 1020 错误时自动降级。


@mcp.tool()
def kpl_market_sentiment(date: str = "") -> dict:
    """
    开盘啦 - 市场情绪指标（涨停/跌停/涨跌家数）。

    参数：
        date: 日期 YYYY-MM-DD，空则今天

    当日走实时端点 MoodNumCount，历史日期走历史端点 HisZhangFuDetail。
    历史端点在当日会**每个字段填 0**，本工具已避开这条路，并在历史库
    确实无该日数据时明确返回「数据状态」而不是 0。

    实际涨停(去一字板)只有历史日期才有。
    """
    return kpl.market_sentiment(date)


@mcp.tool()
def kpl_sentiment_history(n: int = 60, anchor: str = "") -> dict:
    """
    开盘啦 - 市场情绪历史序列（**分位数判断的底座**，一次调用约 250 个交易日）。

    参数：
        n:      返回最近 n 个交易日的明细
        anchor: 锚定日期 YYYY-MM-DD，均值与分位数从这天往前算。空则取序列最新一行。

    返回：
        基准日、基准日数据、近5日均值、近10日均值、近10日分位数、近60日分位数、明细序列。
        指标含 涨停数、跌停数、曾涨停、炸板数、炸板率%、封板成功率%。
        均值窗口不含基准日自身；分位数窗口含自身。

    用途：南京路彼岸体系要求**相对阈值**（与近5/10日均值对比、看分位数位置），
    不看绝对值。>80分位 = 情绪高点区域，<20分位 = 情绪低点区域。

    曾涨停/收盘封板/封板成功率由 炸板数 与 炸板率 反算，公式在 250 个交易日上
    验证过（244 天整除；08-03 反算 曾涨停91/收盘封板75/成功率82.42% 与同花顺官方逐字相同）。

    ⚠️ **盘中调用时序列最新一行是上一交易日收盘**，开盘啦该端点当日盘中不入库。
       anchor 传了今天而序列里没有今天，返回值里会有「锚定状态」说明这件事，
       不会静默把上一交易日当今天。当日复盘请盘后再调。

    ⚠️ 不含连板数与市场高度（开盘啦原始字段不可信），那两项用 get_ladder。
    """
    return kpl.sentiment_history(n, anchor)


@mcp.tool()
def kpl_realtime_market_mood() -> dict:
    """
    开盘啦 - 实时市场情绪。

    返回：当前市场情绪温度、赚钱效应等实时指标。
    """
    return kpl.realtime_market_mood()


@mcp.tool()
def kpl_consecutive_limit_up(date: str = "") -> dict:
    """
    开盘啦 - 连板梯队（口径不可信，市场高度请用 get_ladder）。

    参数：
        date: 日期 YYYY-MM-DD，空则今天

    ⚠️ 实测两处矛盾：当日返回 max_consecutive=0（真实最高板 7）；
       历史 07-31 与 07-30 都返回 5，而同花顺是 9 板与 8 板（爱丽家居连续），
       同源的 kpl_market_limit_up_ladder 同两天却返回 10 板。

    返回 0 时本工具明确报缺数，不冒充真实值。
    """
    return kpl.consecutive_limit_up(date)


@mcp.tool()
def kpl_limit_up_ladder(date: str = "") -> dict:
    """
    开盘啦 - 涨停天梯（一板/二板/三板/高度板 + 连板率 + 破板率 + 市场评价）。

    参数：
        date: 日期 YYYY-MM-DD，空则今天

    ⚠️ 当日盘中该端点不入库（返回「数据状态」），要盘后或查历史日期。

    该端点的「今日涨停破板率」与 kpl_sentiment_history 的炸板率逐位相同（已验证 5 天），
    且 一板+二板+三板+高度板 == 反算的收盘封板数（6/6 天精确相等）。

    注意「高度板」是它自有口径，与同花顺最高板不是一回事（06-30 给 0，07-03 给 1）。
    市场高度请用 get_ladder。
    """
    return kpl.limit_up_ladder(date)


@mcp.tool()
def kpl_broken_limit_up(date: str = "") -> dict:
    """
    开盘啦 - 历史炸板股（曾涨停但收盘没封住）。

    参数：
        date: 日期 YYYY-MM-DD，空则今天

    ⚠️ 当日盘中该端点返回空。空**不代表当天没炸板股**，本工具明确区分
       「无数据」和「确实 0 只」，不让空列表冒充真实结果。
       当日炸板请用 get_zb_pool（东财）。
    """
    return kpl.broken_limit_up(date)


@mcp.tool()
def kpl_sector_ranking(date: str = "") -> dict:
    """
    开盘啦 - 板块排行。

    参数：
        date: 日期 YYYY-MM-DD，空则今天

    返回：板块涨跌幅排行，含成交额、主力资金。
    对标 get_sector_flow，数据维度可能不同。
    """
    return kpl.sector_ranking(date)


@mcp.tool()
def kpl_sector_intraday(sector_code: str, date: str = "") -> dict:
    """
    开盘啦 - 板块分时数据。

    参数：
        sector_code: 板块代码，如 801346
        date: 日期 YYYY-MM-DD，空则今天

    返回：开盘、收盘、最高、最低、分时明细。
    可查历史板块分时（现有数据源不提供）。
    """
    return kpl.sector_intraday(sector_code, date)


@mcp.tool()
def kpl_stock_intraday(stock_code: str, date: str = "") -> dict:
    """
    开盘啦 - 个股分时数据（历史日期用，当日不可用）。

    参数：
        stock_code: 股票代码，如 000001
        date: 日期 YYYY-MM-DD，空则今天

    返回：每分钟价格、均价、成交量、主力净流入。比 get_intraday 多主力资金维度。

    ⚠️ **当日不入库**：实测 08-05 盘中报 errcode 1020，08-04 正常返回 241 个分时点。
       六种代码写法结果一致，与代码格式无关。盘中分时请用 get_intraday（东财/腾讯）。
    """
    return kpl.stock_intraday(stock_code, date)


@mcp.tool()
def kpl_stock_big_order_intraday(stock_code: str) -> dict:
    """
    开盘啦 - 个股大单分时（新增数据），盘中可用。

    参数：
        stock_code: 股票代码

    返回：每分钟大单净额/买卖额/笔数明细 + 全天合计。
    **现有数据源不提供**，用于盯盘时看主力动向。

    ⚠️ 端点自带的三个 total 字段恒为 0（实测明细有 73 行非零、汇总却是 0），
       所以合计由明细逐行求和得出。已用恒等式核对：
       买 160,862,693 + 卖 -335,404,464 = 净额 -174,541,771（000001，08-05 半日）。
    ⚠️ 明细里 unknown1~unknown7 是未解出含义的原始列，原样保留不做解释。
    """
    return kpl.stock_big_order_intraday(stock_code)


@mcp.tool()
def kpl_stock_call_auction(stock_code: str) -> dict:
    """
    开盘啦 - 个股集合竞价（新增数据）。

    参数：
        stock_code: 股票代码

    返回：9:15-9:25 竞价时段的逐笔数据。**现有数据源不提供**，开盘前抢筹信号。

    ⚠️ 该接口内部打东财 push2 的逐笔明细，而东财只保留当日 tick，
       9:25 后竞价那 10 分钟会被盘中成交挤出窗口 —— **盘后查基本都是空的**。
       要用必须在 9:25-9:30 之间调，盘后无替代源。
    """
    return kpl.stock_call_auction(stock_code)


@mcp.tool()
def kpl_sector_bidding_anomaly(sector_code: str) -> dict:
    """
    开盘啦 - 板块集合竞价异动（新增数据）。

    参数：
        sector_code: 板块代码

    返回：9:15-9:25 竞价阶段的异动股票。**现有数据源不提供**，板块开盘强度信号。

    ⚠️ 实测 errcode 1130，且竞价数据只在 9:25 后当日有效，盘后必然为空。
       要用这个接口须在 9:25-9:30 之间调。
    """
    return kpl.sector_bidding_anomaly(sector_code)


@mcp.tool()
def kpl_longhubang_list() -> dict:
    """
    开盘啦 - 龙虎榜股票列表。对标 get_lhb，可互为备份。

    返回：上榜股票、上榜原因、买卖金额。

    ⚠️ **龙虎榜盘后才发布**。盘中调用返回空列表，空列表不等于
       「今天没人上榜」，本工具会明确报「尚未发布」并给出建议日期。
       带上榜后 1/2/5/10 日涨跌幅的请用 get_lhb（东财）。

    发布时点实测（2026-08-05 两轮夹出的区间，非精确时刻）：
       13:00 前为空，17:56 已有 66 只。文档旧版写的「约 18:00」偏保守。
    """
    return kpl.longhubang_list()


@mcp.tool()
def kpl_longhubang_detail(stock_code: str) -> dict:
    """
    开盘啦 - 龙虎榜个股明细。对标 get_lhb_dept。

    参数：
        stock_code: 股票代码

    返回：买入/卖出席位、金额明细。

    ⚠️ 该票当天没上榜时端点返回**空壳**而不报错（stock_name 为空串、
       buy_sell_data 为空、金额字段却是 0）。本工具以有无席位判定，
       没席位就报缺数，不让 0 看起来像「上榜净买 0 元」。
    ⚠️ 端点自带的 on_time_list 是该票**历史**上榜日期，当天没上榜也有值，
       它有内容不代表今天上了榜。
    """
    return kpl.longhubang_detail(stock_code)


@mcp.tool()
def kpl_new_high(date: str = "") -> dict:
    """
    开盘啦 - 百日新高（当日新增创百日新高的家数）。**现有数据源不提供**，突破信号。

    参数：
        date: 日期 YYYY-MM-DD，空则今天

    实测可回溯 360 个交易日（2025-02-13 起），08-04 得 12 家、07-30 得 15 家。

    ⚠️ 当日盘中不入库，最新一行是上一交易日。
    ⚠️ 查不到时区分「晚于最新可用日」「早于最早可用日」「范围内不存在(非交易日)」
       三种情况并报缺数，**绝不返回 0** —— 0 和「没这天」混在一起会让复盘误判成冰点。
    """
    return kpl.new_high(date)


@mcp.tool()
def kpl_index_intraday(index_code: str) -> dict:
    """
    开盘啦 - 指数分时数据。

    参数：
        index_code: 指数代码，**必须带市场前缀且大写**
                    SH000001(上证) / SZ399001(深证) / SZ399006(创业板)

    返回：每分钟价格、成交量、涨跌标志（实测返回 121 个分时点）。

    ⚠️ 该端点区分大小写：SH000001 正常，sh000001 报 errcode 1017。
       本工具会自动转大写，两种写法都能用。
    ⚠️ 别传裸代码：`000001` 不报错但返回的是**平安银行**，不是上证指数。
       缺前缀时本工具直接拒绝，不让个股数据冒充指数。
    """
    return kpl.index_intraday(index_code)


@mcp.tool()
def kpl_rise_fall_analysis() -> dict:
    """
    开盘啦 - 涨跌分析原始返回（字段标签不可信）。

    ⚠️ crawler 给这个接口的字段名是错的：它把 raw_data[3] 当炸板数、
       raw_data[5] 当「昨日涨停今表现」，实测 08-04 得到 炸板数=0 而
       炸板率=9.80%，自相矛盾。

    要情绪历史请用 kpl_sentiment_history（已按验证过的字段序解析）。
    """
    return kpl.rise_fall_analysis()


@mcp.tool()
def kpl_market_index() -> dict:
    """
    开盘啦 - 主要指数行情。

    ⚠️ 实测该端点返回空 DataFrame（2026-08-05 盘中，crawler 无任何错误输出），
       当前拿不到数据，本工具会明确报缺数而不返回空列表冒充「有数据」。
       指数点位请用 get_market_temperature（东财，含五大指数）。
    """
    return kpl.market_index()


@mcp.tool()
def kpl_index_list() -> dict:
    """
    开盘啦 - 所有可查询指数。

    返回：开盘啦支持的全部指数代码和名称列表。
    用于查询 kpl_index_intraday 的可用代码。
    """
    return kpl.index_list()


@mcp.tool()
def kpl_market_limit_up_ladder(date: str = "") -> dict:
    """
    开盘啦 - 市场涨停天梯（快速版）。

    参数：
        date: 日期 YYYY-MM-DD，空则今天（当日走实时，历史日期口径不可信）

    当日实时值可用：08-04 层级 {7,4,3,2,1}，最高板 7（传智教育），与同花顺一致。

    ⚠️ 历史日期该端点最高板与同花顺不一致（07-31 它给 10，同花顺给 9），
       历史市场高度请用 get_ladder。
    """
    return kpl.market_limit_up_ladder(date)


@mcp.tool()
def kpl_actual_limit_up_down() -> dict:
    """
    开盘啦 - 实际涨跌停（去一字板）。

    ⚠️ 实测该端点在交易日返回全 0（08-04 四个字段都是 0，真实涨停 138 家）。
       返回全 0 时本工具明确报缺数，不冒充真实值。
       要实际涨停(去一字板)请用 kpl_market_sentiment 查历史日期。
    """
    return kpl.actual_limit_up_down()


@mcp.tool()
def kpl_sector_limit_up_ladder(sector_code: str) -> dict:
    """
    开盘啦 - 板块涨停天梯（板块内部的涨停梯队分布）。

    参数：
        sector_code: 板块代码

    ⚠️ 实测拿不到数据：801346 与板块排行里的真实代码 801660 都返回 errcode 1130。
       板块内涨停成分请用 get_theme_top(with_stocks=True)（同花顺）。
    """
    return kpl.sector_limit_up_ladder(sector_code)


@mcp.tool()
def kpl_multiple_sectors_strength(sector_codes: str) -> dict:
    """
    开盘啦 - 批量查询板块强度。

    参数：
        sector_codes: 板块代码列表，逗号分隔，如 "801346,801123"

    返回：多个板块的强度指标对比。
    用于板块轮动分析。
    """
    codes_list = [c.strip() for c in sector_codes.split(",")]
    return kpl.multiple_sectors_strength(codes_list)


@mcp.tool()
def kpl_sector_constituent_stocks(sector_code: str) -> dict:
    """
    开盘啦 - 板块成分股（仅代码和名称）。

    参数：
        sector_code: 板块代码

    ⚠️ 实测拿不到数据：errcode 1020，801346 与真实代码 801660 都取不到。
       成分股请用 get_stock_board_cons（东财，行业/概念都支持）。
    """
    return kpl.sector_constituent_stocks(sector_code)


@mcp.tool()
def kpl_sector_all_stocks(sector_code: str) -> dict:
    """
    开盘啦 - 板块所有股票含行情。

    参数：
        sector_code: 板块代码

    ⚠️ 实测拿不到数据：errcode 1020，同 kpl_sector_constituent_stocks。
       成分股+行情请用 get_stock_board_cons（东财）。
    """
    return kpl.sector_all_stocks(sector_code)


@mcp.tool()
def kpl_sharp_withdrawal(date: str = "") -> dict:
    """
    开盘啦 - 大幅回撤（冲高回落），可回溯历史日期。

    参数：
        date: 日期 YYYY-MM-DD，空则今天

    ⚠️ 走历史端点，**当日与上一交易日都可能还没入库**（实测 08-05、08-04 都空，
       08-03 得 3 只、07-30 得 3 只）。盘中请用 kpl_realtime_sharp_withdrawal，
       单只票的冲高回落量化用 get_intraday_shape。

    ⚠️ 端点自带的「总数」与实际明细条数不一致（08-03 说 9 只只给 3 条），
       本工具两个数都列出并标警告，不替它圆。
    """
    return kpl.sharp_withdrawal(date)


@mcp.tool()
def kpl_realtime_sharp_withdrawal() -> dict:
    """
    开盘啦 - 实时大幅回撤。

    返回：实时更新的冲高回落股票。
    比 kpl_sharp_withdrawal 更新频率更高。
    """
    return kpl.realtime_sharp_withdrawal()


@mcp.tool()
def kpl_plate_news(sector_code: str) -> dict:
    """
    开盘啦 - 板块新闻。

    参数：
        sector_code: 板块代码

    返回：指定板块的最新新闻资讯。
    用于跟踪板块题材逻辑。
    """
    return kpl.plate_news(sector_code)


# ==================== RPS 三线红名单（通达信导出 → 统计 / 对比） ====================

@mcp.tool()
def rps_pool_stat(date: str = "", kind: str = "三线红", top: int = 40) -> dict:
    """
    RPS 三线红名单的趋势强度榜 —— **整份名单**，不只是一个池数。

    参数：
        date: 日期 YYYY-MM-DD 或 YYYYMMDD，空则今天
        kind: 三线红 / 一线红 / 板块三线红
        top:  只返回前 N 只（0=全部）。默认 40，按出现次数降序。

    每只票带：股票名 / 代码 / 行业 / 加入日 / 入连续天数 / 入总天数 / 入池次数 / 距60天第一次，
    另给行业分布 TOP。这是南京路彼岸《情绪周期下"如何选股"》第 24/25 页那张表的复现，
    第 26 页作者交代的用法是「按出现次数做统计，作为趋势强度的一种形式，每日复盘」。

    名单有两条来路，都写进同一个目录，下游不区分：
      本机算（推荐）  `rps_local_calc` 直读通达信 extdata + lday，不用写公式不用导出
      手工导出        通达信选股 → 文件丢收件箱 → `rps_pool_scan`

    没有该日名单就明确报缺数并给补数命令，不返回估算值。

    ⚠️ 统计窗口是「已登记日」个数，不是自然交易日 —— 中间漏导的日子不算断档。
       本机算那条路可以用 `rps_local_backfill` 一次回补几十天，不用等时间攒。
    """
    r = rps_pool.stat(date or None, kind)
    if top and isinstance(r.get("趋势强度榜"), list) and len(r["趋势强度榜"]) > top:
        r["趋势强度榜"] = r["趋势强度榜"][:top]
        r["注"] = f"只显示前 {top} 只（共 {r['池数']} 只），全量见 {rps_pool.daily_path(r['日期'], kind)}"
    return r


@mcp.tool()
def rps_pool_diff(date: str = "", kind: str = "三线红", base: str = "") -> dict:
    """
    RPS 三线红名单与上一登记日的对比 —— 逐只列出新进榜 / 掉出榜 / 连续在榜。

    参数：
        date: 日期，空则今天
        kind: 三线红 / 一线红 / 板块三线红
        base: 指定对比基准日，空则自动取上一个已登记日

    返回：池数变化、扩容还是缩容、名单换手率、新进/掉出明细（带行业）、行业层面增减。

    判读：缩容 + 某行业整片掉出 = 该方向退潮；扩容且集中在一个行业 = 主线在扩散。
    《静待风起》里作者就是这么用的：池子 178→101 四天缩容，光通信全灭，
    电子特气+小金属逆势留存，据此判定情绪从高潮转恐慌。
    """
    return rps_pool.diff(date or None, kind, base or None)


@mcp.tool()
def rps_pool_scan() -> dict:
    """
    扫收件箱，把通达信导出的选股结果解析入库，并同步每日计数流水。

    用法：通达信跑完三线红选股 → 导出文件（txt/csv/xlsx 都行）丢进
    `_知识库系统\\data\\rps_pool_inbox\\` → 调这个工具。

    文件名里带日期和池子类型最好（如 `三线红20260805.txt`），
    认不出日期就用文件修改时间，认不出类型按「三线红」算。解析完原件归档不删。

    池数由名单行数自动派生，不需要手填数字。
    名称对不上会点名报出来（最常见原因是公司改名，如 韦尔股份→豪威集团），
    不静默跳过 —— 静默跳过会让池数偏少。

    ℹ️ 本机装了通达信扩展数据，所以**通常不需要这条路** —— 用 `rps_local_calc`
       直接算，不用写选股公式也不用导出。这个工具留给「你就想用通达信自己选股」的场合，
       两条路写进同一个目录，下游统计不区分。
    """
    return rps_pool.scan()


@mcp.tool()
def rps_local_calc(date: str = "", preset: str = "fine", save: bool = False) -> dict:
    """
    **直接在本机算三线红名单** —— 不用写通达信选股公式，不用导出文件。

    参数：
        date:   日期 YYYY-MM-DD 或 YYYYMMDD，空则取最近可算日
        preset: fine=精细版 120>93 250>95 50>90（作者第28页修正版，默认）
                base=基线 三线均>90 / oneil=欧奈尔 三线均>87
        save:   True 则写进每日名单目录并同步计数流水（幂等，同日重复跑是覆盖）

    数据来自通达信本地文件，两样都在本机：
        RPS   `T0002\\extdata\\extdata_{1,2,3}` —— 编号 1=120日 2=250日 3=50日
        日线  `vipdoc\\{sh,sz,bj}\\lday` —— 算 H/HHV(HIGH,150)>0.85

    每只票带 代码/名称/行业/RPS三值/H-HHV比值，另给「过三线RPS」与「被HHV砍掉」的
    分层计数 —— 两个数拉开得越大，说明强势票离自己高点越远。

    ⚠️ 扩展数据的槽位名显示为 ROC，但存的是**排名**（值域 0-1000 = RPS×10）。
       已验证：某日横截面 5459 个值分 10 个百等份桶得 545/546/…/545（理论 545.9），
       只有排名会这么均匀；且 extdata.info 里每条的第二个名字字段都是 `RPS_BASE`。

    ⚠️ 板块三线红算不了 —— 它要编号 7-11（BKRPS5/10/15/20/50），本机没登记这几个槽位。
       作者第 31 页自述「我没有特别挖掘这方面」，可跳过。
    """
    if save:
        return rps_local.save(date=date or None, preset=preset)
    r = rps_local.calc(date=date or None, preset=preset)
    if len(r["stocks"]) > 60:
        r["注"] = f"名单 {len(r['stocks'])} 只，只返回前 60；全量跑 save=True 后看名单文件"
        r["stocks"] = r["stocks"][:60]
    return r


@mcp.tool()
def rps_local_backfill(days: int = 60, preset: str = "fine", overwrite: bool = False) -> dict:
    """
    回补最近 N 个交易日的三线红名单 —— 让跨日统计立刻能用。

    参数：
        days:      回补多少个可算日，默认 60（`rps_pool_stat` 的统计窗口就是 60）
        preset:    fine / base / oneil，见 `rps_local_calc`
        overwrite: False（默认）已有的日期跳过；True 全部重算覆盖

    为什么要回补：作者第 24/25 页那张表的「入连续天数 / 入总天数 / 入池次数 /
    距60天第一次」全都要跨日数据才算得出，只有今天一份名单等于四列全是 1。
    本机扩展数据有 4235 个交易日（2005-08-30 起），不用等几周攒。

    实测 60 天约 32 秒（三个槽位约 400 MB 只读一次，日线按票缓存）。
    """
    return rps_local.backfill(days=days, preset=preset, overwrite=overwrite)


@mcp.tool()
def rps_local_dates() -> dict:
    """
    本机能算哪些日期的三线红，以及扩展数据/日线的更新状态。

    返回：可算天数、最早/最近可算日、最近10天、各周期各自的天数、
    三个扩展数据文件的更新时间、各市场票数、**日线对齐情况**。

    先调这个的两个场合：
      1. 想算某个历史日期，先确认在不在范围内
      2. `rps_local_calc` 算出来的票异常少 —— 看「日线对齐」，
         扩展数据更新了但日线没跟上时 H/HHV 会大面积算不出来，
         修法是在通达信里做一次盘后数据下载（日线）
    """
    return rps_local.dates()


# ==================== 南京路彼岸 7 指标复盘 ====================

def _pick(*vals):
    """取第一个非 None 且非空的值"""
    for v in vals:
        if v is not None and v != "":
            return v
    return None


def _try(fn, *a, **kw):
    """跑一个取数，失败返回 (None, 错误字符串)，绝不返回假数据"""
    try:
        return fn(*a, **kw), None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:100]}"


@mcp.tool()
def get_sentiment_7indicators(date: str = "") -> dict:
    """
    南京路彼岸 7 指标复盘 —— **每日复盘的第一个工具**，一次取齐 7 指标 + 相对阈值。

    参数：
        date: 日期 YYYY-MM-DD 或 YYYYMMDD，空则今天

    7 指标（对应 nanjinglu-bian-perspective 模型2「数据量化情绪系统」）：
        1 涨停数    2 跌停数    3 连板数    4 封板率
        5 涨跌家数比 6 市场高度  7 RPS三线红池数（读名单流水，可本机直接算，见下）

    每项都给出：今日值、来源、近5日均值、近10日均值、近10日分位数、近60日分位数。
    体系要求用相对阈值而非绝对值：>80分位 = 情绪高点区域，<20分位 = 情绪低点区域。

    每个指标都带「数据源」字段，多源交叉校验后的分歧会列在「源间分歧」里。
    取不到就写「缺数」并说明原因，绝不返回 0 或估算值冒充真实盘面。

    ⚠️ 第 7 项的「101只」不是指标名：101 是 2026-07-03 那天 **RPS 三线红选股池的
       股票只数**，每天刷新（原文两个月序列 119起步→138→175→187峰值→101冰点）。
       口径：50/120/250 日 RPS 同时高于自设阈值（作者实操 90，或 120日>93、
       250日>95、50日>90）且接近区间新高，在通达信里靠陶博士 EXTRS 扩展数据 +
       EXTDATA_USER 公式选出。

    ⚠️ 第 7 项不走行情接口，读本地名单流水 —— 网络数据源都不给 RPS
       （东财 clist 实测只有 f24=60日涨跌幅、f25=年初至今，没有 120/250 日字段）。
       但**本机装了通达信扩展数据，所以能直接算**：`rps_local_calc(save=True)`
       读 `T0002\\extdata` 的 RPS + `vipdoc\\*\\lday` 的日线，不用写选股公式也不用导出。
       一次补几十天用 `rps_local_backfill(days=60)`。
       该日没有登记就返回「缺数」+ 补数命令，绝不估算。

    ⚠️ 第 7 项的分位数窗口是「已登记日」个数，前 6 项走开盘啦序列窗口，
       两者不是同一个窗口，别把「近10日」混为一谈。
    """
    d = date or ""
    # 规范成 YYYY-MM-DD，用于和各源返回的数据日期对齐
    _raw = d.replace("-", "")
    if len(_raw) == 8 and _raw.isdigit():
        d_norm = f"{_raw[:4]}-{_raw[4:6]}-{_raw[6:]}"
    else:
        d_norm = lm.datetime.now().strftime("%Y-%m-%d")

    out = {
        "取数时间": lm.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "查询日期": d_norm + ("（今天）" if not d else ""),
        "体系": "南京路彼岸 模型2 数据量化情绪系统",
        "阈值口径": "相对阈值：与近5/10日均值对比 + 看分位数位置；>80分位=情绪高点，<20分位=情绪低点",
    }

    # ---------- 底座：情绪历史序列（提供均值与分位数） ----------
    # 锚定到查询日，否则盘中会把上一交易日的值当成今天，跟东财/同花顺的盘中值混比
    hist, hist_err = _try(kpl.sentiment_history, 250, d_norm)
    if hist_err or (isinstance(hist, dict) and "错误" in hist):
        out["历史序列"] = {"缺数": hist_err or hist.get("错误"),
                          "影响": "无法给出均值与分位数，只能给当日绝对值"}
        hist = {}
    elif hist.get("数据状态"):
        out["历史序列"] = {"缺数": hist["数据状态"], "影响": "无法给出均值与分位数"}
        hist = {}
    else:
        out["历史序列"] = {"样本交易日数": hist.get("样本交易日数"),
                          "基准日": hist.get("基准日"),
                          "数据源": hist.get("数据源")}
        if hist.get("锚定状态"):
            out["历史序列"]["锚定状态"] = hist["锚定状态"]

    h_today = hist.get("基准日数据") or {}
    # 序列基准日与查询日不是同一天时，开盘啦的值不能和东财/同花顺横向比
    h_stale = bool(hist) and hist.get("基准日") != d_norm
    h_base = hist.get("基准日")
    m5 = hist.get("近5日均值") or {}
    m10 = hist.get("近10日均值") or {}
    p10 = hist.get("近10日分位数") or {}
    p60 = hist.get("近60日分位数") or {}
    wins = hist.get("序列窗口") or {}
    detail = hist.get("明细") or []

    def _rank(seq, val):
        """val 在 seq 里的分位数（中位秩），seq 为空或 val 为 None 时返回 None"""
        xs = [x for x in (seq or []) if x is not None]
        if not xs or val is None:
            return None
        below = sum(1 for x in xs if x < val)
        equal = sum(1 for x in xs if x == val)
        return round((below + equal / 2.0) / len(xs) * 100, 1)

    def band(key, val):
        """
        给一个指标配上均值与分位数。

        分位数一律用**实际采用的当日值**在历史窗口里现算，不套用开盘啦序列
        基准日的排名。两个原因：
          1. 盘中查今天时基准日是上一交易日，套用等于把昨天的排名当成今天的
          2. 即使同一天，今日值多来自东财/同花顺，与开盘啦反算值可能差 1-2
             （08-04：同花顺收盘封板 137 / 开盘啦反算 138），排名要跟着实际值走
        """
        r = {"今日": val}
        if key not in m5:
            return r
        r["近5日均值"] = m5.get(key)
        r["近10日均值"] = m10.get(key)
        w10 = (wins.get("近10日") or {}).get(key)
        w60 = (wins.get("近60日") or {}).get(key)
        if w10 or w60:
            r["近10日分位数"] = _rank(w10, val)
            r["近60日分位数"] = _rank(w60, val)
            r["分位数口径"] = f"用今日实际值 {val} 在截至 {h_base} 的窗口里现算"
        else:
            # 序列窗口缺失时退回基准日排名，并说清它属于哪一天
            r["近10日分位数"] = p10.get(key)
            r["近60日分位数"] = p60.get(key)
            r["分位数口径"] = f"⚠️无序列窗口，退回 {h_base} 自身的排名"
        return r

    # ---------- 权威源：东财温度 + 同花顺涨停统计 + 同花顺天梯 ----------
    is_today = (not d)
    temp, temp_err = _try(lm.market_temp) if is_today else (None, "仅当日可用")
    ths_zt, ths_err = _try(ths.zt_reason, d, 1)
    lad, lad_err = _try(ths.ladder, d)

    zt_stat = {}
    if isinstance(ths_zt, dict):
        zt_stat = (ths_zt.get("涨停统计") or {}).get("今日") or {}

    diffs = []

    # 开盘啦序列基准日 != 查询日 时，它的值属于另一天，不参与取值与横向比对
    kpl_tag = f"开盘啦({h_base})" if h_stale else "开盘啦"

    def kv(key):
        """取开盘啦基准日的某项；日期错位时返回 None（不拿别的日期充当今天）"""
        return None if h_stale else h_today.get(key)

    # ---------- 指标 1 涨停数 ----------
    ec_zt = (temp or {}).get("涨停数") if isinstance(temp, dict) else None
    ths_fb = zt_stat.get("收盘封板")
    kpl_fb = kv("收盘封板")
    zt_val = _pick(ec_zt, ths_fb, kpl_fb)
    out["1_涨停数"] = band("收盘封板", zt_val)
    out["1_涨停数"]["数据源"] = {"东财涨停池": ec_zt, "同花顺收盘封板": ths_fb,
                                kpl_tag + "反算收盘封板": h_today.get("收盘封板")}
    if zt_val is None:
        out["1_涨停数"]["缺数"] = f"三源都取不到（东财:{temp_err} 同花顺:{ths_err}）"
    vs = [v for v in (ec_zt, ths_fb, kpl_fb) if v is not None]
    if vs and max(vs) - min(vs) > 2:
        diffs.append(f"涨停数分歧：东财{ec_zt} / 同花顺{ths_fb} / 开盘啦{kpl_fb}")

    # ---------- 指标 2 跌停数 ----------
    ec_dt = (temp or {}).get("跌停数") if isinstance(temp, dict) else None
    kpl_dt = kv("跌停数")
    dt_val = _pick(ec_dt, kpl_dt)
    out["2_跌停数"] = band("跌停数", dt_val)
    out["2_跌停数"]["数据源"] = {"东财": ec_dt, kpl_tag: h_today.get("跌停数")}
    if dt_val is None:
        out["2_跌停数"]["缺数"] = "东财与开盘啦都取不到"
    if ec_dt is not None and kpl_dt is not None and abs(ec_dt - kpl_dt) > 2:
        diffs.append(f"跌停数分歧：东财{ec_dt} / 开盘啦{kpl_dt}")

    # ---------- 指标 3 连板数 ----------
    lb = (lad or {}).get("连板总数") if isinstance(lad, dict) else None
    out["3_连板数"] = {"今日": lb, "数据源": {"同花顺连板天梯": lb},
                      "口径": "2 板及以上的总只数"}
    if lb is None:
        out["3_连板数"]["缺数"] = f"同花顺天梯取不到（{lad_err}）"
    out["3_连板数"]["说明"] = ("开盘啦的连板字段不可信（当日 max=0；07-31 它给 5 或 10，"
                             "同花顺给 9），故不做交叉校验，只用同花顺")

    # ---------- 指标 4 封板率 ----------
    ths_rate = zt_stat.get("封板成功率%")
    kpl_rate = kv("封板成功率%")
    rate_val = _pick(ths_rate, kpl_rate)
    out["4_封板率"] = band("封板成功率%", rate_val)
    out["4_封板率"]["数据源"] = {"同花顺官方": ths_rate,
                               kpl_tag + "反算": h_today.get("封板成功率%")}
    out["4_封板率"]["口径"] = "收盘封板 / 曾涨停（同花顺官方口径）"
    if rate_val is None:
        out["4_封板率"]["缺数"] = f"同花顺与开盘啦都取不到（{ths_err}）"
    if ths_rate is not None and kpl_rate is not None and abs(ths_rate - kpl_rate) > 1:
        diffs.append(f"封板率分歧：同花顺{ths_rate}% / 开盘啦反算{kpl_rate}%")

    # ---------- 指标 5 涨跌家数比 ----------
    # 当日走东财温度；历史日期走开盘啦历史端点（它带上涨/下跌家数，已验证 08-03 = 4005/1466）
    up = (temp or {}).get("沪深上涨家数") if isinstance(temp, dict) else None
    down = (temp or {}).get("沪深下跌家数") if isinstance(temp, dict) else None
    src5 = "东财大盘温度"
    kpl_ms, kpl_ms_err = (None, None)
    if not (up and down):
        kpl_ms, kpl_ms_err = _try(kpl.market_sentiment, d_norm)
        if isinstance(kpl_ms, dict) and not kpl_ms.get("数据状态"):
            up = _pick(up, kpl_ms.get("上涨家数"))
            down = _pick(down, kpl_ms.get("下跌家数"))
            src5 = kpl_ms.get("数据源") or "开盘啦"
    ratio = round(up / down, 2) if (up and down) else None
    out["5_涨跌家数比"] = {"今日": ratio, "上涨家数": up, "下跌家数": down,
                          "数据源": src5, "口径": "上涨家数 / 下跌家数"}
    if ratio is None:
        why = (kpl_ms.get("数据状态") if isinstance(kpl_ms, dict) else None) \
            or kpl_ms_err or temp_err or "两源都未返回涨跌家数"
        out["5_涨跌家数比"]["缺数"] = f"东财与开盘啦历史端点都取不到（{why}）"

    # 历史日期顺带带出「实际涨停(去一字板)」，只有开盘啦历史端点有
    if isinstance(kpl_ms, dict) and kpl_ms.get("实际涨停") is not None:
        out["1_涨停数"]["实际涨停(去一字板)"] = kpl_ms.get("实际涨停")
        out["2_跌停数"]["实际跌停(去一字板)"] = kpl_ms.get("实际跌停")

    # ---------- 指标 6 市场高度 ----------
    height = (lad or {}).get("最高板") if isinstance(lad, dict) else None
    hstocks = (lad or {}).get("最高板个股") if isinstance(lad, dict) else None
    out["6_市场高度"] = {"今日": height, "最高板个股": hstocks,
                        "断层": (lad or {}).get("断层") if isinstance(lad, dict) else None,
                        "数据源": "同花顺连板天梯（可回溯任意历史日期）"}
    if height is None:
        out["6_市场高度"]["缺数"] = f"同花顺天梯取不到（{lad_err}）"
    if is_today:
        kl, kl_err = _try(kpl.market_limit_up_ladder)
        k_h = (kl or {}).get("最高板") if isinstance(kl, dict) else None
        out["6_市场高度"]["数据源交叉"] = {"同花顺": height, "开盘啦实时天梯": k_h}
        if k_h is not None and height is not None and k_h != height:
            diffs.append(f"市场高度分歧：同花顺{height}板 / 开盘啦{k_h}板（以同花顺为准）")

    # ---------- 指标 7 RPS三线红池数（读名单流水；名单可由 rps_local 本机算） ----------
    # 三线红要全市场每只票 50/120/250 日涨幅的百分位排名。东财 clist 批量接口实测
    # 只有 f24=60日涨跌幅、f25=年初至今，**没有 120/250 日字段**，其他源也都不给 RPS。
    # 所以改成读 rps_log.py 的手工流水：通达信选完股，把结果条数填进去。
    _rps_doc = {
        "是什么": ("每日 RPS 三线红选股池里的股票只数，每天刷新。"
                  "「101只」是 2026-07-03 那天的值，不是指标名，也不是固定数字。"),
        "口径": ("三线红 = 50/120/250 日 RPS 同时高于自设阈值"
                "（作者实操 90，或 120日>93、250日>95、50日>90），"
                "再叠加 H/HHV(HIGH,N)>0.85 接近区间新高。"
                "RPS 取值 0-99，是该周期涨幅在全市场的百分位排名。"
                "一线红是同一套公式把 AND 换成 OR（任一周期达标），门槛更高（示例 95）。"),
        "读法": ("看池子相对自身近期区间的扩容/缩容，不套绝对阈值 —— "
                "绝对数取决于你自设的 RPS 门槛，换阈值整条曲线就平移。"),
        "历史参照": ("《静待风起》2026-07-05 的两个月轨迹：119起步→138→175→187峰值→101冰点；"
                    "雪球逐日截图 06/25=187 06/26=184 06/29=179 06/30=166 "
                    "07/02=121 07/03=101（单日最大缩容 36 只）。"),
    }
    try:
        _rec = rps_log.get(d_norm)
        _rps_err = None
    except Exception as e:  # 流水文件被改坏时照实报，不静默当成没登记
        _rec, _rps_err = None, f"读流水失败 {type(e).__name__}: {e}"

    if _rec and _rec.get("三线红池数") is not None:
        _v = _rec["三线红池数"]
        _rows = rps_log.load_all()
        _prev = [r for r in _rows if r["date"] < d_norm and r["三线红池数"] is not None]
        _w10, _w60 = rps_log.window(d_norm, 10), rps_log.window(d_norm, 60)
        _lst0 = rps_pool.load_daily(d_norm, "三线红")
        _has_list = _lst0 is not None
        # 名单有两条来路，标签要说清是哪条 —— 本机算的写成"导出"会误导
        if _has_list and _lst0 and str(_lst0[0].get("match", "")).startswith("local_"):
            _src7 = "本机算（通达信 extdata + lday，rps_local.py）"
        elif _has_list:
            _src7 = "通达信选股导出的名单行数（自动派生）"
        else:
            _src7 = "手工填的条数（无名单文件）"
        r7 = {"今日": _v,
              "数据源": _src7 + f"，登记于 {_rec.get('登记时间')}",
              "阈值口径": _rec.get("阈值口径")}
        for _n, _k in ((5, "近5日均值"), (10, "近10日均值")):
            _s = [r["三线红池数"] for r in _prev][-_n:]
            r7[_k] = round(sum(_s) / len(_s), 1) if _s else None
        r7["近10日分位数"] = _rank(_w10, _v)
        r7["近60日分位数"] = _rank(_w60, _v)
        r7["分位数口径"] = (f"用今日 {_v} 只在三线红流水最近 {len(_w10)}/{len(_w60)} 个"
                          f"登记日里现算（均值窗口不含今日，分位数窗口含今日）。"
                          f"这个窗口是「已登记日」个数，与前 6 项走开盘啦序列的窗口不是同一个")
        if len(_w10) < 3:
            r7["⚠️样本不足"] = (f"流水里只有 {len(_w10)} 个登记日，分位数没有参考意义。"
                             f"这指标只看相对高低，攒到 10 天以上再用。")
        if _rec.get("一线红池数") is not None:
            r7["一线红池数"] = _rec["一线红池数"]
        if _rec.get("板块三线红池数") is not None:
            r7["板块三线红池数"] = _rec["板块三线红池数"]
        if _rec.get("备注"):
            r7["备注"] = _rec["备注"]
        # 前一登记日的环比，扩容/缩容才是这指标的核心读法
        if _prev:
            _p = _prev[-1]
            r7["环比上一登记日"] = (f"{_p['date']} {_p['三线红池数']}只 → {_v}只"
                               f"（{_v - _p['三线红池数']:+d}）")
        # 名单层：有当日名单就带上行业分布和日间进出，光看一个数看不出主线在哪
        _lst = rps_pool.load_daily(d_norm, "三线红")
        if _lst:
            _ind: dict = {}
            for _s in _lst:
                _k = _s.get("industry") or "未知"
                _ind[_k] = _ind.get(_k, 0) + 1
            r7["行业分布TOP5"] = [{"行业": k, "只数": v} for k, v in
                                sorted(_ind.items(), key=lambda kv: -kv[1])[:5]]
            _df = rps_pool.diff(d_norm, "三线红")
            if "缺数" not in _df:
                r7["日间进出"] = {"新进榜": _df["新进榜"]["只数"],
                              "掉出榜": _df["掉出榜"]["只数"],
                              "连续在榜": _df["连续在榜"]["只数"],
                              "对比基准日": _df["对比基准日"],
                              "行业增减TOP3": _df["行业增减"][:3]}
            r7["名单文件"] = rps_pool.daily_path(d_norm, "三线红")
            r7["看整份名单"] = ("rps_pool_stat 出趋势强度榜（按出现次数排，作者第26页的用法）；"
                            "rps_pool_diff 看逐只新进/掉出")
        else:
            r7["⚠️只有计数没有名单"] = (
                f"{d_norm} 有池数但没有名单文件，看不到是哪些票、哪个行业。"
                f"补法：rps_local_calc(date='{d_norm}', save=True) 本机直接算；"
                f"或把通达信导出丢进 {rps_pool.INBOX} 后跑 python rps_pool.py scan")
        r7.update(_rps_doc)
    else:
        _latest = None
        try:
            _latest = rps_log.get(None)
        except Exception:
            pass
        r7 = {"今日": None,
              "缺数": (_rps_err or f"{d_norm} 还没有登记名单"),
              "怎么补上": (f"**本机直接算**：rps_local_calc(date='{d_norm}', save=True) —— "
                        f"读通达信 extdata + lday，不用写选股公式也不用导出。"
                        f"要一次补几十天用 rps_local_backfill(days=60)。"
                        f"命令行等价：python rps_local.py save --date {d_norm}"),
              "另一条路（想用通达信自己选股）": (
                  f"通达信跑三线红选股 → 导出文件丢进 {rps_pool.INBOX} → "
                  f"python rps_pool.py scan。配置步骤见 "
                  f"_知识库系统\\data\\RPS数据获取配置.md"),
              "只想填个数": f"python rps_log.py add <条数> --date {d_norm}（没有名单就只有计数）",
              "流水文件": rps_log.CSV_PATH,
              "已登记名单日期": rps_pool.list_dates("三线红")[-10:]}
        if _latest:
            r7["流水里最新一条"] = f"{_latest['date']} {_latest['三线红池数']}只"
        r7.update(_rps_doc)
    out["7_RPS三线红池数"] = r7

    # ---------- 汇总 ----------
    stage = (ths_zt or {}).get("交易阶段") if isinstance(ths_zt, dict) else None
    if stage:
        out["交易阶段"] = stage
    if h_stale:
        out["⚠️日期错位"] = (
            f"开盘啦情绪序列最新只到 {h_base}，查询日是 {d_norm}（该端点当日盘中不入库）。"
            f"均值与分位数的**对比窗口**截至 {h_base}，但分位数是用查询日实际值现算的，"
            f"不是 {h_base} 的排名。1/2/4 项的开盘啦列已置空，不与东财/同花顺横向比。"
            + ("盘中值随时在变，收盘前的分位数只作参考。"
               if stage == "交易中" else "")
        )
    out["源间分歧"] = diffs or "无（各源在容差内一致）"
    out["缺数项"] = [k for k, v in out.items()
                    if isinstance(v, dict) and ("缺数" in v
                                                or "待定义" in str(v.get("状态", ""))
                                                or "无法取数" in str(v.get("状态", "")))]
    if detail:
        out["近10日明细"] = detail[:10]
    out["用法"] = ("先看分位数定情绪周期位置（混沌/破局/主升/补涨/退潮），"
                  "再按 SKILL 里各阶段的指标权重加权判断。不要只看绝对值。")
    return out


if __name__ == "__main__":
    mcp.run()
