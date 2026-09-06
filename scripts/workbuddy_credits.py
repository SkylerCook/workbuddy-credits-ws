#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
workbuddy_credits.py —— WorkBuddy 积分查询 / 签到 / 快照 / 分析工具

原理：读取本机 WorkBuddy 登录态文件（含 accessToken / uid），直接调用官方接口。
安全：仅读取本机登录态，不存储、不打印 accessToken 明文；零第三方依赖（仅标准库）。

用法（子命令）：
  python workbuddy_credits.py                 # 默认：人类可读汇总 + 批次明细
  python workbuddy_credits.py status          # 每日签到状态（连续天数/今日是否已签）
  python workbuddy_credits.py checkin         # 自动签到（幂等，已签则跳过）
  python workbuddy_credits.py record          # 记录当前余额快照到本地
  python workbuddy_credits.py analyze         # 分析：过期浪费 / 消耗趋势 / 签到效率
  python workbuddy_credits.py render          # 生成工作台数据 dashboard_data.js
  python workbuddy_credits.py --json          # 余额原始 JSON
  python workbuddy_credits.py --token         # 登录态摘要（token 脱敏）
  python workbuddy_credits.py --export [路径]  # 导出按到期时间排序的 Markdown 列表

实测接口（2026-09-06 验证 HTTP 200）：
  积分资源包  POST https://copilot.tencent.com/billing/meter/get-user-resource
  签到状态    POST https://www.codebuddy.cn/v2/billing/meter/checkin-activity-status
  执行签到    POST https://www.codebuddy.cn/v2/billing/meter/daily-checkin
  必带头：Authorization: Bearer <token>、X-User-Id: <uid>、
          User-Agent: Mozilla/5.0（缺此头返回 403 code=10085）
  注意：积分接口用 copilot.tencent.com 且无 /v2/ 前缀；签到接口用 codebuddy.cn 且有 /v2/ 前缀。
