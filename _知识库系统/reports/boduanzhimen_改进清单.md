# 波段之门 skill 改进清单

体检时间：2026-08-09
被检对象：
- `C:\Users\20577\.claude\skills\boduanzhimen-perspective\SKILL.md`（341 行 v1.0.0）
- `_知识库系统/scripts/query_kb.py`、`boduanzhimen_indicators.py`、
  `boduanzhimen_backtest.py`、`import_boduanzhimen.py`
- `_知识库系统/config/synonyms.yaml`

每条都跑过命令验证，命令和输出附在条目里。Python 解释器统一用
`C:/Users/20577/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`
（下称 `PY`），调用前 `export PYTHONIOENCODING=utf-8`。

**统计：P0 阻塞 7 条、P1 重要 7 条、P2 优化 5 条，共 19 条。**

最严重的三条：
1. **P0-7** 检索词表缺 35 个本库核心词，用户的自然提问大面积返回「未找到」
2. **P0-1** 两套指标模块并存、`strength_lines` 同名反签名，报错信息完全看不出原因
3. **P0-3** 拒答清单里的 63.9% 是不存在的数字（实测 38.5% / 42.3% / 61.5%）

---

## 第一部分：SKILL 文档与实际不符（全部 P0）

这一节是任务要求单独列出的部分。每条都是 AI 照 SKILL 说的做就会出错或说错话。

### P0-1　两套指标模块并存，函数名和签名都不一样，SKILL 只提了一个

现象：`scripts/` 下同时有 `bdzm_indicators.py`（21306 字节）和
`boduanzhimen_indicators.py`（23260 字节），函数名几乎全不同：

```
$ PY -c "...inspect.getmembers..."
OLD bdzm_indicators.strength_lines : (close: 'pd.Series', index_close: 'pd.Series')
NEW boduanzhimen_indicators.strength_lines: (frame: 'pd.DataFrame', index_close: 'pd.Series')

只在旧模块: BARSLAST COUNT CROSS EMA EVERY HHV LLV MA REF SMA STD
            double_multiple_pick kd_dunhua kdj_battle_signals ma_climb_pick
            ma_trend_color macd_golden_cross position_index sector_strong_pick
            sell_signal_ma5_pierce shock_market_pick
只在新模块: barslast count ema every hhv llv ma ref sma_tdx std_tdx boll
            do_t_ma5_trend kd_dull kdj_battle macd macd_zero_cross ma_slope_color
            position_line select_11_stocks select_doubling select_ma_climb
            select_sector_strong select_shock_market sell_next_day
同名但签名不同: strength_lines
```

回测脚本也有两份：`bdzm_backtest.py`(14207) 与 `boduanzhimen_backtest.py`(14067)。

根因：改名重构时没删旧文件，两份都留在 `scripts/` 里。
`test_boduanzhimen_indicators.py` 只测新的，旧的没有测试守着。

危害：`strength_lines` 两边同名而参数类型相反。实测传错会抛
`ValueError: If using all scalar values, you must pass an index` —— 这个报错
完全看不出是模块选错了，不懂编程的用户拿到这条只能卡住。
另外 SKILL 第 173 行写 `kd_dull`，而旧模块里叫 `kd_dunhua`，AI 若先 grep 到旧文件
就会给用户一个不存在于新模块的函数名。

建议改法（改代码的 agent 做）：
1. 确认 `bdzm_indicators.py` / `bdzm_backtest.py` 无人引用后删除
   （先 `grep -rn "bdzm_indicators\|bdzm_backtest" 知识库/`）
2. 在 SKILL 第 168 行那句路径后加一行：
   `**只有 boduanzhimen_indicators.py 是现行版本。**`
3. SKILL 第 190-196 行的用法示例里，把 `strength_lines` 的注释从
   `# index_close 是同期大盘收盘价 Series` 改成
   `# 第一个参数收整个 DataFrame（不是 close 列），第二个才是大盘收盘价 Series`

改哪个文件：删 `_知识库系统/scripts/bdzm_indicators.py`、
`_知识库系统/scripts/bdzm_backtest.py`；改 `SKILL.md` 第 168、190-196 行

---

### P0-2　「平均每篇 44 张图」是错的，真实是 7.4 张

现象：SKILL 第 93 行写「这批语料的固有性质：平均每篇 44 张图」。

实测：
```
$ PY -c "读 image_map.jsonl 按 document_id 聚合"
总图 5283 kind {'chart': 3718, 'avatar_or_ui': 1565}
涉及文档 715 平均每文档 7.4 张
chart 平均每文档 5.2

$ PY -c "原始目录 .jpg 计数"
原始目录 .jpg 总数: 5914 / 723 篇 = 8.2
```

