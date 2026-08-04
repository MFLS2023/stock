---
name: market-data-toolbox
description: A 股行情取数对照表，短线专用 + 盘中实时 + 盘后复盘全覆盖。任何需要"当前盘面""今天""现在""实时""复盘""龙虎榜""昨天涨停今天怎么走""为什么涨停""主线是什么""连板天梯"的场景先查这张表，直接调可用接口，不要逐个试错。涵盖涨停归因、题材聚集度、连板天梯、情绪周期、封板细节、炸板率、弱转强验证、历史分钟K线、日内形态量化、分钟资金流、龙虎榜席位的具体接口映射和已知故障。
---

# 行情取数对照表

**核心原则：库内资料是作者的历史观点，本表的接口给的是当前市场事实，两者必须分开陈述。**
引用数据时必须写明取数时间戳。

---

## 一律优先用 `live-market`（自建，29 个工具，实测最稳）

脚本 `live_market.py`（实时基础）+ `live_market_ex.py`（股池/历史/盘后）
+ `live_market_ths.py`（短线专用），MCP 名 `live-market`。
数据源东财 push2delay / push2ex / push2ex异动 / datacenter-web + 同花顺 data.10jqka
+ 腾讯 + 新浪，多源自动降级，全部免费无需注册。

接口清单来自 2026-08-04 的两轮全域探测（37 接口 29 可用 + 短线源 20 接口 10 可用），
模块层回归 12/12 + 18/18、MCP 端到端 17/17、错误路径 7/7 通过。

### 短线专用（主玩短线先看这组）

| 你要什么 | 调什么 | 返回要点 |
|---|---|---|
| **短线看盘第一个调这个** | `get_shortline_board` | 题材主线 TOP8 + 连板天梯 + 官方封板成功率 + 归因热词 TOP15，**约 2KB** |
| **某票为什么涨停** | `get_stock_reason` | 同花顺 AI 归因**全文**：行业原因 + 公司原因（带公告日期、互动易、业绩预告） |
| 涨停池带归因标签 | `get_zt_reason` | 每只票的涨停标签（如「算力租赁+英伟达合作+中报预增」）+ 官方封板成功率 |
| **连板天梯** | `get_ladder` | 按板数分层 + **自动算断层**（7板与4板之间空缺 = 高度不连续） |
| **主线判定** | `get_theme_top` | 每个题材涨停几只、连板几只、最高标几板、连续活跃几天 |
| 盘口异动 | `get_stock_changes` | 封涨停板/打开涨停板/火箭发射/高台跳水四码，**只有当日、不能回溯**。带各类型全市场家次 |
| 异动 type 对照 | `get_change_types` | 类型码表，**东财官方映射**（22 个码） |

同花顺三接口**都能回溯历史**（实测到 2026-06），传 `date` 即可。

### 盘中：情绪与结构

| 你要什么 | 调什么 | 返回要点 |
|---|---|---|
| **情绪周期位置**（先调这个） | `get_market_temperature` | 五指数、沪深涨跌家数、赚钱效应%、两市成交额、实时涨停/跌停数、涨停行业分布 |
| **涨停池详细版**（重点） | `get_zt_pool_detail` | **首次封板时间、最后封板时间、炸板次数、N天M板、封单额、封成比** + 换手/成交额/行业 |
| 炸板池 / 情绪转弱 | `get_zb_pool` | 曾涨停但已打开的票，配合涨停池算封板成功率 |
| **弱转强 / 反包验证** | `get_yesterday_zt` | 昨涨停股今日表现、溢价率、昨日首封时间、昨日连板数、红盘率 |
| 主线扩散标的 | `get_qs_pool` | 强势池，带是否创新高、连涨天数、量比 |
| 跌停池 | `get_dt_pool` | 返回 0 只是合法结果，不是故障 |
| 次新股 | `get_cx_pool` | 带上市天数、上市日，不混入涨停统计 |
| 个股实时 | `get_quote` | 价、涨幅、**量比**、换手、振幅、PE、市值、涨跌停价 |
| 日内强弱 | `get_intraday` | 当日 1 分钟分时 + 均价线 |
| 主线资金流向 | `get_sector_flow` | 行业/概念板块主力净流入、净占比、领涨股 |
| 盘中综合快照 | `get_full_snapshot` | 大盘+板块+涨停池+个股，一次约 8KB |
| 量比榜 | `get_strong_stocks` | 量比降序 |
| 涨停池简版 | `get_limit_up_pool` | 按板制判定，**要封板细节请用 `get_zt_pool_detail`** |

