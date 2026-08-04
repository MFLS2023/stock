#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
实时行情取数工具（免费、无需注册、无需 API Key）

数据源全部为公开免费接口，三源互备：
  1. 东方财富 push2delay  —— 全市场快照、涨停池、板块资金流（主力）
  2. 腾讯 qt.gtimg.cn     —— 单只详情、五档盘口（备份）
  3. 新浪 hq.sinajs.cn    —— 单只快照（第二备份）

设计要点（都是实测踩出来的，别改）：
  - 全部请求 trust_env=False，绕过系统代理。本机代理 127.0.0.1:7897
    会把东财请求掐断，报 RemoteDisconnected。
  - 东财必须用 push2delay 域名。push2 / push2his / 82.push2 三个常用域名
    在本机网络下一律连不通（实测 2026-08-04）。
    push2delay 虽名为 delay，实测行情时间戳与本地时钟只差 2 秒，是实时数据。
  - 单源失败自动切下一个源，全失败则明确报错，不返回假数据。

用法：
    python live_market.py quote 002827 600519      # 个股实时
    python live_market.py zt                        # 涨停池
    python live_market.py sector                    # 行业资金流
    python live_market.py market                    # 大盘温度计
    python live_market.py all 002827                # 全套（喂给 AI 分析用）
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime

import requests

# ---------- 基础设施 ----------

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# 东财可用域名。push2delay 实测唯一稳定可达，其余留作日后网络变化时兜底。
EM_HOSTS = ["push2delay.eastmoney.com", "push2.eastmoney.com", "82.push2.eastmoney.com"]

# 沪深京全部 A 股的板块筛选串（东财 fs 参数）
FS_ALL_A = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"


def _session() -> requests.Session:
    """绕过系统代理的 Session。代理会掐断东财连接。"""
    s = requests.Session()
    s.trust_env = False
    return s


# 复用连接，避免每次握手。翻页取全市场时连接复用是必须的。
_SESS: requests.Session | None = None
_LAST_CALL = 0.0
MIN_INTERVAL = 0.12   # 相邻请求最小间隔（秒）。实测连续无间隔翻 56 页会被掐断。


def _shared_session() -> requests.Session:
    global _SESS
    if _SESS is None:
        _SESS = _session()
        # 连接池放大，翻页时不用反复建连
        ad = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
        _SESS.mount("https://", ad)
    return _SESS


def _throttle() -> None:
    """限速。东财对高频翻页会直接断连，不是返回错误码。"""
    global _LAST_CALL
    gap = time.time() - _LAST_CALL
    if gap < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - gap)
    _LAST_CALL = time.time()


def _em_get(path: str, params: dict, timeout: int = 10, retries: int = 3) -> dict:
    """
    请求东财接口。域名逐个尝试，每个域名带指数退避重试。
    被限频时表现为连接被断（RemoteDisconnected），不是 HTTP 错误码，
    所以必须靠重试 + 退避扛过去。
    """
    last = None
    sess = _shared_session()
    for host in EM_HOSTS:
        for attempt in range(retries):
            try:
                _throttle()
                r = sess.get(f"https://{host}{path}", params=params, headers=UA, timeout=timeout)
                r.raise_for_status()
                j = r.json()
                if j.get("data"):
                    return j["data"]
                last = f"{host} 返回空 data"
                break               # 空 data 是参数问题，重试无意义，换域名
            except Exception as e:  # noqa: BLE001 - 逐源降级，需捕获所有异常
                last = f"{host}: {type(e).__name__} {e}"
                if attempt < retries - 1:
                    time.sleep(0.4 * (2 ** attempt))   # 0.4s → 0.8s
                    global _SESS                        # 连接可能已废，重建
                    _SESS = None
                    sess = _shared_session()
    raise RuntimeError(f"东财所有域名均失败，最后一次：{last}")


def _secid(code: str) -> str:
    """股票代码转东财 secid。1=沪市，0=深市/北交所。"""
    code = code.strip()
    if code.startswith(("60", "68", "51", "58", "11")):
        return f"1.{code}"
    return f"0.{code}"


def _fmt(v, dash="—"):
    """东财用 '-' 表示无数据。"""
    return dash if v in ("-", None, "") else v


# ---------- 1. 个股实时 ----------

