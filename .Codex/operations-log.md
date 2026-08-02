# Operations Log

## 2026-08-01

- 用户授权开始实施多来源交易知识库。
- 采用一个 Codex 本地项目、原始资料只读、分来源知识层、跨来源层、个人日志隔离、实时数据 MCP 的架构。
- `codex-collaboration` 指定的旧版审查 MCP 当前不可用，改为在本项目内生成等价审查报告和评分留痕。
- 第一阶段使用 Markdown/JSONL + SQLite FTS5；向量检索延后到固定评测证明有必要时。
- 南京路彼岸完成 42 个物理文件接入：41 份唯一内容、1 份重复文件、343 个检索块；196 页 OCR、91 页内嵌文本、25 页内嵌文本回退，错误 0。
- 郁金香花园完成 377 个物理文件接入：42 个课程/文章单元、19 份 Word、356 张外部截图；旧 DOC 经 Word COM 转换，545 张外部及内嵌图片进入 OCR，生成 6393 个检索块，错误 0。
- 原始 manifest 最终为 529 个文件全部 unchanged，new/changed/removed 均为 0。
- 统一 SQLite 索引包含 194 文档、667 父块、8180 原始检索块，并另含 20 方法卡和 6 分歧卡；integrity_check 通过。
- 用户补充未来持续新增混合格式来源后，决定保留现有项目与功能角色，新增来源注册表、通用格式适配器、专用适配器闸门和动态跨来源覆盖图。
- 新增 `register_source.py`、`import_source.py`、`import_generic_source.py` 和新来源接入指南；通用适配器支持 MD/TXT/PDF/DOCX/JPG/PNG，复杂结构禁止静默降级。
- 修复五个项目 Skills 的 `agents/openai.yaml` 编码，全部通过 quick_validate。
- 最终 `validate_kb.py` 11/11 通过；典型查询“转点 卡位”“筹码 竞价”“弱转强”“仓位 回撤”均返回跨来源可追溯结果。
