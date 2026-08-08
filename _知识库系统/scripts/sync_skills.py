#!/usr/bin/env python3
"""把项目里的 SKILL 同步到 ~/.claude/skills/，让 Claude Code 能加载。

为什么需要这个脚本：

1. **Claude Code 只扫 `~/.claude/skills/`**，不认项目内的 `.agents/skills/`
   （那是 Codex 的目录）也不认 `_导师试验/skills/`。项目里改了 SKILL，
   Claude Code 那边不会自动更新。

2. **Windows 上 `ln -s` 退化为目录复制**（实测：创建后 `[ -L ]` 为假、
   `ls -la` 显示 `drwx` 而非 `l`），所以没法用符号链接一劳永逸，只能定期同步。

3. 这个坑已经咬过一次：2026-08-08 发现 ~/.claude/skills/ 里的两个导师 SKILL
   是 v1.0 旧版（郁金香 554 行 vs 项目里 1172 行），**旧版缺「9:20-9:25 不接受
   撤单」这条交易所硬规则**，用户此前提问时拿到的是会让他在无法撤单时段挂单的版本。

用法：
    python sync_skills.py            # 看差异，不改
    python sync_skills.py --apply    # 真正同步
"""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import re
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
TARGET = pathlib.Path.home() / ".claude" / "skills"

# 要同步的来源目录。key 是 ~/.claude/skills/ 下的目录名。
SOURCES: dict[str, pathlib.Path] = {}
for path in (ROOT / ".agents" / "skills").glob("*/SKILL.md"):
    SOURCES[path.parent.name] = path.parent
for path in (ROOT / "_导师试验" / "skills").glob("*/SKILL.md"):
    SOURCES[path.parent.name] = path.parent


def digest(path: pathlib.Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def frontmatter_name(path: pathlib.Path) -> str | None:
    """读 frontmatter 的 name 字段。Claude Code 认 name，不认 skill。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not match:
        return None
    for line in match.group(1).splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
        if line.startswith("skill:"):
            return f"⚠️skill:{line.split(':', 1)[1].strip()}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真正复制，默认只看差异")
    args = parser.parse_args()

    if not SOURCES:
        print("没找到任何 SKILL.md", file=sys.stderr)
        return 1
    TARGET.mkdir(parents=True, exist_ok=True)

    plans: list[tuple[str, str, pathlib.Path, pathlib.Path]] = []
    bad_frontmatter: list[tuple[str, str]] = []

    for name, source_dir in sorted(SOURCES.items()):
        source = source_dir / "SKILL.md"
        target = TARGET / name / "SKILL.md"

        field = frontmatter_name(source)
        if field is None:
            bad_frontmatter.append((name, "缺 frontmatter"))
        elif field.startswith("⚠️skill:"):
            bad_frontmatter.append((name, "首字段是 skill: 而非 name:，Claude Code 认不出"))

        if not target.exists():
            plans.append(("新增", name, source, target))
        elif digest(source) != digest(target):
            src_lines = len(source.read_text(encoding="utf-8", errors="replace").splitlines())
            dst_lines = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
            plans.append((f"更新 {dst_lines}→{src_lines} 行", name, source, target))

    print(f"来源 {len(SOURCES)} 个 SKILL，目标 {TARGET}")
    print()

    if bad_frontmatter:
        print("⚠️ frontmatter 有问题（同步了也加载不了）：")
        for name, why in bad_frontmatter:
            print(f"    {name}: {why}")
        print()

    if not plans:
        print("✓ 全部已同步，无需操作")
        return 0

    print(f"需要同步 {len(plans)} 个：")
    for action, name, _, _ in plans:
        print(f"    [{action}] {name}")
    print()

    if not args.apply:
        print("这是预览。加 --apply 真正同步。")
        return 0

    for action, name, source, target in plans:
        target.parent.mkdir(parents=True, exist_ok=True)
        # 目标已存在且内容不同时，先留一份备份（旧版可能是唯一副本）
        if target.exists():
            backup = TARGET / "_backup-synced" / f"{name}.md"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
        shutil.copy2(source, target)
        # 同目录下的辅助文件（references/ 等）不复制：SKILL.md 是唯一入口，
        # 附件路径在 SKILL 里以项目绝对路径引用，复制反而产生两份不同步的附件。
        print(f"  ✓ {name}")

    print()
    print(f"已同步 {len(plans)} 个。旧版备份在 {TARGET / '_backup-synced'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