EM_QUOTE_FIELDS = (
    "f43,f44,f45,f46,f47,f48,f49,f50,f51,f52,f57,f58,f60,f62,f71,f84,f85,f86,"
    "f116,f117,f162,f167,f168,f169,f170,f171,f177,f191,f192,f292"
)


def quote_em(code: str) -> dict | None:
    """东财个股实时。字段最全，含量比、换手、涨速、市值。"""
    try:
        d = _em_get("/api/qt/stock/get",
                    {"secid": _secid(code), "fltt": 2, "invt": 2, "fields": EM_QUOTE_FIELDS})
    except Exception:
        return None
    if not d or not d.get("f58"):
        return None
    ts = d.get("f86")
    return {
        "源": "东财",
        "代码": d.get("f57"),
        "名称": d.get("f58"),
        "最新价": d.get("f43"),
        "涨跌幅": d.get("f170"),
        "涨跌额": d.get("f169"),
        "今开": d.get("f46"),
        "昨收": d.get("f60"),
        "最高": d.get("f44"),
        "最低": d.get("f45"),
        "成交量(手)": d.get("f47"),
        "成交额": d.get("f48"),
        "换手率": d.get("f168"),
        "量比": d.get("f50"),
        "振幅": d.get("f171"),
        "市盈率TTM": d.get("f162"),
        "市净率": d.get("f167"),
        "流通市值": d.get("f117"),
        "总市值": d.get("f116"),
        "涨停价": _fmt(d.get("f51"), None),
        "跌停价": _fmt(d.get("f52"), None),
        "行情时间": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else None,
    }


def quote_tx(code: str) -> dict | None:
    """腾讯个股实时。备份源，含五档盘口。"""
    prefix = "sh" if code.startswith(("60", "68", "51", "58", "11")) else "sz"
    try:
        r = _session().get(f"https://qt.gtimg.cn/q={prefix}{code}", headers=UA, timeout=8)
        r.encoding = "gbk"
        raw = r.text.split('="')[1].rstrip('";\n')
        f = raw.split("~")
    except Exception:
        return None
    if len(f) < 50:
        return None
    t = f[30]
    return {
        "源": "腾讯",
        "代码": f[2],
        "名称": f[1],
        "最新价": float(f[3]),
        "涨跌幅": float(f[32]),
        "涨跌额": float(f[31]),
        "今开": float(f[5]),
        "昨收": float(f[4]),
        "最高": float(f[33]),
        "最低": float(f[34]),
        "成交量(手)": int(f[6]),
        "成交额": float(f[37]) * 10000 if f[37] else None,
        "换手率": float(f[38]) if f[38] else None,
        "量比": float(f[49]) if f[49] else None,
        "振幅": float(f[43]) if f[43] else None,
        "市盈率TTM": float(f[39]) if f[39] else None,
        "涨停价": float(f[47]) if f[47] else None,
        "跌停价": float(f[48]) if f[48] else None,
        "买五档": [(f[9 + i * 2], f[10 + i * 2]) for i in range(5)],
        "卖五档": [(f[19 + i * 2], f[20 + i * 2]) for i in range(5)],
        "行情时间": f"{t[:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}:{t[12:14]}" if len(t) >= 14 else t,
    }


def quote_sina(code: str) -> dict | None:
    """新浪个股实时。第二备份，字段最少但最稳。"""
    prefix = "sh" if code.startswith(("60", "68", "51", "58", "11")) else "sz"
    h = dict(UA, Referer="https://finance.sina.com.cn")
    try:
        r = _session().get(f"https://hq.sinajs.cn/list={prefix}{code}", headers=h, timeout=8)
        r.encoding = "gbk"
        f = r.text.split('="')[1].rstrip('";\n').split(",")
    except Exception:
        return None
    if len(f) < 32:
        return None
    prev, last = float(f[2]), float(f[3])
    return {
        "源": "新浪",
        "代码": code,
        "名称": f[0],
        "最新价": last,
        "涨跌幅": round((last - prev) / prev * 100, 2) if prev else None,
        "今开": float(f[1]),
        "昨收": prev,
        "最高": float(f[4]),
        "最低": float(f[5]),
        "成交量(手)": int(f[8]) // 100 if f[8] else None,
        "成交额": float(f[9]) if f[9] else None,
        "行情时间": f"{f[30]} {f[31]}",
    }


