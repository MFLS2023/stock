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


if __name__ == "__main__":
    mcp.run()
