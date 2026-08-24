#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RPS 三线红池数 —— 手工登记流水 + 读取接口（南京路彼岸 7 指标的第 7 项）

为什么必须手工：三线红要用全市场每只票 50/120/250 日涨幅的百分位排名（RPS）。
东财 clist 批量接口实测只有 f24=60日涨跌幅、f25=年初至今，**没有 120/250 日字段**；
同花顺/腾讯/新浪/开盘啦也都不给 RPS。程序要自己算就得把 5889 只票各 250 根日线
全落到本机，那是离线工程，不该塞进盘中工具。所以走「通达信选股 + 手工填一个数」。

盘后三步：
  1. 通达信跑「三线红」选股，看结果条数（配置步骤见 RPS数据获取配置.md）
  2. 填数：
       python rps_log.py add 112
       python rps_log.py add 112 --yixian 340 --bk 18 --note "缩容第2天"
       python rps_log.py add 112 --date 2026-08-04      # 补历史某天
  3. 让 AI 调 get_sentiment_7indicators，第 7 项就有值了

查看：
  python rps_log.py list              最近 20 条
  python rps_log.py list --n 60
  python rps_log.py show 2026-08-05

流水文件（UTF-8 BOM，可直接用 Excel 打开手改）：
  _知识库系统\\data\\rps_pool.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import re
from datetime import datetime

# ---------------- 路径与表头 ----------------

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
CSV_PATH = os.path.join(DATA_DIR, "rps_pool.csv")

FIELDS = ["date", "三线红池数", "一线红池数", "板块三线红池数", "阈值口径", "备注", "登记时间"]

# 作者实操的一套阈值，换阈值池子绝对数会变，所以每行都记下当时用的是哪套
DEFAULT_THRESHOLD = "120日>93 250日>95 50日>90 + H/HHV(HIGH,N)>0.85"

INT_FIELDS = ("三线红池数", "一线红池数", "板块三线红池数")


def norm_date(s: str | None) -> str:
    """把 20260805 / 2026-08-05 / 2026/8/5 统一成 2026-08-05；空则取今天"""
    if not s:
        return datetime.now().strftime("%Y-%m-%d")
    s = str(s).strip()
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    raise ValueError(f"日期格式看不懂：{s}（要 2026-08-05 或 20260805）")


# ---------------- 读 ----------------

def load_all() -> list[dict]:
    """读全部流水，按日期升序。文件不存在返回空表，不报错。"""
    if not os.path.isfile(CSV_PATH):
        return []
    with open(CSV_PATH, "r", encoding="utf-8-sig", newline="") as f:
        rows = []
        for raw in csv.DictReader(f):
            if not (raw.get("date") or "").strip():
                continue
            r = {k: (raw.get(k) or "").strip() for k in FIELDS}
            for k in INT_FIELDS:
                # 空字符串保持 None，别变成 0 —— 0 只和"没填"是两件事
                try:
                    r[k] = int(r[k]) if r[k] not in ("", "-") else None
                except ValueError:
                    r[k] = None
            rows.append(r)
    rows.sort(key=lambda x: x["date"])
    return rows


def get(date: str | None = None) -> dict | None:
    """取某天的登记；date 为空取最新一条。没有返回 None。"""
    rows = load_all()
    if not rows:
        return None
    if date is None:
        return rows[-1]
    d = norm_date(date)
    for r in rows:
        if r["date"] == d:
            return r
    return None


def window(date: str | None, n: int) -> list[int]:
    """截至 date（含）往前取最近 n 个有三线红池数的值，升序返回。给分位数用。"""
    rows = load_all()
    if date:
        d = norm_date(date)
        rows = [r for r in rows if r["date"] <= d]
    vals = [r["三线红池数"] for r in rows if r["三线红池数"] is not None]
    return vals[-n:] if n > 0 else vals


# ---------------- 写 ----------------

