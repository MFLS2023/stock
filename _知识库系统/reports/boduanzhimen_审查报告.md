# 波段之门代码审查报告

审查对象：`boduanzhimen_indicators.py` / `boduanzhimen_backtest.py` / `import_boduanzhimen.py`
审查日期：2026-08-09
审查方式：对着 20 个原始通达信 txt 逐条核对 + 变异测试 + 全量数据实测
Python：`C:/Users/20577/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`
行情数据：`_知识库系统/tmp/sh000001_daily.csv`（上证日线 6000 根，2001-11-14 ~ 2026-08-07）

**结论摘要：15 个问题（P0×4、P1×7、P2×4）。最严重的是回测脚本方向判定被否定词反转、
docstring 声称已修的过去时误判实际未修、以及 reports/ 里发布的数字来自另一个脚本且与本脚本矛盾。**

---

## P0 级（会算错数 / 丢数据）

### P0-1 方向判定被否定词反转，把「他判断错了」记成「他判断对了」

`boduanzhimen_backtest.py:40-45` 的 `DIRECTION_RULES` 只做正向词匹配，
不检查方向词前面的否定词。「只要没有跌破 3834」中的「跌破」被匹配为**下行**，
于是「他预期不跌破」被记成「他预测会跌到 3834」，指数真跌破后判为**命中**（算他对），
而按原意应是**支撑失效**（算他错）。对错完全颠倒。

复现：

```bash
export PYTHONIOENCODING=utf-8
cd "C:/Users/20577/Documents/炒股/知识库/_知识库系统/scripts"
python - <<'PY'
import boduanzhimen_backtest as B
for t in ["这次不会跌破3980点","大盘不会突破3674点","不会涨到4000点","大概率不会跌到3100点"]:
    m=list(B.POINT.finditer(t))
    print(f"{t:<20} → {B.classify_direction(t, m[0].start())!r}")
PY
```

实际输出：

```
这次不会跌破3980点          → '下行'      ← 应为「下限」
大盘不会突破3674点          → '上行'      ← 应为「上限」
不会涨到4000点             → '上行'      ← 应为「上限」
大概率不会跌到3100点         → '下行'      ← 应为「下限」
```

26 条入选样本里**实际命中 2 条**（不是构造用例）：

```
[2025-09-17] 判为 下行 3834 → 命中     原文「只要没有跌破3834点,就维持这种看法」
   正确语义：3834 是支撑，窗口内最低 3774.53 → 支撑失效（他错了）
[2025-12-30] 判为 下行 3910 → 命中     原文「只要没有跌破3910点,我们都不能肯定…」
   正确语义：3910 是支撑，窗口内最低 3794.68 → 支撑失效（他错了）
```

影响：16 条「命中」里至少 2 条是方向反转造成的错误加分。仅修这 2 条，
命中率从 `16/26 = 61.5%` 降到 `14/26 = 53.8%`。规则里已有「不会超过」「不会跌破」两个否定短语，
但它们排在 `下行`/`上行` 规则**之后**，而 `DIRECTION_RULES` 是顺序匹配、先命中先返回，
所以「不会跌破」永远先被「跌破」吃掉——`上限`/`下限` 规则里的否定短语等于摆设。

建议：在 `classify_direction` 里先做否定检测（点位前 8~10 字内出现 `不会|没有|不能|不至于|难以|只要不`），
命中则把 `下行→下限`、`上行→上限` 翻转；或把 `DIRECTION_RULES` 顺序改为否定模式优先，
并把 `不会跌破|没有跌破|不破` 补进 `下限`、`不会涨到|不会突破|不会达到` 补进 `上限`。

### P0-2 PAST_TENSE 只看点位「前」40 字，docstring 举的那个例子实际没被拦住

`boduanzhimen_backtest.py:150` 只检查 `text[match.start()-40 : match.start()]`。
但中文复述句的过去时词常在点位**之后**（「跌到 3144 点，**跌了**快 300 点了」）。
文件第 64-65 行的注释明确说这条已修：

> 复述已发生的事，不是预测。「从3418跌到3144，跌了快300点了」讲的是过去，
> 首版把它当成"预测跌到3144"并判为命中 —— 这种命中毫无意义，等于用已知答案答题。

实测这条**原封不动地留在结果里，且仍判为命中**。

复现：

```bash
python - <<'PY'
import boduanzhimen_backtest as B
t = "从3418点跌到3144点，跌了快300点了，人家反弹几天不应该吗？"
for m in B.POINT.finditer(t):
    s=m.start()
    print(f"{m.group(0)} 方向={B.classify_direction(t,s)!r} "
          f"前40字PAST命中={bool(B.PAST_TENSE.search(t[max(0,s-40):s]))} "
          f"整句PAST命中={bool(B.PAST_TENSE.search(t))}")
PY
```

实际输出：

```
3418点 方向=''     前40字PAST命中=False 整句PAST命中=True
3144点 方向='下行' 前40字PAST命中=False 整句PAST命中=True
```

