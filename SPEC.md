# SPEC — 交易知识库

> 本文件是需求与验收的唯一依据。`AGENTS.md` 规定怎么干活，`CLAUDE.md` 记录当前状态，
> 本文件规定干什么、不干什么、干到什么程度算完。三者冲突时以本文件为准。
>
> 建立：2026-08-02　　修订：2026-08-02（Codex 审查退回后重订，见第 6 节）
> 范围：检索层修复（第 2 节）

---

## 0. 一句话目标

把交易博主的原始资料，蒸馏成用户审批过的、能照着做的方法卡。

**核心指标是方法卡数量，不是可检索记录数。** 检索是手段，方法卡是产出。
检索不准 → 蒸馏出的草稿就是错的 → 方法卡不敢批。所以先修检索。

---

## 0.1 两种来源口径：固定历史回归样本 vs 全部正式来源

本节区分两个**不可混用**的口径。混用过一次，代价是 SPEC 被写成"项目只有三个来源"。

### 口径一：固定历史回归样本（冻结）

`fulibei`、`nanjinglu_bian`、`tulip_garden` 三个来源被**冻结为回归样本**，
用途只有一个：给绝对数字（块数、命中数）提供一个不会漂移的比较基准。

这是**样本，不是项目的来源范围**。冻结的理由是技术性的：

- `build_index.py:137` 扫描 `source_libraries/` 下所有目录，不看 `sources.yaml`
  的 `status`，所以任何来源一落盘就自动进索引，全库绝对数字随之漂移
- 实测过这种漂移：新增一个来源后，索引从 194/676/3176 变为 223/736/3362，
  正文命中数同步变化（`竞价` 394→395、`筹码` 367→374、`龙头` 615→617、
  `打板` 186→194、`情绪` 888→897）

所以凡是**写死数字的断言**，都按 `source_id IN (回归样本)` 过滤后统计。
测试里对应常量 `REGRESSION_SAMPLE`。

### 口径二：全部正式来源（动态）

`sources.yaml` 中登记的**所有**正式来源，彼此同级，没有主次。
凡是**"检索必须能触达"性质的断言**（召回覆盖、单来源可查、作者可查、
跨来源综合），范围都是这一口径。测试里对应 `registered_sources()`，
**从 `sources.yaml` 动态读取**。

不硬编码来源清单，是因为再接入一个来源时不应该需要改测试文件才能覆盖到它。
`SourceRegistryTests` 守着这条：登记表、结构化目录、索引三者必须一致。

### 当前已接入的正式来源

| id | display_name | 块数 | 批准日期 | 角色 |
|----|--------------|------|---------|------|
| `fulibei` | 复利杯 | 1470 | — | 回归样本 |
| `nanjinglu_bian` | 南京路彼岸 | 525 | — | 回归样本 |
| `tulip_garden` | 郁金香花园 | 1181 | — | 回归样本 |
| `panfeng` | 我有上将潘凤 | 186 | 2026-08-02 | 正式来源 |

以后新增的来源直接进第二个口径，本表不是长期架构，登记表才是唯一事实来源。

### 基线指纹

| 项目 | 值 |
|------|---|
| 基线时间 | 2026-08-02 |
| 回归样本 | fulibei / nanjinglu_bian / tulip_garden |
| 样本块数 | 1470 / 525 / 1181 = 3176 |
| 样本原始文件数 | 110 / 42 / 377 = 529 |
| manifest 状态 | 样本 529 个文件 unchanged |
| 全库块数（会随接入变化，不作基线） | 3362 |
| Git 标签 | `p0-fixed-20260802` |

`build_index.py` 应按 `status` 过滤来源、而不是无条件扫目录，是一个真实缺陷
（未审批的来源会自动进索引）。属于导入/索引层，**不在本轮范围**，记入第 5 节
后续轮次；本轮由 `SourceRegistryTests` 检测登记表与索引不一致来兜住。

---

## 1. 项目边界（长期有效）

### 1.1 要做的

| 能力 | 含义 |
|------|------|
| 单来源定位 | 问"郁金香怎么讲筹码断层"，只返回郁金香的内容，带可回溯定位 |
| 跨来源比较 | 问"各来源对竞价的看法"，凡有相关内容的正式来源都要出现，分歧并列不合并 |
| 引用可回溯 | 每条结论能回到 `source_libraries` 的结构化片段，再回到原始文件 |
| 历史与当前分离 | 库内只说"作者历史上怎么说"；当前事实靠外部核验，注明截止日期 |
| 可信度分层 | 说清用的是方法卡、Parent、Chunk 还是模型常识 |
| 新来源可扩展 | 加来源走 `register_source.py --dry-run`，不逐文件手工处理 |

### 1.2 不做的

- 不执行自动交易，不连接券商
- 不生成荐股结论或收益保证
- 不把作者历史观点当作当前市场规律
- 不把作者案例改写成用户自己的交易经历
- 不在没有固定评测证据的情况下引入向量数据库
- 不在未 dry-run 的情况下直接导入新来源
- 不让语言模型心算关键数字（涨跌幅、胜率、盈亏比、回撤、仓位）

### 1.3 不可触碰的东西

- 各来源的原始资料目录：只读。不得覆盖、改名、移动或写入派生内容。
  当前为 `复利杯/`、`南京路彼岸/`、`郁金香花园付费文章文档版/`、`飞书聊天记录_潘凤/`；
  以 `sources.yaml` 的 `source_path` 为准，新增来源自动适用本条
- 原始目录名与 `source_id` 无需一致：`panfeng` 的原始目录是 `飞书聊天记录_潘凤/`，
  载体名保留原样，只有结构化产物目录 `source_libraries/panfeng/` 跟随 id
- `_知识库系统/personal/`：用户个人交易日志，不得混入外部来源观点
- 高风险操作（`git reset --hard`、`push --force`、`clean -f`、删库、批量删除）须先说明并等确认

---

## 2. 本轮范围：检索层修复

### 2.1 为什么要修

数据层已经可读（2026-08-02 修完郁金香切块与南京路双路抽取），但检索层有三个缺陷，
使得"跨来源比较"这项核心能力实际不可用。

以下全部是 2026-08-02 在 `knowledge.db`（3176 块）上只读实测得到，不是估算。

#### 缺陷 A：FTS 索引被 topics 标签污染

`build_index.py:89` 把 `topics` 列一起塞进 `chunks_fts`。而 topics 由
`infer_topics()` 按关键词计数自动打，每块平均 5-6 个，导致标签命中量级压过正文命中：

| 查询词 | FTS 全列命中 | 正文真含 | topics 含 | 噪声占比 |
|-------|-----------|--------|---------|--------|
| `情绪周期` | 1823 | 202 | 1778 | 89% |
| `龙头与核心` | 1358 | 0 | 1358 | 100% |
| `竞价与盘口` | 1525 | 0 | 1525 | 100% |

后果：bm25 在噪声上排序，正文里真讲情绪周期的 202 条被 1778 条只挂了标签的块淹没。

#### 缺陷 B：两字词不进 FTS

`query_kb.py:34` 主动丢弃长度 < 3 的检索词（`fts_terms = [term for term in terms if len(term) >= 3]`）。
trigram tokenizer 需要至少 3 字符才能建 gram，所以两字词直接 FTS 命中 0：

