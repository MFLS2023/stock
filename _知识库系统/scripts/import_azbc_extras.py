#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""爱在冰川「额外资料」导入器：处理 `爱在冰川\\额外资料\\` 下的 9 篇非 .md 素材。

与 import_aizaibingchuan.py 的分工：
主导入器只扫来源目录顶层的 2584 个 .md（作者本人在公众号发的原文），
这里处理三类结构完全不同、且**不是作者原文**的素材：

  1. 干货点合集 7 篇 pdf —— 读者「股不南」从作者历史文章里摘录的合集，有文本层
  2. 高送转战法 1 篇 pdf —— 付费课程讲义，24 页扫描件，无文本层，必须 OCR
  3. 语录感悟 1 篇 docx —— 店铺 STALK 整理的语录汇编

为什么单独标注而不混进正文块：
实测抽 40 条 12 字长句去库里精确匹配，干货点合集命中 11~30 条（合集 5、4 高达 30/40），
说明 5-7 成内容库里已有作者原文。全部标 `chunk_type: curated_digest`，
`author_or_guest` 写实际整理者，检索时能分清「作者原话」与「他人摘录」。

日期来源（都不是发布日，只作排序用）：
  - 干货点 7 篇：文件名前缀（整理者发布日）
  - 高送转战法：pdf 元数据 /CreationDate = 2018-11-09（OCR 首页可见初稿 2017-11-18）
  - 语录感悟：docx 内嵌 core_properties.created = 2023-09-21
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kb_import_utils import (  # noqa: E402
    ROOT,
    infer_topics,
    ocr_images,
    write_jsonl,
    write_text_lf,
)

SOURCE_ID = "aizaibingchuan"
SOURCE_NAME = "爱在冰川"
EXTRAS_ROOT = ROOT / "爱在冰川" / "额外资料"
LIB = ROOT / "_知识库系统" / "source_libraries" / SOURCE_ID

TARGET_CHARS = 1200
MAX_CHUNK_CHARS = 1600
MIN_CHUNK_CHARS = 400
OCR_DPI = 200
# OCR 页要达到这个中文字数才算「有内容」，低于此值的页整页丢掉。
#
# 一度想按「正文页 / 行情截图页」自动分流，**实测不可行，已放弃**：
# 六个候选指标（中文率、数字率、英文率、句读密度、常用字率、最长连续中文串）
# 在两类页面上的取值区间全部重叠，最优组合也只能做到
# 「正文留 6/11、截图误留 3、正文误删 5」。
# 根因是截图页 OCR 出来的并不是垃圾：K 线图批注、行情表里的股票名、评论区
# 截图文字都是真内容（第 4/9/11/19/22 页各认出 244~287 个中文字，常用字率
# 0.54~0.64，与正文页 0.475~0.759 同区间）。
# 所以不分流、全收，靠 confidence=low + chunk_type=course_ocr 交给检索侧取舍。
# 阈值 40 的依据：实测 24 页里 23 页的中文字数在 66~380 之间，只有末页 30 字。
OCR_MIN_CJK = 40

POPPLER = Path(
    r"C:\Users\20577\.cache\codex-runtimes\codex-primary-runtime"
    r"\dependencies\native\poppler\Library\bin\pdftoppm.exe"
)

# 跨文档行级重复度实测出的样板行（不依赖我手写猜测，见 azbc_dump_extras.py）：
# 出现在 >=3 个 pdf 里的 13 条候选，逐条核对后确认全是样板。
BOILERPLATE_PAT = [
    r"^精选留言$",
    r"^暂无\.{2,}$",
    r"^原创\s*股不南\s*$",
    r"^收录于合集\s*#.*\d+\s*个$",
    r"^股票课程微信订阅\s*[VvＶ]\s*[：:]\s*\S+$",
    r"^免责声明[：:]?本文搬自于网络.*$",
    r"^荐[，,]投资有风险[，,]请谨慎操作.*$",
    r"^联系删除[！!]*$",
    r"^如果觉得文章不错[，,]可以动个小手点点赞.*$",
    r"^后续待更\.{2,}$",
    r"^留言区截图[：:]\s*$",
    r"^\s*[）)。，,]\s*$",
    # 语录 docx 的店铺署名
    r"^店铺[：:]\s*STALK\s*资料整理\s*$",
]
BOILERPLATE_RE = re.compile("|".join(BOILERPLATE_PAT))

