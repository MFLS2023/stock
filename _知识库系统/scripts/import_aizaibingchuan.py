#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""爱在冰川专用导入器：把微信公众号导出的 Markdown 还原成可检索正文。

为什么不能用 import_generic_source.py：
通用 clean_text 是给 docx/pdf 写的，对微信 .md 只压缩 1.1%。
实测抽 300 个文件后仍残留 37,642 处图片语法和 37,673 处裸链接
（mmbiz.qpic.cn/... 这类几十字符的乱码串），噪声占全文 77%。
直接导入会让 chunk 被 URL 填满，检索报废。这里做微信专用预清洗。

已排除（用户 2026-08-04 决定）：资讯汇总、短线资金流、无正文空壳。
排除在复制阶段完成，来源目录里的 2584 个 .md 就是导入全集。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kb_import_utils import (  # noqa: E402
    ROOT,
    infer_topics,
    merge_short_units,
    span_locator,
    split_text,
    write_jsonl,
    write_text_lf,
)

SOURCE_ID = "aizaibingchuan"
SOURCE_NAME = "爱在冰川"
SOURCE_ROOT = ROOT / "爱在冰川"
LIB = ROOT / "_知识库系统" / "source_libraries" / SOURCE_ID

TARGET_CHARS = 1200          # 与项目其他来源一致
MIN_CHUNK_CHARS = 400        # 低于此值的块并入相邻块
MAX_CHUNK_CHARS = 1600
MIN_DOC_CHARS = 100          # 清洗后正文不足此数的文档跳过
MIN_AUTHOR_REPLY = 25        # 作者回复短于此值不单独入库（"嗯""是的"这类）

# 文件名形如 [2021-10-28-0001]20211027复盘.md
NAME_RE = re.compile(r"^\[(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})\](.*)$")

# 微信导出的固定样板行，整行匹配才删，避免伤正文
JUNK_LINES = {
    "预览时标签不可点", "微信扫一扫", "知道了", "取消  允许", "取消 允许",
    "在小说阅读器读本章", "去阅读", "阅读", "继续滑动看下一个", "轻触阅读原文",
    "向上滑动看下一个", "关注", "分享", "留言", "收藏", "点击上方蓝字关注",
    "长按识别二维码", "查看请点击蓝字", "喜欢此内容的人还喜欢", "阅读原文",
    "写留言", "已同步到看一看", "发送", "取消", "允许", "确定", "投诉",
    "视频小程序赞轻点两下取消赞在看轻点两下取消在看",
}

# 免责声明类固定句，每篇都有，重复 2584 次会污染检索
BOILERPLATE_PAT = [
    r"^本复盘仅为鄙人的股市思考历程[,，]非荐股[,，]请勿跟票。?$",
    r"^本文仅为鄙人的股市思考历程[,，]非荐股[,，]请勿跟票。?$",
    r"^以上仅为个人复盘记录[,，]不构成任何投资建议。?$",
    # 署名行：公众号名有「爱在冰川」「爱在冰川复盘」等变体，且会重复多次。
    # 微信「原创」栏还会夹读者笔名并重复两遍：「原创 水川 水川 爱在冰川」，
    # 实测 64 种笔名共 154 行，笔名不可枚举，用通配符占位。
    r"^原创\s+(?:\S{1,14}\s+)*(?:爱在冰川(?:复盘)?\s*)+$",
    r"^(?:爱在冰川(?:复盘)?\s*){2,}$",
    r"^微信扫一扫可打开此内容[,，]?$",
    r"^关注该公众号$",
    r"^使用完整服务$",
    r"^使用小程序$",
    r"^[×xX]\s*分析$",
    r"^分享\s+留言\s+收藏(\s+听过)?$",
    r"^视频\s*小程序\s*赞",
    r"^[：:，,。\s]+视频\s*小程序",
    # 免责声明：原文在词间插空格，且有「请勿票」（漏字）等手打变体。
    # 载体名有「本复盘」「本文」「本栏目」「本 复盘」「本」四种以上写法（实测
    # 「本栏目」670 行是最多的一种），行首偶尔粘着公众号 ID（shizhiwuwei…），
    # 所以载体名整体可选，并允许行首英文串。
    r"^(?:[A-Za-z][A-Za-z0-9_\-]{3,30})?\s*本?\s*(?:复\s*盘|文|栏\s*目)?\s*"
    r"仅[为奉].{0,12}的股市思考历程\s*[,，]\s*非荐股\s*[,，]?\s*请\s*勿\s*跟?\s*票\s*[。.]?$",
    # 平台红包广告，218 行逐字重复
    r"^打开支付宝首页搜索.{0,24}每天可领红包.{0,18}$",
    # 贴单打码的惯例说明，172 行逐字重复，属发布规则不属交易观点
    r"^[（(]\s*阅读上\s*2w.{0,60}打马赛克\s*[。.]?\s*[）)]?$",
    # 微信会把这行截断成两截，也会整行重复两遍（实测 102 字符），放宽长度。
    # 「特」字有时被吃掉或粘上标点：「别提示：」「特.别提示：」
    r"^特?\s*[.、]?\s*别提示\s*[：:]\s*文中股票.*$",
    r"^特?\s*[.、]?\s*别提示\s*[：:].{0,40}(?:不做买卖个股推荐|投资有风险).*$",
    # 段末免责括号独占整行的写法
    r"^[（(]\s*以上\s*[，,]\s*仅为逻辑分享.{0,20}[）)]\s*$",
    r"^(?:分析和历史查阅使用|供分析和历史查阅).{0,40}$",
    r"^修改于$",
    # 引流行：「关注我→→→ 关注我→→→ 爱在冰川」
    r"^(?:关注我\s*[→>\-—\s]*)+(?:爱在冰川(?:复盘)?\s*)*$",
    r"^(?:长按|扫描)?二维码.{0,20}$",
    r"^(?:点击|戳)(?:上方|下方|这里|蓝字).{0,20}$",
    r"^收录于合集\s*#?.{0,20}$",
    r"^上一篇$", r"^下一篇$", r"^人划线$",
    r"^投资有风险\s*[,，]\s*入市需谨慎\s*[！!。.]?$",
]
BOILERPLATE_RE = [re.compile(p) for p in BOILERPLATE_PAT]

