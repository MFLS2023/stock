#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三个此前零测试的选股公式的阈值级测试（《待处理-20260809》P1-2 / P1-3）。

教训来自变异测试：只断言「列名齐全」「三态互斥」这类恒真条件，把阈值改成
反义也测不出来。所以这里的每个公式都构造**能区分对错的数据**，断言：
  ① 满足全部条件时信号为真（正向）；
  ② 破坏其中某一个阈值条件后信号变假（反向）——证明该条件真的参与判定。

用 unittest 而不是 pytest 函数风格，discover 直接收集、不需要桥接文件。
在 Python312 上跑（需要 numpy/pandas）。
"""
import unittest

import numpy as np
import pandas as pd

from boduanzhimen_indicators import (
    select_11_stocks,
    select_sector_strong,
    select_shock_market,
)


def make_frame(values: list[float], spike: dict[int, float] | None = None) -> pd.DataFrame:
    """由收盘价序列造 OHLC frame：high=close*1.01、low=close*0.99；spike 注入指定日高点。"""
    close = pd.Series(values, dtype=float)
    high = close * 1.01
    low = close * 0.99
    for index, value in (spike or {}).items():
        high.iloc[index] = value
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close})


def rising_series(start: float, end: float, n: int, flat_head: int) -> list[float]:
    """前 flat_head 根在 start 附近窄幅震荡，随后线性爬到 end。"""
    head = [start * (1 + 0.01 * ((i % 3) - 1)) for i in range(flat_head)]
    tail_n = n - flat_head
    tail = [start + (end - start) * i / max(tail_n - 1, 1) for i in range(tail_n)]
    return head + tail


class SelectShockMarketTests(unittest.TestCase):
    def _index(self, frame: pd.DataFrame, level: float = 3000.0) -> pd.Series:
        return pd.Series(level, index=frame.index)

    def test_rising_strength_with_range_signals_at_end(self):
        """强度比持续上行 + 价格站上均线 + 104 日振幅 >2 → 尾部出现信号。"""
        values = rising_series(10.0, 30.0, 260, flat_head=120)
        frame = make_frame(values)
        result = select_shock_market(frame, self._index(frame))
        self.assertTrue(bool(result.iloc[-1]), "满足全部条件的最后一根应为 True")
        self.assertFalse(bool(result[:100].any()), "窗口未走满的前段不应有信号")

    def test_amplitude_threshold_actually_gates(self):
        """同款走势但 104 日振幅压到 <2 倍 → 全 False，证明 HHV/LLV>2 这条真的在卡。"""
        values = rising_series(10.0, 18.0, 260, flat_head=120)
        frame = make_frame(values)
        result = select_shock_market(frame, self._index(frame))
        self.assertFalse(bool(result.any()), "振幅不足 2 倍时不应有任何信号")


class Select11StocksTests(unittest.TestCase):
    def _frame(self) -> pd.DataFrame:
        # 前 50 根冲到 25（留下 699 日内的大行情），其后长期横盘 10-12（189 日压缩），
        # 共 701 根，最后一根放量创 144 日新高。
        values = [24.0] * 50 + [10.5 * (1 + 0.008 * (i % 5 - 2)) for i in range(650)]
        values += [12.0]
        frame = make_frame(values, spike={700: 35.0})
        return frame

    def test_breakout_day_signals(self):
        frame = self._frame()
        index_close = pd.Series(3000.0, index=frame.index)
        result = select_11_stocks(frame, index_close)
        self.assertTrue(bool(result.iloc[-1]), "创新高且横盘压缩成立的末日应为 True")
        self.assertFalse(bool(result.iloc[-2]), "前一根未创新高，不应为 True")

    def test_code_exclusion_83_is_total(self):
        frame = self._frame()
        index_close = pd.Series(3000.0, index=frame.index)
        result = select_11_stocks(frame, index_close, code="830001")
        self.assertFalse(bool(result.any()), "CODELIKE('83') 排除应整段为 False")


class SelectSectorStrongTests(unittest.TestCase):
    def setUp(self):
        self.frame = make_frame(rising_series(10.0, 30.0, 200, flat_head=90))

    def test_missing_ref_raises_by_default(self):
        """缺基准价必须报错而不是静默跑弱化版（P1-3 的核心修复）。"""
        with self.assertRaises(ValueError):
            select_sector_strong(self.frame)

    def test_allow_missing_ref_flags_itself(self):
        result = select_sector_strong(self.frame, allow_missing_ref=True)
        self.assertFalse(result.attrs["ref_date_applied"])
        self.assertTrue(bool(result.any()), "无基准弱化版仍应有信号（否则测试对象不存在）")

    def test_ref_condition_actually_gates(self):
        """基准价设在半山腰：带基准的信号必须是弱化版的严格子集——证明条件生效。"""
        weak = select_sector_strong(self.frame, allow_missing_ref=True)
        strict = select_sector_strong(self.frame, ref_date_close=20.0)
        self.assertTrue(strict.attrs["ref_date_applied"])
        self.assertTrue(bool(strict[strict].index.isin(weak[weak].index).all()),
                        "带基准的每一条信号都应同时满足弱化版条件")
        self.assertLess(int(strict.sum()), int(weak.sum()),
                        "基准过滤必须砍掉一部分信号，否则等于没加")

    def test_ref_above_all_prices_kills_all(self):
        result = select_sector_strong(self.frame, ref_date_close=9999.0)
        self.assertFalse(bool(result.any()), "基准价高于一切收盘价时应无任何信号")


if __name__ == "__main__":
    unittest.main()
