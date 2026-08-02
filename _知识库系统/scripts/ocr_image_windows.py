#!/usr/bin/env python3
"""OCR a large image with native Windows OCR, tiling when dimensions exceed the API limit."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
POWERSHELL_OCR = Path(__file__).with_name("windows_ocr.ps1")
TEMP_ROOT = ROOT / "_知识库系统" / "tmp" / "windows-ocr"


def tile_boxes(width: int, height: int, max_dim: int, overlap: int):
    y = 0
    while y < height:
        bottom = min(y + max_dim, height)
        yield (0, y, width, bottom)
        if bottom >= height:
            break
        y = bottom - overlap


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--language", default="zh-Hans")
    parser.add_argument("--max-dimension", type=int, default=9000)
    parser.add_argument("--overlap", type=int, default=120)
    args = parser.parse_args()

    image_path = args.image.resolve()
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="ocr-", dir=TEMP_ROOT))
    texts: list[str] = []
    try:
        with Image.open(image_path) as image:
            if image.width > args.max_dimension:
                ratio = args.max_dimension / image.width
                image = image.resize((args.max_dimension, int(image.height * ratio)))
            boxes = list(tile_boxes(image.width, image.height, args.max_dimension, args.overlap))
            for index, box in enumerate(boxes, start=1):
                tile_path = work / f"tile-{index:03d}.png"
                text_path = work / f"tile-{index:03d}.txt"
                image.crop(box).save(tile_path, format="PNG", optimize=True)
                command = [
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(POWERSHELL_OCR),
                    "-ImagePath", str(tile_path), "-Language", args.language, "-OutputPath", str(text_path),
                ]
                completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
                if completed.returncode != 0:
                    raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
                texts.append(text_path.read_text(encoding="utf-8").strip())
    finally:
        shutil.rmtree(work, ignore_errors=True)

    result = "\n".join(text for text in texts if text).strip() + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result, encoding="utf-8")
    else:
        print(result, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