| 查询词 | FTS 命中 | 正文真含 |
|-------|--------|--------|
| `竞价` | 0 | 394 |
| `筹码` | 0 | 367 |
| `龙头` | 0 | 615 |
| `打板` | 0 | 186 |
| `情绪` | 0 | 888 |

而交易术语大量是两字词，这批词是最高频的检索入口。

#### 缺陷 C：LIKE 回退候选池按插入顺序截断，结果系统性偏向单一来源

FTS 空手而归时走 `query_kb.py:48-61` 的 LIKE 回退。候选池上限是
`max(limit * 30, 120)`，默认 `--limit 8` 时为 240。该查询**没有 `ORDER BY`**，
SQLite 按 rowid（即插入顺序）返回，而 `build_index.py:137` 按目录名字母序导入，
`fulibei` 最先入库：

**口径说明（重要）：** LIKE 回退用四字段 OR（`text`/`title`/`author`/`topics`），
其中 `topics` 是自动标签。下表把口径拆开——混用会得出错误结论。
下表数字为**回归样本口径**，列顺序 fulibei / nanjinglu_bian / tulip_garden。

| 查询词 | 四字段 OR 合计 | 候选池实取 | 候选池来源分布 | 池内正文真含 | 四字段 OR 分布 | **正文（text）分布** |
|-------|-----------|---------|------------|--------|------------|----------------|
| `竞价` | 1615 | 240 | **240 / 0 / 0** | 79 | 343 / 132 / 1140 | 118 / 31 / **245** |
| `龙头` | 1652 | 240 | **240 / 0 / 0** | 110 | 1008 / 412 / 232 | **381** / 161 / 73 |
| `打板` | 1397 | 240 | **240 / 0 / 0** | 84 | 540 / 319 / 538 | **150** / 9 / 27 |
| `情绪` | 2126 | 240 | **240 / 0 / 0** | 97 | 884 / 475 / 767 | 369 / 150 / 369 |
| `筹码` | 1151 | 240 | 52 / 47 / 141 | 131 | 52 / 47 / 1052 | 52 / 8 / **307** |

**后果只有一层：来源偏斜。** 五个高频词里四个的候选池 100% 是复利杯。以 `竞价`
为例，郁金香有 **245 条正文**真讲竞价，一条都进不了候选池。

**更正一条早先写错的结论。** 初版这里写"候选池 240 条里正文真含 0 条，全是标签
噪声"。实测不成立：真库候选池里正文真含 79–131 条，且 `relevance()` 里
`text.count` 上限 8 分，足以把它们排到前面——`--limit 8` 的前 8 条实测
**正文真含 8/8**（`打板` 在样本口径下是 7/7，第 8 条来自样本外的 `panfeng`）。
那个 0/8 是我构造的 fixture 数据，fixture 刻意让复利杯前 280 块只挂标签，
与真库形态不同。

所以缺陷 A（标签污染）在真库上主要伤害的是 **FTS 路径的精度**（`情绪周期`
89% 噪声），不是回退路径的结果质量；缺陷 C 伤害的是 **覆盖面**。两者不叠加成
"结果全是噪声"。这条更正对应的回归测试：
`RealIndexTests.test_default_limit_already_returns_prose_matches`（现为通过基线，
防止后续修改用精度换覆盖）。

**不据此设计来源配额。** 正文分布显示各来源的强项不同（`龙头` 复利杯最多、
`筹码` 郁金香最多），强制配额会把"该来源确实没讲这个"歪曲成"必须凑一条出来"。
本轮只保证候选池能看到全部来源，不保证每个来源都出现在结果里。

同时 `relevance()`（`query_kb.py:63-79`）给 topics 的权重是 4.0，高于正文的 1.0，
而 topics 是自动打的噪声标签：

| 查询词 | 正文真含 | 仅 topics 含（正文不含） |
|-------|--------|-------------------|
| `竞价` | 394 | 1221 |
| `筹码` | 367 | 784 |
| `龙头` | 615 | 926 |

这些"只挂了标签、正文根本没提"的块会被排到正文真讲该概念的块前面。

### 2.2 检索契约（先定契约，再分阶段）

原计划把"候选池去偏斜 + 修正评分"混在一个阶段里，无法分别验证。
先把职责切成四层，每层由哪个阶段负责、用什么证明，一次写清。

| 层 | 职责 | 由谁负责 | 怎么证明 |
|---|------|---------|---------|
| **召回** | 找出所有可能相关的块，不遗漏 | 阶段 2 | 召回数 = `text`/`title`/`author` 三字段去重并集（可枚举核对） |
| **去重** | 同一 chunk_id 只出现一次 | 阶段 2 | 结果里 chunk_id 唯一 |
| **评分** | 把候选排出先后 | 阶段 3 | 正文命中排在纯标签命中之前 |
| **确定性** | 同一查询两次结果完全相同 | 阶段 3 | 连续两次调用结果逐条相等 |

**来源覆盖不是单独一层**，它是召回层等值断言的推论：召回等于三字段并集，
就意味着没有来源被整体丢弃。小 limit 的结果里**不检查来源数量**（见阶段 3）。

覆盖断言的范围是 `sources.yaml` 全部正式来源（0.1 节口径二），不是回归样本。
但**逐词动态计算**"该词在哪些来源有命中"，不要求每个词覆盖全部来源：
实测 `弱转强` 在 `panfeng` 是 0 条（186 块的口语聊天记录，术语密度低），
写成"每词必须覆盖全部来源"会永远失败，且会诱导实现去凑结果。

具体契约：

**召回（阶段 2）**
- 每个检索词独立召回，不因长度被丢弃
- 召回字段：`text` / `title` / `author` 三个，**不含 `topics`**。
  三字段去重并集，同一 chunk 被多个字段命中只算一次
- 两字及以上词：在 `chunks_fts` 上用 `GLOB '*词*'`，实测走索引
  （`VIRTUAL TABLE INDEX 0:G4`，18.2ms vs `chunks` 全表扫 191.4ms，结果一致）。
  三个字段各自 GLOB 后取并集
- 三字及以上词：继续用 `MATCH`，bm25 可用
- 多词查询语义为 **OR**（与现状一致），任一词命中即入候选
- 候选池**不设上限**。全库仅 3176 块，全量候选实测代价可接受，
  不引入分层采样等复杂度

**去重（阶段 2）**
- 一个 chunk 被多条路径命中时，只保留一份
- 保留哪一份由评分决定，与召回顺序无关

**评分（阶段 3）**
- 统一到一个可比标尺。bm25 是负值且尺度与 `relevance()` 不同，
  不能直接混用；阶段 3 定义换算规则
- 字段权重：`text` ≥ `title` > `topics`。`topics` 是自动标签，
  权重不得高于正文（现状 4.0 vs 1.0，方向是反的）
- 多词命中数越多排越前

**确定性（阶段 3）**
- 排序末尾加 `chunk_id` 作为决胜字段，消除并列时的不确定顺序

### 2.3 本轮要交付的

按阶段推进，一次一阶段，每阶段跑测试、汇报、停下等审查。

| 阶段 | 内容 | 缺陷 | 验证什么 |
|------|------|------|---------|
| 0 | 建检索层回归测试 | — | 三个缺陷可被测出 |
| 1 | 把 topics 从 FTS 可匹配列移出 | A | 命中数收敛到正文 |
| 2 | 召回：两字词打通 + 全量候选 + 去重 | B、C 的召回部分 | 召回不遗漏、不重复 |
| 3 | 评分：权重修正 + 确定性排序 | C 的排序部分 | 正文优先、结果稳定 |
| 4 | 导入成功性凭证 | 附带 | 导入失败能被验证发现 |

