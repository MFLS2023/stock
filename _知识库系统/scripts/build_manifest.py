#!/usr/bin/env python3
"""Build an incremental, hash-backed manifest for read-only source folders."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
SYSTEM = ROOT / "_知识库系统"
CONFIG = SYSTEM / "config" / "sources.yaml"
MANIFEST = SYSTEM / "indexes" / "manifest.jsonl"
SUMMARY = SYSTEM / "indexes" / "manifest_summary.json"


def sha256_file(path: Path, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(block_size), b""):
            digest.update(block)
    return digest.hexdigest()


def load_previous(path: Path) -> dict[tuple[str, str], dict]:
    if not path.exists():
        return {}
    records: dict[tuple[str, str], dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        records[(item["source_id"], item["relative_path"])] = item
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force-hash", action="store_true", help="Recompute every hash")
    args = parser.parse_args()

    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    previous = load_previous(MANIFEST)
    current: list[dict] = []
    seen: set[tuple[str, str]] = set()
    change_counts = {"new": 0, "changed": 0, "unchanged": 0, "removed": 0}
    source_counts: dict[str, int] = {}

    for source in config["sources"]:
        source_id = source["id"]
        source_root = ROOT / source["source_path"]
        if not source_root.exists():
            raise FileNotFoundError(f"Missing source folder: {source_root}")

        files = sorted((p for p in source_root.rglob("*") if p.is_file()), key=lambda p: str(p).lower())
        source_counts[source_id] = len(files)
        for path in files:
            stat = path.stat()
            relative = path.relative_to(source_root).as_posix()
            key = (source_id, relative)
            seen.add(key)
            old = previous.get(key)
            same_stat = bool(
                old
                and old.get("size") == stat.st_size
                and old.get("mtime_ns") == stat.st_mtime_ns
            )
            digest = old["sha256"] if same_stat and not args.force_hash else sha256_file(path)
            if old is None:
                change = "new"
            elif old.get("sha256") != digest:
                change = "changed"
            else:
                change = "unchanged"
            change_counts[change] += 1
            current.append(
                {
                    "source_id": source_id,
                    "source_name": source["display_name"],
                    "relative_path": relative,
                    "original_path": str(path),
                    "extension": path.suffix.lower(),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "sha256": digest,
                    "change": change,
                }
            )

    removed = sorted(set(previous) - seen)
    change_counts["removed"] = len(removed)
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in current),
        encoding="utf-8",
    )
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(ROOT),
        "total_files": len(current),
        "source_counts": source_counts,
        "changes": change_counts,
        "removed": [{"source_id": sid, "relative_path": rel} for sid, rel in removed],
    }
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