根因：44 这个数最接近 `26658 / 723 = 36.9`，即 `onboarding_report.md` 第 9 行的
「DOCX内嵌图片较多:总计26658」——那是把同一篇文章的 4 种格式副本
（.md/.html/.mhtml/.docx/.pdf）里的图重复计数了，而入库只按 .md 算一份。

危害：AI 会对用户说「他每篇有 44 张图，文字版信息缺失极严重」，
把 78.8% 的〔图〕占比夸张成几乎无法使用，用户可能因此放弃这个库。
真实情况是每篇 5.2 张 K 线图，配 78.8% 的正文块含〔图〕—— 缺失是真的但没那么夸张。

建议改法：SKILL 第 93 行改成
`这批语料的固有性质：平均每篇 7.4 张图（其中 K 线图 5.2 张），1092 个正文块里 861 块（78.8%）含〔图〕，`

改哪个文件：`SKILL.md` 第 93 行

---

### P0-3　「63.9% 的命中率」这个数字任何窗口都跑不出来

现象：SKILL 第 305 行写
`❌ 把 63.9% 的命中率当成对未来的概率承诺`，
而同一份文档第 232-240 行的实测表里三个窗口是 38.5% / 42.3% / 61.5%。

实测三个窗口全跑了一遍：
```
$ PY _知识库系统/scripts/boduanzhimen_backtest.py --index-csv _知识库系统/tmp/sh000001_daily.csv --window 10|20|60 --show 0
window=10  命中 10/26 = 38.5%
window=20  命中 11/26 = 42.3%
window=60  命中 16/26 = 61.5%
（三次的抽出/排除/可检验条数完全一致：抽出 27 → 去重 1 → 可检验 26）
```
26 条里没有任何整数组合能得到 63.9%（16/25=64.0% 最近但可检验数是 26）。

根因：首版回测没排除四类假预测（SKILL 第 226 行自己写了「首版没排除，数字虚高一倍多」），
63.9% 是那一版的残留，改数时漏了第 305 行这一处。

危害：这是**拒答清单里的数字**，AI 复述禁令时会把 63.9% 一起说出去，
等于用一个不存在的数字去警告用户。用户没法自己发现。

建议改法：SKILL 第 305 行改成
`- ❌ **把 61.5% 当成对未来的概率承诺** —— 那是 26 条样本在 60 日窗口下的触及率，换成 10 日窗口只有 38.5%`

改哪个文件：`SKILL.md` 第 305 行

---

### P0-4　「12 个指标」与「15 个」自相矛盾

现象：同一份 SKILL 里三个数字：
- 第 49 行：`boduanzhimen_indicators.py`（15 个已实现，可直接跑）
- 第 167 行：20 个通达信公式文件去重后 15 个已翻成 Python
- 第 292 行：**他的 12 个指标怎么算怎么用**

实测：
```
$ PY -c "数 SKILL 表里的函数 + 逐个 hasattr 检查"
SKILL 表里列的函数数: 15
表里列了但模块里没有: []
$ ls C:\Users\20577\Documents\炒股\波段之门\*.txt | wc -l
20（其中 公式源码.txt 和 250315沿着均线爬升加强版公式.txt 各重复一次 → 去重 18 个文件名，19 个入库为 indicator_formula 文档）
```
15 是对的，12 是错的。

建议改法：SKILL 第 292 行 `他的 12 个指标` 改成 `他的 15 个指标`

改哪个文件：`SKILL.md` 第 292 行

---

### P0-5　KDJ 外部比对说「60 根」，实际只比了后 40 根

现象：SKILL 第 207 行写
`其中 KDJ 用东财上证指数 60 根真实数据逐根比对，最大偏离 K=0.069、D=0.333`。

实测跑测试，脚本自己打印的是 40 根：
```
$ PY _知识库系统/scripts/test_boduanzhimen_indicators.py
  PASS  test_kdj_against_eastmoney_reference
      (KDJ 对东财最大偏离 K=0.069 D=0.333，40 根逐根比对)
18/18 通过
```
读测试源码确认（`test_boduanzhimen_indicators.py` 第 165 行附近）：
数据是 60 根（2026-05-15~08-07），但 `for offset in range(20, len(rows))` 跳过前 20 根
让 SMA 递推收敛，**实际逐根比对的是后 40 根**。

危害：不算严重，但这是 SKILL 里唯一一处「外部基准验证」的证据，
数字说错会让整段可信度打折。而且偏离值 K=0.069/D=0.333 是对的，只有根数错。

建议改法：SKILL 第 207 行改成
`其中 KDJ 用东财上证指数 60 根真实数据（跳过前 20 根让 SMA 递推收敛，逐根比对后 40 根）`

