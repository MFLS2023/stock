#!/usr/bin/env python3
"""爱在冰川图文映射生成器 —— 产出「正文第几段之后挂着哪张图」。

背景：公众号 .md 里的图片是远程链接，导入器在清洗时整体删除
（import_aizaibingchuan.py 的 clean_wechat_markdown），所以入库正文里
图片位置没留任何痕迹。而作者大量用「上图/下图/图中/箭头」指代图片
（全库 860 处指代，389 块含指代），读者看到这些词却拿不到图。

本脚本把 wechatDownload 下载的本地图片与正文段号对齐，输出 image_map.json，
由导入器读取后填进每个 chunk 的 image_path 字段。

只读原始资料与下载目录，只写 image_map.json。

用法：
    python build_azbc_image_map.py            # 生成
    python build_azbc_image_map.py --verify   # 生成并做指代命中率检验
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import importlib.util
import json
import pathlib
import random
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "爱在冰川"
LIB = ROOT / "_知识库系统" / "source_libraries" / "aizaibingchuan"
OUT = LIB / "image_map.json"

# wechatDownload 的下载目录。图片是远程链接，只有这里有本地副本。
DOWNLOAD_ROOT = pathlib.Path(
    r"C:/Users/20577/Neverflandre/wechatDownload4.6/下载/爱在冰川"
)
IMG_ROOT = DOWNLOAD_ROOT / "图片"
COVER_ROOT = DOWNLOAD_ROOT / "封面"

# 一张图在全库重复出现超过此次数，判定为模板/页脚（关注图、二维码）而非内容
TEMPLATE_MIN_REPEAT = 10

IMG_LINK = re.compile(r"!\[[^\]]*\]\((http[^)]*)\)")
CONTENT_HOST = "mmbiz.qpic.cn"
FNAME = re.compile(r"^\[(\d{4}-\d{2}-\d{2})-(\d{4})\]_(.+)_(\d+)\.(\w+)$")
MDNAME = re.compile(r"^\[(\d{4}-\d{2}-\d{2})-(\d{4})\](.+)\.md$")
CJK = re.compile(r"[\u4e00-\u9fff]")

# \u4f5c\u8005\u6307\u4ee3\u56fe\u7247\u7684\u8bf4\u6cd5\uff0c\u7528\u4e8e --verify \u7684\u547d\u4e2d\u7387\u68c0\u9a8c
IMAGE_REFS = ("\u4e0a\u56fe", "\u4e0b\u56fe", "\u8fd9\u4e2a\u56fe", "\u56fe\u4e2d", "\u5982\u56fe", "\u89c1\u56fe", "\u4e0a\u9762\u8fd9\u4e2a\u56fe", "\u7bad\u5934", "\u7ea2\u6846")


def load_importer():
    """\u76f4\u63a5\u590d\u7528\u5bfc\u5165\u5668\u7684\u6e05\u6d17\u51fd\u6570\uff0c\u4fdd\u8bc1\u6bb5\u53f7\u4e0e\u5165\u5e93 chunks \u5b8c\u5168\u540c\u6e90\u3002

    \u8fd9\u4e00\u70b9\u662f\u786c\u8981\u6c42\uff1a\u81ea\u5df1\u91cd\u5199\u4e00\u4efd\u6e05\u6d17\u903b\u8f91\uff0c\u6bb5\u53f7\u5c31\u4f1a\u4e0e locator \u5bf9\u4e0d\u4e0a\uff0c
    \u6620\u5c04\u4f1a\u6307\u5411\u9519\u8bef\u7684\u6bb5\u843d\u3002
    """
    path = pathlib.Path(__file__).with_name("import_aizaibingchuan.py")
    spec = importlib.util.spec_from_file_location("azbc_importer_for_map", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def md5_file(path: pathlib.Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def final_paragraphs(imp, raw: str, title: str) -> list[str]:
    """\u5bfc\u5165\u5668\u539f\u6837\u7ba1\u7ebf\uff0c\u5f97\u5230\u6700\u7ec8\u53c2\u4e0e\u6bb5\u53f7\u8ba1\u7b97\u7684\u6bb5\u843d\u8868\u3002"""
    cleaned = imp.clean_wechat_markdown(raw)
    body, _ = imp.split_body_and_comments(cleaned)
    body = imp.strip_leading_title(body, title)
    return [p.strip() for p in body.split("\n") if p.strip()]


def paragraph_offsets(raw: str, paras: list[str]) -> list[int | None]:
    """\u628a\u6bcf\u4e2a\u6bb5\u843d\u56de\u6eaf\u5230\u539f\u6587\u5b57\u7b26\u504f\u79fb\u3002

    \u4e3a\u4ec0\u4e48\u4e0d\u5728\u6b63\u6587\u91cc\u63d2\u5360\u4f4d\u7b26\u518d\u8dd1\u7ba1\u7ebf\uff1a\u5b9e\u6d4b\u90a3\u6837\u4f1a\u6539\u53d8\u7ba1\u7ebf\u884c\u4e3a
    \uff08\u7eaf\u56fe\u7247\u884c\u7531\u7a7a\u884c\u53d8\u6210\u975e\u7a7a\u884c\uff0cstrip_leading_title / split_body_and_comments
    \u7684\u5224\u65ad\u968f\u4e4b\u6539\u53d8\uff0c1154 \u7bc7\u6bb5\u53f7\u6f02\u79fb\uff09\u3002\u6240\u4ee5\u7ba1\u7ebf\u539f\u6837\u8dd1\uff0c\u4f4d\u7f6e\u53e6\u7b97\u3002

    \u7528\u6c49\u5b57\u7279\u5f81\u4e32\u5b9a\u4f4d\uff0c\u56e0\u4e3a\u6e05\u6d17\u53ea\u5220\u7b26\u53f7\u4e0e\u94fe\u63a5\uff0c\u6c49\u5b57\u5e8f\u5217\u4fdd\u6301\u4e0d\u53d8\u3002
    \u6e38\u6807\u53ea\u524d\u8fdb\u4e0d\u56de\u5934\uff0c\u907f\u514d\u540c\u4e00\u53e5\u8bdd\u5728\u6587\u4e2d\u91cd\u590d\u51fa\u73b0\u65f6\u9519\u914d\u3002
    """
    positions = [m.start() for m in CJK.finditer(raw)]
    haystack = "".join(CJK.findall(raw))

    offsets: list[int | None] = []
    cursor = 0
    for para in paras:
        chars = CJK.findall(para)
        key = "".join(chars[:12])
        if not key:
            offsets.append(None)
            continue
        found = haystack.find(key, cursor)
        if found < 0 and len(key) > 6:
            found = haystack.find(key[:6], cursor)
        if found < 0:
            offsets.append(None)
        else:
            offsets.append(positions[found])
            cursor = found + 1
    return offsets


def scan_disk() -> tuple[dict, set[str], collections.Counter]:
    """扫描下载目录，返回 (按文章归组的文件清单, 封面指纹集, 全库指纹频次)。

    图片子目录是按「标题」建的而不是按文章：作者的周末系列每周同名，
    实测「肘墨鱼块」一个目录里 1354 张图跨 142 个日期。所以归组必须用
    文件名里的日期+时间，不能用目录名。
    """
    cover_hashes = {md5_file(p) for p in COVER_ROOT.rglob("*") if p.is_file()}

    entries: list[dict] = []
    for folder in sorted(IMG_ROOT.iterdir()):
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if not path.is_file():
                continue
            m = FNAME.match(path.name)
            if not m:
                continue
            entries.append(
                {
                    "key": (m.group(1), m.group(2), m.group(3)),
                    "order": int(m.group(4)),
                    "name": path.name,
                    "folder": folder.name,
                    "md5": md5_file(path),
                }
            )

    freq = collections.Counter(e["md5"] for e in entries)
    grouped: dict[tuple, list[dict]] = collections.defaultdict(list)
    for entry in entries:
        grouped[entry["key"]].append(entry)
    for items in grouped.values():
        items.sort(key=lambda e: e["order"])
    return grouped, cover_hashes, freq


def classify(entry: dict, cover_hashes: set[str], freq: collections.Counter) -> str:
    if entry["md5"] in cover_hashes:
        return "cover"
    if freq[entry["md5"]] >= TEMPLATE_MIN_REPEAT:
        return "template"
    if freq[entry["md5"]] > 1:
        return "duplicate"
    return "content"


def build() -> list[dict]:
    imp = load_importer()
    grouped, cover_hashes, freq = scan_disk()

    stats = collections.Counter()
    mapping: list[dict] = []

    for md in sorted(SOURCE_ROOT.glob("*.md")):
        m = MDNAME.match(md.name)
        if not m:
            stats["文件名不合格式"] += 1
            continue
        date, time_, title = m.group(1), m.group(2), m.group(3)
        key = (date, time_, title)
        if key not in grouped:
            stats["下载目录无对应图"] += 1
            continue

        raw = md.read_text(encoding="utf-8", errors="replace")
        links = [
            mm.start()
            for mm in IMG_LINK.finditer(raw)
            if CONTENT_HOST in (mm.group(1) or "")
        ]
        files = [
            e for e in grouped[key]
            if classify(e, cover_hashes, freq) == "content"
        ]
        if not links or not files:
            stats["无内容图"] += 1
            continue

        # 正文链接序列与磁盘文件序列对齐。
        # 首选：跳过开头的封面张数（可推导）；退化：尾部对齐（按数量猜）
        covers = sum(
            1 for e in grouped[key]
            if classify(e, cover_hashes, freq) == "cover"
        )
        if len(links) - covers == len(files):
            aligned, align_kind = links[covers:], "cover_offset"
        elif len(links) >= len(files):
            aligned, align_kind = links[len(links) - len(files):], "tail"
        else:
            stats["链接少于文件"] += 1
            continue
        stats[align_kind] += 1

        paras = final_paragraphs(imp, raw, title)
        if not paras:
            stats["无段落"] += 1
            continue
        offsets = paragraph_offsets(raw, paras)
        located = [(i + 1, off) for i, off in enumerate(offsets) if off is not None]

        images = []
        for ordinal, (img_offset, entry) in enumerate(zip(aligned, files), start=1):
            after = 0
            for para_no, para_offset in located:
                if para_offset <= img_offset:
                    after = para_no
                else:
                    break
            images.append(
                {
                    "ordinal": ordinal,
                    "after_paragraph": after,
                    "file": entry["name"],
                    "folder": entry["folder"],
                }
            )

        mapping.append(
            {
                "md": md.name,
                "date": date,
                "title": title,
                "paragraphs": len(paras),
                "paragraphs_located": len(located),
                "align_kind": align_kind,
                "images": images,
            }
        )
        stats["已建映射"] += 1

    print("【处理统计】")
    for k, v in stats.most_common():
        print(f"    {k:<20} {v:>6}")
    total = sum(len(x["images"]) for x in mapping)
    print(f"\n共 {len(mapping)} 篇、{total} 张图")
    return mapping


def verify(mapping: list[dict]) -> None:
    """用作者自己的话当标准答案，带对照组。

    作者写「见下图」的那一段，紧邻位置就该有图；不含任何指代的普通段落
    则不该有。两组命中率若无差异，说明映射位置是随机的，等于没建。
    """
    imp = load_importer()
    ref_hit = ref_total = 0
    control: list[bool] = []
    by_align: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    samples: list[dict] = []

    for entry in mapping:
        md = SOURCE_ROOT / entry["md"]
        if not md.exists():
            continue
        raw = md.read_text(encoding="utf-8", errors="replace")
        paras = final_paragraphs(imp, raw, entry["title"])
        if not paras:
            continue
        after = collections.Counter(img["after_paragraph"] for img in entry["images"])

        for index, para in enumerate(paras, start=1):
            near = after.get(index, 0) > 0 or after.get(index - 1, 0) > 0
            hit_word = next((w for w in IMAGE_REFS if w in para), None)
            if hit_word:
                ref_total += 1
                by_align[entry["align_kind"]][1] += 1
                if near:
                    ref_hit += 1
                    by_align[entry["align_kind"]][0] += 1
                    if len(samples) < 5:
                        pics = [
                            x["file"] for x in entry["images"]
                            if x["after_paragraph"] in (index, index - 1)
                        ]
                        samples.append(
                            {
                                "md": entry["md"],
                                "para": index,
                                "word": hit_word,
                                "text": re.sub(r"\s+", " ", para)[:70],
                                "file": pics[0] if pics else "",
                            }
                        )
            else:
                control.append(near)

    if not ref_total:
        print("\n没有指代段可检验")
        return

    random.seed(3)
    sample = random.sample(control, min(len(control), ref_total))
    ctrl_hit = sum(1 for x in sample if x)

    print("\n" + "=" * 60)
    print("【检验：作者说「图」的地方，映射是否真的有图】\n")
    print(f"  指代段  {ref_total:>6} 段，紧邻有图 {ref_hit:>6} -> {ref_hit / ref_total * 100:.1f}%")
    print(f"  对照组  {len(sample):>6} 段，紧邻有图 {ctrl_hit:>6} -> {ctrl_hit / len(sample) * 100:.1f}%")
    if ctrl_hit:
        lift = (ref_hit / ref_total) / (ctrl_hit / len(sample))
        print(f"\n  提升倍数 {lift:.2f}x", end="  ")
        print("=> 映射位置与作者叙述一致" if lift >= 1.15 else "=> 与随机无异，映射不可信")

    print("\n【两种对齐策略】")
    for kind in ("cover_offset", "tail"):
        hit, total = by_align[kind]
        if total:
            label = "封面偏移(可推导)" if kind == "cover_offset" else "尾部对齐(按数量猜)"
            print(f"  {label:<20} 指代段 {total:>5}  命中率 {hit / total * 100:>5.1f}%")

    print("\n【命中样例】")
    for s in samples:
        print(f"  {s['md'][:48]}")
        print(f"     第{s['para']}段（「{s['word']}」）：{s['text']}")
        print(f"     -> {s['file'][-40:]}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="生成后做指代命中率检验")
    args = parser.parse_args()

    if not IMG_ROOT.exists():
        print(f"下载目录不存在：{IMG_ROOT}", file=sys.stderr)
        return 1

    mapping = build()
    LIB.mkdir(parents=True, exist_ok=True)
    # 一行一篇文章：indent 会让 2529 篇膨胀到 16 万行，git diff 完全不可读。
    # 这个格式下每篇是一行，改一篇只脏一行。
    lines = [json.dumps(row, ensure_ascii=False, sort_keys=True) for row in mapping]
    OUT.write_text("[\n" + ",\n".join(lines) + "\n]\n", encoding="utf-8")
    print(f"映射表 -> {OUT}")

    if args.verify:
        verify(mapping)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