回测明细里对应条目：

```
[2023-07-04] 大气一点 | 下行 3144 → 命中
    窗口内最低 3053.04，目标 3144（发文日收盘 3245.35）
    原文：…从3418点跌到3144点,跌了快300点了,人家反弹几天不应该吗?…
```

全量扫描 26 条样本，摘要含过去时词的有 **10 条**（`跌了/涨了/已经/昨天/事实是` 等），
其中 `[2024-06-11] 下行 3016 → 命中` 的原文是「我当初的预想…**事实是提前了**」——
明确的事后复述，也被记为命中。

影响：用已知答案答题，虚高命中率。26 条里 10 条摘要带过去时词，最多可能虚高 10 条。

建议：改为在**点位所在的整句**（按 `。！？` 切句）内检测过去时，而不是固定字符窗口。
`bdzm_backtest.py:105` 已经是按句切分的写法，可直接借用。

### P0-3 reports/ 里发布的回测数字来自另一个脚本，两套数字互相矛盾且无人对账

`_知识库系统/scripts/` 下同时存在两套实现：

| 文件 | POINT 正则 | 方向标签 | 排除机制 | 写报告 |
|---|---|---|---|---|
| `bdzm_backtest.py` | `[12345]\d{3}` 不要求「点」字 | 看涨/看跌 | 单一 `suspect` 标记 | 是，写 `reports/boduanzhimen_预测回测.md` |
| `boduanzhimen_backtest.py`（本次审查对象） | `[2-6]\d{3}...点` 强制「点」字 | 上行/下行/上限/下限 | 四类硬排除 | 否，只打印到 stdout |

同一批语料 + 同一份上证日线，两者结论：

```
bdzm_backtest（已发布报告）:  可判定 101 条，口径清晰 72 条，60日命中 51/72 = 70.8%
boduanzhimen_backtest（本脚本）: 抽出  27 条，去重后  26 条，60日命中 16/26 = 61.5%
```

复现（读已发布报告 + 跑本脚本）：

```bash
grep -E "可判定预测|口径清晰|60 个交易日" "C:/Users/20577/Documents/炒股/知识库/_知识库系统/reports/boduanzhimen_预测回测.md"
python boduanzhimen_backtest.py --index-csv ".../tmp/sh000001_daily.csv" --window 60 | head -12
```

实际输出：

```
- 可判定预测：101 条
- 口径清晰：72 条（下表基于这些）
| 60 个交易日 | 51/72 = 70.8% | 23/29 = 79.3% | 28/43 = 65.1% |
---
抽出显式点位预测 27 条
可检验 26 条
  命中: 16 (61.5%)
```

条数差 3.7 倍，命中率差 9.3 个百分点。根因之一是 POINT 正则：本脚本强制要求「点」字后缀，
漏掉「反弹目标是 3118 附近」「2984 是要跌破的」这类不带「点」字的表述。

```
带「点」字的匹配:      706 处
不要求「点」字的匹配: 2366 处   → 少覆盖约 1660 处候选
```

影响：读报告的人拿到 70.8%，跑脚本的人拿到 61.5%，无法判断哪个是当前口径。
`bdzm_indicators.py` / `boduanzhimen_indicators.py` 同样并存且内容不同（21306B vs 23260B）。

建议：确定唯一权威实现，删除或明确标注废弃另一套；让保留的那个脚本负责写报告，
报告头部写清生成脚本名、生成时间、窗口参数。

### P0-4 `__all__` 导出不存在的 `double_signal`，`import *` 直接崩

`boduanzhimen_indicators.py:32` 的 `__all__` 里有 `"double_signal"`，模块里没有这个函数。

复现：

```bash
python -c "
import sys; sys.path.insert(0,'.')
from boduanzhimen_indicators import *
"
```

实际输出：

```
AttributeError: module 'boduanzhimen_indicators' has no attribute 'double_signal'
```

影响：任何 `from boduanzhimen_indicators import *` 的调用方直接崩溃。
测试文件用的是显式 import 所以 18/18 全过，掩盖了这个问题。
另外模块 docstring 说「去重后 12 个独立公式」，`REGISTRY` 实际 15 条，数字对不上。

建议：删掉 `__all__` 里的 `double_signal`；docstring 的 12 改成 15。

---

## P1 级（逻辑不严谨）

### P1-1 `kdj_battle` 的 6 个阈值可以随便改，测试一个都测不出来

变异测试：改坏 `kdj_battle` 的阈值和逻辑运算符，看测试能否发现。

复现（脚本会自动备份还原）：

```bash
cd "C:/Users/20577/Documents/炒股/知识库/_知识库系统/scripts"
cp boduanzhimen_indicators.py /tmp/bak.py
# 逐个把 15.5→15.0 / AND→OR / 28.3→28.0 / 1.37→1.30 / >6→>5 / C<MA233→C>MA233 后跑测试
python test_boduanzhimen_indicators.py
cp /tmp/bak.py boduanzhimen_indicators.py
```