# 微信表情占位符，实测 5066 处、20403 字符纯噪声。
# 必须用白名单而非「删掉所有 [xx]」：作者也用方括号做强调，删了会伤正文 ——
#   「（即为[造势]），不然你就只能做好[顺势]和[借势]」
#   「[跌了买一点]和[越跌越卖]，这不是写的相互矛盾嘛」
#   「[美] 伊恩·古德费洛 / [加] 约书亚·本吉奥」（书籍作者国籍）
#   「[^2]」（脚注）、「[gubar]」（论坛标签）、「[ 股市生存的基本技能 ]」（带空格）
EMOJI_TOKENS = frozenset("""
捂脸 流泪 呲牙 旺柴 撇嘴 苦涩 發 破涕为笑 微笑 偷笑 发呆 大哭 强 奸笑 抱拳
机智 坏笑 裂开 尴尬 玫瑰 害羞 得意 发怒 愉快 皱眉 吃瓜 抓狂 悠闲 调皮 叹气
疑问 难过 合十 白眼 翻白眼 憨笑 冷汗 衰 色 可怜 嘿哈 让我看看 奋斗 抠鼻 脸红
流汗 惊恐 擦汗 天啊 社会社会 拥抱 困 亲亲 晕 失望 惊讶 拳头 加油 发抖 汗 恐惧
耶 庆祝 跳跳 委屈 握手 快哭了 囧 敲打 打脸 好的 哇 咖啡 闭嘴 蛋糕 笑脸 阴险 吐
无语 傲慢 爱心 骷髅 鄙视 鼓掌 凋谢 胜利 心碎 生病 强壮 再见 猪头 啤酒 太阳
右哼哼 菜刀 月亮 睡 吐舌 鸡 嘘 狗头 炸弹 擦眼泪 鬼魂 左哼哼 怄火 问号脸 转圈
咒骂 便便 鲜花 滑稽 图片 转发 Emm 666 Doge Sob Cry Broken Lol Grin OMG Blush
Hurt Hey
""".split())
EMOJI_RE = re.compile(r"\[([^\[\]\s]{1,8})\]")

# 小节标题里粘着的免责修饰语，实测 2011 行：
#   「个股展望 （记录我自己明天观察的标的，非操作建议） ：」-> 「个股展望」
#   「（ 记录我自己明天观察的标的，非操作建议， 请勿跟票 ） ：」-> 整行删掉
# 918 行是括号独占整行，其中 915 行的前一行正是「闲 聊」小节标题，删掉不丢结构。
INLINE_DISCLAIMER_RE = re.compile(
    r"[（(]\s*(?:记\s*录我自己(?:明天观察的标的|的操作)\s*[，,]\s*)?"
    r"非操作建议\s*(?:[，,]\s*请\s*勿\s*跟?\s*票\s*)?[）)]"
)
# 同样的修饰语也有不带括号的写法：「记录我自己明天观察的标的，非操作建议， 请勿跟票 ） ：」
BARE_DISCLAIMER_RE = re.compile(
    r"^[（(]?\s*记\s*录我自己(?:明天观察的标的|的操作)\s*[，,]\s*非操作建议\s*"
    r"(?:[，,]\s*请\s*勿\s*跟?\s*票\s*)?[）)]?\s*[：:]?\s*$"
)

