# -*- coding: utf-8 -*-
"""
rps_local.py —— 直接读通达信本地文件算三线红名单。

不需要写选股公式，不需要手工导出，不需要点菜单。两个输入都已经在本机：

  RPS   C:\\new_tdx\\T0002\\extdata\\extdata_{1,2,3}.{idx,dat}
        编号 1=120日  2=250日  3=50日（与作者一致；本机 4/5 与作者次序相反，三线红不用）
  日线  C:\\new_tdx\\vipdoc\\{sh,sz,bj}\\lday\\<市场><代码>.day

产出格式与 rps_pool.parse_export() 完全一致，直接喂 rps_pool.save_daily()，
下游（stat / diff / 7指标 / MCP 工具）一行都不用改。

⚠️ 已验证与未验证的部分见文件末尾「验证记录」。板块三线红（编号 7-11）本机没装，
   本脚本不算，也不假装能算。

命令行：
    python rps_local.py dates                    # 本机有哪些日期可算
    python rps_local.py calc                     # 算最近可算日
    python rps_local.py calc --date 2026-02-09   # 算指定日
    python rps_local.py calc --preset base       # 换阈值口径
    python rps_local.py save                     # 算完直接写进每日名单
    python rps_local.py backfill --days 60       # 回补最近 60 个可算日
"""
from __future__ import annotations

import json
import os
import struct
import sys
from datetime import datetime

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))

# ---------- 通达信位置 ----------
# 多候选，按顺序取第一个 extdata 里真有数据的。本机实测只有 new_tdx 有。
TDX_CANDIDATES = (
    r"C:\new_tdx",
    r"C:\tc_hbzq",
    r"C:\zd_zszq",
    r"C:\Program Files\new_tdx",
)

# 扩展数据编号 → 周期。本机 extdata.info 实读：1=ROC120 2=ROC250 3=ROC50 4=ROC10 5=ROC20，
# 每条的第二个名字字段都是 RPS_BASE —— 数据集本身叫 RPS，ROC 只是槽位显示名。
SLOT_PERIOD = {1: 120, 2: 250, 3: 50}

# 市场号 → vipdoc 子目录。idx 前两字节小端，实测 0=深 2875 只 / 1=沪 2293 只 / 2=北 291 只
MARKET_DIR = {0: "sz", 1: "sh", 2: "bj"}

# 阈值口径。fine 是作者第 28 页「做了修正」的那套，也是默认。
PRESETS = {
    "fine": {"label": "精细版 120>93 250>95 50>90", "rps": {120: 93, 250: 95, 50: 90}},
    "base": {"label": "基线 三线均 >90", "rps": {120: 90, 250: 90, 50: 90}},
    "oneil": {"label": "欧奈尔 三线均 >87", "rps": {120: 87, 250: 87, 50: 87}},
}
DEFAULT_PRESET = "fine"

# JJXG:=H/HHV(HIGH,N)>0.85 —— 作者公式里的近高点约束
HHV_N = 150
HHV_RATIO = 0.85

# 日线记录 32 字节定长：日期 开 高 低 收（都是价×100 的整数）成交额(float) 成交量 保留
LDAY_FMT = "<IIIIIfII"
LDAY_SIZE = struct.calcsize(LDAY_FMT)   # 32

_IDX_SIZE = 29                          # 扩展数据索引记录长度
_DAT_SIZE = 12                          # 扩展数据条目长度：日期 int32 + 零 int32 + 值 float32


# ---------- 定位通达信 ----------

def find_tdx() -> str:
    """返回第一个 extdata 里真有 .dat 的通达信根目录。找不到抛异常，不静默返回空。"""
    tried = []
    for root in TDX_CANDIDATES:
        ext = os.path.join(root, "T0002", "extdata")
        tried.append(ext)
        if not os.path.isdir(ext):
            continue
        if any(os.path.exists(os.path.join(ext, f"extdata_{n}.dat")) for n in SLOT_PERIOD):
            return root
    raise FileNotFoundError(
        "没找到装了扩展数据的通达信。查过这些位置：\n  " + "\n  ".join(tried)
        + "\n如果你的通达信装在别处，把路径加到本文件的 TDX_CANDIDATES。")


def extdata_dir(root: str) -> str:
    return os.path.join(root, "T0002", "extdata")