实际输出（每行一个变异）：

```
漏网    kdj_battle: 回调阈值 15.5→15.0
漏网    kdj_battle: M1 AND→OR
漏网    kdj_battle: 过应阈值 28.3→28.0
漏网    kdj_battle: 跌1 阈值 1.37→1.30
漏网    kdj_battle: 危险 >6→>5
漏网    kdj_battle: 九分危险 C<MA233→C>MA233
```

6/6 全部漏网。原因是 `test_kdj_battle_all_flags_present` 只断言「列名齐全」和
「两个九分危险互斥」，从不检查任何阈值。互斥那条也是恒真断言：
`close > ma55` 与 `close < ma55` 天然互斥，无论公式怎么改都成立。

同一轮变异里其它函数也大面积漏网：

```
漏网    boll_top_risk: MA89→MA55
漏网    boll: 2倍标准差→1.5倍
漏网    select_doubling: 涨停阈值 1.099→1.05
漏网    select_doubling: L==O → L<=O
漏网    select_doubling: 放量2倍→1.5倍
漏网    select_ma_climb: >=12→>=10
漏网    select_ma_climb: 1.18→1.10
漏网    position_line: 6周期→14周期
漏网    volume_price: 连续3天→2天
漏网    volume_price: 连续4天→3天
```

被抓住的只有 7 个：`kdj J公式`、`kd_dull 连续5根`、`boll_top_risk 0.998`、
`position_line SMA M值`、`ma min_periods`、`std_tdx ddof`、`sell_next_day AND→OR`、`eleven_sequence +1`。

影响：这套测试证明的是「代码能跑」和「基础函数对」，不能证明「15 个公式的参数忠实于原文」。
以后有人手滑改了阈值，测试全绿。

建议：给每个选股/信号函数补「已知输入 → 已知输出」的定点断言，
即构造一个刚好卡在阈值边界上的序列，断言阈值内为真、阈值外为假。
`test_select_doubling_matches_pattern` 已经是这个思路，推广到其余函数即可。

### P1-2 三个函数没有任何测试覆盖

复现：

```bash
python - <<'PY'
import re, pathlib, boduanzhimen_indicators as M
src=pathlib.Path("test_boduanzhimen_indicators.py").read_text(encoding="utf-8")
imported=set(re.findall(r"\w+", src.split("from boduanzhimen_indicators import (")[1].split(")")[0]))
print([f for f in M.__all__ if hasattr(M,f) and callable(getattr(M,f)) and f not in imported])
PY
```

实际输出：

```
['ema', 'do_t_ma5_trend', 'select_shock_market', 'select_11_stocks', 'select_sector_strong']
```

其中 `select_shock_market`、`select_11_stocks`、`select_sector_strong` 是三个**独立公式**
（不是 `ema`/`do_t_ma5_trend` 那样的薄封装），一行测试都没有。
`select_11_stocks` 的 `CODELIKE('83')` 排除逻辑、`select_sector_strong` 的
`ref_date_close` 可选参数——正是最容易出错的地方——完全没验证。

建议：至少补三个函数的冒烟测试 + `CODELIKE` 排除断言。

### P1-3 `select_sector_strong` 默认调用静默丢掉一个条件，信号数多出 4 倍

原公式 4 个条件：`C>M55 AND C>A AND MA(C,13)>MA(C,55) AND C>XG*0.8`，
其中 `A:=REFDATE(C,1241008)`。Python 把它做成可选参数，不传就**不检查这个条件**，
只在 `Series.attrs` 里留一行说明。

复现（真实上证 6000 根）：

```bash
python - <<'PY'
import pandas as pd, boduanzhimen_indicators as M
f=pd.read_csv(r"C:\Users\20577\Documents\炒股\知识库\_知识库系统\tmp\sh000001_daily.csv")
f["date"]=pd.to_datetime(f["date"]); f=f.sort_values("date").set_index("date"); f["volume"]=1e6
a=M.select_sector_strong(f)
b=M.select_sector_strong(f, ref_date_close=float(f.loc["2024-10-08","close"]))
print(f"不传基准: {int(a.sum())} 根有信号 | 传基准: {int(b.sum())} 根有信号")
print("2024-10-08 收盘 =", float(f.loc["2024-10-08","close"]))
print("attrs =", a.attrs)
PY
```

实际输出：

```
不传基准: 2665 根有信号 | 传基准: 529 根有信号
2024-10-08 收盘 = 3489.775
attrs = {'ref_date_applied': False, 'ref_date_note': '原公式硬编码 REFDATE(C,1241008)=2024-10-08 收盘价'}
```

默认调用比原公式多出 **2136 根**信号（5 倍）。而 `attrs` 是不可靠的告警载体：

```
放进 DataFrame 后列的 attrs: {}          ← attrs 丢失
```

即 `pd.DataFrame({"sig": result})` 之后警告消失，调用方拿到的是「无声地少一个条件」的结果。