改哪个文件：`SKILL.md` 第 207 行

---

### P0-6　六个「块数」全部对不上，口径没写清

现象：SKILL 第 136-138 行和第 156 行给了一批块数。逐个复核：

```
$ PY -c "按 chunk_type 分别统计 text LIKE 命中"
词      SKILL声称  全部块  正文  回复  付费  图OCR
日线        228     415   228   105    54    28
周线        148     271   148    51    46    25
月线         65     135    65    22    36    12
季线         11      19    11     5     2     1
分时         88     138    88    21    28     1
板块        671     763   351   320    56    36
仓位        396     470   257   139    53    21
```

根因：日线/周线/月线/季线/分时五个数是**只数 `article_body`** 的结果，
而 SKILL 第 137 行写的是「语料覆盖：日线 228 块」，读起来像全库。
`仓位 396（正文 257 + 回复 139）` 这个拆分是对的，但 257+139=396 ≠ 470（漏了付费 53 + 图 21）。
`板块 671` 无论怎么组合都对不上（正文 351 + 回复 320 = 671 —— 找到了，也是漏付费和图）。

危害：AI 引用这些数字给用户看「他讲得多不多」时会低估 30-60%。
更麻烦的是口径不一致：同一份文档里 `author_reply 4984` 是全库口径，
`日线 228` 是正文口径，AI 无法判断哪个是哪个。

建议改法：SKILL 第 136-138 行改成（补上口径）
```
季线 → 月线 → 周线 → 日线 → 分时，逐级收敛。**正文块**覆盖：日线 228、
周线 148、月线 65、季线 11、分时 88；算上作者回复和付费文章分别是
415 / 271 / 135 / 19 / 138。
```
第 156 行 `仓位在语料里出现 396 块（正文 257 + 回复 139）` 改成
`仓位在语料里出现 470 块（正文 257 + 回复 139 + 付费 53 + 图内 21）`
第 297 行 `板块 671 块` 改成 `板块 763 块`

改哪个文件：`SKILL.md` 第 136-138、156、297 行

---

## 第二部分：检索层问题

### P0-7　`query_kb.py` 的切词词表缺 35 个波段之门核心词，用户的自然提问查不到

现象：用户按自己的说法提问，大量返回「未找到匹配结果」。实测：

```
$ PY _知识库系统/scripts/query_kb.py "大盘走到哪一步了" --source boduanzhimen --limit 2
未找到匹配结果。

$ PY _知识库系统/scripts/query_kb.py "趋势反转" --source boduanzhimen --limit 3 --expand
未找到匹配结果。

$ PY _知识库系统/scripts/query_kb.py "个股趋势反转开始主升怎么高抛低吸" --source boduanzhimen --limit 2
整句无结果，已按术语切词重试：低吸        ← 只切出「低吸」，主升/高抛低吸/趋势反转全丢
```

根因：`query_kb.py` 第 774-783 行的 `FALLBACK_TERMS` 加上 `synonyms.yaml` 汇总
共 157 个词，是按短线情绪周期体系（复利杯/南京路/爱在冰川）建的，
**没有一个波段之门的核心词**。逐个核对：

```
$ PY -c "import query_kb; vocab=set(query_kb._fallback_vocabulary()) ..."
切词词表大小: 157
缺失且在本库有内容的词（块数）：
大盘 574、反弹 794、底部 426、日线 415、个股 406、顶部 300、新高 307、
周期 283、周线 271、减仓 219(在表内)、结构 197、KD 181、抄底 153、
分时 138、做T 135、月线 135、支撑 85、高抛低吸 72、翻倍 63、逃顶 55、
主升 52、RSI 48、强度 43、破位 41、震荡市 33、目标位 30、时间周期 25、
变盘日 20、季线 19、洗盘 14、钝化 11、防线 6、三浪三 5、风险 207、压力 53
```
`趋势反转` 是真的 0 块（用户的书面说法，他从不这么写），这一条要靠同义词
映射到 `变盘`(83) / `转折`(47) / `见底`(204)，不是补词表能解决的。

危害：这是**最严重的一条**。用户的两个核心需求（#20「我有具体问题问 ai，
ai 用他的内容回答我」）第一步就断了。用户不懂编程，看到「未找到匹配结果」
只会以为库里没有，而实际上 `大盘` 有 574 块。