def lday_dir(root: str, mk: str) -> str:
    return os.path.join(root, "vipdoc", mk, "lday")


# ---------- 读扩展数据（RPS） ----------

def read_slot(root: str, slot: int) -> dict:
    """
    读一个扩展数据槽位 → {"by_code": {代码: {日期int: 值}}, ...元信息}

    idx 每 29 字节一条：[0:2] 市场号小端 / [2:8] ASCII 六位代码 / [8:25] 全零 / [25:29] 条数
    dat 按 idx 顺序顺序排布，每条 12 字节：日期(YYYYMMDD int32) + 零 int32 + 值 float32

    值是 RPS×10（0-1000）。这是实测结论，不是猜的：某一天全市场 5459 只票
    分 10 个百等份桶得 545/546/546/546/546/546/546/546/546/545，
    理论均匀值 545.9 —— 只有排名会这么均匀，涨幅会挤在 0 附近拖长尾。
    """
    ed = extdata_dir(root)
    pi = os.path.join(ed, f"extdata_{slot}.idx")
    pd = os.path.join(ed, f"extdata_{slot}.dat")
    for p in (pi, pd):
        if not os.path.exists(p):
            raise FileNotFoundError(f"缺文件：{p}")

    raw_i = open(pi, "rb").read()
    if len(raw_i) % _IDX_SIZE:
        raise ValueError(f"{pi} 长度 {len(raw_i)} 不是 {_IDX_SIZE} 的整数倍，格式可能变了")
    n_rec = len(raw_i) // _IDX_SIZE

    entries = []                 # [(market, code, count), ...] 顺序即 dat 里的顺序
    total = 0
    for i in range(n_rec):
        r = raw_i[i * _IDX_SIZE:(i + 1) * _IDX_SIZE]
        market = int.from_bytes(r[0:2], "little")
        code = r[2:8].decode("ascii", "replace")
        cnt = int.from_bytes(r[25:29], "little")
        entries.append((market, code, cnt))
        total += cnt

    raw_d = open(pd, "rb").read()
    want = total * _DAT_SIZE
    if want != len(raw_d):
        raise ValueError(f"{pd} 字节数 {len(raw_d)} 与 idx 条数合计推算的 {want} 不符，"
                         "格式或文件不完整")

    # 一次性向量化解析，比逐条 struct.unpack 快两个数量级
    arr = np.frombuffer(raw_d, dtype=np.dtype([("date", "<i4"), ("zero", "<i4"), ("val", "<f4")]))
    dates_all = arr["date"]
    vals_all = arr["val"]

    by_code: dict[str, dict] = {}
    mkt_count: dict[int, int] = {}
    off = 0
    for market, code, cnt in entries:
        d = dates_all[off:off + cnt]
        v = vals_all[off:off + cnt]
        off += cnt
        by_code[code] = {"market": market, "dates": d, "vals": v}
        mkt_count[market] = mkt_count.get(market, 0) + 1

    return {"by_code": by_code, "记录数": n_rec, "条目合计": total,
            "各市场票数": {MARKET_DIR.get(k, str(k)): v for k, v in sorted(mkt_count.items())},
            "文件": os.path.basename(pd),
            "更新时间": datetime.fromtimestamp(os.path.getmtime(pd)).isoformat(timespec="seconds")}


def slot_dates(slot_data: dict, sample: int = 200) -> list[int]:
    """
    这个槽位里出现过哪些日期（int YYYYMMDD，升序）。
    抽样而非全扫：5459 只票的日期序列高度一致，抽 200 只足够列出交易日集合。
    """
    seen: set[int] = set()
    for i, rec in enumerate(slot_data["by_code"].values()):
        if i >= sample:
            break
        seen.update(int(x) for x in rec["dates"])
    return sorted(seen)


def slot_cross_section(slot_data: dict, date_i: int) -> dict[str, float]:
    """取某一天的横截面 {代码: RPS}（已 /10 还原成 0-100）。缺这天的票直接不出现。"""
    out: dict[str, float] = {}
    for code, rec in slot_data["by_code"].items():
        d = rec["dates"]
        pos = np.searchsorted(d, date_i)
        if pos < len(d) and int(d[pos]) == date_i:
            out[code] = float(rec["vals"][pos]) / 10.0
    return out