关于任务里问的「1241008 到底是什么格式」：这是通达信的日期数值 `(年份-1900) 拼 MMDD`，
即 `2024-1900=124` 拼 `1008` = `1241008` → 2024-10-08，**结论正确**。
但 docstring 写「通达信的 REFDATE 用 YYMMDD 或带前缀的日期数」，`YYMMDD` 的说法不准确
（`YYMMDD` 读出来是 124 年 10 月 08 日，不合法）。另外函数**根本没有日期解析逻辑**，
参数是 `float` 收盘价，调用方得自己去查那天的收盘价。

建议：把默认值改成必填，或不传时抛异常/返回全 False，不要静默降级。
警告不要放 `attrs`，改成返回 `(Series, meta)` 或打日志。

### P1-4 `judge()` 的「上限/下限」判定在点位贴近现价时没有区分力

`judge()` 对「上限」只看窗口内最高价是否超过，不看指数是否曾接近该点位。
说「不会超过 X」，结果指数根本没往那个方向走，也记为「未破上限」= 他对了。

实测 4 条上限/下限样本，全部距发文日收盘不到 2%：

```
[2025-01-13] 下限 3137 → 支撑失效   发文日收盘 3160.76，目标距现价 -0.75%
[2025-10-16] 下限 3902 → 支撑失效   发文日收盘 3916.23，目标距现价 -0.36%
[2025-12-23] 上限 3936 → 被突破     发文日收盘 3919.98，目标距现价 +0.41%
[2026-02-02] 下限 3936 → 支撑失效   发文日收盘 4015.75，目标距现价 -1.99%
```

本次样本恰好都很近，所以判定尚有意义，但逻辑本身没有下界保护——
换一批数据就会出现「说不会超过 4500，指数在 3000 附近横盘，记为他对了」。
另外这 4 条里 `[2025-12-23] 上限 3936` 的方向判定本身是错的：

```
点位前缀 = '会,是机会。 前提是:过了3936点。 如果不过'
命中规则 = 上限（靠 '不过'）
```

原文「前提是：过了 3936 点」是**看涨的前置条件**，不是上限。
`不过` 在这里是「不超过」还是转折连词「不过，…」无法用前缀正则区分。

建议：给上限/下限判定加「指数是否曾进入该点位 ±N% 范围」的前置条件，
达不到则记为「未构成检验」而不是算他对。`不过` 单独成词时（后跟逗号）应排除。

### P1-5 四类硬排除有明确误杀，且被排除的条目不出现在任何输出里

排除总量 679 处（无方向 647 + 引述 12 + 非上证 12 + 过去时 8），全部只计数不落地，
无法复核。抽查发现三类误杀：

复现：见下方脚本（完整版在审查过程中运行，这里给关键输出）

```bash
python - <<'PY'
import json, boduanzhimen_backtest as B
rows=[json.loads(l) for l in B.CHUNKS.open(encoding="utf-8")]
for r in rows:
    if r.get("chunk_type")!="article_body" or not r.get("date"): continue
    t=r["text"]
    for m in B.POINT.finditer(t):
        s=m.start(); d=B.classify_direction(t,s)
        if not d: continue
        near=t[max(0,s-80):m.end()+40]
        if B.OTHER_SYMBOL.search(near):
            print(f"[{r['date']}] {d} {m.group(1)} 触发={B.OTHER_SYMBOL.search(near).group(0)!r}")
PY
```

实际输出（节选，`非上证标的` 12 条里 9 条是误杀）：

```
[2025-03-09] 上限 3674 触发='板块'   原文「这次不会超过3674点。下面说说板块,本周AI分支…」
[2025-06-05] 上限 3439 触发='板块'   原文「只要不过3439点,那就要先苟着」
[2025-06-11] 上限 3417 触发='板块'   原文「如果今天下午或者后面任何一天冲过3417点,我不会加仓」
[2025-11-07] 上行 4025 触发='个股'   原文「理论上,还有新高机会,甚至会突破4025点。但指数上去,个股不一定上去」
[2024-11-20] 下行 3356 触发='个股'   原文「为什么市场非要跌破3356点呢?」
[2025-11-17] 下行 3980 触发='板块'   原文「最糟糕的一种走势是:跌破3980点」
```

`OTHER_SYMBOL` 命中的是**同段落里另一句话**提到的「板块/个股」，
点位本身讲的就是上证。`板块`/`个股` 这两个词在他的文章里几乎每段都有，当排除词过于宽。

`过去时` 8 条里也有误杀：

```
[2025-06-24] 上行 3439 触发='涨了'  原文「我的计划是,大盘可以涨,等涨过3439点,我才考虑是否加大仓位」
                                    ← 「昨天已经涨了」是上一句，这句是真预测
```