建议改法：
1. 在 `query_kb.py` 第 783 行 `"满仓",` 之后追加一批词（长词在前，
   `_fallback_vocabulary()` 已按长度降序排，直接加即可）：
   ```python
   # 波段之门（时间周期 + 多周期结构体系）的高频词。2026-08-09 实测块数见
   # evals/boduanzhimen_自测题.md 附表；补这批词之前「大盘走到哪一步了」返回 0 条。
   "高抛低吸", "时间周期", "变盘日", "目标位", "震荡市", "季线", "月线",
   "周线", "日线", "分时", "均线", "大盘", "个股", "主升", "做T", "反弹",
   "见底", "底部", "顶部", "结构", "周期", "抄底", "逃顶", "支撑", "压力",
   "破位", "新高", "洗盘", "翻倍", "强度", "钝化", "防线", "风险",
   "三浪三", "KD", "RSI",
   ```
2. 在 `config/synonyms.yaml` 的 `groups` 下新增（把用户的书面问法映射到他的用词）：
   ```yaml
   趋势反转:
     canonical: 变盘
     variants: [转折, 见底, 反转, 变盘日, 拐点]
     note: 趋势反转是用户书面问法，波段之门 0 命中；他写「变盘」(83)「转折」(47)
   多周期:
     canonical: 周线
     variants: [季线, 月线, 日线, 分时, 60f, 30f]
     note: 他判断按季→月→周→日→分时逐级收敛，问「哪个级别」要一起查
   目标位:
     canonical: 目标位
     variants: [理论位置, 目标, 防线, 支撑, 压力]
     note: 目标位仅 30 块，他也写「理论位置」「第一道防线」
   ```
3. 补完后重跑 `python scripts/check_synonyms.py` 更新 `synonyms.yaml` 头部的
   `measured_at` 和命中数注释（现在写的是 `2026-08-08` / `9175 块`，
   而实测全库已是 **17869 块**，说明这个表在波段之门+爱在冰川接入后没重测过）

改哪个文件：`_知识库系统/scripts/query_kb.py` 第 783 行后；
`_知识库系统/config/synonyms.yaml`（新增 3 组 + 更新头部计数）

验证方式（改完必须跑）：
```bash
for q in "大盘走到哪一步了" "个股趋势反转开始主升怎么高抛低吸" "怎么判断变盘时间"; do
  PY _知识库系统/scripts/query_kb.py "$q" --source boduanzhimen --limit 3
done
# 三条都必须返回结果，不能出现「未找到匹配结果」
```

---

### P1-1　`--expand` 对这个库基本无效，SKILL 却把它写进标准命令

现象：SKILL 第 57 行的标准检索命令带 `--expand`，但同义词表是为短线体系建的。

实测逐词核对 `synonyms.yaml` 全部 142 个词条在本库的命中：
```
$ PY -c "遍历 synonyms.yaml 各组，逐词数 boduanzhimen 命中"
同义词表词条总数 142，其中在波段之门 0 命中 23 条 (16%)

零命中的整组或近乎整组：
  情绪周期  情绪周期=0 冰点=9 高潮=6 分歧=19 退潮=14 情绪冰点=1
  涨停归因  动因=1 涨停动因=0 涨停原因=0
  监管异动  异动=12 监管=5 特停=0 卡异动=0 被关小黑屋=0
  高送转    高送转=0 送转=0 填权=1 除权=1 预增=5
  买卖计划  推演=2 卖点=29 预案=4 计划=162 买卖计划=0
  资金体量  资金体量=0 流动性=8 大资金=18 小资金=6
```
反例：`情绪周期` 查询会被扩展成 `+ 冰点、高潮、分歧、退潮、情绪冰点`，
实测第一条命中是 `[2025-02-28] 亏了个大的` 的一句
「读者问:小鹅通怎么没有看到老师发的周日文章? 作者答:情绪冰点就是啊!」
—— 这是他在开玩笑说自己没更新，不是在讲情绪周期。

根因：同义词扩展是 OR 语义只加词不替换，扩进来的词在本库要么 0 命中（无害），
要么命中的是不相关语境（有害，会挤掉真正相关的块）。

危害：不是崩溃级，但会让 AI 拿着不相关的块去回答，比返回 0 条更难发现。

建议改法：SKILL 第 55-58 行的命令块后加一段：
```
`--expand` 的同义词表是为短线情绪周期体系（复利杯 / 南京路 / 爱在冰川）建的，
142 个词条里有 23 条在本库 0 命中。**问他的时间周期、多周期结构、指标读数时
不要加 `--expand`**，会把「情绪冰点」这类不相关块扩进来；只在查
仓位 / 止损 / 量价这些跨来源共用的词时加。
```

改哪个文件：`SKILL.md` 第 55-58 行之后

---

### P1-2　用户不懂编程，回测那节要求他自己准备 CSV，而 CSV 其实已经有了

现象：SKILL 第 245-248 行：
> 取指数数据的方式（脚本不自带下载，要先备好 CSV）：
> 用 `mcp stock_data index_prices(symbol="000001", limit=1600)` 拿上证日线，
> 存成含 `date,open,close,high,low,volume` 列的 CSV。

