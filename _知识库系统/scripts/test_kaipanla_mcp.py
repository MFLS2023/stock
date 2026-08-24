#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试已暴露为MCP工具的开盘啦接口"""
import io
import sys
import time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

import live_market_kaipanla as kpl

def test_mcp_tools():
    """测试所有MCP工具对应的接口"""
    tests = [
        # 市场整体 (6个)
        ("市场情绪", lambda: kpl.market_sentiment()),
        ("实时市场情绪", lambda: kpl.realtime_market_mood()),
        ("涨跌分析", lambda: kpl.rise_fall_analysis()),
        ("市场指数", lambda: kpl.market_index()),
        ("指数列表", lambda: kpl.index_list()),
        ("指数分时", lambda: kpl.index_intraday("sh000001")),

        # 涨停板块 (6个)
        ("连板梯队", lambda: kpl.consecutive_limit_up()),
        ("涨停天梯", lambda: kpl.limit_up_ladder()),
        ("市场涨停天梯", lambda: kpl.market_limit_up_ladder()),
        ("板块涨停天梯", lambda: kpl.sector_limit_up_ladder("801346")),
        ("实际涨跌停", lambda: kpl.actual_limit_up_down()),
        ("历史炸板", lambda: kpl.broken_limit_up("2026-08-01")),

        # 板块分析 (6个)
        ("板块排行", lambda: kpl.sector_ranking()),
        ("批量板块强度", lambda: kpl.multiple_sectors_strength(["801346", "801001"])),
        ("板块分时", lambda: kpl.sector_intraday("801346")),
        ("板块成分股", lambda: kpl.sector_constituent_stocks("801346")),
        ("板块所有股票", lambda: kpl.sector_all_stocks("801346")),
        ("板块新闻", lambda: kpl.plate_news("801346")),

        # 个股数据 (3个)
        ("个股分时", lambda: kpl.stock_intraday("000001")),
        ("个股大单分时", lambda: kpl.stock_big_order_intraday("000001")),
        ("个股集合竞价", lambda: kpl.stock_call_auction("000001")),

        # 龙虎榜 (2个)
        ("龙虎榜列表", lambda: kpl.longhubang_list()),
        ("龙虎榜明细", lambda: kpl.longhubang_detail("000001")),

        # 其他 (4个)
        ("百日新高", lambda: kpl.new_high("2026-08-01")),
        ("大幅回撤", lambda: kpl.sharp_withdrawal()),
        ("实时大幅回撤", lambda: kpl.realtime_sharp_withdrawal()),
        ("板块竞价异动", lambda: kpl.sector_bidding_anomaly("801346")),
    ]

    print("=" * 70)
    print("开盘啦 MCP 工具接口测试")
    print("=" * 70)

    passed = 0
    failed = 0
    results = []

    for name, fn in tests:
        try:
            t0 = time.time()
            result = fn()
            cost = time.time() - t0

            # 检查是否是错误返回
            if isinstance(result, dict) and "错误" in result:
                failed += 1
                err = result["错误"][:50]
                results.append((name, "⚠", err))
                print(f"⚠ {name:18s} API错误: {err}")
            else:
                passed += 1
                # 判断数据大小
                if isinstance(result, dict):
                    size = f"{len(result)} 键"
                elif isinstance(result, list):
                    size = f"{len(result)} 项"
                else:
                    size = type(result).__name__

                results.append((name, "✓", size))
                print(f"✓ {name:18s} {size:15s} {cost:6.2f}s")

        except Exception as e:
            failed += 1
            err_msg = str(e)[:50]
            results.append((name, "✗", err_msg))
            print(f"✗ {name:18s} {err_msg}")

    print("=" * 70)
    total = passed + failed
    print(f"通过: {passed}/{total}")
    print(f"API错误: {sum(1 for r in results if r[1]=='⚠')}/{total}")
    print(f"代码错误: {sum(1 for r in results if r[1]=='✗')}/{total}")
    print()

    # 显示关键数据
    print("=" * 70)
    print("关键数据示例")
    print("=" * 70)

    print("\n【市场情绪】")
    try:
        data = kpl.market_sentiment()
        if isinstance(data, dict) and "错误" not in data:
            for k, v in list(data.items())[:6]:
                print(f"  {k}: {v}")
    except Exception as e:
        print(f"  错误: {e}")

    print("\n【龙虎榜】")
    try:
        data = kpl.longhubang_list()
        if isinstance(data, dict) and "错误" not in data:
            if "总数" in data:
                print(f"  总数: {data['总数']} 只")
            for k, v in list(data.items())[:5]:
                if k != "总数":
                    print(f"  {k}: {v if not isinstance(v, list) else f'{len(v)} 项'}")
    except Exception as e:
        print(f"  错误: {e}")

    print("\n【百日新高】")
    try:
        data = kpl.new_high("2026-08-01")
        if isinstance(data, dict) and "错误" not in data:
            for k, v in data.items():
                print(f"  {k}: {v}")
    except Exception as e:
        print(f"  错误: {e}")

    print("\n" + "=" * 70)
    return results

if __name__ == "__main__":
    test_mcp_tools()