`引述他人` 12 条基本正确（`有的人说上证要跌到2200点…这种概率是1%` 确实该排），
但 `[2025-12-06] 上行 3914 触发='不可能'` 是误杀：
原文「小盘股…**不可能**交出漂亮的预告。下周初,即使还往上走突破3914点,那也是你高抛的机会」，
`不可能` 说的是财报预告，与点位无关。

影响：误杀方向偏向「上限」类（`板块` 一词在他讲完指数后必然出现），
使统计里上限样本只剩 1 条，分方向命中率失去意义（上限 0/1 = 0.0%）。

建议：把排除判定的作用域从「点位前后 80/40 字」收窄到「点位所在的句子」；
`板块`/`个股` 从 `OTHER_SYMBOL` 移除或要求与点位同句且紧邻。
所有被排除条目写到 CSV（带排除原因）供人工复核。

### P1-6 作者连续回复时，637 条（12.8%）被贴上不相关的「读者问」

`import_boduanzhimen.py:233` 的配对逻辑是「往前找第一个非作者发言」。
他在留言区经常连发多条独立发言，这些全部被配到同一条读者留言上。

复现：

```bash
python - <<'PY'
import json, re, pathlib
from collections import Counter, defaultdict
lib=pathlib.Path(r"C:\Users\20577\Documents\炒股\知识库\_知识库系统\source_libraries\boduanzhimen")
ch=[json.loads(l) for l in (lib/"chunks.jsonl").open(encoding="utf-8")]
ar=[x for x in ch if x["chunk_type"]=="author_reply"]
q=re.compile(r"^读者问[:：](.+?)\n作者答[:：](.+)$", re.S)
bydoc=defaultdict(list); tot=0
for x in ar:
    m=q.match(x["text"])
    if m: bydoc[x["document_id"]].append(m.group(1).strip()); tot+=1
dup=sum(sum(v-1 for v in Counter(qs).values() if v>1) for qs in bydoc.values())
docs=sum(1 for qs in bydoc.values() if any(v>1 for v in Counter(qs).values()))
print(f"带提问 {tot} 条，共用同一提问 {dup} 条 ({dup/tot*100:.1f}%)，涉及 {docs} 篇")
PY
```

实际输出：

```
author_reply 总数 4984：带提问 4974 条，无提问(作者留言) 10 条
与同文档内其它回复共用同一提问: 637 条 (12.8%)，涉及 243 篇文章
```

最严重一篇 `boduanzhimen-wx-48ad5f2a6c14`，一条读者留言
（`甘旭田:首先能真正做预测的作者屈指可数…`）被 **12 条**作者回复共用，
其中 11 条与该留言无关：

```
答: '大盘并没有下跌,也没有创出新高,注意那些在今天创出新高的板块…'
答: '有读者问,现在可否中线建仓,当然可以,前提是必须要选好股票…'
答: '大家看看白酒,还记得媒体在中秋节的时候说什么吗?库存如山…'
答: '天啊,泸州老窖涨停,上一次是3年前。指数被白酒带疯了。'
…共 12 条
```

小样本验证（`[2025-10-23-1745]明天注意.md`，原始 md 结构清楚可读）：

```
原始：关昌虎「谢谢提醒」→ 波段之门「我写了好几行字,发出来只有四个字。」
                        → 波段之门「明天理论上有新高…要注意减掉一些仓位」
配对结果：两条都挂到「关昌虎：谢谢提醒」
```

第二条是他的独立盘面判断（有实际信息量），却被标成在回答「谢谢提醒」。

影响：检索按提问召回时拿到答非所问的内容；这 637 条的 `question` 字段是错的，
用它做问答对训练或引用会误导。

建议：只在「作者回复紧跟在读者留言之后」时配对；作者连发的第 2 条起，
`question` 留空并按 `作者留言：` 格式存（代码已有这个分支，第 521 行）。
或者记录「同一读者留言下的第 N 条回复」，让下游能识别。

### P1-7 `chart_ocr_is_useful` 的 `watermark_only` 门槛把「正文+水印」误判为「纯水印」

`import_boduanzhimen.py:299-301` 先去掉水印，再拿剩余长度和**完整的 `min_chars=40`** 比。
水印本身 6~8 字，于是「38 字正文 + 6 字水印 = 44 字」过了第一道 `min_chars`，
去掉水印剩 38 字，`38 < 40` → 判为 `watermark_only` 丢弃。

复现：

```bash
python - <<'PY'
import sys, re; sys.path.insert(0,r"C:\Users\20577\Documents\炒股\知识库\_知识库系统\scripts")
from import_boduanzhimen import chart_ocr_is_useful
t="下周要涨怎么办?下周要跌怎么办?都是一类操作,持股、换股、做T,就是不减仓。公众号波段之门"
c=re.sub(r"\s+","",t); wo=re.sub(r"公众号[·．.]?波段之门|波段之门","",c)
print(f"原文{len(c)}字 去水印{len(wo)}字 → {chart_ocr_is_useful(t)}")
PY
```

实际输出：

```
原文45字 去水印38字 → (False, 'watermark_only')
```