### 历史与形态：回溯约 60 个交易日

| 你要什么 | 调什么 | 返回要点 |
|---|---|---|
| **日内形态量化**（重点） | `get_intraday_shape` | **把"冲高回落"算成数字**：收距最高%、均价上方时间占比、日内最大回撤及时间、最高点在前/后半场 |
| 历史分钟 K 线 | `get_minute_kline` | 1/5/15/30/60 分钟，**可回溯**。n=320 约 7 日，n=1000 约 21 日，n=3000 约 63 日 |
| **分钟级资金流** | `get_fund_flow_min` | 主力/超大单/大单/中单/小单，**累计值不是增量**，带 30 分钟节奏采样 |
| 日线形态 / 连板数 | `get_daily_kline` | 前复权，最后一根是当日实时值 |

### 盘后：复盘与席位

| 你要什么 | 调什么 | 返回要点 |
|---|---|---|
| **每日复盘全套**（入口） | `get_after_close` | 一次取齐涨停/炸板/昨涨停/跌停 + 自动算封板成功率、炸板率、昨涨停赚钱效应 |
| 龙虎榜个股 | `get_lhb` | 上榜净买、占总成交比、上榜原因 + **上榜后 1/2/5/10 日涨跌幅** |
| **游资 vs 机构** | `get_lhb_dept` | 营业部明细，席位名含"机构专用"标为机构，带该席位 3 日胜率 |
| 大宗交易 | `get_block_trade` | 折溢价率、买卖双方营业部 |
| 两融 | `get_margin_detail` | 融资余额/买入额/融券余量。**这个接口约 8 秒，最慢** |

命令行同样可用（三个脚本各管一段）：

```bash
python _知识库系统/scripts/live_market.py market
```

```bash
python _知识库系统/scripts/live_market_ex.py close
```

```bash
python _知识库系统/scripts/live_market_ths.py board
```

---

## 两套涨停数据的分工（别重复调）

同一件事有两个源，各有独家字段，**问的不是同一个问题**：

| | 东财 `get_zt_pool_detail` | 同花顺 `get_zt_reason` |
|---|---|---|
| 独家字段 | 封单额、封成比、首末封板时间 | **涨停归因标签**、官方封板成功率 |
| 维度 | 钱（多少资金压在板上） | 逻辑（为什么涨） |
| 北交所 920xxx | 含 | **不含**（filter=HS,GEM2STAR） |
| 家数实测 | 137 只 | 136 只（差汉鑫科技 920092） |

**「连板数」两家口径不同，都对，别互相印证：**

| | 同花顺 `high_days` | 东财 `连板数` |
|---|---|---|
| 含义 | 最近 N 个交易日里涨停了 M 次（**不要求连续**） | 末尾**连续**涨停天数 |
| 利通电子实测 | `3天2板` | `1` |

利通电子 7/31 涨停、8/3 回调 -3.03%、8/4 再涨停 —— 同花顺记「3天2板」，
东财记「连板数 1」。这种形态短线里叫反包。
2026-08-04 用日线逐日核实 5 只票，两种口径各自 5/5 吻合。
**想知道是不是连续板看东财；想知道近期涨停密度看同花顺。**
`get_ladder` 的板数是连续口径（与东财一致）。

**顶层 `open_num` 与明细 `open_num` 也不是一回事：**
顶层 16 = 收盘没封住的（已不在涨停池）；明细 63 只 = 打开过又回封的（仍在池内）。

---

## 判读口径（照抄这几条，别自己发明）

**主线判定**（`get_theme_top` / `get_shortline_board`）：
- 涨停多 + 连板多 + 连续活跃天数长 = 主线
- 涨停多但连板少 = 一日游风险
- 归因热词 TOP15 是「今天资金在买什么逻辑」最快的读数

