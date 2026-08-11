#!/usr/bin/env python3
"""主升龙头空空龙导入器 —— 单作者方法论长文（docx/txt），无 PDF、无留言区。

与既有六个来源的形态差异（每条都对应一个实测事实，不是猜的）：

1. **同源冗余判不出来**：两组 docx/txt 同源对的**文件 sha256 不同、抽取后归一化
   文本 sha256 相同**。用 import_generic_source.py 那样的文件 hash 判重会让同一句话
   入库两次。所以判重放在抽取之后。

2. **版本迭代不是重复**：三篇「防爆头」按语义近似（阈值 0.75）实测，
   `防爆头2.0` 有 90% 段落能在 `20260418` 版找到近似对应、独有仅 11%，是同一篇的
   合规改写版（「暴利」→「正反馈」）；而 `20260418` 与 `新日期拆解` 各有 39%/43%
   独有内容，互相替代不了。所以前者标 superseded_by 不建块，后两者都建块。

3. **原始段落太碎**：中位 57 字、75.9% 短于 100 字。不合并会重演郁金香切块过碎
   （修复前中位 19 字）。实测 merge_short_units(500, 1000) 产出中位 564 字、零短块。

4. **超长段的 locator 会撞车**：`split_text` 把 2067 字单段切成 3 块后，三块 locator
   全叫「正文第11段」。既有来源都有这个缺陷（南京路 214 块、郁金香 158 块、
   波段之门 71 块同文档内 locator 重复），引用格式用的是 locator 而非 chunk_id，
   所以引用会指向同一位置。这里给切片加「之N」后缀，不复制该缺陷。

5. **不顶层 import pypdf**：这个来源没有 PDF。import_boduanzhimen.py 顶层
   `from pypdf import PdfReader` 让它在 Python312 上直接 ImportError —— 本机两个
   解释器只有 codex 运行时有 pypdf，而建索引/查询用的是 Python312。

图片按用户决定**只导出记路径、不 OCR**：实测两张行情截图 OCR 出的股票名可读
（中际旭创 300308）但数字全是垃圾（`146 . 98 277 , 76`），CJK 率 0.32 低于
chart_ocr_is_useful 的 0.45 门槛，本来就会被拒。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml
from docx import Document

from kb_import_utils import (
    ROOT,
    clean_text,
    infer_topics,
    merge_short_units,
    natural_key,
    split_text,
    write_jsonl,
    write_text_lf,
)

CONFIG = ROOT / "_知识库系统" / "config" / "sources.yaml"
SOURCE_ID = "kongkonglong"
AUTHOR = "主升龙头空空龙"
UPSTREAM = r"C:\Users\20577\Documents\炒股\主升龙头空空龙"

# 建块的最小中文量。低于这个的文档整篇跳过并记录原因（不静默丢）。
MIN_DOC_CJK = 100

# ---------------------------------------------------------------------------
# 噪声模式
# ---------------------------------------------------------------------------

# 推广联系方式。实测 16 处：`一手拼课vx：hzp9512055818`(4) / `一手拼课QQ865154818`(12)。
# 行内剔除而非删整段——部分嵌在正文句子中间，删整段会连正文一起丢。
# 一条正则搞定所有变体，**不要写成多条**：原先「一手拼课vx:…」和「拼课vx:…」
# 分成两条，第一条删掉后第二条又匹配到残留，同一处被计数两次 ——
# 报出来的「16 处推广」实际只有 8 处，统计虚高一倍。
PROMO_PATTERNS = (
    re.compile(
        r"[一壹]?手?拼课\s*(?:vx|VX|Vx|vX|微信|weixin|QQ|qq|Qq|qQ)\s*[：:]?\s*[A-Za-z0-9_]+"
    ),
)

# 平台与载体标记。淘股吧 2 处（1 全角 + 1 半角）、`【汇阳财策】…一剑封喉…`2 处。
# 后者是发布平台的栏目标题，不是他的观点，但同一段里紧跟着正文，只能行内剔。
PLATFORM_PATTERNS = (
    # 全角和半角都要匹配。原文两处写法不同：`6月21汇阳财策` 用全角【淘股吧】、
    # `竞价抢筹…全篇` 用半角 [淘股吧]。只写全角会让半角那处残留进索引（实测残留 1 块）。
    # ⚠️ clean_text 的 NFKC 归一化救不了这个：它把全角括号 ［］ 转成半角 []，
    # 但不会把半角 [] 转成中文方头括号 【】，两者在 Unicode 里是不同字符。
    re.compile(r"[【\[]淘股吧[】\]]"),
    # 栏目标题整条剔除。**不要用懒惰量词 + 分支**：原先写成
    # `【汇阳财策】[^。\n]{0,80}?(?:独家盘前盘中盘后思路[！!]?|一剑封喉[！!]?)`，
    # 懒惰匹配在遇到「一剑封喉！」就收工，把后半截
    # 「盘中实时分享核心人气股（6.19~6.26号）独家盘前盘中盘后思路！」留在了正文里
    # （实测 3 个块残留）。改成贪婪匹配到最后一个锚点。
    re.compile(r"【汇阳财策】[^。\n]{0,120}独家盘前盘中盘后思路[！!]?"),
    # 上一条要求以「独家…思路」收尾。若原文没有那个尾巴，退回只删品牌头 + 口号。
    re.compile(r"【汇阳财策】\s*一剑封喉[！!]?"),
    # 品牌名被剥掉后可能剩下裸的栏目标题（.doc 转换件里就是这样：
    # 「6月20日补充策略」换行后紧跟「盘中实时分享核心人气股(6.19~6.26号)…」）
    re.compile(r"盘中实时分享核心人气股\s*[（(][^）)]*[）)]\s*独家盘前盘中盘后思路[！!]?"),
    re.compile(r"\s*-\s*补充策略\s*"),
    re.compile(r"时间[：:]\s*今天\s*\d{1,2}[：:]\d{2}"),
    re.compile(r"补充策略\s*今天\s*\d{1,2}[：:]\d{2}"),
    # 「补充策略」单独成行 + 紧跟发布时间戳（竞价抢筹那篇：
    # 「时间:06-14 13:17 / 补充策略 / 06-14 13:17」三行连排）
    re.compile(r"时间[：:]\s*\d{2}-\d{2}\s+\d{1,2}[：:]\d{2}"),
    re.compile(r"^补充策略$", re.MULTILINE),
    re.compile(r"^\s*\d{2}-\d{2}\s+\d{1,2}[：:]\d{2}\s*$", re.MULTILINE),
)

# 免责声明。4 处，其中 1 处整段即声明。保留「不构成投资建议」的语义价值不大，
# 但它是作者的合规习惯，删掉不影响任何检索。
# 免责声明。
# ⚠️ 最后一条曾写成 `[^。\n]{0,30}不构成投资建议[，。！]?` —— 那是个**会吃正文**的
# 正则：`[^。\n]{0,30}` 会把「不构成投资建议」前面 30 字（可能是正文句子的后半截）
# 一起删掉。实测它删掉了「声明:文章仅代表网友个人观点,赠送的内容仅供参考与学习交流,
# 不构成投资建议」这种已被第一条覆盖的内容，属于重复匹配；但换一篇文章就可能误伤正文。
# 改为只从句子边界起匹配整句。
DISCLAIMER_PATTERNS = (
    re.compile(r"(?:免责)?声明[：:][^\n]{0,120}"),
    # 「当然以上…不构成投资建议」整句。要求从「以上」这个明确的声明起始词开始，
    # 不用无锚点的通配前缀。
    re.compile(r"(?:当然[，,]?)?以上[^。\n]{0,40}?不构成投资建议[。，！!]?"),
    # 独立成句的「不构成投资建议」（前面是句末标点或行首）
    re.compile(r"(?<=[。！？\n])[^。！？\n]{0,20}不构成投资建议[。，！!]?"),
    re.compile(r"^[^。！？\n]{0,20}不构成投资建议[。，！!]?", re.MULTILINE),
)

ALL_NOISE = PROMO_PATTERNS + PLATFORM_PATTERNS + DISCLAIMER_PATTERNS


def strip_noise(text: str) -> tuple[str, Counter]:
    """行内剔除噪声，返回清洗后文本和各模式命中计数。"""
    hits: Counter[str] = Counter()
    for group, patterns in (
        ("promo", PROMO_PATTERNS),
        ("platform", PLATFORM_PATTERNS),
        ("disclaimer", DISCLAIMER_PATTERNS),
    ):
        for pattern in patterns:
            text, count = pattern.subn("", text)
            if count:
                hits[group] += count
    # 剔除后可能留下孤立标点和多余空白
    text = re.sub(r"[ \u3000]{2,}", " ", text)
    text = re.sub(r"^[\s，。、；：！？]+", "", text)
    return text.strip(), hits


# ---------------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalized_fingerprint(text: str) -> str:
    """抽取后正文的指纹：去掉全部空白后取 sha256。

    这是判定 docx/txt 同源对的唯一可靠方式。实测 `防爆头2.0.docx` 与
    `防爆头2.0.txt` 文件 sha256 分别是 9c6ee088ca5e / d6cb4fe917ac（不同），
    但本函数对两者返回同一个值 799273463fa6。
    """
    return sha256_bytes(re.sub(r"\s", "", text).encode("utf-8"))


def read_text_file(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def raw_paragraphs(path: Path) -> list[str]:
    """按载体取原始段落，保持文档顺序。docx 同时取表格。"""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        document = Document(path)
        items = [paragraph.text for paragraph in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                items.append(" | ".join(cell.text for cell in row.cells))
        return items
    if suffix == ".txt":
        return read_text_file(path).splitlines()
    return []


def export_images(path: Path, assets_dir: Path, doc_id: str) -> list[dict]:
    """把 docx 内嵌图导出到 assets，返回图片清单。不做 OCR。

    word/media/ 里的目录项（file_size 0）要排除，否则会被算成一张图 ——
    最初统计出「12 张」就是把两个目录项算进去了，真实是 10 张。
    """
    exported: list[dict] = []
    if path.suffix.lower() != ".docx":
        return exported
    try:
        with zipfile.ZipFile(path) as archive:
            media = [
                name
                for name in sorted(archive.namelist(), key=natural_key)
                if name.startswith("word/media/")
                and not name.endswith("/")
                and archive.getinfo(name).file_size > 0
            ]
            for order, name in enumerate(media, start=1):
                data = archive.read(name)
                target = assets_dir / f"{doc_id}-img{order:02d}{Path(name).suffix.lower()}"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                exported.append(
                    {
                        "order": order,
                        "file": str(target),
                        "bytes": len(data),
                        "source_entry": name,
                    }
                )
    except (OSError, zipfile.BadZipFile) as exc:
        exported.append({"error": f"zip_failed: {exc}"})
    return exported


# ---------------------------------------------------------------------------
# 日期与标题
# ---------------------------------------------------------------------------

# 共享的 extract_date() 对这批文件名只认出 1/9（只有 `20260418-` 成功）。
# `6月20日`、`6.14-19`、`05-23 13:18` 全部返回空。所以自己实现三级回退。
#
# 关于误判的一个好消息：担心 `6.14-19` 被 short_patterns 的
# `(\d{2})[./_-](\d{1,2})[./_-](\d{1,2})` 匹配成 2006-14-19 —— 实测没有发生
# （14 > 12 被月份校验挡住）。但本函数不依赖那个巧合，自己显式校验。

FILENAME_FULL = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
# 「6月20日」和「6月21」两种写法都要认：实测 `6月21汇阳财策—空空龙` 没有「日」字，
# 只匹配带「日」的写法会让文件名日期漏掉，然后被正文里提到的另一个日期（6月18）顶掉，
# 得到一个看起来合理其实错的 date。
FILENAME_MONTH_DAY = re.compile(r"(?<!\d)(\d{1,2})月(\d{1,2})日?")
FILENAME_RANGE = re.compile(r"(?<!\d)(\d{1,2})[.．](\d{1,2})\s*[-—~]\s*(\d{1,2})(?!\d)")
FILENAME_DOTTED = re.compile(r"(?<!\d)(\d{1,2})[.．](\d{1,2})(?!\d)")
BODY_TIMESTAMP = re.compile(r"(?<!\d)(\d{2})-(\d{2})\s+\d{1,2}[：:]\d{2}")
BODY_FULL_DATE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})")
BODY_YEAR = re.compile(r"(20\d{2})\s*年")
# 举例/回顾语境里的年份**不能当成文时间**。
# 实测 `主升龙头空空1090 情绪周期 四万字` 全篇只有两个年份，都是举例：
#   「比如 2022 年浙江建投作为基建总真龙…」
#   「比如 2023 年消费退潮期的全聚德…」
# 取最大值会把这篇 26701 字的方法论标成 2023 年（真实成文时间不明，同批在 2026），
# 「按时间看作者观点演变」的查询会因此错排三年。宁可返回 unknown。
EXAMPLE_YEAR_CONTEXT = re.compile(
    r"(?:比如|例如|譬如|好比|回顾|当年|历史上|早在|记得|曾经|参考)"
    r"[^。！？\n]{0,12}?(20\d{2})\s*年"
)


def _valid(month: int, day: int) -> bool:
    return 1 <= month <= 12 and 1 <= day <= 31


def resolve_date(filename: str, body: str, batch_year: int | None = None) -> tuple[str, str]:
    """定日期，返回 (date, precision)。

    precision 取值：
      ``day``          精确到日，可信
      ``day_inferred`` 月日来自文件名或正文时间戳、年份由**本文正文**的 ``20XX年`` 推断
      ``day_batch``    月日可信，但年份来自**批次推断**（同系列其它文件），可信度更低
      ``year_only``    只有年份，且年份是本文正文明示的
      ``year_batch``   只有年份，且**年份本身是批次推的** —— 最弱的一档
      ``unknown``      定不出来

    年份必须有出处。绝不用「当前年份」兜底 —— 那会给历史文章打上今年的日期，
    比留空更糟（留空至少是诚实的缺失）。

    ``batch_year`` 是调用方按同批次证据传进来的年份。它与本文正文推断出的年份
    分开标注（``day_batch`` vs ``day_inferred``），因为前者的依据在文件之外，
    检索到这类块时要知道日期是推的。
    """
    # 1) 文件名里的完整 8 位日期，最可信
    match = FILENAME_FULL.search(filename)
    if match:
        year, month, day = (int(part) for part in match.groups())
        if _valid(month, day):
            return f"{year:04d}-{month:02d}-{day:02d}", "day"

    # 正文里能拿到的年份，供后面补全月日用
    year_hint = None
    full = BODY_FULL_DATE.search(body)
    if full:
        year_hint = int(full.group(1))
    else:
        # 先剔掉「比如2023年…」这类举例语境里的年份，剩下的才可能是成文时间。
        example_years = Counter(EXAMPLE_YEAR_CONTEXT.findall(body))
        all_years = Counter(BODY_YEAR.findall(body))
        usable = all_years - example_years
        if usable:
            # 取最大值：正文确实会回溯往年（「早在2024年初就锚定了国产替代」），
            # 在排除举例之后，最大的那个更接近成文时间
            year_hint = max(int(value) for value in usable.elements())

    def with_year(month: int, day: int) -> tuple[str, str] | None:
        """给定月日，按「本文年份 > 批次年份」的优先级补全，并标出精度来源。"""
        if not _valid(month, day):
            return None
        if year_hint:
            return f"{year_hint:04d}-{month:02d}-{day:02d}", "day_inferred"
        if batch_year:
            return f"{batch_year:04d}-{month:02d}-{day:02d}", "day_batch"
        return None

    # 2) 文件名里的「N月N日」/「N月N」。文件名日期优先于正文里提到的任何日期 ——
    # 实测 `6月21汇阳财策—空空龙` 正文里提到 2026年6月18日，若让正文优先
    # 会把这篇标成 06-18，而它其实是 6月21 的策略。
    match = FILENAME_MONTH_DAY.search(filename)
    if match:
        resolved = with_year(int(match.group(1)), int(match.group(2)))
        if resolved:
            return resolved

    # 3) 文件名里的日期区间「6.14-19」，取区间起点
    match = FILENAME_RANGE.search(filename)
    if match:
        resolved = with_year(int(match.group(1)), int(match.group(2)))
        if resolved:
            return resolved

    # 4) 正文里的发布时间戳「05-23 13:18」
    match = BODY_TIMESTAMP.search(body)
    if match:
        resolved = with_year(int(match.group(1)), int(match.group(2)))
        if resolved:
            return resolved

    # 5) 正文里的完整年月日
    if full:
        year, month, day = (int(part) for part in full.groups())
        if _valid(month, day):
            return f"{year:04d}-{month:02d}-{day:02d}", "day_inferred"

    # 6) 文件名里的「6.21」这类无区间点分日期。
    # 必须排除版本号：「防爆头2.0」的 `2.0` 会匹配成 2月0日，`day != 0` 挡掉它。
    match = FILENAME_DOTTED.search(filename)
    if match and "月" not in filename:
        month, day = (int(part) for part in match.groups())
        if day != 0:
            resolved = with_year(month, day)
            if resolved:
                return resolved

    # 只有年、没有月日。区分年份是本文明示的还是批次推的 ——
    # 统一叫 `month` 会暗示「年是准的、只缺月日」，而后者连年份都是猜的。
    if year_hint:
        return f"{year_hint:04d}", "year_only"
    if batch_year:
        return f"{batch_year:04d}", "year_batch"
    return "", "unknown"


def resolve_title(path: Path) -> str:
    """标题：剥掉日期前缀和文件后缀，但不丢掉正文里没有的信息。

    不用共享的 clean_title()：它把 `6月20日中午空空龙汇阳财策` 剥成
    `中午空空龙汇阳财策`（把日期吃掉了，而 extract_date 又认不出来，
    结果日期信息两边都丢），且对 `20260418-…` 前缀无效（正则要求分隔符，
    纯 8 位数字不匹配），还会留下 emoji。
    """
    title = path.stem
    title = FILENAME_FULL.sub("", title, count=1)
    # emoji 与装饰符号：`✅️` 之类进标题会出现在 FTS 索引和引用里
    title = re.sub(r"[\U0001F300-\U0001FAFF☀-➿️‍]+", "", title)
    title = re.sub(r"（\s*[\d.]+\s*万字\s*）", "", title)
    title = title.strip(" .-_—　")
    # 剥完只剩装饰符或纯数字/版本号时退回原名。`2.0.docx` 会得到标题 `2.0`，
    # 而引用格式 `[来源|作者|文档|日期|定位]` 的文档位显示 `2.0` 读者无从判断是哪篇。
    if not title or not re.search(r"[一-鿿A-Za-z]", title):
        return path.stem
    return title


# ---------------------------------------------------------------------------
# 内容分类
# ---------------------------------------------------------------------------

NAMED_STOCK = re.compile(
    r"[一-鿿]{2,6}\s*[（(]?\d{6}"
    r"|宁德时代|工业富联|中际旭创|寒武纪|新易盛|东山精密|诺德股份|双星新材"
    r"|旭光电子|麦格米特|昊华科技|永鼎股份|风华高科|沪电股份|阳光电源|大唐发电"
)
CONCRETE_DATE = re.compile(r"\d{1,2}[.月]\d{1,2}\s*号|(?<!\d)\d{2}-\d{2}\s+\d{1,2}[：:]\d{2}")
RULE_WORD = re.compile(r"必须|坚决|绝对不|直接放弃|红线|铁律|不能碰|一定要|切忌")


def classify_document(text: str) -> str:
    """区分方法论长文与盘面点评。

    这个区分不是分类学洁癖，它决定引用时的性质：方法论长文是可长期复用的规则，
    盘面点评是特定时点对具体个股的判断，**不能当规律引用**（CLAUDE.md 的
    「不把作者历史观点当作当前市场规律」）。

    判据来自实测：两类文本在「具名个股数」和「具体日期数」上分得很开 ——
    5 篇方法论长文的具名个股数全为 0，2 篇盘面点评是 20 和 5。
    """
    stocks = len(NAMED_STOCK.findall(text))
    dates = len(CONCRETE_DATE.findall(text))
    if stocks >= 5 and dates >= 3:
        return "market_commentary"
    return "method_article"


def is_truncated(text: str) -> bool:
    """正文是否在句子中间断掉。

    `虚假骗炮封单全维度拆解.docx` 的 document.xml 只有 1157 字符，末尾停在
    「虚假封单的峰值量对应流通盘」——**原文就是残缺的**，不是抽取问题。
    这类要标出来，否则读者会以为读到了完整论述。
    """
    compact = re.sub(r"\s", "", text)
    if not compact:
        return False
    # 表格行（`a | b | c`）会被 raw_paragraphs 追加到段落末尾，它天然不以句末标点结尾。
    # 只看「最后一句有没有句号」会把任何以表格收尾的 docx 误标成残缺。
    if compact.rstrip().endswith("|") or "|" in compact[-40:]:
        return False
    tail = compact[-30:]
    if re.search(r"[。！？；）」】…】.!?)]$", tail):
        return False
    # 缺句号但语义完整的收尾（「…要果断止盈离场」）不算残缺 —— 实测 2 篇被这样误报。
    # 真残缺的特征是**断在句子成分不完整处**：末尾停在介词/连词/助词，或停在一个
    # 等着被描述的名词上（`虚假骗炮封单` 停在「…峰值量对应流通盘」——「流通盘」后面
    # 本该跟「的百分之几」）。前者靠词尾判断，后者靠「句中出现了引导词却没有下文」。
    if re.search(r"(?:的|地|得|把|将|对|对应|超过|不足|达到|占|比|在|从|与|和|及|或|且|而|则|即)$", tail):
        return True
    # 「A 对应 B」「A 相当于 B」这类结构里，B 是个光名词且句子就此结束 —— 没说完。
    return bool(re.search(r"(?:对应|相当于|等于|取决于|依赖于)[一-鿿]{2,6}$", tail))


# ---------------------------------------------------------------------------
# 建单元
# ---------------------------------------------------------------------------

# 合并参数。实测四组取值在这批文档上的产出：
#   (400, 800)  → 292 块，中位 451，仍有 0.3% 短块
#   (500, 1000) → 242 块，中位 564，零短块        ← 采用
# 既有来源的正文块中位是 428（南京路）～878（爱在冰川），564 落在区间内。
MERGE_MIN = 500
MERGE_MAX = 1000
# 单段超过这个长度先切开再合并。取 800 与 MERGE_MAX 拉开距离，
# 避免切出的片段一进合并就超上限。
SPLIT_TARGET = 800


def build_units(paragraphs: list[str]) -> tuple[list[dict], Counter]:
    """原始段落 → 检索单元。返回 (单元列表, 噪声计数)。

    locator 用**原始段号**而不是切块序号，这与波段之门不同：
    `import_boduanzhimen.py:510` 的 `正文第{index}段` 里 index 来自
    `split_text()` 的枚举，也就是「第几个切块」，不是原文第几段 ——
    回原文找不到对应段落。这里保留真实段号，超长段切片加「之N」后缀区分。
    """
    noise: Counter[str] = Counter()
    units: list[dict] = []
    for number, raw in enumerate(paragraphs, start=1):
        text = clean_text(raw)
        if not text:
            continue
        text, hits = strip_noise(text)
        noise.update(hits)
        if not text:
            noise["paragraph_dropped_all_noise"] += 1
            continue
        pieces = split_text(text, SPLIT_TARGET)
        if len(pieces) <= 1:
            units.append({"locator": f"正文第{number}段", "text": text})
            continue
        # 超长段被切开：给每片加序号，否则 N 片共用一个 locator，
        # 引用时无法定位到具体位置（既有来源普遍有此缺陷：
        # 南京路 214 块、郁金香 158 块、波段之门 71 块同文档内 locator 重复）
        noise["paragraph_split"] += 1
        for index, piece in enumerate(pieces, start=1):
            units.append({"locator": f"正文第{number}段之{index}", "text": piece})
    return units, noise


def merge_units(units: list[dict]) -> list[dict]:
    return merge_short_units(
        units, min_chars=MERGE_MIN, max_chars=MERGE_MAX, join_with="\n"
    )


# ---------------------------------------------------------------------------
# 版本关系（人工登记，来自实测的近似重复度矩阵）
# ---------------------------------------------------------------------------

# 判据：段落级语义近似（difflib ratio ≥ 0.75，标点与空白归一化后，段长 ≥15 字）。
# 门槛：**对照全部已建块文档**后，独有实质内容 < 1500 字才算真冗余。
#
# ⚠️ 对照集必须是「全部已建块文档」，不是单一目标。这里踩过坑：
#   只对 20260418 一篇算，防爆头2.0 独有 2434 字（16.4%）—— 超门槛；
#   对 20260418 + 新日期拆解 两篇算，独有降到 1576 字，其中 120 字还是重复 5 次的
#   平台头（`[空空龙(5.17-5.22)][主升龙头空空龙]05-23 13:18`），
#   **实质内容只有 3 段 1426 字**，且那 3 段是 20260418 里同一章节的改写版
#   （「一、正本清源」「筹码结构是生死线」，论述相同、措辞不同）。
#   我最初写的「独有 9 段/1576 字 = 11%」分母取错了，11% 这个数不成立。
#
#   防爆头2.0.docx        被 20260418 覆盖 ~90%（各段长门槛下 89.2%~90.4%）；
#                        对照 20260418+新日期拆解 后独有 1576 字（实质 1426 字）→ 判冗余
#   创业板科创板短差.docx  被 科创20cm套利 覆盖 88%~96%，独有 4~11 段 / 128~313 字 → 判冗余
#
# 不登记的（差异真实，都要建块）：
#   20260418 vs 新日期拆解 —— 各有 39% / 43% 独有内容，互不可替代
#   竞价抢筹全篇 被 新日期拆解覆盖 75% —— 未达门槛，且它有独立的量化公式小节
SUPERSEDED = {
    "防爆头2.0.docx": "20260418-超预期防爆头炸板全体系干货心得拆解（2.2万字）✅️.docx",
    "创业板科创板短差的思路：量化时代的超短生存法则.docx": "科创20cm套利.docx",
}

# 同一篇的另一种载体，内容一致（抽取后指纹相同时才生效，代码会校验）。
# 保留 docx 作正文源，与波段之门一致（md 为正文源、其它格式登记为 alternate）。
PREFER_DOCX_OVER_TXT = {
    "防爆头2.0.txt": "防爆头2.0.docx",
    "创业科创20cm套利.txt": "科创20cm套利.docx",
}

# 这批文件同属 2026 年「汇阳财策」系列。证据（不是假设）：
#   竞价抢筹全篇  栏目「6.12~6.19号」+ 时间戳 06-14
#   空空龙防爆头新日期拆解文字6.14-19  正文明确写 2026年，同一周同系列
#   6月20日中午…  栏目「6.19~6.26号」，紧接其后
# 所以对正文无年份、月日可信的文件，用 2026 补全并标 day_batch。
BATCH_YEAR = 2026


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个文件，0 为全部（调试用）")
    parser.add_argument("--dry-run", action="store_true", help="只报告不写产物")
    args = parser.parse_args()

    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    source = next(item for item in config["sources"] if item["id"] == SOURCE_ID)
    source_root = ROOT / source["source_path"]
    lib = ROOT / "_知识库系统" / "source_libraries" / SOURCE_ID
    converted_dir = lib / "converted_docx"
    assets_dir = lib / "assets"
    for directory in ("texts", "maps"):
        (lib / directory).mkdir(parents=True, exist_ok=True)

    documents: list[dict] = []
    parents: list[dict] = []
    chunks: list[dict] = []
    errors: list[dict] = []
    stats: Counter[str] = Counter()
    noise_total: Counter[str] = Counter()
    image_map: list[dict] = []

    # 两张人工登记表的名字必须真实存在，否则「该判冗余的建了块」或「指向不存在的目标」
    # 都会静默发生而 errors 仍是 0。文件改名就会让登记表失效，这里先拦住。
    present = {
        path.name
        for path in source_root.iterdir()
        if path.is_file() and path.suffix.lower() in (".docx", ".txt", ".doc")
    }
    for table_name, table in (("SUPERSEDED", SUPERSEDED), ("PREFER_DOCX_OVER_TXT", PREFER_DOCX_OVER_TXT)):
        for key, value in table.items():
            for role, filename in (("key", key), ("value", value)):
                if filename not in present:
                    errors.append(
                        {
                            "stage": "registry",
                            "path": filename,
                            "error": f"{table_name} 的 {role} 指向不存在的文件 —— 登记表已过期，需更新",
                        }
                    )

    # 收集待处理文件。.doc 用已转换好的 docx 顶替，原 .doc 记在 converted_from。
    candidates: list[tuple[Path, Path]] = []  # (读取用的路径, 登记用的原始路径)
    for path in sorted(source_root.iterdir(), key=lambda item: natural_key(item.name)):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in (".docx", ".txt"):
            candidates.append((path, path))
        elif suffix == ".doc":
            converted = converted_dir / f"{path.stem}.docx"
            if converted.exists():
                candidates.append((converted, path))
                stats["doc_converted"] += 1
            else:
                errors.append(
                    {
                        "stage": "doc_convert",
                        "path": str(path),
                        "error": "缺少转换产物，先跑 convert_doc_to_docx.ps1",
                    }
                )
        else:
            stats["unsupported"] += 1
            errors.append({"stage": "scan", "path": str(path), "error": f"未支持的格式 {suffix}"})
    if args.limit:
        candidates = candidates[: args.limit]

    # 第一遍：抽取正文并算指纹，为判重做准备
    extracted: list[dict] = []
    for read_path, origin in candidates:
        try:
            paragraphs = raw_paragraphs(read_path)
        except Exception as exc:
            errors.append({"stage": "extract", "path": str(origin), "error": repr(exc)})
            continue
        units, noise = build_units(paragraphs)
        body = "\n".join(unit["text"] for unit in units)
        extracted.append(
            {
                "read_path": read_path,
                "origin": origin,
                "units": units,
                "body": body,
                # 求日期必须用**未清洗**的原文。清洗会销毁日期证据：platform 正则里的
                # `时间[：:]\d{2}-\d{2} \d{1,2}[：:]\d{2}` 和独立成行的时间戳两条，
                # 正是为了剔掉发布时间戳而写的，而 BODY_TIMESTAMP 要找的就是它们。
                # 实测 `竞价抢筹…全篇` 用清洗后正文求日期会从 2026-06-14 退化成 2026 ——
                # 清洗噪声顺手把唯一的日期依据删掉了。
                "raw_body": "\n".join(paragraphs),
                "noise": noise,
                "fingerprint": normalized_fingerprint(body),
                "cjk": len(re.findall(r"[一-鿿]", body)),
            }
        )

    by_name = {item["origin"].name: item for item in extracted}

    # 指纹判重：先按 PREFER_DOCX_OVER_TXT 的人工指定挑正本，没指定的才按候选顺序。
    #
    # ⚠️ 这里曾有一个让整篇内容静默消失的 bug：原先无条件 setdefault（按候选顺序
    # 定正本），再让 PREFER_DOCX_OVER_TXT 事后覆盖 duplicate_of。两个机制反向打架 ——
    # `创业科创20cm套利.txt` 排在 `科创20cm套利.docx` 之前，于是 docx 被判为 txt 的副本；
    # 紧接着登记表又把 txt 判为 docx 的副本。**两篇互指对方是正本，结果都没建块**，
    # `20cm 套利` 那套方法论（4231 字）整篇不可检索，而 errors 仍是 0。
    # 现在正本选择只有一处，从源头上不可能自相矛盾。
    canonical_by_fingerprint: dict[str, Path] = {}
    for item in extracted:
        preferred_name = PREFER_DOCX_OVER_TXT.get(item["origin"].name)
        if preferred_name:
            partner = by_name.get(preferred_name)
            if partner is not None and partner["fingerprint"] == item["fingerprint"]:
                # 登记表指定了正本，且指纹确实相同 —— 以登记表为准
                canonical_by_fingerprint[item["fingerprint"]] = partner["origin"]
    for item in extracted:
        canonical_by_fingerprint.setdefault(item["fingerprint"], item["origin"])

    for item in extracted:
        origin: Path = item["origin"]
        name = origin.name
        units: list[dict] = item["units"]
        body: str = item["body"]
        doc_id = f"{SOURCE_ID}-{sha256_bytes(name.encode('utf-8'))[:12]}"

        duplicate_of = ""
        superseded_by = ""
        skip_reason = ""

        # ① 指纹相同 → 副本。正本已在上面统一选定，这里只做归属判定，不再二次覆盖。
        canonical = canonical_by_fingerprint[item["fingerprint"]]
        if canonical != origin:
            duplicate_of = canonical.name
            skip_reason = "duplicate_fingerprint"
        # 登记表的一致性校验：指定的正本必须在候选里、且指纹真的相同。
        # 不一致就报错而不是默默按登记表走（登记表可能因内容更新而过期）。
        expected = PREFER_DOCX_OVER_TXT.get(name)
        if expected:
            partner = by_name.get(expected)
            if partner is None:
                errors.append({"stage": "dedup", "path": name, "error": f"登记的正本 {expected} 不在候选里"})
            elif partner["fingerprint"] != item["fingerprint"]:
                errors.append(
                    {
                        "stage": "dedup",
                        "path": name,
                        "error": f"登记 {expected} 为正本，但抽取指纹不同 —— 内容已分叉，登记表需更新",
                    }
                )

        # ② 版本被取代 → 不建块，但登记关系。
        # 注意顺序：副本（①）也要走这一步。`防爆头2.0.txt` 是 `防爆头2.0.docx` 的副本，
        # 而后者自己被 20260418 版取代 —— 若只登记「指向 docx」，那是个断链：
        # docx 没建块，读者顺着指引找不到内容。这里让副本继承正本的 superseded 关系，
        # 指向真正建了块的那一篇。
        if name in SUPERSEDED:
            superseded_by = SUPERSEDED[name]
            if not skip_reason:
                skip_reason = "superseded"
        elif duplicate_of and duplicate_of in SUPERSEDED:
            superseded_by = SUPERSEDED[duplicate_of]

        # ③ 内容太少 → 整篇跳过并记录（不静默丢）
        if not skip_reason and item["cjk"] < MIN_DOC_CJK:
            skip_reason = f"too_short_cjk({item['cjk']})"

        images = [] if skip_reason else export_images(item["read_path"], assets_dir, doc_id)
        real_images = [entry for entry in images if "error" not in entry]
        for entry in images:
            if "error" in entry:
                errors.append({"stage": "image_export", "path": name, "error": entry["error"]})
                continue
            image_map.append({"document_id": doc_id, "title": resolve_title(origin), **entry})
        stats["images_exported"] += len(real_images)

        date, precision = resolve_date(name, item["raw_body"], batch_year=BATCH_YEAR)
        title = resolve_title(origin)
        content_kind = classify_document(body)
        truncated = is_truncated(body) if body else False

        risk: list[str] = []
        if truncated:
            risk.append("原文在句子中间截断，论述不完整，需回看原文件")
        if real_images:
            risk.append(f"含{len(real_images)}张行情截图未OCR，图中数据需回看原文件")
        if precision == "day_batch":
            risk.append("日期的年份由同批次文件推断，非本文明示")
        elif precision == "month":
            risk.append("只能定位到月份，无具体日期")
        elif precision == "unknown":
            risk.append("无日期线索")
        if content_kind == "market_commentary":
            risk.append("盘面点评：针对特定时点与个股的判断，不可作为通用规律引用")

        documents.append(
            {
                "source_id": SOURCE_ID,
                "document_id": doc_id,
                "title": title,
                "date": date,
                "author_or_guest": AUTHOR,
                "content_type": content_kind,
                "topics": infer_topics(title, body),
                "characters": item["cjk"],
                "original_path": str(origin),
                "normalized_text_path": str(lib / "texts" / f"{doc_id}.txt"),
                "sha256": sha256_bytes(origin.read_bytes()),
                "content_fingerprint": item["fingerprint"],
                "duplicate_of": duplicate_of,
                "superseded_by": superseded_by,
                "skip_reason": skip_reason,
                "converted_from": str(origin) if origin.suffix.lower() == ".doc" else "",
                "date_precision": precision,
                "image_count": len(real_images),
                "truncated": truncated,
                "risk_flags": "；".join(risk),
                "_units": [] if skip_reason else merge_units(units),
                "_kind": content_kind,
            }
        )
        noise_total.update(item["noise"])
        if skip_reason:
            stats[f"skipped_{skip_reason.split('(')[0]}"] += 1
            continue

        write_text_lf(
            lib / "texts" / f"{doc_id}.txt",
            "\n\n".join(f"[{unit['locator']}]\n{unit['text']}" for unit in units) + "\n",
        )

    # 建 parents / chunks
    for document in documents:
        units = document.pop("_units", [])
        kind = document.pop("_kind", "method_article")
        if not units:
            continue
        groups: list[list[dict]] = []
        current: list[dict] = []
        length = 0
        for unit in units:
            if current and length + len(unit["text"]) > 4000:
                groups.append(current)
                current, length = [], 0
            current.append(unit)
            length += len(unit["text"])
        if current:
            groups.append(current)
        for parent_number, group in enumerate(groups, start=1):
            parent_id = f"{document['document_id']}-p{parent_number:03d}"
            locator = (
                group[0]["locator"]
                if len(group) == 1
                else f"{group[0]['locator']}—{group[-1]['locator']}"
            )
            parents.append(
                {
                    "source_id": SOURCE_ID,
                    "document_id": document["document_id"],
                    "parent_id": parent_id,
                    "title": document["title"],
                    "date": document["date"],
                    "author_or_guest": AUTHOR,
                    "locator": locator,
                    "text": "\n".join(unit["text"] for unit in group),
                }
            )
            for child_number, unit in enumerate(group, start=1):
                chunks.append(
                    {
                        "source_id": SOURCE_ID,
                        "source_name": source["display_name"],
                        "document_id": document["document_id"],
                        "parent_id": parent_id,
                        "chunk_id": f"{parent_id}-c{child_number:02d}",
                        "chunk_type": kind,
                        "title": document["title"],
                        "date": document["date"],
                        "author_or_guest": AUTHOR,
                        "topics": document["topics"],
                        # claim_type 在别的来源全是写死的常量、不可用于过滤。
                        # 这里按文档性质给两个真实取值，至少这个来源可用它区分
                        # 「规则」与「历史观点」。
                        "claim_type": "rule_or_method" if kind == "method_article" else "opinion_or_case",
                        "market_regime": "未标注",
                        "locator": unit["locator"],
                        "text": unit["text"],
                        "original_path": document["original_path"],
                        "confidence": "high",
                        "extraction_method": "docx" if document["original_path"].lower().endswith((".docx", ".doc")) else "txt",
                        "image_path": "",
                        "date_precision": document["date_precision"],
                    }
                )
                stats[f"chunk_{kind}"] += 1

    # 闭合性断言：每一篇不建块的文档，它指向的正本/新版**必须真的建了块**。
    # 这是上面那个循环 bug 唯一能被自动抓住的地方 —— 当时两篇互指对方，
    # 双方都没建块，而 errors 是 0、报告看起来一切正常。
    # 「A 不建块因为内容在 B 里」这句话只有在 B 确实入库时才成立。
    built_names = {
        Path(document["original_path"]).name
        for document in documents
        if not document["skip_reason"]
    }
    for document in documents:
        if not document["skip_reason"]:
            continue
        # 优先看 superseded_by：副本继承正本的取代关系后，它才是真正建块的那一篇。
        target = document["superseded_by"] or document["duplicate_of"]
        if not target:
            continue
        if target not in built_names:
            errors.append(
                {
                    "stage": "dedup_closure",
                    "path": Path(document["original_path"]).name,
                    "error": (
                        f"标为 {document['skip_reason']} 并指向 {target}，"
                        f"但 {target} 自己也没建块 —— 这篇的内容会整篇丢失"
                    ),
                }
            )

    if args.dry_run:
        report = {
            "dry_run": True,
            "candidates": len(candidates),
            "documents": len(documents),
            "would_build_chunks": len(chunks),
            "skipped": {key: value for key, value in stats.items() if key.startswith("skipped_")},
            "noise": dict(noise_total),
            "errors": errors,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if not errors else 1

    write_jsonl(lib / "documents.jsonl", documents)
    write_jsonl(lib / "parents.jsonl", parents)
    write_jsonl(lib / "chunks.jsonl", chunks)
    write_jsonl(lib / "maps" / "image_map.jsonl", image_map)

    content_map = [
        f"# {source['display_name']} 内容地图",
        "",
        "| 日期 | 日期精度 | 标题 | 类型 | 中文字数 | 配图 | 状态 |",
        "|---|---|---|---|---:|---:|---|",
    ]
    for item in sorted(documents, key=lambda row: (row.get("date") or "9999", row["title"])):
        state = item["skip_reason"] or "已建块"
        content_map.append(
            f"| {item.get('date') or '未标注'} | {item['date_precision']} | "
            f"{item['title'].replace('|', '｜')} | {item['content_type']} | "
            f"{item['characters']} | {item['image_count']} | {state} |"
        )
    write_text_lf(lib / "content_map.md", "\n".join(content_map) + "\n")

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_id": SOURCE_ID,
        "upstream_path": UPSTREAM,
        "candidates": len(candidates),
        "documents": len(documents),
        "documents_with_chunks": sum(1 for item in documents if not item["skip_reason"]),
        "parents": len(parents),
        "chunks": len(chunks),
        "stats": dict(stats),
        "noise_removed": dict(noise_total),
        "errors": errors,
        "error_count": len(errors),
    }
    write_text_lf(lib / "source_summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")

    built = [item for item in documents if not item["skip_reason"]]
    skipped = [item for item in documents if item["skip_reason"]]
    quality = [
        f"# {source['display_name']} 导入质量报告",
        "",
        f"- 候选文件：{len(candidates)}（原始 14 个文件，其中 1 个 .doc 已转 docx）",
        f"- 建块文档：{len(built)}",
        f"- 登记但不建块：{len(skipped)}",
        f"- 父块：{len(parents)}",
        f"- 检索块：{len(chunks)}",
        f"- 导出配图：{stats['images_exported']} 张（未 OCR）",
        f"- 剔除噪声：{json.dumps(dict(noise_total), ensure_ascii=False)}",
        f"- 错误：{len(errors)}",
        "",
        "## 不建块的文档及原因",
        "",
        "| 文件 | 原因 | 指向 |",
        "|---|---|---|",
    ]
    for item in skipped:
        target = item["duplicate_of"] or item["superseded_by"] or "—"
        quality.append(f"| {item['title']} | {item['skip_reason']} | {target} |")
    quality += [
        "",
        "## 已知边界",
        "",
        "- **原始资料是副本**：上游在 `" + UPSTREAM + "`，本目录是复制进来的。"
        "上游更新后本副本不会自动同步，重新导入前需手动同步。",
        "- **`防爆头2.0` 不建块**：段数口径下被 `20260418` 覆盖 89.2%~91.0%（随段长门槛变化）。"
        "**对照集是「全部已建块文档」而非单一目标** —— 只对 `20260418` 算独有 2434 字（16.47%），"
        "对 `20260418`+`新日期拆解` 两篇一起算降到 1576 字，其中 120 字是重复 5 次的平台头，"
        "**实质 4 段 1456 字**（含一个 30 字章节标题）。"
        "这 4 段的 12-gram 主要被 **`新日期拆解`** 吸收（覆盖 85.0%/95.8%/90.7%/100%），"
        "而非 `20260418`（46.0%~68.9%）—— 是同一论述的改写版（「暴利」→「正反馈」）。"
        "而 `20260418` 与 `新日期拆解` 各有 39%/43% 独有内容，两篇都建块。",
        "- **副本的指向会继承正本的取代关系**：`防爆头2.0.txt` 是 `防爆头2.0.docx` 的副本，"
        "而后者被 `20260418` 取代，所以它的 `superseded_by` 指向 `20260418` 而不是 docx —— "
        "否则读者顺着指引会找到一篇同样没建块的文档。导入器有闭合断言强制这一点。",
        "- **`虚假骗炮封单全维度拆解` 原文残缺**：末尾停在「虚假封单的峰值量对应流通盘」，"
        "句子未完，`document.xml` 仅 1157 字符。已建块但标了 risk_flag。",
        "- **10 张行情截图未 OCR**：实测 OCR 能读出股票名（中际旭创 300308）但数字全是"
        "垃圾（`146 . 98 277 , 76`），CJK 率 0.32 低于 0.45 质量门槛。图已导出到 "
        "`assets/`，`maps/image_map.jsonl` 记路径，需要时回看原图。",
        "- **日期精度分五档**：`day`（文件名有完整日期）/ `day_inferred`（月日可信、"
        "年份来自本文正文）/ `day_batch`（月日可信、**年份由同批次推断**）/ "
        "`year_only`（只有年份且为本文明示）/ `year_batch`（**连年份都是推的**）/ `unknown`。"
        "2026-08-09 起该字段**已进索引**（`chunks` 表第 20 列），可直接 SQL 过滤："
        "`WHERE date_precision IN ('day','day_inferred')` 才是日期可信的那部分（本来源 86/172 块）。",
        "- **求日期用未清洗原文**：噪声正则会剔掉发布时间戳（`时间:06-14 13:17`），"
        "而那往往是唯一的日期依据。曾因为拿清洗后正文求日期，让 `竞价抢筹…全篇` "
        "从 2026-06-14 退化成 2026。",
        "- **举例年份不算成文时间**：`主升龙头空空1090` 全篇只有「比如2022年浙江建投」"
        "「比如2023年全聚德」两个年份，都是历史举例。取最大值会把这篇 26701 字的方法论"
        "标成 2023 年（真实成文时间不明，同批在 2026）。已按举例语境排除，改判 `year_batch`。",
        "- **推广联系方式已剔除**：`一手拼课vx：hzp9512055818`、`一手拼课QQ865154818` 共 16 处，"
        "行内剔除不删整段（部分嵌在正文句子中间）。分布：`科创20cm套利.docx` 6 处"
        "（**该篇已建块**，所以这项清洗对入库正文确有贡献）、`创业科创20cm套利.txt` 6 处、"
        "`创业板科创板短差` 4 处（后两篇不建块）。已核验入库 172 块里推广/平台/免责残留均为 0。",
        "- **`claim_type` 在本来源可用**：`rule_or_method`（方法论长文）与 "
        "`opinion_or_case`（盘面点评）两个真实取值，不是写死的常量。"
        "其它五个来源该字段全是固定值、不可用于过滤。",
    ]
    write_text_lf(lib / "quality_report.md", "\n".join(quality) + "\n")

    source["status"] = "integrated" if not errors else "integrated_with_warnings"
    source["adapter"] = "specialized_kongkonglong"
    source["pipeline"] = "docx_method_articles"
    source["primary_locator"] = "paragraph_number"
    source["source_type"] = "method_article_docx"
    source["upstream_path"] = UPSTREAM
    source["last_import_summary"] = {
        "documents": len(documents),
        "documents_with_chunks": len(built),
        "chunks": len(chunks),
        "errors": len(errors),
    }
    source.pop("review_required", None)
    source.pop("complexity_signals", None)
    source.pop("format_counts", None)

    # 只写本来源自己的 source.yaml，**不回写全局 config/sources.yaml**。
    #
    # ⚠️ 这里原来是 `yaml.safe_dump(config)` 整文件覆盖 —— 那会把 sources.yaml 的
    # **24 行注释全部抹掉**（包括 smoke_query 字段的用法说明、panfeng 那段「为什么
    # id 用作者身份而非载体」的设计理由），还会把行内列表 `[.md, .txt, ...]` 展开成
    # 逐行，产生 263 行的假 diff 把真实改动埋掉。既有的 register_source.py 和
    # import_boduanzhimen.py 都有同样的问题。
    #
    # 更糟的是并发场景：两个会话同时导入不同来源时，safe_dump 是「读整个 config →
    # 改一处 → 全文覆盖」，后写的会静默丢掉先写的改动。
    #
    # 所以导入器只负责产出自己目录里的东西；全局登记表的字段更新（chunks 数、
    # status）由人按需手改，diff 小、可审阅、不会互相覆盖。
    # 需要机器更新时应该做成「只改目标来源那几行」的定点编辑，而不是整文件 dump。
    write_text_lf(lib / "source.yaml", yaml.safe_dump(source, allow_unicode=True, sort_keys=False))
    print(
        "提示：本导入器不再自动回写 config/sources.yaml（那会抹掉 24 行注释）。\n"
        f"      如需更新登记表，手改 kongkonglong 的 last_import_summary 为："
        f" documents={len(documents)} documents_with_chunks={len(built)}"
        f" chunks={len(chunks)} errors={len(errors)}",
        flush=True,
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