# 留言区起始边界。实测留言区平均占全文 73%，其中读者留言 47% 必须丢弃，
# 作者本人回复 10% 是导师原话，单独提取成 qa_reply 块。
COMMENT_BOUNDARY = (
    "精选留言", "使用完整服务", "分享 留言 收藏 听过", "写留言", "留言区",
)

# 留言行形如「宁静致远来自」「小家四不像来自上海」「爱在冰川来自」。
# 昵称可以为空：匿名读者的留言行就是纯「来自广东」，实测 599 行、涉及 394 个文档。
# 早先写成 {1,40} 要求至少一个字符，这些行没被认成边界，后面的读者留言就被并进
# 上一位发言人的正文，污染了作者原话（实测答句 43 行、问句 10 行）。
# 正文里独占整行的「来自X」实测 0 命中，所以放宽到 {0,40} 不会误切正文。
COMMENT_AUTHOR_RE = re.compile(r"^(.{0,40}?)来自(?:[一-鿿]{2,4})?$")

# 留言行有时被拼到上一条内容的尾部：「…好吗？ 来自浙江」。
# 这类尾巴要从答案里剪掉，否则读者昵称和归属地会混进作者原话。
TRAILING_AUTHOR_RE = re.compile(r"\s+\S{0,40}?来自(?:[一-鿿]{2,4})?\s*$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def clean_wechat_markdown(text: str) -> str:
    """微信 .md -> 纯正文。顺序有讲究：先去块级语法，再逐行过滤。"""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = t.replace("​", "").replace("﻿", "").replace("\xa0", " ")

    # 1) 图片整体删掉（含 ![cover_image](...)）；图片不参与文本检索
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    # 2) 链接只留锚文本，丢掉 URL
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    # 3) 残留裸链接
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"\bmmbiz\.\S+", "", t)
    # 4) Markdown 强调符号：微信导出会写成 ** 一：** 这种带空格的畸形粗体
    t = re.sub(r"\*+", "", t)
    t = re.sub(r"_{2,}", "", t)
    t = re.sub(r"~{2,}", "", t)          # 删除线残留，常粘在小节标题尾部
    # 5) 分隔线。微信会把它转义成 \--------- ，反斜杠要一起吃掉
    t = re.sub(r"^\s*\\?[-=~_]{3,}\s*$", "", t, flags=re.M)
    # 6) 微信表情占位符。只删白名单内的词，方括号本身要留给作者的强调用法
    t = EMOJI_RE.sub(lambda m: "" if m.group(1) in EMOJI_TOKENS else m.group(0), t)

    lines: list[str] = []
    for raw_line in t.split("\n"):
        s = raw_line.strip()
        if not s:
            lines.append("")
            continue
        if s in JUNK_LINES:
            continue
        # 纯符号行（含被转义的分隔线 \-----）
        if re.fullmatch(r"[\s#\-_=~·•◆▲★☆|\\/]+", s):
            continue
        # 公众号署名行（可能重复多次）
        if re.fullmatch(r"爱在冰川(\s+爱在冰川)*", s):
            continue
        # 发布时间行 _2021年10月28日 00:01_ __ _ _ _ 江苏 _
        if re.match(r"^_?\d{4}年\d{2}月\d{2}日\s*\d{2}:\d{2}_?[\s_]*[一-鿿]{0,6}[\s_]*$", s):
            continue
        # 标题行前的 # 保留文字本身
        s = re.sub(r"^#+\s*", "", s).strip()
        if not s:
            continue
        if any(r.match(s) for r in BOILERPLATE_RE):
            continue
        # 免责修饰语独占整行（无标题主干），整行丢掉
        if BARE_DISCLAIMER_RE.match(s):
            continue
        # 免责修饰语粘在小节标题里：删修饰语，保留「个股展望」这类主干
        if INLINE_DISCLAIMER_RE.search(s):
            s = INLINE_DISCLAIMER_RE.sub("", s).strip()
            s = re.sub(r"^[：:，,、\s]+", "", s)
            s = re.sub(r"[：:，,、\s]+$", "", s).strip()
            if not s:
                continue
        # 微信把换行编码成字面 n 的伪影：行首孤立 n
        s = re.sub(r"^n{1,3}\s*", "", s)
        if not s:
            continue
        lines.append(s)

    t = "\n".join(lines)
    # 折叠空行
    while "\n\n\n" in t:
        t = t.replace("\n\n\n", "\n\n")
    # 全角空格与重复空格
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


def cjk_count(text: str) -> int:
    return len(re.findall(r"[一-鿿]", text))


