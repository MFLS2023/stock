# 知识库验证报告

- 通过：11/11
- 总体：通过

- ✅ 根 AGENTS.md：项目规则可被新对话自动发现
- ✅ 来源登记：登记来源数=4
- ✅ 文件 manifest：记录数=530
- ✅ 全部来源内容入库：{"fulibei": {"documents": 110, "parents": 441, "chunks": 1444}, "nanjinglu_bian": {"documents": 42, "parents": 80, "chunks": 525}, "tulip_garden": {"documents": 42, "parents": 155, "chunks": 1181}, "panfeng": {"documents": 29, "parents": 60, "chunks": 186}}
- ✅ 统一检索块：块数=3336
- ✅ 检索块字段：缺字段块数=0
- ✅ 引用定位：所有来源检索块均有 locator
- ✅ SQLite 完整性：ok
- ✅ 索引记录数一致：SQLite={"documents": 223, "parents": 736, "chunks": 3362} JSONL={"documents": 223, "parents": 736, "chunks": 3362}
- ✅ 样例检索：{"情绪周期": 516, "仓位 回撤": 498, "92科比": 88, "筹码": 422, "弱转强": 111}
- ✅ 项目 Skills：缺失=[]
