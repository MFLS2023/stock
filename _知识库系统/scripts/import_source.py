#!/usr/bin/env python3
"""Dispatch one registered source through its adapter, then rebuild and validate."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = Path(__file__).resolve().parent
CONFIG = ROOT / "_知识库系统" / "config" / "sources.yaml"


def run(command: list[str]) -> None:
    completed = subprocess.run(command, cwd=ROOT)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--ingest-only", action="store_true")
    args = parser.parse_args()
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    source = next((item for item in config.get("sources", []) if item["id"] == args.source), None)
    if source is None:
        raise KeyError(f"Unknown source: {args.source}. Run register_source.py first.")

    run([sys.executable, str(SCRIPTS / "build_manifest.py")])
    adapter = source.get("adapter") or {
        "fulibei": "specialized_fulibei",
        "nanjinglu_bian": "specialized_nanjinglu",
        "tulip_garden": "specialized_tulip",
    }.get(source["id"], "generic_mixed")
    if adapter == "specialized_nanjinglu":
        command = [sys.executable, str(SCRIPTS / "import_nanjinglu.py")]
        if args.force:
            command.append("--force")
        run(command)
    elif adapter == "specialized_tulip":
        command = [sys.executable, str(SCRIPTS / "import_tulip_garden.py")]
        if args.force:
            command.extend(["--force-ocr", "--force-convert"])
        run(command)
    elif adapter == "specialized_fulibei":
        raise RuntimeError("复利杯依赖既有逐稿成果和参数化导入器；请使用 import_fulibei.py 的专用参数。")
    elif adapter == "specialized_feishu_chat":
        command = [sys.executable, str(SCRIPTS / "import_feishu_chat.py")]
        if args.force:
            command.append("--force")
        run(command)
    elif adapter == "specialized" or source.get("review_required") and source.get("adapter") == "specialized":
        raise RuntimeError("该来源已标记为需要专用适配器，不能用通用导入器静默处理。")
    else:
        command = [sys.executable, str(SCRIPTS / "import_generic_source.py"), "--source", source["id"]]
        if args.force:
            command.append("--force")
        run(command)

    if not args.ingest_only:
        for script in ("build_index.py", "build_cross_source.py", "validate_kb.py"):
            run([sys.executable, str(SCRIPTS / script)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