**情绪高度**（`get_ladder`）：
- 最高板 = 当前情绪能给到的天花板
- **断层**（如「7板与4板之间空缺」）= 高度不连续，接力意愿弱
- 2026-08-04 实测 5 个交易日，3 天有断层、2 天连续

**封板成功率**（`get_zt_reason` 的「涨停统计」，同花顺官方口径）：
- `封板成功率 = 收盘封板 / 曾涨停`
- 两条恒等式已在 5 个交易日 20 组数据上校验：
  `收盘封板 + 炸板 == 曾涨停`（20/20）、`rate == num/history_num`（20/20 浮点精确）
- 自带「上一交易日」对比，且**跨周末自动正确**（周一的 yesterday 指向上周五），
  比自己推算交易日可靠

**封板质量**（`get_zt_pool_detail`）：
- `首次封板` 早 = 资金抢筹果断
- `炸板次数` > 0 = 当天封板不牢，反复打开过
- `封成比` = 封单额 / 成交额，越高说明卖压越轻。实测同日四票分布 54% / 9.6% / 8.7%

**情绪延续**（`get_after_close`）：
- `封板成功率` = 涨停数 / (涨停数 + 炸板数)
- `昨涨停今日赚钱效应` = 昨涨停票今日红盘占比，这是弱转强能不能成立的前提

**冲高回落**（`get_intraday_shape`）：
- `收距最高%` 接近 0 = 收在最高，没有回落
- `收距最高%` 低于 -5% = 明显冲高回落
- `在均价上方占比` > 80% = 全天强势

**资金流口径**（`get_fund_flow_min`）：
- 是**累计值**，每个时间点表示当日开盘至该分钟的累计净流入，不是每分钟增量
- 字段已用两条恒等式校验：大单 + 超大单 = 主力；主力 + 小单 + 中单 = 0

---

## 数据发布时点（查不到先看这里，多半不是接口坏）

| 数据 | 发布时点 | 盘中查当天的表现 |
|---|---|---|
| 实时行情 / 股池 | 实时，延迟 1-3 秒 | 正常 |
| 龙虎榜 | **盘后** | 返回 `数据状态: 尚未发布` + 建议日期，不是故障 |
| 大宗交易 | **盘后** | 同上 |
| 两融 | **T+1** | 最新一条通常是上一交易日 |
| 日频资金流（第三方） | **盘后** | 当日无数据 |

东财数据中心的返回码语义（**必须区分，否则会把"没数据"当"接口坏"**）：

```
code=0     success=True   → 正常有数据
code=9201  success=False  → "返回数据为空"，查无数据，是合法结果
其他        success=False  → 真故障（如 reportName 写错报"报表配置不存在"）
```

盘后类工具的 `date` 传空会自动取最近已收盘交易日（15:00 前算今天没收盘）。
**该判定不含节假日日历**，遇长假可能落到休市日，此时返回 `数据状态` 而非假数据。

---

## 补充用 `stock_data`（第三方 MCP，部分接口坏）

`live-market` 没覆盖的走它。以下状态是 2026-08-04 盘中实测：

| 需求 | 接口 | 状态 |
|---|---|---|
| 筹码集中度 | `stock_chip` | 可用，给获利比例、90%/70%成本区间、集中度 |
| **历史日频资金流** | `stock_fund_flow` | 可用，超大单/大单十日明细。`live-market` 的分钟资金流只有当日，历史日频走这个 |
| 个股所属板块 | `stock_sector_spot` | 可用，含"昨日连板""最近多板"等情绪标签 |
| 盘中快讯 | `stock_news_global` | 可用，实时板块异动 + 涨停家数播报 |
| 北向资金 | `stock_north_flow` | 可用 |
| 市场估值分位 | `stock_market_pe_percentile` | 可用 |
| 龙虎榜统计 | `stock_lhb_ggtj_sina` | 可用。但要席位性质用 `live-market` 的 `get_lhb_dept` |
| ~~个股实时~~ | ~~`stock_realtime`~~ | **坏**，用 `get_quote` |
| ~~批量实时~~ | ~~`stock_batch_realtime`~~ | **坏**，同上 |
| ~~行业资金流~~ | ~~`stock_sector_fund_flow_rank`~~ | **坏**，用 `get_sector_flow` |
| ~~个股新闻~~ | ~~`stock_news`~~ | **坏**（JSON 解析失败） |
| ~~昨日涨停今表现~~ | ~~`stock_zt_pool(昨日涨停)`~~ | 可用但字段少，**改用 `get_yesterday_zt`**（带溢价率、昨日首封时间） |

