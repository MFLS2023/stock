"""变异测试：故意改坏指标里的阈值，检查测试能否发现。

为什么需要它：2026-08-09 实测发现 18 个测试「全绿」是虚假信心 ——
把 kdj_battle 的阈值 15.5 改成 15.0、把 AND 改成 OR、把布林带的 2 倍标准差
改成 1.5 倍、把翻倍选股的涨停线 1.099 放宽到 1.05，测试一个都察觉不到（0/7）。
那批测试只断言「列名齐全」「三态互斥」这类恒真条件。

补了阈值级断言后现在是 7/7 全抓住。以后改动指标或测试，跑这个脚本确认没退化。

用法：python _知识库系统/scripts/test_mutation_boduanzhimen.py
只在原文件上临时改写并在 finally 里还原，不留副本。

⚠️ **必须用 main() 包起来，不能放在模块顶层。**
2026-08-09 实测：这个文件原来整个是顶层代码、没有 `if __name__` 保护，
于是 `unittest discover` 一 import 它就真的去改写 boduanzhimen_indicators.py、
起子进程跑测试、再还原。后果是主套件里 11 failures + 14 errors，
而同样的测试单独跑却是 23/23 和 17/17 全通过 —— 典型的测试间污染。
文件名以 test_ 开头就会被 discover 收集，所以副作用绝不能放在 import 期。
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path(r'C:\Users\20577\Documents\炒股\知识库\_知识库系统\scripts\boduanzhimen_indicators.py')
BAK = SRC.with_suffix('.py.mutbak')

MUTATIONS = [
    ('kdj_battle 回调阈值 15.5→15.0', 'k - d > 15.5', 'k - d > 15.0'),
    ('kdj_battle M1 的 AND→OR', '(j > 110) & (d > 75)', '(j > 110) | (d > 75)'),
    ('kdj_battle 九分危险 C<MA233 → C>MA233', '(close < ma233)', '(close > ma233)'),
    ('翻倍选股 涨停阈值 1.099→1.05', '> 1.099', '> 1.05'),
    ('沿均线爬升 12根→10根', 'count(rising, 14) >= 12', 'count(rising, 14) >= 10'),
    ('布林带 2倍标准差→1.5倍', 'width: float = 2.0', 'width: float = 1.5'),
    ('KD钝化 连续5根→3根', 'count(values["k"] > 80, 5) == 5', 'count(values["k"] > 80, 3) == 3'),
]

TEST = Path(r'C:\Users\20577\Documents\炒股\知识库\_知识库系统\scripts\test_boduanzhimen_indicators.py')

def main() -> int:
    """跑全部变异。返回 0 表示无漏网且还原成功。"""
    shutil.copy(SRC, BAK)
    original = SRC.read_text(encoding='utf-8')
    caught = missed = 0
    try:
        for name, old, new in MUTATIONS:
            if old not in original:
                print(f'  跳过（未找到目标代码）: {name}')
                continue
            SRC.write_text(original.replace(old, new, 1), encoding='utf-8')
            # 必须清 __pycache__：源文件改了但 .pyc 的 mtime 判定有 1 秒粒度，
            # 快速连续改写会让 Python 复用旧字节码，测出来的是上一轮的变异结果。
            # 这个坑已经让我误判过一次（还原后仍显示 21/23，其实是缓存里的坏版本）。
            shutil.rmtree(SRC.parent / '__pycache__', ignore_errors=True)
            r = subprocess.run([sys.executable, str(TEST)], capture_output=True,
                               text=True, encoding='utf-8', errors='replace')
            ok = '23/23 通过' in (r.stdout or '')
            if ok:
                missed += 1
                print(f'  [漏网] {name}  ← 改坏了但测试仍全绿')
            else:
                caught += 1
                fails = re.findall(r'FAIL\s+(\S+)', r.stdout or '')
                print(f'  [抓住] {name}  ← {fails[:2]}')
    finally:
        SRC.write_text(original, encoding='utf-8')
        shutil.rmtree(SRC.parent / '__pycache__', ignore_errors=True)
        BAK.unlink(missing_ok=True)

    print(f'\n变异 {caught+missed} 个：抓住 {caught}，漏网 {missed}')
    r = subprocess.run([sys.executable, str(TEST)], capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    restored = '23/23 通过' in (r.stdout or '')
    print('还原后测试:', '23/23 通过' if restored else '异常！需检查')
    return 0 if (missed == 0 and restored) else 1


if __name__ == '__main__':
    raise SystemExit(main())
