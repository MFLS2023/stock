#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开盘啦 API 全接口测试 - 使用实际可用方法"""
import io
import sys
import time
# 只在直接运行时重包 stdout：模块级执行会在 unittest discover 导入本文件时
# 替换并最终关闭原 stdout，害得套件里后面 print 的测试报 I/O closed（实测踩过）
if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from kaipanla_crawler import KaipanlaCrawler

def test_all():
    """测试所有可用接口"""
    crawler = KaipanlaCrawler()
    results = []

    tests = [
        # 市场整体
        ("市场行情", lambda: crawler.get_daily_data("2026-08-03")),
        ("市场情绪", lambda: crawler.get_market_sentiment()),
        ("实时市场情绪", lambda: crawler.get_realtime_market_mood()),
        ("涨跌分析", lambda: crawler.get_realtime_rise_fall_analysis()),
        ("市场指数", lambda: crawler.get_market_index()),
        ("指数列表", lambda: crawler.get_realtime_index_list()),

        # 涨停板块
        ("连板梯队", lambda: crawler.get_consecutive_limit_up()),
        ("涨停天梯", lambda: crawler.get_limit_up_ladder()),
        ("市场涨停天梯", lambda: crawler.get_market_limit_up_ladder()),
        ("板块涨停天梯", lambda: crawler.get_sector_limit_up_ladder("801346")),
        ("实际涨跌停", lambda: crawler.get_realtime_actual_limit_up_down()),
        ("历史炸板", lambda: crawler.get_historical_broken_limit_up("2026-08-03")),

        # 板块相关
        ("板块排行", lambda: crawler.get_sector_ranking()),
        ("板块强度", lambda: crawler.get_sector_strength()),
        ("板块强度DF", lambda: crawler.get_sector_strength_dataframe()),
        ("板块资金数据", lambda: crawler.get_sector_capital_data()),
        ("多板块强度", lambda: crawler.get_multiple_sectors_strength(["801346", "801001"])),
        ("板块分时", lambda: crawler.get_sector_intraday("801346")),
        ("板块成分股", lambda: crawler.get_sector_constituent_stocks("801346")),
        ("板块所有股票", lambda: crawler.get_sector_all_stocks("801346")),
        ("板块股票统计", lambda: crawler.get_board_stocks_count_and_list("801346")),
        ("板块集合竞价异动", lambda: crawler.get_sector_bidding_anomaly("801346")),

        # 个股相关
        ("个股分时", lambda: crawler.get_stock_intraday("000001")),
        ("个股大单分时", lambda: crawler.get_stock_big_order_intraday("000001")),
        ("个股集合竞价", lambda: crawler.get_stock_call_auction_tick("000001")),

        # 龙虎榜
        ("龙虎榜列表", lambda: crawler.get_longhubang_stock_list()),
        ("龙虎榜DF", lambda: crawler.get_longhubang_dataframe()),
        ("龙虎榜明细", lambda: crawler.get_longhubang_stock_detail("000001")),

        # 其他
        ("百日新高", lambda: crawler.get_new_high_data("2026-08-03")),
        ("大幅回撤", lambda: crawler.get_sharp_withdrawal()),
        ("实时大幅回撤", lambda: crawler.get_realtime_sharp_withdrawal()),
        ("同花顺热榜", lambda: crawler.get_ths_hot_rank()),
        ("ETF排行", lambda: crawler.get_etf_ranking()),
        ("所有ETF排行", lambda: crawler.get_all_etf_ranking()),
        ("板块新闻", lambda: crawler.get_plate_news("801346")),
    ]

    print("=" * 70)
    print("开盘啦 API 全接口测试（实际可用方法）")
    print("=" * 70)

    for name, fn in tests:
        try:
            t0 = time.time()
            data = fn()
            cost = time.time() - t0

            # 判断数据类型和大小
            if isinstance(data, dict):
                keys = list(data.keys())[:5]
                size = f"{len(data)} 键"
                sample = f"{keys}"
            elif hasattr(data, 'shape'):  # DataFrame
                size = f"{data.shape[0]}行"
                sample = f"列:{list(data.columns)[:3]}"
            elif isinstance(data, list):
                size = f"{len(data)}项"
                sample = ""
            else:
                size = f"{type(data).__name__}"
                sample = str(data)[:40]

            results.append((name, "✓", size, f"{cost:.2f}s"))
            print(f"✓ {name:18s} {size:12s} {cost:6.2f}s")

        except Exception as e:
            err_msg = str(e)[:40]
            results.append((name, "✗", err_msg, ""))
            print(f"✗ {name:18s} {err_msg}")

    print("=" * 70)
    print(f"通过 {sum(1 for r in results if r[1]=='✓')}/{len(results)}")
    print()

    # 详细输出关键数据
    print("=" * 70)
    print("关键数据示例")
    print("=" * 70)

    # 1. 市场情绪
    print("\n【市场情绪】")
    try:
        data = crawler.get_market_sentiment()
        if isinstance(data, dict):
            for k, v in list(data.items())[:8]:
                print(f"  {k}: {v}")
    except Exception as e:
        print(f"  错误: {e}")

    # 2. 涨停天梯
    print("\n【涨停天梯】")
    try:
        data = crawler.get_limit_up_ladder()
        if isinstance(data, dict):
            print(f"  总涨停数: {data.get('total_limit_up_count')}")
            ladder = data.get('ladder', [])
            for item in ladder[:5]:
                print(f"    {item.get('board_count')}板: {item.get('count')}只")
    except Exception as e:
        print(f"  错误: {e}")

    # 3. 板块排行
    print("\n【板块排行 TOP5】")
    try:
        data = crawler.get_sector_ranking()
        if hasattr(data, 'head'):
            print(data.head())
        elif isinstance(data, dict):
            print(f"  键: {list(data.keys())[:5]}")
    except Exception as e:
        print(f"  错误: {e}")

    # 4. 龙虎榜
    print("\n【龙虎榜】")
    try:
        data = crawler.get_longhubang_stock_list()
        if isinstance(data, list):
            print(f"  总数: {len(data)} 只")
            if data:
                s = data[0]
                print(f"  示例: {s.get('name')} {s.get('code')} - {s.get('reason')}")
        elif isinstance(data, dict):
            print(f"  键: {list(data.keys())[:5]}")
    except Exception as e:
        print(f"  错误: {e}")

    # 5. 市场行情
    print("\n【市场行情 2026-08-03】")
    try:
        data = crawler.get_daily_data("2026-08-03")
        if hasattr(data, 'to_dict'):
            d = data.to_dict()
            for k, v in list(d.items())[:10]:
                print(f"  {k}: {v}")
        elif isinstance(data, dict):
            for k, v in list(data.items())[:10]:
                print(f"  {k}: {v}")
    except Exception as e:
        print(f"  错误: {e}")

    # 6. 百日新高
    print("\n【百日新高】")
    try:
        data = crawler.get_new_high_data("2026-08-03")
        print(f"  新高股票数: {data}")
    except Exception as e:
        print(f"  错误: {e}")

    print("\n" + "=" * 70)
    return results

if __name__ == "__main__":
    test_all()
