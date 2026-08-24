# -*- coding: utf-8 -*-
"""生产素材上的 OCR 引擎 A/B：Windows OCR vs RapidOCR。

_ground-truth 基准（_bench_ocr.py，南京路文本层页）已经证明 RapidOCR 还原率
92% vs Windows 40%；本脚本在**没有 ground truth 的真实图层**上对比：
K 线图（波段之门）、截图（郁金香，含生产同款 2000px 放大）、扫描讲义页
（爱在冰川课程 PDF）。指标用「孤立部首字符坏块率」——拆字是两引擎共同的
失败模式，部首密度能横向比出谁拆得少。输出逐张落盘供人工抽读。
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kb_import_utils import ROOT, clean_text, ocr_images  # noqa: E402
from _bench_ocr import POPPLER, WORK, render, run_rapid  # noqa: E402

SYSTEM_PY = Path(r"C:\Users\20577\AppData\Local\Programs\Python\Python312\python.exe")

# 与体检报告同一判据：孤立部首/偏旁字符 ≥2 记疑似坏块
RAD_RE = re.compile("[忄亻讠钅纟饣彳氵灬扌宀辶阝卩刂疒礻衤犭罒勹廾匚冂丬亠乛攵殳爫虍髟鬲黾巛彐尢屮乚]")


def collect_bdzm(n: int) -> list[Path]:
    map_path = ROOT / "_知识库系统/source_libraries/boduanzhimen/maps/image_map.jsonl"
    rows = []
    for line in map_path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        if item.get("kind") == "chart":
            path = Path(item["file"])
            if path.exists():
                rows.append(path)
    step = max(1, len(rows) // n)
    return rows[::step][:n]


def collect_tulip(n: int) -> list[Path]:
    import random

    cache_dir = ROOT / "_知识库系统/source_libraries/tulip_garden/image_ocr_cache"
    paths = []
    for f in cache_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        p = Path(data.get("source_path", ""))
        if p.exists():
            paths.append(p)
    random.Random(42).shuffle(paths)
    return paths[:n]


def collect_course(pages: list[int]) -> list[Path]:
    pdfs = list((ROOT / "爱在冰川" / "额外资料").rglob("*.pdf"))
    if not pdfs:
        raise SystemExit("找不到课程讲义 PDF")
    out = []
    for page in pages:
        out.append(render(pdfs[0], page, 200, WORK / f"course-p{page:02d}.png"))
    return out


def upscale_tulip(images: list[Path]) -> dict[Path, Path]:
    """复刻郁金香生产的预处理：<2000px 的图 LANCZOS 放大到 2000。"""
    from PIL import Image

    mapping = {}
    for p in images:
        with Image.open(p) as im:
            if im.width >= 2000:
                mapping[p] = p
                continue
            ratio = 2000 / im.width
            big = im.resize((2000, round(im.height * ratio)), Image.LANCZOS)
        tmp = WORK / "tulip_scaled" / p.name
        tmp.parent.mkdir(parents=True, exist_ok=True)
        big.convert("RGB").save(tmp)
        mapping[p] = tmp
    return mapping


def rapid_ocr_images(items, *, max_dimension: int = 9000, overlap: int = 120):
    """与 kb_import_utils.ocr_images 完全同构的预处理（EXIF 转正、宽封顶、竖向分片、
    重叠合并去重），引擎换成 RapidOCR。第一轮 A/B 把整张长图直接喂 RapidOCR，
    超长手机截图没分片导致它整图失败——那轮郁金香数据不公平，这轮修正。
    """
    from PIL import Image, ImageOps
    from kb_import_utils import _merge_tile_texts, _tile_boxes

    import numpy as np
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    results = {}
    for key, image_path in items:
        try:
            with Image.open(image_path) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGB")
                if image.width > max_dimension:
                    ratio = max_dimension / image.width
                    image = image.resize((max_dimension, max(1, int(image.height * ratio))))
                texts = []
                for box in _tile_boxes(image.width, image.height, max_dimension, overlap):
                    crop = image.crop(box)
                    result, _ = engine(np.asarray(crop))
                    lines = [line[1] for line in (result or [])]
                    texts.append("\n".join(lines))
            results[key] = {"text": _merge_tile_texts(texts), "error": "", "tiles": len(texts)}
        except Exception as exc:
            results[key] = {"text": "", "error": f"prepare_failed: {exc}", "tiles": 0}
    return results


def stats_for(texts: dict[str, str]) -> dict:
    n_bad = 0
    chars_total = 0
    cjk_total = 0
    for text in texts.values():
        cleaned = clean_text(text, ocr=True)
        cjk = len(re.sub(r"[^\u4e00-\u9fff]", "", cleaned))
        chars_total += len(cleaned)
        cjk_total += cjk
        if len(re.findall(RAD_RE, cleaned)) >= 2:
            n_bad += 1
    return {
        "bad_rate": n_bad / max(len(texts), 1),
        "chars_avg": chars_total // max(len(texts), 1),
        "cjk_ratio": cjk_total / max(chars_total, 1),
    }


def main() -> int:
    if "--retulip" in sys.argv:
        # 第二轮：郁金香组公平复测（两引擎都走生产同款分片）
        WORK.mkdir(parents=True, exist_ok=True)
        images = upscale_tulip(collect_tulip(30))
        images = [images[p] for p in images]
        t0 = time.perf_counter()
        win = ocr_images([(f"{i}", p) for i, p in enumerate(images)])
        win_cost = time.perf_counter() - t0
        t1 = time.perf_counter()
        rapid = rapid_ocr_images([(f"{i}", p) for i, p in enumerate(images)])
        rapid_cost = time.perf_counter() - t1
        win_texts = {str(i): clean_text(win.get(f"{i}", {}).get("text", ""), ocr=True)
                     for i in range(len(images))}
        rapid_texts = {str(i): clean_text(rapid.get(f"{i}", {}).get("text", ""), ocr=True)
                       for i in range(len(images))}
        s_win, s_rapid = stats_for(win_texts), stats_for(rapid_texts)
        print("=== tulip_shot 公平复测（同款分片）===")
        print(f"  windows: 坏块率 {s_win['bad_rate']:.0%}  字/图 {s_win['chars_avg']}  "
              f"CJK率 {s_win['cjk_ratio']:.2f}  {win_cost / len(images):.2f}s/图")
        print(f"  rapid  : 坏块率 {s_rapid['bad_rate']:.0%}  字/图 {s_rapid['chars_avg']}  "
              f"CJK率 {s_rapid['cjk_ratio']:.2f}  {rapid_cost / len(images):.2f}s/图")
        out_dir = WORK / "ab_outputs" / "tulip_tiled"
        out_dir.mkdir(parents=True, exist_ok=True)
        for i, p in enumerate(images):
            stem = re.sub(r"[^\w.-]", "_", Path(p).stem)[:60]
            (out_dir / f"{i:02d}-{stem}.win.txt").write_text(win_texts[str(i)], encoding="utf-8")
            (out_dir / f"{i:02d}-{stem}.rapid.txt").write_text(rapid_texts[str(i)], encoding="utf-8")
        return 0

    WORK.mkdir(parents=True, exist_ok=True)
    groups: dict[str, list[Path]] = {
        "bdzm_chart": collect_bdzm(30),
        "tulip_shot": collect_tulip(30),
        "azbc_course": collect_course([2, 7, 10, 13]),
    }
    tulip_scaled = upscale_tulip(groups["tulip_shot"])
    groups["tulip_shot"] = [tulip_scaled[p] for p in groups["tulip_shot"]]

    results = {}
    for group, images in groups.items():
        print(f"\n=== {group}（{len(images)} 张）===", flush=True)

        t0 = time.perf_counter()
        win = ocr_images([(f"{i}", p) for i, p in enumerate(images)])
        win_cost = time.perf_counter() - t0
        win_texts = {str(i): clean_text(win.get(f"{i}", {}).get("text", ""), ocr=True)
                     for i in range(len(images))}

        t1 = time.perf_counter()
        rapid_raw = run_rapid(images)
        rapid_cost = time.perf_counter() - t1
        rapid_texts = {str(Path(p)): clean_text(rapid_raw.get(str(p), ""), ocr=True)
                       for p in images}

        s_win = stats_for(win_texts)
        s_rapid = stats_for(rapid_texts)
        results[group] = {
            "windows": {**s_win, "sec_per_img": round(win_cost / len(images), 2)},
            "rapid": {**s_rapid, "sec_per_img": round(rapid_cost / len(images), 2)},
        }
        print(f"  windows: 坏块率 {s_win['bad_rate']:.0%}  字/图 {s_win['chars_avg']}  "
              f"CJK率 {s_win['cjk_ratio']:.2f}  {results[group]['windows']['sec_per_img']}s/图")
        print(f"  rapid  : 坏块率 {s_rapid['bad_rate']:.0%}  字/图 {s_rapid['chars_avg']}  "
              f"CJK率 {s_rapid['cjk_ratio']:.2f}  {results[group]['rapid']['sec_per_img']}s/图")

        out_dir = WORK / "ab_outputs" / group
        out_dir.mkdir(parents=True, exist_ok=True)
        for i, p in enumerate(images):
            stem = re.sub(r"[^\w.-]", "_", p.stem)[:60]
            (out_dir / f"{i:02d}-{stem}.win.txt").write_text(win_texts[str(i)], encoding="utf-8")
            (out_dir / f"{i:02d}-{stem}.rapid.txt").write_text(rapid_texts[str(p)], encoding="utf-8")
        (out_dir / "images.json").write_text(
            json.dumps({str(i): str(p) for i, p in enumerate(images)}, ensure_ascii=False, indent=1),
            encoding="utf-8")

    summary = WORK / "ab_results.json"
    summary.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n汇总 -> {summary}\n逐张输出 -> {WORK / 'ab_outputs'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
