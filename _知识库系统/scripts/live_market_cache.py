#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
历史行情本地缓存（SQLite）—— 查过就存，重复查读本地。

**只缓存「已收盘定型」的数据，这是整个模块最关键的约束。**
盘中的当日 K 线还在动（lm.daily 最后一根就是实时价），存下来就是一根残缺的
半截 K 线，下次读到会当成真数据用。所以：
  已收盘（15:00 后）→ 当日 K 线定型，可以存
  盘中             → 当日那根一律不存，每次都走网络取最新

这样做的副作用正好符合使用习惯：盘后复盘时缓存全命中（0 次网络请求），
盘中看盘时永远拿实时数据。

库文件独立：`indexes/market_cache.db`，**不碰知识库的 knowledge.db**。
删掉这个文件不会丢任何东西，下次查询会自动重建。

命令行：
    python live_market_cache.py stats           # 看占用和覆盖
    python live_market_cache.py daily 600519 60 # 取日线（走缓存）
    python live_market_cache.py clear 600519    # 清某只票
    python live_market_cache.py vacuum          # 整理碎片
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_market as lm          # noqa: E402
import live_market_ex as ex       # noqa: E402

DB = Path(__file__).resolve().parent.parent / "indexes" / "market_cache.db"

# 建表。**必须有主键**，否则重复写入会静默累积重复行 ——
# 社区项目 tdx2db 的 README 明确记了这个坑：老版本缺唯一约束，
# SQLite/MySQL 下不报错，数据越导越多。
DDL = """
CREATE TABLE IF NOT EXISTS daily (
    code   TEXT NOT NULL,
    date   TEXT NOT NULL,
    adjust TEXT NOT NULL,
    o REAL, h REAL, l REAL, c REAL, vol REAL,
    PRIMARY KEY (code, date, adjust)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS minute (
    code   TEXT NOT NULL,
    period INTEGER NOT NULL,
    dt     TEXT NOT NULL,
    o REAL, h REAL, l REAL, c REAL, vol REAL,
    PRIMARY KEY (code, period, dt)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def _conn() -> sqlite3.Connection:
    """打开缓存库，不存在则建。WAL 模式减少写锁等待。"""
    DB.parent.mkdir(parents=True, exist_ok=True)
    cx = sqlite3.connect(DB, timeout=10)
    cx.executescript(DDL)
    cx.execute("PRAGMA journal_mode=WAL")
    cx.execute("PRAGMA synchronous=NORMAL")
    return cx


# ---------- 「定型」判定：整个缓存正确性的地基 ----------

CLOSE_H, CLOSE_M = 15, 5      # 15:05 之后认为当日 K 线已定型（收盘 15:00 + 5 分钟余量）


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def today_settled() -> bool:
    """今天的 K 线是否已定型（已过收盘 + 余量）。周末视为已定型。"""
    n = datetime.now()
    if n.weekday() >= 5:
        return True
    return (n.hour, n.minute) >= (CLOSE_H, CLOSE_M)


def _settled(date_str: str) -> bool:
    """
    某个日期的 K 线是否已定型。只有「今天且还没收盘」才算未定型。
    未来日期也算未定型（防止时钟错乱写进脏数据）。
    """
    d = str(date_str).replace("-", "")[:8]  # 统一成 YYYYMMDD
    t = datetime.now().strftime("%Y%m%d")
    if d < t:
        return True
    if d > t:
        return False
    return today_settled()


def _last_settled_day() -> str:
    """最近一个已定型的日历日（不是交易日，本模块不需要交易日历）。"""
    d = datetime.now()
    if not today_settled():
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


# ---------- 日线缓存 ----------

def _read_daily(cx, code: str, adjust: str, n: int) -> list[dict]:
    """从缓存读最近 n 根。按日期倒序取再反转，避免全表扫。"""
    cur = cx.execute(
        "SELECT date,o,h,l,c,vol FROM daily WHERE code=? AND adjust=? "
        "ORDER BY date DESC LIMIT ?", (code, adjust, n))
    rows = cur.fetchall()[::-1]
    out = []
    for date, o, h, l, c, vol in rows:
        prev = out[-1]["收"] if out else None
        out.append({
            "日期": date, "开": o, "收": c, "高": h, "低": l,
            "量(手)": int(vol) if vol is not None else None,
            # 涨跌幅按缓存内相邻两根重算。第一根没有前一根，与 lm.daily 一致给 None
            "涨跌幅": round((c - prev) / prev * 100, 2) if prev else None,
        })
    return out


def _write_daily(cx, code: str, adjust: str, rows: list[dict]) -> int:
    """
    写入日线。**只写已定型的**，盘中的当日那根丢掉。
    INSERT OR REPLACE 靠主键去重，重复调用不会累积重复行。
    """
    data = [(code, r["日期"], adjust, r.get("开"), r.get("高"), r.get("低"),
             r.get("收"), r.get("量(手)"))
            for r in rows if _settled(r.get("日期", ""))]
    if data:
        cx.executemany("INSERT OR REPLACE INTO daily"
                       "(code,date,adjust,o,h,l,c,vol) VALUES(?,?,?,?,?,?,?,?)", data)
        cx.commit()
    return len(data)


def daily(code: str, n: int = 30, adjust: str = "qfq",
          force: bool = False) -> dict:
    """
    日线，带缓存。返回结构与 lm.daily 的列表不同 —— 这里额外带缓存命中信息，
    K 线在 "K线" 键里。要纯列表用 daily_rows()。

    命中规则（两个条件都满足才算命中）：
      1. 缓存里有 >= n 根
      2. 缓存里最新那根 >= 最近一个已定型日
    第 2 条是防「昨天存的数据今天直接拿来用」。

    盘中调用时永远回源，因为当日那根还在动。force=True 强制回源并刷新缓存。
    """
    cx = _conn()
    try:
        hit_rows = _read_daily(cx, code, adjust, n)
        newest = hit_rows[-1]["日期"] if hit_rows else None
        fresh = bool(newest and newest >= _last_settled_day())
        # 盘中当日 K 线在动，缓存里不可能有今天的，必须回源
        intraday = not today_settled()

        if hit_rows and len(hit_rows) >= n and fresh and not intraday and not force:
            return {"代码": code, "复权": adjust or "不复权", "根数": len(hit_rows),
                    "数据来源": "本地缓存", "缓存命中": True,
                    "网络请求": 0, "最新一根": newest, "K线": hit_rows}

        t0 = time.time()
        rows = lm.daily(code, n, adjust)          # 取数失败会抛，不吞异常
        cost = round(time.time() - t0, 2)
        wrote = _write_daily(cx, code, adjust, rows)
        why = ("强制刷新" if force else
               "盘中：当日K线未定型，不能用缓存" if intraday else
               "缓存无此票" if not hit_rows else
               f"缓存不够新（最新 {newest}，需要 {_last_settled_day()}）" if not fresh else
               f"缓存只有 {len(hit_rows)} 根，需要 {n} 根")
        return {"代码": code, "复权": adjust or "不复权", "根数": len(rows),
                "数据来源": "网络（腾讯/新浪）", "缓存命中": False, "回源原因": why,
                "网络请求": 1, "取数耗时": f"{cost}s",
                "已写入缓存": wrote,
                "未写入": len(rows) - wrote,
                "未写入原因": "当日K线盘中还在动，不缓存" if len(rows) > wrote else None,
                "K线": rows}
    finally:
        cx.close()


def daily_rows(code: str, n: int = 30, adjust: str = "qfq") -> list[dict]:
    """只要 K 线列表，签名与 lm.daily 一致，可直接替换调用。"""
    return daily(code, n, adjust)["K线"]


# ---------- 分钟线缓存 ----------

def _read_minute(cx, code: str, period: int, n: int) -> list[dict]:
    cur = cx.execute(
        "SELECT dt,o,h,l,c,vol FROM minute WHERE code=? AND period=? "
        "ORDER BY dt DESC LIMIT ?", (code, period, n))
    return [{"时间": dt, "开": o, "收": c, "高": h, "低": l,
             "量(手)": int(vol) if vol is not None else None}
            for dt, o, h, l, c, vol in cur.fetchall()[::-1]]


def _write_minute(cx, code: str, period: int, rows: list[dict]) -> int:
    """
    写分钟线，判定口径与日线**共用 `_settled()`**：

      盘中     → 今天所有分钟线都不写（尾部还在动，且无法逐根判断哪根走完了）
      收盘之后 → 今天的分钟线已定型，可以写

    上一版写死「日期 < 今天」，后果是收盘后查当天的分钟线永远存不进缓存，
    每次都得重新回源 —— 而那时候数据其实已经定型了。
    """
    data = [(code, period, r["时间"], r.get("开"), r.get("高"), r.get("低"),
             r.get("收"), r.get("量(手)"))
            for r in rows if _settled(str(r.get("时间", ""))[:10])]
    if data:
        cx.executemany("INSERT OR REPLACE INTO minute"
                       "(code,period,dt,o,h,l,c,vol) VALUES(?,?,?,?,?,?,?,?)", data)
        cx.commit()
    return len(data)


def _meta_get(cx, k: str) -> str | None:
    r = cx.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def _meta_set(cx, k: str, v: str) -> None:
    cx.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, v))
    cx.commit()


def minute(code: str, period: int = 5, n: int = 320,
           force: bool = False) -> dict:
    """
    分钟 K 线「最近 n 根」，带缓存。**当日数据永不缓存**（尾部还在动）。

    ⚠️ 所以在交易日调这个函数，**必然回源**，缓存帮不上忙 ——
    「最近 n 根」这个语义天然包含今天，而今天的不能缓存。这不是 bug，
    是「最近 n 根」和「不缓存当日」两个约束的必然结果。

    命中条件用「上次从网络看到的最新一根的日期」做基准，不用日历：
      缓存最新 >= 上次网络观测到的最新 → 命中
    这样休市日（周末、节假日）能命中，因为那时最新一根就是上个交易日的，
    而它已经在缓存里。用日历推「最近交易日」会在节假日算错，所以不用。

    **真正靠缓存省事的是按日期查历史那一天** —— 用 day_bars()，
    它查的是「某个已收盘日期的全部分钟线」，与今天无关，必然命中。
    """
    cx = _conn()
    try:
        mk = f"last_bar:{code}:{period}"
        have = _read_minute(cx, code, period, n)
        newest = have[-1]["时间"][:10] if have else None
        seen = _meta_get(cx, mk)          # 上次从网络观测到的最新一根日期
        fresh = bool(newest and seen and newest >= seen)
        if have and len(have) >= n and fresh and not force:
            return {"代码": code, "周期": f"{period}分钟", "根数": len(have),
                    "数据来源": "本地缓存", "缓存命中": True, "网络请求": 0,
                    "时间范围": f"{have[0]['时间']} ~ {have[-1]['时间']}",
                    "K线": have}
        t0 = time.time()
        rows = ex.minute_kline(code, period, n)
        cost = round(time.time() - t0, 2)
        wrote = _write_minute(cx, code, period, rows)
        if rows:
            _meta_set(cx, mk, rows[-1]["时间"][:10])
        why = ("强制刷新" if force else
               "缓存无此票" if not have else
               "还没有网络观测基准（首次查这个周期）" if not seen else
               f"缓存最新 {newest} 落后于上次观测到的 {seen}"
               "（当日分钟线不缓存，交易日必然回源）" if not fresh else
               f"缓存只有 {len(have)} 根，需要 {n} 根")
        return {"代码": code, "周期": f"{period}分钟", "根数": len(rows),
                "数据来源": "网络（腾讯/新浪）", "缓存命中": False, "回源原因": why,
                "网络请求": 1, "取数耗时": f"{cost}s",
                "已写入缓存": wrote, "未写入(当日不缓存)": len(rows) - wrote,
                "时间范围": f"{rows[0]['时间']} ~ {rows[-1]['时间']}" if rows else None,
                "K线": rows}
    finally:
        cx.close()


def day_bars(code: str, date: str, period: int = 5,
             fetch_n: int = 3000) -> dict:
    """
    某个**已收盘日期**的全部分钟线。这是分钟缓存真正能命中的用法。

    典型场景：对比「今天和上周三的日内结构差异」、回看某天的冲高回落。
    这类查询与今天无关，所以第一次取过之后永久命中（历史数据不会再变）。

    date 格式 YYYY-MM-DD。当天或未来日期直接拒绝 —— 那种情况该用 minute()
    或 get_intraday，混进来会把半截 K 线存成历史。

    缓存没有时按 fetch_n 根回源（3000 根 5 分钟线约能回溯 63 个交易日），
    整批落库，所以第一次查会顺带把附近几十个交易日都缓存好。
    """
    d = str(date)[:10]
    if not _settled(d):
        return {"代码": code, "日期": d, "错误": "该日期尚未收盘定型",
                "说明": "day_bars 只服务已收盘的历史日期。今天的分钟线请用 "
                        "minute() 或 get_intraday，它们每次取实时值"}
    cx = _conn()
    try:
        cur = cx.execute(
            "SELECT dt,o,h,l,c,vol FROM minute WHERE code=? AND period=? "
            "AND dt LIKE ? ORDER BY dt", (code, period, f"{d}%"))
        rows = [{"时间": dt, "开": o, "收": c, "高": h, "低": l,
                 "量(手)": int(vol) if vol is not None else None}
                for dt, o, h, l, c, vol in cur.fetchall()]
        if rows:
            return {"代码": code, "日期": d, "周期": f"{period}分钟",
                    "根数": len(rows), "数据来源": "本地缓存",
                    "缓存命中": True, "网络请求": 0, "K线": rows}
        t0 = time.time()
        got = ex.minute_kline(code, int(period), fetch_n)
        cost = round(time.time() - t0, 2)
        wrote = _write_minute(cx, code, period, got)
        if got:
            _meta_set(cx, f"last_bar:{code}:{period}", got[-1]["时间"][:10])
        day = [r for r in got if str(r["时间"])[:10] == d]
        covered = sorted({str(r["时间"])[:10] for r in got})
        if not day:
            return {"代码": code, "日期": d, "周期": f"{period}分钟", "根数": 0,
                    "数据来源": "网络", "缓存命中": False, "网络请求": 1,
                    "数据状态": "该日期无分钟线",
                    "可能原因": f"{d} 非交易日，或超出可回溯范围"
                                f"（本次取回覆盖 {covered[0]} ~ {covered[-1]}）"
                    if covered else "回源返回空",
                    "顺带缓存": wrote, "K线": []}
        return {"代码": code, "日期": d, "周期": f"{period}分钟", "根数": len(day),
                "数据来源": "网络（腾讯/新浪）", "缓存命中": False, "网络请求": 1,
                "取数耗时": f"{cost}s", "顺带缓存根数": wrote,
                "顺带缓存了": f"{covered[0]} ~ {covered[-1]} 共 {len(covered)} 天"
                if covered else None,
                "K线": day}
    finally:
        cx.close()


# ---------- 维护 ----------

def stats() -> dict:
    """缓存占用与覆盖。想知道「到底占了多少硬盘」就调这个。"""
    if not DB.exists():
        return {"缓存文件": str(DB), "状态": "尚未创建（还没查过任何历史数据）",
                "占用": "0 MB"}
    cx = _conn()
    try:
        d_n, d_codes, d_min, d_max = cx.execute(
            "SELECT COUNT(*),COUNT(DISTINCT code),MIN(date),MAX(date) FROM daily"
        ).fetchone()
        m_n, m_codes, m_min, m_max = cx.execute(
            "SELECT COUNT(*),COUNT(DISTINCT code),MIN(dt),MAX(dt) FROM minute"
        ).fetchone()
        per = cx.execute("SELECT period,COUNT(*) FROM minute "
                         "GROUP BY period ORDER BY period").fetchall()
    finally:
        cx.close()
    # WAL 模式下 -wal 文件也占空间，一起算才是真实占用
    size = sum(f.stat().st_size for f in
               [DB, DB.with_suffix(".db-wal"), DB.with_suffix(".db-shm")]
               if f.exists())
    total = (d_n or 0) + (m_n or 0)
    return {
        "缓存文件": str(DB),
        "占用": f"{round(size / 1048576, 2)} MB",
        "总行数": total,
        "单行均摊": f"{round(size / total)} B" if total else "—",
        "日线": {"行数": d_n, "股票数": d_codes, "日期范围": f"{d_min} ~ {d_max}"
                 if d_min else "空"},
        "分钟线": {"行数": m_n, "股票数": m_codes, "时间范围": f"{m_min} ~ {m_max}"
                   if m_min else "空",
                   "各周期行数": {f"{p}分钟": c for p, c in per} or "空"},
        "换算参考": "实测约 93-94 B/行。全市场日线 1 年约 109 MB，"
                    "300 只 5 分钟线 1 年约 315 MB。本缓存只存查过的票，"
                    "实际占用远小于全市场",
        "说明": "删掉这个文件不会丢数据，下次查询自动重建",
    }


def clear(code: str = "") -> dict:
    """清缓存。code 传空清全部，传代码只清那一只。不删库文件本身。"""
    cx = _conn()
    try:
        if code:
            d = cx.execute("DELETE FROM daily WHERE code=?", (code,)).rowcount
            m = cx.execute("DELETE FROM minute WHERE code=?", (code,)).rowcount
        else:
            d = cx.execute("DELETE FROM daily").rowcount
            m = cx.execute("DELETE FROM minute").rowcount
        cx.commit()
    finally:
        cx.close()
    return {"清理范围": code or "全部", "删除日线行": d, "删除分钟行": m,
            "提示": "空间要跑 vacuum 才会真正释放"}


def vacuum() -> dict:
    """整理碎片，真正释放删除后的空间。"""
    before = DB.stat().st_size if DB.exists() else 0
    cx = sqlite3.connect(DB, timeout=60)
    try:
        cx.execute("VACUUM")
    finally:
        cx.close()
    after = DB.stat().st_size if DB.exists() else 0
    return {"整理前": f"{round(before / 1048576, 2)} MB",
            "整理后": f"{round(after / 1048576, 2)} MB",
            "释放": f"{round((before - after) / 1048576, 2)} MB"}


# ---------- CLI ----------

def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    cmd = argv[1] if len(argv) > 1 else "stats"
    a2 = argv[2] if len(argv) > 2 else ""
    a3 = argv[3] if len(argv) > 3 else ""
    p = lambda o: print(json.dumps(o, ensure_ascii=False, indent=2))  # noqa: E731
    if cmd == "stats":
        p(stats())
    elif cmd == "daily":
        r = daily(a2 or "600519", int(a3 or 30))
        r["K线"] = r["K线"][-3:]          # CLI 只看尾部三根，别刷屏
        p(r)
    elif cmd == "minute":
        r = minute(a2 or "600519", int(a3 or 5))
        r["K线"] = r["K线"][-3:]
        p(r)
    elif cmd == "clear":
        p(clear(a2))
    elif cmd == "vacuum":
        p(vacuum())
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