def strip_leading_title(text: str, title: str) -> str:
    """删掉正文开头重复文件名标题的那一行。

    实测 40/40 抽样篇首行都是标题的复述（如「2024-7-3 数据」），
    留着会产生 12 字符的碎块，也让 chunk 首行没有信息量。
    """
    lines = text.split("\n")
    if not lines:
        return text
    key = lambda s: re.sub(r"[^一-鿿0-9A-Za-z]", "", s)
    head, want = key(lines[0]), key(title)
    if head and want and (head == want or head in want or want in head):
        return "\n".join(lines[1:]).strip()
    # 首行极短且不含句读，多半也是标题变体
    if len(key(lines[0])) <= 14 and not re.search(r"[。，、；？！]", lines[0]):
        if want and key(lines[0])[:6] and key(lines[0])[:6] in want:
            return "\n".join(lines[1:]).strip()
    return text


def split_body_and_comments(text: str) -> tuple[str, list[str]]:
    """把清洗后的全文切成 (正文, 留言区行列表)。

    边界优先用「精选留言」这类显式标记；没有标记时退化为首个留言行。
    """
    lines = [line for line in text.split("\n") if line.strip()]
    cut = None
    for index, line in enumerate(lines):
        if line.strip() in COMMENT_BOUNDARY:
            cut = index
            break
    if cut is None:
        # 没有显式标记时才退化到「昵称行」判断。不能见到第一个就切：
        # 正文里「这个月的最高利润来自三个股」这类句子同样匹配，会截断正文。
        # 真留言区是昵称行密集出现，所以要求 6 行窗口内至少 3 个昵称行。
        for index, line in enumerate(lines):
            if not COMMENT_AUTHOR_RE.match(line.strip()):
                continue
            window = lines[index:index + 6]
            hits = sum(1 for item in window if COMMENT_AUTHOR_RE.match(item.strip()))
            if hits >= 3:
                cut = index
                break
    if cut is None:
        return "\n".join(lines), []
    return "\n".join(lines[:cut]), lines[cut:]


def extract_author_replies(comment_lines: list[str]) -> list[dict]:
    """从留言区提取「读者提问 -> 爱在冰川回复」配对。

    读者留言本身不入库（占全文约 47%，是噪声），但保留最近一条提问作为
    回复的上下文，否则单看回复不知道在答什么。
    """
    pairs: list[dict] = []
    pending_question = ""
    index = 0
    total = len(comment_lines)

    while index < total:
        match = COMMENT_AUTHOR_RE.match(comment_lines[index].strip())
        if not match:
            index += 1
            continue
        speaker = match.group(1).strip()
        cursor = index + 1
        buffer: list[str] = []
        while cursor < total and not COMMENT_AUTHOR_RE.match(comment_lines[cursor].strip()):
            candidate = comment_lines[cursor].strip()
            if candidate not in COMMENT_BOUNDARY and not any(
                    r.match(candidate) for r in BOILERPLATE_RE):
                buffer.append(candidate)
            cursor += 1
        body = " ".join(buffer).strip()
        # 剪掉粘在尾部的下一位留言者昵称
        for _ in range(2):
            trimmed = TRAILING_AUTHOR_RE.sub("", body).strip()
            if trimmed == body:
                break
            body = trimmed

        if speaker == SOURCE_NAME:
            if len(body) >= MIN_AUTHOR_REPLY:
                pairs.append({"question": pending_question, "answer": body})
            pending_question = ""
        elif body:
            pending_question = body
        index = cursor

    return pairs


# 正文里的小节标题：微信原文写成 ** 各 种 数 据 **，清洗后变成「各 种 数 据」
SECTION_HINTS = (
    "各种数据", "赚钱效应", "情绪周期", "市场情绪", "涨停板", "连板",
    "板块", "复盘", "题材", "龙头", "计划", "策略", "总结", "个股",
    "大盘", "指数", "资金", "明日", "今日", "盘面", "热点", "主线",
    # 删掉标题里的免责修饰语后露出来的主干，实测「方向展望」384 行、
    # 「个人记录」346 行，都是每日复盘的固定小节
    "方向展望", "个人记录", "个人操作", "持仓如下", "涨停动因", "闲聊", "趋势",
)


def looks_like_section(line: str) -> bool:
    """判断是否小节标题。微信小节常被写成字间加空格的粗体。"""
    s = line.strip()
    if not s or len(s) > 24:
        return False
    if s.endswith(("。", "，", "、", "；", "：", "？", "！", ",", ".")):
        return False
    compact = re.sub(r"\s+", "", s)
    if len(compact) > 14:
        return False
    # 字间空格是微信小节标题的强特征：「各 种 数 据」
    spaced = bool(re.fullmatch(r"(?:[一-鿿]\s+){2,}[一-鿿]", s))
    if spaced:
        return True
    # 「一：」「二、」这类序号小节。注意冒号后可能跟空格，不能要求紧邻非空白
    if re.match(r"^[一二三四五六七八九十]+\s*[：:、.]", compact):
        return True
    if re.match(r"^\d{1,2}\s*[：:、.]", compact) and len(compact) <= 14:
        return True
    return any(h in compact for h in SECTION_HINTS) and len(compact) <= 10


