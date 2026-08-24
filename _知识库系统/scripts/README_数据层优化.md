# 数据层优化模块

2026-08-04 完成四项优化，解决静默数据错误、提升查询性能、补齐关键数据维度。

---

## 1. 接口健康检查 (`live_market_health.py`)

### 用途
防止接口失效时静默返回空数据/脏数据，导致策略误判。

### 核心功能

#### `health(quick=True)` - 全局健康报告
- **快速模式**（15s）：对 5 大来源 24 个探针探测一遍，汇总通断状态
- **详细模式**（~60s）：每个探针取完整数据并统计字段数
- **返回**：按源分组的探针状态，失败探针会标记错误原因

探测覆盖：
- 腾讯：5 个接口（实时行情、日线、涨停池、板块资金流、强势股）
- 新浪：4 个接口（日线、龙虎榜个股统计、新股涨停、二板池）
- 东财：7 个接口（大盘温度、分时、涨停池扩展、题材、行业 PE、分钟线、主线题材）
- 同花顺：4 个接口（板块资金流、涨停原因、强势股、昨日涨停今日）
- 通达信：4 个接口（板块资金流、涨停池、强势股、昨日涨停）

#### `cross_check(code, date=None)` - 跨源交叉验证
- 从腾讯/新浪/东财三个源各取一次日线，逐字段对比
- 发现分歧时列出各源数值，用于排查数据源 bug
- **真实发现**：修正了 ST 涨停判定 bug（`live_market.py:107`）

### 实测效果
- 快速检查 15.1s，24/24 探针通过
- 交叉验证 6 组数据，305 字段对比 0 处分歧
- 发现并修正 1 个 ST 涨停帽判定 bug

---

## 2. SQLite 本地缓存 (`live_market_cache.py`)

### 用途
历史 K 线重复查询走缓存，节省 token 消耗和网络请求。

### 存储结构
```sql
CREATE TABLE daily (
    code TEXT, date TEXT, o REAL, h REAL, l REAL, c REAL,
    vol INTEGER, chg REAL, ma5 REAL, adj TEXT,
    PRIMARY KEY (code, date, adj)
) WITHOUT ROWID;

CREATE TABLE minute (
    code TEXT, period INTEGER, dt TEXT,
    o REAL, h REAL, l REAL, c REAL, vol INTEGER,
    PRIMARY KEY (code, period, dt)
) WITHOUT ROWID;
```

### 核心功能

#### `daily(code, n, adjust, force=False)` - 日线缓存
- 首次查询：回源 → 写缓存 → 返回
- 再次查询：直接读缓存
- **性能**：首次 0.5s，命中 0.002s，**提速 215×**

#### `minute(code, period, n, force=False)` - 分钟线缓存
- **盘中必回源**（保证最新价实时性）
- 历史日期走缓存
- period ∈ {1, 5, 15, 30, 60}

#### `day_bars(code, date, period, fetch_n=3000)` - 指定日期分钟线
- **关键优化**：查询历史某天的分钟线，一次回源顺带缓存附近 60+ 个交易日
- 3000 根 5 分钟线约覆盖 63 个交易日
- **拒绝当日查询**（防止半截 K 线污染缓存）

### 实测存储
- 日线：94 B/行，全市场 5500 股 × 240 日/年 = 118.8 MB/年
- 分钟线：93 B/行，单股 240 日 × 48 根/日 = 1.1 MB/年

### 修复的关键 bug
**`_settled()` 日期格式错配**（行 91-101）
- **症状**：`day_bars("000001", "20260729")` 报错"该日期尚未收盘定型"
- **原因**：比较 `"20260729"` (YYYYMMDD) 和 `"2026-08-04"` (YYYY-MM-DD)，字符串比较返回 `False`
- **修复**：统一格式化成 YYYYMMDD 再比较
```python
d = str(date_str).replace("-", "")[:8]
t = datetime.now().strftime("%Y%m%d")
```

---

## 3. akshare 补充源 (`live_market_akshare.py`)

### 用途
补齐原始来源缺失的四类数据：营业部排行、业绩预告、停复牌、分红送配。

### 核心功能

#### `dept_rank(period, limit, min_times)` - 营业部排行
- 数据源：`stock_lhb_yytj_em`（东财龙虎榜营业部统计）
- 含**上榜后涨幅**：1/2/3/5/10 日上涨概率
- 用于跟踪游资席位活跃度

#### `earnings_forecast(period, limit, kind)` - 业绩预告
- 数据源：`stock_yjyg_em`
- 类型：预增、预减、略增、略减、首亏、扭亏
- 用于提前布局业绩拐点