"""

import json
import os
import sys
import time
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime, timedelta

# ---------- 接口配置 ----------
API_BILLING_BASE = "https://copilot.tencent.com/billing/meter"
API_GET_RESOURCE = "/get-user-resource"

API_CHECKIN_BASE = "https://www.codebuddy.cn/v2/billing/meter"
API_CHECKIN_STATUS = "/checkin-activity-status"
API_DAILY_CHECKIN = "/daily-checkin"

# CapacityType -> 分类
#   4 = 个人体验版（套餐基础用量）；1 = 权益赠送包（运营裂变包）；其余 = 加量包/其他
TYPE_TRIAL = 4
TYPE_GIFT = 1
TYPE_LABEL = {TYPE_TRIAL: "套餐用量", TYPE_GIFT: "权益赠送包"}

# 快照数据目录（独立于 skill 目录，避免 skill 更新/删除影响历史数据）
DATA_DIR = os.path.join(os.path.expanduser("~"), ".workbuddy", "workbuddy-credits-data")
SNAPSHOT_FILE = os.path.join(DATA_DIR, "snapshots.jsonl")
CHECKIN_HISTORY_FILE = os.path.join(DATA_DIR, "checkin_history.json")
INCOME_EVENTS_FILE = os.path.join(DATA_DIR, "income_events.json")
USAGE_HISTORY_FILE = os.path.join(DATA_DIR, "usage_history.json")

# skill 根目录（脚本位于 <skill>/scripts/ 下）
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD_DATA_FILE = os.path.join(SKILL_DIR, "dashboard_data.js")


# ---------- 登录态定位 ----------
def auth_candidates():
    home = os.path.expanduser("~")
    cands = []
    if sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support",
                            "CodeBuddyExtension", "Data", "Public", "auth")
        cands.append(os.path.join(base, "workbuddy-desktop.info"))
        cands.append(os.path.join(base, "Tencent-Cloud.coding-copilot.info"))
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        cands.append(os.path.join(local, "CodeBuddyExtension", "Data",
                                  "Public", "auth", "workbuddy-desktop.info"))
    else:
        cands.append(os.path.join(home, ".config", "CodeBuddyExtension",
                                  "Data", "Public", "auth", "workbuddy-desktop.info"))
        cands.append(os.path.join(home, ".workbuddy", "auth", "workbuddy-desktop.info"))
    return cands


def load_login():
    for p in auth_candidates():
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            token = (d.get("auth") or {}).get("accessToken", "")
            uid = (d.get("account") or {}).get("uid", "")
            if not token or not uid:
                continue
            return token, uid, d.get("account") or {}, p
        except Exception:
            continue
    return None


# ---------- 通用 API 调用 ----------
def _api_call(base, path, token, uid, body=None):
    url = base + path
    data = json.dumps(body).encode("utf-8") if body is not None else b"{}"
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("X-User-Id", uid)
    req.add_header("User-Agent", "Mozilla/5.0")  # 关键：缺此头返回 403
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, str(e)
    try:
        data = json.loads(raw)
    except Exception:
        return None, "非 JSON 响应: " + raw[:200]
    if data.get("code") != 0:
        return None, "code=%s msg=%s" % (data.get("code"), data.get("msg"))
    return data, None


def api_get_resource(token, uid):
    data, err = _api_call(API_BILLING_BASE, API_GET_RESOURCE, token, uid, {})
    if err:
        return None, err
    try:
        accounts = data["data"]["Response"]["Data"]["Accounts"]
    except Exception:
        return None, "响应结构异常"
    return accounts, None


def api_checkin_status(token, uid):
    data, err = _api_call(API_CHECKIN_BASE, API_CHECKIN_STATUS, token, uid, {})
    if err:
        return None, err
    return data.get("data") or {}, None


def api_daily_checkin(token, uid):
    data, err = _api_call(API_CHECKIN_BASE, API_DAILY_CHECKIN, token, uid, {})
    if err:
        return None, err
    return data.get("data") or {}, None


# ---------- 解析 ----------
def _num(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def classify(acc):
    ct = int(acc.get("CapacityType", -1))
    if ct in TYPE_LABEL:
        return ct, TYPE_LABEL[ct]
    name = acc.get("PackageName", "")
    return -2, ("加量包" if ("加量" in name or "叠加" in name) else "加量包/其他")


def _parse_expire(cycle_end, deduction_end):
    if cycle_end:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(cycle_end, fmt).timestamp() * 1000
            except Exception:
                continue
    if deduction_end:
        try:
            return int(deduction_end)
        except Exception:
            pass
    return None


def parse_accounts(accounts):
    now_ts = time.time() * 1000
    packages = []
    for a in accounts:
        remain = _num(a.get("CapacityRemainPrecise") or a.get("CapacityRemain"))
        size = _num(a.get("CapacitySizePrecise") or a.get("CapacitySize"))
        used = _num(a.get("CapacityUsedPrecise") or a.get("CapacityUsed"))
        cycle_end = a.get("CycleEndTime", "")
        exp_ts = _parse_expire(cycle_end, a.get("DeductionEndTime"))
        expired = exp_ts is not None and exp_ts < now_ts
        key, label = classify(a)
        packages.append({
            "key": key, "label": label, "name": a.get("PackageName", ""),
            "remain": remain, "size": size, "used": used,
            "cycle_end": cycle_end, "cycle_start": a.get("CycleStartTime", ""),
            "resource_id": a.get("ResourceId", ""),
            "exp_ts": exp_ts,
            "expired": expired, "available": remain > 0 and not expired,
        })
    return packages


def summarize(packages):
    groups = {}
    for p in packages:
        groups.setdefault(p["key"], []).append(p)
    result = []
    for key in (TYPE_TRIAL, TYPE_GIFT, -2):
        if key not in groups:
            continue
        items = groups[key]
        avail = [p for p in items if p["available"]]
        result.append({
            "key": key, "label": items[0]["label"],
            "total_count": len(items), "avail_count": len(avail),
            "remain_sum": sum(p["remain"] for p in avail),
            "size_sum": sum(p["size"] for p in items),
            "items": sorted(items, key=lambda x: x["cycle_end"] or ""),
        })
    return result


# ---------- 快照 ----------
def record_snapshot(packages, checkin=None):
    os.makedirs(DATA_DIR, exist_ok=True)
    now = datetime.now()
    avail = [p for p in packages if p["available"]]
    snap = {
        "ts": int(time.time() * 1000),
        "time": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "total_remain": round(sum(p["remain"] for p in avail), 4),
        "total_used": round(sum(p["used"] for p in packages), 4),
        "avail_count": len(avail),
        "checkin": checkin,
        "packages": [
            {"label": p["label"], "remain": p["remain"], "cycle_end": p["cycle_end"],
             "available": p["available"], "expired": p["expired"],
             "resource_id": p.get("resource_id", ""), "used": p["used"]}
            for p in packages
        ],
    }
    with open(SNAPSHOT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")
    return snap


def load_snapshots():
    if not os.path.exists(SNAPSHOT_FILE):
        return []
    snaps = []
    with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                snaps.append(json.loads(line))
            except Exception:
                continue
    return snaps


# ---------- 本地消耗明细（workbuddy.db / session_usage） ----------
def _usage_db_path():
    return os.path.join(os.path.expanduser("~"), ".workbuddy", "workbuddy.db")


def load_usage():
    """读取本地 session_usage 表，返回会话级消耗明细列表。

    每项：{session_id, title, model, updated_at(ms), credit(该会话消耗积分), n_calls}
    覆盖最近约 17 天（本地库仅保留近期会话）。返回空列表表示无数据或库不可读。
    """
    db = _usage_db_path()
    if not os.path.exists(db):
        return []
    try:
        con = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute(
            "SELECT su.session_id, su.updated_at, su.credit_json, "
            "s.title, s.model "
            "FROM session_usage su "
            "LEFT JOIN sessions s ON s.id = su.session_id "
            "ORDER BY su.updated_at ASC"
        )
        rows = cur.fetchall()
        con.close()
    except Exception:
        return []

    usage = []
    for sid, upd, cj, title, model in rows:
        credit = 0.0
        n_calls = 0
        if cj:
            try:
                j = json.loads(cj)
                credit = sum(float(v) for v in j.values())
                n_calls = len(j)
            except Exception:
                pass
        usage.append({
            "session_id": sid,
            "title": title or "",
            "model": model or "",
            "updated_at": upd,
            "credit": round(credit, 4),
            "n_calls": n_calls,
        })
    return usage


def usage_daily(usage):
    """按天聚合消耗（按会话最后更新时间归日），缺失日期补 0，返回日期连续的 [{date, credit}]。"""
    daily = {}
    for u in usage:
        if not u["updated_at"]:
            continue
        d = datetime.fromtimestamp(u["updated_at"] / 1000).strftime("%Y-%m-%d")
        daily[d] = daily.get(d, 0.0) + u["credit"]
    if not daily:
        return []
    dates = sorted(daily.keys())
    start = datetime.strptime(dates[0], "%Y-%m-%d")
    end = datetime.strptime(dates[-1], "%Y-%m-%d")
    result = []
    cur = start
    while cur <= end:
        key = cur.strftime("%Y-%m-%d")
        result.append({"date": key, "credit": round(daily.get(key, 0.0), 4)})
        cur += timedelta(days=1)
    return result


def usage_heatmap(usage):
    """按 (日期, 小时) 聚合消耗（会话最后更新时间归小时），返回 [{date, hour, credit}]。"""
    cells = {}
    for u in usage:
        if not u["updated_at"]:
            continue
        dt = datetime.fromtimestamp(u["updated_at"] / 1000)
        date = dt.strftime("%Y-%m-%d")
        hour = dt.hour
        cells[(date, hour)] = cells.get((date, hour), 0.0) + u["credit"]
    return [{"date": d, "hour": h, "credit": round(cells[(d, h)], 4)}
            for d, h in sorted(cells)]


# ---------- 自积累账本 ----------
def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def _save_json(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def accumulate_checkin(checkin_dates):
    """把当前活动周期的签到日期合并进本地历史，返回累计签到日期（升序）。"""
    history = _load_json(CHECKIN_HISTORY_FILE, {"dates": []})
    dates = set(history.get("dates", []))
    dates.update(checkin_dates or [])
    history["dates"] = sorted(dates)
    _save_json(CHECKIN_HISTORY_FILE, history)
    return history["dates"]


def accumulate_income(packages):
    """对比 ResourceId 记录新包到账事件，返回全部到账事件。"""
    events = _load_json(INCOME_EVENTS_FILE, {"events": []})
    known = set(e.get("resource_id") for e in events["events"])
    changed = False
    for p in packages:
        rid = p.get("resource_id")
        if not rid or rid in known:
            continue
        events["events"].append({
            "resource_id": rid,
            "name": p["name"],
            "label": p["label"],
            "size": p["size"],
            "income_time": p.get("cycle_start") or "",
        })
        known.add(rid)
        changed = True
    if changed:
        _save_json(INCOME_EVENTS_FILE, events)
    return events["events"]


def accumulate_usage(packages):
    """逐分钟记录 used/remain 到 usage_history.json，自积累突破 17 天窗口。

    结构：{"packages": {rid: {"name", "size", "daily": {<YYYY-MM-DD HH:MM>: {"used", "remain"}}}}}
    返回历史 dict。

    粒度说明：快照 key 精确到分钟。自动化任务每小时跑一次 record 作为兜底（保证每小时至少一条），
    用户手动刷新也写实时快照；分钟级 key 使两者互不覆盖（向后覆盖），不再有"同小时向前覆盖"的问题。
    """
    hist = _load_json(USAGE_HISTORY_FILE, {"packages": {}})
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M")
    pkgs = hist.setdefault("packages", {})
    for p in packages:
        rid = p.get("resource_id") or ""
        if not rid:
            continue
        entry = pkgs.setdefault(rid, {"name": p["name"], "size": p["size"], "daily": {}})
        daily = entry["daily"]
        # 迁移旧 key：天级（长度 10）→ 当天 00:00；小时级（长度 16，HH:00）→ 已是分钟级格式无需动
        for k in list(daily.keys()):
            if len(k) == 10:
                daily[k + " 00:00"] = daily.pop(k)
        entry["name"] = p["name"]
        entry["size"] = p["size"]
        daily[stamp] = {"used": round(p["used"], 4), "remain": round(p["remain"], 4)}
    _save_json(USAGE_HISTORY_FILE, hist)
    return hist


def compute_usage_daily(hist):
    """从逐分钟 used 历史计算每日消耗（逐包差值，免疫签到/裂变/过期）。

    历史天 = Σ(各包 used 跨天增量)；今天 = Σ(各包 used 日内增量，last - first)。
    返回日期连续的 [{date, credit}]。
    """
    daily = {}
    today = datetime.now().strftime("%Y-%m-%d")
    for rid, entry in hist.get("packages", {}).items():
        stamps = sorted(entry.get("daily", {}).keys())
        if not stamps:
            continue
        by_date = {}
        for s in stamps:
            by_date[s[:10]] = entry["daily"][s].get("used", 0.0)
        days = sorted(by_date.keys())
        prev_used = None
        for d in days:
            used = by_date[d]
            if prev_used is not None and d < today:  # 历史天：跨天差值
                delta = used - prev_used
                if delta > 1e-9:
                    daily[d] = daily.get(d, 0.0) + delta
            prev_used = used
        # 今天：日内差值（今日最后 used - 今日最早 used）
        if today in by_date:
            ts = [s for s in stamps if s.startswith(today)]
            first = entry["daily"][ts[0]].get("used", 0.0)
            last = entry["daily"][ts[-1]].get("used", 0.0)
            dlt = last - first
            if dlt > 1e-9:
                daily[today] = daily.get(today, 0.0) + dlt
    if not daily:
        return []
    dates = sorted(daily.keys())
    start = datetime.strptime(dates[0], "%Y-%m-%d")
    end = datetime.strptime(dates[-1], "%Y-%m-%d")
    result = []
    cur = start
    while cur <= end:
        key = cur.strftime("%Y-%m-%d")
        result.append({"date": key, "credit": round(daily.get(key, 0.0), 4)})
        cur += timedelta(days=1)
    return result


def _today_delta_by_pkg(hist):
    """今日各包 used 的日内增量之和（last - first），逐包差值。

    免疫签到/裂变（新包 used 从 0 起）与包过期（过期包停止更新，日内差值 0）。
    返回浮点。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    delta = 0.0
    for rid, entry in hist.get("packages", {}).items():
        stamps = sorted(s for s in entry.get("daily", {}).keys() if s.startswith(today))
        if not stamps:
            continue
        first = entry["daily"][stamps[0]].get("used", 0.0)
        last = entry["daily"][stamps[-1]].get("used", 0.0)
        d = last - first
        if d > 1e-9:
            delta += d
    return round(delta, 4)