def quote(code: str) -> dict:
    """个股实时，三源自动降级。"""
    for fn in (quote_em, quote_tx, quote_sina):
        d = fn(code)
        if d:
            return d
    raise RuntimeError(f"{code}: 东财/腾讯/新浪三个源全部取数失败")


# ---------- 2. 涨停池 / 强势股 ----------

def _price_cap(code: str, name: str = "") -> int:
    """
    该股的涨跌幅上限（百分比）。
    北交所 30%，创业板/科创板 20%，ST 5%，其余主板 10%。
    新股上市首日无限制，单独识别（名称带 N 或 C 前缀）。
    """
    if name.startswith(("N", "C")):
        return 999  # 新股/次新无涨跌幅限制或 44%，不计入涨停统计
    if "ST" in name:
        return 5
    if code.startswith(("8", "4", "920", "43")):
        return 30
    if code.startswith(("30", "68")):
        return 20
    return 10


def limit_up_pool(limit: int = 120) -> list[dict]:
    """
    实时涨停池。按各板块自己的涨停幅度判定（10/20/30/5），
    不是一刀切 9.8%。新股（N/C 开头）排除，它们没有涨停概念。

    比 akshare 的 stock_zt_pool 强的地方：这个是盘中实时的，
    akshare 那个涨停池盘中数据经常滞后或不可用。
    """
    out, newly = [], []
    # 必须翻页：20cm 票全排在涨幅榜前面，只取前 300 条会把 10cm 涨停全挤掉。
    # 涨幅降序，一旦某页最大涨幅已低于 9.7%，后面不可能再有涨停，提前收工。
    for page in range(1, 8):
        d = _em_get("/api/qt/clist/get", {
            "pn": page, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f3",
            "fs": FS_ALL_A,
            "fields": "f2,f3,f5,f6,f8,f10,f12,f14,f22,f100,f62",
        })
        rows = d.get("diff") or []
        if not rows:
            break
        page_max = max((x["f3"] for x in rows if isinstance(x.get("f3"), (int, float))),
                       default=0)
        for x in rows:
            pct, code, name = x.get("f3"), x.get("f12", ""), x.get("f14", "")
            if not isinstance(pct, (int, float)):
                continue
            cap = _price_cap(code, name)
            row = {
                "代码": code, "名称": name, "最新价": x["f2"], "涨跌幅": pct,
                "板制": f"{cap}cm" if cap != 999 else "新股",
                "成交额(亿)": round(x["f6"] / 1e8, 2) if isinstance(x.get("f6"), (int, float)) else None,
                "换手率": _fmt(x.get("f8")), "量比": _fmt(x.get("f10")),
                "主力净流入(万)": round(x["f62"] / 1e4) if isinstance(x.get("f62"), (int, float)) else None,
                "行业": x.get("f100"),
            }
            if cap == 999:
                newly.append(row)
            elif pct >= cap - 0.3:  # 留 0.3 容差，四舍五入导致的 9.98 也算涨停
                out.append(row)
        if page_max < 9.7 or len(out) >= limit:
            break
    # 涨停股在前，新股附在后面单独标出，不混淆涨停家数
    return out[:limit] + newly[:5]


def strong_pool(limit: int = 30) -> list[dict]:
    """按量比排序的强势股，量比是情绪强度的直接读数。"""
    d = _em_get("/api/qt/clist/get", {
        "pn": 1, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f10",
        "fs": FS_ALL_A, "fields": "f2,f3,f6,f8,f10,f12,f14,f100",
    })
    out = []
    for x in d["diff"]:
        if not isinstance(x.get("f10"), (int, float)):
            continue
        out.append({"代码": x["f12"], "名称": x["f14"], "最新价": x["f2"],
                    "涨跌幅": x["f3"], "量比": x["f10"], "换手率": _fmt(x.get("f8")),
                    "行业": x.get("f100")})
        if len(out) >= limit:
            break
    return out


# ---------- 3. 板块资金流 ----------

