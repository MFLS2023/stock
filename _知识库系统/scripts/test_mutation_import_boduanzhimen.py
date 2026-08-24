# -*- coding: utf-8 -*-
"""变异测试 v2：在同一进程内用改写后的源码建模块，不落盘、不碰字节码缓存。

v1 靠改写源文件 + 子进程跑测试，结果在两次运行间不一致（同一变异一次"抓住"
一次"漏网"）—— 根因是 .pyc 复用，清缓存也没彻底解决。
这一版改用 exec 把变异后的源码编译成独立模块对象，测试用例直接调它的函数，
不写磁盘，也就没有缓存这一环。
"""
import pathlib
import re
import sys
import types
import unittest

ROOT = pathlib.Path(r"C:\Users\20577\Documents\炒股\知识库\_知识库系统")
SOURCE = (ROOT / "scripts" / "import_boduanzhimen.py").read_text(encoding="utf-8")
sys.path.insert(0, str(ROOT / "scripts"))

# 从测试文件里取用例，保证变异测的就是正式测试断言的那批
CASES_KEPT = [
    "明天大盘先跌后涨然后再跌，最后收阳这是我的判断你们。",              # 26 字 2 标点
    "明天大盘先跌后涨然后再，跌最后收阳这是我的判断。",                  # 24 字 2 标点
    "明天大盘先跌后涨然后再跌最，后收阳这是我的判断你们自己。",          # 28 字 2 标点
    "看到上面的黄线了吗?那里是3090附近,这是这次上涨的第一理论目标。",
    "亻乍者短线的底,大概率会破掉3356,抄底不宜过早。公众号·波段之门",
    "今日开盘就跌,最新指数已经破了3900点,明天怎么走?我也不知道。",
]
CASES_DROPPED = [
    "明天大盘先跌后涨然后再跌最后收阳这是我的判断你们自己看着办。",      # 30 字 1 标点
    "明天大盘先跌后涨然后再跌最后收阳这是我的判断你们自己看着办不要问我了反正就这。",  # 39 字 1 标点
    "明天大盘先跌后涨然后，再跌最后收阳这是我的判。",                    # 23 字 2 标点
    "方解石Calcite中国.内蒙古自治区.克什克腾旗·黄冈梁铁矿五采区公众号。波段之门",
    "上指訕旧线湔0342443354的2.3341的囗3囗5g50中顶部波段之门",
    "A股成交B股成交国债成交基金成交权证成交最新指数今日开盘昨日收盘",
    "上证指数最新指数今日开盘3930点昨日收盘3921点整体表现平稳今天没有太大波动",
    "公众号·波段之门",
    # 必须够长（65 字）才能测到 low_cjk：短的会先被 too_short 拦住
    "11:28 11:28 11:28 3930.42 3921.15 3950.80 0.23% 1.63% 2.05% 4021.33 3877.90",
    # 中文比 0.32：落在 0.45 之下但离 0 有距离，这样 min_cjk 放宽到 0.20 时能测出来
    "沪指3930深成12045创业2456科创1122北证0899合计26452今开3921昨收3877量比1.63",
]

# 已知会漏网且原因明确的变异 —— 不是断言不足，是两道长度门槛串联同构：
# 第一道 too_short 和第二道 watermark_only 都要求「>=2 标点且 >=24 字」，
# 只改一道另一道仍会拦下同一批文本，最终结果不变。
KNOWN_REDUNDANT = {
    "第一道 句读 >=2 → >=1",
    "第一道 系数 0.6 → 0.4",
    "第一道 取消句读要求",
    # 等价变异，逻辑上不可观测：这行只在 run_index == 1 的分支里执行，
    # 而 run_index == 1 意味着前一条必然是读者发言（否则 run 不会被重置为 1），
    # 所以 entries[index-1] 与 entries[index-2:index] 在该分支内结果相同。
    # 实测两种场景（作者开头连发、读者后连发 3 条）输出逐位一致。
    "配对 紧邻判定放宽到前两条",
}