# ---------- 读日线（算 H/HHV） ----------

def lday_path(root: str, market: int, code: str) -> str | None:
    """<市场前缀><代码>.day，前缀 sh/sz/bj。文件不存在返回 None，不抛。"""
    mk = MARKET_DIR.get(market)
    if not mk:
        return None
    p = os.path.join(lday_dir(root, mk), f"{mk}{code}.day")
    return p if os.path.exists(p) else None


def read_lday(path: str) -> np.ndarray:
    """
    读一个 .day 文件 → structured array（date/open/high/low/close/amount/volume）。
    价格字段是原始整数（价×100），本函数不除 —— H/HHV 是比值，除不除结果一样，
    少一次浮点转换。
    """
    raw = open(path, "rb").read()
    n = len(raw) // LDAY_SIZE
    if n == 0:
        return np.empty(0, dtype=[("date", "<u4"), ("high", "<u4"), ("close", "<u4")])
    arr = np.frombuffer(raw[: n * LDAY_SIZE], dtype=np.dtype([
        ("date", "<u4"), ("open", "<u4"), ("high", "<u4"), ("low", "<u4"),
        ("close", "<u4"), ("amount", "<f4"), ("volume", "<u4"), ("rsv", "<u4"),
    ]))
    return arr


def hhv_ratio_at(arr: np.ndarray, date_i: int, n: int = HHV_N) -> tuple[float | None, str]:
    """
    算 H/HHV(HIGH,N)：该日最高价 ÷ 含该日往前 N 根的最高价。

    返回 (比值, 状态)。状态取值：
      ok            正常
      no_bar        这只票在该日没有日线（停牌或未上市）
      short_history 日线不足 N 根 —— 照样算，但用实际根数，并在状态里说明
    """
    if len(arr) == 0:
        return None, "no_bar"
    d = arr["date"]
    pos = int(np.searchsorted(d, date_i))
    if pos >= len(d) or int(d[pos]) != date_i:
        return None, "no_bar"
    lo = max(0, pos - n + 1)
    win = arr["high"][lo:pos + 1]
    hhv = float(win.max())
    if hhv <= 0:
        return None, "no_bar"
    ratio = float(arr["high"][pos]) / hhv
    return ratio, ("ok" if (pos - lo + 1) >= n else "short_history")


# ---------- 三线红 ----------