def compute_today_used(hist):
    """今日实时消耗 = Σ(各包 今日最新 used - 今日最早 used)，逐包差值。

    免疫签到/裂变（新包 used 从 0 起）与包过期（过期包停止更新，差值 0）。
    返回浮点（首日为 0）。
    """
    return _today_delta_by_pkg(hist)


def compute_usage_hourly(hist):
    """从逐小时 used 历史计算每小时消耗（日期×小时），供热力图使用。

    返回 [{date, hour, credit}]。
    """
    cells = {}
    for rid, entry in hist.get("packages", {}).items():
        stamps = sorted(entry.get("daily", {}).keys())
        prev_used = None
        for s in stamps:
            used = entry["daily"][s].get("used", 0.0)
            if prev_used is not None:
                delta = used - prev_used
                if delta > 1e-9:
                    d = s[:10]
                    h = int(s[11:13])
                    cells[(d, h)] = cells.get((d, h), 0.0) + delta
            prev_used = used
    return [{"date": d, "hour": h, "credit": round(cells[(d, h)], 4)}
            for d, h in sorted(cells)]


def build_ledger(daily_expense, packages, checkin_dates, daily_credit):
    """构建日粒度收支账本：收入=签到+包到账，支出=消耗。

    参数：
      daily_expense: {date: credit} 每日消耗，建议用逐包追踪口径（compute_usage_daily 结果）
      packages: 当前资源包列表，用于按 cycle_start 归日的包到账收入
      checkin_dates / daily_credit: 签到日与每日奖励积分

    说明：
      - 签到收入仅能覆盖本地累计到的日期。
      - 包到账用资源包 CycleStartTime 归日。
      - 关键去重：WorkBuddy 的签到奖励（daily_credit）本身会以「size==daily_credit 的裂变包」形式
        发放到账，其 cycle_start 即签到时刻。若再计入包到账会与签到列重复（同一笔 +100 算两次）。
        故包到账排除「size==daily_credit 且 cycle_start 日期在签到日期内」的签到奖励包。
    """
    daily_credit_val = float(daily_credit or 0)
    sign_dates = set(checkin_dates or [])
    daily_sign = {}
    for d in (checkin_dates or []):
        daily_sign[d] = daily_sign.get(d, 0.0) + daily_credit_val
    daily_pkg = {}
    for p in packages:
        cs = p.get("cycle_start") or ""
        if len(cs) < 10:
            continue
        d = cs[:10]
        # 签到奖励包去重：size==daily_credit 且 cycle_start 日期在签到日期内 → 视为签到，不计包到账
        if daily_credit_val > 0 and d in sign_dates and abs(p["size"] - daily_credit_val) < 1e-6:
            continue
        daily_pkg[d] = daily_pkg.get(d, 0.0) + p["size"]
    all_dates = sorted(set(list(daily_sign.keys()) + list(daily_pkg.keys()) + list(daily_expense.keys())))
    ledger = []
    for d in all_dates:
        sign = round(daily_sign.get(d, 0.0), 4)
        pkg = round(daily_pkg.get(d, 0.0), 4)
        inc = round(sign + pkg, 4)
        exp = round(daily_expense.get(d, 0.0), 4)
        ledger.append({"date": d, "sign_in": sign, "package_in": pkg,
                       "income": inc, "expense": exp, "net": round(inc - exp, 4)})
    return ledger


