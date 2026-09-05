# OCR 坏块率汇总（2026-09-05）

判据：孤立部首 [忄亻讠钅纟饣彳氵灬扌宀辶阝卩刂] >=2 记坏块（保守口径）。
由 `scripts/report_ocr_quality.py` 生成；OCR 重跑或索引重建后手动执行。

| 层 | 块数 | 坏块 | 坏块率 |
|---|---:|---:|---:|
| K线图 OCR（boduanzhimen/chart_ocr） | 586 | 0 | 0.0% |
| 课程讲义 OCR（aizaibingchuan/course_ocr） | 13 | 0 | 0.0% |
| 南京路截图 OCR（nanjinglu_bian/screenshot_ocr） | 228 | 26 | 11.4% |
| 郁金香截图 OCR（tulip_garden/image_ocr） | 696 | 129 | 18.5% |
| 付费扫描页（boduanzhimen/paid_article） | 531 | 92 | 17.3% |

各层最长坏块样本（chunk_id 可回查原图核对）：

- K线图 OCR: （无坏块）
- 课程讲义 OCR: （无坏块）
- 南京路截图 OCR: nanjinglu-c0bf0cd2860c-p006-c01, nanjinglu-554254de485c-p002-c01, nanjinglu-554254de485c-p002-c02
- 郁金香截图 OCR: tulip-4dff27e4b75f-p002-c01, tulip-4dff27e4b75f-p007-c05, tulip-ecfb26a9b797-p002-c02
- 付费扫描页: boduanzhimen-paid-87a5ab88a659-p001-c04, boduanzhimen-paid-50ee183b101e-p001-c01, boduanzhimen-paid-c25d0f48de8d-p001-c02