def para_locator(items: list[dict]) -> str:
    """由段落真实序号生成定位串。"""
    if not items:
        return "正文"
    first, last = items[0]["index"], items[-1]["index"]
    return f"正文第{first}段" if first == last else f"正文第{first}段—正文第{last}段"


def split_sections(text: str) -> list[dict]:
    """按小节切成 parent 单元；没有小节的文档整体作为一个单元。

    每个段落带真实序号，小节标题也占一个序号。必须这样：
    标题行不进 paras，若按 paras 顺序数位置，标题之后的段号会整体前移，
    引用就指错段了。
    """
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    if not paragraphs:
        return []

    sections: list[dict] = []
    heading = ""
    heading_index = 0
    current: list[dict] = []

    def flush() -> None:
        if not current and not heading:
            return
        sections.append({
            "heading": heading,
            "heading_index": heading_index,
            "paras": list(current),
            "text": "\n".join(item["text"] for item in current),
            "locator": para_locator(current) if current else f"正文第{heading_index}段",
        })

    for index, para in enumerate(paragraphs, start=1):
        if looks_like_section(para):
            flush()
            heading = re.sub(r"\s+", "", para)
            heading_index = index
            current = []
            continue
        current.append({"index": index, "text": para})

    flush()
    return merge_short_sections(sections)


def merge_short_sections(sections: list[dict]) -> list[dict]:
    """把过短的小节并进相邻小节。

    merge_short_units 只在单个小节内部合并，遇到整节只有一句话的
    （例如「大盘 / 见闲聊。」）无从下手，会留下 7 字符的碎块。
    这里在建 parent 之前先把这类小节并掉，被并掉的小节标题降级成正文行，
    内容不丢。
    """
    if not sections:
        return sections

    def absorb(host: dict, guest: dict) -> None:
        """把 guest 并进 host：guest 的标题降级成正文行，段号保持真实。"""
        extra = list(guest["paras"])
        if guest["heading"]:
            extra = [{"index": guest["heading_index"], "text": guest["heading"]}] + extra
        host["paras"] = host["paras"] + extra
        host["text"] = "\n".join(item["text"] for item in host["paras"])
        host["locator"] = para_locator(host["paras"])

    merged: list[dict] = []
    for section in sections:
        if len(section["text"]) < MIN_CHUNK_CHARS and merged:
            absorb(merged[-1], section)
            continue
        merged.append(dict(section))

    # 首节过短且当时没有前节可并：并进后一节
    while len(merged) > 1 and len(merged[0]["text"]) < MIN_CHUNK_CHARS:
        absorb(merged[0], merged[1])
        del merged[1]

    return merged


def build_chunk_units(section: dict) -> list[dict]:
    """把一个 section 的段落切成 chunk 单元，locator 精确到段号。

    段落本身超过 MAX_CHUNK_CHARS 时用项目的 split_text 再切，
    这样长段不会撑爆单块，也不会丢内容。
    """
    units: list[dict] = []
    buffer: list[dict] = []

    def flush() -> None:
        if not buffer:
            return
        units.append({
            "text": "\n".join(item["text"] for item in buffer),
            "locator": para_locator(buffer),
        })
        buffer.clear()

    for item in section["paras"]:
        para = item["text"]
        # 单段就超长：先把已积累的吐出，再把这段自身切开
        if len(para) > MAX_CHUNK_CHARS:
            flush()
            for piece in split_text(para, TARGET_CHARS):
                units.append({"text": piece, "locator": f"正文第{item['index']}段"})
            continue

        if buffer and sum(len(x["text"]) for x in buffer) + len(para) > TARGET_CHARS:
            flush()

        buffer.append(item)

    flush()

    # 过短的相邻块合并，保证每块能独立读懂
    merged = merge_short_units(
        units, min_chars=MIN_CHUNK_CHARS, max_chars=MAX_CHUNK_CHARS,
        text_key="text", locator_key="locator",
    )

    # merge_short_units 累积到 min_chars 就立即吐出，末尾剩下的短单元
    # 没有往回并的机会（实测出现「祝好」这种 2 字符块）。这里补一次反向合并。
    result: list[dict] = []
    for unit in merged:
        if (result and len(unit["text"]) < MIN_CHUNK_CHARS
                and len(result[-1]["text"]) + len(unit["text"]) <= MAX_CHUNK_CHARS):
            host = result[-1]
            host["text"] = host["text"] + "\n" + unit["text"]
            host["locator"] = span_locator(host["locator"], unit["locator"])
            continue
        result.append(dict(unit))
    return result