# ---------- 分析 ----------
def compute_waste(packages, now_ts=None, soon_days=7):
    """过期浪费统计：已过期但剩余>0 的积分（浪费）+ 即将到期（≤N天）的剩余积分（风险）。"""
    now_ts = now_ts or (time.time() * 1000)
    soon_limit = now_ts + soon_days * 86400 * 1000
    wasted = 0.0          # 已过期但未用完
    wasted_count = 0
    at_risk = 0.0         # 即将到期（≤N天）
    at_risk_count = 0
    for p in packages:
        if p["remain"] <= 0:
            continue
        if p["exp_ts"] is None:
            continue
        if p["exp_ts"] < now_ts:  # 已过期
            wasted += p["remain"]
            wasted_count += 1
        elif p["exp_ts"] <= soon_limit:  # 即将到期
            at_risk += p["remain"]
            at_risk_count += 1
    return {"wasted": round(wasted, 4), "wasted_count": wasted_count,
            "at_risk": round(at_risk, 4), "at_risk_count": at_risk_count,
            "soon_days": soon_days}


def build_timeline(snapshots):
    """从快照构建余额时间线与日净变化（净变化正=消耗，负=到账）。"""
    if not snapshots:
        return [], []
    # 按天取每天最后一个快照的余额
    daily = {}
    for s in snapshots:
        daily[s.get("date", s.get("time", "")[:10])] = s.get("total_remain", 0)
    dates = sorted(daily.keys())
    balance = [{"date": d, "remain": daily[d]} for d in dates]
    net_change = []
    for i in range(1, len(dates)):
        prev = daily[dates[i - 1]]
        cur = daily[dates[i]]
        net_change.append({"date": dates[i], "delta": round(prev - cur, 4)})
    return balance, net_change


