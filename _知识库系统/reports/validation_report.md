# 知识库验证报告

- 通过：15/15
- 总体：通过

- ✅ 根 AGENTS.md：项目规则可被新对话自动发现
- ✅ 来源登记：登记来源数=7
- ✅ 文件 manifest：记录数=12898
- ✅ 来源 status 一致性：OK
- ✅ 索引来源合法性：OK
- ✅ 全部来源内容入库：{"fulibei": {"documents": 110, "parents": 441, "chunks": 1444}, "nanjinglu_bian": {"documents": 43, "parents": 95, "chunks": 586}, "tulip_garden": {"documents": 42, "parents": 161, "chunks": 1213}, "panfeng": {"documents": 29, "parents": 60, "chunks": 186}, "aizaibingchuan": {"documents": 2609, "parents": 5483, "chunks": 7338}, "kongkonglong": {"documents": 14, "parents": 32, "chunks": 172}, "boduanzhimen": {"documents": 849, "parents": 1690, "chunks": 8082}}
- ✅ 统一检索块：块数=19021
- ✅ 检索块字段：缺字段块数=0
- ✅ 引用定位：所有来源检索块均有 locator
- ✅ SQLite 完整性：ok
- ✅ 索引记录数一致：SQLite={"documents": 3696, "parents": 7962, "chunks": 19047} JSONL={"documents": 3696, "parents": 7962, "chunks": 19047}
- ✅ 样例检索：{"情绪周期": 723, "仓位 回撤": 2348, "92科比": 94, "筹码": 990, "弱转强": 368}
- ✅ 方法卡审批进度：reviewed=20/20  fulibei={'reviewed': 20}
- ✅ 登记表块数一致性：OK（只检查写了 last_import_summary.chunks 的来源）
- ✅ 项目 Skills：缺失=[]