`watermark_only` 命中的全部 12 条里，**8 条是他的真实判断**：

```
明天大概率还是震荡。这个b反,做的好了,小亏。做不好,大亏。做的非常好,小贝公众号·波段之门
下周要涨怎么办?下周要跌怎么办?都是一类操作,持股、换股、做T,就是不减仓。公众号波段之门
所以,下周一怎么走重要吗?不重要。所以,下周怎么走重要吗?不重要。公众号"波段之门
波段之门亻乍者短线的底,大概率会破掉3356,抄底不宜过早。公众号·波段之门13°C1:01
《》波段之门作者kd已过80,明天要小心点了。昨天15:09:09共9条回复凸230口
-波段之门亻乍者接近3816或者稍微破一占10:49:15260就可以皇回etf
```

其中「短线的底,大概率会破掉3356,抄底不宜过早」是带点位的明确判断，直接丢了。

建议：`without_mark` 的长度门槛应该独立设一个更低的值（比如 `min_chars * 0.6`），
或者判定改为「水印占比 > 70% 才算纯水印」。

---

## P2 级（可改进）

### P2-1 `too_short(40)` 门槛误杀 33 条带明确判断的短句

被丢弃的 2695 条里，`too_short` 753 条。抽查发现大量是他写在图上的完整判断，
只是字数在 20~40 之间。

复现：

```bash
python - <<'PY'
import sys; sys.path.insert(0,r"C:\Users\20577\Documents\炒股\知识库\_知识库系统\scripts")
from import_boduanzhimen import chart_ocr_is_useful
for s in ["看到上面的黄线了吗?那里是3090附近,这是这次上涨的第一理论目标。",
          "我们每天的底部是稍微有些移动的,明天的30分底部在3137点。",
          "理论上来说,始于3332的反弹是可以接近3390点的。"]:
    print(f"{chart_ocr_is_useful(s)}  →  放宽min_chars=20: {chart_ocr_is_useful(s,min_chars=20)}")
PY
```

实际输出：

```
(False, 'too_short(34)')  →  放宽min_chars=20: (True, '')
(False, 'too_short(31)')  →  放宽min_chars=20: (True, '')
(False, 'too_short(27)')  →  放宽min_chars=20: (True, '')
```

统计：`too_short` 里语句通顺（含标点 + 含判断词）的有 **33 条**，
含点位且含判断词且非面板的有 2 条。相比之下 `low_cjk`(1165) 和 `empty`(765) 的丢弃是正确的
（实测样本确实是行情软件面板逐字读出的乱码）。

值得注意：`[2024-02-22] 3090` 这条被 OCR 门槛丢掉的图内文字，
在已发布报告的明细里作为正文出现过（`看到上面的黄线了吗?那里是3090附近`），
说明该内容在正文里也有——本例没有实际丢失，但门槛确实会丢只存在于图里的判断。

建议：`min_chars` 降到 20~25，同时保留 `low_cjk` 和 `panel_hits` 两道过滤
（这两道实测有效，是拦住噪声的主力）。

### P2-2 正文块残留微信 UI 文本和外链 URL，URL 占正文 10% 字符

复现：

```bash
python - <<'PY'
import json, pathlib, re
lib=pathlib.Path(r"C:\Users\20577\Documents\炒股\知识库\_知识库系统\source_libraries\boduanzhimen")
body=[json.loads(l) for l in (lib/"chunks.jsonl").open(encoding="utf-8")]
body=[x for x in body if x["chunk_type"]=="article_body"]
URL=re.compile(r"https?://[^\s)\]]+")
n=sum(1 for x in body if URL.search(x["text"]))
chars=sum(len(m) for x in body for m in URL.findall(x["text"]))
allc=sum(len(x["text"]) for x in body)
print(f"含URL的块 {n}/{len(body)}，URL占字符 {chars}/{allc} = {chars/allc*100:.1f}%")
print("含『取消 允许』的块:", sum(1 for x in body if "取消 允许" in x["text"]))
PY
```

实际输出：

```
含URL的块 299/1092，URL占字符 75129/749953 = 10.0%
含『取消 允许』的块: 47
```

`clean_body` 只处理了 `![]()` 图片和 `[X](javascript:void(0);)`，
普通 `[文字](http://mp.weixin.qq.com/s?...)` 外链原样保留。
`取消 允许` 是 `[取消](javascript:...) [允许](javascript:...)` 被替换成纯文字后的产物，
`UI_NOISE` 的备选里没有这两个裸词，所以漏过。

建议：`clean_body` 里把 `[文字](http...)` 替换为 `文字`；`UI_NOISE` 补 `取消|允许|分享|收藏|听过` 的组合。

### P2-3 `select_11_stocks` 返回全 False 时无法区分「被代码排除」和「形态不满足」

`select_11_stocks` / `select_ma_climb` 在代码命中排除时直接 `return pd.Series(False, ...)`，
与「形态不满足」的返回值完全相同，调用方无法区分。
`select_sector_strong` 用了 `attrs` 标注，同模块内两种风格不一致。