# ---------- 输出 ----------
def mask(s, n=8):
    return (s[:n] + "...(共%d字符)" % len(s)) if s else "(空)"


def render_human(groups):
    lines = ["=" * 62, "WorkBuddy 积分余额", "=" * 62]
    for g in groups:
        lines.append("")
        lines.append("【%s】  可用 %d 个 · 剩余 %.4f 积分"
                     % (g["label"], g["avail_count"], g["remain_sum"]))
        for p in g["items"]:
            status = "可用" if p["available"] else ("已过期" if p["expired"] else "已用完")
            lines.append("  · %s  %.2f/%.2f（已用/总量） 剩余 %.4f  到期 %s  [%s]"
                         % (p["name"], p["used"], p["size"], p["remain"],
                            p["cycle_end"] or "-", status))
    lines += ["", "=" * 62]
    return "\n".join(lines)


def render_markdown_flat(packages, account, generated_at):
    ordered = sorted(packages, key=lambda x: (x["cycle_end"] == "", x["cycle_end"] or ""))
    avail = [p for p in packages if p["available"]]
    L = ["# WorkBuddy 积分列表", "",
         "> 生成时间：%s　·　账号：%s（%s）"
         % (generated_at, account.get("nickname", "-"), account.get("type", "-")), "",
         "## 汇总", "", "| 项目 | 数值 |", "|------|------|",
         "| 资源包总数 | %d 个 |" % len(packages),
         "| 可用包数 | %d 个 |" % len(avail),
         "| 可用剩余积分 | %.4f |" % sum(p["remain"] for p in avail), "",
         "## 完整列表（按到期时间升序）", "",
         "| # | 到期时间 | 状态 | 剩余积分 | 已用/总量 | 类型 | 资源包 |",
         "|---|----------|------|----------|-----------|------|--------|"]
    for i, p in enumerate(ordered, 1):
        status = "可用" if p["available"] else ("已过期" if p["expired"] else "已用完")
        L.append("| %d | %s | %s | %.4f | %.2f/%.2f | %s | %s |"
                 % (i, p["cycle_end"] or "-", status, p["remain"], p["used"],
                    p["size"], p["label"], p["name"]))
    L.append("")
    return "\n".join(L)


# ---------- 子命令 ----------
def cmd_status(token, uid):
    st, err = api_checkin_status(token, uid)
    if err:
        print("查询失败：", err)
        return 1
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0


def cmd_checkin(token, uid):
    st, err = api_checkin_status(token, uid)
    if err:
        print("查询签到状态失败：", err)
        return 1
    already = bool(st.get("today_checked_in"))
    if already:
        print("今日已签到，无需重复。连续 %s 天，今日 +%s 积分。"
              % (st.get("streak_days", "?"), st.get("daily_credit", "?")))
        return 0
    data, err = api_daily_checkin(token, uid)
    if err:
        print("签到失败：", err)
        return 1
    print("签到成功：本次 +%s 积分 | 连续 %s 天"
          % (data.get("credit", "?"), data.get("streak_days", "?")))
    return 0


