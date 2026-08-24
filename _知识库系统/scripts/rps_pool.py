"""
RPS 三线红名单：接通达信导出 → 规范化 → 跨日统计 → 日间对比

为什么要这个脚本
----------------
南京路彼岸《情绪周期下"如何选股"》第 24/25 页贴的那张表，列是
    序号 / 日期 / 股票名 / 加入日 / 入连续天数 / 入总天数 / 入池次数 / 距60天第一次
**后面五列不是通达信导出的** —— 通达信选股只给当日结果（代码 + 名称）。
那五列是拿多天名单累积算出来的。所以分工是：

    通达信每天选股 → 导出文件丢进收件箱 → 本脚本算出全部统计列

作者第 26 页自己交代的用法：
    "大家可以将跑出的个股按照出现次数做统计，作为趋势强度的一种形式，每日复盘。"

目录布局
--------
    data/rps_pool_inbox/          收件箱：通达信导出的原始文件丢这里，文件名随意
    data/rps_pool_inbox/_done/    解析完归档，不删原件
    data/rps_pool_daily/          规范化后的每日名单  <日期>_<类型>.csv
    data/rps_pool.csv             每日计数流水，由名单行数自动派生（不再手填）
    data/_symbol_map.json         全市场 代码↔名称↔行业 缓存，隔天自动刷新

命令
----
    python rps_pool.py scan                      扫收件箱，自动解析入库
    python rps_pool.py import <文件> [--date] [--kind]
    python rps_pool.py stat  [--date] [--kind]   出那张统计表
    python rps_pool.py diff  [--date] [--kind]   与上一登记日对比
    python rps_pool.py dates [--kind]            已登记的日期
    python rps_pool.py refresh-map               强制重拉全市场代码表
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, date as _date

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(_HERE), "data")
INBOX = os.path.join(DATA_DIR, "rps_pool_inbox")
INBOX_DONE = os.path.join(INBOX, "_done")
DAILY_DIR = os.path.join(DATA_DIR, "rps_pool_daily")
MAP_PATH = os.path.join(DATA_DIR, "_symbol_map.json")
# 改名别名表：{"旧名": "代码"}。公司改名后老名单就查不到了，
# 实例：韦尔股份 → 豪威集团（603501）。手动往这个文件里加一行即可。
ALIAS_PATH = os.path.join(DATA_DIR, "_symbol_alias.json")

# 表头关键词。命中两个以上且行内没有代码，就当表头静默跳过，不进警告。
HEADER_WORDS = ("序号", "代码", "名称", "股票名", "简称", "涨幅", "现价", "加入日",
                "入连续天数", "入总天数", "入池次数", "第一次", "行业", "板块", "日期")

# 池子类型。通达信里一个公式一个选股方案，导出文件名建议带上类型，scan 靠它自动归类。
KINDS = ("三线红", "一线红", "板块三线红")
DEFAULT_KIND = "三线红"

# 跨日统计窗口（交易日个数）。作者表里"距60天第一次"暗示是 60。
STAT_WINDOW = 60

DAILY_FIELDS = ["code", "name", "industry", "match"]


# ---------- 通用小工具 ----------

def _ensure_dirs() -> None:
    for d in (DATA_DIR, INBOX, INBOX_DONE, DAILY_DIR):
        os.makedirs(d, exist_ok=True)


def norm_date(s: str | None) -> str:
    """20260805 / 2026-08-05 / 2026/8/5 / 26.08.05 → 2026-08-05。空则取今天。"""
    if not s:
        return _date.today().isoformat()
    t = re.sub(r"[^\d]", "", str(s))
    if len(t) == 8:
        return f"{t[:4]}-{t[4:6]}-{t[6:8]}"
    if len(t) == 6:                      # 260805 → 2026-08-05（作者表里有 26.02.09 这种写法）
        return f"20{t[:2]}-{t[2:4]}-{t[4:6]}"
    raise ValueError(f"看不懂的日期：{s!r}（用 20260805 或 2026-08-05）")


def _norm_name(s: str) -> str:
    """股票名归一：全角转半角、去空白、去 ST/N/*/退 等前后缀标记。用于名称匹配。"""
    s = unicodedata.normalize("NFKC", str(s or "")).strip()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"^(\*?ST|N|C|U|XD|XR|DR|X)", "", s)
    s = s.replace("*", "").replace("退", "")
    return s


def _is_code(s: str) -> bool:
    return bool(re.fullmatch(r"\d{6}", str(s or "").strip()))


# ---------- 全市场代码表 ----------

def _load_alias() -> dict:
    """读改名别名表 {"旧名": "代码"}。以 _ 开头的键是说明文字，跳过。"""
    if not os.path.exists(ALIAS_PATH):
        return {}
    try:
        with open(ALIAS_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:  # noqa: BLE001 - 别名表坏了不该拖垮解析
        return {}
    out = {}
    for k, v in raw.items():
        if k.startswith("_"):
            continue
        code = v if isinstance(v, str) else str((v or {}).get("code", ""))
        if _is_code(code):
            out[_norm_name(k)] = code
    return out


def _apply_alias(m: dict) -> dict:
    """
    把别名并进 by_name。每次加载都做，所以改完别名表立刻生效，不用重拉代码表。
    别名指向的代码必须在全市场表里，否则忽略（避免把手误写的代码当真）。
    """
    alias = _load_alias()
    by_name, by_code = m.get("by_name", {}), m.get("by_code", {})
    n = 0
    for nm, code in alias.items():
        if code in by_code and nm not in by_name:
            by_name[nm] = [code]
            n += 1
    m["alias_applied"] = n
    return m


def load_symbol_map(force: bool = False) -> dict:
    """
    返回 {"by_code": {代码: {name, industry}}, "by_name": {归一名: [代码,...]}, "fetched": ISO时间}

    通达信导出可能只有股票名没有代码（作者第 24/25 页那张表就是只有名字），
    所以要靠这张表把名字换回代码，顺手带上行业 —— 行业分布是判断主线的直接读数。
    缓存一天，隔天自动重拉。
    """
    if not force and os.path.exists(MAP_PATH):
        try:
            with open(MAP_PATH, encoding="utf-8") as f:
                m = json.load(f)
            if m.get("fetched", "")[:10] == _date.today().isoformat() and m.get("by_code"):
                return _apply_alias(m)
        except Exception:  # noqa: BLE001 - 缓存坏了就重拉，不值得区分原因
            pass

    sys.path.insert(0, _HERE)
    import live_market as lm            # 复用它绕代理 + 限速 + 多域名降级的 session

    by_code: dict[str, dict] = {}
    pn, total = 1, None
    while True:
        d = lm._em_get("/api/qt/clist/get", {
            "pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f12",
            "fs": lm.FS_ALL_A, "fields": "f12,f13,f14,f100",
        })
        total = total or d.get("total") or 0
        for x in d.get("diff") or []:
            code = str(x.get("f12") or "")
            if not _is_code(code):
                continue
            by_code[code] = {"name": str(x.get("f14") or ""),
                             "industry": str(x.get("f100") or "")}
        if len(by_code) >= total or not d.get("diff"):
            break
        pn += 1
        if pn > 80:                     # 5889/100≈59 页，留余量后硬停，防翻页不收敛
            break

    by_name: dict[str, list] = {}
    for code, info in by_code.items():
        by_name.setdefault(_norm_name(info["name"]), []).append(code)

    m = {"by_code": by_code, "by_name": by_name,
         "total_reported": total, "fetched": datetime.now().isoformat(timespec="seconds")}
    _ensure_dirs()
    tmp = MAP_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False)
    os.replace(tmp, MAP_PATH)
    return _apply_alias(m)


# ---------- 解析通达信导出 ----------

def _read_text_any(path: str) -> str:
    """通达信导出默认 GBK，也可能是 UTF-8。逐个编码试，不猜。"""
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("gb18030", errors="replace")


def _rows_from_xlsx(path: str) -> list[list[str]]:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        for r in ws.iter_rows(values_only=True):
            out.append(["" if c is None else str(c) for c in r])
    wb.close()
    return out


def _rows_from_text(path: str) -> list[list[str]]:
    txt = _read_text_any(path)
    lines = [ln for ln in txt.splitlines() if ln.strip()]
    # 分隔符按出现次数挑：制表符 > 逗号 > 连续空格。通达信"导出到文件"给制表符。
    tabs = sum(ln.count("\t") for ln in lines)
    commas = sum(ln.count(",") for ln in lines)
    if tabs >= max(1, len(lines) // 2):
        return [ln.split("\t") for ln in lines]
    if commas >= max(1, len(lines) // 2):
        return list(csv.reader(lines))
    return [re.split(r"\s{2,}|\s", ln.strip()) for ln in lines]


def _clean_cell(c: str) -> str:
    """去掉 Excel 防丢零的 ="600519" 包装、去引号空白。"""
    c = str(c or "").strip().strip('"').strip("'")
    if c.startswith("=") :
        c = c[1:].strip().strip('"')
    return c.strip()


def _pick_code(cell: str) -> str | None:
    """
    从单元格里取 6 位代码。通达信自定义板块文件常带市场前缀：
        1600519（1=沪）、0300248（0=深）、SH600519、sz300248
    """
    c = _clean_cell(cell)
    if not c:
        return None
    if _is_code(c):
        return c
    m = re.fullmatch(r"(?:SH|SZ|BJ|sh|sz|bj)?[01]?(\d{6})(?:\.(?:SH|SZ|BJ))?", c)
    if m:
        return m.group(1)
    m = re.fullmatch(r"(\d{6})\.(?:SH|SZ|BJ|sh|sz|bj)", c)
    return m.group(1) if m else None


def parse_export(path: str) -> dict:
    """
    解析一个通达信导出文件 → {"stocks": [{code,name,industry,match}], "warnings": [...]}

    不要求列顺序，也不要求有表头。逐行找 6 位代码；找不到代码就退回用股票名
    去全市场表里反查（作者第 24/25 页那张表就是只有名字没有代码）。
    match 字段记录这一行是怎么认出来的，便于事后追责：
        code           行里直接有代码
        name           靠名称唯一反查到
        name_ambiguous 名称对上多个代码，取了第一个（会进 warnings）
        code_unknown   代码不在全市场表里（退市/新股未收录），保留代码
    """
    ext = os.path.splitext(path)[1].lower()
    rows = _rows_from_xlsx(path) if ext in (".xlsx", ".xlsm") else _rows_from_text(path)
    m = load_symbol_map()
    by_code, by_name = m["by_code"], m["by_name"]

    stocks: list[dict] = []
    seen: set[str] = set()
    warnings: list[str] = []
    unmatched: list[str] = []
    headers_skipped = 0

    for row in rows:
        cells = [_clean_cell(c) for c in row]
        if not any(cells):
            continue
        # 表头行静默跳过：命中两个以上表头词、且整行没有 6 位代码
        if (sum(1 for w in HEADER_WORDS if any(w == c or w in c for c in cells)) >= 2
                and not any(_pick_code(c) for c in cells)):
            headers_skipped += 1
            continue

        code = None
        for c in cells:
            code = _pick_code(c)
            if code:
                break

        if code:
            info = by_code.get(code)
            if code in seen:
                continue
            seen.add(code)
            if info:
                stocks.append({"code": code, "name": info["name"],
                               "industry": info["industry"], "match": "code"})
            else:
                # 代码不认识：把行里最像股票名的一格留下来，不丢数据
                nm = next((c for c in cells if re.search(r"[一-鿿]", c)
                           and not re.search(r"\d{4}-\d{2}-\d{2}", c)), "")
                stocks.append({"code": code, "name": nm, "industry": "",
                               "match": "code_unknown"})
                warnings.append(f"代码 {code} 不在全市场表里（退市或未收录），已保留")
            continue

        # 没代码 → 用名字反查
        for c in cells:
            if not re.search(r"[一-鿿]", c):
                continue
            if re.fullmatch(r"[一-鿿]{1,6}[A-Z]?\d?", c) is None and len(c) > 8:
                continue                     # 太长的中文格子是备注/行业，不是股票名
            cands = by_name.get(_norm_name(c))
            if not cands:
                continue
            code = cands[0]
            if code in seen:
                break
            seen.add(code)
            info = by_code[code]
            stocks.append({"code": code, "name": info["name"], "industry": info["industry"],
                           "match": "name" if len(cands) == 1 else "name_ambiguous"})
            if len(cands) > 1:
                warnings.append(f"名称 {c} 对应多个代码 {cands}，取了 {code}")
            break
        else:
            cn = [c for c in cells if re.search(r"[一-鿿]", c)]
            if cn:
                unmatched.append(" ".join(cn)[:30])

    if unmatched:
        # 这些名字必须点名报出来。最常见的原因是公司改名（韦尔股份→豪威集团），
        # 静默跳过等于悄悄少几只票，池数就不准了。
        warnings.append(
            f"⚠️ {len(unmatched)} 个名字在全市场表里查不到，**已跳过（池数会少这么多）**："
            + "、".join(unmatched[:12]) + ("…" if len(unmatched) > 12 else "")
            + f"。最常见原因是公司改名。修法：往 {ALIAS_PATH} 里加 "
            + '{"旧名":"代码"} 后重跑，例如 {"韦尔股份":"603501"}')
    return {"stocks": stocks, "warnings": warnings, "raw_rows": len(rows),
            "表头行": headers_skipped, "查不到的名字": unmatched}


def guess_kind(path: str) -> str:
    """从文件名猜池子类型。板块要放在三线红之前判断，否则会被前者抢走。"""
    base = os.path.basename(path)
    if "板块" in base:
        return "板块三线红"
    if "一线红" in base or "一线" in base:
        return "一线红"
    return DEFAULT_KIND


def guess_date(path: str) -> str:
    """
    从文件名猜日期，猜不到用文件修改时间。
    盘后导出，修改时间就是当天，所以这个兜底是可靠的。
    """
    base = os.path.basename(path)
    m = re.search(r"(20\d{6})", base) or re.search(r"(20\d{2}[-_./]\d{1,2}[-_./]\d{1,2})", base)
    if m:
        try:
            return norm_date(m.group(1))
        except ValueError:
            pass
    return datetime.fromtimestamp(os.path.getmtime(path)).date().isoformat()


# ---------- 每日名单存取 ----------

def daily_path(d: str, kind: str) -> str:
    return os.path.join(DAILY_DIR, f"{d}_{kind}.csv")


def save_daily(d: str, kind: str, stocks: list[dict]) -> str:
    """写规范化后的当日名单。UTF-8 BOM，Excel 双击能直接打开。"""
    _ensure_dirs()
    p = daily_path(d, kind)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=DAILY_FIELDS)
        w.writeheader()
        for s in stocks:
            w.writerow({k: s.get(k, "") for k in DAILY_FIELDS})
    os.replace(tmp, p)
    return p


def load_daily(d: str, kind: str) -> list[dict] | None:
    p = daily_path(d, kind)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def list_dates(kind: str = DEFAULT_KIND) -> list[str]:
    """已登记的日期，升序。"""
    _ensure_dirs()
    out = []
    for fn in os.listdir(DAILY_DIR):
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})_(.+)\.csv", fn)
        if m and m.group(2) == kind:
            out.append(m.group(1))
    return sorted(out)


def prev_date(d: str, kind: str = DEFAULT_KIND) -> str | None:
    ds = [x for x in list_dates(kind) if x < d]
    return ds[-1] if ds else None


# ---------- 计数流水（由名单行数派生） ----------

def sync_count_log() -> dict:
    """
    把每日名单的行数汇总进 data/rps_pool.csv。
    这个数就是第 7 指标要的"三线红池数" —— 不再需要手填，名单在就有数。
    """
    import rps_log                       # 同目录，沿用它的 CSV 结构与原子写

    counts: dict[str, dict] = {}
    for kind in KINDS:
        for d in list_dates(kind):
            rows = load_daily(d, kind) or []
            counts.setdefault(d, {})[kind] = len(rows)

    pool = rps_log.load_all()
    by_date = {r["date"]: r for r in pool}
    changed = 0
    for d, kv in sorted(counts.items()):
        rec = by_date.get(d)
        want = {
            "三线红池数": kv.get("三线红"),
            "一线红池数": kv.get("一线红"),
            "板块三线红池数": kv.get("板块三线红"),
        }
        if rec is None:
            rec = {k: None for k in rps_log.FIELDS}
            rec["date"] = d
            rec["阈值口径"] = rps_log.DEFAULT_THRESHOLD
            pool.append(rec)
            by_date[d] = rec
        for k, v in want.items():
            if v is not None and rec.get(k) != v:
                rec[k] = v
                changed += 1
        rec["备注"] = "由名单文件自动派生（rps_pool.py）"
        rec["登记时间"] = datetime.now().isoformat(timespec="seconds")
    pool.sort(key=lambda r: r["date"])
    rps_log.save_all(pool)
    return {"日期数": len(counts), "更新字段数": changed, "流水文件": rps_log.CSV_PATH}


# ---------- 跨日统计（作者第 24/25 页那张表） ----------

def stat(d: str | None = None, kind: str = DEFAULT_KIND, window: int = STAT_WINDOW) -> dict:
    """
    出作者第 24/25 页那张表：
        股票名 / 代码 / 行业 / 加入日 / 入连续天数 / 入总天数 / 入池次数 / 距60天第一次

    口径（自定义，作者原文未交代精确定义，这里明确写下来免得日后对不上）：
      加入日        本轮连续在榜的起始登记日
      入连续天数    截至该日连续在榜的登记日个数（含该日，所以最小是 1）
      入总天数      窗口内在榜的登记日总数
      入池次数      窗口内出现过几段连续在榜（进进出出算多次）
      距60天第一次  窗口内第一次出现的登记日

    ⚠️ 窗口是"已登记的日期个数"，不是自然交易日。中间漏登记的日子不算断档，
       它们只是不存在 —— 所以刚开始用的头两个月，这些数字必然偏小。
    """
    d = norm_date(d)
    today = load_daily(d, kind)
    if today is None:
        return {"日期": d, "池子": kind, "缺数": f"{d} 没有 {kind} 名单",
                "怎么补上": f"把通达信导出文件丢进 {INBOX} 后跑 python rps_pool.py scan",
                "已登记日期": list_dates(kind)[-10:]}

    dates = [x for x in list_dates(kind) if x <= d][-window:]
    member: dict[str, list[bool]] = {}
    lists = {x: {r["code"] for r in (load_daily(x, kind) or [])} for x in dates}
    codes_today = [r["code"] for r in today]
    for code in codes_today:
        member[code] = [code in lists[x] for x in dates]

    info = {r["code"]: r for r in today}
    rows = []
    for code in codes_today:
        v = member[code]
        total = sum(v)
        # 尾部连续
        streak = 0
        for flag in reversed(v):
            if not flag:
                break
            streak += 1
        join_day = dates[len(v) - streak] if streak else d
        # 连续段数
        times, prev = 0, False
        for flag in v:
            if flag and not prev:
                times += 1
            prev = flag
        first = next((dates[i] for i, flag in enumerate(v) if flag), d)
        rows.append({
            "股票名": info[code].get("name", ""),
            "代码": code,
            "行业": info[code].get("industry", ""),
            "加入日": join_day,
            "入连续天数": streak,
            "入总天数": total,
            "入池次数": times,
            f"距{window}天第一次": first,
        })
    # 作者的用法：按出现次数排，次数就是趋势强度
    rows.sort(key=lambda r: (-r["入总天数"], -r["入连续天数"], r["代码"]))
    for i, r in enumerate(rows, 1):
        r["序号"] = i

    ind: dict[str, int] = {}
    for r in rows:
        ind[r["行业"] or "未知"] = ind.get(r["行业"] or "未知", 0) + 1
    ind_top = sorted(ind.items(), key=lambda kv: -kv[1])[:12]

    return {
        "日期": d,
        "池子": kind,
        "池数": len(rows),
        "统计窗口": f"最近 {len(dates)} 个已登记日（{dates[0]} ~ {dates[-1]}）"
                    + ("　⚠️登记日不足，数字会偏小" if len(dates) < 10 else ""),
        "行业分布TOP": [{"行业": k, "只数": v} for k, v in ind_top],
        "趋势强度榜": rows,
        "口径": "入总天数=窗口内在榜登记日数；入连续天数含当日；窗口按登记日计非自然交易日",
    }


# ---------- 日间对比 ----------

def diff(d: str | None = None, kind: str = DEFAULT_KIND, base: str | None = None) -> dict:
    """与上一个登记日对比：新进榜 / 掉出榜 / 连续在榜，并给行业层面的增减。"""
    d = norm_date(d)
    cur = load_daily(d, kind)
    if cur is None:
        return {"日期": d, "池子": kind, "缺数": f"{d} 没有 {kind} 名单",
                "已登记日期": list_dates(kind)[-10:]}
    b = norm_date(base) if base else prev_date(d, kind)
    if not b:
        return {"日期": d, "池子": kind, "池数": len(cur),
                "缺数": "没有更早的登记日可比（这是第一天）",
                "提示": "至少登记两天才有对比"}
    old = load_daily(b, kind) or []

    cm = {r["code"]: r for r in cur}
    om = {r["code"]: r for r in old}
    new_codes = [c for c in cm if c not in om]
    out_codes = [c for c in om if c not in cm]
    stay = [c for c in cm if c in om]

    def _brief(m: dict, codes: list) -> list:
        return sorted(({"股票名": m[c].get("name", ""), "代码": c,
                        "行业": m[c].get("industry", "")} for c in codes),
                      key=lambda r: (r["行业"], r["代码"]))

    def _ind(m: dict, codes) -> dict:
        o: dict[str, int] = {}
        for c in codes:
            k = m[c].get("industry") or "未知"
            o[k] = o.get(k, 0) + 1
        return o

    ic, io = _ind(cm, cm.keys()), _ind(om, om.keys())
    delta = []
    for k in set(ic) | set(io):
        dv = ic.get(k, 0) - io.get(k, 0)
        if dv:
            delta.append({"行业": k, "今日": ic.get(k, 0), "上一日": io.get(k, 0), "增减": dv})
    delta.sort(key=lambda r: -abs(r["增减"]))

    n_cur, n_old = len(cur), len(old)
    return {
        "日期": d,
        "对比基准日": b,
        "池子": kind,
        "池数": f"{n_old} → {n_cur}",
        "净变化": n_cur - n_old,
        "扩容还是缩容": "扩容" if n_cur > n_old else ("缩容" if n_cur < n_old else "持平"),
        "换手率": f"{(len(new_codes) + len(out_codes)) / max(1, n_old + n_cur) * 200:.1f}%"
                  "（进+出 占两日均量的比例，衡量名单翻新程度）",
        "新进榜": {"只数": len(new_codes), "明细": _brief(cm, new_codes)},
        "掉出榜": {"只数": len(out_codes), "明细": _brief(om, out_codes)},
        "连续在榜": {"只数": len(stay)},
        "行业增减": delta[:15],
        "读法": "缩容+某行业整片掉出=该方向退潮；扩容且集中在一个行业=主线在扩散",
    }


# ---------- 入库 ----------

def import_file(path: str, d: str | None = None, kind: str | None = None,
                archive: bool = False) -> dict:
    """解析一个导出文件并落成当日名单。同日同类型重复导入直接覆盖（重跑幂等）。"""
    if not os.path.exists(path):
        raise SystemExit(f"文件不存在：{path}")
    kind = kind or guess_kind(path)
    if kind not in KINDS:
        raise SystemExit(f"池子类型只能是 {KINDS} 之一，收到 {kind!r}")
    dd = norm_date(d) if d else guess_date(path)

    r = parse_export(path)
    if not r["stocks"]:
        return {"结果": "解析到 0 只，未写入", "文件": path, "日期": dd, "池子": kind,
                "原始行数": r["raw_rows"], "警告": r["warnings"],
                "怎么办": "确认导出的是选股结果（含代码或股票名），或贴一行样例给我看"}

    p = save_daily(dd, kind, r["stocks"])
    moved = None
    if archive:
        _ensure_dirs()
        tgt = os.path.join(INBOX_DONE, f"{dd}_{kind}_{os.path.basename(path)}")
        try:
            os.replace(path, tgt)
            moved = tgt
        except OSError as e:
            r["warnings"].append(f"归档失败（原件留在收件箱）：{e}")

    mt: dict[str, int] = {}
    for s in r["stocks"]:
        mt[s["match"]] = mt.get(s["match"], 0) + 1
    return {"结果": "已写入", "日期": dd, "池子": kind, "只数": len(r["stocks"]),
            "名单文件": p, "识别方式": mt, "原始行数": r["raw_rows"],
            "警告": r["warnings"], "已归档到": moved}


def scan(archive: bool = True) -> dict:
    """扫收件箱里所有文件，逐个解析入库，然后同步计数流水。"""
    _ensure_dirs()
    files = [os.path.join(INBOX, fn) for fn in sorted(os.listdir(INBOX))
             if os.path.isfile(os.path.join(INBOX, fn))
             and os.path.splitext(fn)[1].lower() in (".txt", ".csv", ".xls", ".xlsx", ".xlsm", ".blk", ".ebk", "")]
    if not files:
        return {"结果": "收件箱是空的", "收件箱": INBOX,
                "怎么用": "通达信选股后导出，文件丢进这个目录，再跑 python rps_pool.py scan"}
    done, failed = [], []
    for f in files:
        try:
            done.append(import_file(f, archive=archive))
        except Exception as e:  # noqa: BLE001 - 单个文件坏了不能拖垮整批
            failed.append({"文件": f, "错误": f"{type(e).__name__}: {e}"})
    return {"处理": len(files), "成功": done, "失败": failed, "计数流水": sync_count_log()}


# ---------- CLI ----------

def _p(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="RPS 三线红名单：导入 / 统计 / 日间对比")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="扫收件箱自动入库")
    s.add_argument("--keep", action="store_true", help="不归档，原件留在收件箱")

    s = sub.add_parser("import", help="导入指定文件")
    s.add_argument("path")
    s.add_argument("--date", default=None, help="覆盖日期，如 20260805")
    s.add_argument("--kind", default=None, choices=list(KINDS))

    for name, helptext in (("stat", "出趋势强度统计表"), ("diff", "与上一登记日对比")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--date", default=None)
        s.add_argument("--kind", default=DEFAULT_KIND, choices=list(KINDS))
        if name == "diff":
            s.add_argument("--base", default=None, help="指定对比基准日")
        else:
            s.add_argument("--top", type=int, default=0, help="只看前 N 只，0=全部")

    s = sub.add_parser("dates", help="已登记日期")
    s.add_argument("--kind", default=DEFAULT_KIND, choices=list(KINDS))

    sub.add_parser("refresh-map", help="强制重拉全市场代码表")
    sub.add_parser("sync", help="按名单重算计数流水")

    a = ap.parse_args(argv)
    if a.cmd == "scan":
        _p(scan(archive=not a.keep))
    elif a.cmd == "import":
        r = import_file(a.path, d=a.date, kind=a.kind)
        _p(r)
        if r.get("结果") == "已写入":
            _p(sync_count_log())
    elif a.cmd == "stat":
        r = stat(a.date, a.kind)
        if a.top and isinstance(r.get("趋势强度榜"), list):
            r["趋势强度榜"] = r["趋势强度榜"][:a.top]
            r["注"] = f"只显示前 {a.top} 只，全量看 {daily_path(r['日期'], a.kind)}"
        _p(r)
    elif a.cmd == "diff":
        _p(diff(a.date, a.kind, a.base))
    elif a.cmd == "dates":
        _p({"池子": a.kind, "已登记": list_dates(a.kind)})
    elif a.cmd == "refresh-map":
        m = load_symbol_map(force=True)
        _p({"代码表条数": len(m["by_code"]), "东财报告总数": m.get("total_reported"),
            "取数时间": m["fetched"], "缓存文件": MAP_PATH})
    elif a.cmd == "sync":
        _p(sync_count_log())
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