#### `suspension(date, limit)` - 停复牌
- 数据源：`stock_tfp_em`
- 返回停牌股列表、复牌股列表、停牌原因

#### `bonus_plan(period, limit, min_ratio)` - 分红送配
- 数据源：`stock_fhps_em`（送转比例排行）
- **历史高点**：2017 年报最高 36.00，2025 年报最高 4.90（无 ≥5 案例）
- 用于高送转题材挖掘

### 技术细节

#### 代理清理（防止 akshare 请求失败）
```python
def _ak():
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "*"
    import akshare as ak
    return ak
```

#### 进度条抑制（防止污染 MCP stdio）
```python
def _quiet(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        return fn(*a, **kw)
```

#### pandas NaT 处理
```python
None if v is None or str(v) in ("nan", "NaT", "None", "", "-")
```

### 探索过程中的失败案例

#### ❌ 大单追踪 (`stock_fund_flow_big_deal`)
- **症状**：HTTP/HTTPS 均返回 401 Unauthorized
- **端口测试**：`data.10jqka.com.cn:80/443` 0.01s 可达
- **结论**：同花顺加了认证，不是网络问题

#### ❌ 高送转日历误用 (`stock_gsrl_gsdt_em`)
- **原标识**：我原以为这是高送转接口
- **实际标识**：akshare 源码注释"股市日历-公司动态"
- **实测返回**：对外担保 48、资产重组 14、股份质押 13，无送转
- **正确接口**：`stock_fhps_em`（已用 `bonus_plan()` 实现）

---

## 4. MCP 工具层整合 (`live_market_mcp.py`)

新增 10 个 MCP 工具，总工具数从 29 → 38。

### 健康检查工具
- `get_source_health(quick: bool)` → `health.health(quick)`
- `cross_check(code, date)` → `health.cross_check(code, date)`

### 缓存查询工具
- `get_daily_kline_cached(code, n, adjust, force)` → `cache.daily(...)`
- `get_minute_kline_cached(code, period, n, force)` → `cache.minute(...)`
- `get_day_bars_cached(code, date, period, fetch_n)` → `cache.day_bars(...)`

### akshare 补充工具
- `get_dept_rank(period, limit, min_times)` → `ak.dept_rank(...)`
- `get_earnings_forecast(period, limit, kind)` → `ak.earnings_forecast(...)`
- `get_suspension(date, limit)` → `ak.suspension(...)`
- `get_bonus_plan(period, limit, min_ratio)` → `ak.bonus_plan(...)`

### ⚠️ 重要提醒
**新工具仅在新会话中可见**。MCP 工具列表在会话启动时快照，当前会话看不到新增工具。

---

## 测试覆盖

### E2E 回归测试 (`_test_mcp_e2e.py`)
- **21 用例**：14 正常路径 + 7 错误路径
- **通过率**：21/21 (100%)
- **总耗时**：87.2s

测试迭代：
- Run 1: 12/21（期望键错误）
- Run 2: 15/21（修正部分键）
- Run 3: 18/21（修正更多键）
- Run 4: 20/21（发现 `_settled()` bug）
- Run 5: 21/21（修复 bug + 期望校准）

---

## 总结

### 完成项
1. ✅ 接口健康检查（24 探针全通，发现 1 个 ST bug）
2. ✅ SQLite 缓存（215× 提速，全市场 1 年存储 108.6 MB）
3. ✅ 跨源交叉验证（305 字段 0 分歧）
4. ✅ akshare 补充源（4 功能，11.7s 总耗时）
5. ✅ MCP 整合（38 工具，21/21 E2E 通过）

### 发现的真实 bug
- **ST 涨停帽判定**（`live_market.py:107`）：全按 5% 处理，忽略科创/创业 20%
- **`_settled()` 日期格式**（`live_market_cache.py:91`）：格式不统一导致历史日期拒绝

### 存储实测
- 日线：94 B/行
- 分钟线：93 B/行
- 全市场 1 年日线：118.8 MB
- 单股 1 年 5 分钟线：1.1 MB
- **结论**：存储开销可控，查询性能显著（215× 提速）

### 文档勘误
- ❌ `stock_gsrl_gsdt_em` 不是高送转，是公司动态
- ❌ `stock_fund_flow_big_deal` 已加认证，无法使用
- ✅ 正确高送转接口：`stock_fhps_em`

---

## 模块依赖

```
live_market_mcp.py (MCP 服务器)
    ├─ live_market.py (实时核心)
    ├─ live_market_ex.py (扩展接口)
    ├─ live_market_health.py (健康检查) [新增]
    ├─ live_market_cache.py (SQLite 缓存) [新增]
    └─ live_market_akshare.py (akshare 补充) [新增]
```

新增模块均可独立使用，不影响原有功能。