**阶段 2 和 3 的分界**：阶段 2 只管"找得到"，用大 limit 查全量召回，不看顺序；
阶段 3 只管"排得对"，用小 limit 查前几条，不看总数。这样阶段 2 改了召回路径后，
阶段 3 的测试仍然有效——它测的是排序，与走哪条召回路径无关。

阶段 2 采用查询层方案（改 `query_kb.py`），**不换 tokenizer**。
理由：换 tokenizer 要重建索引且会重排全库召回特性，改动不可逆；
查询层方案改动局部、可回滚。已实测 `GLOB` 在 trigram 表上走索引，
两字词无需换 tokenizer 即可被检索，方案可行性已验证。

### 2.4 本轮不做的

- 不换 FTS tokenizer（除非阶段 2 验收失败）
- 不重新导入任何来源（阶段 1 只重建索引，不重跑导入器）
- 不动 `claim_type` 固定值问题（P1，下轮）
- 不做复利杯闲聊标记（P1，下轮）
- 不做方法卡管道与 `status` 字段（待用户定义门槛后另开一轮）
- 不引入向量检索

---

## 3. 怎样算完成

### 3.0 测试命令与 expectedFailure 生命周期

**命令（从项目根目录执行，不依赖当前工作目录）**

两个 shell 各给一版。Windows 上双击打开终端默认是 **PowerShell**，
下面 bash 版的 `export`、`$HOME`、`VAR=x cmd` 前置赋值在 PowerShell 里都不成立。

<b>PowerShell（Windows 默认）</b>

```powershell
Set-Location "C:\Users\20577\Documents\炒股\知识库"
$env:PYTHONIOENCODING = "utf-8"
$PY = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

# 单元测试：必须指定 -t（顶层目录）和 -s（用例目录）
& $PY -m unittest discover -s _知识库系统/scripts -t _知识库系统/scripts -v
"退出码=$LASTEXITCODE"

# 验收时加上这个环境变量：索引缺失从静默 skip 变成硬失败
$env:KB_REQUIRE_REAL_INDEX = "1"
& $PY -m unittest discover -s _知识库系统/scripts -t _知识库系统/scripts -v
"退出码=$LASTEXITCODE"
Remove-Item Env:KB_REQUIRE_REAL_INDEX      # 用完清掉，避免污染后续会话
```

<b>Git Bash</b>

```bash
cd "C:/Users/20577/Documents/炒股/知识库"
export PYTHONIOENCODING=utf-8   # Git Bash 下中文输出需要
PY="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe"

"$PY" -m unittest discover -s _知识库系统/scripts -t _知识库系统/scripts -v
echo "退出码=$?"

KB_REQUIRE_REAL_INDEX=1 \
  "$PY" -m unittest discover -s _知识库系统/scripts -t _知识库系统/scripts -v
echo "退出码=$?"
```

**取退出码时不要接管道。** `cmd | tail -5` 之后 `$?` 是 `tail` 的退出码，
不是测试的。已实测踩过这个坑：`unexpected success` 本该是 1，接了管道显示成 0。
要么不接管道，要么用 `${PIPESTATUS[0]}`（bash）。

系统默认的 `Python312` 缺 `pypdf`/`PIL`，跑不起来导入器测试，必须用上面这个运行时。

**`-m unittest test_query_kb` 会失败**，因为从项目根目录跑时 `sys.path` 里没有
`scripts` 目录。已实测：`FAILED (errors=1)`，退出码 1。必须用 `discover`。

**expectedFailure 的生命周期（硬规则）**

`unittest` 遇到 `unexpected success` 时**退出码是 1**，不是"全绿"。已实测确认。
因此：

1. **已完成阶段**的实现与"删除该阶段对应的 `expectedFailure` 装饰器"必须在
   **同一个提交**内完成。装饰器留着不删，修复一落地就变成 `unexpected success`，
   退出码 1，红灯
2. **尚未实施的后续阶段允许继续保留 `expectedFailure`。** 那些标记描述的是本轮后续
   阶段才动手的缺陷，此刻失败正是它们该有的状态，不是本阶段的遗留问题
3. 输出里**不得出现 `unexpected successes` 或 `skipped`**。这两项各自对应一个真实
   故障：前者是"修好了但标记没摘"，后者是"这批断言根本没跑"（索引缺失时
   `KB_REQUIRE_REAL_INDEX=1` 把静默 skip 变成硬失败，见 3.2 节表格）
4. 剩余的 `expected failures` **必须全部属于后续阶段**。每阶段的验收报告要报出
   剩余数量和阶段归属，逐项对得上号——只报总数不算，混进一条本阶段该修的
   就是验收不通过
5. **阶段 3 完成后 `expected failures` 才必须归零。** 在那之前的每一阶段，
   非零是允许的中间状态，前提是第 4 条能逐项交代清楚
6. 每阶段的验收报告必须贴出 `Ran N tests ... OK` 那两行、退出码，以及 N 的分文件组成
   （见 3.2 节末）

**各阶段允许剩余的标记数**

| 阶段完成时 | 允许剩余 | 归属 |
|-----------|---------|------|
| 阶段 0 | 15 | 阶段 1/2/3 各自的缺陷 |
| **阶段 1** | **12（fixture 9 + 真库 3）** | **全部属于阶段 2/3** |
| **阶段 2** | **1（fixture 1 + 真库 0）** | **全部属于阶段 3** |
| 阶段 3 | **0** | — |

阶段 1 剩余 12 项的逐项归属（2026-08-02 实测，`grep -c` 计数 12，与运行输出的
`expected failures=12` 一致）：

| # | 用例 | 所属类 | 子集 | 阶段 |
|---|------|-------|-----|------|
| 1 | `test_two_character_term_must_not_need_the_fallback` | ShortTermSearchTests | fixture | 2 |
| 2 | `test_two_character_term_must_reach_full_recall_without_the_fallback` | ShortTermSearchTests | fixture | 2 |
| 3 | `test_title_and_author_only_matches_must_be_recalled` | ShortTermSearchTests | fixture | 2 |
| 4 | `test_recall_layer_must_reach_every_source_that_holds_matches` | SourceCoverageTests | fixture | 2 |
| 5 | `test_fallback_must_not_leak_label_only_matches_at_large_limits` | SourceCoverageTests | fixture | 2 |
| 6 | `test_prose_matches_must_outrank_label_only_matches` | SourceCoverageTests | fixture | **3** |
| 7 | `test_short_term_must_contribute_in_a_mixed_length_query` | RetrievalContractTests | fixture | 2 |
| 8 | `test_multiple_two_character_terms_must_not_return_label_only_chunks` | RetrievalContractTests | fixture | 2 |
| 9 | `test_multiple_two_character_terms_must_both_contribute` | RetrievalContractTests | fixture | 2 |
| 10 | `test_two_character_terms_must_be_recalled_without_the_fallback` | RealIndexTests | 真库 | 2 |
| 11 | `test_recall_layer_must_reach_every_source_that_holds_matches` | RealIndexTests | 真库 | 2 |
| 12 | `test_recall_layer_must_reach_every_registered_source_that_holds_matches` | RegistryScopedIndexTests | 真库 | 2 |