MUTATIONS = [
    ("第一道 句读 >=2 → >=1",
     'looks_like_prose = len(re.findall(r"[。！？；，,、?]", compact)) >= 2',
     'looks_like_prose = len(re.findall(r"[。！？；，,、?]", compact)) >= 1'),
    ("第一道 系数 0.6 → 0.4",
     "if not (looks_like_prose and len(compact) >= min_chars * 0.6):",
     "if not (looks_like_prose and len(compact) >= min_chars * 0.4):"),
    ("第一道 系数 0.6 → 0.9",
     "if not (looks_like_prose and len(compact) >= min_chars * 0.6):",
     "if not (looks_like_prose and len(compact) >= min_chars * 0.9):"),
    ("第一道 取消句读要求",
     "if not (looks_like_prose and len(compact) >= min_chars * 0.6):",
     "if not (len(compact) >= min_chars * 0.6):"),
    ("水印道 句读 >=2 → >=1",
     'has_sentence_punct = len(re.findall(r"[。！？；，,、?]", without_mark)) >= 2',
     'has_sentence_punct = len(re.findall(r"[。！？；，,、?]", without_mark)) >= 1'),
    ("水印道 系数 0.6 → 1.0（恢复缺陷）",
     "if not (has_sentence_punct and len(without_mark) >= min_chars * 0.6):",
     "if not (has_sentence_punct and len(without_mark) >= min_chars * 1.0):"),
    ("min_cjk 0.45 → 0.20", "min_cjk: float = 0.45", "min_cjk: float = 0.20"),
    # 配对逻辑（P1-6）：这三个变异分别恢复原缺陷、去掉打断重置、放宽紧邻判定
    ("配对 恢复原缺陷（无条件往前找）",
     "        if run_index == 1:\n"
     "            # 紧邻的前一条若是读者留言才算提问；留言区以作者发言开头时前面没有读者\n"
     "            if index > 0 and entries[index - 1][\"speaker\"] != AUTHOR:\n"
     "                prior = entries[index - 1]\n"
     "                question = f\"{prior['speaker']}：{prior['text']}\"",
     "        if True:\n"
     "            for prior in reversed(entries[:index]):\n"
     "                if prior[\"speaker\"] != AUTHOR:\n"
     "                    question = f\"{prior['speaker']}：{prior['text']}\"\n"
     "                    break"),
    ("配对 读者发言不再重置 run_index",
     "            run_index = 0\n            run_first_order = 0\n            continue",
     "            continue"),
    ("配对 紧邻判定放宽到前两条",
     'if index > 0 and entries[index - 1]["speaker"] != AUTHOR:',
     'if index > 0 and any(e["speaker"] != AUTHOR for e in entries[max(0, index - 2):index]):'),
    ("面板 panel_hits >=2 → >=5",
     "if panel_hits >= 2 and not re.search", "if panel_hits >= 5 and not re.search"),
]


def load(source: str):
    """把源码编译成独立模块（避免执行 __main__ 逻辑），返回模块本身。"""
    module = types.ModuleType("mutant")
    module.__dict__["__file__"] = str(ROOT / "scripts" / "import_boduanzhimen.py")
    exec(compile(source, "<mutant>", "exec"), module.__dict__)
    return module


def _comments(*pairs: tuple[str, str]) -> str:
    """按原始 md 的留言区格式拼字符串：`<昵称>来自<地区>` 一行，正文一行。"""
    lines = []
    for speaker, text in pairs:
        lines += ["波段之门来自" if speaker == "波段之门" else f"{speaker}来自湖北",
                  "", text, ""]
    return "\n".join(lines)