而第 217-219 行的命令里 `--index-csv <上证日线csv>` 是个占位符。

实测：文件早就在了，而且**列名和 SKILL 说的不一样**（没有 volume）：
```
$ head -3 _知识库系统/tmp/sh000001_daily.csv
date,open,high,low,close
2001-11-14,1615.752,1627.972,1614.593,1617.102
$ wc -l < _知识库系统/tmp/sh000001_daily.csv
6001
```
用它直接跑通了（覆盖 2001-11-14~2026-08-07，6000 根）：
```
$ PY _知识库系统/scripts/boduanzhimen_backtest.py --index-csv _知识库系统/tmp/sh000001_daily.csv --window 60 --show 0
可检验 26 条，0 条发文日期不在行情数据覆盖范围内
命中: 16 (61.5%)
```

根因：写 SKILL 时按「脚本不自带下载」的事实推导出「用户要自己准备」，
没检查 tmp 目录里已经有一份。

危害：用户明确说过自己不懂配置（#5「我的通达信我还没有完全按照去弄」、
#26、#69「所以板块的我要怎么弄呢，怎么安装」）。让他去调 MCP、
拼 CSV、对齐列名，这一步他做不到，等于这个功能对他不存在。

建议改法：SKILL 第 215-219 行的命令块整段替换成可直接粘贴的版本：
```bash
cd "C:\Users\20577\Documents\炒股\知识库"
python _知识库系统/scripts/boduanzhimen_backtest.py \
  --index-csv _知识库系统/tmp/sh000001_daily.csv --window 60
```
第 245-248 行改成：
```
上证日线 CSV 已备好在 `_知识库系统/tmp/sh000001_daily.csv`
（6000 根，2001-11-14~2026-08-07，列 `date,open,high,low,close`，无 volume 列，
脚本不需要 volume）。**用户不需要自己下载任何数据。**
要更新到最新交易日时才用 `mcp stock_data index_prices(symbol="000001", limit=1600)` 重取。
注意 `live-market` 的 `get_daily_kline_cached("000001")` 返回的是平安银行不是上证指数。
```

改哪个文件：`SKILL.md` 第 215-219、245-248 行

---

### P1-3　SKILL 说系统 python 跑不了，实测能跑；反而没说清用哪个解释器

现象：SKILL 里所有命令都写 `python ...`，没说是哪个 python。
本机有两个：系统 `C:\Users\20577\AppData\Local\Programs\Python\Python312\python.exe`
和 codex runtime 那个。

实测系统 python 三个脚本全跑通：
```
$ which python
/c/Users/20577/AppData/Local/Programs/Python/Python312/python
$ python -V
Python 3.12.10
$ for m in yaml pandas numpy pypdf docx PIL; do python -c "import $m"; done
yaml OK / pandas OK / numpy OK / ModuleNotFoundError: pypdf / docx OK / PIL OK
$ python _知识库系统/scripts/test_boduanzhimen_indicators.py   → 18/18 通过
$ python _知识库系统/scripts/boduanzhimen_backtest.py ...      → 正常输出
$ python _知识库系统/scripts/query_kb.py "情绪周期" --source boduanzhimen --expand → 正常
```
系统 python **只缺 pypdf**，而 pypdf 只有导入器 `import_boduanzhimen.py` 用得到
（重新导入语料才需要），日常查库/跑指标/跑回测都不需要。

根因：没验证过，凭「缺依赖」的印象。

危害：用户按 SKILL 复制命令时能跑通（这是好事），但如果哪天报错，
他不知道该换哪个解释器。而 AI 也可能画蛇添足地让他去装依赖。

建议改法：在 SKILL 第 53 行「Step 2：查库」的命令块前加一行：
```
命令里的 `python` 用系统那个即可（Python 3.12.10，yaml/pandas/numpy 都在，
实测能跑本 SKILL 提到的全部脚本）。只有**重新导入语料**才需要 pypdf，
那时用 `C:/Users/20577/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`。
```

改哪个文件：`SKILL.md` 第 53 行附近

---

### P1-4　这个 SKILL 在项目里没有源文件，`sync_skills.py` 管不到它