def cmd_record(token, uid):
    accounts, err = api_get_resource(token, uid)
    if err:
        print("查询失败：", err)
        return 1
    packages = parse_accounts(accounts)
    st, _ = api_checkin_status(token, uid)  # 签到状态可选
    snap = record_snapshot(packages, st or None)
    accumulate_usage(packages)  # 顺带累积逐包 used 历史
    print("快照已记录：%s | 总可用剩余 %.4f | 可用包 %d 个 | 快照文件 %s"
          % (snap["time"], snap["total_remain"], snap["avail_count"], SNAPSHOT_FILE))
    return 0


def cmd_analyze(token, uid):
    accounts, err = api_get_resource(token, uid)
    if err:
        print("查询失败：", err)
        return 1
    packages = parse_accounts(accounts)
    waste = compute_waste(packages)
    snaps = load_snapshots()
    balance, net_change = build_timeline(snaps)
    st, _ = api_checkin_status(token, uid)
    st = st or {}

    L = ["=" * 62, "积分分析", "=" * 62]
    L.append("【过期浪费】已过期未用完 %d 批 / %.4f 积分；未来 %d 天内到期风险 %d 批 / %.4f 积分"
            % (waste["wasted_count"], waste["wasted"], waste["soon_days"],
               waste["at_risk_count"], waste["at_risk"]))
    L.append("【签到效率】每日可得 %s 积分 | 连续 %s 天 | 今日已签：%s | 活动：%s"
            % (st.get("daily_credit", "?"), st.get("streak_days", "?"),
               "是" if st.get("today_checked_in") else "否", st.get("activity_name", "-")))
    L.append("【快照统计】已积累 %d 条快照，覆盖 %d 天" % (len(snaps), len(balance)))
    if net_change:
        L.append("【近 7 天净变化（正=消耗，负=到账）】")
        for c in net_change[-7:]:
            L.append("  %s  %+.4f" % (c["date"], c["delta"]))
    L += ["", "=" * 62]
    print("\n".join(L))
    return 0


def cmd_expire_check(token, uid, hours=36):
    """检查未来 N 小时内到期的批次，供自动化巡检使用。"""
    accounts, err = api_get_resource(token, uid)
    if err:
        print("查询失败：", err)
        return 1
    packages = parse_accounts(accounts)
    now_ts = time.time() * 1000
    limit = now_ts + hours * 3600 * 1000
    expiring = [p for p in packages
                if p["remain"] > 0 and p["exp_ts"] is not None
                and now_ts <= p["exp_ts"] <= limit]
    if not expiring:
        print("未来 %d 小时内无到期批次" % hours)
        return 0
    expiring.sort(key=lambda x: x["cycle_end"] or "")
    print("未来 %d 小时内到期 %d 批：" % (hours, len(expiring)))
    for p in expiring:
        print("  · 到期 %s | 剩余 %.2f 积分 | %s"
              % (p["cycle_end"], p["remain"], p["label"]))
    return 0


def cmd_usage():
    """输出本地 session_usage 消耗明细。"""
    usage = load_usage()
    if not usage:
        print("无本地消耗明细（未找到 workbuddy.db 或暂无会话记录）。")
        return 0
    daily = usage_daily(usage)
    today = datetime.now().strftime("%Y-%m-%d")
    today_credit = sum(d["credit"] for d in daily if d["date"] == today)
    total_credit = round(sum(u["credit"] for u in usage), 4)

    L = ["=" * 62, "积分消耗明细（本地 session_usage）", "=" * 62]
    L.append("今日消耗：%.4f 积分" % today_credit)
    L.append("累计消耗（近 %d 天）：%.4f 积分" % (len(daily), total_credit))
    L.append("")
    L.append("【按天消耗】")
    for d in daily:
        L.append("  %s  %.4f" % (d["date"], d["credit"]))
    L.append("")
    L.append("【最近会话（按时间倒序，最多 10 条）】")
    for u in sorted(usage, key=lambda x: x["updated_at"] or 0, reverse=True)[:10]:
        t = datetime.fromtimestamp(u["updated_at"] / 1000).strftime("%m-%d %H:%M") if u["updated_at"] else "?"
        title = (u["title"] or "(无标题)")[:24]
        L.append("  %s  %-6s  %8.2f 分 | %s" % (t, u["model"] or "-", u["credit"], title))
    L += ["", "=" * 62]
    print("\n".join(L))
    return 0


