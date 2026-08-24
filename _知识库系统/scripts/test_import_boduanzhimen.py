#!/usr/bin/env python3
"""波段之门导入器的质量门槛测试。

## 为什么需要

审查报告 P1-1/P1-2 记录：`import_boduanzhimen.py` 此前**一行测试都没有**，
`chart_ocr_is_useful` 的两个字符门槛（min_chars / min_cjk）谁都能改，测试全绿。
而这两个门槛直接决定 3661 张 K 线图里哪些进库 —— 调错一个数字就会
批量丢掉他写在图上的判断，或批量放进行情软件面板的乱码。

## 门槛的判据是「有没有句读」，不是单纯的字数

这是本文件要守住的核心设计。实测分界线（2026-08-09，全量 3661 张 chart）：

    他写在图上的话   标点 2~7 个   「明天大概率还是震荡。这个b反,做的好了,小亏。」
    行情面板/商品标签 标点 0 个     「方解石Calcite中国.内蒙古自治区.克什克腾旗…」

所以放宽字数门槛时必须同时要求句读，否则噪声会一起进来
（实测 12 条 watermark_only 全部落在 32~39 字，任何低于 32 的纯字数门槛都会全收）。

## 依赖

`import_boduanzhimen` 顶层 import pypdf，系统 Python312 没装。
缺依赖时整个文件 skip，不让无关依赖把主套件拖挂。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from import_boduanzhimen import chart_ocr_is_useful, extract_author_replies
    IMPORT_ERROR = ""
except Exception as exc:                                    # pragma: no cover
    chart_ocr_is_useful = None                              # type: ignore[assignment]
    extract_author_replies = None                           # type: ignore[assignment]
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

WATERMARK = "公众号·波段之门"


@unittest.skipIf(chart_ocr_is_useful is None, f"缺依赖，跳过：{IMPORT_ERROR}")
class ChartOcrThresholdTests(unittest.TestCase):
    """OCR 质量门槛的双向断言：该收的收进来，该拦的仍拦住。"""

    def assert_kept(self, text: str, note: str) -> None:
        keep, reason = chart_ocr_is_useful(text)
        self.assertTrue(keep, f"{note} 应保留，实际被丢：{reason}｜{text[:50]}")

    def assert_dropped(self, text: str, note: str) -> None:
        keep, reason = chart_ocr_is_useful(text)
        self.assertFalse(keep, f"{note} 应丢弃，实际保留了｜{text[:50]}")
        self.assertTrue(reason, "丢弃时必须给出原因，否则无法复核")

    def test_short_judgements_with_punctuation_are_kept(self) -> None:
        """审查报告 P2-1：40 字门槛误杀他写在图上的短判断。

        这三条实测被 too_short 丢掉，而它们是完整的句子且带点位。
        """
        self.assert_kept("看到上面的黄线了吗?那里是3090附近,这是这次上涨的第一理论目标。", "34 字短判断")
        self.assert_kept("我们每天的底部是稍微有些移动的,明天的30分底部在3137点。", "31 字短判断")
        self.assert_kept("理论上来说,始于3332的反弹是可以接近3390点的。", "27 字短判断")

    def test_body_plus_watermark_is_not_watermark_only(self) -> None:
        """审查报告 P1-7：「38 字正文 + 6 字水印」被判成纯水印。

        去掉水印后拿剩余长度和**完整的** min_chars 比，于是正文被水印拖下门槛。
        实测 12 条 watermark_only 里 8 条是他的真实判断。
        """
        self.assert_kept(
            "明天大概率还是震荡。这个b反,做的好了,小亏。做不好,大亏。做的非常好,小贝" + WATERMARK,
            "正文 38 字 + 水印",
        )
        self.assert_kept(
            "亻乍者短线的底,大概率会破掉3356,抄底不宜过早。" + WATERMARK,
            "带点位的真实判断",
        )
        self.assert_kept(
            "下周要涨怎么办?下周要跌怎么办?都是一类操作,持股、换股、做T,就是不减仓。公众号波段之门",
            "正文 + 水印变体",
        )

    def test_pure_watermark_still_dropped(self) -> None:
        """放宽门槛不能把纯水印放进来。"""
        self.assert_dropped(WATERMARK, "纯水印")
        self.assert_dropped("分时线", "水印残留")
        self.assert_dropped("波段之门 波段之门 波段之门", "水印重复")

    def test_panel_noise_without_punctuation_stays_dropped(self) -> None:
        """没有句读的一律拦住 —— 这是放宽字数门槛的安全垫。

        商品标签和行情面板中文占比很高（每个字都是汉字），光靠 min_cjk 拦不住，
        靠的就是「标点 0 个」这个特征。
        """
        self.assert_dropped(
            "方解石Calcite中国.内蒙古自治区.克什克腾旗·黄冈梁铁矿五采区公众号。波段之门",
            "商品标签",
        )
        self.assert_dropped(
            "上指訕旧线湔0342443354的2.3341的囗3囗5g50中顶部波段之门",
            "面板乱码",
        )
        self.assert_dropped(
            "A股成交B股成交国债成交基金成交权证成交最新指数今日开盘昨日收盘",
            "面板字段名连排",
        )

    def test_empty_and_low_cjk_unaffected(self) -> None:
        """审查报告确认 empty 和 low_cjk 两类丢弃是对的，改动不该影响它们。

        测 low_cjk 的用例必须够长（>= 40 字），否则会先被 too_short 拦住，
        测出来的是 too_short 而不是 low_cjk —— 变异测试里 min_cjk 那条
        一直漏网就是因为用例只有 34 字。
        """
        self.assert_dropped("", "空")
        self.assert_dropped("   \n  ", "只有空白")
        # 65 字、中文比 0.00 → 唯一能拦它的是 low_cjk
        self.assert_dropped(
            "11:28 11:28 11:28 3930.42 3921.15 3950.80 0.23% 1.63% 2.05% 4021.33 3877.90",
            "长的纯数字刻度",
        )
        # 中文比 0.32 —— 落在 0.45 门槛之下但离 0 有距离，这样把 min_cjk 放宽到
        # 0.20 时本条会误留而变红。纯 0.00 的用例测不出阈值变化（改成 0.20 仍拦得住）。
        self.assert_dropped(
            "沪指3930深成12045创业2456科创1122北证0899合计26452今开3921昨收3877量比1.63",
            "中文比 0.32 的指数面板",
        )

    def test_two_length_gates_are_intentionally_redundant(self) -> None:
        """记录一个设计事实：两道长度门槛对 < 40 字的文本是串联同构的。

        第一道 too_short 和第二道 watermark_only 都要求「>= 2 标点且 >= 24 字」，
        所以只改其中一道不会改变最终结果 —— 变异测试里「第一道句读 >=2 → >=1」
        显示为漏网，根因就是这个冗余，不是断言写得不够。
        这不是缺陷（多一道保险），但改动任何一道时要知道另一道还在兜着。

        本测试守住这个冗余关系本身：40 字以下、标点不足的文本，
        无论走哪道都必须被拦下。
        """
        short_one_mark = "明天大盘先跌后涨然后再跌最后收阳这是我的判断你们自己看着办。"  # 30 字
        keep, reason = chart_ocr_is_useful(short_one_mark)
        self.assertFalse(keep, "30 字 1 标点必须被拦")
        self.assertIn(reason.split("(")[0], ("too_short", "watermark_only"),
                      f"应由两道长度门槛之一拦住，实际是 {reason}")

    def test_punctuation_requirement_is_exactly_two_marks(self) -> None:
        """句读门槛卡在「>= 2 个标点」，改成 >= 1 或取消句读要求都必须变红。

        用例必须落在 24~39 字：第一道门槛只在 len < min_chars(40) 时才走放宽分支，
        40 字以上根本不经过它，拿超长句子去测等于什么都没测（我第一版就是这样，
        变异测试里 4 个变异全漏网才发现）。
        两条同长同中文比、只差一个标点，把阈值卡死在 2 上。
        """
        # 30 字、1 个标点 → 丢
        self.assert_dropped("明天大盘先跌后涨然后再跌最后收阳这是我的判断你们自己看着办。", "30 字仅 1 标点")
        # 39 字、1 个标点 → 仍丢（证明不是靠长度过关）
        self.assert_dropped(
            "明天大盘先跌后涨然后再跌最后收阳这是我的判断你们自己看着办不要问我了反正就这。",
            "39 字仅 1 标点",
        )
        # 26 字、2 个标点 → 留
        self.assert_kept("明天大盘先跌后涨然后再跌，最后收阳这是我的判断你们。", "26 字 2 标点")

    def test_threshold_is_sixty_percent_of_min_chars(self) -> None:
        """放宽后的下限是 min_chars * 0.6 = 24 字，两侧各卡一条。

        改成 0.4（下限 16）会让 23 字那条误留；
        改成 0.9（下限 36）会让 24/26 字那两条误丢。
        用例由 scripts/test_import_boduanzhimen.py 按目标字数生成并校验，不靠手数。
        """
        # 恰好 24 字 2 标点 → 达到下限，保留
        self.assert_kept("明天大盘先跌后涨然后再，跌最后收阳这是我的判断。", "24 字，正好到下限")
        # 23 字 2 标点 → 差 1 字，丢
        self.assert_dropped("明天大盘先跌后涨然后，再跌最后收阳这是我的判。", "23 字，差 1 字到下限")
        # 28 字 2 标点 → 明确过线，保留（守住上界，防 0.9 那种改法）
        self.assert_kept("明天大盘先跌后涨然后再跌最，后收阳这是我的判断你们自己。", "28 字带句读")

    def test_panel_detection_threshold_is_two_hits(self) -> None:
        """面板判定卡在「命中 2 个以上面板词且无句读」，收紧到 >= 5 必须变红。

        「A股成交B股成交国债成交基金成交」这类字段名连排中文比 0.94、
        字数也够，前面三道门槛全过，只有 panel_hits 能拦。
        实测放宽第一道门槛后，有 244 张面板图改由这道拦住 —— 它现在是主力过滤。
        """
        # 恰好 2 个面板词、无句读 → 必须丢
        self.assert_dropped(
            "上证指数最新指数今日开盘3930点昨日收盘3921点整体表现平稳今天没有太大波动",
            "2 个面板词无句读",
        )
        # 面板词多但**有句读** → 是他在讲话，不该按面板丢
        self.assert_kept(
            "今日开盘就跌,最新指数已经破了3900点,明天怎么走?我也不知道。",
            "含面板词但有句读",
        )


@unittest.skipIf(extract_author_replies is None, f"缺依赖，跳过：{IMPORT_ERROR}")
class AuthorReplyPairingTests(unittest.TestCase):
    """留言区配对：只有紧跟读者留言的那条才算问答（审查报告 P1-6）。

    原实现无条件往前找第一个非作者发言，他连发时第 2、3… 条全挂到同一条留言上，
    实测 637 条 (12.8%) 的 question 是错的，涉及 243 篇。
    """

    @staticmethod
    def comments(*pairs: tuple[str, str]) -> str:
        """按原始 md 的留言区格式拼字符串：`<昵称>来自<地区>` 一行，正文一行。"""
        lines = []
        for speaker, text in pairs:
            lines += [f"{speaker}来自湖北" if speaker != "波段之门" else "波段之门来自",
                      "", text, ""]
        return "\n".join(lines)

    def test_reply_next_to_a_reader_comment_is_paired(self) -> None:
        """紧邻读者留言 → 配对，且 run_index 为 1。"""
        replies = extract_author_replies(self.comments(
            ("关昌虎", "谢谢提醒"),
            ("波段之门", "我写了好几行字，发出来只有四个字。"),
        ))
        self.assertEqual(len(replies), 1)
        self.assertIn("关昌虎", replies[0]["question"])
        self.assertEqual(replies[0]["run_index"], 1)
        self.assertEqual(replies[0]["run_first_order"], 0, "第 1 条不该自指")

    def test_consecutive_replies_are_not_paired_to_the_same_comment(self) -> None:
        """连发的第 2 条起 question 必须为空 —— 它回答的不是那条读者留言。

        实测样本 [2025-10-23] 「明天理论上有新高…要注意减掉一些仓位」是独立盘面判断，
        原来被标成在回答「谢谢提醒」。
        """
        replies = extract_author_replies(self.comments(
            ("关昌虎", "谢谢提醒"),
            ("波段之门", "我写了好几行字，发出来只有四个字。"),
            ("波段之门", "明天理论上有新高，要注意减掉一些仓位。"),
        ))
        self.assertEqual(len(replies), 2)
        self.assertIn("关昌虎", replies[0]["question"])
        self.assertEqual(replies[1]["question"], "", "连发第 2 条不该带提问")
        self.assertEqual(replies[1]["run_index"], 2)
        # 但要保留上下文：能回溯到这一串的首条（记序号，不复制正文）
        self.assertEqual(replies[1]["run_first_order"], replies[0]["order"])

    def test_reader_comment_resets_the_run(self) -> None:
        """读者发言打断连发，之后第一条重新算 run_index=1 并重新配对。"""
        replies = extract_author_replies(self.comments(
            ("甲", "问题一"),
            ("波段之门", "答一"),
            ("波段之门", "补充一句"),
            ("乙", "问题二"),
            ("波段之门", "答二"),
        ))
        self.assertEqual([r["run_index"] for r in replies], [1, 2, 1])
        self.assertIn("甲", replies[0]["question"])
        self.assertEqual(replies[1]["question"], "")
        self.assertIn("乙", replies[2]["question"],
                      "被读者打断后必须重新配对，不能继续挂在上一串")

    def test_comments_starting_with_author_have_no_question(self) -> None:
        """留言区以他自己开头时前面没有读者留言，question 必须为空。

        实测 6 篇是这种情况（如 [2026-03-04] 开头连发 4 条）。
        原实现这里本来就是对的，本测试防止改配对逻辑时把它弄坏。
        """
        replies = extract_author_replies(self.comments(
            ("波段之门", "先说一句，今天等待机会。"),
            ("波段之门", "再补充，别急着动手。"),
            ("甲", "收到"),
            ("波段之门", "嗯"),
        ))
        self.assertEqual(replies[0]["question"], "")
        self.assertEqual(replies[0]["run_first_order"], 0, "开头第 1 条不该自指")
        self.assertEqual(replies[1]["question"], "")
        self.assertEqual(replies[1]["run_index"], 2)
        self.assertIn("甲", replies[2]["question"])

    def test_order_is_continuous_across_runs(self) -> None:
        """order 是该文第几条作者回复，跨连发串连续，不因分串重置。"""
        replies = extract_author_replies(self.comments(
            ("甲", "问"), ("波段之门", "答"), ("波段之门", "续"),
            ("乙", "再问"), ("波段之门", "再答"),
        ))
        self.assertEqual([r["order"] for r in replies], [1, 2, 3])


if __name__ == "__main__":
    unittest.main(verbosity=2)