现象：
```
$ PY _知识库系统/scripts/sync_skills.py
来源 10 个 SKILL，目标 C:\Users\20577\.claude\skills
✓ 全部已同步，无需操作

$ PY -c "对比 .agents/skills + _导师试验/skills 与 ~/.claude/skills"
.agents/skills:   cross-source-synthesizer kb-ask market-data-toolbox
                  market-evidence-verifier trading-journal-reviewer
                  trading-knowledge-tutor trading-source-curator
_导师试验/skills:  aizaibingchuan-perspective nanjinglu-bian-perspective
                  yujinxiang-perspective
~/.claude/skills 里但项目里没有源的:
   boduanzhimen-perspective   ← 就是它
   codex-collaboration fetch-community grill-me nuwa-skill panfeng-trading
```
另三个导师 skill 都在 `_导师试验/skills/` 下有源文件，只有波段之门没有。

根因：新建时直接写到了 `~/.claude/skills/`，跳过了项目目录。

危害：`sync_skills.py` 的文档（该文件第 13 行）自己写过
「这个坑已经咬过一次：2026-08-08 发现 ~/.claude/skills/ 里的两个导师 SKILL
（内容已过期）」。现在波段之门这份不在版本管理范围内，重装 Claude Code、
清 `~/.claude`、或误删都会直接丢失，且没有备份对照。

建议改法：
1. 把 `~/.claude/skills/boduanzhimen-perspective/` 整个复制到
   `_知识库系统/../_导师试验/skills/boduanzhimen-perspective/`（与另三个导师同级）
2. 之后所有修改改项目里那份，再 `python _知识库系统/scripts/sync_skills.py --apply`
3. 跑完确认 `sync_skills.py` 输出里出现 `boduanzhimen-perspective`

改哪个文件：新建 `_导师试验/skills/boduanzhimen-perspective/SKILL.md`（复制）

---

### P1-5　付费文章带盗版分发水印，SKILL 没警告

现象：付费块正文里混进了第三方转售站的水印。实测：
```
$ PY -c "统计水印词命中"
idown  195 块
xxhgp   12 块
```
样例（`boduanzhimen-paid-3fcfc97c7728-p001-c01`，翻倍选股方法）：
> `公众号 : iDOWN 分享翻选股方法主站 : idown.top 此文只发送给了几百、`

样例（`boduanzhimen-paid-8b768011e090`，`250223+瑞+这波行情的规律`）：
> `订阅 v °C xxhgpl 1 13 号两次冲板 LTIk1vx: xxhgp 所以 , 我说上图黄线里面的...`

第二个还有更麻烦的问题：这篇标题带 `+瑞+` 前缀、日期未标注、
讲的是「分时承接」「核心人气股」「板块首次分歧」这类短线打板语言，
与他本人「我只做周线级别的行情」的风格明显不同 —— **很可能不是他的文章**，
是转售包里混进来的别人内容。

危害：
1. AI 会把 `iDOWN 分享` `主站 idown.top` 当正文引用给用户
2. 更严重的是把别人的文章当成他的观点，用户拿去当他的方法用
3. SKILL 第 341 行写了「他的资料是付费内容，使用范围限于本机个人研究」，
   但没说语料本身来自盗版渠道且可能掺了别人的东西

建议改法：在 SKILL 第 313-315 行（付费文章的数据边界）后追加：
```
- **付费文章来自第三方转售包，正文混有分发水印**：`iDOWN 分享` / `主站 idown.top`
  （195 块）、`xxhgp` / `订阅`（12 块）。这些不是他写的字，引用时要剔掉。
- **付费包里可能掺了别人的文章**。`250223+瑞+这波行情的规律，分时承接的深度理解`
  （`boduanzhimen-paid-8b768011e090`，日期未标注）讲分时承接、核心人气股、
  板块首次分歧，是短线打板语言，与他「我只做周线级别」的自述不符。
  引用日期未标注的付费块（共 55 块）前先看风格是否像他。
```

改哪个文件：`SKILL.md` 第 315 行之后

---

### P1-6　付费文章 OCR 错字比 SKILL 说的严重得多，且没给可核对的例子

现象：SKILL 第 313-315 行只举了两个例子（「翻倍选股」→「翻选股」、
`000523`→`000S23`）。实测检索时几乎每条付费块都在掉字：

```
$ PY _知识库系统/scripts/query_kb.py "高抛低吸" --source boduanzhimen --limit 6 --expand
#2 [波段之门｜大盘的后期走势-20240525｜2024-05-25｜第19页] 类型: paid_article
  「买了臼冫有能力高抛低吸的做高抛低吸 , 没能力的 , 死拿一年都没问题 , 臼酒这个亻立置」
  → 白酒→臼酒、位置→亻立置、这篇文章→这篇文童（同篇）
```
同一句话在 `chart_ocr` 块里反而是干净的：
```
#1 [...｜去和留你们自己定｜2024-09-30｜配图2] 类型: chart_ocr
  「买了白酒的 , 有能力高抛低吸的做高抛低吸 , 没能力的 , 死拿一年都没问题」
```