11 项是召回与兜底（缺陷 B、C 的召回部分，阶段 2）；第 6 项是排序权重
（`relevance()` 给 topics 4.0 高于正文 1.0，缺陷 C 的排序部分，阶段 3）。
第 4 项与第 11 项同名但不同类：一个跑 fixture、一个跑真库，`-k` 分两个子集各跑一次。

阶段 2 剩余 1 项（2026-08-02 实测，`grep -c` 计数 1，与运行输出的
`expected failures=1` 一致）：

| # | 用例 | 所属类 | 子集 | 阶段 |
|---|------|-------|-----|------|
| 1 | `test_prose_matches_must_outrank_label_only_matches` | SourceCoverageTests | fixture | **3** |

就是上表的第 6 项，阶段 1、阶段 2 都不属于它的范围，两轮都原样留着。
上表其余 11 项已在阶段 2 摘除（实现同提交），其中 9 项按"修复后的事实"改写并改名
（名字里的 `must_` / `fallback` 去掉——兜底路径已删除），2 项仅去掉装饰器保留原名。

阶段 1 摘除的三项（实现同提交）：
`IndexPollutionTests.test_topic_label_must_not_match_full_text_search`、
`IndexPollutionTests.test_hits_must_converge_on_prose_matches`（改名为
`test_hits_converge_on_the_recall_target`，口径改为三字段并集）、
`RealIndexTests.test_topic_labels_must_not_dominate_full_text_search`（改名为
`test_topic_labels_do_not_dominate_full_text_search`）。三项已转为普通断言，
连同新增的 6 条守卫一起防止 topics 被塞回 FTS。

### 3.1 各阶段验收

数字全部来自 2026-08-02 实测基线，**按回归样本过滤后统计**（0.1 节口径一）。
验收时用同样的只读查询复核。

"检索必须能触达"性质的检查项另有一张表，范围是全部正式来源（口径二），
见 3.1 节末「跨来源可达性验收」。

#### 阶段 0：检索层回归测试

- 从项目根目录用 3.0 节的命令可运行
- `test_kb_import_utils` 27 项不受影响
- 测试覆盖三个缺陷，每个缺陷至少一条 fixture 用例 + 一条真库用例
- 阶段 0 是唯一允许留 `expectedFailure` 的提交

#### 阶段 1：FTS 去污染

| 检查项 | 通过标准 | 实测（2026-08-02 阶段 1 完成后） |
|-------|---------|------------------|
| `情绪周期` FTS 命中 | 1823 → 516（收敛到三字段并集） | 516 ✅ |
| `龙头与核心` FTS 命中 | 1358 → 0（该词三字段里根本不存在） | 0 ✅ |
| `弱转强` FTS 命中 | 111 保持不变（正文 87 + 标题 24，标题命中保留） | 111 ✅ |
| `筹码断层` FTS 命中 | 52 保持不变 | 52 ✅ |
| 回归样本块数 | 1470 / 525 / 1181 不变 | 一致 ✅ |
| 登记表与索引一致 | `sources.yaml` 的 id 集合 = 索引里 `DISTINCT source_id` | 4 个来源一致 ✅ |
| `PRAGMA integrity_check` | ok | ok ✅ |
| `metadata.fts_tokenizer` | 仍为 `trigram` | trigram ✅ |

标题（`title`）和作者（`author`）列**保留**在 FTS 中——那是人写的真实文本，不是自动标签。

**收敛口径是 `text`/`title`/`author` 三字段并集，不是正文单列。** 初版这一行写的是
"`情绪周期` → 202 ± 5（收敛到正文真含数）"，与紧接的上一句自相矛盾：真库里
`情绪周期` 有 314 块是标题含词、正文不含，要收敛到 202 就必须把 `title` 一起移出
FTS，而那正是本节禁止的。同表 `弱转强` 那一行（"正文 87 + 标题保留"）用的已经是
并集口径，两行口径不一致。阶段 2 的表格（正文 202 + 标题作者独有 314 = 并集 516，
"召回不低于 516"）和阶段 1 对应的 `expectedFailure`（断言"命中数不超过三字段并集"）
同样指向并集，所以按并集口径改正，并把实测值直接写进表里。

`弱转强` 的标题独有命中实测是 24 条（87 + 24 = 111），初版写的 31 是
`FTS title 列 MATCH` 数，那个口径与正文有 7 条重叠，不能相加。总数 111 两种算法一致。

移出 FTS 不等于删数据：`topics` 仍在 `chunks` 表里（样本 3176 块全部有值），
检索结果的"主题:"一行和 `relevance()` 照旧读它，只是不再参与全文匹配。
`chunks_fts` 行数不变（3362），变的只是列定义。

#### 阶段 2：召回

**固定词表**（避免"三字词不得下降"这种无法穷举证明的表述）。
基线为回归样本过滤后的正文命中数：

**召回口径：`text` / `title` / `author` 三字段去重并集，排除 `topics`。**

初版写"召回 = 正文命中数"，与阶段 1「标题和作者列保留在 FTS 中」直接冲突。
只对 `text` 做 GLOB 会丢掉一批合法结果——实测样本中"仅标题或作者含词、
正文不含"的块数：

| 词 | 正文 | 标题/作者独有 | 三字段并集 | 仅 topics 含 | 四字段 OR |
|---|-----|---------|---------|-----------|--------|
| `竞价` | 394 | **58** | 452 | 1163 | 1615 |
| `筹码` | 367 | **48** | 415 | 736 | 1151 |
| `龙头` | 615 | **310** | 925 | 727 | 1652 |
| `打板` | 186 | 0 | 186 | 1211 | 1397 |
| `情绪` | 888 | **406** | 1294 | 832 | 2126 |
| `弱转强` | 87 | 24 | 111 | 0 | 111 |
| `情绪周期` | 202 | 314 | 516 | 1307 | 1823 |
| `筹码断层` | 52 | 0 | 52 | 0 | 52 |

`龙头` 会丢 310 条、`情绪` 会丢 406 条。这些块的标题里真写着这个词，属于合法
结果，不是标签噪声。实测这些独有命中**全部来自 `title`，`author` 独有为 0**，
但契约仍纳入 `author`：它是人写文本，且 `--author` 过滤依赖该列。

**通过标准**（数字为回归样本过滤后的实测值）：

| 词 | 字数 | 三字段并集基线 | 通过标准 | 实测（阶段 2 完成后） |
|---|-----|----------|---------|-----------------|
| `竞价` | 2 | 452 | 召回 = 452 | 452 ✅ |
| `筹码` | 2 | 415 | 召回 = 415 | 415 ✅ |
| `龙头` | 2 | 925 | 召回 = 925 | 925 ✅ |
| `打板` | 2 | 186 | 召回 = 186 | 186 ✅ |
| `情绪` | 2 | 1294 | 召回 = 1294 | 1294 ✅ |
| `弱转强` | 3 | 111 | 召回不低于 111 | 111 ✅ |
| `情绪周期` | 4 | 516 | 召回不低于 516 | 516 ✅ |
| `筹码断层` | 4 | 52 | 召回不低于 52 | 52 ✅ |

实测数字为回归样本过滤后的值，与并集逐条集合相等（不只是计数相等）：
五个两字词的召回集合与三字段并集 `==` 成立，标签块漏入 0 条；
三字及以上词的并集是召回的子集，实测恰好相等。全库口径同样相等：
453 / 422 / 927 / 194 / 1303，与下面那张参照表一致。

