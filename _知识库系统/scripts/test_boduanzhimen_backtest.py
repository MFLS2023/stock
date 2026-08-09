#!/usr/bin/env python3
"""回测抽取规则的回归测试。

为什么要单独一份：`boduanzhimen_backtest.py` 的正则是全靠语料实测调出来的，
每一条都对应一个真实的误判样本。没有测试守着，下次谁想"顺手放宽一点"
就会把踩过的坑重新踩一遍 —— 而且不会报错，只会静默给出错误的命中率。

下面每个用例的原文都来自语料（注释里标了日期），不是编的。

跑法：
    python test_boduanzhimen_backtest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

import boduanzhimen_backtest as bt


def direction_of(text: str, needle: str) -> tuple[str, bool]:
    """取 needle（点位串）在 text 里的位置，问抽取器判什么方向。"""
    position = text.index(needle)
    return bt.classify_direction(text, position)


def test_resistance_counts_as_upper_bound() -> None:
    """阻力/压力都该判成「上限」。原来只认「压力」，「阻力」全被丢掉。"""
    cases = [
        "股指今天的强阻力在3168点",                    # [2023-08-30]
        "今天的阻力位在3493点左右",                    # [2025-07-04]
        "喇叭口上的第一阻力是3475点",                  # [2025-07-06]
        "今天上涨的阻力位在3933点",                    # [2025-10-16]
        "周五的支撑是3215,压力是3249.59点",            # [2023-07-16]
    ]
    for text in cases:
        needle = next(part for part in ("3168", "3493", "3475", "3933", "3249") if part in text)
        label, neutral = direction_of(text, needle)
        assert label == "上限", f"{text!r} 应判上限，实际 {label!r}（neutral={neutral}）"


def test_support_counts_as_lower_bound() -> None:
    """支撑该判成「下限」。"""
    label, _ = direction_of("今天上涨的阻力位在3933点,支撑在3902点。", "3902")
    assert label == "下限", f"支撑应判下限，实际 {label!r}"


def test_target_and_theory_are_neutral() -> None:
    """「目标位是X」「理论点位是X」本身不含方向，必须走中性分支。

    这类句式方向相反的两种用法都存在：
      「第一调整目标在2335点」向下、「b区上涨的理论点位是3960点」向上。
    所以不能硬编方向，必须交给 infer_neutral_direction 看上下文。
    """
    cases = [
        ("我当初预判的第一目标:3090点", "3090"),          # [2024-04-20]
        ("b区上涨的理论点位是3960点附近", "3960"),        # [2025-09-18]
        ("我们计算的理论点位是3883点", "3883"),           # [2025-09-15]
        ("周线计算的理论点位在4260点", "4260"),           # [2026-05-16]
        ("这波小反弹,理论位置在3949点", "3949"),          # [2025-12-24]
    ]
    for text, needle in cases:
        label, neutral = direction_of(text, needle)
        assert neutral and not label, f"{text!r} 应走中性分支，实际 label={label!r} neutral={neutral}"


def test_neutral_direction_uses_nearest_hint_in_same_clause() -> None:
    """中性目标定方向只看本句，且取离点位最近的涨跌词。

    实测踩过的坑（[2025-09-18]）：上一句的「调整」和本句的「上涨」同时命中，
    按前 60 字取就判不出方向，而本句其实写得很清楚。
    """
    text = "也许,股指调整时,你的股票会不断拉升。牛市的特点是,板块不会齐涨齐跌。b区上涨的理论点位是3960点"
    assert bt.infer_neutral_direction(text, text.index("3960")) == "上行"

    # 同一句里既有"下跌"又有"目标"，取靠近点位的那个
    text2 = "上证一共进行了三波下跌,它的第一调整目标在2335点"
    assert bt.infer_neutral_direction(text2, text2.index("2335")) == "下行"

    # 本句里没有任何涨跌词 → 判不出，返回空串（宁可丢也不猜）
    text3 = "我们看看这个数。理论点位是3883点"
    assert bt.infer_neutral_direction(text3, text3.index("3883")) == ""


def test_no_numeric_fallback_for_direction() -> None:
    """不许用"点位高于收盘价就是上行"做兜底 —— 那会把方向判反。

    [2024-09-06]「上证跌到了第一目标2892点」，2892 高于当日收盘 2765，
    数值兜底会判成「上行」，与原文的「跌到」正好相反，还会算出一个假命中。
    """
    text = "2024年7月25日,上证跌到了第一目标2892点"
    assert bt.infer_neutral_direction(text, text.index("2892")) == "下行"


def test_shanghai50_is_not_shanghai_index() -> None:
    """上证50 的点位不能当上证指数的。

    [2024-09-04] 整段分析上证50，给的 2335/2248 是它的调整目标，
    而当时上证指数在 2700 多 —— 拿 2248 去对上证会被判成"命中"。
    """
    assert bt.OTHER_SYMBOL.search("上证50一共进行了三波下跌,第一调整目标在2335点")
    assert bt.OTHER_SYMBOL.search("上证50的这次调整,第二目标是2248点")
    assert bt.OTHER_SYMBOL.search("50指数的89线现在是2509点")


def test_other_symbol_does_not_eat_real_levels() -> None:
    """标的物过滤不能误伤真点位。

    两类实测误伤，都必须继续放行：
      1. `50\\s*的` 会命中「3050的支撑位」里的「50的」
      2. 「板块」「个股」是泛指，他谈完上证顺口提一句板块不代表换了标的
         （[2025-09-18] 那条 3960 就是这么被误删的，实测 12 处误伤）
    """
    must_pass = [
        "跌到3050的支撑位",
        "大盘在3350的位置反弹",
        "上证在2850的位置",
        "牛市的特点是,板块不会齐涨齐跌。b区上涨的理论点位是3960点",
        "这次不会超过3674点。下面说说板块,本周AI分支出现了分化",
        "指数一直这样上涨,个股会活跃起来。今天的阻力位在3493点左右",
    ]
    for text in must_pass:
        hit = bt.OTHER_SYMBOL.search(text)
        assert not hit, f"{text!r} 不该被判成非上证标的，却命中了 {hit.group(0)!r}"


def test_not_a_level_excludes_index_names_counts_years() -> None:
    """NOT_A_LEVEL 三条规则各自防的误判。

    现行 POINT 正则要求数字后带「点」字，所以这三条在当前口径下大多不触发；
    但它是道保险 —— 一旦有人放宽 POINT（去掉「点」字要求），这些立刻生效。
    这里用宽松口径直接验规则本身。
    """
    import re
    loose = re.compile(r"(?<!\d)([2-6]\d{3})")
    cases = [
        ("突然发现国证2000就要完蛋", "指数或板块名"),      # [2025-01-11]
        ("国证2000点附近有支撑", "指数或板块名"),
        ("已经有1000多只股票跌破了2024年9月24日的低点", "年份"),
        ("这种渐宽底在2013年曾经出现过", "年份"),           # [2023-03-10]
        ("4年才拉黑5000多人", "数量单位"),                  # [2023-04-24]
    ]
    for text, expected in cases:
        blocked = [
            bt.is_not_a_level(text, m.start(), m.end())
            for m in loose.finditer(text)
        ]
        assert any(expected in (reason or "") for reason in blocked), \
            f"{text!r} 应被 {expected} 拦下，实际 {blocked}"
    # 真点位必须放行
    assert not bt.is_not_a_level("跌到3144点", 3, 7)


def test_expects_breakout_excluded() -> None:
    """他给阻力位却紧接着说会突破的，不是「上限」主张。

    [2025-07-06]「第一阻力是3475点…所以,冲破3475以后,就直奔3493点去了」
    判它"被突破=他说错"是把话读反了。
    """
    assert bt.EXPECTS_BREAKOUT.search("冲破3475以后,就直奔3493点去了")
    assert bt.EXPECTS_BREAKOUT.search("这种阻力,在牛市是会站上去的")   # [2025-08-14]


def test_negated_past_vs_forward_condition() -> None:
    """「没有跌破X」要分清是既成事实还是前瞻条件。

    「还好,没有跌破3761点」= 今天没破，既成事实，不是预测。 [2025-09-23]
    「只要没有跌破3834点,就维持这种看法」= 前瞻条件，该保留。 [2025-09-17]
    """
    assert bt.is_negated_past("还好,还好,没有跌破")
    assert bt.is_negated_past("周五收盘没有达到")
    assert not bt.is_negated_past("只要没有跌破")
    assert not bt.is_negated_past("如果没有跌破")


def test_hypothetical_only_matches_enumeration() -> None:
    """假设句过滤只认"他明说这是若干种可能之一"，不能按「也许」滤。

    [2025-03-31]「也许,这次下跌完毕后,指数突然大幅拉升,一举突破3439点」
    是实打实的预测，只是说得客气。按「也许」滤会误杀真样本（实测误杀 2 条）。
    """
    assert bt.HYPOTHETICAL.search("这种情况最糟糕的一种走势是:跌破3980点")   # [2025-11-17]
    assert not bt.HYPOTHETICAL.search("也许,这次下跌完毕后,指数突然大幅拉升,一举突破3439点")
    assert not bt.HYPOTHETICAL.search("不排除跌破3900点")


def test_index_data_sanity_check_rejects_pingan() -> None:
    """收盘中位数 < 500 必须直接报错。

    这个坑踩过两次：market_cache.db 和 live-market MCP 的 000001 都是
    **平安银行**（11 元）不是上证指数（3900 点）。不校验就会静默出一份
    全部"未达"的报告，而且看不出哪里错了。
    """
    import tempfile
    dates = pd.date_range("2025-01-01", periods=60, freq="B")
    fake = pd.DataFrame({
        "date": dates, "open": 11.0, "high": 11.2, "low": 10.8, "close": 11.1,
    })
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "pingan.csv"
        fake.to_csv(path, index=False)
        try:
            bt.load_index(path)
        except SystemExit as exc:
            assert "不像上证指数" in str(exc), f"报错信息应点明问题，实际 {exc}"
        else:
            raise AssertionError("平安银行价位的数据必须被拒绝")


def test_index_data_requires_ohlc_columns() -> None:
    """缺列要报错，不能等到 judge 里才 KeyError。"""
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "bad.csv"
        pd.DataFrame({"date": ["2025-01-02"], "close": [3900.0]}).to_csv(path, index=False)
        try:
            bt.load_index(path)
        except SystemExit as exc:
            assert "缺列" in str(exc)
        else:
            raise AssertionError("缺 open/high/low 应被拒绝")


def test_breach_tolerance_uses_his_own_definition() -> None:
    """上限判定留 0.6% 容差，来自他自己给的突破定义。

    [2025-04-14]「要比阻力位置高出0.6%才有可能」算突破。
    实测 [2023-08-30] 声称阻力 3168，窗口内最高 3177.06 只超 0.29%，
    按他的标准那不叫突破 —— 碰一下就算破会把他判对的算成错。
    """
    assert bt.BREACH_TOLERANCE == 0.006
    # 发文日必须落在数据区间内，否则 judge 直接记「无数据」
    dates = pd.date_range("2025-01-01", periods=20, freq="B")
    index = pd.DataFrame(
        {"open": 3100.0, "high": 3177.06, "low": 3050.0, "close": 3150.0}, index=dates
    )
    index.index.name = "date"
    item = bt.Prediction(
        date="2025-01-03", title="t", point=3168.0, direction="上限",
        has_deadline=False, excerpt="", base_close=3137.14,
    )
    bt.judge(item, index, (5,))
    assert item.verdict[5] == "未破上限", f"超出仅 0.29% 不该算突破，实际 {item.verdict[5]}"

    # 超出 1% 就该算突破
    index2 = index.copy()
    index2["high"] = 3168 * 1.01
    item2 = bt.Prediction(
        date="2025-01-03", title="t", point=3168.0, direction="上限",
        has_deadline=False, excerpt="", base_close=3137.14,
    )
    bt.judge(item2, index2, (5,))
    assert item2.verdict[5] == "被突破", f"超出 1% 该算突破，实际 {item2.verdict[5]}"


def test_incomplete_window_is_not_counted_as_miss() -> None:
    """窗口没走完的要记「无数据」，不能当「未达」。

    用 30 根行情去判 120 日窗口，会把"还没到期"当"没实现"，系统性压低命中率。
    """
    dates = pd.date_range("2025-01-01", periods=30, freq="B")
    index = pd.DataFrame(
        {"open": 3100.0, "high": 3150.0, "low": 3050.0, "close": 3100.0}, index=dates
    )
    index.index.name = "date"
    item = bt.Prediction(
        date="2025-01-02", title="t", point=3500.0, direction="上行",
        has_deadline=False, excerpt="", base_close=3100.0,
    )
    bt.judge(item, index, (5, 120))
    assert item.verdict[5] == "未达", "5 日窗口有数据，应正常判定"
    assert item.verdict[120] == "无数据", f"120 日窗口未走完应记无数据，实际 {item.verdict[120]}"


def test_intraday_levels_are_flagged() -> None:
    """日内点位要被识别出来，且级别词优先于时间词。"""
    # 「今天的阻力位」是日内
    assert bt.INTRADAY_SCOPE.search("今天上涨的阻力位在")
    # 「长期阻力」即使前面有「今天」也不是日内
    assert bt.LONG_TERM_SCOPE.search("它今天碰到了长期阻力")


def test_end_to_end_extraction_is_stable() -> None:
    """端到端：抽出的条数落在预期区间，且不含明显的假样本。

    不写死具体条数（语料会变），但守住两条：
      1. 可检验样本 >= 20（低于这个说明过滤器写崩了）
      2. 抽出的每条都必须有方向，且点位在合理区间内
    """
    if not bt.INDEX_CACHE.exists():
        print("      （跳过端到端：缺行情缓存）")
        return
    index = bt.load_index(bt.INDEX_CACHE)
    predictions, report = bt.extract_predictions(bt.CHUNKS, index)
    assert len(predictions) >= 20, f"抽出 {len(predictions)} 条，过少，过滤器可能写崩了"
    for item in predictions:
        assert item.direction in ("上行", "下行", "上限", "下限"), f"方向异常 {item.direction!r}"
        assert bt.POINT_MIN <= item.point <= bt.POINT_MAX, f"点位越界 {item.point}"
    # 已知必须被排除的（都是实测确认的假样本）
    banned = {
        ("2024-09-04", 2335.0),   # 上证50 的调整目标
        ("2024-09-04", 2248.0),   # 上证50 的第二目标
        ("2024-04-20", 3090.0),   # 复述"我当初预判的第一目标"
        ("2025-09-23", 3761.0),   # "还好,没有跌破3761点" 既成事实
        ("2025-11-17", 3980.0),   # "最糟糕的一种走势是" 假设句
    }
    got = {(item.date, item.point) for item in predictions}
    leaked = banned & got
    assert not leaked, f"这些已确认的假样本又漏进来了: {sorted(leaked)}"
    print(f"      （端到端：抽出 {len(predictions)} 条，"
          f"无方向 {report.skipped_no_direction} 处被丢）")


def main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    failed = []
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except Exception as exc:
            failed.append((test.__name__, exc))
            print(f"  FAIL  {test.__name__}: {exc}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
