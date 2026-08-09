#!/usr/bin/env python3
"""波段之门指标实现的验证。

重点不是"代码跑通"，而是"算出来的数和行情软件一致"。
KDJ 用东财接口给的 KDJ.K/KDJ.D 作外部基准对照 —— 这是唯一能证明
SMA 递推翻译正确的办法，自洽测试证明不了这一点。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from boduanzhimen_indicators import (  # noqa: E402
    barslast, barslastcount, boll, count, every, hhv, kd_dull, kdj, kdj_battle,
    llv, ma, ma_slope_color, macd, macd_zero_cross, position_line, ref,
    select_doubling, select_ma_climb, sell_next_day, sma_tdx, std_tdx,
    strength_lines, volume_price_rule, eleven_sequence, boll_top_risk,
)


def make_frame(rows: int = 300, seed: int = 7) -> pd.DataFrame:
    """构造一段有涨有跌的价格序列，索引是连续交易日。"""
    generator = np.random.default_rng(seed)
    steps = generator.normal(0, 0.015, rows)
    close = 100 * np.exp(np.cumsum(steps))
    high = close * (1 + np.abs(generator.normal(0, 0.008, rows)))
    low = close * (1 - np.abs(generator.normal(0, 0.008, rows)))
    open_ = low + (high - low) * generator.random(rows)
    volume = generator.integers(1_000_000, 9_000_000, rows).astype("float64")
    index = pd.bdate_range("2024-01-01", periods=rows)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def test_ma_matches_manual() -> None:
    frame = make_frame(30)
    result = ma(frame["close"], 5)
    assert result.iloc[:4].isna().all(), "MA 前 N-1 根必须是 NaN"
    expected = frame["close"].iloc[0:5].mean()
    assert abs(result.iloc[4] - expected) < 1e-9


def test_sma_tdx_recursion() -> None:
    """SMA(X,N,M) 必须严格按 Y=(M*X+(N-M)*Y')/N 递推，且不等于 ewm。"""
    series = pd.Series([10.0, 20.0, 30.0, 40.0])
    result = sma_tdx(series, 3, 1)
    # 首值为起点，之后逐项手算
    expected = [10.0]
    for value in series.iloc[1:]:
        expected.append((1 * value + (3 - 1) * expected[-1]) / 3)
    assert np.allclose(result.to_numpy(), expected), f"{result.tolist()} != {expected}"
    # SMA(X,N,M) 与 ewm(alpha=M/N, adjust=False) 数学等价 —— 二者一致才说明递推没错。
    # 用 span 或 adjust=True 才是错的，那才是必须避开的写法。
    equivalent = series.ewm(alpha=1 / 3, adjust=False).mean()
    assert np.allclose(result.to_numpy(), equivalent.to_numpy())
    wrong = series.ewm(span=3, adjust=False).mean()  # alpha=2/(3+1)=0.5，与 M/N=1/3 不同
    assert not np.allclose(result.to_numpy(), wrong.to_numpy()), "span 写法必须与 M/N 递推不同"


def test_ref_hhv_llv_count_every() -> None:
    series = pd.Series([1.0, 5.0, 3.0, 9.0, 2.0])
    assert ref(series, 1).tolist()[1:] == [1.0, 5.0, 3.0, 9.0]
    assert hhv(series, 3).tolist() == [1.0, 5.0, 5.0, 9.0, 9.0]
    assert llv(series, 3).tolist() == [1.0, 1.0, 1.0, 3.0, 2.0]
    condition = series > 2
    assert count(condition, 3).tolist() == [0.0, 1.0, 2.0, 3.0, 2.0]
    assert every(condition, 2).tolist() == [False, False, True, True, False]


def test_barslast_and_run_length() -> None:
    condition = pd.Series([False, True, False, False, True, False])
    assert barslast(condition).tolist()[1:] == [0.0, 1.0, 2.0, 0.0, 1.0]
    assert np.isnan(barslast(condition).iloc[0]), "首根之前从未成立应为 NaN"
    run = barslastcount(pd.Series([True, True, True, False, True]))
    assert run.tolist() == [1.0, 2.0, 3.0, 0.0, 1.0]


def test_std_uses_sample_deviation() -> None:
    """通达信 STD 是样本标准差；用总体标准差会让布林带偏窄。"""
    frame = make_frame(40)
    mine = std_tdx(frame["close"], 20).iloc[-1]
    sample = frame["close"].iloc[-20:].std(ddof=1)
    population = frame["close"].iloc[-20:].std(ddof=0)
    assert abs(mine - sample) < 1e-9
    assert abs(mine - population) > 1e-12


def test_kdj_against_eastmoney_reference() -> None:
    """用东财返回的上证指数 KDJ 作外部基准。

    数据取自 mcp stock_data index_prices(000001) 2026-07-27~2026-08-07 的输出，
    含它自己算的 KDJ.K / KDJ.D。这里用同期 OHLC 独立算一遍，比对最后几根。

    只比最后几根：KDJ 的 SMA 递推有初值依赖，短样本前段必然偏离，
    递推收敛后才可比 —— 这是方法本身的性质，不是实现缺陷。
    """
    rows = [
        # date, open, high, low, close, 东财K, 东财D —— 上证指数 2026-05-15~08-07 共 60 根
        ("2026-05-15", 4174.18, 4191.81, 4114.09, 4135.39, 62.59, 80.39),
        ("2026-05-18", 4120.14, 4145.66, 4108.60, 4131.53, 46.81, 69.20),
        ("2026-05-19", 4122.96, 4170.29, 4107.99, 4169.54, 44.81, 61.07),
        ("2026-05-20", 4152.70, 4169.85, 4139.97, 4162.19, 41.84, 54.66),
        ("2026-05-21", 4174.38, 4199.53, 4074.22, 4077.28, 28.45, 45.92),
        ("2026-05-22", 4096.17, 4120.09, 4067.75, 4112.90, 26.84, 39.56),
        ("2026-05-25", 4126.34, 4153.88, 4119.80, 4152.57, 32.69, 37.27),
        ("2026-05-26", 4137.32, 4150.29, 4104.46, 4145.37, 35.33, 36.62),
        ("2026-05-27", 4138.81, 4153.56, 4077.62, 4093.73, 30.13, 34.46),
        ("2026-05-28", 4080.30, 4110.78, 4055.83, 4098.64, 30.01, 32.98),
        ("2026-05-29", 4110.52, 4112.95, 4055.89, 4068.57, 22.96, 29.64),
        ("2026-06-01", 4067.16, 4093.04, 4045.69, 4057.74, 17.92, 25.73),
        ("2026-06-02", 4061.46, 4089.57, 4032.58, 4075.10, 20.44, 23.97),
        ("2026-06-03", 4068.34, 4107.05, 4059.91, 4083.97, 27.75, 25.23),
        ("2026-06-04", 4053.67, 4080.72, 4043.43, 4057.78, 25.42, 25.29),
        ("2026-06-05", 4044.83, 4078.93, 4015.06, 4027.74, 20.00, 23.53),
        ("2026-06-08", 3938.71, 4007.49, 3927.85, 3959.34, 17.98, 21.68),
        ("2026-06-09", 3977.54, 4010.87, 3955.91, 4010.03, 26.79, 23.38),
        ("2026-06-10", 3985.12, 4006.31, 3963.44, 3993.23, 29.63, 25.46),
        ("2026-06-11", 3979.71, 3997.48, 3958.44, 3987.01, 30.76, 27.23),
        ("2026-06-12", 4017.86, 4060.27, 4008.18, 4031.51, 39.79, 31.42),
        ("2026-06-15", 4053.58, 4097.17, 4051.07, 4096.47, 57.89, 40.24),
        ("2026-06-16", 4094.21, 4103.93, 4077.87, 4091.89, 69.65, 50.04),
        ("2026-06-17", 4074.29, 4109.96, 4073.73, 4108.08, 79.42, 59.84),
        ("2026-06-18", 4094.23, 4117.45, 4080.29, 4090.48, 81.54, 67.07),
        ("2026-06-22", 4093.95, 4164.42, 4070.17, 4163.10, 87.48, 73.87),
        ("2026-06-23", 4153.59, 4175.35, 4085.59, 4106.25, 81.04, 76.26),
        ("2026-06-24", 4090.10, 4117.28, 4075.49, 4110.81, 77.44, 76.65),
        ("2026-06-25", 4103.48, 4133.10, 4093.01, 4120.28, 73.98, 75.76),
        ("2026-06-26", 4098.69, 4099.78, 4007.86, 4027.26, 53.18, 68.24),
        ("2026-06-29", 4026.69, 4075.33, 3992.55, 4073.90, 50.29, 62.25),
        ("2026-06-30", 4058.17, 4097.41, 4052.17, 4094.40, 52.10, 58.87),
        ("2026-07-01", 4090.76, 4143.31, 4087.53, 4112.44, 56.59, 58.11),
        ("2026-07-02", 4054.09, 4093.68, 4019.22, 4028.90, 44.36, 53.53),
        ("2026-07-03", 4031.34, 4073.88, 4027.26, 4043.64, 38.89, 48.65),
        ("2026-07-06", 4059.19, 4060.07, 4005.41, 4041.24, 36.69, 44.66),
        ("2026-07-07", 4019.49, 4028.51, 3971.71, 3990.24, 28.06, 39.13),
        ("2026-07-08", 3996.81, 4016.03, 3967.91, 3970.88, 19.27, 32.51),
        ("2026-07-09", 3977.55, 4040.54, 3938.88, 4036.59, 28.78, 31.26),
        ("2026-07-10", 4031.54, 4074.83, 3995.81, 3996.16, 28.53, 30.35),
        ("2026-07-13", 3966.02, 3983.05, 3900.67, 3913.79, 20.82, 27.17),
        ("2026-07-14", 3909.27, 3967.13, 3869.30, 3967.13, 28.41, 27.59),
        ("2026-07-15", 3963.73, 3981.67, 3943.70, 3955.58, 32.93, 29.37),
        ("2026-07-16", 3912.38, 3940.45, 3867.60, 3882.41, 24.34, 27.69),
        ("2026-07-17", 3865.32, 3869.22, 3745.17, 3764.16, 18.15, 24.51),
        ("2026-07-20", 3791.66, 3831.66, 3741.11, 3796.28, 17.61, 22.21),
        ("2026-07-21", 3812.16, 3864.60, 3743.36, 3864.37, 24.05, 22.82),
        ("2026-07-22", 3839.66, 3884.43, 3839.66, 3867.03, 28.61, 24.75),
        ("2026-07-23", 3868.09, 3878.83, 3851.71, 3876.78, 37.77, 29.09),
        ("2026-07-24", 3853.63, 3861.04, 3808.64, 3814.20, 35.30, 31.16),
        ("2026-07-27", 3808.90, 3858.31, 3793.45, 3858.24, 39.77, 34.03),
        ("2026-07-28", 3823.13, 3844.01, 3797.37, 3813.32, 38.59, 35.55),
        ("2026-07-29", 3823.29, 3845.77, 3782.48, 3828.47, 46.04, 39.05),
        ("2026-07-30", 3812.11, 3839.34, 3767.50, 3804.69, 45.48, 41.19),
        ("2026-07-31", 3833.54, 3847.09, 3822.37, 3832.26, 51.33, 44.57),
        ("2026-08-03", 3812.61, 3827.64, 3797.64, 3809.66, 46.24, 45.13),
        ("2026-08-04", 3816.37, 3831.94, 3799.52, 3822.28, 47.23, 45.83),
        ("2026-08-05", 3815.12, 3884.40, 3815.12, 3878.43, 63.12, 51.59),
        ("2026-08-06", 3864.27, 3902.05, 3864.27, 3900.35, 74.99, 59.39),
        ("2026-08-07", 3896.49, 3940.93, 3885.62, 3940.04, 83.15, 67.31),
    ]
    frame = pd.DataFrame(
        [{"open": r[1], "high": r[2], "low": r[3], "close": r[4]} for r in rows],
        index=pd.to_datetime([r[0] for r in rows]),
    )
    frame["volume"] = 1.0
    mine = kdj(frame)
    reference_k = [r[5] for r in rows]
    reference_d = [r[6] for r in rows]
    # 前 20 根让 SMA 递推收敛，之后逐根比对；60 根足够收敛，容差压到 0.5 个点
    worst_k = worst_d = 0.0
    worst_at = ""
    for offset in range(20, len(rows)):
        gap_k = abs(mine["k"].iloc[offset] - reference_k[offset])
        gap_d = abs(mine["d"].iloc[offset] - reference_d[offset])
        if gap_k > worst_k:
            worst_k, worst_at = gap_k, rows[offset][0]
        worst_d = max(worst_d, gap_d)
    assert worst_k < 0.5, f"K 最大偏离东财 {worst_k:.3f} @ {worst_at}"
    assert worst_d < 0.5, f"D 最大偏离东财 {worst_d:.3f}"
    print(f"      (KDJ 对东财最大偏离 K={worst_k:.3f} D={worst_d:.3f}，40 根逐根比对)")
    # J 的定义必须是 3K-2D
    assert np.allclose(mine["j"], 3 * mine["k"] - 2 * mine["d"], equal_nan=True)


def test_macd_zero_cross_is_single_bar() -> None:
    """零轴上穿只应在由负转正那一根为真。"""
    frame = make_frame(200)
    signal = macd_zero_cross(frame)
    values = macd(frame)
    difference = (values["dif"] - values["dea"]).to_numpy()
    for index in range(1, len(difference)):
        expected = difference[index - 1] <= 0 and difference[index] > 0
        assert bool(signal.iloc[index]) == expected, f"第 {index} 根不一致"


def test_strength_lines_requires_aligned_index() -> None:
    frame = make_frame(120)
    index_close = pd.Series(3000.0, index=frame.index)
    result = strength_lines(frame, index_close)
    assert {"strength", "m13", "m34", "m55", "m89", "bull_stack"} <= set(result.columns)
    # 指数恒定时，强度就是收盘价的等比缩放，均线关系应与价格均线一致
    assert np.allclose(
        result["m13"].dropna().to_numpy(),
        (ma(frame["close"], 13) / 3000 * 10000).dropna().to_numpy(),
    )
    # 索引完全不重叠必须报错而不是静默出 NaN
    try:
        strength_lines(frame, pd.Series(3000.0, index=pd.bdate_range("2010-01-01", periods=120)))
    except ValueError:
        pass
    else:
        raise AssertionError("索引不重叠时应抛 ValueError")


def test_ma_slope_color_partitions() -> None:
    """涨/平/跌三态互斥，且并集覆盖所有非 NaN 根。"""
    frame = make_frame(100)
    result = ma_slope_color(frame, 5)
    valid = result["line"].notna() & ref(result["line"], 1).notna()
    triple = result[["rising", "flat", "falling"]][valid].sum(axis=1)
    assert (triple == 1).all(), "三态必须恰好一个为真"


def test_position_line_bounded() -> None:
    """位置指标是 RSI 变体，取值必须在 0..100。"""
    frame = make_frame(150)
    result = position_line(frame)
    values = result["position"].dropna()
    assert values.between(0, 100).all(), f"越界: {values.min()}~{values.max()}"


def test_kd_dull_needs_five_consecutive() -> None:
    frame = make_frame(200)
    result = kd_dull(frame)
    values = kdj(frame)
    for index in range(len(frame)):
        if not result["dull_signal"].iloc[index]:
            continue
        window = values["k"].iloc[max(0, index - 4): index + 1]
        assert (window > 80).all(), f"第 {index} 根信号为真但 K 未连续 5 根 >80"
        assert frame["close"].iloc[index] > ma(frame["close"], 55).iloc[index]


def test_kdj_battle_all_flags_present() -> None:
    frame = make_frame(400)
    result = kdj_battle(frame)
    expected = {
        "k", "d", "j", "pullback", "m1", "seven_danger", "overreact",
        "nine_danger_above", "nine_danger_below", "fall_1", "danger",
    }
    assert expected <= set(result.columns)
    # 九分危险的两个变体互斥（股价不能同时在 55/89 线上方和下方）
    assert not (result["nine_danger_above"] & result["nine_danger_below"]).any()


def test_kdj_battle_thresholds_match_formula() -> None:
    """逐个阈值对着原公式反推验证。

    为什么要单写这个：`test_kdj_battle_all_flags_present` 只断言列名齐全和一条
    恒真的互斥关系，**阈值可以随便改而测试不报错**。实测把 `k-d>15.5` 改成
    `>15.0`、把 M1 的 `AND` 改成 `OR`、把九分危险的 `C<MA233` 改成 `C>MA233`，
    18/18 依然全绿 —— 那种"通过"是虚假信心。

    这里的写法是：对每个信号，取它为真的所有 bar，逐 bar 断言原公式条件成立；
    再取它为假的 bar，断言条件不成立。双向都查，改动任一阈值都会被抓住。
    """
    # 1200 根 / seed=31：实测这组能让全部 8 个信号都至少触发一次
    # （`nine_danger_below` 要 6 个条件同时成立，600 根样本里会出现 0 次，
    #  那样"全 False == 全 False"也能通过断言，测试就失去判别力）。
    frame = make_frame(1200, seed=31)
    result = kdj_battle(frame)
    values = kdj(frame)
    k, d, j = values["k"], values["d"], values["j"]
    close = frame["close"]
    stack_run = barslastcount((j > k) & (k > d) & (j > d))
    ma55, ma89, ma233 = ma(close, 55), ma(close, 89), ma(close, 233)

    # 逐个信号写出它的原公式定义，然后要求「信号 == 定义」完全相等。
    # 用 == 而非单向蕴含：单向只能抓住"该真却假"，抓不住阈值放宽导致的"该假却真"。
    checks = {
        "pullback": (k - d > 15.5),                                  # 日K:K-D>15.5
        "m1": (j > 110) & (d > 75),                                  # M1:J>110 AND D>75
        "seven_danger": barslastcount((j - k > 0) & (j - d > 0)) > 12,  # M3:>12
        "overreact": (j - k > 28.3) & (d > 40),                      # AL:J-K>28.3 AND D>40
        "nine_danger_above": (stack_run < 6) & (k > 78) & (close < ma233)
        & (stack_run > 1) & (close > ma55) & (close > ma89),
        "nine_danger_below": (stack_run < 6) & (k > 78) & (close < ma233)
        & (stack_run > 1) & (close < ma55) & (close < ma89),
        "fall_1": (k / d.replace(0, np.nan) > 1.37) & (close > ref(close, 1)),
        "danger": (barslastcount(d > 80) > 6) & (j > 90),            # M4:>6 AND J>90
    }
    for name, expected_series in checks.items():
        expected_bool = expected_series.fillna(False).astype(bool)
        actual_bool = result[name].fillna(False).astype(bool)
        mismatch = int((expected_bool != actual_bool).sum())
        assert mismatch == 0, f"{name} 与原公式定义不符，{mismatch} 根不一致"
        # 该信号在这段数据里必须至少触发一次，否则"全 False == 全 False"也会通过，
        # 阈值改坏了照样过不了这一关。
        assert actual_bool.any(), f"{name} 在 600 根样本里从未触发，测试无判别力"


def test_boll_width_is_two_sigma() -> None:
    """布林带必须是 ±2 倍标准差 —— 改成 1.5 倍原先测不出来。"""
    frame = make_frame(120)
    bands = boll(frame)
    deviation = std_tdx(frame["close"], 20)
    mid = ma(frame["close"], 20)
    valid = bands["upper"].notna()
    assert np.allclose(bands["upper"][valid], (mid + 2 * deviation)[valid])
    assert np.allclose(bands["lower"][valid], (mid - 2 * deviation)[valid])
    # 上下轨到中轨的距离必须相等且为正
    span_up = (bands["upper"] - bands["mid"])[valid]
    span_down = (bands["mid"] - bands["lower"])[valid]
    assert np.allclose(span_up, span_down)
    assert (span_up > 0).all()


def test_select_doubling_rejects_weaker_limit_up() -> None:
    """涨停阈值必须是 1.099 —— 放宽到 1.05 会把 6% 的涨幅也当涨停。

    原公式 `T1:=REF(C,2)/REF(C,3)>1.099`，1.099 对应 A 股 10% 涨停板。
    构造一个前天只涨 6% 的序列：其余条件全部满足，唯独不是涨停，必须不被选中。
    """
    # 前天涨 6%（10.0→10.6），昨天放量收阴，今天收阳且 low==open
    frame = pd.DataFrame(
        {
            "open": [10.0, 10.0, 10.2, 10.5, 10.2],
            "high": [10.1, 10.1, 10.7, 10.6, 10.6],
            "low": [9.9, 9.9, 10.1, 10.1, 10.2],
            "close": [10.0, 10.0, 10.6, 10.2, 10.5],
            "volume": [1e6, 1e6, 1e6, 3e6, 1e6],
        },
        index=pd.bdate_range("2025-03-03", periods=5),
    )
    assert not select_doubling(frame).any(), "6% 涨幅不是涨停，不该命中翻倍形态"
    # 同样的形态换成真涨停（10.0→11.2）就必须命中，证明测试有判别力
    frame.loc[frame.index[2], ["close", "high", "open"]] = [11.2, 11.3, 10.2]
    frame.loc[frame.index[3], ["open", "close", "high", "low"]] = [11.1, 10.8, 11.2, 10.7]
    frame.loc[frame.index[4], ["open", "low", "close", "high"]] = [10.7, 10.7, 11.0, 11.1]
    assert select_doubling(frame).iloc[4], "真涨停形态必须命中"


def test_select_ma_climb_requires_twelve_of_fourteen() -> None:
    """14 根里至少 12 根 5 日线向上 —— 放宽到 10 根必须被测出来。

    关键是构造**恰好 11 根**向上的序列：11 落在 10 和 12 之间，
    所以原阈值(>=12)不命中、被改坏的阈值(>=10)会命中，差异才暴露得出来。
    构造 8 根或 13 根都测不出这个变异 —— 前者两个阈值都不满足，后者都满足。

    做法：26 根单调上涨（保证 5 日线一直上行），在第 12/13/14 根各砸 8 元打断，
    实测得向上 11 根、C/LLV(L,14)=1.538（>1.18 的涨幅条件也满足，
    确保不是因为涨幅不够才没命中）。
    """
    length = 26
    closes = [100.0 + index * 2 for index in range(length)]
    for position in (12, 13, 14):
        closes[position] = closes[position - 1] - 8
    frame = pd.DataFrame(
        {
            "open": closes, "high": [value + 0.5 for value in closes],
            "low": [value - 0.5 for value in closes], "close": closes,
            "volume": [1e6] * length,
        },
        index=pd.bdate_range("2025-01-01", periods=length),
    )
    ma5 = ma(frame["close"], 5)
    rising_count = int((ma5 > ref(ma5, 1)).tail(14).sum())
    # 用例的判别力依赖这个数恰好是 11，所以先断言它，避免数据漂移后静默失效
    assert rising_count == 11, f"构造数据失效：向上根数应为 11，实际 {rising_count}"
    ratio = float(frame["close"].iloc[-1] / frame["low"].tail(14).min())
    assert ratio > 1.18, f"涨幅条件未满足（{ratio:.3f}），无法孤立验证根数阈值"
    assert not select_ma_climb(frame, code="600519").any(), "11 根向上（<12）不该命中"


def test_kd_dull_requires_exactly_five_consecutive() -> None:
    """K 必须连续 5 根 > 80 —— 放宽到 3 根原先测不出来。

    `test_kd_dull_needs_five_consecutive` 只做单向检查（信号为真时验证条件），
    改成 3 根后信号变多，但每个新信号的前 3 根确实都 > 80，单向检查照样通过。
    这里改成双向：条件成立的每一根都必须出信号。
    """
    frame = make_frame(400, seed=31)
    result = kd_dull(frame)
    values = kdj(frame)
    expected = (count(values["k"] > 80, 5) == 5) & (frame["close"] > ma(frame["close"], 55))
    expected_bool = expected.fillna(False).astype(bool)
    actual_bool = result["dull_signal"].fillna(False).astype(bool)
    mismatch = int((expected_bool != actual_bool).sum())
    assert mismatch == 0, f"钝化信号与「连续5根>80 且站55日线」不符，{mismatch} 根不一致"
    assert actual_bool.any(), "样本里从未触发钝化，测试无判别力"


def test_select_doubling_matches_pattern() -> None:
    """构造一个符合翻倍形态的序列，确认能被选出。"""
    close = [10.0, 10.0, 11.2, 10.8, 11.0]   # 第2根涨停(11.2/10.0)，第3根收阴，第4根收阳
    open_ = [10.0, 10.0, 10.2, 11.1, 10.7]   # 第4根 low==open
    high = [10.1, 10.1, 11.3, 11.2, 11.1]
    low = [9.9, 9.9, 10.1, 10.7, 10.7]
    volume = [1e6, 1e6, 1e6, 3e6, 1e6]       # 第3根放量 >2倍
    frame = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=pd.bdate_range("2025-01-01", periods=5),
    )
    signal = select_doubling(frame)
    assert bool(signal.iloc[4]), "最后一根应命中翻倍形态"
    assert not signal.iloc[:4].any(), "前几根不应命中"