def sector_flow(kind: str = "industry", limit: int = 20) -> list[dict]:
    """行业/概念板块实时主力资金流。MCP 里坏掉的那个接口的替代。"""
    fs = "m:90+t:2+f:!50" if kind == "industry" else "m:90+t:3+f:!50"
    d = _em_get("/api/qt/clist/get", {
        "pn": 1, "pz": max(limit, 30), "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f62",
        "fs": fs, "fields": "f2,f3,f12,f14,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205",
    })
    out = []
    for x in d["diff"][:limit]:
        out.append({
            "板块": x["f14"], "涨跌幅": x["f3"],
            "主力净流入(亿)": round(x["f62"] / 1e8, 2) if isinstance(x.get("f62"), (int, float)) else None,
            "主力净占比%": x.get("f184"),
            "超大单净流入(亿)": round(x["f66"] / 1e8, 2) if isinstance(x.get("f66"), (int, float)) else None,
            "领涨股": x.get("f204"),
        })
    return out


# ---------- 4. 大盘温度计 ----------

def market_temp(with_dt: bool = True) -> dict:
    """
    盘面情绪温度：指数 + 涨跌家数 + 涨停跌停数 + 两市成交额。
    判断情绪周期位置的最小充分集。

    涨跌家数用 ulist.np 接口一次取回（字段 f104 涨/f105 跌/f106 平），
    不翻页扫全市场——那样要 56 次请求，会被东财限频掐断，实测 17 秒后失败。
    """
    idx, up, down, flat, total_amt = {}, 0, 0, 0, 0.0
    d = _em_get("/api/qt/ulist.np/get", {
        "fltt": 2, "invt": 2,
        "secids": "1.000001,0.399001,0.399006,1.000688,0.899050",
        "fields": "f2,f3,f4,f6,f12,f14,f104,f105,f106",
    })
    for x in d.get("diff", []):
        name = x.get("f14")
        idx[name] = {
            "点位": x.get("f2"), "涨跌幅": x.get("f3"),
            "成交额(亿)": round(x["f6"] / 1e8, 1) if isinstance(x.get("f6"), (int, float)) else None,
            "上涨": x.get("f104"), "下跌": x.get("f105"), "平盘": x.get("f106"),
        }
        # 沪 + 深 两市合计，创业板/科创50 是深市子集不重复计
        if name in ("上证指数", "深证成指"):
            up += x.get("f104") or 0
            down += x.get("f105") or 0
            flat += x.get("f106") or 0
            total_amt += x.get("f6") or 0

    res = {
        "取数时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "指数": idx,
        "沪深上涨家数": up, "沪深下跌家数": down, "沪深平盘家数": flat,
        "两市成交额(亿)": round(total_amt / 1e8, 1),
        "赚钱效应": f"{up}/{up + down + flat}"
                    + (f" ({up / (up + down + flat) * 100:.0f}%)" if (up + down + flat) else ""),
    }

    if with_dt:
        # 涨停数复用涨停池。上限 200 足够（历史极值约 180 只），
        # 传 300 会多翻页、更容易触发限频退避。
        zt = [r for r in limit_up_pool(200) if r["板制"] != "新股"]
        res["涨停数"] = len(zt)
        res["涨停行业分布"] = _top_industries(zt, 6)
        res["跌停数"] = _count_limit_down()
    return res


def _top_industries(rows: list[dict], n: int) -> dict:
    """涨停股的行业聚集度。主线强度的直接读数。"""
    c: dict[str, int] = {}
    for r in rows:
        k = r.get("行业") or "未分类"
        c[k] = c.get(k, 0) + 1
    return dict(sorted(c.items(), key=lambda kv: -kv[1])[:n])


def _count_limit_down() -> int:
    """跌停家数。按涨幅升序取，遇到跌幅小于 -9.7% 的页就停。"""
    n = 0
    for page in range(1, 4):
        d = _em_get("/api/qt/clist/get", {
            "pn": page, "pz": 100, "po": 0, "np": 1, "fltt": 2, "invt": 2, "fid": "f3",
            "fs": FS_ALL_A, "fields": "f3,f12,f14",
        })
        rows = d.get("diff") or []
        if not rows:
            break
        page_min = min((x["f3"] for x in rows if isinstance(x.get("f3"), (int, float))),
                       default=0)
        for x in rows:
            p = x.get("f3")
            if not isinstance(p, (int, float)):
                continue
            cap = _price_cap(x.get("f12", ""), x.get("f14", ""))
            if cap != 999 and p <= -(cap - 0.3):
                n += 1
        if page_min > -9.7:
            break
    return n