危害：AI 引用付费块原话时会把「臼酒」「亻立置」「这篇文童」照抄给用户，
看起来像 AI 在乱码。而这些块又是排序靠前的（`paid_article` 常排在第 2 位）。

建议改法：SKILL 第 313-315 行的付费 OCR 警告改成给出可操作的规则：
```
- 65 个付费 PDF 是扫描图，走 OCR 入库，**错字密度高到不能直接引用原话**。
  实测样例：白酒→臼酒、位置→亻立置、文章→文童、判断→判、及格→篡及格。
  **引用付费块时只转述意思、不逐字照抄**；要给原话就先在
  `source_libraries/boduanzhimen/texts/<document_id>.txt` 里核对（850 个文件，按 document_id 命名）。
  同一段话若在 `article_body` 里也有（他常把付费内容改写成公众号文），**优先引正文块**
  —— `article_body` 直接来自公众号 .md 文本，完全没走 OCR，不存在错字。
```

改哪个文件：`SKILL.md` 第 313-315 行

---

### P1-7　没有「怎么复盘」的落地流程，而这是用户第一诉求

现象：用户原话（real_questions.txt#30）：
> 那你能一步步的教我怎么复盘吗，因为我复盘有很多问题，经常都是事后看涨起来了…
> 我经常复盘潜意识觉得累或者啥的，就这里缺一点，那里缺一点

SKILL 341 行里没有任何一节讲复盘步骤。第 287-293 行的「可以用他的框架」
列的是「多周期节奏定位」「变盘时间窗口推算」「仓位纪律」等能力清单，
没有把它们串成用户能照着做的顺序。

而库里有材料：
```
$ PY -c "分 chunk_type 数 复盘/计划/预案"
复盘 37 块、计划 162 块、预案 4 块、推演 2 块
```
`[2019-07-14] 预测与操作`（付费）讲得最系统：
> 每年的年底是我最忙的时候，我要费时多天，将下一年大的变盘点计算出来。
> 次序是先将下一年的变盘月份先推出来，然后再利用日线将具体变盘在哪一天推出来。
`[2026-02-13]` 的顺序：先看大盘，再看板块，再看个股。
`[2025-05-13]`：复盘不就是复习历史、借鉴历史、指导现在吗。

对比：`aizaibingchuan-perspective/SKILL.md` 有专门的复盘节
（第 93-96 行「1：复盘寻找待涨逻辑」…），`nanjinglu-bian-perspective` 有 2082 行
的每日复盘流程。波段之门 341 行是四个导师里最短的，缺的正是这一块。

建议改法：在 SKILL 第 118 行（〔图〕那节结束）之后新增一节
`## 用他的方法复盘（他的顺序，不是通用模板）`，按他的原话组织：
1. 先定时间：年底推下一年变盘月份 → 用日线推到具体哪天（`[2019-07-14]` 付费）
2. 再定级别：季 → 月 → 周 → 日 → 分时逐级看（`模型2`）
3. 再定顺序：先看大盘，再看板块，再看个股。这句他反复讲过至少 4 次，最早
   `[2023-04-24]`「要先看大盘，再看板块，三看个股。这就是格局！」，
   另见 `[2023-06-06]`、`[2023-12-12]`、`[2024-05-17]`
4. 落到指标读数：KD 是否过 80、5 日线方向、相对强度有没有上穿
   （可直接跑 `boduanzhimen_indicators.py`）
5. 定仓位：7 成上限、单票 2 成
6. 写明天的计划：什么价卖、什么条件买（`[2026-06-25]`「头一天计划好了，
   第二天只要满足条件就可以干活了」）

每一步都要标「这一步查什么关键词」，因为用户不知道该查什么词。
**写之前先按 P0-7 补完切词词表**，否则第 1、2 步的关键词（`变盘日`、
`时间周期`、`季线`、`月线`）现在查不出来。

改哪个文件：`SKILL.md` 第 118 行之后新增一节

---

## 第三部分：P2 优化

### P2-1　`chart_ocr` 的 532 块里有 33 块数字被字母污染，可以自动标出来

现象：
```
$ PY -c "chart_ocr 块里正则 \d[A-Za-z]\d|\d{2}[OoIl]\d"
chart_ocr 块数: 532
含 3xxx/4xxx 四位数的块: 112
数字中混字母(疑似误认): 33
例：3065 丐 0 3065 涌 1 30b5 . 76 / 3 1g5 . 31 - 31g7 . 32 / 3r31 毛 3
```
建议：`import_boduanzhimen.py` 的 `chart_ocr_is_useful()` 通过后再加一步，
命中该正则时给 `confidence` 打 `low` 而不是 `medium`（现在通过门槛的一律
`medium`，见 `import_boduanzhimen.py` 第 556 行）。这样 AI 看到 `confidence=low`
就知道数字不可信。代价是要重跑导入（OCR 有缓存，不用重识别）。
优先级低是因为 SKILL 里已有文字警告，这只是让它可机读。