两字词要求**等于**三字段并集（`GLOB` 是精确子串，可枚举核对）；
三字及以上词要求**不低于**基线（`MATCH` 的分词方式与子串不同，可以更多）。

**上界同样要卡住**：召回数不得达到四字段 OR 的量级（`竞价` 1615、`情绪` 2126），
否则说明 `topics` 又漏进召回了。测试里对两字词用等值断言，天然卡住上界。

**全库口径参照表**（2026-08-02 实测，`sources.yaml` 全部正式来源）。
上面那张表是回归样本口径，用于比对冻结数字；这张表是验收实际覆盖面的口径，
且不写进测试断言——测试里按 `registered_sources()` 动态计算，接入新来源自动更新：

| 词 | 样本并集 | 全库并集 | 增量 | 全库四字段 OR | 持有命中的来源 |
|---|--------|--------|-----|------------|------------|
| `竞价` | 452 | 453 | +1 | 1620 | 全部 4 个 |
| `筹码` | 415 | 422 | +7 | 1158 | 全部 4 个 |
| `龙头` | 925 | 927 | +2 | 1661 | 全部 4 个 |
| `打板` | 186 | 194 | +8 | 1405 | 全部 4 个 |
| `情绪` | 1294 | 1303 | +9 | 2161 | 全部 4 个 |
| `弱转强` | 111 | 111 | 0 | 111 | 3 个（`panfeng` 无命中） |
| `情绪周期` | 516 | 516 | 0 | 1850 | 3 个（`panfeng` 无命中） |
| `筹码断层` | 52 | 52 | 0 | 52 | 1 个（仅 `tulip_garden`） |

最后一列是**逐词动态计算**的结果，不是配额。`筹码断层` 只有一个来源持有，
`弱转强` 有三个——这是内容事实，不是缺陷。覆盖断言的表述必须是
"每个**真的持有命中**的来源都要出现"，不能是"每个登记来源都要出现"。

前五个词今天恰好全部 4 个来源都有，**这个巧合不许写成断言**。曾经写过
`assertEqual(set(有命中的来源), set(全部登记来源))`，意思是"这几个词必须每个来源都有"；
只要某个来源换一批选材，或新来源讲的是别的品种，它就会失败，而检索层完全正确。
命中面是内容属性。断言只保留两条：召回集合等于三字段并集、返回来源等于**该词实际
持有来源**；另加一条防空转（这些词在全库不能一条命中都没有，否则两边都是空集会假通过）。

**统计口径**：`chunks_fts` 表里没有 `source_id` 列，所以直接 GLOB 的数字是全库值。
核对冻结数字时必须 `JOIN chunks` 再按回归样本过滤。实测正文口径两种统计的差值是
1/7/2/8/9 条，来自样本外的 `panfeng`。测试中不得硬编码任何一侧的绝对数字：
样本侧用 `REGRESSION_SAMPLE` 常量过滤后与冻结值比较，全库侧用
`registered_sources()` 动态计算。

附加（均已实测通过）：
- 结果里 `chunk_id` 唯一，无重复
- 空查询、纯标点查询不抛异常
- `--source` / `--author` 过滤仍生效
- 混合长短词（`竞价 弱转强`）两个词都参与召回，不再静默丢弃短词

**实现方式**（阶段 2 落地，仅改 `query_kb.py`）：全部检索词都 ≥3 字时走一次 `MATCH`
并用 bm25 排序，与之前一致；一旦出现两字词，候选集改为 `MATCH` 命中与三列
`GLOB '*词*'` 命中的 `UNION`，由 `relevance()` 排序。两把标尺不在同一次查询里混用——
bm25 是负值且尺度与 `relevance()` 不同，换算规则归阶段 3。

**用户输入先转义 GLOB 元字符**，再在两侧加 `*`。SQLite 的 `GLOB` 没有 `ESCAPE` 子句
（那是 `LIKE` 才有的），反斜杠也不是转义符，只能把元字符包进字符类：
`[` → `[[]`、`*` → `[*]`、`?` → `[?]`，且 `[` 必须第一个替换（否则先转 `*` 得到的
`[*]` 里那个 `[` 会被二次转义成 `[[]*]`）。`]` 只在紧跟 `[` 时特殊，不用转。
不转义的实测后果（真库，召回 vs 字面子串并集）：`*` 3362 对 45（返回全库）、
`?` 3362 对 614、`**` 3362 对 4、`竞*` 534 对 0；`[` 方向相反，未闭合的字符类让整个
模式失效，0 对 1864，真实命中全丢。验收断言写成"召回集合严格等于字面子串并集"，
不是"不抛异常"——后者在转义之前就已经成立，测不出这个缺陷。

`LIKE` 兜底连同它的 `candidate_limit`（`max(limit*30, 120)`，无 `ORDER BY` 按 rowid
截断，缺陷 C 的成因）一起删除，所以兜底的固定分 `rank == 999.0` 不再出现在任何路径上。
去重靠 SQL 侧的 `chunk_id IN (... UNION ...)`：一个块被多个词、多个字段、两条路径同时
命中仍只返回一份，过滤器在最外层套一次，不可能漏在某条路径上。`relevance()` 的权重
一个都没动（`topics` 4.0 高于正文 1.0 的方向问题留给阶段 3）。

#### 阶段 3：评分与排序

用小 `limit` 查前几条，只看顺序，不看总数。

| 检查项 | 当前 | 通过标准 |
|-------|-----|---------|
| `竞价 --limit 8` 前 8 条正文真含率 | **8/8（已达标）** | 不得下降 |
| topics 字段权重 | 4.0（高于正文 1.0） | ≤ 正文权重 |
| 同一查询连续两次结果 | 未验证 | 逐条完全相等 |
| `--limit 8` 与 `--limit 40` 的前 8 条 | 未验证 | 完全相同（前缀稳定） |
| `--source tulip_garden` 过滤 | 正常 | 仍只返回该来源 |

第一行是**防退化项**，不是待修项：真库前 8 条已经全是正文真含（见缺陷 C 的更正）。
把 topics 权重降下去、把候选池放开，都有可能让精度掉下来，所以这一项必须每次复测。

**小 limit 的结果里不检查来源数量。** 这里只验证"排得对"和"排得稳"。

#### 来源覆盖的验收放在哪一层

| 层 | 验收什么 | 范围 | 用什么 limit | 不验收什么 |
|---|--------|-----|-----------|---------|
| 召回（阶段 2） | 命中块**全部**可被召回 | 全部正式来源（动态） | 大 limit（≥ 全库） | 顺序 |
| 排序（阶段 3） | 正文优先、结果稳定 | 回归样本（冻结数字） | 小 limit（8） | 来源数量 |
| 跨来源综合 | 按 SKILL **逐来源分别查询**再合并 | 全部正式来源（动态） | 每来源各自 | 单次查询的来源分布 |

**不设来源配额，测试也不得隐性要求配额。** 各来源强项不同（`龙头` 复利杯正文
381 条、郁金香仅 73 条、`panfeng` 仅 2 条），要求"默认前 8 至少两个来源"会诱导
实现去凑来源，把"该来源讲得少"歪曲成"必须挤进来一条"。

