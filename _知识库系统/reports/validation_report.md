# 知识库验证报告

- 通过：11/11
- 总体：通过

- ✅ 根 AGENTS.md：项目规则可被新对话自动发现
- ✅ 来源登记：登记来源数=3
- ✅ 文件 manifest：记录数=529
- ✅ 全部来源内容入库：{"fulibei": {"documents": 110, "parents": 441, "chunks": 1444}, "nanjinglu_bian": {"documents": 42, "parents": 72, "chunks": 343}, "tulip_garden": {"documents": 42, "parents": 154, "chunks": 6393}}
- ✅ 统一检索块：块数=8180
- ✅ 检索块字段：缺字段块数=0
- ✅ 引用定位：所有来源检索块均有 locator
- ✅ SQLite 完整性：ok
- ✅ 索引记录数一致：SQLite={"documents": 194, "parents": 667, "chunks": 8206} JSONL={"documents": 194, "parents": 667, "chunks": 8206}
- ✅ 样例检索：{"情绪周期": 462, "仓位 回撤": 480, "92科比": 88, "筹码": 1263, "弱转强": 107}
- ✅ 项目 Skills：缺失=[]