def build_dashboard_data(token, uid, account):
    """构建工作台数据 dict。返回 (data, err)。供 render 命令与 serve.py 复用。"""
    accounts, err = api_get_resource(token, uid)
    if err:
        return None, err
    packages = parse_accounts(accounts)
    groups = summarize(packages)
    waste = compute_waste(packages)
    snaps = load_snapshots()
    balance, net_change = build_timeline(snaps)
    st, _ = api_checkin_status(token, uid)
    st = st or {}

    # 到期批次（含已过期，按到期时间排序）
    ordered = sorted(packages, key=lambda x: (x["cycle_end"] == "", x["cycle_end"] or ""))
    expiry_list = [
        {"label": p["label"], "name": p["name"], "remain": p["remain"],
         "size": p["size"], "used": p["used"], "cycle_end": p["cycle_end"],
         "status": "可用" if p["available"] else ("已过期" if p["expired"] else "已用完")}
        for p in ordered
    ]

    # 累计消耗与今日消耗
    total_used = round(sum(p["used"] for p in packages), 4)
    today_str = datetime.now().strftime("%Y-%m-%d")

    # 精确消耗（本地 session_usage 表）—— 用于「会话明细」「今日已使用」「日均预测」等总数/聚合口径
    usage = load_usage()
    usage_total = round(sum(u["credit"] for u in usage), 4)
    usage_daily_local = usage_daily(usage)
    usage_today = sum(d["credit"] for d in usage_daily_local if d["date"] == today_str)

    # 逐包追踪的每日消耗（准确口径，自积累突破 17 天窗口）—— 用于每日/累计/日历逐日图表
    usage_hist = accumulate_usage(packages)
    udaily = compute_usage_daily(usage_hist)
    uhourly = compute_usage_hourly(usage_hist)  # 逐小时消耗，供小时热力图

    # 会话级明细（按时间倒序）
    usage_sessions = [
        {
            "time": datetime.fromtimestamp(u["updated_at"] / 1000).strftime("%m-%d %H:%M") if u["updated_at"] else "",
            "date": datetime.fromtimestamp(u["updated_at"] / 1000).strftime("%Y-%m-%d") if u["updated_at"] else "",
            "title": u["title"] or "(无标题)",
            "model": u["model"] or "-",
            "credit": u["credit"],
            "n_calls": u["n_calls"],
        }
        for u in sorted(usage, key=lambda x: x["updated_at"] or 0, reverse=True)
    ]

    # 热力图数据（日期 × 小时）
    heatmap = usage_heatmap(usage)

    # ---- 自积累账本（签到合并 + 包到账对比 ResourceId）----
    checkin_dates_raw = st.get("checkin_dates", [])
    accumulated_checkin = accumulate_checkin(checkin_dates_raw)
    income_events = accumulate_income(packages)
    daily_credit_val = st.get("daily_credit", 0) or 0
    ledger = build_ledger({d["date"]: d["credit"] for d in udaily}, packages, accumulated_checkin, daily_credit_val)
    # 收入日数据（签到 + 包到账，按天），供收入日历热力图使用
    income_daily = [{"date": r["date"], "income": r["income"]} for r in ledger]

    # ---- 预测：近 7 天日均消耗 + 预计可用天数 ----
    # 预测用 session_usage 近期日均（聚合口径，跨天误差被平均，历史充分）；逐包口径首日无差值不可用
    recent7 = [d["credit"] for d in usage_daily_local[-7:]]
    daily_avg = round(sum(recent7) / len(recent7), 4) if recent7 else 0.0
    total_remain_val = round(sum(g["remain_sum"] for g in groups), 4)
    days_left = round(total_remain_val / daily_avg, 1) if daily_avg > 0 else None

    # ---- 建议（规则引擎）----
    advice = []
    if waste["at_risk_count"] > 0:
        advice.append("你有 %d 个批次将在 %d 天内到期（合计 %.2f 积分），建议优先消耗"
                      % (waste["at_risk_count"], waste["soon_days"], waste["at_risk"]))
    if days_left is not None:
        advice.append("按近 7 天日均消耗 %.2f 积分，剩余积分约可用 %.1f 天" % (daily_avg, days_left))
    advice.append("坚持每日签到（每日 +%s 积分），每月约可补 %d 积分"
                  % (daily_credit_val, int(float(daily_credit_val) * 30)))
    if waste["wasted"] > 0:
        advice.append("已有 %.2f 积分因过期浪费，建议及时使用临期积分" % waste["wasted"])

    # ---- 使用摘要（一段综合文字）----
    summary_parts = []
    if daily_avg > 0:
        summary_parts.append("近 7 天日均消耗 %.1f 积分" % daily_avg)
    if days_left is not None:
        summary_parts.append("按当前节奏，剩余积分约可用 %.1f 天" % days_left)
    if waste["at_risk_count"] > 0:
        summary_parts.append("%d 个批次 %d 天内到期（%.2f 积分）"
                             % (waste["at_risk_count"], waste["soon_days"], waste["at_risk"]))
    summary_parts.append("连续签到 %d 天，每日 +%s 积分"
                         % (st.get("streak_days", 0), daily_credit_val))
    summary = "，".join(summary_parts) + "。"

    # 今日已使用：逐包日内差值（免疫签到/裂变/过期），刷新即可响应
    today_used = compute_today_used(usage_hist)

    data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "nickname": account.get("nickname", "-"),
        "account_type": account.get("type", ""),
        "groups": [
            {"label": g["label"], "avail_count": g["avail_count"],
             "remain_sum": g["remain_sum"], "total_count": g["total_count"]}
            for g in groups
        ],
        "total_remain": round(sum(g["remain_sum"] for g in groups), 4),
        "total_used": total_used,
        "today_used": today_used,
        "usage_total": usage_total,
        "usage_daily": udaily,
        "income_daily": income_daily,
        "usage_sessions": usage_sessions,
        "usage_heatmap": heatmap,
        "usage_hourly": uhourly,
        "daily_avg": daily_avg,
        "days_left": days_left,
        "ledger": ledger,
        "advice": advice,
        "summary": summary,
        "checkin_accumulated_days": len(accumulated_checkin),
        "income_events_count": len(income_events),
        "avail_count": sum(g["avail_count"] for g in groups),
        "waste": waste,
        "checkin": {
            "today_checked_in": bool(st.get("today_checked_in")),
            "streak_days": st.get("streak_days", 0),
            "daily_credit": st.get("daily_credit", 0),
            "activity_name": st.get("activity_name", ""),
            "theme_name": st.get("theme_name", ""),
            "checkin_dates": st.get("checkin_dates", []),
            "week_progress": st.get("week_progress", []),
            "start_time": st.get("start_time", ""),
            "end_time": st.get("end_time", ""),
        },
        "balance_timeline": balance,
        "net_change": net_change,
        "expiry_list": expiry_list,
    }
    return data, None