def parse_name(path: Path) -> dict | None:
    """从文件名解出发布日期与标题。"""
    match = NAME_RE.match(path.stem)
    if not match:
        return None
    year, month, day, hour, minute, title = match.groups()
    return {
        "date": f"{year}-{month}-{day}",
        "time": f"{hour}:{minute}",
        "title": title.strip() or path.stem,
    }


def classify_content(title: str) -> str:
    """内容类型，供检索时按体裁过滤。"""
    zh = re.sub(r"[^一-鿿]", "", title)
    if "鱼块" in zh:
        return "weekend_essay"
    if "复盘" in zh:
        return "daily_review"
    if "数据" in zh and len(zh) <= 6:
        return "daily_data"
    return "article"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="占位参数，保持与其他导入器一致的调用形式")
    parser.add_argument("--limit", type=int, default=0,
                        help="只处理前 N 个文件，用于试导入")
    parser.add_argument("--out", default="",
                        help="改写输出目录，试导入时避免覆盖正式产物")
    args = parser.parse_args()

    lib = Path(args.out) if args.out else LIB
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "texts").mkdir(exist_ok=True)

    files = sorted(SOURCE_ROOT.glob("*.md"), key=lambda p: p.name)
    if args.limit:
        # 跨年份取样，避免只测到同一时期的格式
        step = max(1, len(files) // args.limit)
        files = files[::step][:args.limit]
    if not files:
        raise SystemExit(f"来源目录没有 .md：{SOURCE_ROOT}")

    document_rows: list[dict] = []
    parent_rows: list[dict] = []
    chunk_rows: list[dict] = []
    skipped: list[dict] = []
    content_counter: Counter = Counter()
    topic_counter: Counter = Counter()
    stat_body_chars = 0
    stat_reply_chars = 0
    stat_dropped_chars = 0
    stat_reply_count = 0

    print(f"来源目录 {SOURCE_ROOT}")
    print(f"待处理 {len(files)} 个 .md -> 输出 {lib}")

    for index, path in enumerate(files, start=1):
        meta = parse_name(path)
        if meta is None:
            skipped.append({"file": path.name, "reason": "文件名不符合 [日期]标题 格式"})
            continue

        raw = path.read_text(encoding="utf-8", errors="replace")
        cleaned = clean_wechat_markdown(raw)
        body, comment_lines = split_body_and_comments(cleaned)
        body = strip_leading_title(body, meta["title"])
        replies = extract_author_replies(comment_lines)

        if cjk_count(body) < MIN_DOC_CHARS and not replies:
            skipped.append({"file": path.name,
                            "reason": f"清洗后正文过短（中文 {cjk_count(body)} 字）且无作者回复"})
            continue

        digest = sha256_file(path)
        document_id = f"azbc-{digest[:12]}"
        sections = split_sections(body) if cjk_count(body) >= MIN_DOC_CHARS else []
        if not sections and not replies:
            skipped.append({"file": path.name, "reason": "清洗后无可用段落"})
            continue

        reply_chars = sum(len(r["answer"]) for r in replies)
        reader_dropped = sum(len(x) for x in comment_lines) - reply_chars
        stat_body_chars += len(body)
        stat_reply_chars += reply_chars
        stat_dropped_chars += max(0, reader_dropped)
        stat_reply_count += len(replies)

        reply_text = "\n".join(item["answer"] for item in replies)
        content_type = classify_content(meta["title"])
        topics = infer_topics(meta["title"], body + "\n" + reply_text)
        content_counter[content_type] += 1
        topic_counter.update(topics)

        # 归一化文本留档：正文 + 作者回复，与入库内容一致，便于回看
        archive = body
        if replies:
            archive += "\n\n=== 作者留言区回复 ===\n" + "\n\n".join(
                (f"问：{item['question']}\n答：{item['answer']}"
                 if item["question"] else f"答：{item['answer']}")
                for item in replies)
        text_path = lib / "texts" / f"{document_id}.txt"
        write_text_lf(text_path, archive + "\n")

        unit_total = 0
        for section_index, section in enumerate(sections, start=1):
            parent_id = f"{document_id}-p{section_index:03d}"
            parent_text = ((section["heading"] + "\n") if section["heading"] else "") + section["text"]
            parent_rows.append({
                "source_id": SOURCE_ID,
                "document_id": document_id,
                "parent_id": parent_id,
                "title": meta["title"],
                "date": meta["date"],
                "author_or_guest": SOURCE_NAME,
                "locator": section["locator"],
                "text": parent_text,
            })

            units = build_chunk_units(section)
            for chunk_index, unit in enumerate(units, start=1):
                unit_total += 1
                chunk_text = unit["text"]
                # 小节标题拼进块首，让单块脱离上下文也知道在讲什么
                if section["heading"] and not chunk_text.startswith(section["heading"]):
                    chunk_text = f"{section['heading']}\n{chunk_text}"
                chunk_rows.append({
                    "source_id": SOURCE_ID,
                    "source_name": SOURCE_NAME,
                    "document_id": document_id,
                    "parent_id": parent_id,
                    "chunk_id": f"{parent_id}-c{chunk_index:02d}",
                    "chunk_type": content_type,
                    "title": meta["title"],
                    "date": meta["date"],
                    "author_or_guest": SOURCE_NAME,
                    "topics": topics,
                    "claim_type": "opinion_or_case",
                    "market_regime": "未标注",
                    "locator": unit["locator"],
                    "text": chunk_text,
                    "original_path": str(path),
                    "image_path": "",
                    "confidence": "high",
                    "extraction_method": "wechat_markdown",
                })

        # 作者在留言区的回复：读者提问 + 川哥作答，单独成块。
        # 这是导师原话，价值密度高于复盘正文，但载体不同，用 qa_reply 区分。
        if replies:
            parent_id = f"{document_id}-q001"
            paired = [(f"问：{item['question']}\n答：{item['answer']}"
                       if item["question"] else f"答：{item['answer']}")
                      for item in replies]
            parent_rows.append({
                "source_id": SOURCE_ID,
                "document_id": document_id,
                "parent_id": parent_id,
                "title": meta["title"],
                "date": meta["date"],
                "author_or_guest": SOURCE_NAME,
                "locator": f"留言区第1条—第{len(replies)}条",
                "text": "\n\n".join(paired),
            })

            reply_units: list[dict] = []
            buffer: list[str] = []
            buf_start = 1
            for position, block in enumerate(paired, start=1):
                if buffer and sum(len(x) for x in buffer) + len(block) > TARGET_CHARS:
                    reply_units.append({
                        "text": "\n\n".join(buffer),
                        "locator": (f"留言区第{buf_start}条" if buf_start == position - 1
                                    else f"留言区第{buf_start}条—第{position - 1}条"),
                    })
                    buffer, buf_start = [], position
                buffer.append(block)
            if buffer:
                reply_units.append({
                    "text": "\n\n".join(buffer),
                    "locator": (f"留言区第{buf_start}条" if buf_start == len(paired)
                                else f"留言区第{buf_start}条—第{len(paired)}条"),
                })

            for chunk_index, unit in enumerate(reply_units, start=1):
                unit_total += 1
                chunk_rows.append({
                    "source_id": SOURCE_ID,
                    "source_name": SOURCE_NAME,
                    "document_id": document_id,
                    "parent_id": parent_id,
                    "chunk_id": f"{parent_id}-c{chunk_index:02d}",
                    "chunk_type": "qa_reply",
                    "title": meta["title"],
                    "date": meta["date"],
                    "author_or_guest": SOURCE_NAME,
                    "topics": topics,
                    "claim_type": "opinion_or_case",
                    "market_regime": "未标注",
                    "locator": unit["locator"],
                    "text": unit["text"],
                    "original_path": str(path),
                    "image_path": "",
                    "confidence": "high",
                    "extraction_method": "wechat_comment_reply",
                })

        document_rows.append({
            "source_id": SOURCE_ID,
            "document_id": document_id,
            "title": meta["title"],
            "date": meta["date"],
            "author_or_guest": SOURCE_NAME,
            "content_type": content_type,
            "topics": topics,
            "characters": len(body),
            "author_reply_count": len(replies),
            "author_reply_characters": reply_chars,
            "unit_count": unit_total,
            "original_path": str(path),
            "related_original_paths": [],
            "normalized_text_path": str(text_path),
            "sha256": digest,
            "risk_flags": "图片未OCR，图表内容需回看原文",
        })

        if index % 400 == 0:
            print(f"  已处理 {index}/{len(files)}  累计 {len(chunk_rows)} 块")

    write_jsonl(lib / "documents.jsonl", document_rows)
    write_jsonl(lib / "parents.jsonl", parent_rows)
    write_jsonl(lib / "chunks.jsonl", chunk_rows)

    total_chars = sum(len(row["text"]) for row in chunk_rows)
    lengths = sorted(len(row["text"]) for row in chunk_rows)
    summary = {
        "source_id": SOURCE_ID,
        "source_name": SOURCE_NAME,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files_seen": len(files),
        "documents": len(document_rows),
        "parents": len(parent_rows),
        "chunks": len(chunk_rows),
        "chunk_characters": total_chars,
        "chunk_chars_avg": round(total_chars / len(chunk_rows), 1) if chunk_rows else 0,
        "chunk_chars_median": lengths[len(lengths) // 2] if lengths else 0,
        "chunk_chars_min": lengths[0] if lengths else 0,
        "chunk_chars_max": lengths[-1] if lengths else 0,
        "skipped": len(skipped),
        "content_types": dict(content_counter),
        "chunk_types": dict(Counter(row["chunk_type"] for row in chunk_rows)),
        "body_characters": stat_body_chars,
        "author_reply_characters": stat_reply_chars,
        "author_reply_count": stat_reply_count,
        "reader_comment_characters_dropped": stat_dropped_chars,
        "extraction_method": "wechat_markdown + wechat_comment_reply",
    }
    write_text_lf(lib / "source_summary.json",
                  json.dumps(summary, ensure_ascii=False, indent=2) + "\n")

    content_map = ["# 爱在冰川内容地图", "",
                   "| 日期 | 标题 | 类型 | 主题 | 字符数 | 块数 | 文档ID |",
                   "|---|---|---|---|---:|---:|---|"]
    for row in sorted(document_rows, key=lambda item: (item["date"], item["title"])):
        title = row["title"].replace("|", "丨")
        content_map.append(
            f"| {row['date']} | {title} | {row['content_type']} | "
            f"{'、'.join(row['topics'])} | {row['characters']} | "
            f"{row['unit_count']} | {row['document_id']} |")
    write_text_lf(lib / "content_map.md", "\n".join(content_map) + "\n")

    quality = [
        "# 爱在冰川导入质量报告", "",
        f"- 生成时间：{summary['generated_at']}",
        f"- 来源目录：`{SOURCE_ROOT}`",
        f"- 扫描文件：{len(files)}，成功 {len(document_rows)}，跳过 {len(skipped)}",
        f"- 层级产物：documents {len(document_rows)} / parents {len(parent_rows)} / chunks {len(chunk_rows)}",
        f"- 块字符：合计 {total_chars:,}，均值 {summary['chunk_chars_avg']}，"
        f"中位 {summary['chunk_chars_median']}，区间 {summary['chunk_chars_min']}–{summary['chunk_chars_max']}",
        f"- 内容类型：{dict(content_counter)}",
        f"- 块类型：{dict(Counter(row['chunk_type'] for row in chunk_rows))}",
        "",
        "## 正文与留言区的拆分",
        f"- 正文入库 {stat_body_chars:,} 字符。",
        f"- 作者留言区回复入库 {stat_reply_chars:,} 字符，共 {stat_reply_count:,} 条，"
        "标为 `chunk_type: qa_reply`。",
        f"- 读者留言丢弃 {stat_dropped_chars:,} 字符。",
        "",
        "微信导出的 .md 把「精选留言」整块塞在正文之后，实测留言区平均占全文 73%。",
        "其中读者留言是噪声，但作者本人的回复是导师原话（问答配对，价值密度高于复盘"
        "正文），因此单独提取成 `qa_reply` 块，并保留最近一条读者提问作为上下文。",
        "",
        "## 清洗说明",
        "- 微信导出的 .md 中图片语法、URL、样板行占原文约 77%，本导入器整体删除。",
        "- 图片未做 OCR：正文里的数据表格和涨停梯队图以图片形式存在，检索不到，需回看原文。",
        "- 每篇固定的免责声明与署名行已移除，避免 2584 篇重复文本污染检索。",
        "- 正文首行重复文件名标题的那一行已删除（实测抽样 40/40 篇都重复）。",
        "",
        "## 已排除内容（用户 2026-08-04 决定）",
        "- 资讯汇总 621 篇：正文几乎只有外链列表。",
        "- 短线资金流：正文为图片，无文字价值，已在来源目录外单独存放。",
        "- 无正文空壳 79 篇：清洗后中文不足 100 字。",
        "",
        "## 跳过明细",
    ]
    if skipped:
        quality.append("| 文件 | 原因 |")
        quality.append("|---|---|")
        for item in skipped[:80]:
            quality.append(f"| {item['file'].replace('|', '丨')} | {item['reason']} |")
        if len(skipped) > 80:
            quality.append(f"| … | 其余 {len(skipped) - 80} 条略 |")
    else:
        quality.append("无。")
    write_text_lf(lib / "quality_report.md", "\n".join(quality) + "\n")

    print("-" * 62)
    print(f"documents {len(document_rows)}  parents {len(parent_rows)}  chunks {len(chunk_rows)}")
    print(f"块字符合计 {total_chars:,}  均值 {summary['chunk_chars_avg']}  "
          f"中位 {summary['chunk_chars_median']}  区间 {summary['chunk_chars_min']}–{summary['chunk_chars_max']}")
    print(f"正文 {stat_body_chars:,} 字符  作者回复 {stat_reply_chars:,} 字符"
          f"（{stat_reply_count:,} 条）  读者留言丢弃 {stat_dropped_chars:,} 字符")
    print(f"跳过 {len(skipped)}  内容类型 {dict(content_counter)}")
    print(f"块类型 {dict(Counter(row['chunk_type'] for row in chunk_rows))}")
    print(f"高频主题 {topic_counter.most_common(8)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