def add(pool: int, date: str | None = None, yixian: int | None = None,
        bk: int | None = None, threshold: str | None = None,
        note: str = "", force: bool = False) -> tuple[dict, str]:
    """
    追加或覆盖一天的登记。返回 (写入的行, 动作)。
    同一天已存在时，force=False 就报错让用户确认，避免手滑改掉旧数据。
    """
    d = norm_date(date)
    if pool is None or int(pool) < 0:
        raise ValueError("三线红池数必须是 >=0 的整数")

    rows = load_all()
    old = next((r for r in rows if r["date"] == d), None)
    if old and not force:
        raise SystemExit(
            f"{d} 已登记：三线红={old['三线红池数']}（登记于 {old['登记时间']}）。\n"
            f"要改就加 --force：python rps_log.py add {pool} --date {d} --force")

    row = {
        "date": d,
        "三线红池数": int(pool),
        "一线红池数": int(yixian) if yixian is not None else (old or {}).get("一线红池数"),
        "板块三线红池数": int(bk) if bk is not None else (old or {}).get("板块三线红池数"),
        "阈值口径": threshold or (old or {}).get("阈值口径") or DEFAULT_THRESHOLD,
        "备注": note or (old or {}).get("备注") or "",
        "登记时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    action = "覆盖" if old else "新增"
    rows = [r for r in rows if r["date"] != d] + [row]
    rows.sort(key=lambda x: x["date"])
    save_all(rows)
    return row, action


def save_all(rows: list[dict]) -> None:
    """整表重写。加 BOM 让 Excel 双击打开不乱码。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CSV_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in FIELDS})
    os.replace(tmp, CSV_PATH)  # 原子替换，中途断电不会留下半个文件


# ---------------- 命令行 ----------------

def _fmt(r: dict) -> str:
    def s(k, unit="只"):
        v = r.get(k)
        return f"{v}{unit}" if v is not None else "—"
    line = f"{r['date']}  三线红 {s('三线红池数'):>7}"
    if r.get("一线红池数") is not None:
        line += f"  一线红 {s('一线红池数')}"
    if r.get("板块三线红池数") is not None:
        line += f"  板块三线红 {s('板块三线红池数')}"
    if r.get("备注"):
        line += f"  # {r['备注']}"
    return line


def main() -> None:
    ap = argparse.ArgumentParser(
        description="RPS 三线红池数手工登记（南京路彼岸 7 指标第 7 项）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="登记一天的池数")
    a.add_argument("pool", type=int, help="三线红池数（通达信选股结果条数）")
    a.add_argument("--date", help="日期，默认今天。支持 2026-08-05 或 20260805")
    a.add_argument("--yixian", type=int, help="一线红池数（可选）")
    a.add_argument("--bk", type=int, help="板块三线红池数（可选）")
    a.add_argument("--threshold", help="本次用的阈值口径，默认沿用上次/作者实操那套")
    a.add_argument("--note", default="", help="备注，比如「缩容第2天」")
    a.add_argument("--force", action="store_true", help="同一天已登记时覆盖")

    l = sub.add_parser("list", help="看最近的登记")
    l.add_argument("--n", type=int, default=20)

    s = sub.add_parser("show", help="看某一天")
    s.add_argument("date")

    args = ap.parse_args()

    if args.cmd == "add":
        row, action = add(args.pool, args.date, args.yixian, args.bk,
                          args.threshold, args.note, args.force)
        print(f"[{action}] {_fmt(row)}")
        print(f"阈值口径：{row['阈值口径']}")
        vals = window(row["date"], 10)
        if len(vals) >= 3:
            below = sum(1 for x in vals if x < row["三线红池数"])
            eq = sum(1 for x in vals if x == row["三线红池数"])
            pct = round((below + eq / 2.0) / len(vals) * 100, 1)
            zone = "高位区" if pct >= 80 else ("低位区" if pct <= 20 else "中性")
            print(f"近{len(vals)}日分位：{pct}（{zone}）  窗口值：{vals}")
        else:
            print(f"已登记 {len(vals)} 天，攒到 3 天以上才给分位数（这指标只看相对高低）")
        print(f"文件：{CSV_PATH}")

    elif args.cmd == "list":
        rows = load_all()
        if not rows:
            print(f"还没有任何登记。文件：{CSV_PATH}")
            return
        for r in rows[-args.n:]:
            print(_fmt(r))
        print(f"\n共 {len(rows)} 天。文件：{CSV_PATH}")

    elif args.cmd == "show":
        r = get(args.date)
        if not r:
            print(f"{norm_date(args.date)} 没有登记")
            return
        for k in FIELDS:
            v = r.get(k)
            print(f"{k:>14}: {'' if v is None else v}")


if __name__ == "__main__":
    main()
