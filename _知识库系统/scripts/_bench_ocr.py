#!/usr/bin/env python3
"""OCR 引擎与 DPI 的对照基准。

为什么能量化：南京路这批 PDF 是「文本层正文 + 嵌入截图」的混合页，
文本层完好的页面自带 ground truth —— OCR 也扫了同一张整页图，
所以「文本层的 n-gram 有多少能在 OCR 输出里原样找到」就是还原率。
截图部分没有 ground truth，但两个引擎在同一页正文上的表现差异
可以外推到截图部分（同一张图、同样的字号分布）。

指标用 n-gram 命中率而不是编辑距离：OCR 输出是「正文 + 截图」的混合，
整体编辑距离会被截图内容拉低，没有可比性；而 n-gram 只问
「这句话在输出里出现了吗」，不受额外内容干扰。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kb_import_utils import ROOT, clean_text, ocr_images  # noqa: E402

POPPLER = Path(
    r"C:\Users\20577\.cache\codex-runtimes\codex-primary-runtime"
    r"\dependencies\native\poppler\Library\bin\pdftoppm.exe"
)
SYSTEM_PYTHON = Path(r"C:\Users\20577\AppData\Local\Programs\Python\Python312\python.exe")
WORK = ROOT / "_知识库系统" / "tmp" / "ocr_bench"


def render(pdf: Path, page: int, dpi: int, out: Path) -> Path:
    """渲染单页为 PNG，与 import_nanjinglu.render_pdf_page 同参数。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    prefix = out.with_suffix("")
    completed = subprocess.run(
        [str(POPPLER), "-png", "-r", str(dpi), "-f", str(page), "-l", str(page),
         "-singlefile", str(pdf), str(prefix)],
        cwd=str(POPPLER.parent), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if completed.returncode != 0 or not out.exists():
        raise RuntimeError(completed.stderr.strip() or "render failed")
    return out


def ngrams(text: str, n: int) -> list[str]:
    """只取汉字序列做 n-gram：标点和空格的 OCR 差异不该算成识别错误。"""
    chars = re.sub(r"[^\u4e00-\u9fff]", "", text)
    return [chars[i:i + n] for i in range(len(chars) - n + 1)]


def recall(truth: str, hypothesis: str, n: int = 8) -> tuple[float, int, int]:
    """truth 的 n-gram 有多少能在 hypothesis 里原样找到。"""
    hyp_chars = re.sub(r"[^\u4e00-\u9fff]", "", hypothesis)
    grams = ngrams(truth, n)
    if not grams:
        return 0.0, 0, 0
    hit = sum(1 for gram in grams if gram in hyp_chars)
    return hit / len(grams), hit, len(grams)


def run_paddle(images: list[Path]) -> dict[str, str]:
    """在系统 Python 里跑 PaddleOCR（codex 运行时没装）。"""
    script = WORK / "_paddle_runner.py"
    script.write_text(
        "import json,sys,warnings\n"
        "warnings.filterwarnings('ignore')\n"
        "from paddleocr import PaddleOCR\n"
        "ocr=PaddleOCR(use_textline_orientation=False, lang='ch')\n"
        "out={}\n"
        "for p in sys.argv[1:]:\n"
        "    r=ocr.predict(p)\n"
        "    lines=[]\n"
        "    for page in r:\n"
        "        lines.extend(page.get('rec_texts') or [])\n"
        "    out[p]='\\n'.join(lines)\n"
        "print('<<<JSON>>>')\n"
        "print(json.dumps(out,ensure_ascii=False))\n",
        encoding="utf-8",
    )
    # 环境必须继承而非重建：清空 PATH 会让子进程 import asyncio 时
    # 加载不到 Winsock，报 WinError 10106，看起来像 PaddleOCR 挂了。
    # 只覆盖必要的两个变量：编码，以及绕开本机那个已失效的代理
    # （HTTP_PROXY=127.0.0.1:7897 不通，PaddleOCR 首次下模型会卡死）。
    import os

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(key, None)
    env["NO_PROXY"] = "*"
    completed = subprocess.run(
        [str(SYSTEM_PYTHON), str(script), *[str(p) for p in images]],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,
    )
    if "<<<JSON>>>" not in completed.stdout:
        raise RuntimeError(
            f"PaddleOCR 失败 rc={completed.returncode}\n"
            f"stdout尾部: {completed.stdout[-1500:]}\n"
            f"stderr尾部: {completed.stderr[-1500:]}"
        )
    payload = completed.stdout.split("<<<JSON>>>", 1)[1].strip()
    return json.loads(payload)


def run_rapid(images: list[Path]) -> dict[str, str]:
    """在系统 Python 里跑 RapidOCR（rapidocr_onnxruntime，模型随包自带、不联网）。"""
    import os

    script = WORK / "_rapid_runner.py"
    script.write_text(
        "import json,sys\n"
        "from rapidocr_onnxruntime import RapidOCR\n"
        "ocr=RapidOCR()\n"
        "out={}\n"
        "for p in sys.argv[1:]:\n"
        "    result,_=ocr(p)\n"
        "    lines=[line[1] for line in (result or [])]\n"
        "    out[p]='\\n'.join(lines)\n"
        "print('<<<JSON>>>')\n"
        "print(json.dumps(out,ensure_ascii=False))\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(key, None)
    env["NO_PROXY"] = "*"
    completed = subprocess.run(
        [str(SYSTEM_PYTHON), str(script), *[str(p) for p in images]],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,
    )
    if "<<<JSON>>>" not in completed.stdout:
        raise RuntimeError(
            f"RapidOCR 失败 rc={completed.returncode}\n"
            f"stdout尾部: {completed.stdout[-1500:]}\n"
            f"stderr尾部: {completed.stderr[-1500:]}"
        )
    payload = completed.stdout.split("<<<JSON>>>", 1)[1].strip()
    return json.loads(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", default=str(ROOT / "南京路彼岸" / "2026-08-09风起云涌.pdf"))
    parser.add_argument("--pages", default="2,5,8,10", help="逗号分隔的页号（要选文本层完好的页）")
    parser.add_argument("--dpis", default="140,300", help="逗号分隔的 DPI")
    parser.add_argument("--skip-paddle", action="store_true")
    parser.add_argument("--skip-rapid", action="store_true")
    args = parser.parse_args()

    from pypdf import PdfReader

    pdf = Path(args.pdf)
    pages = [int(x) for x in args.pages.split(",")]
    dpis = [int(x) for x in args.dpis.split(",")]
    WORK.mkdir(parents=True, exist_ok=True)

    reader = PdfReader(pdf)
    truth = {p: clean_text(reader.pages[p - 1].extract_text() or "") for p in pages}
    for page, text in truth.items():
        chars = len(re.sub(r"[^\u4e00-\u9fff]", "", text))
        print(f"第{page}页 文本层汉字 {chars}")
    print()

    results: list[dict] = []
    rendered: dict[tuple[int, int], Path] = {}
    for dpi in dpis:
        for page in pages:
            path = WORK / f"p{page:02d}-dpi{dpi}.png"
            render(pdf, page, dpi, path)
            rendered[(dpi, page)] = path

    # Windows OCR：一次批量提交，与导入器走同一条路径
    for dpi in dpis:
        items = [(f"{page}", rendered[(dpi, page)]) for page in pages]
        got = ocr_images(items)
        for page in pages:
            text = clean_text(got.get(f"{page}", {}).get("text", ""), ocr=True)
            rate, hit, total = recall(truth[page], text)
            results.append({
                "engine": "windows", "dpi": dpi, "page": page,
                "recall": rate, "hit": hit, "total": total,
                "out_chars": len(re.sub(r"[^\u4e00-\u9fff]", "", text)),
            })
            (WORK / f"windows-p{page:02d}-dpi{dpi}.txt").write_text(text, encoding="utf-8")

    if not args.skip_paddle:
        for dpi in dpis:
            images = [rendered[(dpi, page)] for page in pages]
            try:
                got = run_paddle(images)
            except Exception as exc:
                print(f"PaddleOCR dpi={dpi} 失败：{exc}\n")
                continue
            for page in pages:
                text = clean_text(got.get(str(rendered[(dpi, page)]), ""), ocr=True)
                rate, hit, total = recall(truth[page], text)
                results.append({
                    "engine": "paddle", "dpi": dpi, "page": page,
                    "recall": rate, "hit": hit, "total": total,
                    "out_chars": len(re.sub(r"[^\u4e00-\u9fff]", "", text)),
                })
                (WORK / f"paddle-p{page:02d}-dpi{dpi}.txt").write_text(text, encoding="utf-8")

    if not args.skip_rapid:
        import time

        for dpi in dpis:
            images = [rendered[(dpi, page)] for page in pages]
            t0 = time.perf_counter()
            got = run_rapid(images)
            cost = time.perf_counter() - t0
            print(f"RapidOCR dpi={dpi}: {len(images)} 页 {cost:.1f}s（{cost / len(images):.2f}s/页）")
            for page in pages:
                text = clean_text(got.get(str(rendered[(dpi, page)]), ""), ocr=True)
                rate, hit, total = recall(truth[page], text)
                results.append({
                    "engine": "rapid", "dpi": dpi, "page": page,
                    "recall": rate, "hit": hit, "total": total,
                    "out_chars": len(re.sub(r"[^一-鿿]", "", text)),
                })
                (WORK / f"rapid-p{page:02d}-dpi{dpi}.txt").write_text(text, encoding="utf-8")

    print(f"{'引擎':10s}{'DPI':>6s}{'页':>4s}{'还原率':>9s}{'命中/总':>12s}{'输出汉字':>10s}")
    for row in results:
        ratio = "{}/{}".format(row["hit"], row["total"])
        print(f"{row['engine']:10s}{row['dpi']:>6d}{row['page']:>4d}"
              f"{row['recall'] * 100:>8.1f}%{ratio:>12s}"
              f"{row['out_chars']:>10d}")

    print()
    print(f"{'引擎':10s}{'DPI':>6s}{'加权还原率':>12s}")
    for engine in ("windows", "paddle", "rapid"):
        for dpi in dpis:
            subset = [r for r in results if r["engine"] == engine and r["dpi"] == dpi]
            if not subset:
                continue
            hit = sum(r["hit"] for r in subset)
            total = sum(r["total"] for r in subset)
            print(f"{engine:10s}{dpi:>6d}{hit / total * 100:>11.1f}%")

    (WORK / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n明细与逐页输出：{WORK}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