def calc(date: str | None = None, preset: str = DEFAULT_PRESET,
         hhv_n: int = HHV_N, hhv_ratio: float = HHV_RATIO,
         root: str | None = None, _slots: dict | None = None,
         _lday_cache: dict | None = None) -> dict:
    """
    算某一天的三线红名单。

    date 传 None 取「三个槽位都有、且日线也对齐得上」的最近一天。
    返回 {"date","口径","stocks":[{code,name,industry,match,rps120,rps250,rps50,hhv}],
          "统计",...}，stocks 可直接喂 rps_pool.save_daily()。

    _slots / _lday_cache 是给 backfill() 复用的，单独调用不用传 ——
    三个槽位合计约 400 MB，回补几十天时重复读会白等几分钟。
    """
    if preset not in PRESETS:
        raise ValueError(f"未知口径 {preset}，可选：{'/'.join(PRESETS)}")
    th = PRESETS[preset]["rps"]
    root = root or find_tdx()

    slots = _slots if _slots is not None else {s: read_slot(root, s) for s in SLOT_PERIOD}
    per = {SLOT_PERIOD[s]: v for s, v in slots.items()}

    # 三个槽位共有的日期
    common = None
    for v in slots.values():
        ds = set(slot_dates(v))
        common = ds if common is None else (common & ds)
    common = sorted(common or [])
    if not common:
        raise RuntimeError("三个扩展数据槽位没有共同日期，无法计算")

    if date:
        di = int(str(date).replace("-", ""))
        if di not in common:
            raise ValueError(f"{date} 不在可算日期里。最近可算：{fmt_date(common[-1])}，"
                             f"共 {len(common)} 天，用 dates 子命令看全部")
    else:
        di = common[-1]

    xs = {p: slot_cross_section(v, di) for p, v in per.items()}
    cov = {p: len(x) for p, x in xs.items()}

    # 先按 RPS 过三线，再逐只查日线（日线要开文件，能少开就少开）
    codes_all = set(xs[120]) & set(xs[250]) & set(xs[50])
    rps_pass = [c for c in sorted(codes_all)
                if xs[120][c] > th[120] and xs[250][c] > th[250] and xs[50][c] > th[50]]

    market_of = {c: slots[1]["by_code"][c]["market"] for c in rps_pass
                 if c in slots[1]["by_code"]}

    final, cut_hhv, no_bar, short_hist = [], [], [], []
    for c in rps_pass:
        if _lday_cache is not None and c in _lday_cache:
            arr = _lday_cache[c]
        else:
            p = lday_path(root, market_of.get(c, -1), c)
            arr = read_lday(p) if p else None
            if _lday_cache is not None:
                _lday_cache[c] = arr
        if arr is None:
            no_bar.append(c)
            continue
        ratio, st = hhv_ratio_at(arr, di, hhv_n)
        if ratio is None:
            no_bar.append(c)
            continue
        if st == "short_history":
            short_hist.append(c)
        if ratio > hhv_ratio:
            final.append((c, ratio))
        else:
            cut_hhv.append(c)

    # 补名字和行业。这一步复用 rps_pool 的代码表，不另起一套
    sys.path.insert(0, _HERE)
    import rps_pool
    m = rps_pool.load_symbol_map()
    by_code = m["by_code"]

    stocks = []
    for c, ratio in final:
        info = by_code.get(c) or {}
        stocks.append({
            "code": c,
            "name": info.get("name", ""),
            "industry": info.get("industry", ""),
            "match": "local_extdata" if info else "local_extdata_unknown",
            "rps120": round(xs[120][c], 1),
            "rps250": round(xs[250][c], 1),
            "rps50": round(xs[50][c], 1),
            "hhv": round(ratio, 3),
        })
    stocks.sort(key=lambda s: -(s["rps120"] + s["rps250"] + s["rps50"]))

    warnings = []
    unknown = [s["code"] for s in stocks if s["match"] == "local_extdata_unknown"]
    if unknown:
        warnings.append(f"⚠️ {len(unknown)} 只票在东财代码表里查不到名字（可能已退市或新上市）："
                        + "、".join(unknown[:10]) + "。已保留在名单里，不丢数据")
    if no_bar:
        warnings.append(f"ℹ️ {len(no_bar)} 只过了 RPS 但该日没有日线（停牌/未上市/日线未下载），"
                        f"已排除：{'、'.join(no_bar[:10])}")
    if short_hist:
        warnings.append(f"ℹ️ {len(short_hist)} 只日线不足 {hhv_n} 根，H/HHV 用实际根数算")

    # 日线整体对齐情况：扩展数据到今天但日线还停在昨天，是最容易踩的坑。
    # 回补历史日时跳过 —— 那些日期本来就在日线中段，不存在"没跟上"的问题。
    align = {"跳过": "回补模式"} if _slots is not None else check_align(root, di, sample=60)
    if align.get("未对齐比例", 0) > 0.5:
        warnings.append(
            f"⚠️ 抽查 {align['抽样数']} 只票，{align['未对齐']} 只的日线里没有 {fmt_date(di)} 这天"
            f"（最近日线多为 {align['日线最近日']}）。扩展数据已更新但日线没跟上，"
            "H/HHV 会大面积算不出来。修法：通达信里做一次盘后数据下载（日线）")

    return {
        "date": fmt_date(di),
        "口径": PRESETS[preset]["label"] + f" + H/HHV({hhv_n})>{hhv_ratio}",
        "preset": preset,
        "stocks": stocks,
        "统计": {
            "过三线RPS": len(rps_pass),
            "过HHV(最终)": len(stocks),
            "被HHV砍掉": len(cut_hhv),
            "无日线排除": len(no_bar),
            "三周期齐全票数": len(codes_all),
            "各周期覆盖": {f"RPS{p}": cov[p] for p in sorted(cov)},
        },
        "warnings": warnings,
        "通达信目录": root,
        "扩展数据更新时间": {f"编号{s}": v["更新时间"] for s, v in sorted(slots.items())},
    }