# ---------- 5. 分时 / 日线 ----------

def minutes(code: str, days: int = 1) -> list[dict]:
    """当日分时（1 分钟）。days=1 只取今天。"""
    d = _em_get("/api/qt/stock/trends2/get", {
        "secid": _secid(code), "fields1": "f1,f2,f3,f4,f5",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58", "iscr": 0, "ndays": days,
    })
    out = []
    for line in d.get("trends", []):
        p = line.split(",")
        out.append({"时间": p[0], "开": p[1], "收": p[2], "高": p[3],
                    "低": p[4], "量": p[5], "额": p[6], "均价": p[7]})
    return out


def daily(code: str, n: int = 30, adjust: str = "qfq") -> list[dict]:
    """
    日线。adjust: qfq 前复权 / hfq 后复权 / '' 不复权。

    走腾讯，不走东财：push2delay 域名只做实时推送，K 线请求返回 klines: []，
    而东财历史专用域名 push2his 在本机网络下连不通（实测 2026-08-04）。
    腾讯这个口最后一根就是当日实时价，盘中可直接用。
    """
    prefix = "sh" if code.startswith(("60", "68", "51", "58", "11")) else "sz"
    sym = f"{prefix}{code}"
    sess = _shared_session()
    # 主源：腾讯
    try:
        r = sess.get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                     params={"param": f"{sym},day,,,{n},{adjust}"}, headers=UA, timeout=10)
        blk = r.json()["data"][sym]
        rows = blk.get(f"{adjust}day") or blk.get("day") or []
        out = []
        for p in rows:
            prev = out[-1]["收"] if out else None
            close = float(p[2])
            out.append({
                "日期": p[0], "开": float(p[1]), "收": close,
                "高": float(p[3]), "低": float(p[4]), "量(手)": int(float(p[5])),
                "涨跌幅": round((close - prev) / prev * 100, 2) if prev else None,
            })
        if out:
            return out
    except Exception:
        pass
    # 备份源：新浪
    try:
        r = sess.get("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php"
                     "/CN_MarketData.getKLineData",
                     params={"symbol": sym, "scale": 240, "ma": 5, "datalen": n},
                     headers=UA, timeout=10)
        out = []
        for x in r.json():
            prev = out[-1]["收"] if out else None
            close = float(x["close"])
            out.append({
                "日期": x["day"], "开": float(x["open"]), "收": close,
                "高": float(x["high"]), "低": float(x["low"]),
                "量(手)": int(x["volume"]) // 100,
                "涨跌幅": round((close - prev) / prev * 100, 2) if prev else None,
                "MA5": x.get("ma_price5"),
            })
        if out:
            return out
    except Exception:
        pass
    raise RuntimeError(f"{code}: 腾讯/新浪日线均取数失败")


# ---------- CLI ----------

def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd, args = argv[1], argv[2:]
    try:
        if cmd == "quote":
            if not args:
                print("用法: quote <代码> [代码...]")
                return 1
            _print([quote(c) for c in args])
        elif cmd == "zt":
            r = limit_up_pool()
            print(f"# 实时涨停 {len(r)} 只  取数时间 {datetime.now():%H:%M:%S}")
            _print(r)
        elif cmd == "strong":
            _print(strong_pool())
        elif cmd == "sector":
            kind = args[0] if args else "industry"
            _print(sector_flow(kind))
        elif cmd == "market":
            _print(market_temp())
        elif cmd == "min":
            _print(minutes(args[0])[-20:])
        elif cmd == "daily":
            _print(daily(args[0], int(args[1]) if len(args) > 1 else 30))
        elif cmd == "all":
            if not args:
                print("用法: all <代码>")
                return 1
            _print({
                "大盘": market_temp(),
                "个股": quote(args[0]),
                "板块资金流TOP10": sector_flow("industry", 10),
                "涨停池": limit_up_pool(40),
            })
        else:
            print(f"未知命令 {cmd}")
            print(__doc__)
            return 1
    except Exception as e:  # noqa: BLE001 - CLI 边界，明确报错不返回假数据
        print(f"取数失败: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