坏掉的原因：该 MCP 实时行情类的 `EfinanceFetcher` 熔断器已跳（`data_source_status` 可查）。

## `akshare` 本机可用（前提是 import 前清代理）

**本机已装 akshare 1.17.80。** 上一版写「全挂」是误判 —— 根因（代理拦截）判断对了，
但少走一步：`requests.Session.trust_env = False` 只能管我自己建的 session，
**管不了 akshare 内部自建的 session**。必须在 `import akshare` **之前**清掉环境变量：

```python
import os
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
          "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "*"
import akshare as ak          # 顺序不能反
```

这么改之后 `ak.stock_zt_pool_em(date="20260804")` 0.5 秒返回 (138, 16)。

2026-08-04 逐接口实测（单进程单接口 + 外层 timeout，避免整批挂死）：

| akshare 接口 | 状态 | 我现在有没有 |
|---|---|---|
| `stock_changes_em`（异动四类） | 可用，火箭发射 2877 行 | 有（`get_stock_changes`） |
| `stock_zt_pool_em` | 可用 (138,16) | 有，且交叉校验同源 |
| `stock_lhb_yybph_em` 营业部排行 | 可用 (1178,17)，**带上榜后 1/2/3/5 日胜率** | **无** |
| `stock_yjyg_em` 业绩预告 | 可用 (4806,11) | **无** |
| `stock_gsrl_gsdt_em` 高送转 | 可用 (61,6) | **无** |
| `stock_fund_flow_big_deal` 大单追踪 | 可用 (5000,9) | **无** |
| `news_trade_notify_suspend_baidu` 停复牌 | 可用 (4,6) | **无** |
| `stock_zh_a_hist_pre_min_em` 竞价分时 | ConnectionError | 无 |
| `stock_hot_rank_em` / `stock_hot_up_em` 人气榜 | ConnectionError | 无 |
| `stock_board_concept_hist_em` 题材日线 | ConnectionError | 无 |
| `stock_individual_fund_flow` 资金流历史 | ConnectionError | 走 `stock_data` MCP |
| `stock_market_activity_legu` 赚钱效应 | AttributeError | 有（自算） |
| `stock_lhb_hyyyb_em` 游资一览 | TypeError | 部分 |
| `stock_info_global_cls` 财联社 | 超时 30s | 走 `stock_data` MCP |

**没接进 MCP 的原因：** akshare 是同步阻塞库，单接口 0.5-30 秒不等，且失败的那批
都是 ConnectionError（本机到东财某些域名不通，不是 akshare 的问题）。
真要用就在脚本里临时 import，别塞进常驻的 MCP server 拖慢启动。

## 不要用 `akshare_one`

2026-08-04 实测实时和历史接口全部 `Connection aborted`。
和 akshare 同一个根因（代理），但这个包没有 akshare 那样的接口广度，不值得再试。

---

## 本机网络的四个坑（改代码前必看）

1. **必须绕过系统代理**。本机代理 `127.0.0.1:7897` 会掐断东财请求，
   报 `RemoteDisconnected`。自己建的 session 设 `session.trust_env = False`；
   **第三方库（akshare 等）自建 session，管不到，必须在 `import` 之前 pop 掉
   proxy 环境变量**（见上一节代码）。这也是 `akshare_one` 全挂、
   `stock_data` 实时类熔断的共同根因。
2. **东财实时只有 `push2delay.eastmoney.com` 可达**。`push2his` / `82.push2` 连不通
   （`push2` 后来实测也可达，但不稳，仍以 push2delay 为主）。
   域名带 delay 但实测行情时间戳与本地时钟只差 1-3 秒，是实时数据。