# 留言区配对的期望（审查报告 P1-6）。每项是 (说明, 留言序列, 期望的 question 有无)
PAIRING_CASES = [
    ("紧邻读者留言要配对",
     (("关昌虎", "谢谢提醒"), ("波段之门", "我写了好几行字。")),
     [True]),
    ("连发第 2 条不能挂到同一条留言",
     (("关昌虎", "谢谢提醒"), ("波段之门", "我写了好几行字。"),
      ("波段之门", "明天理论上有新高，要减仓。")),
     [True, False]),
    ("读者发言打断连发后要重新配对",
     (("甲", "问题一"), ("波段之门", "答一"), ("波段之门", "补充"),
      ("乙", "问题二"), ("波段之门", "答二")),
     [True, False, True]),
    ("留言区以作者开头时没有提问",
     (("波段之门", "先说一句。"), ("波段之门", "再补充。"), ("甲", "收到"),
      ("波段之门", "嗯")),
     [False, False, True]),
]


def failures(module) -> list[str]:
    bad = []
    ocr = module.chart_ocr_is_useful
    for text in CASES_KEPT:
        keep, reason = ocr(text)
        if not keep:
            bad.append(f"该留却丢({reason}): {text[:30]}")
    for text in CASES_DROPPED:
        keep, _ = ocr(text)
        if keep:
            bad.append(f"该丢却留: {text[:30]}")
    pair = module.extract_author_replies
    for note, sequence, expected in PAIRING_CASES:
        replies = pair(_comments(*sequence))
        got = [bool(r["question"]) for r in replies]
        if got != expected:
            bad.append(f"配对错({note}): 期望 {expected} 实际 {got}")
    return bad


def run() -> int:
    """跑全部变异，返回退出码。

    必须包在函数里并由 `if __name__` 守着 —— 文件名是 test_*.py，
    `unittest discover` 会 import 它。模块级代码在 import 时就会执行，
    而这里还有 sys.exit()，会直接中断整个主套件
    （留档第 4 条记录过同样的坑：test_mutation_boduanzhimen.py 当时
    一被 import 就真的去改写指标源文件，主套件冒出 14 个 error）。
    """
    baseline = failures(load(SOURCE))
    if baseline:
        print("基线就不干净，先修测试用例：")
        for item in baseline:
            print("  ", item)
        return 1
    print(f"基线：{len(CASES_KEPT) + len(CASES_DROPPED)} 条用例全部符合预期\n")

    caught, escaped = [], []
    for note, old, new in MUTATIONS:
        if old not in SOURCE:
            escaped.append(f"{note}（锚点未匹配）")
            print(f"  锚点缺失 {note}")
            continue
        bad = failures(load(SOURCE.replace(old, new, 1)))
        if bad:
            caught.append(note)
            print(f"  抓住   {note}  ← {bad[0]}")
        else:
            escaped.append(note)
            print(f"  漏网   {note}")
    return report(caught, escaped)


def report(caught: list[str], escaped: list[str]) -> int:
    unexpected = [item for item in escaped if item not in KNOWN_REDUNDANT]
    print(f"\n{len(caught)}/{len(MUTATIONS)} 个变异被抓住")
    if escaped:
        print("漏网明细：")
        for item in escaped:
            tag = "已知冗余，可接受" if item in KNOWN_REDUNDANT else "未预期，需补断言"
            print(f"  - {item}  [{tag}]")
    # 只有「未预期的漏网」算失败：已知冗余那三条原因写在 KNOWN_REDUNDANT 注释里
    return 1 if unexpected else 0


def _deps_ready() -> str:
    """检查 exec 导入器源码所需的依赖在不在（缺则返回原因）。

    load() 会 exec 整个 import_boduanzhimen.py，顶层 import pypdf。
    系统 Python312 没装，缺了要 skip 而不是 error，否则主套件被无关依赖拖挂。
    """
    try:
        load(SOURCE)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return ""


class MutationSuiteTest(unittest.TestCase):
    """让 discover 也能跑这套变异（作为一个用例聚合）。"""

    def test_mutations_are_caught(self) -> None:
        reason = _deps_ready()
        if reason:
            self.skipTest(f"缺依赖，跳过：{reason}")
        self.assertEqual(run(), 0, "有未预期的变异漏网，见上方输出")


if __name__ == "__main__":
    raise SystemExit(run())