### P2-2　`quality_report.md` 的「OCR：已识别 0，命中缓存 3718」读起来像没做 OCR

现象：`source_libraries/boduanzhimen/quality_report.md` 第 8 行。
实际是全部命中缓存（之前跑过），不是没识别。
建议：改成 `OCR：3718 张全部命中缓存（前次已识别），本次新识别 0`。

### P2-3　`onboarding_report.md` 里「暂不支持的格式」的提示已过时

现象：该文件第 16 行说
`暂不支持的格式：{".html": 723, ".mhtml": 723, ".tn6": 2, ".mp4": 16}。请增加专用适配器，勿静默跳过。`
而 `quality_report.md` 第 14 行已说明 html/mhtml 是 .md 的同源冗余、
故意不重复建块 —— 这不是「静默跳过」而是设计决定。
建议：在 `onboarding_report.md` 补一句指向 quality_report 的结论，
避免下次有人真去写 html 适配器造成重复入库。

### P2-4　`indicator_formula` 只有 19 块，但源文件里有 2 个重名文件

现象：
```
$ ls C:\Users\20577\Documents\炒股\波段之门\*.txt
20 个 .txt，其中「公式源码.txt」和「250315 沿着均线爬升加强版公式.txt」各出现两次
（在不同子目录），导入器按 sha256 去重（import_boduanzhimen.py 第 661 行）
```
19 = 20 - 1 个重复。另一个重名文件内容不同所以都留了。
建议：SKILL 第 167 行「20 个通达信公式文件去重后 15 个已翻成 Python」
改成「20 个公式文件（按内容去重后 19 个入库）中的 15 个已翻成 Python」，
把 19 这个数字和块数表对上。

### P2-5　`.tn6` 和 `.mp4` 没解析，SKILL 没提

现象：`quality_report.md` 第 18 行提了（`.tn6` 通达信指标二进制 2 个、
`.mp4` 16 个未解析），SKILL 的「知识来源」节（第 331-339 行）没提。
建议：SKILL 第 337 行后加一行
`未入库：2 个 .tn6（通达信指标二进制，无法解析）、16 个 .mp4（视频）。`
问「他有没有讲过某个指标」时，答案可能在这两个 .tn6 里。

---

## 附：本次跑过的全部验证命令

```bash
export PYTHONIOENCODING=utf-8
PY=C:/Users/20577/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe
cd "C:/Users/20577/Documents/炒股/知识库"

# 1 块类型 / 文档数 / 日期范围（直连 sqlite）
$PY -c "sqlite3 ... group by chunk_type"                        → 与 SKILL 表一致
# 2 逐词块数分 chunk_type 统计                                    → 发现 P0-6
# 3 原始 md 词频复核（723 个 .md 文件）                            → 词频表正确
# 4 两个 indicators 模块的函数名与签名对比                          → 发现 P0-1
# 5 $PY _知识库系统/scripts/test_boduanzhimen_indicators.py       → 18/18，发现 P0-5
# 6 $PY _知识库系统/scripts/boduanzhimen_backtest.py --window 10/20/60 → 发现 P0-3
# 7 image_map.jsonl 聚合（5283 条）                               → 发现 P0-2
# 8 用 chart_ocr_is_useful() 重判 3718 张 chart 的 OCR 缓存        → OCR 表完全正确
# 9 query_kb.py 跑 20+ 个真实用户问法                              → 发现 P0-7
# 10 query_kb._fallback_vocabulary() 逐词核对（157 词）            → 发现 P0-7 词表
# 11 synonyms.yaml 142 个词条逐词数本库命中                        → 发现 P1-1
# 12 系统 python 跑三个脚本 + 六个依赖 import                      → 发现 P1-3
# 13 $PY _知识库系统/scripts/sync_skills.py                       → 发现 P1-4
# 14 水印词 idown/xxhgp 命中统计                                  → 发现 P1-5
# 15 chart_ocr 数字污染正则扫描                                    → 发现 P2-1
```

**没验证的**（留给下一轮）：
- 没让 AI 真的跑一遍自测题库打分。本清单是对文档和脚本的静态核查 +
  检索层实测，不是端到端的回答质量评测。
- 没核对 `paid_article` 542 块逐块的 OCR 质量，只抽样看了检索命中的那几块。
- 没验证 `select_*` 五个选股函数在真实行情数据上的输出是否符合他文章里的例子
  （测试用的是构造数据，只验了逻辑不验了结果）。