3. **K 线不能走东财 push2delay**。它只做实时推送，`kline/get` 返回 `klines: []`；
   历史专用域名 `push2his` 本机不可达。日线走腾讯 `web.ifzq.gtimg.cn`，新浪兜底。
4. **分钟 K 线的域名不能带 `web.` 前缀**。`web.ifzq.gtimg.cn` 会被解析到
   `web3.ifzq.gtimg.cn` 然后报 `SSLError: UNEXPECTED_EOF_WHILE_READING`。
   必须用 `ifzq.gtimg.cn`，实测去掉 `web.` 后连打 5 次全成功。

另外三条：
- 翻页取全市场会被限频（连续 56 页在 17 秒后被断连）。涨跌家数用 `ulist.np` 一次取回。
- **腾讯分钟 K 单次硬上限 320 根**，请求 3000 也只回 320。长历史必须走新浪
  （`datalen` 无硬上限，5 分钟粒度 3000 根回溯到约 63 个交易日）。
- 腾讯 `data.gtimg.cn/flashdata/` 是 2021 年的过期缓存（`date:211008`），**废弃不用**。

---

## 已探测确认不可用的源（别再试）

| 源 | 状态 |
|---|---|
| 百度股市通 `finance.pae.baidu.com` | HTTP 403 |
| 网易 `quotes.money.163.com/service/chddata.html` | HTTP 502 |
| 网易 `api.money.126.net` | SSLError |
| 新浪逐笔下载 `downxls.php` | 返回"服务已下线" |
| 东财 `fflow/daykline`（日频资金流） | push2delay 返回空，push2his 不可达 |
| 东财 `trends2` 多日分时 | `ndays=5` 和 `iscr=0/1` 都只返回当日，历史日内走分钟 K |
| **开盘啦 `apphq.longhuvip.com`** | **反爬，放弃**。8 个端点实测：`DaBanList` 返回 200 但 37 个字段全被填成 `"kaipanla.com"` 字符串（投毒）；`RealRankingInfo`/`ZhiShuStockList` 返回空 list 但 errcode=0；其余 5 个返回空 body。换 APP 版 UA 无效 |
| 同花顺 `open_pool`/`limit_down_pool`/`limit_up_trend`/`limit_up_promote` | 404 `url不存在`。**换过 15 个路径变体全部 404**，别再猜路径。炸板池用东财 `get_zb_pool`，跌停池用 `get_dt_pool` |
| 东财 `RPT_CUSTOM_BLOCK_SECURITY` | 报表配置不存在 |

**同花顺踩过的两个坑（不是接口坏）：**
- `limit` 硬上限 **200**，传 300 直接报 `limit must be less than or equal to 200`
  并返回 `status_code=-1`。看到 status=-1 先查 limit，别当接口挂了
- 东财 `getAllStockChanges` **必须带 `dpt=wzchanges`**，不带返回 `rc=102 data=null`。
  上一轮误判为「接口坏」，加上这个参数就通了

**异动 type 码用官方映射，来自 akshare 源码**
（`site-packages/akshare/stock_feature/stock_pankou_em.py` 的 `symbol_map`，22 个码）。

上一版这里写着「官方名抓不到，只能按实测归纳」—— 那是错的，代价是 **22 个码里 15 个名字是错的**。
我在东财页面和 13 个 JS 里找了两轮 0 命中，就下结论说抓不到，却没想到查现成库的源码。
错得最离谱的几个：`8201` 我写「快速反弹」实为**火箭发射**、`8203` 我写「向上缺口」实为**高台跳水**、
`8213` 我写「有大买盘」实为**60日新高**。只有 `4/8/16/64/128/8193/8194` 七个猜对了。

**教训：自己归纳字段含义之前，先翻一遍现成库的源码。**

另外两个已修的坑：
- **东财 `type` 参数只认第一个码**，逗号后面的静默忽略。实测 `type=4,8201` 与 `type=4`
  返回完全一致（都是 344 条封涨停板）。我原来默认传 8 个码，**实际只查了封涨停板一类**。
  现在改成逐码请求再按时间倒序合并，每多一个码约 0.6 秒