# 干货点合集的条目分隔：`1，《高中低位股的诠释》` / `12、《xxx》`
ENTRY_RE = re.compile(r"^\s*(\d{1,2})\s*[，,、.．]\s*[《【]?(.{2,60}?)[》】]?\s*$")
# 条目末尾的原文出处：`原文链接： 明天可能是个大面日（2019-1-7 爱在冰川复盘）`
SOURCE_LINK_RE = re.compile(r"^原文链接[：:]\s*(.*)$")
# 出处里的原文日期，用来把摘录挂回作者原文的时间轴
ORIG_DATE_RE = re.compile(r"(20[12]\d)\s*[-./年]\s*(\d{1,2})\s*[-./月]\s*(\d{1,2})")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


HAN_RUN_RE = re.compile(r"[一-鿿]+")


def longest_han_run(text: str) -> int:
    """最长连续中文串。正文句子有 5 个以上连续汉字；
    行情截图 OCR 出来的中文是 1-3 字碎片（`豆门 0@蓬 Ft0 0 亍记一巳`）。"""
    return max((len(m.group(0)) for m in HAN_RUN_RE.finditer(text)), default=0)


def han_ratio(text: str) -> float:
    compact = re.sub(r"\s", "", text)
    if not compact:
        return 0.0
    return sum(1 for ch in compact if "一" <= ch <= "鿿") / len(compact)


def clean_ocr_lines(lines: list[str]) -> list[str]:
    """OCR 专用清洗：删样板行、去掉汉字之间的插空。**不做行级内容过滤。**

    原本想在这里按行剔除截图乱码，实测行不通，原因有两层：

    1. Windows OCR（windows_ocr_batch.ps1）**每页只返回一整行**，没有换行符。
       实测第 1 页 1 行 389 字符、第 12 页 1 行 519 字符，按 2+ 空格切段也只有 1 段。
       所以「行级过滤」的粒度实际等于「整页过滤」，挑不出页内的乱码段落。
    2. 就算按整页判，判据也区分不开 —— 详见 OCR_MIN_CJK 处的说明。
       `longest_han_run` 尤其会被表头连读骗到：第 12 页那串 37 个连续汉字
       是 `前收盘们开盘们最们最低们收盘们成交里成交额`，比正文页的 18 还长。

    页级取舍改由 OCR_MIN_CJK 负责，这里只做无损清洗。
    """
    kept: list[str] = []
    for raw in lines:
        line = raw.replace("　", " ").strip()
        line = re.sub(r"[ \t]{2,}", " ", line)
        if not line or BOILERPLATE_RE.match(line):
            continue
        # OCR 常在汉字之间插空格（`高 送 转` -> `高送转`），这一步是无损的
        kept.append(re.sub(r"(?<=[一-鿿]) +(?=[一-鿿])", "", line))
    return clean_lines(kept)


def clean_lines(lines: list[str]) -> list[str]:
    """删样板行、合并被 pdf 换行切断的句子。pdf 文本层按视觉行断句，
    一句话常被切成 3-4 行，直接切块会让块边界落在句子中间。"""
    kept: list[str] = []
    for raw in lines:
        line = raw.replace("\u3000", " ").strip()
        line = re.sub(r"[ \t]{2,}", " ", line)
        if not line:
            continue
        if BOILERPLATE_RE.match(line):
            continue
        # pdf 会把中文逐字拆开加空格：`来 自 评 论 区` -> `来自评论区`
        if re.fullmatch(r"(?:[一-鿿][ ]){3,}[一-鿿][ ]?[（）()。，,\d\-]*", line):
            line = re.sub(r"(?<=[一-鿿]) (?=[一-鿿])", "", line)
        kept.append(line)

    merged: list[str] = []
    for line in kept:
        if not merged:
            merged.append(line)
            continue
        prev = merged[-1]
        # 上一行没有终止标点、也不是标题/条目行 => 是被切断的句子，接上
        breakable = (
            not re.search(r"[。！？；：!?;:～~）)】》]$", prev)
            and not ENTRY_RE.match(prev)
            and not SOURCE_LINK_RE.match(prev)
            and not ENTRY_RE.match(line)
            and not SOURCE_LINK_RE.match(line)
            and len(prev) >= 12
        )
        if breakable:
            merged[-1] = prev + line
        else:
            merged.append(line)
    return merged


