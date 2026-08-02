#!/usr/bin/env python3
"""Import a Feishu exported chat HTML while preserving speakers and chronology."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import yaml

from kb_import_utils import (
    ROOT,
    clean_text,
    infer_topics,
    meaningful_char_count,
    write_jsonl,
    write_text_lf,
)


# 来源身份用作者署名，不用资料载体命名：飞书只是这批资料的导出格式，
# 换个载体（微信、语音转写）来源还是同一个人。因此 SOURCE_ID 是 panfeng，
# 而 source_type / chunk_type / adapter 仍带 feishu——那几个描述的是结构和解析
# 能力，将来别人的飞书导出可以复用同一套适配器。
SOURCE_ID = "panfeng"
SOURCE_NAME = "我有上将潘凤"
SOURCE_ROOT = ROOT / "飞书聊天记录_潘凤"      # 原始资料目录，只读，不随 ID 改名
LIB = ROOT / "_知识库系统" / "source_libraries" / SOURCE_ID

# 用户批准该来源与复利杯、南京路彼岸、郁金香花园同级的日期。CLAUDE.md 要求新来源
# 经 dry-run 与审批，这个字段就是审批留痕，写进 sources.yaml 和 source.yaml。
APPROVED_ON = "2026-08-02"
CONFIG = ROOT / "_知识库系统" / "config" / "sources.yaml"
EXPECTED_MESSAGES = 3445
ROLLOVER_THRESHOLD_MINUTES = 90
REDUNDANT_SIGNATURE = re.compile(r"(?:\s*【潘凤老师】\s*)+$")
DATE_CLOCK = re.compile(r"^(?:(?P<month>\d{1,2})月(?P<day>\d{1,2})日\s+|(?P<yesterday>昨天)\s+)?(?P<hour>\d{1,2}):(?P<minute>\d{2})$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
    values = dict(attrs).get("class") or ""
    return set(values.split())


class FeishuExportParser(HTMLParser):
    """Small purpose-built DOM reader for the stable Feishu export structure."""

    CAPTURE_CLASSES = {
        "meta-info": "meta",
        "sender-name": "sender",
        "msg-time": "header_time",
        "msg-text-bubble": "main_text",
        "consecutive-time": "consecutive_time",
        "msg-bubble-consecutive": "consecutive_text",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.group_depth: int | None = None
        self.current_group: dict | None = None
        self.groups: list[dict] = []
        self.meta_text = ""
        self.capture_kind = ""
        self.capture_tag = ""
        self.capture_depth = -1
        self.capture_buffer: list[str] = []
        self.image_count = 0
        self.external_link_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.depth += 1
        classes = _classes(attrs)
        attributes = dict(attrs)
        if tag == "img":
            self.image_count += 1
        if tag == "a" and attributes.get("href"):
            self.external_link_count += 1
        if tag == "div" and "msg-group" in classes:
            if self.current_group is not None:
                raise ValueError("Nested msg-group is not supported")
            self.group_depth = self.depth
            self.current_group = {
                "sender": "",
                "header_time": "",
                "main_text": "",
                "consecutive_times": [],
                "consecutive_texts": [],
            }
        if not self.capture_kind:
            for class_name, kind in self.CAPTURE_CLASSES.items():
                if class_name in classes:
                    self.capture_kind = kind
                    self.capture_tag = tag
                    self.capture_depth = self.depth
                    self.capture_buffer = []
                    break

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.capture_kind:
            self.capture_buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.capture_kind and self.capture_depth == self.depth and self.capture_tag == tag:
            value = "".join(self.capture_buffer).strip()
            self._finish_capture(self.capture_kind, value)
            self.capture_kind = ""
            self.capture_tag = ""
            self.capture_depth = -1
            self.capture_buffer = []
        if self.current_group is not None and self.group_depth == self.depth and tag == "div":
            if len(self.current_group["consecutive_times"]) != len(self.current_group["consecutive_texts"]):
                raise ValueError("A message group has mismatched consecutive times and bubbles")
            self.groups.append(self.current_group)
            self.current_group = None
            self.group_depth = None
        self.depth -= 1

    def _finish_capture(self, kind: str, value: str) -> None:
        if kind == "meta":
            self.meta_text = value
            return
        if self.current_group is None:
            return
        if kind == "consecutive_time":
            self.current_group["consecutive_times"].append(value)
        elif kind == "consecutive_text":
            self.current_group["consecutive_texts"].append(value)
        else:
            self.current_group[kind] = value


def parse_html(path: Path) -> tuple[list[dict], dict]:
    parser = FeishuExportParser()
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    if parser.current_group is not None:
        raise ValueError("HTML ended before the final msg-group was closed")
    return parser.groups, {
        "meta_text": parser.meta_text,
        "images": parser.image_count,
        "external_links": parser.external_link_count,
    }


def parse_export_date(meta_text: str) -> date:
    match = re.search(r"导出时间:\s*(20\d{2})/(\d{1,2})/(\d{1,2})", meta_text)
    if not match:
        raise ValueError(f"Cannot read export date from meta-info: {meta_text!r}")
    return date(*(int(value) for value in match.groups()))


def parse_time_label(label: str, export_date: date) -> tuple[date | None, int | None, str]:
    normalized = unicodedata.normalize("NFKC", label or "").strip()
    if not normalized or normalized.casefold() == "unknown" or normalized == "未知":
        return None, None, "unknown"
    match = DATE_CLOCK.fullmatch(normalized)
    if not match:
        raise ValueError(f"Unsupported Feishu time label: {label!r}")
    minutes = int(match.group("hour")) * 60 + int(match.group("minute"))
    if minutes >= 24 * 60:
        raise ValueError(f"Invalid clock value: {label!r}")
    explicit_date = None
    kind = "clock"
    if match.group("yesterday"):
        explicit_date = export_date - timedelta(days=1)
        kind = "relative_date"
    elif match.group("month"):
        candidate = date(export_date.year, int(match.group("month")), int(match.group("day")))
        if candidate > export_date:
            candidate = date(export_date.year - 1, candidate.month, candidate.day)
        explicit_date = candidate
        kind = "absolute_date"
    return explicit_date, minutes, kind


def next_market_weekday(value: date) -> date:
    candidate = value + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def normalize_message_text(value: str) -> tuple[str, bool]:
    text = clean_text(value)
    cleaned = REDUNDANT_SIGNATURE.sub("", text).strip()
    # A few exported records contain only the signature marker.  Keep those
    # verbatim so message numbering remains lossless instead of turning them into
    # artificial empty messages.
    if not cleaned:
        return text, False
    return cleaned, cleaned != text


def reconstruct_messages(groups: list[dict], export_date: date) -> tuple[list[dict], dict]:
    messages: list[dict] = []
    last_global_date: date | None = None
    last_global_clock: int | None = None
    counters = Counter()
    anchor_gaps: list[int] = []

    for group_no, group in enumerate(groups, start=1):
        sender = clean_text(group.get("sender", ""))
        if not sender:
            raise ValueError(f"Message group {group_no} has no sender")
        labels = [group.get("header_time", ""), *group.get("consecutive_times", [])]
        texts = [group.get("main_text", ""), *group.get("consecutive_texts", [])]
        if len(labels) != len(texts):
            raise ValueError(f"Message group {group_no} has {len(labels)} times and {len(texts)} texts")

        header_date, _, header_kind = parse_time_label(labels[0], export_date)
        counters[f"header_{header_kind}"] += 1
        if header_date is not None:
            if last_global_date is not None:
                if header_date < last_global_date:
                    raise ValueError(
                        f"Date anchor moves backwards at group {group_no}: {last_global_date} -> {header_date}"
                    )
                anchor_gaps.append((header_date - last_global_date).days)
            current_date = header_date
            previous_clock = None
        else:
            if last_global_date is None:
                raise ValueError(f"Message group {group_no} has no date anchor")
            current_date = last_global_date
            previous_clock = last_global_clock

        for item_no, (raw_label, raw_text) in enumerate(zip(labels, texts), start=1):
            explicit_date, clock, time_kind = parse_time_label(raw_label, export_date)
            time_inherited = False
            if explicit_date is not None:
                current_date = explicit_date
            if clock is None:
                if previous_clock is None:
                    raise ValueError(f"Cannot inherit unknown time at group {group_no}, item {item_no}")
                clock = previous_clock
                time_inherited = True
                counters["unknown_time_inherited"] += 1
            elif (
                explicit_date is None
                and previous_clock is not None
                and previous_clock - clock >= ROLLOVER_THRESHOLD_MINUTES
            ):
                current_date = next_market_weekday(current_date)
                counters["market_day_rollovers"] += 1

            text, signature_removed = normalize_message_text(raw_text)
            if not text:
                raise ValueError(f"Empty normalized message at group {group_no}, item {item_no}")
            if signature_removed:
                counters["redundant_signatures_removed"] += 1
            message_no = len(messages) + 1
            hour, minute = divmod(clock, 60)
            time_text = f"{hour:02d}:{minute:02d}"
            messages.append(
                {
                    "source_id": SOURCE_ID,
                    "message_no": message_no,
                    "message_id": f"{SOURCE_ID}-m{message_no:04d}",
                    "group_no": group_no,
                    "group_item_no": item_no,
                    "sender": sender,
                    "date": current_date.isoformat(),
                    "time": time_text,
                    "datetime": f"{current_date.isoformat()}T{time_text}:00+08:00",
                    "raw_time": raw_label,
                    "time_kind": time_kind,
                    "time_inherited": time_inherited,
                    "text": text,
                }
            )
            previous_clock = clock

        last_global_date = current_date
        last_global_clock = previous_clock

    counters["anchor_gap_max_days"] = max(anchor_gaps, default=0)
    counters["anchor_gap_over_one_day"] = sum(gap > 1 for gap in anchor_gaps)
    return messages, dict(counters)


def render_message(message: dict) -> str:
    return f"[{message['time']}｜{message['sender']}｜消息{message['message_no']:04d}] {message['text']}"


def message_datetime(message: dict) -> datetime:
    return datetime.fromisoformat(message["datetime"])


def message_locator(items: list[dict]) -> str:
    first, last = items[0], items[-1]
    times = sorted(item["time"] for item in items)
    time_range = times[0] if times[0] == times[-1] else f"{times[0]}—{times[-1]}"
    message_range = (
        f"消息{first['message_no']:04d}"
        if first["message_no"] == last["message_no"]
        else f"消息{first['message_no']:04d}—{last['message_no']:04d}"
    )
    return f"{first['date']} {time_range}｜{message_range}"


def group_messages(
    items: list[dict], *, target_chars: int, max_chars: int, gap_minutes: int
) -> list[list[dict]]:
    groups: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0
    for item in items:
        rendered_length = len(render_message(item))
        gap = 0.0
        if current:
            gap = (message_datetime(item) - message_datetime(current[-1])).total_seconds() / 60
        should_break = bool(
            current
            and (
                current_chars + rendered_length > max_chars
                or (gap > gap_minutes and current_chars >= target_chars // 2)
            )
        )
        if should_break:
            groups.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += rendered_length
        if current_chars >= target_chars:
            groups.append(current)
            current = []
            current_chars = 0
    if current:
        groups.append(current)
    return groups


def load_registered_source() -> dict:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    source = next((item for item in config.get("sources", []) if item["id"] == SOURCE_ID), None)
    if source is None:
        raise KeyError(f"Source {SOURCE_ID} is not registered. Run register_source.py first.")
    return source


def build_library(path: Path, messages: list[dict], parse_meta: dict, stats: dict) -> dict:
    source = load_registered_source()
    LIB.mkdir(parents=True, exist_ok=True)
    texts_dir = LIB / "texts"
    texts_dir.mkdir(parents=True, exist_ok=True)
    source_hash = sha256_file(path)

    daily: dict[str, list[dict]] = defaultdict(list)
    for message in messages:
        daily[message["date"]].append(message)

    document_rows: list[dict] = []
    parent_rows: list[dict] = []
    chunk_rows: list[dict] = []
    for day in sorted(daily):
        day_messages = daily[day]
        document_id = f"{SOURCE_ID}-{day.replace('-', '')}"
        title = f"{SOURCE_NAME}｜{day}"
        normalized_text = "\n".join(render_message(message) for message in day_messages)
        text_path = texts_dir / f"{day}.txt"
        write_text_lf(text_path, normalized_text + "\n")
        speakers = list(dict.fromkeys(message["sender"] for message in day_messages))
        topics = infer_topics(title, normalized_text)
        document_rows.append(
            {
                "source_id": SOURCE_ID,
                "document_id": document_id,
                "title": title,
                "date": day,
                "author_or_guest": "、".join(speakers),
                "content_type": "feishu_chat_daily",
                "messages": len(day_messages),
                "message_start": day_messages[0]["message_no"],
                "message_end": day_messages[-1]["message_no"],
                "time_start": min(message["time"] for message in day_messages),
                "time_end": max(message["time"] for message in day_messages),
                "characters": meaningful_char_count(normalized_text),
                "topics": topics,
                "original_path": str(path),
                "normalized_text_path": str(text_path),
                "sha256": source_hash,
                "risk_flags": "日期按绝对锚点和交易日时间回绕重建；原文混有拼音及字符替代，未擅自纠正",
            }
        )

        for parent_number, parent_messages in enumerate(
            group_messages(day_messages, target_chars=3200, max_chars=4800, gap_minutes=60), start=1
        ):
            parent_id = f"{document_id}-p{parent_number:03d}"
            parent_text = "\n".join(render_message(message) for message in parent_messages)
            parent_speakers = list(dict.fromkeys(message["sender"] for message in parent_messages))
            parent_rows.append(
                {
                    "source_id": SOURCE_ID,
                    "document_id": document_id,
                    "parent_id": parent_id,
                    "title": title,
                    "date": day,
                    "author_or_guest": "、".join(parent_speakers),
                    "locator": message_locator(parent_messages),
                    "message_start": parent_messages[0]["message_no"],
                    "message_end": parent_messages[-1]["message_no"],
                    "text": parent_text,
                }
            )
            for chunk_number, chunk_messages in enumerate(
                group_messages(parent_messages, target_chars=750, max_chars=1200, gap_minutes=30), start=1
            ):
                chunk_text = "\n".join(render_message(message) for message in chunk_messages)
                chunk_speakers = list(dict.fromkeys(message["sender"] for message in chunk_messages))
                chunk_rows.append(
                    {
                        "source_id": SOURCE_ID,
                        "source_name": SOURCE_NAME,
                        "document_id": document_id,
                        "parent_id": parent_id,
                        "chunk_id": f"{parent_id}-c{chunk_number:02d}",
                        "chunk_type": "feishu_chat",
                        "title": title,
                        "date": day,
                        "author_or_guest": "、".join(chunk_speakers),
                        "speakers": chunk_speakers,
                        "topics": infer_topics(title, chunk_text),
                        "claim_type": "opinion_or_case",
                        "market_regime": "历史实盘聊天，环境需结合日期判断",
                        "locator": message_locator(chunk_messages),
                        "message_start": chunk_messages[0]["message_no"],
                        "message_end": chunk_messages[-1]["message_no"],
                        "time_start": chunk_messages[0]["time"],
                        "time_end": chunk_messages[-1]["time"],
                        "text": chunk_text,
                        "original_path": str(path),
                        "confidence": "high",
                        "extraction_method": "html_structure",
                    }
                )

    write_jsonl(LIB / "messages.jsonl", messages)
    write_jsonl(LIB / "documents.jsonl", document_rows)
    write_jsonl(LIB / "parents.jsonl", parent_rows)
    write_jsonl(LIB / "chunks.jsonl", chunk_rows)

    source.update(
        {
            "source_type": "feishu_chat_html",
            "status": "integrated",
            "approved_on": APPROVED_ON,
            "primary_locator": "date_time_message_number",
            "pipeline": "speaker_time_aware_html",
            "adapter": "specialized_feishu_chat",
            "review_required": False,
            "notes": "飞书HTML连续消息导出；保留发言人、重建交易日日期、继承unknown时间，并按消息编号引用。",
        }
    )
    write_text_lf(LIB / "source.yaml", yaml.safe_dump(source, allow_unicode=True, sort_keys=False))

    content_map = [
        f"# {SOURCE_NAME}内容地图（飞书聊天记录）",
        "",
        "> 每个日期为一个文档；定位以标准化后的连续消息编号为准，原HTML保持不变。",
        "",
        "| 日期 | 消息数 | 时间范围 | 发言人 | 主题 | 字符数 | 消息范围 | 文档ID |",
        "|---|---:|---|---|---|---:|---|---|",
    ]
    for item in document_rows:
        content_map.append(
            f"| {item['date']} | {item['messages']} | {item['time_start']}—{item['time_end']} | "
            f"{item['author_or_guest']} | {'、'.join(item['topics']) or '未标注'} | {item['characters']} | "
            f"{item['message_start']:04d}—{item['message_end']:04d} | {item['document_id']} |"
        )
    write_text_lf(LIB / "content_map.md", "\n".join(content_map) + "\n")

    speaker_counts = Counter(message["sender"] for message in messages)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_id": SOURCE_ID,
        "html_file": str(path),
        "sha256": source_hash,
        "html_groups": len({message["group_no"] for message in messages}),
        "messages": len(messages),
        "documents": len(document_rows),
        "parents": len(parent_rows),
        "chunks": len(chunk_rows),
        "date_start": min(daily),
        "date_end": max(daily),
        "message_dates": len(daily),
        "speakers": dict(speaker_counts),
        "images": parse_meta["images"],
        "external_links": parse_meta["external_links"],
        "date_and_cleaning_stats": stats,
    }
    write_text_lf(
        LIB / "source_summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    )
    quality = [
        f"# {SOURCE_NAME}导入质量报告（飞书聊天记录）",
        "",
        f"- 原始HTML：1个，SHA256 `{source_hash}`",
        f"- HTML消息组：{summary['html_groups']}；逐条消息：{summary['messages']}",
        f"- 发言人：{json.dumps(dict(speaker_counts), ensure_ascii=False)}",
        f"- 日期范围：{summary['date_start']} 至 {summary['date_end']}，有消息的交易日共 {summary['message_dates']} 个",
        f"- 每日文档：{summary['documents']}；父块：{summary['parents']}；检索块：{summary['chunks']}",
        f"- 图片：{summary['images']}；外链：{summary['external_links']}",
        f"- `unknown`时间继承：{stats.get('unknown_time_inherited', 0)}条",
        f"- 按时间回绕推断下一交易日：{stats.get('market_day_rollovers', 0)}次",
        f"- 去除标准化文本尾部冗余`【潘凤老师】`：{stats.get('redundant_signatures_removed', 0)}条",
        "",
        "## 日期重建说明",
        "",
        "部分组头只有时分，没有年月日。导入器从绝对日期组头开始，在组内时间明显回退90分钟以上时推进到下一个周一至周五；后续绝对日期锚点没有出现倒退。该规则与这份A股盘中聊天的全部锚点闭合，但仍属于结构化推断，不等同于飞书原生消息时间戳。",
        "",
        "## 文本风险",
        "",
        "原文存在拼音、希腊字母、同音字和规避关键词的替代写法。标准化仅做Unicode、空白和冗余签名清理，不猜测恢复证券名称；关键结论需按消息编号回看上下文。所有聊天判断均标为历史观点或案例，不作为当前市场事实。",
    ]
    write_text_lf(LIB / "quality_report.md", "\n".join(quality) + "\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, help="Override the registered source HTML")
    parser.add_argument("--force", action="store_true", help="Accepted for dispatcher compatibility; import is idempotent")
    args = parser.parse_args()
    candidates = sorted(SOURCE_ROOT.glob("*.html"))
    path = args.file.resolve() if args.file else (candidates[0] if len(candidates) == 1 else None)
    if path is None:
        raise ValueError(f"Expected exactly one HTML file under {SOURCE_ROOT}, found {len(candidates)}")
    if not path.exists():
        raise FileNotFoundError(path)

    groups, parse_meta = parse_html(path)
    export_date = parse_export_date(parse_meta["meta_text"])
    messages, stats = reconstruct_messages(groups, export_date)
    if len(messages) != EXPECTED_MESSAGES:
        raise ValueError(f"Expected {EXPECTED_MESSAGES} messages, parsed {len(messages)}")
    summary = build_library(path, messages, parse_meta, stats)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
