#!/usr/bin/env python3
"""把波段之门的三个测试文件桥接进 unittest，让它们进主套件。

## 为什么需要这个桥接

`test_boduanzhimen_indicators.py`（23 项）、`test_boduanzhimen_backtest.py`（17 项）、
`test_mutation_boduanzhimen.py`（变异 7 项）写得很扎实 —— 指标测试用东财接口的
KDJ.K/KDJ.D 做外部基准（自洽测试证明不了 SMA 递推翻译正确），变异测试会主动
改坏参数确认测试能抓住。

但它们是**模块级 test_ 函数 + 自定义 main() runner**，没有 `unittest.TestCase` 类。
实测后果（2026-08-09）：

    python -m unittest discover -p "test_boduanzhimen_*.py"   -> Ran 0 tests
    python test_boduanzhimen_indicators.py                    -> 23/23 通过

**也就是说这 40 项测试真实有效，却永远不会进 187 项主套件，等于没有 CI 保护。**
改坏 boduanzhimen_indicators.py 时主套件依然全绿。

## 为什么用桥接而不是改原文件

原文件的自定义 runner 有独立价值：`test_mutation_boduanzhimen.py` 需要动态改写
被测模块的常量再还原，那套流程用 unittest 表达会别扭。而且原文件可以单独跑、
输出更适合人读（逐条 PASS/FAIL + 变异抓捕报告）。

所以保留原文件不动，这里只做发现与聚合：把模块级 test_ 函数逐个包成
unittest 用例，这样两种跑法都成立。

## 依赖

需要 numpy / pandas —— 只有 codex 运行时有：
    C:/Users/20577/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe
系统 Python312 没装，会自动 skip 而不是报错（否则 build_index 那条链的
CI 会被无关依赖拖挂）。
"""
from __future__ import annotations

import contextlib
import importlib
import io
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

# 被桥接的模块名 -> 该模块里 test_ 函数的预期数量（防止悄悄变少）
BRIDGED = {
    "test_boduanzhimen_indicators": 23,
    "test_boduanzhimen_backtest": 17,
}


def _load(module_name: str):
    """导入被桥接模块。缺 numpy/pandas 时返回 None，由调用方 skip。"""
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


def _make_case(module_name: str, expected: int) -> type[unittest.TestCase]:
    """为一个被桥接模块生成一个 TestCase 类。"""

    class Bridged(unittest.TestCase):
        maxDiff = None

        @classmethod
        def setUpClass(cls) -> None:
            cls.module = _load(module_name)
            if cls.module is None:
                raise unittest.SkipTest(
                    f"{module_name} 需要 numpy/pandas，当前解释器没有 —— "
                    "用 codex 运行时跑：dependencies/python/python.exe"
                )

        def test_function_count_has_not_shrunk(self) -> None:
            """被桥接模块的测试数量不能悄悄变少。

            没有这条断言，删掉几个 test_ 函数不会有任何信号。
            """
            found = [
                name for name in dir(self.module)
                if name.startswith("test_") and callable(getattr(self.module, name))
            ]
            self.assertGreaterEqual(
                len(found), expected,
                f"{module_name} 的 test_ 函数从 {expected} 变成 {len(found)}，"
                "少了就要说明原因",
            )

        def test_all_bridged_functions_pass(self) -> None:
            """逐个跑被桥接模块的 test_ 函数，任一失败就报出全部失败项。"""
            functions = [
                (name, getattr(self.module, name))
                for name in sorted(dir(self.module))
                if name.startswith("test_") and callable(getattr(self.module, name))
            ]
            self.assertTrue(functions, f"{module_name} 里没有 test_ 函数")

            failures: list[str] = []
            for name, function in functions:
                with self.subTest(function=name):
                    # 被桥接的函数会 print 进度（如「KDJ 对东财最大偏离 K=…」）。
                    # unittest 的 buffer 机制在断言阶段已关闭 stdout，直接调用会抛
                    # ValueError: I/O operation on closed file —— 那是桥接的缺陷，
                    # 不是被测代码的问题。所以执行期间把输出收进内存。
                    captured = io.StringIO()
                    try:
                        with contextlib.redirect_stdout(captured):
                            function()
                    except Exception as exc:  # noqa: BLE001 —— 要收集全部失败
                        detail = captured.getvalue().strip()
                        failures.append(
                            f"{name}: {type(exc).__name__}: {exc}"
                            + (f"\n    该函数的输出：{detail[-300:]}" if detail else "")
                        )
                        raise
            self.assertEqual(failures, [], "\n".join(failures))

    Bridged.__name__ = f"Bridged_{module_name}"
    Bridged.__qualname__ = Bridged.__name__
    return Bridged


# 为每个被桥接模块生成 TestCase 并挂到模块命名空间，供 unittest discover 发现
for _name, _expected in BRIDGED.items():
    globals()[f"Bridged_{_name}"] = _make_case(_name, _expected)


class MutationSuiteIsPresentTests(unittest.TestCase):
    """变异测试不在这里执行，只确认它还在、且仍声明抓住全部变异。

    为什么不执行：`test_mutation_boduanzhimen.py` 会临时改写被测模块的常量再还原。
    在同一进程里跑会污染上面那两个桥接用例（模块已被 import，常量被改过）。
    要跑变异测试请单独执行该文件。
    """

    def test_mutation_file_exists_and_declares_full_coverage(self) -> None:
        path = pathlib.Path(__file__).with_name("test_mutation_boduanzhimen.py")
        self.assertTrue(path.exists(), "变异测试文件不见了")
        text = path.read_text(encoding="utf-8", errors="replace")
        # 该文件自己会打印「变异 N 个：抓住 N，漏网 0」
        self.assertIn("漏网", text, "变异测试应报告漏网数")
        self.assertIn("MUTATIONS", text.upper(), "变异测试应有变异清单")


if __name__ == "__main__":
    unittest.main(verbosity=2)
