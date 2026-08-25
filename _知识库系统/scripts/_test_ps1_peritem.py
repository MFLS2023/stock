# -*- coding: utf-8 -*-
"""冒烟验证 windows_ocr_batch.ps1 的逐项容错（2026-08-25 审查 🟡1）。

构造 manifest：一张真实 PNG + 一条不存在的路径。
修复前：坏路径让整个脚本中止、returncode≠0，好图的结果也拿不到；
修复后：rc=0、好图产出 txt、坏路径在 stderr 留痕且无输出文件。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PS1 = HERE / "windows_ocr_batch.ps1"


def main() -> int:
    from PIL import Image

    work = Path(tempfile.mkdtemp(prefix="ps1-peritem-"))
    good_png = work / "good.png"
    Image.new("RGB", (200, 100), "white").save(good_png, format="PNG")

    good_txt = work / "good.txt"
    bad_txt = work / "bad.txt"
    manifest = work / "manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {"key": "good", "image_path": str(good_png), "output_path": str(good_txt)},
                {"key": "bad", "image_path": str(work / "不存在.png"), "output_path": str(bad_txt)},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(PS1), "-ManifestPath", str(manifest), "-Language", "zh-Hans",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    checks = {
        "returncode==0（不再整批中止）": completed.returncode == 0,
        "好图产出 txt": good_txt.exists(),
        "坏图无输出文件": not bad_txt.exists(),
        "stderr 留痕含「分片OCR失败」": "分片OCR失败" in (completed.stderr or ""),
    }
    ok = all(checks.values())
    for name, passed in checks.items():
        print(("PASS " if passed else "FAIL ") + name)
    if not ok:
        print("--- stdout ---")
        print(completed.stdout or "(空)")
        print("--- stderr ---")
        print(completed.stderr or "(空)")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