初版把这条写进了阶段 3 验收表（"候选池能看到的来源数 1 → 3"），且测试里
`test_two_character_query_must_span_sources` 直接断言前 8 条覆盖 ≥2 个来源——
与本节自相矛盾，已删除。**而且那条断言本身就是错的**：实测 `打板` 的 8 条结果
是 `fulibei 7 + panfeng 1`，已经"覆盖两个来源"，但南京路和郁金香一条都没有，
断言会误判为通过。

来源覆盖改由阶段 2 的召回等值断言保证：召回 = 三字段并集，等值意味着一条不漏，
比"至少两个来源"强得多，且不诱导配额。

#### 跨来源可达性验收（范围＝全部正式来源）

以下检查项**不写死来源清单**，从 `sources.yaml` 动态读取，接入新来源自动覆盖。
对应测试类 `RegistryScopedIndexTests` 与 `SourceRegistryTests`。

| 检查项 | 通过标准 | 当前状态 |
|-------|---------|---------|
| 登记表 ↔ 结构化目录 | `sources.yaml` id 集合 = `source_libraries/` 子目录名集合 | 通过 |
| 登记表 ↔ 索引 | id 集合 = 索引 `DISTINCT source_id` | 通过 |
| 三表非空 | 每个正式来源在 `documents`/`parents`/`chunks` 均 > 0 | 通过 |
| 单来源可查 | 每个正式来源 `--source <id>` 能返回其自有内容且不掺入他源 | 通过 |
| 作者可查 | **带作者数据的**来源，其主要作者 `--author <name>` 能召回 | 通过 |
| 三字词召回覆盖 | 每个**持有命中**的来源都出现在大 limit 结果里 | 通过（FTS 路径本已正确） |
| 两字词召回覆盖 | 同上 | **待阶段 2 修复**（当前走 LIKE 兜底，偏斜） |

**这几项都不许假设新来源的词汇和字段构成。** 三条具体约定：

**1. 单来源 smoke query 从数据里取，不用固定词表。**
初版试 `情绪/龙头/仓位/半路/复盘/打板` 六个词，谁先命中用谁。这六个词恰好覆盖今天
这四个来源，但对第五个来源什么都不保证——一个讲期权、债券或宏观的来源可能六个词全 0
命中，测试会把"没有缺陷"报成导入损坏。改为两条路径：

- `sources.yaml` 里给该来源写 `smoke_query:`（可选字段），显式指定
- 没写就从该来源自己最长的 40 个块里取文档频率最高的三字片段，再验证 ≥ 3 条命中

三字是因为 trigram 建 gram 的最小长度就是 3，短于此测的就不是 FTS 路径，
`SourceRegistryTests` 会拦下写了两字 `smoke_query` 的情况。按文档频率而非出现次数排序，
避免一个啰嗦的长块独断；同频按字典序，保证每次跑选中同一个词。
实测选出的分别是 `发言人`/`情绪周`/`上将潘`/`郁金香`，四个都走 FTS 路径。
拿一个六词全 0 命中的债券来源验证过：动态选出 `与凸性`，全部覆盖断言通过。

**2. 作者可查只覆盖真的有作者数据的来源。** 有署名是资料属性，不是检索层责任——
匿名帖子、无署名讲义、无 byline 的数据导出都是合法来源，要求它们必须有作者会在正确
数据上失败。所以范围是「chunks 里有非空 `author`，或 `sources.yaml` 声明了 `author:`」；
声明了却查不到是真的不一致，那种情况报 fail 而不是跳过。
同时兜一层空转保护：一个来源都没有作者数据时，测试报"这条路完全没被测到"。

**3. `author` 列不是来源标签。**
实测 `fulibei` 是合集，41 个不同作者值（最大的 `未从文件名明确识别` 618 块）；
`panfeng` 存的是聊天参与者（`我有上将潘凤` 175 块、`5280、我有上将潘凤` 5 块）。
所以测试是「取该来源块数最多的作者，再按该作者自己的文本取 smoke query」，
不能假设作者名等于 `display_name`。同名作者跨来源出现是合法的（跨来源综合正需要
这个），故断言是"返回的来源 ⊆ 持有该作者名的来源"，不是等于单一来源。

#### 阶段 4：导入成功性凭证

mtime 比对不够——它只能证明"数据库不早于 JSONL"，识别不了：导入器写文件前就崩溃、
用旧 JSONL 重建了新库、时钟或复制操作改了 mtime、部分输出更新部分是旧的。

初版只记录**输出** JSONL 的哈希和成功后的 `build_id`。这漏掉了最要命的一类失败：

> 原始文件改了 → 重跑导入器 → 写 JSONL 前崩溃 → 上一次成功的 summary 和旧 JSONL
> 都还在 → 重建索引 → 旧 `build_id` 被重新写进数据库 → 三方完全一致，校验通过。

结果是"用旧数据装成新数据"，而这正是要防的场景。根因：凭证只覆盖输出，
不覆盖**输入**，也没有区分"这一次跑成功了"和"上一次跑成功了"。

改为**输入 + 状态 + 输出**三段式凭证：

**1. 输入摘要（新增）**

导入器开始时，先扫描本次要读的原始文件，算出 `inputs` 摘要：

```json
"inputs": {
  "manifest_digest": "<所有输入文件 (相对路径, sha256, size) 排序后拼接再哈希>",
  "file_count": 110,
  "files": [{"path": "...", "sha256": "...", "size": 12345}]
}
```

`manifest_digest` 是单值指纹，`validate_kb.py` 只比这一个值就能判断"原始文件
和当次导入读到的是不是同一批"。`files` 明细用于定位是哪个文件变了。

**2. 运行状态（新增）**

导入器**开始时**就写一次 summary，`status = "in_progress"`，带 `attempt_id`（UUID）
和 `started_at`。成功结束时原子改写为 `status = "success"`，补上 `finished_at`
与 `outputs`。崩溃则文件停在 `in_progress`。

```json
"run": {"attempt_id": "...", "status": "in_progress|success", "started_at": "...", "finished_at": "..."}
```

`build_id` 定义为 `attempt_id`——同一次成功运行的唯一标识，不复用。

**3. 输出摘要**

`outputs`：各 JSONL 的 SHA256 + 行数（与初版相同）。

**4. 索引侧记录完整凭证摘要，不只是 build_id**

`build_index.py` 把每个来源的 `{build_id, status, manifest_digest, outputs 哈希}`
一并写进 `metadata`，键名 `source_credentials`。只存 `build_id` 不够：
`build_id` 相同但输入变了的情况识别不出来。

**5. `validate_kb.py` 的四项校验**

| 校验 | 挡住什么 |
|-----|--------|
| 每个来源 `run.status == "success"` | 导入器崩在中途，summary 停在 `in_progress` |
| 原始文件实算 `manifest_digest` == summary 记录值 | 原始文件改了但没重新导入成功 |
| JSONL 实算 SHA256 == `outputs` 记录值 | JSONL 被改动或部分更新 |
| 索引 `source_credentials` == 当前 summary 的对应字段 | 拿旧 JSONL 重建索引 |

第 1、2 项是新增的，正是初版漏掉的那条路径：崩溃后 `status` 停在 `in_progress`，
第 1 项直接失败；即使旧 summary 侥幸是 `success`，原始文件已变导致
`manifest_digest` 不匹配，第 2 项失败。两道闸任一道都能挡住。

