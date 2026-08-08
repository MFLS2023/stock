# 知识库验证报告

- 通过：14/14
- 总体：通过

- ✅ 根 AGENTS.md：项目规则可被新对话自动发现
- ✅ 来源登记：登记来源数=5
- ✅ 文件 manifest：记录数=3194
- ✅ 来源 status 一致性：OK
- ✅ 索引来源合法性：OK
- ✅ 全部来源内容入库：{"fulibei": {"documents": 110, "parents": 441, "chunks": 1444}, "nanjinglu_bian": {"documents": 42, "parents": 80, "chunks": 525}, "tulip_garden": {"documents": 42, "parents": 155, "chunks": 1181}, "panfeng": {"documents": 29, "parents": 60, "chunks": 186}, "aizaibingchuan": {"documents": 2537, "parents": 5184, "chunks": 5813}}
- ✅ 统一检索块：块数=9149
- ✅ 检索块字段：缺字段块数=0
- ✅ 引用定位：所有来源检索块均有 locator
- ✅ SQLite 完整性：ok
- ✅ 索引记录数一致：SQLite={"documents": 2760, "parents": 5920, "chunks": 9175} JSONL={"documents": 2760, "parents": 5920, "chunks": 9175}
- ✅ 样例检索：{"情绪周期": 632, "仓位 回撤": 1358, "92科比": 91, "筹码": 772, "弱转强": 287}
- ✅ 方法卡审批进度：reviewed=20/20  fulibei={'reviewed': 20}
- ✅ 项目 Skills：缺失=[]
