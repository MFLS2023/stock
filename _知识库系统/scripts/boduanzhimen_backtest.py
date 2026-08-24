#!/usr/bin/env python3
"""波段之门公开预测的准确率回测。

做法：从知识库正文块里抽出「发文日期 + 明确点位 + 预测方向」的语句，
再用真实上证指数数据检验这个点位在此后是否被触及。

为什么只能做到这个程度（先说清边界，避免把结果当成胜率）：

1. **他的预测大量依赖图**。正文常是「看这张图，4 月会有变盘」，点位画在图上而非
   写在字里。能自动抽出来的只是显式写了数字的那部分，约占全部预测的一小部分。
2. **方向词是中文自然语言**，「至少到 4152」「不会超过 100 点」「大概率没有」
   的逻辑结构不同，规则匹配必然有误判。所以每条判定都保留原文，供人工复核。
3. **没有时间窗口的预测无法判错**。「将来肯定突破 3674」永远等着被兑现，
   这类只统计不判定 —— 他本人也说过「没有时间的推论，等于没说」。
4. 因此本脚本输出的不是「预测准确率」，而是「显式点位在给定窗口内的触及率」，
   两者不能混为一谈。

用法：
    # 最简：什么都不用准备，默认读 _知识库系统/data/sh000001_daily.csv
    python boduanzhimen_backtest.py

    # 指定别的行情文件、或只跑单一窗口
    python boduanzhimen_backtest.py --index-csv <上证日线csv> --window 60
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
CHUNKS = ROOT / "_知识库系统" / "source_libraries" / "boduanzhimen" / "chunks.jsonl"

# 上证日线缓存。放在 data/ 而不是 tmp/：tmp/ 随时可能被清理，
# 而这份文件是回测的默认数据源，丢了脚本就跑不起来。
INDEX_CACHE = ROOT / "_知识库系统" / "data" / "sh000001_daily.csv"

# 新浪的指数 K 线接口，datalen=6000 能一次取到 2001-11 至今。
# 只在缓存不存在时才打，正常使用不联网。
SINA_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData?symbol=sh000001&scale=240&ma=no&datalen=6000"
)

# 报告输出的三档窗口。他很少写明期限，单跑一档会让人误以为那个数字是"他的准确率"；
# 三档一起出，命中率随窗口拉长而上升这件事本身就是结论。
DEFAULT_WINDOWS = (20, 60, 120)

# 日内级别点位（「今天的阻力位在3493点」）用的窗口。他这类读数是拿 30 分钟 K 线算的，
# 有效期就是当天到次日，所以用 1/3 个交易日检验，而不是塞进 20/60/120 那张表。
# 混在一起统计会得出"他的阻力位判断 0% 正确"这种假结论 —— 牛市里任何点位
# 60 天后都会被穿，那测的是指数涨没涨，不是他判断准不准。
INTRADAY_WINDOWS = (1, 3)

# 上证指数的合理点位区间。低于这个数的四位数基本不可能是上证点位。
POINT_MIN, POINT_MAX = 1600, 6200

# 4 位数点位。限定 2000-6999 排除年份（2023）和涨幅（1.15）等噪声，
# 但 2023/2024/2025/2026 这几个数本身也落在区间内，靠「点」字后缀区分。
POINT = re.compile(r"(?<!\d)([2-6]\d{3})(?:\.\d+)?\s*点")

# 带「点」字仍然不是点位的情况。逐条说明各自防的是什么误判：
#
# 1. 指数/板块名里的数字 —— 「国证2000」「中证500」「沪深300」「科创50」「上证50」。
#    实测 2025-01-11 那句"突然发现国证2000就要完蛋"，如果只看方向词会被当成
#    他看跌到 2000 点。「上证50」尤其阴险：它带「上证」二字，最容易被误认成上证指数，
#    而 2024-09-04 那篇整段在谈上证50 的调整目标（2335/2248 点），
#    拿这些点位去对上证指数（当时 2700 多）会得出完全错误的命中判定。
# 2. 数量单位 —— 「3317只股票」「2535人」「1000亿」「4年才拉黑5000多人」。
#    这些数字和点位在字形上无法区分，只能靠后面跟的量词排除。
# 3. 年份 —— 「2023年」「2008年4月」。他极爱拿历史年份作类比
#    （"这种渐宽底在2013年曾经出现过"），2013-2026 全部落在点位区间内。
#
# 实现上不是"整句丢弃"而是"逐个数字剔除"：一句话里可能既有「国证2000」
# 又有真实的上证点位，整句丢会连真的一起丢。
NOT_A_LEVEL = (
    ("指数或板块名", re.compile(r"(国证|中证|深证|沪深|科创|创业板指|上证|恒生|标普|道指|日经)\s*\d{2,4}")),
    # 允许数字和量词之间夹「多/余/来/左右」：他常写「拉黑5000多人」「1000余只」，
    # 不留这个空档就漏掉一半的数量表达
    ("数量单位", re.compile(r"\d{3,4}\s*(多|余|来|左右)?\s*(只|人|家|亿|万|元|倍|次|名|个)")),
    ("年份", re.compile(r"\d{4}\s*年")),
)

# 否定词 + 动词方向，用来在进 DIRECTION_RULES 之前先做语义反转。
# 见 classify_direction() 里的注释：不先判否定，「不会跌破X」会被判成「预测跌到X」，
# 方向正好反过来，命中率也就跟着反。
NEGATION = re.compile(r"(不会|没有|不至于|无法|未能|不能|除非不|只要不|尚未|还没)")
DOWNWARD_VERB = re.compile(r"(跌破|跌到|跌至|下跌到|回落到|破掉|失守|杀到|探到)")
UPWARD_VERB = re.compile(r"(突破|超过|涨到|涨至|上攻到|冲到|站上|过了?)")

# 方向判定：先匹配更具体的模式，避免「不会超过」被「超过」误吞。
DIRECTION_RULES = (
    ("下行", re.compile(r"(跌到|跌破|下跌到|回落到|杀到|探到|跌至)\s*$")),
    ("上行", re.compile(r"(涨到|突破|达到|上攻到|冲到|至少要达到|涨至|直奔)\s*$")),
    # 阻力 = 压力，都是"上面挡着的那个价"，语义等同上限：他给阻力位是让人到那里高抛，
    # 声称"到这里会受阻"，所以判定口径和上限一致（被有力突破才算他说错）。
    # 实测他写「强阻力在3168点」「今天的阻力位在3493点左右」「第二阻力3493点」，
    # 原来的规则只有「压力」没有「阻力」，这类全被当成"无法判定方向"丢掉了。
    ("上限", re.compile(r"(不会超过|不过|最高到|上限|压力位?在?|压力是|压力点位[:：]?|"
                        r"强?阻力位?在?|阻力是|阻力区|第[一二三四]阻力是?)\s*$")),
    ("下限", re.compile(r"(支撑位?在?|支撑是|不会跌破|底部在?|长线底部|支撑点位[:：]?|支撑和?压力点位[:：]?)\s*$")),
)

# 「目标位是X」「理论点位是X」这两类他用得极多，但方向词本身是中性的 ——
# 「第一调整目标在2335点」是向下，「上涨的理论点位是3960点」是向上。
# 所以先认出"这是个目标/理论测算点位"，方向再交给 infer_neutral_direction
# 按本句里的涨跌词判断。
NEUTRAL_TARGET_RULES = (
    # 目标位：目标位是/在、第一目标、第二目标位:、下跌目标大约在、最低的目标位是
    re.compile(r"(目标位?是?在?|第[一二三四五]目标位?是?在?|目标位?[:：])\s*$"),
    # 理论测算：理论点位是/在、理论上的空间大概在、理论终点是、计算的理论底部点位是、理论位置在
    re.compile(r"(理论[上的]*(点位|终点|目标|位置|空间|低点|底部点位|下跌点位)?是?在?大约?|"
               r"理论[上的]*\S{0,6}(点位|终点|位置|空间)是?在?)\s*$"),
)

# 判断中性目标的方向时看这些词，只在点位所在的那一句里找（见 infer_neutral_direction）。
# 必须收进「涨到/跌到」这类动词：实测「上证跌到了第一目标2892点」若只认
# 「下跌」这种名词形式就会漏掉，方向判不出来 —— 而这条恰恰是方向最明确的一句。
UPWARD_HINT = re.compile(
    r"(上涨|反弹|上攻|冲高|新高|涨幅|主升|向上|升至|攻击|突破|上沿|高点|收缩目标|涨到|涨至|冲到|站上)"
)
DOWNWARD_HINT = re.compile(
    r"(下跌|调整|回落|杀跌|新低|跌幅|向下|下探|下行|降至|底部|低点|下沿|见底|跌到|跌至|跌破|探到)"
)

# 明确的时间窗口线索
TIME_HINT = re.compile(r"(今年|明年|本周|下周|下月|\d{1,2}月|年底|年内|季度)")

# 日内级别的点位。他给的阻力位/支撑位大量是「今天的阻力位在3493点」
# 「周一上午阻力位:3410点」这种当日或次日盘中有效的读数，是拿 30 分钟 K 线算的。
# 拿 60 个交易日的窗口去检验"今天的阻力"是口径错误：牛市里几乎必然被突破，
# 统计出来的不是他判断准不准，而是"60 天里指数涨没涨"。这类单独标出、不计入窗口统计。
INTRADAY_SCOPE = re.compile(r"(今天|今日|明天|次日|上午|下午|盘中|开盘|收盘|尾市|周一|分钟|分时|\d{1,2}[:：]\d{2})")

# 但他也会说「它今天碰到了长期阻力3701.57点」—— 时间词描述的是"今天碰到"这个动作，
# 阻力本身是长期级别的，这类不能当日内点位。级别词优先于时间词。
LONG_TERM_SCOPE = re.compile(r"(长期|长线|中长线|周线|月线|季线|年线|大级别|历史)")

# 他给出阻力位却在紧后面说"会突破"的情况。这不是「上限」主张，判它"被突破=他说错了"
# 是把话完全读反了。实测三例：
#   [2025-07-06]「第一阻力是3475点,第二阻力3493点。所以,冲破3475以后,就直奔3493点去了」
#   [2025-08-14]「碰到了长期阻力3701.57点…这种阻力,在牛市是会站上去的」
# 命中就整条剔除：他此刻表达的是"这里会被突破"，和"这里挡得住"正好相反。
EXPECTS_BREAKOUT = re.compile(r"(冲破|会站上去|站上去的|直奔|一举突破|翻出喇叭口|突破以后|过了这)")

# 条件从句里的点位不是预测。「如果不过3936点，3815被破的机会蛮大」——
# 他在说"若 A 则 B"，A 本身没有被主张会发生。判定"A 发生了所以他错了"是逻辑错误。
# 假设句里的点位不是他的主张。「这种情况最糟糕的一种走势是:跌破3980点,而反弹不过3993点」
# —— 他在列举一种可能，而且明说是"最糟糕的一种"，不是在预测它会发生。
# 「有可能的走势是」「也许」「不排除」同理。这类不该记入命中率的任何一边。
#
# 只认「他明确说这是若干种可能之一」的措辞，不要把「也许」「不排除」算进来 ——
# 他给真预测时也常带这些软化词（「也许,这次下跌完毕后,指数突然大幅拉升,一举突破3439点」
# 是实打实的预测，只是说得客气）。按「也许」滤会误杀真样本，实测误杀 2 条。
HYPOTHETICAL = re.compile(
    r"(最?糟糕的?一?种?走势|有可能的?走势是|可能的?一?种?走势是|一种可能是|"
    r"极端情况|最坏的?情况|悲观的?情况下)"
)

CONDITIONAL_CLAUSE = re.compile(r"(如果|只要|倘若|假如|若是|要是|前提是)[^。！？]{0,24}$")

# 「还好,还好,没有跌破3761点」「没有跌破X」是在报告已经发生的事（今天没破），
# 不是预测未来会破。PAST_TENSE 抓不到它是因为句子里没有过去时动词。
# 「只要没有跌破3834点,就维持这种看法」是前瞻条件不是既成事实，
# 所以命中后还要确认前面没有「只要/如果」这类条件引导词（见 is_negated_past）。
NEGATED_PAST = re.compile(r"(没有|未能|没能|尚未|不曾)[^。！？]{0,4}(跌破|涨到|突破|达到|破掉|到达|跌到)\s*$")
# 条件引导词。出现在否定词前面就说明整句是"若不发生 A 则 B"的前瞻判断。
CONDITION_LEAD = re.compile(r"(只要|如果|倘若|假如|若是|要是|除非|一旦)")

# 「我当初预判的第一目标:3090点」「2024年7月25日,上证跌到了第一目标2892点」
# 「我再强调一遍:我们计算的理论点位是3883点」—— 这些是复述他**早前**给出的点位，
# 发文日已经知道结果了。拿发文日之后的行情去判它，等于用已知答案答题：
#   实测 2024-09-06 那条，2892 在 7 月就已触及，从 9 月起算 60 天必然"命中"。
# PAST_TENSE 拦不住是因为它只看点位前 40 字，而"我当初""我提出了"这类
# 复述标记常出现在更前面，或者句子本身没有过去时动词。
#
# 这类不是"预测错了"，是"根本不该进样本"。他真正的预测在原始那篇文章里，
# 那篇如果显式写了点位，已经被单独抽出来了，不需要靠复述句重复计一次。
RESTATEMENT = re.compile(
    r"(我当初|当初(说|预判|计算|给|的)|早前|先前|之前(说|提|给|计算)|"
    r"我(提出|说|给|计算)过|我提出了|再强调一遍|已经(说|提|给)过|"
    r"上证(已经)?跌到了|上证(已经)?涨到了|[前上]面的文章|那篇文章|"
    r"\d{4}年\d{1,2}月\d{1,2}日[,，]|我在\d{1,2}月)"
)

# 他自己给的"突破"定义（`[2025-04-14]`原话）：
#   "突破阻力,不是指数超过这个点位就是突破了…一般情况下,要比阻力位置高出0.6%才有可能"
# 所以判定上限被破、支撑失效时留 0.6% 容差 —— 用他的标准判他的话，
# 而不是用"碰到就算破"这个更严的口径。实测 2023-08-30 声称阻力 3168，
# 窗口内最高 3177.06 只超出 0.29%，按他自己的定义那不叫突破。
BREACH_TOLERANCE = 0.006

# 无时间窗口的空头承诺，单独统计不判定
NO_DEADLINE = re.compile(r"(将来|早晚|终究|总会|迟早)")

# 别人的观点，不是他的预测。他文章里大量引用他人看法然后反驳，
# 首版回测把「有的人说上证要跌到2200点…这种概率是1%」记成了他预测 2200，
# 方向完全反了。这类必须排除，否则统计出来的是"他反驳过的观点的命中率"。
QUOTED_OTHERS = re.compile(
    r"(有的?人说|有人说|不少人|许多人|很多人|大家都?说|市场上?说|"
    r"别人|他人|李大霄|据说|传闻|网上说|读者说|专家说|某某|大V)"
)
# 明确否定他人观点的措辞
REBUTTAL = re.compile(r"(概率是?1?%|不可能|太脱离|胡说|扯|荒唐|我不认同|别信|哪来的|做梦)")

# 复述已发生的事，不是预测。「从3418跌到3144，跌了快300点了」讲的是过去，
# 首版把它当成"预测跌到3144"并判为命中 —— 这种命中毫无意义，等于用已知答案答题。
#
# 词表分两组，因为中文时间词本身不区分时态：
# 「今天」在「今天跌到3123点时反弹了」是过去，在「如果今天跌破3733点」是将来。
# 靠时间词单独判会两头出错（实测：只用时间词会误杀 2 条真预测，
# 去掉时间词又会漏进 3 条复述句），所以拆成
#   PAST_TENSE      —— 无歧义的过去标记（体貌助词、明确的过去时间词）
#   PAST_TENSE_SOFT —— 有歧义的，只在同句没有前瞻标记时才算过去
PAST_TENSE = re.compile(
    r"(跌了|涨了|已经|昨天|上周|上个?月|去年|前天|当时|那天|回顾|"
    r"产生了|出现了|结束了|见到|见过|收盘了|完成了|破掉|之后就|事实是|"
    # 下面这批是按句检测后新暴露出来的：原先靠 40 字窗口跨到上一句偶然拦住，
    # 属于用错的方法得到对的结果，改成按句后必须显式列出。
    r"却在|可是它|突然中?期?转|的时候[,，]|时[,，]突然|"
    # 「动词+了」是中文完成体的通用标记，逐词穷举补不完（漏过「调整了」一例）。
    # 只收行情语境里的动词，不用泛化的 `.了`——那会命中「要涨了」这类将来时。
    r"(反弹|调整|下跌|上涨|回落|冲高|回调|震荡|走完|走了|站上|跌破|突破)了)"
)

# 有歧义的时间词：同句出现前瞻标记（如果/只要/明天/将来）时不算过去。
PAST_TENSE_SOFT = re.compile(r"(今天|今日|本周[一二三四五]|午后|上午|下午|刚才|盘中)")
# 前瞻标记。出现在同一句里，说明这句讲的是尚未发生的事。
FORWARD_LOOKING = re.compile(
    r"(如果|只要|倘若|假如|若是|要是|除非|一旦|明天|后天|下周|下个?月|"
    r"将来|接下来|后面|理论上|预计|可能要|我判断|计划是|不能超过|不会)"
)

# 句首（前 6 字内）就是条件引导词 —— 整句是「若 A 则 B」的前瞻判断，
# 从句里的完成体词（「上涨结束了」）说的是被假设的事，不是既成事实。
# 实测这条能救回 [2025-12-30]「只要没有跌破3910点,我们都不能肯定…上涨结束了」，
# 它被硬词表的「结束了」误杀，而它本该走 is_negated_past 的前瞻分支。
CONDITION_OPENER = re.compile(r"^[^。！？]{0,6}?(只要|如果|倘若|假如|若是|要是|除非|一旦)")

# 反事实假设的时间标记：条件句里出现这些词，说明他在对**已发生**的事做假想
# （「如果昨天下午不过4096点,那会发生什么?」），不是对未来的预测。
COUNTERFACTUAL_TIME = re.compile(r"(昨天|前天|上周|上个?月|去年|当初|之前|刚才|本周[一二三四五])")

# 整块级的标的切换宣告。他在一篇里会明说「应读者要求,我今天分析一下上证50」，
# 之后整段的点位都属于那个标的，与上证指数无关。
# 只收**显式宣告**的句式，不收顺口一提 —— 「要分析上证,必须分析上证50」不算切换。
# 「我」和时间/次序词都不能省 —— 只写「分析+标的」会命中顺口一提的
# 「要分析上证,必须要分析上证50和沪深300」（实测误命中）。
_OTHER_INDEX = r"(上证\s*50|上证五十|沪深\s*300|创业板|科创|深证|国证\d*|中证\d*)"
SUBJECT_SWITCH = re.compile(
    # 「我今天分析一下上证50」「下面说说创业板」—— 有主语或次序词的显式宣告
    rf"(我(今天|下面|接下来|这里|先|再)?\s*(来|再)?(分析|说说|看看|讲讲)\s*一?下?\s*{_OTHER_INDEX}|"
    rf"(今天|下面|接下来)\s*(来|再)?(分析|说说|看看|讲讲)\s*一?下?\s*{_OTHER_INDEX}|"
    # 「上证50, 加油!」—— 对着某个标的喊话，说明整段在讲它
    rf"{_OTHER_INDEX}\s*[,，]?\s*加油)"
)

# 当日读数句式：「今天的阻力位在3493点」「今天上涨的阻力位在3933点,支撑在3902点」。
# 这类是他拿 30 分钟 K 线算的**当日前瞻**，有效期到次日，由下游日内分支单独判定。
# 不能因为句里有「今天」就当复述剔掉 —— 实测会误杀 3 条日内点位（7 条掉到 3 条）。
INTRADAY_LEVEL = re.compile(r"(阻力位?|压力位?|支撑位?|强阻力)(在|是|大约|位于)?\s*[2-6]\d{3}")

# 上证以外的标的。他同一段里会谈券商、保险、创业板的点位，
# 拿这些点位去对上证指数是错的（2138 是保险板块压力位，不是上证）。
#
# 补了「上证50 / 50指数 / 上证五十」：实测 2024-09-04 那篇整段分析上证50，
# 给的 2335/2248 点是上证50 的调整目标，而当时上证指数在 2700 多 ——
# 拿 2248 去对上证会被判成"命中"，那是彻底的假命中。
# 「上证50」不能靠 NOT_A_LEVEL 排除数字本身（那只排掉"50"这两位数，
# 2335 这个真点位仍在），必须在标的物层面整条剔除。
# 注意不要写 `50\s*的` 这种模式：它会命中「3050的支撑位」里的「50的」，
# 把真正的上证点位当成上证50 剔掉（实测误伤 3050/3350/2850 三类写法）。
# 必须带「上证」或「指数」这类限定词才能安全区分。
#
# 「板块」「个股」这两个词从这里移出去了：它们是泛指，不指认任何具体标的。
# 实测 13 处「板块」+3 处「个股」的命中全是行文噪声，例如
#   [2025-09-18]「牛市的特点是,板块不会齐涨齐跌…b区上涨的理论点位是3960点」
#   [2025-03-09]「这次不会超过3674点。下面说说板块,本周AI分支…」
# 他谈完上证顺口说一句板块，就把这条真预测整条剔掉了 —— 12 处误伤全出自这两个词。
OTHER_SYMBOL = re.compile(
    r"(上证\s*50|上证五十|(?<!\d)50\s*指数|券商|证券|保险|银行|创业板|科创|深证|中小板|北证|"
    r"恒生|标普|纳斯达克|国证|沪深300|hs300|中证\d*|这只票|平安|茅台|微盘|etf|ETF)"
)


@dataclass
class Prediction:
    date: str
    title: str
    point: float
    direction: str
    has_deadline: bool
    excerpt: str
    # 方向是"由句式直接读出"还是"按上下文推断"。推断出来的可信度低一点，
    # 报告里标出来，便于人工优先复核这一批。
    direction_inferred: bool = False
    # 这个点位是日内级别的（今天的阻力位/上午支撑），不该用多周窗口检验
    intraday: bool = False
    verdict: dict[int, str] = field(default_factory=dict)
    detail: dict[int, str] = field(default_factory=dict)
    extreme: dict[int, float] = field(default_factory=dict)
    base_close: float = float("nan")


@dataclass
class Report:
    predictions: list[Prediction] = field(default_factory=list)
    skipped_no_direction: int = 0
    skipped_no_data: int = 0
    skipped_no_deadline: int = 0
    skipped_quoted: int = 0
    skipped_other_symbol: int = 0
    skipped_past_tense: int = 0
    skipped_not_a_level: int = 0
    skipped_out_of_range: int = 0
    # 中性目标里连上下文都判不出方向的
    skipped_neutral_unresolved: int = 0
    skipped_expects_breakout: int = 0
    skipped_conditional: int = 0
    skipped_restatement: int = 0
    skipped_hypothetical: int = 0
    skipped_negated: int = 0
    not_a_level_samples: list[str] = field(default_factory=list)
    intraday_samples: list[str] = field(default_factory=list)


def download_index(target: Path) -> None:
    """从新浪拉上证日线存成 CSV。只在缓存不存在时调用。

    为什么内置下载：用户不懂编程，让他自己去 MCP 取数存 CSV 是个门槛。
    """
    print(f"缓存不存在，从新浪下载上证日线 → {target}")
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(
        SINA_URL, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )
    raw = urllib.request.urlopen(request, timeout=40, context=context).read().decode("utf-8", "replace")
    records = json.loads(raw)
    if not records:
        raise SystemExit("新浪接口返回空数据，请改用 --index-csv 指定本地文件")
    frame = pd.DataFrame(records)
    frame["date"] = pd.to_datetime(frame["day"])
    for column in ("open", "high", "low", "close"):
        frame[column] = frame[column].astype(float)
    frame = frame[["date", "open", "high", "low", "close"]].sort_values("date")
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(target, index=False, encoding="utf-8")
    print(f"已写入 {len(frame)} 根日线")


def load_index(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    missing = {"date", "open", "high", "low", "close"} - set(frame.columns)
    if missing:
        raise SystemExit(f"{path} 缺列 {sorted(missing)}，需要 date/open/high/low/close")
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.sort_values("date").set_index("date")
    for column in ("open", "close", "high", "low"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["close"])
    if frame.empty:
        raise SystemExit(f"{path} 没有可用的收盘价")

    # 数据合法性校验：这个坑已经踩过两次。
    # market_cache.db 和 live-market MCP 的 "000001" 返回的都是**平安银行**（11 元一股），
    # 不是上证指数（3900 点）。拿平安银行的价格去检验"上证会不会到 3900 点"，
    # 结果是全部"未达"，而且不会报错 —— 静默给出一份全错的报告是最坏的失败方式。
    # 取上证指数只能用 stock_data MCP 的 index_prices，或本脚本内置的新浪接口。
    median_close = float(frame["close"].median())
    if median_close < 500:
        raise SystemExit(
            f"{path} 的收盘价中位数是 {median_close:.2f}，这不像上证指数数据"
            f"（可能拿到了平安银行 —— market_cache.db 和 live-market MCP 的 000001 都是它）。\n"
            f"  上证指数的历史点位在 1600~6200 之间。\n"
            f"  正确取数：stock_data MCP 的 index_prices(symbol='000001')，"
            f"或删掉本地缓存让本脚本走内置的新浪接口重下。"
        )
    return frame


def sentence_around(text: str, position: int) -> str:
    """取 position 所在的那一句（按 。！？；：\\n 切分）。

    过去时检测原来只看点位**前** 40 个字符，而中文复述句的过去时词常落在点位
    **之后**：「从3418点跌到3144点,跌了快300点了」里的「跌了」在 3144 后面第 1 个字，
    固定前向窗口永远看不到它，于是这条复述被当成「预测跌到3144」并判为命中。

    为什么不直接放宽成「整段都搜」：那会把隔了好几句的过去时词也算进来，
    审查报告 P1-5 记录过同类误杀 ——
    「昨天已经涨了…我的计划是,等涨过3439点,我才考虑加大仓位」
    后半句是真预测，只因前一句有「涨了」就被整条剔掉。
    按句切分能同时解决两头：句内看得全，句外看不到。
    """
    # 冒号也算边界：「如果明天跌破3733点,我只会给你一个结论:只能上涨了」
    # 冒号后是另一个分句，那里的「上涨了」不代表点位所在的分句是复述。
    boundaries = "。！？；：:\n"
    start = 0
    for i in range(position - 1, -1, -1):
        if text[i] in boundaries:
            start = i + 1
            break
    end = len(text)
    for i in range(position, len(text)):
        if text[i] in boundaries:
            end = i
            break
    return text[start:end]


def is_restating_past(sentence: str) -> bool:
    """这一句是在复述已经发生的事，还是在预测？

    三级判定，从强到弱：
      1. 句首是条件引导词（「只要没有跌破3910点,…上涨结束了」）—— 整句都是前瞻，
         即使句中有「结束了」这类完成体词，那也落在被假设的从句里，判为预测。
      2. 无歧义的过去标记（「跌了」「事实是」）出现即判过去。
      3. 有歧义的时间词（「今天」「本周四」）只在同句**没有**前瞻标记、
         且不是当日读数句式时才判过去 ——
         「今天跌到3123点时反弹了」是复述，「今天的阻力位在3493点」是当日前瞻读数。
    """
    # 条件引导句默认是前瞻，但「如果昨天下午不过4096点,那会发生什么?」是**反事实**
    # 假设（对已发生的事做假想），不是预测。带明确过去时间词的条件句仍判为复述。
    if CONDITION_OPENER.search(sentence) and not COUNTERFACTUAL_TIME.search(sentence):
        return False
    if PAST_TENSE.search(sentence):
        return True
    if INTRADAY_LEVEL.search(sentence):
        return False
    if PAST_TENSE_SOFT.search(sentence) and not FORWARD_LOOKING.search(sentence):
        return True
    return False


def is_negated_past(prefix: str) -> bool:
    """「没有跌破X」是否在报告既成事实（而非前瞻条件）。

    「还好,没有跌破3761点」= 今天没破，既成事实，不该当预测。
    「只要没有跌破3834点,就维持这种看法」= 前瞻条件，该保留。
    区别在于否定词前面有没有条件引导词。
    """
    match = NEGATED_PAST.search(prefix)
    if not match:
        return False
    # 只看否定词之前那一小段，避免上一句的「如果」把这句也带过去
    lead = prefix[max(0, match.start() - 8): match.start()]
    return not CONDITION_LEAD.search(lead)


def is_not_a_level(text: str, match_start: int, match_end: int) -> str:
    """这个数字是否落在"不是点位"的片段里。返回命中的规则名，没命中返回空串。

    逐个数字判断而非整句丢弃：一句话里可能既有「国证2000」又有真实的上证点位。
    """
    for name, pattern in NOT_A_LEVEL:
        for hit in pattern.finditer(text):
            # 数字的起始位置落在干扰片段内部，说明这个数字就是干扰的一部分
            if hit.start() <= match_start < hit.end():
                return f"{name}（{hit.group(0)}）"
    return ""


def infer_neutral_direction(text: str, match_start: int) -> str:
    """给「目标位是X」「理论点位是X」这类中性表述定方向。

    只在**点位所在的这一句**里找涨跌词，并取离点位最近的那个。
    两条设计决定，都是被实测逼出来的：

    1. 限定在本句内（从上一个句末标点之后算起）。原先取前 60 字会跨句串味：
       [2025-09-18]「也许,股指调整时,你的股票会不断拉升。牛市的特点是,板块不会齐涨齐跌…
       b区上涨的理论点位是3960点」—— 上一句的「调整」和本句的「上涨」同时命中，
       方向就判不出来了，而本句其实写得很清楚。
    2. 不做"按点位高于/低于发文收盘价来定方向"的数值兜底。那个办法看着聪明，
       实测会把方向判反：[2024-09-06]「上证跌到了第一目标2892点」，2892 高于
       当日收盘 2765，数值兜底判成「上行」，与原文的「跌到」正好相反，
       还会顺带算出一个假命中。文字给不出方向就丢掉，不猜。
    """
    # 本句的起点：上一个句末标点或换行之后
    boundary = max(
        (text.rfind(mark, 0, match_start) for mark in ("。", "！", "？", "\n", "；", "?", "!")),
        default=-1,
    )
    clause = text[boundary + 1: match_start]
    up = UPWARD_HINT.search(clause)
    down = DOWNWARD_HINT.search(clause)
    if bool(up) == bool(down):
        # 两类都没有 → 判不出；两类都有 → 取离点位更近的那个（后出现的）
        if not up:
            return ""
        return "上行" if up.start() > down.start() else "下行"
    return "上行" if up else "下行"


def classify_direction(text: str, match_start: int) -> tuple[str, bool]:
    """看点位前面 24 个字里的方向词。返回 (方向, 是否中性目标)。

    只看前文不看后文：中文里「跌到 3000 点」的方向词在数字前，
    而后文常是下一句的开头，取了会串味。

    中性目标（目标位/理论点位）返回 ("", True)，方向留给 infer_neutral_direction，
    因为「第一调整目标在2335点」和「上涨的理论点位是3960点」句式相同、方向相反。
    """
    prefix = text[max(0, match_start - 24): match_start]

    # 否定词必须先判，否则方向会被判反。
    # DIRECTION_RULES 是顺序匹配、先命中先返回，而「下行」排在「下限」之前，
    # 所以「不会跌破3902点」里的「跌破」会先被「下行」规则吃掉 ——
    # 结果把他给的**支撑位**当成「他预测跌到这里」，指数真跌破了反而记为命中（算他对），
    # 而按原意应该是支撑失效（算他错）。实测这类在样本里有 2 条，会让结论方向颠倒。
    #
    # 处理方式是语义反转而非简单跳过：
    #   「不会/没有跌破 X」= X 是下方支撑 → 下限
    #   「不会/没有突破 X」= X 是上方压力 → 上限
    # 「不会超过」已经写在「上限」规则里且排在「上行」之前，侥幸没错，
    # 但那是规则顺序碰巧对，不能依赖 —— 这里统一处理，不留侥幸。
    negated = NEGATION.search(prefix)
    if negated:
        tail = prefix[negated.end():]
        if DOWNWARD_VERB.search(tail):
            return "下限", False
        if UPWARD_VERB.search(tail):
            return "上限", False

    for label, pattern in DIRECTION_RULES:
        if pattern.search(prefix):
            return label, False
    for pattern in NEUTRAL_TARGET_RULES:
        if pattern.search(prefix):
            return "", True
    return "", False


def extract_predictions(chunks_path: Path, index: pd.DataFrame) -> tuple[list[Prediction], Report]:
    """抽取预测。需要 index 是因为中性目标定方向时要拿发文日收盘价做参照。"""
    report = Report()
    rows = [json.loads(line) for line in chunks_path.open(encoding="utf-8")]
    for row in rows:
        if row.get("chunk_type") != "article_body":
            continue
        date = row.get("date") or ""
        if not date:
            continue
        text = row["text"]
        base_close = close_on(index, date)
        for match in POINT.finditer(text):
            point = float(match.group(1))
            # 先按合理点位区间筛：低于 1600 或高于 6200 的四位数不可能是上证点位
            if not POINT_MIN <= point <= POINT_MAX:
                report.skipped_out_of_range += 1
                continue
            # 再排除"带点字但不是点位"的：指数名、数量单位、年份
            reason = is_not_a_level(text, match.start(), match.end())
            if reason:
                report.skipped_not_a_level += 1
                if len(report.not_a_level_samples) < 15:
                    snippet = text[max(0, match.start() - 40): match.end() + 15].replace("\n", " ")
                    report.not_a_level_samples.append(f"[{date}] {reason} …{snippet}…")
                continue
            direction, neutral = classify_direction(text, match.start())
            if not direction and not neutral:
                report.skipped_no_direction += 1
                continue
            window = text[max(0, match.start() - 60): match.end() + 60]
            # 引述他人观点的、或谈非上证标的的，都不算他对上证的预测
            near = text[max(0, match.start() - 80): match.end() + 40]
            if QUOTED_OTHERS.search(near) or REBUTTAL.search(near):
                report.skipped_quoted += 1
                continue
            # 标的物过滤对中性目标要更严：他分析上证50/创业板时也用"第一调整目标在X点"
            # 这个句式，往前多看 60 字才能抓到段首的"上证50的这次调整"
            scope = text[max(0, match.start() - (140 if neutral else 80)): match.end() + 40]
            if OTHER_SYMBOL.search(scope):
                report.skipped_other_symbol += 1
                continue
            # 整块级的标的宣告：他明说「我今天分析一下上证50」之后，整段讲的都是那个标的，
            # 段内所有点位都不该拿去对上证指数。
            # 不能靠加大 OTHER_SYMBOL 的字符窗口解决 —— 审查报告 P1-5 记录过窗口过宽
            # 造成 12 处误伤（他谈完上证顺口提一句板块，真预测被整条剔掉）。
            # 实测这条拦住 [2024-09-04]「我判断,最后的目标可能要跌破2248点」：
            # 2248 是上证50 的第二目标，「上证50」在块首距点位约 90 字，超出 80 字窗口。
            if SUBJECT_SWITCH.search(text[:match.start()]):
                report.skipped_other_symbol += 1
                continue
            # 在点位所在的**整句**内查过去时词，不用固定字符窗口。
            # 原来只看前 40 字，漏掉「跌到3144点,跌了快300点了」这类过去时词在点位
            # 之后的复述句（审查报告 P0-2）；放宽成整段又会误杀隔句的真预测（P1-5）。
            if is_restating_past(sentence_around(text, match.start())):
                report.skipped_past_tense += 1
                continue
            # 复述早前给过的点位（"我当初预判的第一目标"）往前看 80 字，
            # 因为复述标记离点位比过去时动词远
            if RESTATEMENT.search(text[max(0, match.start() - 80): match.start()]):
                report.skipped_restatement += 1
                continue
            # 「没有跌破3761点」：报告已经发生的事，不是预测
            if is_negated_past(text[max(0, match.start() - 30): match.start()]):
                report.skipped_negated += 1
                continue
            # 假设句列举的走势，不是他主张会发生的
            if HYPOTHETICAL.search(text[max(0, match.start() - 60): match.start()]):
                report.skipped_hypothetical += 1
                continue
            # 上限/下限是"这里挡得住/托得住"的主张，两种情况下这个主张不成立：
            #   1. 紧后面就说会突破（他给的是路径上的关卡，不是天花板）
            #   2. 点位出现在条件从句里（"如果不过X"，X 没有被主张会发生）
            if direction in ("上限", "下限"):
                if EXPECTS_BREAKOUT.search(text[match.end(): match.end() + 90]):
                    report.skipped_expects_breakout += 1
                    continue
                if CONDITIONAL_CLAUSE.search(text[max(0, match.start() - 40): match.start()]):
                    report.skipped_conditional += 1
                    continue
            inferred = False
            if neutral:
                direction = infer_neutral_direction(text, match.start())
                if not direction:
                    report.skipped_neutral_unresolved += 1
                    continue
                inferred = True
            has_deadline = bool(TIME_HINT.search(window)) and not NO_DEADLINE.search(window)
            # 日内点位只在上限/下限（阻力/支撑）这类读数上判断：
            # 「今天的阻力位在3493点」是当日有效，「今天跌破3497点才确认回踩」
            # 说的是一个待触发的条件，不受当日限制。
            scope_prefix = text[max(0, match.start() - 30): match.start()]
            intraday = (
                direction in ("上限", "下限")
                and bool(INTRADAY_SCOPE.search(scope_prefix))
                and not LONG_TERM_SCOPE.search(scope_prefix)  # 级别词优先于时间词
            )
            if intraday and len(report.intraday_samples) < 12:
                report.intraday_samples.append(
                    f"[{date}] {direction} {point:.0f} …{window[:80].replace(chr(10), ' ')}…"
                )
            report.predictions.append(
                Prediction(
                    date=date,
                    title=row.get("title", ""),
                    point=point,
                    direction=direction,
                    has_deadline=has_deadline,
                    excerpt=window.replace("\n", " ").strip(),
                    direction_inferred=inferred,
                    intraday=intraday,
                    base_close=base_close,
                )
            )
    return report.predictions, report


def close_on(index: pd.DataFrame, date: str) -> float:
    """发文日（或之前最近一个交易日）的收盘价。取不到返回 nan。"""
    try:
        stamp = pd.Timestamp(date)
    except (ValueError, TypeError):
        return float("nan")
    rows = index[index.index <= stamp]
    return float(rows["close"].iloc[-1]) if not rows.empty else float("nan")


# 每个方向"算他说对了"的判定词。上限/下限是反向口径：他说这里挡得住/托得住，
# 被穿了才算错，所以正确的判定词是"未破上限"和"支撑有效"。
GOOD_VERDICT = {"上行": "命中", "下行": "命中", "上限": "未破上限", "下限": "支撑有效"}


def judge(prediction: Prediction, index: pd.DataFrame, windows=DEFAULT_WINDOWS) -> None:
    """在发文后各档窗口内检验点位是否被触及，结果按窗口分别记录。"""
    start = pd.Timestamp(prediction.date)
    # 发文日必须落在行情数据覆盖区间内。否则"窗口"实际是从数据起点开始算的，
    # 2023 年的预测会被 2024-07 之后的行情检验 —— 那不是它的窗口，判定无意义。
    if start < index.index[0] or start > index.index[-1]:
        note = f"发文日 {prediction.date} 不在行情数据 {index.index[0].date()}~{index.index[-1].date()} 区间内"
        for window in windows:
            prediction.verdict[window] = "无数据"
            prediction.detail[window] = note
        return
    forward_all = index[index.index > start]
    for window in windows:
        forward = forward_all.iloc[:window]
        # 窗口未走完的（最近发的文章）不参与统计：用 30 根行情去判 120 日窗口，
        # 会把"还没到期"当成"没实现"，系统性压低命中率
        if len(forward) < window:
            prediction.verdict[window] = "无数据"
            prediction.detail[window] = f"发文后只有 {len(forward)} 个交易日，不足 {window} 日窗口"
            continue
        highest = float(forward["high"].max())
        lowest = float(forward["low"].min())
        if prediction.direction in ("上行", "上限"):
            prediction.extreme[window] = highest
            if prediction.direction == "上行":
                # 上行是他给的目标，摸到就算触及，不留容差
                prediction.verdict[window] = "命中" if highest >= prediction.point else "未达"
                prediction.detail[window] = f"窗口内最高 {highest:.2f}，目标 {prediction.point:.0f}"
            else:  # 上限：说这里挡得住，被有效突破才算错（0.6% 容差用他自己的定义）
                breached = highest >= prediction.point * (1 + BREACH_TOLERANCE)
                prediction.verdict[window] = "被突破" if breached else "未破上限"
                prediction.detail[window] = (
                    f"窗口内最高 {highest:.2f}，声称上限 {prediction.point:.0f}"
                    f"（超出 {(highest - prediction.point) / prediction.point * 100:+.2f}%，"
                    f"超 {BREACH_TOLERANCE * 100:.1f}% 才算突破）"
                )
        else:
            prediction.extreme[window] = lowest
            if prediction.direction == "下行":
                prediction.verdict[window] = "命中" if lowest <= prediction.point else "未达"
                prediction.detail[window] = f"窗口内最低 {lowest:.2f}，目标 {prediction.point:.0f}"
            else:  # 下限：说是支撑，被有效跌破才算错
                breached = lowest <= prediction.point * (1 - BREACH_TOLERANCE)
                prediction.verdict[window] = "支撑失效" if breached else "支撑有效"
                prediction.detail[window] = (
                    f"窗口内最低 {lowest:.2f}，声称支撑 {prediction.point:.0f}"
                    f"（低出 {(lowest - prediction.point) / prediction.point * 100:+.2f}%，"
                    f"低 {BREACH_TOLERANCE * 100:.1f}% 才算失效）"
                )
        if pd.notna(prediction.base_close):
            prediction.detail[window] += f"（发文日收盘 {prediction.base_close:.2f}）"


def hit_rate(items: list[Prediction], window: int) -> tuple[int, int, float]:
    """某档窗口下的命中数/样本数/百分比。只算该窗口有判定结果的条目。"""
    scoped = [item for item in items if item.verdict.get(window, "无数据") != "无数据"]
    if not scoped:
        return 0, 0, 0.0
    hits = sum(1 for item in scoped if item.verdict[window] == GOOD_VERDICT[item.direction])
    return hits, len(scoped), hits / len(scoped) * 100


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--index-csv",
        default="",
        help=f"上证指数日线 CSV（列含 date/open/high/low/close）。"
        f"不给就用默认缓存 {INDEX_CACHE.relative_to(ROOT)}，缓存不存在时自动从新浪下载",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=0,
        help="只跑单一窗口（交易日）。默认不给，一次出 20/60/120 三档",
    )
    parser.add_argument("--deadline-only", action="store_true", help="只统计带时间窗口的预测")
    parser.add_argument("--show", type=int, default=25, help="打印明细条数")
    parser.add_argument("--out", default="", help="明细写出到 CSV")
    args = parser.parse_args()

    windows = (args.window,) if args.window else DEFAULT_WINDOWS

    if args.index_csv:
        index_path = Path(args.index_csv)
        if not index_path.exists():
            raise SystemExit(f"找不到 {index_path}")
    else:
        index_path = INDEX_CACHE
        if not index_path.exists():
            download_index(index_path)
    index = load_index(index_path)
    print(f"行情数据：{index_path}")
    print(f"  {index.index[0].date()} ~ {index.index[-1].date()}（{len(index)} 根），"
          f"收盘中位 {float(index['close'].median()):.0f} 点")

    predictions, report = extract_predictions(CHUNKS, index)
    print(f"\n抽出显式点位预测 {len(predictions)} 条")
    print(
        f"  已排除：无法判定方向 {report.skipped_no_direction} 处、"
        f"不是点位(指数名/数量/年份) {report.skipped_not_a_level} 处、"
        f"超出点位合理区间 {report.skipped_out_of_range} 处、"
        f"引述他人观点 {report.skipped_quoted} 处、非上证标的 {report.skipped_other_symbol} 处、"
        f"复述已发生的事 {report.skipped_past_tense} 处、"
        f"目标方向仍判不出 {report.skipped_neutral_unresolved} 处、"
        f"阻力位但他预期会突破 {report.skipped_expects_breakout} 处、"
        f"点位在条件从句里 {report.skipped_conditional} 处、"
        f"复述早前给过的点位 {report.skipped_restatement} 处、"
        f"「没有跌破X」报告既成事实 {report.skipped_negated} 处、"
        f"假设句列举的走势 {report.skipped_hypothetical} 处"
    )
    if report.not_a_level_samples:
        print("  「不是点位」的样本（防的就是这类误判）：")
        for line in report.not_a_level_samples[:5]:
            print(f"    {line}")
    else:
        print("  注：NOT_A_LEVEL 命中 0 处是正常的 —— POINT 正则要求数字后必须带「点」字，"
              "「2013年」「3317只」本就不匹配。它防的是口径放宽后的误判，是道保险。")

    # 同一篇文章被切成多块时，同一句预测会重复抽出；按(日期,点位,方向)去重，
    # 否则一条预测按块数被计数多次，比率会被人为拉偏。
    unique: dict[tuple[str, float, str], Prediction] = {}
    for item in predictions:
        unique.setdefault((item.date, item.point, item.direction), item)
    duplicates = len(predictions) - len(unique)
    predictions = list(unique.values())
    if duplicates:
        print(f"  去重：同一(日期,点位,方向)重复 {duplicates} 条，保留首次出现")

    if args.deadline_only:
        predictions = [item for item in predictions if item.has_deadline]
        print(f"仅保留带时间线索的：{len(predictions)} 条")

    for prediction in predictions:
        judge(prediction, index, windows)

    # 至少有一档窗口能判的才算可检验样本
    judgeable = [
        item for item in predictions
        if any(item.verdict.get(window, "无数据") != "无数据" for window in windows)
    ]
    dropped = len(predictions) - len(judgeable)
    # 日内点位单独列，不进窗口统计：用 60 个交易日检验"今天的阻力位"是口径错误
    intraday = [item for item in judgeable if item.intraday]
    testable = [item for item in judgeable if not item.intraday]
    inferred = sum(1 for item in testable if item.direction_inferred)
    print(f"\n可检验 {len(testable)} 条（{dropped} 条发文日期不在行情覆盖范围或窗口未走完，"
          f"{len(intraday)} 条是日内级别点位，单独列出不计入窗口统计）")
    print(f"  其中方向由句式直接读出 {len(testable) - inferred} 条，由上下文推断 {inferred} 条")
    if not testable:
        print("没有可检验的预测，无法给出任何比率。")
        return 1

    label = f"{len(windows)} 档窗口" if len(windows) > 1 else f"{windows[0]} 日窗口"
    print(f"\n=== {label}触及率 ===")
    print("| 窗口 | 全部 | 上行 | 下行 | 上限 | 下限 |")
    print("|---|---|---|---|---|---|")
    for window in windows:
        cells = []
        for group in (testable,) + tuple(
            [item for item in testable if item.direction == direction]
            for direction in ("上行", "下行", "上限", "下限")
        ):
            hits, total, percentage = hit_rate(group, window)
            cells.append(f"{hits}/{total} = {percentage:.1f}%" if total else "—")
        print(f"| {window} 个交易日 | " + " | ".join(cells) + " |")

    for window in windows:
        tally: dict[str, int] = {}
        scoped = [item for item in testable if item.verdict.get(window, "无数据") != "无数据"]
        for item in scoped:
            tally[item.verdict[window]] = tally.get(item.verdict[window], 0) + 1
        parts = "，".join(f"{verdict} {number}" for verdict, number in sorted(tally.items(), key=lambda p: -p[1]))
        print(f"  窗口 {window} 日判定分布（{len(scoped)} 条）：{parts}")

    if intraday:
        # 用 1/3 个交易日单独判：他这类读数的有效期就是当天到次日
        for item in intraday:
            item.verdict.clear()
            item.detail.clear()
            item.extreme.clear()
            judge(item, index, INTRADAY_WINDOWS)
        print(f"\n=== 日内级别点位 {len(intraday)} 条（用 1/3 个交易日判，不进上表）===")
        print("   他的「今天的阻力位在X」是拿 30 分钟 K 线算的当日读数，有效期到次日。")
        print("   用 60 日窗口检验它，测出来的是「这段时间指数涨没涨」，不是他判断准不准。")
        for window in INTRADAY_WINDOWS:
            hits, total, percentage = hit_rate(intraday, window)
            print(f"   {window} 个交易日内：{hits}/{total} = {percentage:.1f}% 判对")
        for item in sorted(intraday, key=lambda row: row.date):
            marks = " ".join(f"{w}日:{item.verdict.get(w, '无数据')}" for w in INTRADAY_WINDOWS)
            print(f"   [{item.date}] {item.direction} {item.point:.0f} | {marks}")
            print(f"       {item.detail.get(INTRADAY_WINDOWS[0], '')}")
            print(f"       原文…{item.excerpt[:80]}…")

    print(f"\n=== 明细（前 {args.show} 条）===")
    reference = windows[len(windows) // 2] if len(windows) > 1 else windows[0]
    for item in sorted(testable, key=lambda row: row.date)[: args.show]:
        marks = " ".join(
            f"{window}日:{item.verdict.get(window, '无数据')}" for window in windows
        )
        flag = "（方向推断）" if item.direction_inferred else ""
        print(f"[{item.date}] {item.title[:22]} | {item.direction} {item.point:.0f}{flag}")
        print(f"    {marks}")
        print(f"    {item.detail.get(reference, '')}")
        print(f"    原文：{item.excerpt[:110]}")

    if args.out:
        frame = pd.DataFrame(
            [
                {
                    "date": item.date, "title": item.title, "direction": item.direction,
                    "direction_inferred": item.direction_inferred, "point": item.point,
                    "base_close": item.base_close, "has_deadline": item.has_deadline,
                    "intraday": item.intraday,
                    # 日内点位的 verdict 存的是 1/3 日窗口的结果，键不同，取并集
                    **{
                        f"判定_{window}日": item.verdict.get(window, "")
                        for window in tuple(windows) + INTRADAY_WINDOWS
                    },
                    **{
                        f"极值_{window}日": item.extreme.get(window, float("nan"))
                        for window in tuple(windows) + INTRADAY_WINDOWS
                    },
                    "excerpt": item.excerpt,
                }
                # 日内点位也写进 CSV（intraday 列标出），只是不进上面的比率
                for item in sorted(testable + intraday, key=lambda row: row.date)
            ]
        )
        frame.to_csv(args.out, index=False, encoding="utf-8-sig")
        print(f"\n明细已写出：{args.out}")

    print(
        "\n⚠️ 这是「显式点位在窗口内的触及率」，不是他的预测准确率。"
        "\n   他的多数判断在图里和条件句里（『如果那里是低点，可以介入』），规则抽不出来；"
        "\n   窗口长度也会直接改变数字 —— 三档一起看就是为了让这件事一眼可见，"
        "\n   单独引用某一档（尤其是最长那档）是在误导。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