- `pagesize` 传 1000 会截断（同一批 akshare 拿到 2877 行、我只有 1000 行），现改 5000。
  `tc` 字段不受 pagesize 影响，可以用小 pagesize 拿准确总数

码值说明：`256` / `512` 实测始终 0 条；`32`（打开跌停板）当日无跌停股时 0 条是合法结果；
`8217` 实测有数据（1390 家次）但官方表未收录，按「未收录码」标注。

**同花顺三接口对休市日返回 `success` + 空数据，不报错。** 实测 `20260101`（元旦）、
`20260802`（周日）都是 `info` 0 条，而 `limit_up_count.yesterday` 仍带数字但
**对不上真实的上一交易日**（20260802 的 yesterday 给 num=98，真实上一交易日 20260731 是 75）。
`zt_reason` / `ladder` / `theme_top` / `shortline_board` 现在都会返回 `数据状态: 无数据`，
看到这个字段就不要引用里面任何统计。

---

## 概念到数据的映射

库里的概念要落到具体读数才能验证：

| 库内概念 | 对应读数 |
|---|---|
| 情绪冰点 / 高潮 | 涨停数、赚钱效应%、跌停数、两市成交额（`get_market_temperature`） |
| **主线 / 人气核心** | `get_theme_top` 的涨停聚集度 + 连板只数 + 连续活跃天数 |
| **涨停归因 / 动因** | `get_stock_reason` 归因全文、`get_zt_reason` 的归因热词 |
| **板高 / 连板高度** | `get_ladder` 的最高板 + **断层** |
| **弱转强 / 反包** | `get_yesterday_zt` 的今日红盘率 + 逐只溢价率；同花顺「N天M板」里 M<N 就是反包形态 |
| **封板质量 / 一致性** | `get_zt_pool_detail` 的首封时间、炸板次数、封成比 |
| **情绪转弱** | `get_zb_pool` 炸板家数 + `get_zt_reason` 的官方封板成功率（今日 vs 上一交易日） |
| 承接 / 容量 | 换手率、量比、成交额（注意：容量在南京路指市场资金容量，在郁金香指个股换手承接能力） |
| 分歧 / 一致 | 炸板次数、量比、`get_intraday_shape` 的均价上方占比 |
| **冲高回落 / 长上影** | `get_intraday_shape` 的收距最高% |
| **均线低吸为何失效** | `get_daily_kline` 算 MA5 日位移，下跌趋势里标尺每天下移 |
| 筹码集中 | `stock_chip` 的 90%/70% 集中度 |
| **游资 / 机构** | `get_lhb_dept` 席位性质 + `get_fund_flow_min` 超大单 |

---

## 多源拼接的成本（别怕烧 token）

**建设成本是一次性的**，日常调用很省：

| 调用 | 耗时 | 返回体积 |
|---|---|---|
| `get_shortline_board` 短线全景 | 1.7-2.4s | **约 2.1 KB** |
| `get_ladder` 连板天梯 | 0.5s | 约 1.1 KB |
| `get_theme_top` 题材排行 | 0.7s | 约 2.4 KB |
| `get_stock_reason` 归因全文 | 0.8s | 约 0.6 KB |
| `get_zt_reason limit=8` | 0.5s | 约 2.5 KB |
| `get_zt_reason limit=200` | 0.6s | 约 30 KB |
| `get_theme_top with_stocks=True` | 0.8s | **约 71 KB（慎用）** |

控体积的三条：
- **看盘优先 `get_shortline_board`** —— 2 KB 拿齐主线 + 天梯 + 归因热词，够回答多数问题
- `get_zt_reason` 的 `limit` 只截明细，**统计和归因热词始终基于全部涨停股不失真**
  （已验证 limit=10/30/60/200 四档统计完全一致）
- `with_stocks=True`、`limit=200` 只在真要看成分时开

---

## 禁止

- 不执行交易，不连券商
- 不生成荐股结论或收益保证
- 取数失败就说失败，**不用模型记忆里的行情假装是实时数据**
- 不把作者历史观点写成当前市场规律
- 报数据必须带取数时间戳；盘后数据没发布就说没发布，不拿前一天的冒充当天