def cmd_render(token, uid, account):
    data, err = build_dashboard_data(token, uid, account)
    if err:
        print("查询失败：", err)
        return 1
    n_packages = len(data["expiry_list"])
    n_snaps = len(load_snapshots())
    os.makedirs(SKILL_DIR, exist_ok=True)
    with open(DASHBOARD_DATA_FILE, "w", encoding="utf-8") as f:
        f.write("window.CREDITS_DATA = ")
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write(";\n")
    print("工作台数据已生成：%s（%d 个资源包，%d 条快照）"
          % (DASHBOARD_DATA_FILE, n_packages, n_snaps))

    # 生成内嵌数据版 HTML（数据写死，避免外部 data.js 同步问题；preview/分享用）
    inline_html_path = os.path.join(SKILL_DIR, "dashboard_inline.html")
    dashboard_html_path = os.path.join(SKILL_DIR, "dashboard.html")
    try:
        with open(DASHBOARD_DATA_FILE, "r", encoding="utf-8") as f:
            data_js = f.read()
        with open(dashboard_html_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        inline_html = html_content.replace(
            '<script src="dashboard_data.js"></script>',
            '<script>\n' + data_js + '\n</script>'
        )
        with open(inline_html_path, "w", encoding="utf-8") as f:
            f.write(inline_html)
        print("内嵌版已生成：%s（自含数据，单文件可分享）" % inline_html_path)
    except Exception as e:
        print("内嵌版生成失败：%s（不影响主工作台）" % e)
    return 0


# ---------- 主入口 ----------
def main():
    args = sys.argv[1:]

    login = load_login()
    if not login:
        print("错误：未找到 WorkBuddy 登录态文件，请先登录 WorkBuddy 客户端。")
        sys.exit(2)
    token, uid, account, filepath = login

    # 子命令
    if args and args[0] in ("status", "checkin", "record", "analyze", "render", "expire-check", "usage"):
        cmd = args[0]
        if cmd == "status":
            sys.exit(cmd_status(token, uid))
        if cmd == "checkin":
            sys.exit(cmd_checkin(token, uid))
        if cmd == "record":
            sys.exit(cmd_record(token, uid))
        if cmd == "analyze":
            sys.exit(cmd_analyze(token, uid))
        if cmd == "expire-check":
            hours = float(args[1]) if len(args) > 1 else 36
            sys.exit(cmd_expire_check(token, uid, hours))
        if cmd == "usage":
            sys.exit(cmd_usage())
        if cmd == "render":
            sys.exit(cmd_render(token, uid, account))

    show_json = "--json" in args
    show_token = "--token" in args
    export = "--export" in args
    export_path = None
    if export:
        i = args.index("--export")
        if i + 1 < len(args) and not args[i + 1].startswith("--"):
            export_path = args[i + 1]
        else:
            export_path = "WorkBuddy积分列表.md"

    if show_token:
        print(json.dumps({
            "uid": uid, "nickname": account.get("nickname"),
            "type": account.get("type"), "token": mask(token), "file": filepath,
        }, ensure_ascii=False, indent=2))
        return

    accounts, err = api_get_resource(token, uid)
    if err:
        print("查询失败：", err)
        sys.exit(1)
    packages = parse_accounts(accounts)
    groups = summarize(packages)

    if show_json:
        print(json.dumps({
            "uid": uid, "nickname": account.get("nickname"), "groups": groups,
        }, ensure_ascii=False, indent=2))
        return

    if export:
        md = render_markdown_flat(packages, account, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        with open(export_path, "w", encoding="utf-8") as f:
            f.write(md)
        print("已导出：%s（共 %d 个资源包）" % (export_path, len(packages)))
        return

    print(render_human(groups))


if __name__ == "__main__":
    main()