def test_select_ma_climb_excludes_bse_codes() -> None:
    frame = make_frame(60)
    assert not select_ma_climb(frame, code="830001").any(), "8 开头必须排除"
    assert not select_ma_climb(frame, code="430047").any(), "4 开头必须排除"
    normal = select_ma_climb(frame, code="600519")
    assert normal.dtype == bool


def test_sell_next_day_pierces_ma5() -> None:
    frame = make_frame(200)
    signal = sell_next_day(frame)
    ma5, ma13 = ma(frame["close"], 5), ma(frame["close"], 13)
    for index in np.flatnonzero(signal.to_numpy())[:20]:
        assert frame["high"].iloc[index] > ma5.iloc[index]
        assert frame["low"].iloc[index] < ma5.iloc[index]
        assert frame["close"].iloc[index] > ma13.iloc[index]
        assert frame["close"].iloc[index] < frame["close"].iloc[index - 1]


def test_volume_price_rule_flags_amount_proxy() -> None:
    frame = make_frame(100)
    result = volume_price_rule(frame)
    assert result.attrs["amount_proxy"] is True, "无 amount 列时必须标注用了成交量代替"
    frame_with_amount = frame.assign(amount=frame["volume"] * frame["close"])
    assert volume_price_rule(frame_with_amount).attrs["amount_proxy"] is False


def test_boll_top_risk_requires_all_three() -> None:
    frame = make_frame(300)
    signal = boll_top_risk(frame)
    bands = boll(frame)
    for index in np.flatnonzero(signal.to_numpy())[:20]:
        assert frame["high"].iloc[index] / bands["upper"].iloc[index] > 0.998
        assert frame["close"].iloc[index] < ma(frame["close"], 89).iloc[index]
        assert bands["mid"].iloc[index] < bands["mid"].iloc[index - 1]


def test_eleven_sequence_cycles_1_to_11() -> None:
    frame = make_frame(60)
    start = str(frame.index[10].date())
    result = eleven_sequence(frame, start)
    assert result.iloc[10] == 1, "起始日应标 1"
    assert result.iloc[21] == 1, "11 根后回到 1"
    valid = result.dropna()
    assert valid.between(1, 11).all()
    assert result.iloc[:10].isna().all(), "起始日之前不应有值"
    try:
        eleven_sequence(frame, "1990-01-01")
    except ValueError:
        pass
    else:
        raise AssertionError("起始日不在范围内应抛 ValueError")


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
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
