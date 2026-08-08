#!/usr/bin/env python3
"""Query the local trading knowledge base with optional source and author filters."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from functools import lru_cache
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATABASE = ROOT / "_知识库系统" / "indexes" / "knowledge.db"

# trigram 分词器要三个字符才能建一个 gram，所以两字词的 MATCH 恒为 0（SPEC 缺陷 B）。
# 短于这个长度的词改走 GLOB 子串路径。trigram 在三字及以上词上表现良好，只需为短词
# 补一条召回路径（SPEC 2.3）。
MATCH_MIN_LENGTH = 3

# 召回字段：人写的三列。topics 由 infer_topics() 按关键词计数自动打，标签文本本身
# 从不出现在正文里，阶段 1 已把它移出 FTS 可匹配列，召回同样不带它（SPEC 2.2）。
# 顺序无所谓——三列在同一次扫描里 OR 起来。
RECALL_COLUMNS = ("text", "title", "author")

# 短词召回：**查 chunks 表、三列一次扫完**，不查 chunks_fts、不拆成三条 UNION。
#
# 这是 glob_fold_ascii_case() 里那句预告（"新增来源之前必须重新做大语料性能验收"）
# 到期后的实测结论。爱在冰川接入把语料从 3362 块推到 9175 块，短词路径本就是线性扫描，
# 耗时按比例涨上来了，于是两处开销同时暴露：
#
# 1. **查 chunks_fts 比查 chunks 贵。** FTS5 的内容表按段压缩存放，扫它要一路解压；
#    chunks 是普通 b-tree，直接顺序读。而短词模式（``*[Aa][Ii]*``、``*龙头*``）凑不出
#    可用的 gram，两边都是全表扫，走 FTS 白付解压成本却换不到索引。
# 2. **三条 UNION 子查询就是扫三遍。** text/title/author 各一条，每条独立扫全表，
#    而三列在同一行上，一次扫描全都读到了。
#
# 真库 9175 块实测（min of 7，2026-08-04）：
#
# ==========  ======================  ======================  ========
# 词          现状 IN(fts5 三列 UNION)  改法 IN(chunks 单扫 OR)   提速
# ==========  ======================  ======================  ========
# ``龙头``     159.3ms                 95.6ms                  1.7x
# ``竞价``     163.7ms                 119.3ms                 1.4x
# ``板块``     188.4ms                 119.3ms                 1.6x
# ``低吸``     138.3ms                 86.9ms                  1.6x
# ``AI``      175.7ms                 119.2ms                  1.5x
# ==========  ======================  ======================  ========
#
# 混合查询（长词 MATCH + 短词 GLOB）同样受益：``龙头 竞价 弱转强`` 294.0 → 180.1ms。
#
# **等价性是改法的前提，已逐词验收**：139 个词（两字常用词、同义词表全量、ASCII 大小写
# 四写法、希腊 ``ΑΑΑ``/``ααα``/``ΣΣΣ``、全角 ``ＡＡＡ``、GLOB 元字符 ``*``/``?``/``[``
# 及其组合、只在 title 或 author 出现的词、单字、标点、空串）逐词比对两种写法的
# chunk_id 集合，**不同的 0 个**。地基也验了：两表 9175 行、chunk_id 集合相同，
# ``text``/``title``/``author`` 三列逐行不一致 0 行。
#
# 为什么保留 ``IN (...)`` 外套而不直接 ``WHERE (text GLOB ? OR ...)``：后者在单短词时
# 更快一点（``龙头`` 72.1ms），但混合查询仍要 UNION 结构，而"过滤器只在最外层套一次"
# 是 build_filters() 那条注释在意的性质——拆开写就可能漏在某条路径上。这里只把三条
# 子查询合成一条、把表换掉，结构不动。
SHORT_TERM_RECALL_SQL = "SELECT chunk_id FROM chunks WHERE " + " OR ".join(
    f"{column} GLOB ?" for column in RECALL_COLUMNS
)

# 排序全部在 Python 侧由 ranking_key() 完成，SQL 不再产生任何分数，所以 rank 是个
# 纯占位列。
#
# 阶段 2 时这里叫 SUBSTRING_RANK，只给 GLOB 路径用，MATCH 路径那边选的是
# bm25(chunks_fts)。阶段 3 把两条路径统一到一把尺子（见 ranking_key()），bm25 不再
# 参与排序，也就不再出现在 SELECT 里，于是两条路径共用这一个占位值。
#
# 更早的实现在这里放的是 999.0，标记"这批结果来自 LIKE 兜底"。兜底连同它的
# candidate_limit 一起删掉了——那是缺陷 C 的成因（按 rowid 截断候选池，结果系统性
# 偏向插入顺序最靠前的来源），所以 999.0 这个值不再出现在任何路径上。一批验收断言
# 的写法就是 rank != 999.0，语义为"不是靠兜底给的"，所以这个列保留而不是删掉。
RANK_PLACEHOLDER = 0.0

# 短词走 GLOB，而 GLOB 是大小写敏感的——这是它与 MATCH、LIKE 唯一不一致的地方，
# 也是本轮修的回归：`AI` 召回 144 块、`ai` 83 块、`Ai` 3 块，而三列不区分大小写的
# 并集是 218 块，三个查询各自丢 74/135/215 条。同一个词换个大小写就换一批结果。
#
# 折叠范围严格限定在 **ASCII 字母**，用显式字符集而不是 `str.isalpha()`。因为验收
# 口径是"等于 text/title/author 的不区分大小写并集"，而那个并集由 SQLite 的
# `lower()`/`LIKE` 定义，两者都只折 ASCII：`lower('Ａ')` 还是全角 `Ａ`，希腊 `Α`
# 也不变。用 Python 的 `isalpha()` 会把全角字母和希腊字母一起折进去，召回比基准
# 多出一批，等值断言反而失败——那是"更强"而不是"一致"，本轮不做。
#
# panfeng 的原文有拿希腊字母、拼音替代证券名称的规避写法（见 CLAUDE.md），所以
# 非 ASCII 大小写不是纯理论问题，但它属于同义词归一，不是大小写折叠，两回事。
ASCII_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")

# ASCII-only 折叠用 str.translate 的映射表，只列 A-Z 一条方向（大写→小写）。
# 逐字符生成器（`"".join(c.lower() if c in ASCII_LETTERS else c for c in text)`）在
# 真库 3362 块 / 254 万字符上实测 173.0ms，translate 168.6ms——**只快 2.5%**，
# 两者都是 `str.lower()`（10.8ms）的 16 倍以上。所以换 translate 本身修不了性能，
# 真正的杠杆是"少折几遍"：见 FieldFolds 与 fold_kind_for()。
ASCII_FOLD_TABLE = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")

# ASCII 折叠表的**像集合**：``a-z``，即"存在别的写法（大写）折叠到它"的那些字符。
# 与 UNICODE_FOLD_IMAGES 是同一个概念的 ASCII 口径版本，一起给 needs_cased_fold() 用。
# 注意像里只有小写：大写 ``A`` 不是任何字符的折叠目标，所以 ``ΑΑ``（希腊）在 ASCII
# 口径下判定为"不用折"——GLOB 本来也不折它。
ASCII_FOLD_IMAGES = frozenset("abcdefghijklmnopqrstuvwxyz")


def glob_literal(term: str) -> str:
    """把用户输入转成只匹配自身的 GLOB 模式片段。

    SQLite 的 GLOB 没有 ESCAPE 子句（LIKE 才有），反斜杠也不是转义符，唯一的办法是
    把元字符包进字符类：``[*]`` 只匹配一个字面星号。三个元字符要转——``*``（任意长）、
    ``?``（任意一字）、``[``（字符类开始）。``]`` 不用转：在 GLOB 里它只有紧跟在
    ``[`` 后面才特殊，单独出现就是字面量。

    ``[`` 必须第一个替换。反过来的话，先转 ``*`` 得到的 ``[*]`` 里那个 ``[`` 会被第二遍
    再转一次，变成 ``[[]*]``，匹配的东西完全不对。

    不转义的后果实测（真库，2026-08-02）：查 ``*`` 返回全部 3362 块而字面命中只有 45 块，
    查 ``?`` 返回 3362 对 614，查 ``竞*`` 返回 534 而字面 0 块；查 ``[`` 更糟，未闭合的
    字符类让整个模式失效，返回 0 块而字面命中有 1864 块。前者是把无关内容当成命中，
    后者是把真实命中全丢掉，两个方向都错。
    """
    return term.replace("[", "[[]").replace("*", "[*]").replace("?", "[?]")


def glob_fold_ascii_case(pattern: str) -> str:
    """把已转义模式里的每个 ASCII 字母换成 ``[Aa]`` 形式的双字符类。

    在**转义之后**折叠是安全的，顺序不可颠倒。glob_literal() 只会引入
    ``[``、``]``、``*``、``?`` 四种字符，其中一个字母都没有，所以折叠这一遍不会碰到
    转义产物的内部；反过来先折叠，得到的 ``[Aa]`` 会被接下来的转义改成 ``[[]Aa]``，
    模式全错。

    ``]`` 不用特殊处理：折叠只在字母位置插入完整闭合的字符类，``a]`` → ``[Aa]]``
    在 GLOB 里读作"类 [Aa]，然后字面右括号"，仍是字面语义。

    为什么用字符类而不是枚举大小写变体去做多次 GLOB：真库实测（2026-08-02），
    查 ``AI`` 的三列并集，字符类一条模式 62.2ms，四个变体 × 三列共 12 条子查询
    147.5ms，慢 2.4 倍，且变体数随字母个数翻倍（``5GAI`` 就是 8 个变体）。
    两者结果集完全相同，也都等于不区分大小写并集。

    也没有换成 LIKE，理由是**复用现有的字面转义语义**，不是索引性能：``LIKE`` 的
    ASCII 折叠虽然天然（真库同一查询 36.8ms），但它自带 ``%``/``_`` 另一套通配符，
    得再引入一层 ESCAPE 转义分支（且 SQLite 的 ``ESCAPE`` 只接受单字符），
    glob_literal() 已经验收过的元字符字面行为要重新证一遍。保留 GLOB 就把阶段 2
    的转义结论原样留住了。

    **不要以为字符类模式走了 trigram 索引。** ``EXPLAIN QUERY PLAN`` 给出的
    ``INDEX 0:G3`` 只说明虚拟表接收了 GLOB 约束，不代表通过 trigram postings 定位。
    ``*[Aa][Ii]*`` 里没有任何连续三个字面字符，凑不出可用的 gram，本质是线性扫描。
    受控实测（固定命中 50 行、只增填充行，2026-08-02）：表从 1000 涨到 100000 行
    （100 倍），``*[Aa][Ii]*`` 0.545→68.1ms（125 倍）、``*AI*`` 0.196→21.2ms
    （108 倍），都随数据量成正比；而含三个连续字面字符的 ``*弱转强*``
    0.040→0.044ms、``MATCH '弱转强'`` 0.018→0.019ms（均 1.1 倍）才是真的走索引。
    真库同样印证：``AI`` 三列并集 67.8ms、``竞价`` 44.1ms，而 ``弱转强`` 0.33ms。

    所以短词路径的代价随语料线性上涨。当前 3362 块下几十毫秒可以接受，
    **新增来源（如"爱在冰川"）之前必须重新做大语料性能验收**，见 SPEC 阶段 2。
    """
    return "".join(
        f"[{character.upper()}{character.lower()}]" if character in ASCII_LETTERS else character
        for character in pattern
    )


def glob_pattern(term: str) -> str:
    """``*字面词*``：先转义元字符，再折叠 ASCII 大小写，最后才在两侧加通配符。

    两侧的 ``*`` 必须最后加，否则自己加的星号也会被转掉；折叠夹在中间，
    见 glob_fold_ascii_case() 里对顺序的说明。
    """
    return f"*{glob_fold_ascii_case(glob_literal(term))}*"


def dedup_key(term: str) -> tuple[str, str]:
    """去重键：**按检索等价关系**，不是按字面。

    两个词只要走同一条召回路径、且在那条路径的大小写规则下折叠到同一个串，检索行为就
    完全相同，必须算作一个词。``AI``/``ai``/``Ai``/``aI`` 都走 GLOB 短词路径、
    ASCII 折叠后同为 ``ai``，四者召回的块集合逐条相同（真库 218 块）。

    键里带上口径名，是为了不把两条路径的词混为一谈：假如某个两字词与某个三字词折叠后
    偶然相同，它们走的召回路径不同、命中集合也不同，不该合并。实际上长度不同的词折叠后
    也不可能相等（折叠不改变长度——全 Unicode 只有 U+0130 ``İ`` 的 ``lower()`` 是多字符，
    而它的小写形式长度 2，仍不与任何单字符词碰撞），所以这一维是防御性的。

    **为什么不按字面去重。** 旧实现用原始写法作键，于是 ``AI ai Ai aI 情绪`` 被当成
    5 个词：召回集合与 ``AI 情绪`` 完全相同（四者本就等价），但 matched_term_count
    对同时含 ``AI`` 和 ``情绪`` 的块给 5 而不是 2，对只含 ``AI`` 的块给 4 而不是 1——
    第 2 层排序键被重复计数撑大，结果顺序与 ``AI 情绪`` 不同。用户看到的是"同一个查询
    多打了几遍大小写变体，结果就换了一批顺序"。
    """
    kind = fold_kind_for(term)
    return (kind, fold(term, kind))


def terms_from_query(query: str) -> list[str]:
    """分词并按首次出现顺序去重，**去重按检索等价关系**（见 dedup_key()）。

    "竞价 情绪" 和 "竞价 竞价 情绪" 必须返回相同的词表，召回和排序都不能因重复而改变。
    大小写变体同理：``AI 情绪`` 与 ``AI ai Ai aI 情绪`` 的结果和顺序必须完全相同。

    **保留首次出现的原始写法**，不返回折叠后的串。折叠形式只用于去重判定和评分比较；
    召回侧要拿原词去构造 GLOB 模式和 MATCH 短语，而 ``glob_pattern()`` 自己会把 ASCII
    字母折成 ``[Aa]`` 字符类——那一层已经不区分大小写了，这里再折一遍只会让
    ``--json`` 输出和错误信息里出现用户没打过的写法。
    """
    parts = [part for part in re.split(r"[\s,，。；;、]+", query.strip()) if part]
    seen = set()
    unique = []
    for term in parts:
        key = dedup_key(term)
        if key not in seen:
            seen.add(key)
            unique.append(term)
    return unique or [query.strip()]


def split_terms_by_length(terms: list[str]) -> tuple[list[str], list[str]]:
    """把检索词分成"能进 MATCH 的"和"要走 GLOB 的"两组。

    每个词独立分流。旧实现是 ``[t for t in terms if len(t) >= 3]``，短词被整个丢掉：
    查 ``竞价 弱转强`` 与单查 ``弱转强`` 返回完全一样的结果，而用户看不出短词的
    贡献是零（SPEC 缺陷 B）。分流之后两组各自召回、结果取并集，长词在场不再让短词
    被静默吞掉。
    """
    long_terms = [term for term in terms if len(term) >= MATCH_MIN_LENGTH]
    short_terms = [term for term in terms if len(term) < MATCH_MIN_LENGTH]
    return long_terms, short_terms


def match_expression(terms: list[str]) -> str:
    """OR 连接的 FTS5 短语表达式。多词查询语义为 OR，任一词命中即入候选。"""
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def build_filters(source: str | None, author: str | None) -> tuple[str, list[object]]:
    """``--source`` / ``--author`` 的 SQL 片段，供每一条召回路径共用。

    只生成一次并在最外层套用，而不是每条路径各写一遍：漏在任何一条上，过滤器就会
    在那条路径上静默失效，而结果看起来仍然像是过滤过的。
    """
    clauses: list[str] = []
    params: list[object] = []
    if source:
        clauses.append("c.source_id = ?")
        params.append(source)
    if author:
        # 故意放宽到 title：作者名常常只写在标题里。
        clauses.append("(c.author LIKE ? OR c.title LIKE ?)")
        params.extend([f"%{author}%", f"%{author}%"])
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


# 字段权重（SPEC 2.2 评分层：``text`` ≥ ``title`` > ``topics``）。
#
# 阶段 2 之前的方向是反的，四个权重的排序是 **author 5.0 > topics 4.0 > title 3.0 >
# text 1.0**（取自 f05be13 的 relevance()）——正文垫底，最高的是 author。topics 是
# infer_topics() 按关键词计数自动打的，每块 5-6 个，标签文本本身从不出现在正文里，
# 却拿到 4.0、排在正文的 4 倍：等于让"机器猜的标签"压过"作者真写的字"。
# （topics 不是那批权重里最高的一个，author 才是；但它高于正文这一点才是缺陷所在。）
#
# 三个数的关系是契约要求的，具体取值不是：只要 text ≥ title > topics 成立，
# 相对顺序就不变。topics 取 0.0 而不是一个小正数，因为阶段 2 之后 topics 已经不参与
# 召回，纯标签块根本进不了候选集；留在这里的 topics 命中一定同时是三字段命中，
# 给它任何正权重都只是在已经入选的块之间加噪声。author 与 title 同级：都是人写的
# 短字段，SPEC 只对 text/title/topics 三者定序，author 归到 title 这一档。
FIELD_WEIGHTS = {"text": 3.0, "title": 2.0, "author": 2.0, "topics": 0.0}

# 每个字段单个词的命中次数上限，防止一个啰嗦的长块靠重复刷分盖过多词命中。
# 数值沿用阶段 2 的 8/3/2，只是权重换了——改上限会动到与排序无关的另一件事。
FIELD_CAPS = {"text": 8, "title": 3, "author": 2, "topics": 3}

# 块类型加成。人工整理的方法卡和冲突卡比原始转录更值得先看。阶段 2 就有这两条，
# 数值原样保留：它与字段权重方向无关，本阶段不借机改。
CHUNK_TYPE_BONUS = {"curated_method": 5.0, "conflict": 2.0}


# 两套折叠口径的名字。选哪套**由词长决定**（fold_kind_for()），不由内容决定——
# 因为它要与该词实际走的那条召回路径的大小写行为一致，而路径就是按词长分的。
ASCII_FOLD = "ascii"
UNICODE_FOLD = "unicode"


def fold_ascii(text: str) -> str:
    """只折叠 ASCII A-Z，与短词路径 GLOB 字符类 ``[Aa]`` 的口径一致。

    短词走三列 ``GLOB '*[Aa][Ii]*'``，而 **GLOB 对非 ASCII 不折叠大小写**：实测
    ``GLOB '*ΑΑΑ*'`` 只命中含大写 ``ΑΑΑ`` 的块，含 ``ααα`` 的块不在结果里；全角
    ``ＡＡＡ`` 与 ``ａａａ`` 同样互不命中；``SELECT lower('ΑΑΑ')='ααα'`` 也是 0。
    所以短词的评分侧只能折 ASCII——用 ``str.lower()`` 会给一个根本没被召回的 ``αα``
    块打上命中，那是排序用的幽灵命中。

    用 ``str.translate()`` 而不是逐字符生成器，但要说清楚：真库 3362 块 / 254 万字符
    实测（min of 7）逐字符 173.0ms、translate 168.6ms，**只快 2.5%**，两者都是
    ``str.lower()``（10.8ms）的 16 倍以上。换 translate 本身修不掉性能回归，真正的
    两个杠杆是 FieldFolds（每行每口径只折一次）和 FoldedTerm.needs_fold
    （折叠对该词是恒等变换时直接搜原文，一次折叠都不做）。
    """
    return text.translate(ASCII_FOLD_TABLE)


# FTS5 trigram 折叠域的码点区间，用于向 FTS5 本体问出它的折叠表（见 build_fold_table()）。
#
# **不是猜的，也不是抄 Unicode 数据库的。** 全部 1112063 个码点（跳过代理区和 U+0000）
# 逐个喂给 trigram 分词器实测，被改变的只有 1057 个，落在下面这 13 个区间里。区间按
# "相邻间隔 ≤ 64 就并合"压缩，覆盖 2176 个码点——比逐点列举好维护，又不像整个 BMP
# 那么浪费。实测覆盖率：用这 2176 个码点建出的表与全量扫描建出的表**逐条相同**。
#
# 为什么不用 "Python 认为有大小写的码点" 当候选集：那个集合确实也够（实测同样得到
# 1057 项），但枚举它要对 111 万个码点各算一次 lower()/upper()，实测 254ms——
# 而这张表要在 import 时建，254ms 比整次查询还慢。用固定区间是 2.8ms。
#
# 末尾两项是两个非字符码点：U+FFFE / U+FFFF 被 FTS5 折成 U+FFFD（替换字符），
# 而 Python 不认为它们有大小写。漏掉它们，含这两个码点的查询词就会与字段侧对不上。
# 星际面那一段（U+10400-U+10427，Deseret 大写）同样不能漏：只扫 BMP 会少 40 项。
FTS5_FOLD_RANGES = (
    (0x0041, 0x005A), (0x00B5, 0x024E), (0x0345, 0x0556), (0x10A0, 0x10CD),
    (0x1E00, 0x1FFC), (0x2126, 0x2183), (0x24B6, 0x24CF), (0x2C00, 0x2CF2),
    (0xA640, 0xA696), (0xA722, 0xA7AA), (0xFF21, 0xFF3A), (0xFFFE, 0xFFFF),
    (0x10400, 0x10427),
)


def build_fold_table() -> dict[int, str]:
    """向 FTS5 trigram 分词器本体问出它的折叠表，返回 str.translate() 用的映射。

    **做法**：把候选码点拼成一个长串喂进内存 trigram 表，再用 ``fts5vocab(..., 'instance')``
    把它切出的 gram 连同 offset 读回来。长度 n 的串产出 n-2 个 trigram，第 i 个恰是
    ``folded[i:i+3]``，所以折叠形式 = 第 0 个 term + 其后每个 term 的末字符。一次
    tokenize 就能读出整串的折叠形式（2176 个码点实测 2.8ms）。offset 序列必须恰好是
    ``0..n-3``、每个 term 恰好 3 字符，否则说明分词行为与假设不符，直接抛错而不是
    出一张错表——那种表会让排序静默错掉，比启动失败难查得多。

    **为什么必须问 FTS5，不能用 str.lower() 或 casefold()**（真 tokenizer 差分实测）：

    ============== ============================ ==================================
    规则           与 FTS5 的分歧               典型错法
    ============== ============================ ==================================
    ``lower()``    406 个码点 / 252 例整串 158  ``ΣΣΣ`` → ``σσς``（词尾 Σ 走
                                                Final_Sigma，折成 ``ς`` 不是 ``σ``）
    ``casefold()`` 505 个码点 / 252 例整串 90   ``ẞ`` → ``ss``（FTS5 折成 ``ß``，
                                                长度都变了）
    本表           **0**                        ——
    ============== ============================ ==================================

    ``lower()`` 那条不是边缘差异，是**查询词自己被折错**：查 ``ΣΣΣ`` 时 FTS5 召回了
    正文含 ``σσσ``/``ςςς`` 的块，而评分侧拿着折错的 ``σσς`` 去比对，两个正文块
    prose_hit 全 0，被压到"仅标题命中"的块后面——召回对了、排序反了。

    **这张表的性质**（全部实测，见 SPEC 阶段 3）：折叠恒为 1:1 单码点，所以能用
    ``str.maketrans`` 精确表达，不像 ``casefold()`` 会改变长度；1057 项、1035 个像；
    每个像都是不动点，因而 ``f(f(x)) == f(x)`` 幂等——查询词和字段两侧都要折，
    不幂等就会错。CJK 一个都不在像集合里，所以纯中文词仍能走 needs_fold 快路。

    在 import 时建一次而不是每次查询建：内存库 + 一次 tokenize 是 2.8ms，够便宜到
    不必缓存到磁盘，但放进查询路径就是每次查询白付 2.8ms。
    """
    codepoints = [
        codepoint
        for low, high in FTS5_FOLD_RANGES
        for codepoint in range(low, high + 1)
        if not (0xD800 <= codepoint <= 0xDFFF)
    ]
    probe = "".join(map(chr, codepoints))
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE p USING fts5(t, tokenize='trigram')")
        connection.execute("INSERT INTO p(t) VALUES (?)", (probe,))
        connection.execute("CREATE VIRTUAL TABLE v USING fts5vocab(p, 'instance')")
        rows = connection.execute("SELECT offset, term FROM v ORDER BY offset").fetchall()
    finally:
        connection.close()
    if [offset for offset, _ in rows] != list(range(len(probe) - 2)):
        raise RuntimeError("FTS5 trigram 的 offset 序列与预期不符，无法读出折叠表")
    if any(len(term) != 3 for _, term in rows):
        raise RuntimeError("FTS5 trigram 产出了非 3 字符的 token，折叠不是 1:1 单码点")
    folded = rows[0][1] + "".join(term[2] for _, term in rows[1:])
    return {ord(source): image
            for source, image in zip(probe, folded) if source != image}


# 折叠表在 import 时建一次（2.8ms），此后 fold_unicode() 只是一次 str.translate。
UNICODE_FOLD_TABLE = build_fold_table()

# 折叠表的**像集合**：某个字符是像，等价于"存在别的写法折叠到它"。
# 这是 needs_cased_fold() 的判据，比"Python 认为有大小写"准确——见那个函数的说明。
UNICODE_FOLD_IMAGES = frozenset(UNICODE_FOLD_TABLE.values())


def fold_unicode(text: str) -> str:
    """按 FTS5 trigram 的折叠表折叠，与长词路径 ``MATCH`` 的口径逐字符一致。

    三字及以上的词走 ``MATCH``，而 trigram 分词器按 Unicode 折叠，比 GLOB 宽：实测
    ``MATCH '"ΑΑΑ"'`` 同时命中 ``ΑΑΑ`` 和 ``ααα`` 两个块，全角 ``ＡＡＡ``/``ａａａ``
    也互相命中。召回既然把 ``ααα`` 块放进了候选，评分就必须承认那是**正文命中**，
    否则它会被判成"仅标题命中"、被压到真·仅标题块后面——同一次查询里召回宽而评分窄，
    结果比两边都窄更难解释。

    折叠表从 FTS5 本体问出来（见 build_fold_table()），不用 ``str.lower()``：后者对
    **词尾 Σ** 做上下文相关映射（Final_Sigma），``'ΣΣΣ'.lower()`` 是 ``'σσς'`` 而
    FTS5 折成 ``'σσσ'``，查询词自己就折错了。也不用 ``casefold()``：它把 ``ẞ``
    展开成 ``ss``，而 FTS5 折成 ``ß``，长度都不一样。

    代价：整库正文 254 万字符一次性折叠，``str.lower()`` 10.2ms、本实现 179.0ms。
    那是最坏上界而非实际开销——长词只折 ``MATCH`` 候选集（最坏的 ``ETF`` 是 30 行），
    且 needs_cased_fold() 为假的词一次折叠都不做。端到端 ``search()`` 实测在噪声内，
    数字见 SPEC 阶段 3 的性能表。
    """
    return text.translate(UNICODE_FOLD_TABLE)


def fold_kind_for(term: str) -> str:
    """这个词用哪套大小写规则——按词长，与 split_terms_by_length() 同一个阈值。

    分流依据必须是**词长**而不是"词里有没有 ASCII 字母"：口径要跟着召回路径走，
    而路径是按长度选的。一个两字的 ``ΑΑ`` 走 GLOB（不折希腊），一个三字的 ``ΑΑΑ``
    走 MATCH（折希腊），同样是希腊字母却是两种行为，这不是不一致，正是一致。
    """
    return UNICODE_FOLD if len(term) >= MATCH_MIN_LENGTH else ASCII_FOLD


def fold(text: str, kind: str) -> str:
    """按指定口径折叠一段文本。"""
    return fold_unicode(text) if kind == UNICODE_FOLD else fold_ascii(text)


def needs_cased_fold(folded: str, kind: str) -> bool:
    """折叠后的这个词，存不存在别的写法也折叠到它——决定字段侧要不要折叠。

    折叠字段的唯一目的是让"写法不同、折叠后相同"的两串对上。若没有任何别的写法折到
    这个串（``竞价``、``情绪``、``弱转强`` 全是汉字），字段折了也只会得到同一个串，
    整行折叠可以完全跳过。这是纯中文查询不付折叠开销的原因。

    **判据是"折叠后的词里有没有字符是某个别的写法的折叠目标"**，即该字符落在折叠表的
    **像集合**里。这个判据是精确的而不是近似：折叠恒为 1:1 单码点（实测），所以
    ``f(s) == folded`` 且 ``s != folded`` 时必有某位 ``i`` 满足 ``folded[i] = f(s[i])``
    是个像；反过来像集合里一个都没有，就不存在别的写法。

    **收折叠后的形式，不是原词。** 这里踩过一次：拿原词查像集合时 ``ΣΣΣ`` 会被判成
    "不用折"——大写 ``Σ`` 不是任何字符的折叠目标，可它折叠后的 ``σσσ`` 是。于是正文
    写作 ``ςςς`` 的块（折叠后同为 ``σσσ``）在未折叠的原文上搜不到，命中静默丢掉。
    参数名写成 ``folded`` 就是为了让调用点看得见这件事。

    **也不能用"词折叠前后是否相同"代替。** ``ai`` 折叠前后都是 ``ai``，但字段侧必须折，
    否则它搜不到写作 ``AI`` 的块。判据是"存不存在其它写法"，不是"这个写法要不要改"。

    两个口径各查各自的像集合：ASCII 口径的像是 ``a-z``（GLOB 只折 ASCII），Unicode
    口径的像是 FTS5 折叠表的 1035 个像。所以两字的 ``ΑΑ`` 不用折（GLOB 不折希腊）、
    三字的 ``ααα`` 要折，与各自的召回路径一致。
    """
    images = ASCII_FOLD_IMAGES if kind == ASCII_FOLD else UNICODE_FOLD_IMAGES
    return any(character in images for character in folded)


class FoldedTerm:
    """一个检索词 + 它该用的折叠口径，折叠结果预先算好。

    每个词的折叠只做一次，而不是每行每字段做一次。旧实现在
    ``min(haystack.count(ascii_lower(term)), cap)`` 里把 ``ascii_lower(term)`` 放进了
    最内层循环：4 个字段 × N 个词 × 每行一次，一次查询 1303 行就是几千次重复折同一个词。

    ``needs_fold`` 是本轮性能修复的第二个杠杆：**词里一个带大小写的字符都没有时，
    字段也不用折**。``竞价``、``情绪``、``弱转强`` 全是无大小写的汉字，此前每行仍被
    完整 translate 一遍；跳过之后纯中文查询的折叠开销归零。

    **判据是"折叠后的词里有没有字符是别的写法的折叠目标"，不是"词折叠前后是否相同"**
    ——后者是错的。``ai`` 折叠前后都是 ``ai``，但字段侧必须折：目标是让 ``ai`` 命中含
    ``AI`` 的块，而那要靠把字段的 ``AI`` 折成 ``ai``。按"折叠前后相同就跳过"来判，
    ``ai``、``Ai`` 这类已含小写的写法会在原文上搜，``AI硬件`` 反而搜不到，直接违反
    本轮要守的四写法等值。判据传的是 ``self.folded`` 而不是 ``term``，理由见
    needs_cased_fold()——传原词会让 ``ΣΣΣ`` 被判成不用折。
    """

    __slots__ = ("term", "kind", "folded", "needs_fold")

    def __init__(self, term: str) -> None:
        self.term = term
        self.kind = fold_kind_for(term)
        self.folded = fold(term, self.kind)
        self.needs_fold = needs_cased_fold(self.folded, self.kind)

    def haystack(self, raw: str, folds: "FieldFolds", field: str) -> str:
        """返回该词应当在其上做子串判断的文本。

        ``needs_fold`` 为假时直接返回原始字段值——省掉整行折叠。为真时向 FieldFolds
        取该行该口径的折叠结果（每行每口径只算一次）。
        """
        return folds.get(field, self.kind) if self.needs_fold else raw


class FieldFolds:
    """一行的字段折叠缓存：每个字段、每个口径最多折一次。

    旧实现对同一行同一字段反复折叠：prose_hit 折一次 text，matched_term_count 逐词在
    text/title/author 上折到命中即停，field_hit_score 再折 text/title/author 各一次
    （topics 权重 0.0 直接跳过，不折）。用计数器实测：单词查询每行 5 次，词只命中
    author 时折满 7 次，其中 text 列（真库平均 755 字）必折 3 次。3362 行 / 正文合计
    254 万字符下这就是性能回归的主体，修复前后的实测数字见 SPEC 阶段 3 的性能表。
    """

    __slots__ = ("row", "_cache")

    def __init__(self, row: sqlite3.Row) -> None:
        self.row = row
        self._cache: dict[tuple[str, str], str] = {}

    def raw(self, field: str) -> str:
        return self.row[field] or ""

    def get(self, field: str, kind: str) -> str:
        key = (field, kind)
        cached = self._cache.get(key)
        if cached is None:
            cached = fold(self.raw(field), kind)
            self._cache[key] = cached
        return cached


@lru_cache(maxsize=256)
def folded_terms(terms: tuple[str, ...]) -> tuple[FoldedTerm, ...]:
    """把词表转成 FoldedTerm 元组，按词表缓存。

    ranking_key() 对每一行都要拿一次同样的词表，而词表在一次查询里是固定的。
    缓存键是词表元组本身，所以 ``rank_and_truncate(rows, ["竞价"], 500)``
    这种测试里的直接调用同样受益，不需要调用方改成先构造再传。
    """
    return tuple(FoldedTerm(term) for term in terms)


def field_hit_score(folds: FieldFolds, terms: tuple[FoldedTerm, ...]) -> float:
    """字段加权命中分 + 块类型加成。``text`` ≥ ``title`` > ``topics``，见 FIELD_WEIGHTS。

    这一项单独拿出来，是因为它只回答"命中得多不多、命中在哪个字段"，
    而"命中了几个词"由 matched_term_count() 单独计量并且优先级更高——
    两件事混在一个浮点数里，多词命中就会被单字段的高频命中盖过去。

    **块类型加成（CHUNK_TYPE_BONUS）在这一层计入：** curated_method +5.0、
    conflict +2.0。人工整理的方法卡和冲突卡比原始转录更值得先看，这是第三层的
    实际排序因素，不只是"字段命中"。

    收 FieldFolds 而不是 sqlite3.Row：折叠结果按行缓存，三个评分函数共用同一份，
    同一字段同一口径在一行上只折一次（见 FieldFolds 的文档）。
    """
    score = 0.0
    for field, weight in FIELD_WEIGHTS.items():
        if not weight:
            continue
        cap = FIELD_CAPS[field]
        raw = folds.raw(field)
        for term in terms:
            haystack = term.haystack(raw, folds, field)
            score += min(haystack.count(term.folded), cap) * weight
    return score + CHUNK_TYPE_BONUS.get(folds.row["chunk_type"], 0.0)


def prose_hit(folds: FieldFolds, terms: tuple[FoldedTerm, ...]) -> int:
    """正文里是否真含任一检索词。1 = 含，0 = 只在标题/作者里含。

    SPEC 2.2 要求"正文命中排在纯标签命中之前"。阶段 2 把纯 topics 块从召回里删掉之后，
    候选集里已经没有纯标签块了，剩下的对立面变成了**仅标题或仅作者含词**的块——
    实测 fixture 前 8 条正是 5 条 author-only（旧权重 9.0 分）+ 3 条 title-only（7.0），
    而正文块只有 6.0。所以这一层的实际职责是把"正文真讲这个词"与"只是标题里带了这个
    词"分开，前者优先。

    做成独立的布尔层而不是靠 text 权重压过 title：权重是可以被数量抵消的——
    一个标题里重复三次该词的块，靠 title 命中就能盖过只提一次的正文块。而"正文有没有
    讲"是个定性区别，不该被计数抵消。

    **折叠口径按词长分流，与该词实际走的召回路径一致**（见 fold_kind_for()）：
    两字的 ``ΑΑ`` 走 GLOB，GLOB 不折希腊字母，所以只含 ``αα`` 的块不算命中；
    三字的 ``ΑΑΑ`` 走 MATCH，MATCH 按 Unicode 折叠、确实召回了只含 ``ααα`` 的块，
    所以那个块必须判为正文命中——否则它会被当成"仅标题命中"压到真·仅标题块之后。
    """
    raw = folds.raw("text")
    return 1 if any(term.folded in term.haystack(raw, folds, "text") for term in terms) else 0


def matched_term_count(folds: FieldFolds, terms: tuple[FoldedTerm, ...]) -> int:
    """三字段里命中了几个**不同**的检索词（SPEC 2.2：多词命中数越多排越前）。

    按去重后的词计数，不是按出现次数：查 ``竞价 情绪`` 时，两个词各提一次的块要排在
    只提 ``竞价`` 十次的块前面——后者对第二个词一无所知，对多词查询来说是更差的答案。
    单词查询时这一项恒为 1，对排序没有影响。

    口径是 text/title/author 三列，与召回字段一致（RECALL_COLUMNS），不含 topics：
    自动标签命中不算"这个块讲了这个词"。

    折叠口径按词长分流（见 fold_kind_for()）：短词按 ASCII、长词按 Unicode，
    各自与它实际走的召回路径一致。**词表本身也按检索等价关系去重过**
    （terms_from_query()），所以 ``AI ai Ai aI`` 只会在这里贡献 1，不是 4。
    """
    count = 0
    for term in terms:
        for column in RECALL_COLUMNS:
            raw = folds.raw(column)
            if term.folded in term.haystack(raw, folds, column):
                count += 1
                break
    return count


def ranking_key(row: sqlite3.Row, terms: list[str]) -> tuple:
    """SPEC 2.2 排序契约，两条路径共用的唯一标尺。

    五层，逐层决胜，前一层相等才看下一层：

    1. **正文命中优先**（prose_hit）——正文真含词的块排在仅标题/仅作者含词的块之前。
    2. **多词命中数**（matched_term_count）——命中的不同检索词越多越前。
    3. **字段加权命中分**（field_hit_score）——``text`` ≥ ``title`` > ``topics``。
    4. **日期降序**——前三层相等时，更新的内容在前；空日期排末。实现：把 "YYYY-MM-DD"
       去掉连字符转整数再取负（如 "2024-08-04" → 20240804 → -20240804），空日期/None
       用整数 0，排序后 0 > 任何负整数，故空日期自然落末。
    5. **chunk_id 升序**——最终决胜，消除仍然并列时的顺序不确定性。

    分层而不是加权求和，因为这三个性质不可互换：一个标题里刷了三次该词的块不该因为
    分数够高就越过正文块，一个高频单词块也不该越过双词命中块。加权求和总能被"多刷
    几次命中"抵消掉，分层不会。

    加第 4 层的原因：``FIELD_CAPS`` 封顶导致高频词大量块并列（实测"赚钱效应"有 1759
    块前三层同分），此时第 5 层 chunk_id 字典序让 ``azbc-`` 前缀的爱在冰川系统性靠前。
    加入日期降序后，并列块里时间更近的内容优先，行为更符合用户预期；爱在冰川不再
    因字母序占优，而是靠内容日期竞争。

    **bm25 不再参与排序。** 见阶段 3 注释（原文保留在 git 历史）。

    第 5 层是确定性的来源。前四层都可能出现真正的并列（同一天的多个块），
    chunk_id 不变，sorted() 稳定排序保证结果确定。

    返回的元组前三项和第四项取负号：sorted() 升序排列，取负让"分高/日期新的在前"，
    第 5 层 chunk_id 保持正序（字典序升序）。

    ``terms`` 收 ``list[str]``（调用方和测试都这么传），内部转成 FoldedTerm 元组并
    按词表缓存；一行的字段折叠由 FieldFolds 在三个评分层之间共享，见各自的文档。
    """
    prepared = folded_terms(tuple(terms))
    folds = FieldFolds(row)
    # 日期层：把 "YYYY-MM-DD" 去掉连字符转整数再取负；非日期字符串/"未知"/空值用 0（排末）
    raw_date = row["date"] or ""
    try:
        date_int = int(raw_date.replace("-", "")) if raw_date else 0
    except ValueError:
        date_int = 0
    return (
        -prose_hit(folds, prepared),
        -matched_term_count(folds, prepared),
        -field_hit_score(folds, prepared),
        -date_int,        # 日期降序：大整数取负后更小，故排前；0（空/非日期）最大，排末
        row["chunk_id"],
    )


def rank_and_truncate(rows: list[sqlite3.Row], terms: list[str], limit: int) -> list[sqlite3.Row]:
    """先按 ranking_key() 排全量候选，再截断到 limit。

    两条召回路径都走这一个函数，这是"两条路径同一把尺子"的落点。顺序不能颠倒：
    先截断再排序等于按召回顺序挑前几条，那正是缺陷 C 的形态（按 rowid 截断，
    结果偏向插入顺序最靠前的来源）。

    ``limit`` 只影响返回条数，不影响任何一条的位次——所以 ``--limit 8`` 的结果
    必然是 ``--limit 40`` 结果的前 8 条（SPEC 阶段 3「前缀稳定」验收项）。
    """
    return sorted(rows, key=lambda row: ranking_key(row, terms))[:limit]


def search(connection: sqlite3.Connection, query: str, source: str | None, author: str | None, limit: int):
    """SPEC 2.2 的召回层（阶段 2）+ 评分与确定性排序层（阶段 3）。

    召回仍是两条路径，取决于有没有短词：

    * 全部检索词都 ≥3 字 → 一次 ``MATCH``。
    * 出现短词 → 候选集是 ``MATCH`` 命中与三列 ``GLOB`` 命中的并集（模式先经
      glob_pattern() 处理：用户输入的 ``*``/``?``/``[`` 只匹配自身，ASCII 字母
      不分大小写）。

    **排序只有一条路径。** 阶段 2 是两条路径两把尺子（``ORDER BY bm25`` vs
    relevance()），阶段 3 统一到 ranking_key()：两条路径都先取全量候选，再用同一个
    键在 Python 侧排序，然后截断。所以"查 ``弱转强``"和"查 ``竞价 弱转强``"里
    ``弱转强`` 那部分命中的相对顺序不一定一致——因为评分层看的是整个查询的词表，
    单词查询时第 2 层恒为 1（所有块都只命中这一个词），双词查询时它才开始分化。

    两条路径的大小写行为也一致：``MATCH`` 本来就不区分（``AI硬件`` 与 ``ai硬件`` 实测
    同为 8 块），短词路径靠 glob_pattern() 里的字符类折叠对齐到同一口径。

    两条路径都先拿到完整候选再在 Python 侧切片。SQL 里不留任何 rowid 预截断：
    旧实现的 ``LIMIT max(limit*30, 120)`` 没有 ORDER BY，按插入顺序截断，
    郁金香的命中一条都进不来（SPEC 缺陷 C）。全库 3176 块，全量候选代价可接受。
    """
    terms = [term for term in terms_from_query(query) if term]
    if not terms or limit <= 0:
        # 空查询、纯标点查询走到这里：返回空列表，不抛异常，也不当成一次真的检索
        # （LIKE '%%' 会匹配全表，那才是最坏的结果——看起来像检索到了东西）。
        return []

    filter_sql, filter_params = build_filters(source, author)
    long_terms, short_terms = split_terms_by_length(terms)

    if not short_terms:
        # SELECT 里不再取 bm25(chunks_fts)，ORDER BY 也去掉了：阶段 3 起两条路径共用
        # ranking_key()，bm25 不参与排序（理由见 ranking_key() 的文档）。SQL 侧留一个
        # 确定的 rowid 顺序，好让下面的稳定排序有个确定的输入——但决胜靠 chunk_id，
        # 不靠这个顺序。
        sql = f"""
            SELECT c.*, {RANK_PLACEHOLDER} AS rank
            FROM chunks_fts
            JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
            WHERE chunks_fts MATCH ? {filter_sql}
            ORDER BY c.rowid
        """
        rows = connection.execute(sql, [match_expression(long_terms), *filter_params]).fetchall()
        return rank_and_truncate(rows, terms, limit)

    # 子查询逐条只查 chunks_fts，不 JOIN chunks；UNION 去重后由外层一次套用过滤器。
    # 这样过滤器不可能漏在某条路径上，chunk_id 也天然唯一——一个块被多个词、多个
    # 字段、两条路径同时命中，仍然只返回一份（SPEC 2.2 去重层）。
    #
    # 写成 IN + UNION 而不是"先在 Python 里取 id 集合再 IN (?,?,...)"：真库里
    # 『情绪』的并集是 1303 条，绑定变量个数会撞上 SQLite 的上限。
    clauses: list[str] = []
    params: list[object] = []
    if long_terms:
        clauses.append("SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ?")
        params.append(match_expression(long_terms))
    for term in short_terms:
        # GLOB 是精确子串匹配。模式经 glob_pattern() 处理：元字符转成字面量，
        # ASCII 字母折成 [Aa] 字符类抹掉 GLOB 自带的大小写敏感——否则 AI/ai/Ai
        # 会各返回一批不同的结果。
        #
        # 这条路径本质是**线性扫描**：两字模式凑不出三个连续字面字符，没有可用的
        # trigram，耗时随语料成正比（受控实测 100 倍数据 → 125 倍耗时，
        # 见 glob_fold_ascii_case() 的说明）。所以每个短词只扫一遍、且扫便宜的那张表，
        # 三列在同一次扫描里 OR——理由与实测数字见 SHORT_TERM_RECALL_SQL。
        clauses.append(SHORT_TERM_RECALL_SQL)
        pattern = glob_pattern(term)
        params.extend([pattern] * len(RECALL_COLUMNS))

    sql = f"""
        SELECT c.*, {RANK_PLACEHOLDER} AS rank
        FROM chunks c
        WHERE c.chunk_id IN ({' UNION '.join(clauses)}) {filter_sql}
        ORDER BY c.rowid
    """
    candidates = connection.execute(sql, [*params, *filter_params]).fetchall()

    # 这里**没有** Python 侧的二次过滤，是有意的。
    #
    # 阶段 3 的第一版在此处加过一段：先用一条额外的 MATCH 查询取出长词命中的 ID，再要求
    # 其余候选至少有一个短词能用 ascii_lower() 匹配上。理由写的是"GLOB 对非 ASCII 不区分
    # 大小写，会把希腊 Α/α 当成一回事，得滤掉这类幽灵命中"。
    #
    # 那个前提是错的，实测（真库 + 内存库，2026-08-02/03）：
    #
    # | 探针 | GLOB | MATCH |
    # |-----|------|-------|
    # | 大写希腊 `ΑΑΑ` 找只含 `ααα` 的块 | 不命中 | 命中 |
    # | 全角 `ＡＡＡ` 找只含 `ａａａ` 的块 | 不命中 | 命中 |
    # | `SELECT lower('ΑΑΑ')='ααα'` | 0 | — |
    #
    # GLOB 只对 ASCII A-Z 折叠（且那是 glob_pattern() 自己插的 `[Aa]` 字符类给的，
    # 不是 GLOB 的行为），非 ASCII 一律精确匹配。所以短词路径根本产生不了希腊幽灵命中，
    # 那段过滤在真实数据上是恒真的死代码——它每次混合查询还多跑一条全表 MATCH。
    #
    # 反过来，宽的那条是 MATCH：它确实会召回只含 `ααα` 的块。那不是幽灵命中而是真召回，
    # 评分侧要承认它（fold_kind_for() 给长词 Unicode 口径），不是滤掉它。
    return rank_and_truncate(candidates, terms, limit)


SYNONYMS_CONFIG = ROOT / "_知识库系统" / "config" / "synonyms.yaml"


def expand_query(query: str) -> tuple[str, list[str]]:
    """用 config/synonyms.yaml 把查询词扩成同义词组。返回 (新查询, 新增的词)。

    为什么做在查询字符串这一层、而不是改 search()：检索层的召回与排序合同
    已被 139 项测试钉死，动它要重测全部基线。扩展只是「把一个词换成多个词」，
    多词查询本来就是 OR 语义（SPEC 2.2），所以改写查询串即可，检索层零改动。

    只加词不替换 —— `转势` 在复利杯零命中，拿它顶替 `弱转强` 会丢掉该来源全部内容。
    yaml 读不到时静默退化为不扩展：同义词是增强，不是硬依赖。
    """
    try:
        import yaml
        config = yaml.safe_load(SYNONYMS_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return query, []

    terms = terms_from_query(query)
    if not terms:
        return query, []

    added: list[str] = []
    for group in (config.get("groups") or {}).values():
        pool = [group.get("canonical", "")] + list(group.get("variants") or [])
        pool = [p for p in pool if p]
        # 查询词命中该组任一写法，就把该组其余写法都加进来
        if any(t in pool for t in terms):
            for word in pool:
                if word not in terms and word not in added:
                    added.append(word)

    if not added:
        return query, []
    return query + " " + " ".join(added), added


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    parser.add_argument("--source")
    parser.add_argument("--author")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--show-parent", action="store_true")
    parser.add_argument("--expand", action="store_true",
                        help="用 config/synonyms.yaml 扩展同义词后再检索")
    args = parser.parse_args()

    if not DATABASE.exists():
        raise FileNotFoundError(f"Index not found. Run build_index.py first: {DATABASE}")

    query = args.query
    if args.expand:
        query, added = expand_query(query)
        if added and not args.json:
            print(f"同义词扩展：+ {'、'.join(added)}\n")

    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    rows = search(connection, query, args.source, args.author, args.limit)
    results = []
    for row in rows:
        item = dict(row)
        if args.show_parent and item.get("parent_id"):
            parent = connection.execute("SELECT text, locator FROM parents WHERE parent_id=?", (item["parent_id"],)).fetchone()
            if parent:
                item["parent_text"] = parent["text"]
                item["parent_locator"] = parent["locator"]
        results.append(item)
    connection.close()

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    if not results:
        print("未找到匹配结果。")
        return 1
    for index, item in enumerate(results, start=1):
        preview = item["text"].replace("\n", " ")[:420]
        citation = f"[{item.get('source_name') or item['source_id']}｜{item.get('author') or '未标注'}｜{item['title']}｜{item.get('date') or '日期未标注'}｜{item['locator']}]"
        print(f"\n#{index} {citation}")
        print(f"类型: {item['chunk_type']} | 主题: {item.get('topics') or '未标注'} | ID: {item['chunk_id']}")
        print(preview)
        if args.show_parent and item.get("parent_text"):
            print("\n父块上下文：")
            print(item["parent_text"][:1800])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