另外 `code` 默认 `""`，不传时永不排除：

```
python -c "..."  →  code='' 时 startswith('83') = False → 北交所票混入选股结果
```

通达信里 `CODELIKE` 总能拿到当前股票代码，Python 版把它变成了可选参数。

排除逻辑本身**是正确的**（已验证）：

```
代码      select_11 排除?     select_ma_climb 排除?
830001  True              True     ← 83 开头，两者都排除
430047  True              True     ← 4 开头，只有 select_ma_climb 该排除
600519  True(形态不满足)    False
```

（`select_11_stocks` 对 600519 也返回全 False 是因为构造数据形态不满足，不是排除。）

建议：`code` 改必填，或不传时告警；用 `attrs` 或异常区分排除原因。

### P2-4 `eleven_sequence` 用 `index.astype(str)` 匹配日期，索引带时间/时区就永远匹配不上

`boduanzhimen_indicators.py:496` 的 `frame.index.astype(str) == start_date`
依赖索引字符串化后恰好是 `YYYY-MM-DD`。

复现：

```bash
python - <<'PY'
import pandas as pd, numpy as np, boduanzhimen_indicators as M
a=pd.bdate_range("2025-01-01",periods=30)
variants={"纯日期":a, "带15:00":pd.to_datetime([d.strftime("%Y-%m-%d")+" 15:00:00" for d in a]),
          "带时区":a.tz_localize("Asia/Shanghai")}
c=np.linspace(10,20,30)
for name,ix in variants.items():
    f=pd.DataFrame({"open":c,"high":c,"low":c,"close":c,"volume":1e6},index=ix)
    try: print(f"{name}: OK 起始值={M.eleven_sequence(f, str(a[10].date())).iloc[10]}")
    except ValueError as e: print(f"{name}: ValueError {e}")
PY
```

实际输出：

```
纯日期: OK 起始值=1.0
带15:00: ValueError 起始日期 2025-01-15 不在数据范围内，无法计数
带时区: ValueError 起始日期 2025-01-15 不在数据范围内，无法计数
```

好消息是它**抛异常而不是静默返回错结果**，且项目自己的 `sh000001_daily.csv`
是纯日期索引，实测可用（`2024-10-08` 起始值 1.0，11 根后回到 1.0）。
但任何带时间戳的分钟/小时数据都会失败——而他的公式在通达信里是可以用在任意周期上的。

建议：改成 `pd.Timestamp(start_date).normalize()` 与 `frame.index.normalize()` 比较，
或用 `frame.index.date == pd.Timestamp(start_date).date()`。

---

## 已验证是对的（不用重复验证）

这些我实际跑过并对照原文/外部基准确认无误：

**通达信基础函数语义**
- `sma_tdx(X,N,M)` 递推正确，等价 `ewm(alpha=M/N, adjust=False)`，首值取第一个有效值。
  中间遇 NaN 时跳过该根、用上一个有效值继续递推，与通达信一致。
- `ema(X,N)` 与 `sma_tdx(X,N+1,2)` 数值相同（最大差 2.1e-14），docstring 声明正确。
- `ma` 前 N-1 根 NaN、`hhv/llv` 窗口不足即用现有数据、`every` 窗口不足为假、
  `count` 窗口不足按现有计数 —— 四种边界都与通达信一致。
  `ma(min_periods=n)` 与 `hhv(min_periods=1)` 的不一致是**故意且正确的**，不是 bug。
- `barslast` 首根成立返回 0，从未成立返回 NaN；`barslastcount` 连续计数、中断归零。都正确。
- `std_tdx` 用 `ddof=1`（样本标准差），与通达信 STD 一致，且测试能抓住改成 `ddof=0`。

**KDJ 对外部基准**
- `kdj()` 与东财 `index_prices(000001)` 的 KDJ.K/KDJ.D 逐根比对 60 根，
  最大偏离 K=0.069、D=0.333，在 0.5 容差内。这是全套测试里唯一有外部基准的验证，
  也是最有价值的一条——它证明了 `sma_tdx` 的翻译是对的。

**公式与原文的逐条核对**（对着 20 个 txt 核过）
- `position_line` = `SMA(MAX(C-A,0),6,1)/SMA(ABS(C-A),6,1)*100`，5 条参照线 16/80/91/97.8 齐全，
  取值恒在 0~100，横盘（分母 0）时返回 NaN 且 `buy_zone` 不误报。
- `kd_dull` = `COUNT(K>80,5)=5 AND C>MA(C,55)`，与《3219点的目标位》《KD钝化指标》两文一致
  （两文公式确实完全相同）。真实上证 6000 根出现 650 次。
- `select_doubling` 的 T1/T2/T3 三条件与原文逐字对应（`REF(C,2)/REF(C,3)>1.099`、
  `REF(V,1)>2*REF(V,2) AND REF(C,1)<REF(O,1)`、`C>REF(C,1) AND L=O`）。