**验收**（在临时 fixture 目录里构造，**不篡改真实 `source_libraries/`**）：

| 场景 | 构造方式 | 期望 |
|-----|--------|-----|
| 导入器中途崩溃 | summary 停在 `in_progress` | 校验 1 失败 |
| 原始文件改了但导入没成功 | 改 fixture 输入文件内容，summary 仍是旧的 success | 校验 2 失败 |
| JSONL 被改 | 改一行 JSONL | 校验 3 失败 |
| 用旧 JSONL 重建索引 | 索引记录旧凭证，summary 已更新 | 校验 4 失败 |
| 正常状态 | 不动任何东西 | 全部通过，不误报 |

五个场景各一个测试用例，跑在 `tmp_path` 级别的临时目录里。
`validate_kb.py` 的检查项从 11 增加到 15。

### 3.2 全轮总验收

跑完四个阶段，以下全部成立才算完成。**顺序不能颠倒**——先跑真库测试再重建索引，
测到的会是旧数据库。

第 1 步用 `-k` 只挑 fixture 用例，**不是**跑全套。初版这里写的是不带 `-k` 的
`discover`，那样第 1 步会连 `RealIndexTests` 一起跑，而这一步在重建之前——
测到的是即将被覆盖的旧索引，报出来的"通过"没有意义。

<b>Git Bash</b>

```bash
cd "C:/Users/20577/Documents/炒股/知识库"
export PYTHONIOENCODING=utf-8
PY="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe"
S="-s _知识库系统/scripts -t _知识库系统/scripts"
FIXTURE="-k FixtureShapeTests -k IndexPollutionTests -k ShortTermSearchTests \
         -k SourceCoverageTests -k RetrievalContractTests -k SubsetMarkerTests \
         -k SourceRegistryTests"
REAL="-k RealIndexRequirementTests -k RealIndexTests -k RegistryScopedIndexTests"

# 1. fixture 单元测试（纯内存，不碰 knowledge.db）
"$PY" -m unittest discover $S $FIXTURE -v
echo "退出码=$?"

# 2. 重建索引
"$PY" _知识库系统/scripts/build_index.py

# 3. 真库回归测试（此时数据库才是新的）。KB_REQUIRE_REAL_INDEX=1 让"索引不存在"
#    从静默 skip 变成硬失败，这是"零 skip"的执行机制，不是口头要求
KB_REQUIRE_REAL_INDEX=1 "$PY" -m unittest discover $S $REAL -v
echo "退出码=$?"

# 4. 全套一起跑一遍（含导入工具测试），确认没有互相干扰
KB_REQUIRE_REAL_INDEX=1 "$PY" -m unittest discover $S -v
echo "退出码=$?"   # 必须是 0

# 5. 结构与引用验证
"$PY" _知识库系统/scripts/validate_kb.py

# 6. 原始资料未改动
"$PY" _知识库系统/scripts/build_manifest.py
```

<b>PowerShell</b>

```powershell
Set-Location "C:\Users\20577\Documents\炒股\知识库"
$env:PYTHONIOENCODING = "utf-8"
$PY = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$S = @("-s","_知识库系统/scripts","-t","_知识库系统/scripts")
$FIXTURE = @("-k","FixtureShapeTests","-k","IndexPollutionTests","-k","ShortTermSearchTests",
             "-k","SourceCoverageTests","-k","RetrievalContractTests","-k","SubsetMarkerTests",
             "-k","SourceRegistryTests")
$REAL = @("-k","RealIndexRequirementTests","-k","RealIndexTests","-k","RegistryScopedIndexTests")

& $PY -m unittest discover @S @FIXTURE -v ; "退出码=$LASTEXITCODE"
& $PY _知识库系统/scripts/build_index.py
$env:KB_REQUIRE_REAL_INDEX = "1"
& $PY -m unittest discover @S @REAL -v ; "退出码=$LASTEXITCODE"
& $PY -m unittest discover @S -v ; "退出码=$LASTEXITCODE"
Remove-Item Env:KB_REQUIRE_REAL_INDEX
& $PY _知识库系统/scripts/validate_kb.py
& $PY _知识库系统/scripts/build_manifest.py
```

**`-k` 分组的清单不能漏。** 两份类名清单写在 `test_query_kb.py` 里
（`FIXTURE_TEST_CLASSES` / `REAL_INDEX_TEST_CLASSES`），由 `SubsetMarkerTests`
的四条测试守住：

| 守卫 | 挡住什么 |
|-----|--------|
| 两份清单穷举所有测试类 | 新类漏列 → 两个子集都不跑，而两条命令都显示 OK |
| 两份清单不相交 | 同一个类被跑两次，计数对不上 |
| **没有类名是另一个类名的子串** | `-k` 是子串匹配，见下 |
| fixture 类源码不出现 `DATABASE` | 第 1 步偷偷读旧库 |

第三条是实测踩出来的坑，不是假想：`RegistryScopedIndexTests` 最初命名为
`RegisteredSourceCoverageTests`，其中包含 `SourceCoverageTests`，于是
`-k SourceCoverageTests` 把这个真库类一起收进了 fixture 子集——第 1 步实取 55 项
而非预期的 48 项，多出的 7 项在重建索引之前就读了旧库。改名后加总与
`test_query_kb.py` 全量一致（当前 51 + 25 = 76）。
**所以给测试类命名时不能让一个名字包含另一个。**

**多个 `-k` 是 OR 关系**（已实测：`-k FixtureShapeTests -k IndexPollutionTests` → 13 项）。

**为什么不用 `-m unittest test_query_kb`**：从项目根目录直接这样跑会
`FAILED (errors=1)`（`sys.path` 里没有 `scripts` 目录，模块导入不到）。已实测。

| 项目 | 标准 |
|------|------|
| 单元测试 | 普通 PASS，无 expected failure / unexpected success / skip |
| 真库测试 skip 数 | 索引存在时**必须为 0**。索引缺失时 `KB_REQUIRE_REAL_INDEX=1` 让 `RealIndexRequirementTests` 硬失败、退出码 1（已实测：`failures=1, errors=0, skipped=17`——`RealIndexTests` 仍整类 skip，但退出码已经是 1，验收不可能被误判为通过） |
| `validate_kb.py` | 全部通过，退出码 0 |
| `PRAGMA integrity_check` | ok |
| 回归样本块数 | 1470 / 525 / 1181（不用全库绝对行数，见 0.1 节口径一） |
| 原始资料 | 样本 529 个文件 changed=0 removed=0；`panfeng` 原始 HTML sha256 不变 |
| 跨来源可达性 | 3.1 节末那张表全部通过，范围＝`sources.yaml` 全部正式来源 |
| 登记表一致性 | `sources.yaml` id 集合 = `source_libraries/` 目录名 = 索引 `DISTINCT source_id` |
| 固定评测 | 见 3.2.1，证据包落盘到 `_知识库系统/evals/` |
| Git | 每阶段一个提交；工作区在**本轮涉及的文件**上干净（见 4.3 节） |

**报告 `Ran N tests` 时必须注明 N 的组成。** 全套 `discover` 会把 `scripts/` 下所有
`test_*.py` 一起收，单看总数无法判断某轮改动交付了多少测试。当前组成：

```
Ran 108 tests = test_query_kb.py         76   ← 检索层（51 fixture + 25 真库）
              + test_kb_import_utils.py  27   ← 导入工具共用逻辑
              + test_import_feishu_chat.py 5  ← panfeng 的载体解析（飞书 HTML）
```