def check_align(root: str, date_i: int, sample: int = 60) -> dict:
    """抽查日线有没有跟上到 date_i。返回未对齐比例和日线实际最近日。"""
    n_ok = n_miss = 0
    last_seen: dict[int, int] = {}
    for mk in ("sh", "sz"):
        d = lday_dir(root, mk)
        if not os.path.isdir(d):
            continue
        files = sorted(os.listdir(d))[: sample // 2]
        for fn in files:
            if not fn.endswith(".day"):
                continue
            arr = read_lday(os.path.join(d, fn))
            if len(arr) == 0:
                continue
            ld = int(arr["date"][-1])
            last_seen[ld] = last_seen.get(ld, 0) + 1
            if int(np.searchsorted(arr["date"], date_i)) < len(arr) and \
                    date_i in arr["date"][-5:]:
                n_ok += 1
            else:
                n_miss += 1
    tot = n_ok + n_miss
    mode = max(last_seen.items(), key=lambda kv: kv[1])[0] if last_seen else 0
    return {"抽样数": tot, "对齐": n_ok, "未对齐": n_miss,
            "未对齐比例": (n_miss / tot) if tot else 0.0,
            "日线最近日": fmt_date(mode) if mode else "无"}


def fmt_date(i: int) -> str:
    s = str(int(i))
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


# ---------- 可算日期 ----------

def dates(root: str | None = None) -> dict:
    """本机能算哪些日期。三个槽位的日期取交集，再报日线对齐情况。"""
    root = root or find_tdx()
    slots = {s: read_slot(root, s) for s in SLOT_PERIOD}
    per_dates = {SLOT_PERIOD[s]: slot_dates(v) for s, v in slots.items()}
    common = None
    for ds in per_dates.values():
        st = set(ds)
        common = st if common is None else (common & st)
    common = sorted(common or [])
    align = check_align(root, common[-1], sample=60) if common else {}
    return {
        "通达信目录": root,
        "可算天数": len(common),
        "最早": fmt_date(common[0]) if common else "无",
        "最近": fmt_date(common[-1]) if common else "无",
        "最近10天": [fmt_date(x) for x in common[-10:]],
        "各周期天数": {f"RPS{p}": len(ds) for p, ds in sorted(per_dates.items())},
        "扩展数据更新时间": {f"编号{s}": v["更新时间"] for s, v in sorted(slots.items())},
        "各市场票数": slots[1]["各市场票数"],
        "日线对齐": align,
    }


# ---------- 写进每日名单 ----------

def save(date: str | None = None, preset: str = DEFAULT_PRESET,
         kind: str = "三线红", root: str | None = None) -> dict:
    """
    算完直接写 data/rps_pool_daily/<日期>_<类型>.csv，并同步计数流水。
    同日同类型重复跑是覆盖（幂等），不会算成两天。
    """
    r = calc(date=date, preset=preset, root=root)
    sys.path.insert(0, _HERE)
    import rps_pool
    p = rps_pool.save_daily(r["date"], kind, r["stocks"])
    sync = rps_pool.sync_count_log()
    return {"date": r["date"], "口径": r["口径"], "只数": len(r["stocks"]),
            "名单文件": p, "统计": r["统计"], "warnings": r["warnings"],
            "流水同步": sync}


def backfill(days: int = 60, preset: str = DEFAULT_PRESET, kind: str = "三线红",
             root: str | None = None, overwrite: bool = False) -> dict:
    """
    回补最近 N 个可算日的名单。

    这是作者第 24/25 页那张表能立刻用起来的前提 —— 那张表的
    「入连续天数 / 入总天数 / 入池次数 / 距60天第一次」都要跨日数据才算得出，
    本机扩展数据有 4235 天，不用等几周攒。

    overwrite=False 时已有的日期跳过（默认），True 时全部重算覆盖。
    """
    root = root or find_tdx()
    sys.path.insert(0, _HERE)
    import rps_pool

    slots = {s: read_slot(root, s) for s in SLOT_PERIOD}     # 读一次，全程复用
    common = None
    for v in slots.values():
        ds = set(slot_dates(v))
        common = ds if common is None else (common & ds)
    common = sorted(common or [])
    targets = common[-days:]

    have = set(rps_pool.list_dates(kind))
    cache: dict = {}
    done, skipped, failed = [], [], []
    for di in targets:
        ds = fmt_date(di)
        if not overwrite and ds in have:
            skipped.append(ds)
            continue
        try:
            r = calc(date=ds, preset=preset, root=root, _slots=slots, _lday_cache=cache)
            rps_pool.save_daily(ds, kind, r["stocks"])
            done.append({"date": ds, "只数": len(r["stocks"]),
                         "过三线RPS": r["统计"]["过三线RPS"]})
        except Exception as e:                   # noqa: BLE001 - 单日失败不能拖垮整批
            failed.append({"date": ds, "错误": f"{type(e).__name__}: {e}"})

    sync = rps_pool.sync_count_log()
    return {"口径": PRESETS[preset]["label"], "目标天数": len(targets),
            "新算": len(done), "跳过(已有)": len(skipped), "失败": len(failed),
            "日期范围": f"{fmt_date(targets[0])} ~ {fmt_date(targets[-1])}" if targets else "无",
            "逐日": done, "失败明细": failed, "流水同步": sync}


# ---------- 命令行 ----------

def _p(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    rest = argv[1:]

    def opt(name: str, default=None):
        if name in rest:
            i = rest.index(name)
            if i + 1 < len(rest):
                return rest[i + 1]
        return default

    date = opt("--date")
    preset = opt("--preset", DEFAULT_PRESET)

    try:
        if cmd == "dates":
            _p(dates())
        elif cmd == "calc":
            r = calc(date=date, preset=preset)
            top = r["stocks"][:30]
            _p({**{k: v for k, v in r.items() if k != "stocks"},
                "前30只": top, "名单总数": len(r["stocks"])})
        elif cmd == "save":
            _p(save(date=date, preset=preset))
        elif cmd == "backfill":
            days = int(opt("--days", "60"))
            r = backfill(days=days, preset=preset, overwrite=("--overwrite" in rest))
            _p({**{k: v for k, v in r.items() if k != "逐日"},
                "逐日(后10天)": r["逐日"][-10:]})
        else:
            print(f"未知命令 {cmd}。可用：dates / calc / save / backfill")
            return 2
    except Exception as e:                       # noqa: BLE001 - 命令行入口统一报错
        print(f"{type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))


# ==================== 验证记录 ====================
#
# 一、格式是怎么确定的（三条恒等式，全部闭合）
#   1. idx 各条 count 合计 × 12 == dat 文件字节数（差 0）
#   2. 每条 12 字节里中间 4 字节恒为 0（5459 只票全查）
#   3. 5459 只票的末条日期全部等于同一天（扩展数据更新日）
#
# 二、值到底是 RPS 还是涨幅（结论：排名 ×10）
#   最新横截面 5459 个值分 10 个百等份桶：545/546/546/546/546/546/546/546/546/545
#   理论均匀值 545.9。只有排名会这么均匀 —— 涨幅会挤在 0 附近拖长尾。
#   min 0 / median 500 / max 1000，正好解释作者公式里的 /10。
#   第二个独立证据：extdata.info（293 字节定长 5 条）每条第二个名字字段都是 RPS_BASE。
#
# 三、日线解析对不对（与东财逐位比对）
#   20260804 三只票：行云 high 33.64 / close 33.64、盛科 342.98 / 338.0、
#   茅台 1350.94 / 1328.36 —— 与东财逐位相同。
#
# 四、三线红复现对不对（拿作者第 24 页 20260209 真名单当标尺）
#   OCR 能认出的 35 个名字全部反查到代码，0 查不到、0 多义。
#
#       口径              仅RPS  +HHV150>0.85   命中作者名单
#       93/95/90 精细版    126        83         35/35
#       90/90/90 基线      213       151         35/35
#       87/87/87 欧奈尔    302       221         35/35
#
#   召回 100%（三档阈值都是 35/35），精细版 126→83 跨过作者说的约 101。
#
# 五、还没验证的
#   - 精确率。OCR 只认出 35 个名字，作者那天序号到 57，我算 83 只偏多。
#     可能 N 不等于 150，也可能作者的 RPS 源与本机 ROC 有细微差异。
#   - 板块三线红（编号 7-11 BKRPS）本机没装，本脚本不算，也不假装能算。
#     作者第 31 页自述「我没有特别挖掘这方面」。
#   - 本机编号 4=ROC10 / 5=ROC20，与作者的 4=RPS20 / 5=RPS10 次序相反。
#     三线红只用 1/2/3，不受影响，但如果以后要用 10日/20日 RPS 必须注意这个差异。