- `select_ma_climb` = `COUNT(A,14)>=12 AND C/LLV(L,14)>1.18` + 排除 8/4 开头，与原文一致。
- `macd_zero_cross` = `REF(A,1)<=0 AND A>0`，且测试逐根验证只在由负转正那根为真。
- `sell_next_day`、`boll_top_risk`、`volume_price_rule`、`ma_slope_color`(5/25/1597)、
  `strength_lines`(13/34/55/89) 均与原文一致。
- `select_shock_market` 里 `M21>M1`（强度均线比价格均线）这个量纲问题**是原公式就有的**，
  代码按原样实现并在 docstring 标注，处理方式正确。
- `CODELIKE('83')` → `startswith("83")`、`CODELIKE('8')/('4')` → `startswith(("8","4"))`，
  前缀匹配语义正确。

**回测**
- `POINT` 正则实测无年份误判：`2025年3月` 不匹配、`涨了3个点` 不匹配、`0.5点` 不匹配。
  真实语料 706 处匹配里仅 1 处可疑（`我们计算的3883点位`）。
  最终 26 条样本的点位全在 2000~4127，符合上证历史范围。
- `judge()` 的窗口边界处理正确：发文日不在数据区间时标「无数据」而非用数据起点硬算；
  实测 26 条全部落在覆盖区内，0 条窗口不足 60 根。
- 去重键 `(date, point, direction)` 本次仅消除 1 条重复，影响小。
  同一天不同点位算两条（如 `2026-02-02` 的下限3936 + 下行3936）是合理的，
  但那两条恰好点位相同方向不同，反映的是同一段行情的两种表述。
- 窗口敏感性已实测并与脚本声明一致：20日 42.3% → 60日 61.5% → 120日 65.4% → 250日 69.2%。
  脚本结尾的免责声明（「这是触及率不是准确率」）是诚实的。

**导入适配器**
- 数据完整性**完全自洽**：documents 849 / parents 1650 / chunks 7169，
  0 个 orphan chunk（parent_id 全部存在）、0 个 orphan parent、
  document_id/parent_id/chunk_id 均无重复、0 个空 parent、`_units` 内部字段无泄漏。
  4 个无 parent 的 document 全是 `duplicate_of` 非空的重复文件，按设计不建块，正确。
- `split_body_and_comments` **没有误截问题**：723 篇 md 里
  `预览时标签不可点` 充当边界 505 篇、`微信扫一扫` 171 篇、47 篇无标志走全文。
  「精选留言」出现在其它标志之前的文件数 = **0**，即任务里担心的
  「正文本身出现精选留言导致截断」实际不存在。正文最短的几篇
  （`明天注意` 11 字）核对原始 md 后确认原文就是「一句话+图」，不是被截断。
- `is_avatar` 判定**可靠**：1565 张 avatar_or_ui 里 1451 张是 240x240
  （微信留言头像标准尺寸，占 93%），另有 128x128/96x96 等。
  靠「最短边<=40」杀掉的 55 张是宽高比 25~37 的极扁条（微信点赞/在看图标条），
  K 线截图不可能是这个形状。PIL 打不开时返回 False（当作 chart 送 OCR），偏保守，方向正确。
- `chart_ocr_is_useful` 的 `low_cjk`(1165) 和 `empty`(765) 两类丢弃**判断正确**，
  抽样确认是行情软件面板逐字读出的乱码（如
  `A股成交B股成交国债成交基金成交…11:2811:2811:28`）。`panel_hits>=2 且无句读` 这条设计合理。
- OCR 缓存的「命中缓存时用当前门槛重判」设计正确（门槛调整后旧缓存不必重跑，也不会绕过过滤）。
  实测 3458 个图片缓存全部带 `kept` 字段。
- `real_image_suffix` 按文件头判格式（不信扩展名）、PDF 渲染的 `-scale-to` 像素上限、
  重复 md 的 `-dupN` 后缀处理 —— 三处设计都合理。

---

## 修复优先级建议

1. **P0-1 方向反转** —— 直接导致对错颠倒，改动最小收益最大（在 `classify_direction` 加否定检测）。
2. **P0-2 过去时窗口** —— 改成按句检测，可复用 `bdzm_backtest.py` 的切句写法。
3. **P0-3 两套脚本** —— 需要人决策保留哪个，不是纯代码问题。
4. **P0-4 `__all__`** —— 一行删除。
5. **P1-6 回复配对** —— 影响 637 条已入库数据，修完需重跑导入。
6. **P1-1/P1-2 测试补强** —— 不修则以后任何改动都无保护。
7. 其余按标号处理。

**注意 P0-1/P0-2/P1-5 修完后命中率会变**，26 条里有 12 条（46%）带疑问标记。
样本量本身太小（26 条覆盖 3 年、723 篇文章），修完也不建议对外给单一百分比数字，
脚本结尾那段免责声明应当保留。