def render_page(pdf: Path, page: int, work: Path) -> Path | None:
    if not POPPLER.exists():
        raise FileNotFoundError(f"pdftoppm 不存在：{POPPLER}")
    stem = work / f"page-{page:03d}"
    result = subprocess.run(
        [str(POPPLER), "-r", str(OCR_DPI), "-png", "-f", str(page), "-l", str(page),
         str(pdf), str(stem)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        return None
    found = sorted(work.glob(f"page-{page:03d}*.png"))
    return found[0] if found else None


def cjk_count(text: str) -> int:
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")


def ocr_pdf(pdf: Path) -> list[dict]:
    """整本 OCR，逐页返回。只丢「几乎没认出字」的页（见 OCR_MIN_CJK），不区分页面类型。"""
    from pypdf import PdfReader

    total = len(PdfReader(str(pdf)).pages)
    work = Path(tempfile.mkdtemp(prefix="azbc-extras-", dir=ROOT / "_知识库系统" / "tmp"))
    units: list[dict] = []
    try:
        batch: list[tuple[str, Path]] = []
        for page in range(1, total + 1):
            image = render_page(pdf, page, work)
            if image is not None:
                batch.append((str(page), image))
        results = ocr_images(batch) if batch else {}
        for page in range(1, total + 1):
            item = results.get(str(page)) or {}
            raw = (item.get("text") or "").strip()
            raw_lines = [x for x in raw.split("\n") if x.strip()]
            lines = clean_ocr_lines(raw_lines)
            body = "\n".join(lines)
            cjk = cjk_count(body)
            usable = cjk >= OCR_MIN_CJK
            units.append({"page": page, "text": body, "cjk": cjk,
                          "raw_lines": len(raw_lines), "kept_lines": len(lines),
                          "usable": usable})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return units


def read_pdf_text(pdf: Path) -> list[list[str]]:
    from pypdf import PdfReader

    pages: list[list[str]] = []
    for page in PdfReader(str(pdf)).pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        pages.append(text.split("\n"))
    return pages


def split_entries(pages: list[list[str]]) -> list[dict]:
    """按 `N，《标题》` 切条目。每条带页码区间和原文出处，locator 才能落到实处。"""
    flat: list[tuple[int, str]] = []
    for page_no, lines in enumerate(pages, start=1):
        for line in clean_lines(lines):
            flat.append((page_no, line))

    entries: list[dict] = []
    current: dict | None = None
    preface: list[tuple[int, str]] = []
    for page_no, line in flat:
        match = ENTRY_RE.match(line)
        if match:
            if current:
                entries.append(current)
            current = {"no": int(match.group(1)), "heading": match.group(2).strip(),
                       "lines": [], "page_start": page_no, "page_end": page_no,
                       "origin": ""}
            continue
        if current is None:
            preface.append((page_no, line))
            continue
        link = SOURCE_LINK_RE.match(line)
        if link:
            current["origin"] = link.group(1).strip()
            current["page_end"] = page_no
            continue
        current["lines"].append(line)
        current["page_end"] = page_no
    if current:
        entries.append(current)

    if preface:
        entries.insert(0, {
            "no": 0, "heading": "前言", "lines": [x for _, x in preface],
            "page_start": preface[0][0], "page_end": preface[-1][0], "origin": "",
        })
    return entries


def pack(units: list[dict], key: str = "text") -> list[list[dict]]:
    """把单元装进 400-1600 字的块，不切断单元自身。"""
    groups: list[list[dict]] = []
    buffer: list[dict] = []
    size = 0
    for unit in units:
        length = len(unit[key])
        if buffer and size + length > TARGET_CHARS:
            groups.append(buffer)
            buffer, size = [], 0
        buffer.append(unit)
        size += length
    if buffer:
        # 末块太短就并回上一组，避免出现几十字的碎块
        if groups and size < MIN_CHUNK_CHARS:
            groups[-1].extend(buffer)
        else:
            groups.append(buffer)
    return groups


def emit(*, doc_id: str, title: str, date: str, author: str, chunk_type: str,
         extraction_method: str, confidence: str, path: Path,
         sections: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    """把 sections 铺成 parents + chunks。字段与主导入器完全一致（18 列），
    否则 build_index.py 灌库时会缺列。"""
    joined = "\n".join(
        u["text"] for sec in sections for u in sec["units"]
    )
    topics = infer_topics(title, joined)
    parents: list[dict] = []
    chunks: list[dict] = []
    for si, sec in enumerate(sections, start=1):
        parent_id = f"{doc_id}-p{si:03d}"
        parent_text = ((sec["heading"] + "\n") if sec.get("heading") else "") + \
            "\n".join(u["text"] for u in sec["units"])
        parents.append({
            "source_id": SOURCE_ID,
            "document_id": doc_id,
            "parent_id": parent_id,
            "title": title,
            "date": date,
            "author_or_guest": author,
            "locator": sec["locator"],
            "text": parent_text,
        })
        for ci, unit in enumerate(sec["units"], start=1):
            text = unit["text"]
            if sec.get("heading") and not text.startswith(sec["heading"]):
                text = f"{sec['heading']}\n{text}"
            chunks.append({
                "source_id": SOURCE_ID,
                "source_name": SOURCE_NAME,
                "document_id": doc_id,
                "parent_id": parent_id,
                "chunk_id": f"{parent_id}-c{ci:02d}",
                "chunk_type": chunk_type,
                "title": title,
                "date": date,
                "author_or_guest": author,
                "topics": topics,
                "claim_type": "opinion_or_case",
                "market_regime": "未标注",
                "locator": unit["locator"],
                "text": text,
                "original_path": str(path),
                "image_path": "",
                "confidence": confidence,
                "extraction_method": extraction_method,
            })
    return parents, chunks, topics


def handle_digest(path: Path) -> dict:
    """干货点合集 7 篇：有文本层，按 `N，《标题》` 切条目。"""
    pages = read_pdf_text(path)
    entries = split_entries(pages)
    date_match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", path.stem)
    date = "-".join(date_match.groups()) if date_match else "2023-01-01"
    title = re.sub(r"^\d{4}-\d{2}-\d{2}\s*", "", path.stem)

    # 条目单元：一条摘录一个单元，带自己的标题、页码、原文出处
    units: list[dict] = []
    for entry in entries:
        body = "\n".join(entry["lines"]).strip()
        if cjk_count(body) < 20:
            continue
        span = (f"第{entry['page_start']}页" if entry["page_start"] == entry["page_end"]
                else f"第{entry['page_start']}页—第{entry['page_end']}页")
        label = f"{entry['no']}，{entry['heading']}" if entry["no"] else entry["heading"]
        # 原文出处带日期，让摘录能挂回作者原文的时间轴（检索到摘录时知道去看哪天原文）
        origin = entry["origin"]
        match = ORIG_DATE_RE.search(origin)
        origin_date = (f"{match.group(1)}-{int(match.group(2)):02d}-"
                       f"{int(match.group(3)):02d}" if match else "")
        text = f"{label}\n{body}"
        if origin:
            text += f"\n（原文出处：{origin}）"
        units.append({"text": text, "label": label, "span": span,
                      "page_start": entry["page_start"], "page_end": entry["page_end"],
                      "origin": origin, "origin_date": origin_date})

    # 相邻条目合并成 400-1600 字的块。实测 110 条摘录里 83% 不足 400 字，
    # 一条一块会重演郁金香「切块过碎」的老问题（当初 6393 块中位 19 字）。
    sections: list[dict] = []
    for group in pack(units):
        first, last = group[0], group[-1]
        page_span = (f"第{first['page_start']}页" if first["page_start"] == last["page_end"]
                     else f"第{first['page_start']}页—第{last['page_end']}页")
        labels = "、".join(u["label"] for u in group)
        locator = f"{page_span}·{labels}" if len(labels) <= 80 else \
            f"{page_span}·{first['label']}等{len(group)}条"
        body = "\n\n".join(u["text"] for u in group)
        sections.append({
            "heading": "", "locator": locator,
            "units": [{"text": part, "locator": locator} for part in _wrap(body)],
            "origin_dates": [u["origin_date"] for u in group if u["origin_date"]],
        })
    return {"date": date, "title": title, "sections": sections,
            "pages": len(pages), "entries": len(units)}


def _wrap(body: str) -> list[str]:
    """条目正文超长时按行切成 <=1600 字的片段，短的原样返回。"""
    if len(body) <= MAX_CHUNK_CHARS:
        return [body]
    out: list[str] = []
    buffer: list[str] = []
    size = 0
    for line in body.split("\n"):
        if buffer and size + len(line) > TARGET_CHARS:
            out.append("\n".join(buffer))
            buffer, size = [], 0
        buffer.append(line)
        size += len(line)
    if buffer:
        if out and size < MIN_CHUNK_CHARS:
            out[-1] = out[-1] + "\n" + "\n".join(buffer)
        else:
            out.append("\n".join(buffer))
    return out


def handle_ocr_pdf(path: Path) -> dict:
    """高送转战法：24 页扫描件，无文本层，整本 OCR。

    24 页全收，只丢「OCR 几乎没认出字」的页（OCR_MIN_CJK，实测只有末页触发）。
    不区分正文页与行情截图页 —— 试过六个指标都分不开，且截图页认出的
    批注/股票名/评论文字本身就是内容。丢掉的页号写进报告备查。"""
    units = ocr_pdf(path)
    usable = [u for u in units if u["usable"]]
    dropped = [u["page"] for u in units if not u["usable"]]
    sections: list[dict] = []
    for group in pack(usable):
        first, last = group[0]["page"], group[-1]["page"]
        span = f"第{first}页" if first == last else f"第{first}页—第{last}页"
        body = "\n".join(u["text"] for u in group)
        sections.append({
            "heading": "",
            "locator": span,
            "units": [{"text": part, "locator": span} for part in _wrap(body)],
        })
    return {"date": "2018-11-09", "title": "爱在冰川高送转投机战法（付费课程讲义）",
            "sections": sections, "pages": len(units),
            "ocr_dropped_pages": dropped, "ocr_usable_pages": len(usable)}


def handle_docx(path: Path) -> dict:
    """语录感悟：docx，133 段短语录。按段装块，不按标题切（原文没有标题层级）。"""
    import docx

    document = docx.Document(str(path))
    paragraphs = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    kept: list[dict] = []
    for index, text in enumerate(paragraphs, start=1):
        if BOILERPLATE_RE.match(text):
            continue
        kept.append({"text": text, "index": index})

    sections: list[dict] = []
    for group in pack(kept):
        first, last = group[0]["index"], group[-1]["index"]
        span = (f"第{first}段" if first == last else f"第{first}段—第{last}段")
        body = "\n".join(u["text"] for u in group)
        sections.append({"heading": "", "locator": span,
                         "units": [{"text": part, "locator": span}
                                   for part in _wrap(body)]})
    created = getattr(document.core_properties, "created", None)
    date = created.strftime("%Y-%m-%d") if created else "2023-09-21"
    return {"date": date, "title": "爱在冰川语录感悟（STALK 整理）",
            "sections": sections, "paragraphs": len(paragraphs)}


# 三类素材的处理方式、整理者署名与可信度。
# author_or_guest 不写「爱在冰川」——这些不是作者本人发的内容，写成作者名
# 会让检索结果误认为是导师原话。
PLAN = {
    "爱在冰川干货文章点集": {
        "handler": handle_digest,
        "author": "股不南（读者摘录，非作者原文）",
        "chunk_type": "curated_digest",
        "extraction_method": "pdf_text_layer",
        "confidence": "medium",
        "risk_flags": "第三方摘录，非作者原文；与库内原文有 3-8 成重复；截图未OCR",
    },
    "爱在冰川付费课程高送转战法": {
        "handler": handle_ocr_pdf,
        "author": "爱在冰川（课程讲义，OCR）",
        "chunk_type": "course_ocr",
        "extraction_method": "pdftoppm+windows_ocr",
        "confidence": "low",
        "risk_flags": "扫描件OCR，误识多；含行情截图页，图内文字与乱码混杂未剔除；"
                      "数字（价格、日期、涨幅）尤其不可信；任何结论必须回看原页",
    },
    "爱在冰川语录及感悟-整理店铺STALK资料": {
        "handler": handle_docx,
        "author": "STALK 整理（非作者原文排版）",
        "chunk_type": "curated_digest",
        "extraction_method": "docx_paragraphs",
        "confidence": "medium",
        "risk_flags": "第三方整理的语录汇编，无原文出处，不可作为定论引用",
    },
}

# 本导入器产出的 extraction_method 集合，用于重跑时先剔除自己的旧行（幂等）
OWN_METHODS = {plan["extraction_method"] for plan in PLAN.values()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="只统计不落盘，先看抽取结果是否合理")
    parser.add_argument("--only", default="",
                        help="只处理文件名含该子串的素材，用于单篇调试")
    args = parser.parse_args()

    if not EXTRAS_ROOT.exists():
        raise SystemExit(f"额外资料目录不存在：{EXTRAS_ROOT}")

    files: list[tuple[Path, dict]] = []
    for group, plan in PLAN.items():
        folder = EXTRAS_ROOT / group
        if not folder.exists():
            print(f"  跳过（目录不存在）：{group}")
            continue
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() not in {".pdf", ".docx"}:
                continue
            if args.only and args.only not in path.name:
                continue
            files.append((path, plan))

    if not files:
        raise SystemExit("没有待处理素材")

    print(f"额外资料目录 {EXTRAS_ROOT}")
    print(f"待处理 {len(files)} 篇")
    print("-" * 62)

    doc_rows: list[dict] = []
    parent_rows: list[dict] = []
    chunk_rows: list[dict] = []
    topic_counter: Counter = Counter()
    notes: list[dict] = []

    for path, plan in files:
        digest = sha256_file(path)
        doc_id = f"azbcx-{digest[:12]}"
        result = plan["handler"](path)
        sections = result["sections"]
        if not sections:
            print(f"  无可用内容，跳过：{path.name}")
            notes.append({"file": path.name, "note": "无可用内容"})
            continue

        parents, chunks, topics = emit(
            doc_id=doc_id, title=result["title"], date=result["date"],
            author=plan["author"], chunk_type=plan["chunk_type"],
            extraction_method=plan["extraction_method"],
            confidence=plan["confidence"], path=path, sections=sections,
        )
        parent_rows.extend(parents)
        chunk_rows.extend(chunks)
        topic_counter.update(topics)

        chars = sum(len(row["text"]) for row in chunks)
        archive = "\n\n".join(
            ((sec["heading"] + "\n") if sec.get("heading") else "")
            + "\n".join(u["text"] for u in sec["units"])
            for sec in sections
        )
        text_path = LIB / "texts" / f"{doc_id}.txt"
        if not args.dry_run:
            (LIB / "texts").mkdir(parents=True, exist_ok=True)
            write_text_lf(text_path, archive + "\n")

        doc_rows.append({
            "source_id": SOURCE_ID,
            "document_id": doc_id,
            "title": result["title"],
            "date": result["date"],
            "author_or_guest": plan["author"],
            "content_type": plan["chunk_type"],
            "topics": topics,
            "characters": len(archive),
            "author_reply_count": 0,
            "author_reply_characters": 0,
            "unit_count": len(chunks),
            "original_path": str(path),
            "related_original_paths": [],
            "normalized_text_path": str(text_path),
            "sha256": digest,
            "risk_flags": plan["risk_flags"],
        })

        extra = ""
        if "ocr_dropped_pages" in result:
            extra = (f"  OCR 可用 {result['ocr_usable_pages']}/{result['pages']} 页，"
                     f"剔除图表页 {len(result['ocr_dropped_pages'])}")
        elif "entries" in result:
            extra = f"  条目 {result['entries']} 个 / {result['pages']} 页"
        elif "paragraphs" in result:
            extra = f"  段落 {result['paragraphs']} 个"
        print(f"  {path.name[:46]:<48} {len(chunks):>3} 块 {chars:>7,} 字符{extra}")
        notes.append({"file": path.name, "chunks": len(chunks), "chars": chars,
                      "detail": extra.strip()})

    print("-" * 62)
    total = sum(len(row["text"]) for row in chunk_rows)
    lengths = sorted(len(row["text"]) for row in chunk_rows)
    print(f"documents {len(doc_rows)}  parents {len(parent_rows)}  chunks {len(chunk_rows)}")
    print(f"块字符合计 {total:,}  中位 {lengths[len(lengths)//2] if lengths else 0}  "
          f"区间 {lengths[0] if lengths else 0}–{lengths[-1] if lengths else 0}")
    print(f"块类型 {dict(Counter(r['chunk_type'] for r in chunk_rows))}")
    print(f"高频主题 {topic_counter.most_common(8)}")

    if args.dry_run:
        print("\n--dry-run：未写入任何文件")
        return 0

    # 与主导入器的产物合并。先剔除本导入器上次写入的行，保证重跑幂等。
    merged = {}
    for name, new_rows, key in (
        ("documents.jsonl", doc_rows, "document_id"),
        ("parents.jsonl", parent_rows, "parent_id"),
        ("chunks.jsonl", chunk_rows, "chunk_id"),
    ):
        path = LIB / name
        old: list[dict] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("extraction_method") in OWN_METHODS:
                    continue                      # chunks：按抽取方式识别自己的旧行
                if str(row.get(key, "")).startswith("azbcx-"):
                    continue                      # documents/parents：按 id 前缀识别
                old.append(row)
        combined = old + new_rows
        write_jsonl(path, combined)
        merged[name] = (len(old), len(new_rows), len(combined))

    print()
    for name, (old_n, new_n, all_n) in merged.items():
        print(f"  {name:<18} 原有 {old_n:>5} + 新增 {new_n:>4} = {all_n:>5}")

    summary_path = LIB / "extras_summary.json"
    write_text_lf(summary_path, json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_root": str(EXTRAS_ROOT),
        "files": len(files),
        "documents": len(doc_rows),
        "parents": len(parent_rows),
        "chunks": len(chunk_rows),
        "chunk_characters": total,
        "chunk_types": dict(Counter(r["chunk_type"] for r in chunk_rows)),
        "confidence": dict(Counter(r["confidence"] for r in chunk_rows)),
        "per_file": notes,
    }, ensure_ascii=False, indent=2) + "\n")
    print(f"\n统计写入 {summary_path}")

    # source_summary.json 由主导入器（import_aizaibingchuan.py）写，只统计 .md 那批，
    # 加上额外资料后它的 documents/chunks 会偏小。这里把合并后的总数补进去，
    # 免得后来的人读到 2457/5633 以为就是全部。
    #
    # 注意主导入器是整文件覆盖写 jsonl 的，所以重跑 import_aizaibingchuan.py 会抹掉
    # 这 49 行，必须紧接着再跑一次本脚本。这个 marker 就是提醒。
    main_summary = LIB / "source_summary.json"
    if main_summary.exists():
        data = json.loads(main_summary.read_text(encoding="utf-8"))
        data["extras_included"] = {
            "note": "以下总数含 import_azbc_extras.py 导入的额外资料；"
                    "重跑 import_aizaibingchuan.py 会覆盖 jsonl 并抹掉这批，需再跑一次额外资料导入器",
            "extras_documents": len(doc_rows),
            "extras_parents": len(parent_rows),
            "extras_chunks": len(chunk_rows),
            "total_documents": merged["documents.jsonl"][2],
            "total_parents": merged["parents.jsonl"][2],
            "total_chunks": merged["chunks.jsonl"][2],
        }
        write_text_lf(main_summary,
                      json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        print(f"总数补记 {main_summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