分文件计数用 `discover -p "test_query_kb.py"` 单跑取得，不要靠心算或相减；
子集计数用 3.2 节的 `$FIXTURE` / `$REAL` 单跑取得，且两者之和必须等于
`test_query_kb.py` 的全量数（当前 51 + 25 = 76），不等就是 `-k` 串台了。

#### 3.2.1 固定评测的证据要求

自动验收只能证明"关键词能返回行"，证明不了"检索修复真的服务于方法卡"。
因此 `benchmark_questions.md` 第 7-12 题（跨来源比较）必须留下人工判定记录。

**逐来源检索，不合并。** `cross-source-synthesizer` SKILL 第 10-11 行明确要求
"先声明哪些来源可用、哪些待接入"、"分来源检索后再合并"，并列出同义词扩展
（竞价/竟价、弱转强/转势/先弱后强、龙头/核心票/人气核心）。

每题记录：

| 字段 | 内容 |
|------|------|
| 查询词 | 每个来源实际用的词，含同义词扩展 |
| 命中 | chunk_id、parent_id、可回溯定位 |
| 是否答题 | 命中的内容真的回答了题意，还是只是含关键词 |
| 无证据来源 | 某来源确实没讲，是否诚实报告"未找到"而不是硬凑 |
| 证据包 | 这些材料合起来能否支撑一张待用户审批的方法卡草稿 |

**逐来源检索的范围是 `sources.yaml` 全部正式来源**，不是回归样本那三个。
`panfeng` 与另外三个同级，同样要单独检索、单独记录。

**不要求每题所有来源都出现。** 某来源没有相关内容时，如实记录"未找到"即通过；
硬凑反而违反项目规则。`panfeng` 只有 186 块且是口语聊天记录，术语密度显著低于
另外三个来源（实测 `弱转强` 0 条、`龙头` 2 条），"未找到"会比其他来源更常见，
这是内容事实，不作为缺陷记录。

结果保存为 `_知识库系统/evals/benchmark_run_<日期>.md`，
含修复前后对照。不只更新 `CLAUDE.md`。

### 3.3 完成的判定权在用户

跑通上述检查只说明"没坏"。是否"够用"由用户拿实际问题试，说了算。
我不用"应该可以"或"已优化"结题；每阶段汇报必须给出实测数字和遗留问题。

---

## 4. 风险与回滚

### 4.1 已知风险

1. **阶段 1 会改变全部现有查询的召回特性。** 移除 topics 后，某些现在能查到的东西会
   查不到。那些命中本来就是标签噪声，但已有使用习惯可能依赖它。
   缓解：重建前后各跑一遍 benchmark 第 1-12 题，逐题记录召回变化再决定是否保留。

2. **`query_kb.py` 是检索唯一入口**，`trading-knowledge-tutor` Skill 直接调用它。
   缓解：阶段 0 的测试先能跑，再动它。

3. **`build_index.py` 有 tokenizer 降级分支**（trigram 不可用时静默退到 unicode61），
   改 schema 时容易只改一条分支。
   缓解：测试断言 `metadata.fts_tokenizer` 的值。

4. **内存 SQLite 测试可能与真库行为不一致**（bm25 打分尺度、大表 LIMIT 行为）。
   缓解：关键断言在真库上用只读连接复核，不只靠单测。

5. **"改一个参数"类判断已经错过一次。** 2026-08-01 判断南京路只需调低阈值，实测该方案
   反而丢了约 1 万字正文。同类陷阱在本轮是"换 tokenizer 看起来只改一个词"，实际会重排
   全库召回。因此阶段 2 明确不动索引。

6. **绝对行数会随来源接入漂移。** `build_index.py:137` 无条件扫 `source_libraries/`，
   不看 `sources.yaml` 的 `status`，所以任何来源落盘即进索引。已实测漂移一次
   （194/676/3176 → 223/736/3362）。
   缓解：写死数字的断言按回归样本过滤（0.1 节口径一）；覆盖面断言从登记表动态读取
   （口径二）；`SourceRegistryTests` 在登记表与索引不一致时失败，把未审批的来源
   变成一条明确的红灯而不是静默的基线漂移。

7. **SPEC 曾被一条来源不明的并行任务带偏。** 上一轮索引里出现了未登记的第四来源，
   当时的处理是加过滤、改基线、在 SPEC 里写"第四来源正由并行工作接入中"——
   等于让 SPEC 去适配一条来路不明的改动。正确处理是停下来问来源出处再决定。
   缓解：来源身份问题不自行绕过；`SourceRegistryTests` 提供机器检测；
   新来源必须在 `sources.yaml` 记 `approved_on`。
   `panfeng` 已于 2026-08-02 获用户批准为正式同级来源，此项作为教训保留。

### 4.3 「工作区干净」的验收范围

原验收要求"工作区干净"，在有并行工作的情况下不成立。改为**限定范围**：

本轮涉及的文件必须无未提交修改：

```
SPEC.md
_知识库系统/scripts/query_kb.py
_知识库系统/scripts/build_index.py
_知识库系统/scripts/validate_kb.py
_知识库系统/scripts/test_query_kb.py
_知识库系统/evals/benchmark_run_*.md
CLAUDE.md
```

`panfeng` 接入产物（`source_libraries/panfeng/`、`sources.yaml`、
`import_feishu_chat.py`、`test_import_feishu_chat.py`、`manifest.jsonl`）属于
**来源接入**这条线，与检索层修复分两个提交，各自验收各自的文件。
`.Codex/` 留痕不在任何一条的验收范围内。

如果有并行工作也需要修改 `build_index.py`、`query_kb.py` 或 `validate_kb.py`，
**必须先协调再动手**——这三个文件本轮要改，同时改会冲突。

### 4.2 回滚点

| 标签 | 内容 | 备份 |
|------|------|------|
| `baseline-20260802` | 修复前快照 | `backups/baseline-20260802/`（194/667/8206） |
| `p0-fixed-20260802` | 数据层修完，本轮起点 | `backups/p0-fixed-20260802/`（194/676/3176） |
| （无标签） | `panfeng` 重命名前状态 | `backups/pre-rename-20260802/`（38 个结构化文件 + db + sources.yaml + 导入器 + sha256 清单） |

回滚命令见 `backups/baseline-20260802/恢复说明.md`。
索引可由 JSONL 重建，数据库损坏不是不可恢复故障。
`panfeng` 的结构化产物可由 `import_feishu_chat.py --force` 从原始 HTML 重新生成，
原始 HTML 另有一份在 `~/Downloads/`，sha256 与工作区副本一致。

---

## 5. 后续轮次（不在本轮范围）

| 优先级 | 事项 | 前置条件 |
|-------|------|---------|
| P0 | `build_index.py` 无条件扫 `source_libraries/`，不按 `sources.yaml` 的 `status` 过滤，未审批来源自动进索引 | — |
| P1 | `claim_type` 各来源均为固定值，元数据过滤失效 | 需定义标注规则 |
| P1 | 复利杯闲聊未标记，检索夹杂无关内容 | — |
| P2 | 方法卡仅 20 张且全来自复利杯，无 `status` 字段 | 需用户定义审批门槛 |
| 待定 | 自然语言问答 + 实时数据联动 | 需用户确认那份未定稿的需求文档 |


