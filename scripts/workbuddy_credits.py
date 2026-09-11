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
  python workbuddy_credits.py sync [--days N] # 同步并归档请求级消耗明细到本地（不含提示词文本）
  python workbuddy_credits.py requests [--limit N]  # 查看本地归档的请求流水（离线可读）
  python workbuddy_credits.py packages [--valid|--expired] [--limit N]  # 积分包生命周期明细
  python workbuddy_credits.py waste           # 权威过期浪费分析（含按到期月分布）
  python workbuddy_credits.py --json          # 余额原始 JSON
  python workbuddy_credits.py --token         # 登录态摘要（token 脱敏）
  python workbuddy_credits.py --export [路径]  # 导出按到期时间排序的 Markdown 列表

实测接口（2026-09-06 验证 HTTP 200）：
  积分资源包  POST https://copilot.tencent.com/billing/meter/get-user-resource
  签到状态    POST https://www.codebuddy.cn/v2/billing/meter/checkin-activity-status
  执行签到    POST https://www.codebuddy.cn/v2/billing/meter/daily-checkin
  请求明细    POST https://copilot.tencent.com/billing/meter/get-user-request-usage
              body: {startTime, endTime, pageNum, pageSize}（2026-09-10 验证）
  包明细(付费) POST https://copilot.tencent.com/billing/meter/get-user-resource-paid-packages
  包明细(免费) POST https://copilot.tencent.com/billing/meter/get-user-resource-free-packages
              body 必填 PackageCodes（付费/免费两套白名单，错传会被静默过滤成空）
              「已过期」必带 PackageEndTimeRangeBegin（-101 年）；PageSize 上限 200
  必带头：Authorization: Bearer <token>、X-User-Id: <uid>、
          User-Agent: Mozilla/5.0（缺此头返回 403 code=10085）
  注意：积分接口用 copilot.tencent.com 且无 /v2/ 前缀；签到接口用 codebuddy.cn 且有 /v2/ 前缀。
  注意：请求明细接口单次查询窗口宽度必须 ≤31 天，超窗会**静默**剥离 input/agentPurpose
        （HTTP 200 + code:0，无任何报错）；未来日期或起止倒置则**静默**返回 total:0。
"""

import json
import os
import sys
import time
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime, timedelta, date

# ---------- 接口配置 ----------
API_BILLING_BASE = "https://copilot.tencent.com/billing/meter"
API_GET_RESOURCE = "/get-user-resource"
API_REQUEST_USAGE = "/get-user-request-usage"
API_PKG_PAID = "/get-user-resource-paid-packages"
API_PKG_FREE = "/get-user-resource-free-packages"

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
REQUESTS_HISTORY_FILE = os.path.join(DATA_DIR, "requests_history.jsonl")

# 请求级明细（L5）归档约束 —— 均为实测硬约束
REQ_MAX_WINDOW_DAYS = 31   # 单次查询窗口宽度上限；>31 天服务端会静默剥离 input/agentPurpose
REQ_PAGE_SIZE = 500        # 实测无静默截断（5000 亦可），500 足够且更快
REQ_STORE_FIELDS = ("requestId", "requestTime", "credit", "model", "client", "agentPurpose")

# ---------- 积分包生命周期（L6）常量 ----------
# 包码全表 + 白名单分派，2026-09-10 从客户端 app.asar 的 CommodityCode 与
# PLAN_PACKAGE_CODES / PAID_PACKAGE_CODES / FREE_PACKAGE_CODES 提取（源码注释已确认同源）。
# **必须按白名单分派**：把付费码传给 free 端点会被后端**静默过滤成空**（源码注释亦点名此坑）。
PKG_CODES_PAID = [
    "TCACA_code_002_AkiJS3ZHF5",  # proMon        Pro 包月
    "TCACA_code_005_maRGyrHhw1",  # proMonPlus    Pro+ 包月
    "TCACA_code_003_FAnt7lcmRT",  # proYear       Pro 包年
    "TCACA_code_023_4xbGhMrE6q",  # youth         青年版
    "TCACA_code_026_BaESVICNoi",  # advanced      进阶版
    "TCACA_code_027_0FCGVA6vSa",  # flagship      旗舰版
    "TCACA_code_009_0XmEQc2xOf",  # extra         加量包
    "TCACA_code_038_OhvqZtiPKr",  # extra38       加量包(38)
    "TCACA_code_036_lupO5WgNdG",  # extraIntl     加量包(国际)
]
PKG_CODES_FREE = [
    "TCACA_code_001_PqouKr6QWV",  # free          免费包
    "TCACA_code_008_cfWoLwvjU4",  # freeMon       免费月包
    "TCACA_code_035_ArVxJcGDsm",  # freeMonIntl   免费月包(国际)
    "TCACA_code_006_DbXS0lrypC",  # gift          一次性赠送
    "TCACA_code_039_KRcQj7wUat",  # proTrialMon   Pro 试用月
    "TCACA_code_040_mi9rCYg46x",  # proTrialYear  Pro 试用年
    "TCACA_code_007_nzdH5h4Nl0",  # activity      运营活动/裂变
    "TCACA_code_028_NtpWi0jzXs",  # bonus28       奖励包 28
    "TCACA_code_037_WxOD3MpI2o",  # bonusIntl     奖励包(国际)
    "TCACA_code_029_6wCGEWquYy",  # bonus29       奖励包 29
    "TCACA_code_030_BjSt89qTvr",  # bonus30       奖励包 30
]
PKG_STATUS_EXPIRED = [2, 3]   # 已过期页签：已过期 + 已用完（到期时间已过）
PKG_STATUS_VALID = [0, 3]     # 有效期内页签：有效 + 已用完（用完但未过期仍属有效期内）
PKG_MAX_PAGE_SIZE = 200       # 契约限定 [1, 200]，超出后端报 ParameterInvalid
PKG_CAPACITY_TYPE_SLICE = 4   # 分片递减型（每日刷新的免费包），官网明细里过滤掉不展示
PKG_TIME_RANGE_YEARS = 101    # 已过期查询的时间下界跨度，等价「不限时间」

# skill 根目录（脚本位于 <skill>/scripts/ 下）
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD_DATA_FILE = os.path.join(SKILL_DIR, "dashboard_data.js")


# ---------- 登录态定位 ----------
def auth_candidates():
    home = os.path.expanduser("~")
    cands = []
    # 环境变量覆盖（最高优先级）：WORKBUDDY_AUTH_FILE 直接指定登录态文件路径。
    # 用于后台进程被沙箱拦截读 C 盘登录态的 Windows 环境，指向非 C 盘副本。
    override = os.environ.get("WORKBUDDY_AUTH_FILE")
    if override:
        cands.append(override)
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


def _norm_range(start, end):
    """把 date / datetime / 字符串统一成 (start_dt, end_dt)。
    只给日期时：start 补 00:00:00、end 补 23:59:59。无法解析返回 (None, None)。"""
    def _p(v, end_of_day):
        if isinstance(v, datetime):
            return v
        if isinstance(v, date):
            return datetime.combine(v, datetime.max.time().replace(microsecond=0)
                                    if end_of_day else datetime.min.time())
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                d = datetime.strptime(str(v), fmt)
            except Exception:
                continue
            if fmt == "%Y-%m-%d" and end_of_day:
                d = d.replace(hour=23, minute=59, second=59)
            return d
        return None

    s, e = _p(start, False), _p(end, True)
    if s is None or e is None:
        return None, None
    return s, e


def api_request_usage(token, uid, start, end, page_size=REQ_PAGE_SIZE):
    """拉取请求级消耗明细（服务端流水）并自动分页收敛。

    start/end：datetime.date / datetime / "YYYY-MM-DD"[ HH:MM:SS]。
    返回 (rows, meta, err)，meta = {total, fetched, window_start, window_end, window_days, clamped}。

    服务端实测硬约束（**均为静默失败，不报错**）：
      - 单次窗口宽度 >31 天 → input/agentPurpose 整批被剥离（HTTP 200 + code:0）
      - 未来日期 / 起止倒置 → HTTP 200 + code:0 + total:0
    故本函数自行收敛：未来 end 截到当前时刻、窗口压到 ≤REQ_MAX_WINDOW_DAYS 天，
    并把是否发生收敛写进 meta["clamped"]（调用侧可据此提示，而不是拿到空数据猜）。
    页码越界会 HTTP 400，因此按 total 收敛、不「翻到空为止」。
    """
    s_dt, e_dt = _norm_range(start, end)
    if s_dt is None:
        return None, None, "日期区间无法解析"
    clamped_end = clamped_window = False
    today = datetime.now().date()
    # 实测：end 落在「今天 23:59:59」是安全的（单日查询已验证），只有跨到**未来日期**才静默返 0
    if e_dt.date() > today:
        e_dt = datetime.combine(today, datetime.max.time().replace(microsecond=0))
        clamped_end = True
    if s_dt > e_dt:
        return None, None, "区间倒置：start 晚于 end（服务端会静默返回 total:0，此处直接拒绝）"
    # 注意含首尾的窗口宽度：e - (REQ_MAX_WINDOW_DAYS - 1) 才是 REQ_MAX_WINDOW_DAYS 天
    # （实测 31 天窗口 input 正常回填，32 天整批剥离 —— 差一天就踩红线）
    if (e_dt.date() - s_dt.date()).days + 1 > REQ_MAX_WINDOW_DAYS:
        s_dt = datetime.combine(e_dt.date() - timedelta(days=REQ_MAX_WINDOW_DAYS - 1),
                                datetime.min.time())
        clamped_window = True

    rows, page, total = [], 1, None
    while True:
        body = {
            "startTime": s_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "endTime": e_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "pageNum": page,
            "pageSize": page_size,
        }
        data, err = _api_call(API_BILLING_BASE, API_REQUEST_USAGE, token, uid, body)
        if err:
            return None, None, err
        payload = data.get("data") or {}
        if total is None:
            total = int(payload.get("total") or 0)
        chunk = payload.get("data") or []
        rows.extend(chunk)
        if not chunk or len(rows) >= total or page > (total // max(page_size, 1)) + 2:
            break
        page += 1
    meta = {
        "total": total or 0,
        "fetched": len(rows),
        "window_start": s_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "window_end": e_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "window_days": (e_dt.date() - s_dt.date()).days + 1,
        "clamped": clamped_end or clamped_window,
        "clamped_end": clamped_end,
        "clamped_window": clamped_window,
    }
    return rows, meta, None


def _split_pkg_codes(codes):
    """按白名单把码集拆成 (paid, free)。未知码丢弃并回报，避免静默走空。"""
    if codes is None:
        return list(PKG_CODES_PAID), list(PKG_CODES_FREE), []
    paid, free, unknown = [], [], []
    for c in codes:
        if c in PKG_CODES_PAID:
            paid.append(c)
        elif c in PKG_CODES_FREE:
            free.append(c)
        else:
            unknown.append(c)
    return paid, free, unknown


def api_packages(token, uid, status=None, codes=None, page_size=PKG_MAX_PAGE_SIZE):
    """拉取积分包明细（有效期内 / 已过期），自动按包码白名单分派到 paid / free 两个端点。

    status：PKG_STATUS_VALID(默认，有效期内) 或 PKG_STATUS_EXPIRED(已过期)。
    codes ：None = 全量白名单；也可只传子集（仍会自动分派）。
    返回 (items, meta, err)；items 已过滤掉「分片递减型」(CapacityType=4) 的当日刷新包。

    服务端实测/源码确认的坑：
      - 付费码传给 free 端点会被**静默过滤成空**（反之亦然）→ 必须分派
      - 「已过期」**必须**给 PackageEndTimeRangeBegin（-101 年，等价不限时间），
        否则后端不返回已过期的包；上界取当前时刻
      - PageSize 上限 200，超出后端报 ParameterInvalid
      - 缺 `User-Agent` → 403 code=10085（`_api_call` 已统一带）
      - 两路各自独立分页，**不能合并 TotalCount 判定 hasMore**
    """
    if status is None:
        status = PKG_STATUS_VALID
    paid, free, unknown = _split_pkg_codes(codes)
    page_size = max(1, min(int(page_size), PKG_MAX_PAGE_SIZE))

    if status == PKG_STATUS_EXPIRED:
        now = datetime.now()
        far_past = now - timedelta(days=365 * PKG_TIME_RANGE_YEARS)
        filter_body = {
            "Status": list(PKG_STATUS_EXPIRED),
            "PackageEndTimeRangeBegin": far_past.strftime("%Y-%m-%d %H:%M:%S"),
            "PackageEndTimeRangeEnd": now.strftime("%Y-%m-%d %H:%M:%S"),
            "OrderBy": "endTime",   # OrderBy=排序字段，SortBy=方向，勿记反
            "SortBy": "desc",
        }
    else:
        filter_body = {"Status": list(PKG_STATUS_VALID), "OnlyValidPeriod": True}

    items, errors = [], []
    for path, code_set in ((API_PKG_PAID, paid), (API_PKG_FREE, free)):
        if not code_set:
            continue
        page = 1
        total = None
        while True:
            body = {"PageNumber": page, "PageSize": page_size, "IsDisplayTotalInfo": True,
                    "PackageCodes": code_set}
            body.update(filter_body)
            data, err = _api_call(API_BILLING_BASE, path, token, uid, body)
            if err:
                errors.append("%s: %s" % (path.rsplit("-", 2)[-2], err))
                break
            payload = data.get("data") or {}
            if total is None:
                total = int(payload.get("TotalCount") or 0)
            chunk = payload.get("Accounts") or []
            items.extend(chunk)
            # 各路口径独立：不能合并总数判定；按本路 total 收敛，避免页码越界 400
            if not chunk or (total and page * page_size >= total):
                break
            page += 1

    if errors and not items:
        return None, None, "；".join(errors)

    kept = [a for a in items if int(_num(a.get("CapacityType"))) != PKG_CAPACITY_TYPE_SLICE]
    meta = {
        "total": len(items),
        "shown": len(kept),
        "filtered_slice": len(items) - len(kept),
        "paid_codes": len(paid),
        "free_codes": len(free),
        "unknown_codes": unknown,
        "status": list(status),
        "partial_errors": errors,
    }
    return kept, meta, None


def api_packages_both(token, uid):
    """一次取回「有效期内」+「已过期」两份清单。返回 ({"valid":..,"expired":..}, meta, err)。

    任一份失败不影响另一份（失败方为空列表并在 meta 标记）。
    """
    out, metas, errs = {}, {}, []
    for key, status in (("valid", PKG_STATUS_VALID), ("expired", PKG_STATUS_EXPIRED)):
        items, meta, err = api_packages(token, uid, status=status)
        out[key] = items or []
        if err:
            errs.append("%s: %s" % (key, err))
            metas[key] = {"error": err}
        else:
            metas[key] = meta
    if len(errs) == 2:
        return None, None, "；".join(errs)
    return out, {"valid": metas.get("valid"), "expired": metas.get("expired"),
                 "partial_errors": errs}, None


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


# ---------- 积分包生命周期（L6）解析 / 浪费分析 ----------
def parse_pkg_lifecycle(raw_accounts, status_kind="valid"):
    """把 L6 的 Accounts[] 规整成统一结构（金额口径与 parse_accounts 一致，取 Precise 优先）。

    status_kind: "valid" / "expired"，用于标注页签归属。
    """
    now_ts = time.time() * 1000
    out = []
    for a in raw_accounts or []:
        size = _num(a.get("CapacitySizePrecise") or a.get("CapacitySize"))
        used = _num(a.get("CapacityUsedPrecise") or a.get("CapacityUsed"))
        remain = _num(a.get("CapacityRemainPrecise") or a.get("CapacityRemain"))
        cycle_end = a.get("CycleEndTime") or ""
        deduction_end = a.get("DeductionEndTime")
        exp_ts = _parse_expire(cycle_end, deduction_end)
        st = int(_num(a.get("Status")))
        if st == 2:
            status_label = "已过期"
        elif st == 3:
            status_label = "已用完"
        elif st == 1:
            status_label = "已退款"
        else:
            status_label = "有效"
        out.append({
            "package_code": a.get("PackageCode", ""),
            "name": a.get("PackageName", ""),
            "size": size, "used": used, "remain": remain,
            "status": st, "status_label": status_label,
            "cycle_end": cycle_end,
            "expired_time": a.get("ExpiredTime"),
            "deduction_end": deduction_end,
            "exp_ts": exp_ts,
            "capacity_type": int(_num(a.get("CapacityType"))),
            "resource_id": a.get("ResourceId", ""),
            "kind": status_kind,
            "wasted": round(max(0.0, remain), 4) if status_kind == "expired" else 0.0,
        })
    return out


def compute_waste_authoritative(expired_items):
    """基于 L6 权威数据统计过期浪费（不再推算）。

    定义：**已过期**（Status=2，到期时间已过且未用尽）的包，其剩余额度即真实浪费。
    已用完(Status=3) 的包 remain 恒为 0，不产生浪费 —— 因此按 remain 求和天然正确。
    另外单独回报「已过期且从未使用」的包（used==0），这是最刺眼的浪费形态。
    """
    wasted_items = [i for i in expired_items if i["wasted"] > 0]
    unused_items = [i for i in expired_items if i["size"] > 0 and i["used"] <= 0]
    by_month = {}
    for i in wasted_items:
        month = (i.get("cycle_end") or "")[:7] or "(未知)"
        by_month[month] = round(by_month.get(month, 0.0) + i["wasted"], 4)
    return {
        "expired_count": len(expired_items),
        "wasted_count": len(wasted_items),
        "wasted_total": round(sum(i["wasted"] for i in wasted_items), 4),
        "never_used_count": len(unused_items),
        "never_used_total": round(sum(i["size"] for i in unused_items), 4),
        "by_month": dict(sorted(by_month.items())),
        "items": sorted(wasted_items, key=lambda x: x["wasted"], reverse=True),
    }


def req_daily(rows):
    """从请求流水（L5）聚合每日消耗。返回 [{date, credit}]（日期升序、连续）。"""
    daily = {}
    for r in rows or []:
        d = (r.get("requestTime") or "")[:10]
        if not d:
            continue
        daily[d] = daily.get(d, 0.0) + _num(r.get("credit"))
    if not daily:
        return []
    dates = sorted(daily)
    cur = datetime.strptime(dates[0], "%Y-%m-%d")
    end = datetime.strptime(dates[-1], "%Y-%m-%d")
    out = []
    while cur <= end:
        k = cur.strftime("%Y-%m-%d")
        out.append({"date": k, "credit": round(daily.get(k, 0.0), 4)})
        cur += timedelta(days=1)
    return out


def req_hourly(rows):
    """从请求流水（L5）聚合逐小时消耗（日期 × 小时），供热力图。"""
    grid = {}
    for r in rows or []:
        rt = r.get("requestTime") or ""
        if len(rt) < 13:
            continue
        d, h = rt[:10], rt[11:13]
        try:
            hi = int(h)
        except Exception:
            continue
        slot = grid.setdefault(d, [0.0] * 24)
        slot[hi] = round(slot[hi] + _num(r.get("credit")), 4)
    return grid


def req_breakdown(rows, field):
    """按字段（model / client / agentPurpose）聚合消耗，返回降序 [{name, credit, count}]。"""
    agg = {}
    for r in rows or []:
        name = (r.get(field) or "").strip() or "(未知)"
        e = agg.setdefault(name, {"name": name, "credit": 0.0, "count": 0})
        e["credit"] = round(e["credit"] + _num(r.get("credit")), 4)
        e["count"] += 1
    return sorted(agg.values(), key=lambda x: (-x["credit"], -x["count"]))


def compute_today_used_l5(rows, today=None):
    """今日消耗（L5 口径）：来自服务端流水，精确到请求。"""
    today = today or datetime.now().strftime("%Y-%m-%d")
    return round(sum(_num(r.get("credit")) for r in rows or []
                     if (r.get("requestTime") or "")[:10] == today), 4)


# ---------- 双通路对账（L5 请求级 ↔ L2 包级采样）----------
# 结构说明（2026-09-10 读码修正，勿再按「三方对账」理解）：
#   A 通路 = L5 /get-user-request-usage         —— 独立记账口径，有服务端历史
#   B 通路 = L3 /get-user-resource → L2 采样存档 —— 同一字段的不同时点采样，**不独立**
#   故对账只有 A ↔ B 一条比较线；L2 内部不能自证。
# 三态必须显式，禁止把「没有基准」当成「对不上」而误告警。
RECONCILE_TOLERANCE = 0.05   # 偏差阈值 5%（按周/月聚合比对，单日噪声大）
RECONCILE_WARMUP_DAYS = 14   # 上线前两周只记录不告警


def reconcile(l5_rows, usage_hist, snapshots=None, tolerance=RECONCILE_TOLERANCE,
              income_events=None, l2_start=None):
    """双通路对账：L5 请求级消耗 vs L2 逐包余量差分。

    返回 {status, reason, window, l5_total, l2_total, diff, diff_pct, by_day, alerts}
    status 三态：
      "no_baseline" —— L2 采样起始晚于 L5（或根本没有），**无基准，不告警**
      "low_precision" —— L2 采样点不足（<2 天或样本太少），**只给趋势，不告警**
      "ok" —— 可对账，diff_pct 超阈值才产生 alert

    income_events：L1/L3 记录的真实到账事件（含 resource_id），用于把 L2 的
    「新包到账」伪增量与真实口径偏差区分开 —— 这是防误报的关键。
    """
    l5_daily = {d["date"]: d["credit"] for d in req_daily(l5_rows)}
    l5_dates = sorted(l5_daily)
    if not l5_dates:
        return {"status": "no_baseline", "reason": "L5 无数据（未同步或窗口内无请求）",
                "window": None, "l5_total": 0.0, "l2_total": 0.0,
                "l2_start": None, "income_artifacts": [], "baseline_gap_days": [],
                "diff": 0.0, "diff_pct": None, "by_day": [], "alerts": []}

    # L2 采样覆盖：每个包每天是否至少 2 个采样点（才能算出日内/跨天差值）
    l2_dates, sample_points = [], 0
    for _rid, entry in (usage_hist or {}).get("packages", {}).items():
        stamps = sorted(entry.get("daily", {}).keys())
        sample_points += len(stamps)
        for s in stamps:
            l2_dates.append(s[:10])
    l2_start = min(l2_dates) if l2_dates else None
    l5_start = l5_dates[0]

    if l2_start is None or l2_start > l5_start:
        return {"status": "no_baseline",
                "reason": "L2 无采样或采样起始(%s)晚于 L5 起始(%s) —— 装 skill 前的时段无基准"
                          % (l2_start or "-", l5_start),
                "window": None, "l2_start": l2_start,
                "l5_total": round(sum(l5_daily.values()), 4),
                "l2_total": 0.0, "diff": 0.0, "diff_pct": None, "by_day": [],
                "income_artifacts": [], "baseline_gap_days": [], "alerts": []}

    l2_daily = {d["date"]: d["credit"] for d in compute_usage_daily(usage_hist)}
    # L2 首个采样日**没有跨天基线**，该日 L2 恒为 0 —— 属机制性缺口，
    # 必须从可对账窗口剔除，否则会永远把「采样首日」算成偏差。
    window = [d for d in l5_dates if d > l2_start]
    if len(window) < 2 or sample_points < 4:
        return {"status": "low_precision",
                "reason": "可对账窗口仅 %d 天 / L2 采样点 %d 个，样本不足 —— 只给趋势不告警"
                          % (len(window), sample_points),
                "window": [window[0], window[-1]] if window else None,
                "l2_start": l2_start,
                "l5_total": round(sum(l5_daily.get(d, 0) for d in window), 4),
                "l2_total": round(sum(l2_daily.get(d, 0) for d in window), 4),
                "diff": 0.0, "diff_pct": None, "by_day": [],
                "income_artifacts": [], "baseline_gap_days": [], "alerts": []}

    by_day, l5_sum, l2_sum = [], 0.0, 0.0
    for d in window:
        a, b = round(l5_daily.get(d, 0.0), 4), round(l2_daily.get(d, 0.0), 4)
        l5_sum += a
        l2_sum += b
        by_day.append({"date": d, "l5": a, "l2": b, "diff": round(a - b, 4)})
    diff = round(l5_sum - l2_sum, 4)
    diff_pct = round(abs(diff) / l5_sum * 100, 2) if l5_sum > 0 else None

    # ---- 归因 A：L2 采样首日无跨天基线 → 该日 L2 恒为 0，属机制性缺口而非偏差 ----
    # ---- 归因 B：新包到账被误算成消耗 → 用真实到账事件(ResourceId)定位 ----
    known_income = {str(e.get("resource_id")) for e in (income_events or [])}
    income_artifacts, baseline_gap_days = [], []
    if l2_start:
        baseline_gap_days = [d for d in window if d <= l2_start]
    for rid, entry in (usage_hist or {}).get("packages", {}).items():
        stamps = sorted(entry.get("daily", {}).keys())
        if not stamps:
            continue
        first_day = stamps[0][:10]
        if first_day not in window:
            continue
        # 该包是否确有到账事件（ResourceId 命中）—— 命中才算污染确证
        if str(rid) not in known_income:
            continue
        size = _num(entry.get("size"))
        first_used = _num(entry["daily"][stamps[0]].get("used"))
        if size > 0 and first_used > 0:
            income_artifacts.append({
                "resource_id": rid, "name": entry.get("name", ""),
                "day": first_day, "size": round(size, 2), "used": round(first_used, 2),
            })

    alerts = []
    span_days = len(window)
    if diff_pct is not None and diff_pct > tolerance * 100:
        artifact_total = round(sum(a["used"] for a in income_artifacts), 2)
        gap_note = ("；另 L2 采样首日 %s 无跨天基线（L2 该日恒为 0），属机制性缺口"
                    % "、".join(baseline_gap_days)) if baseline_gap_days else ""
        if artifact_total and abs(diff) <= artifact_total * 1.1:
            alerts.append({"level": "info",
                           "msg": "L2 偏高 %.2f，可由 %d 个「新包到账被误算成消耗」解释"
                                  "（伪增量合计 %.2f）—— 属 L2 已知缺陷，非接口异常，"
                                  "已由 L5 口径覆盖%s"
                                  % (abs(diff), len(income_artifacts), artifact_total, gap_note)})
        elif span_days < RECONCILE_WARMUP_DAYS:
            alerts.append({"level": "info",
                           "msg": "偏差 %.2f%% 超阈值（阈值 %.0f%%），但可对账窗口仅 %d 天"
                                  "（未满 %d 天预热期），仅记录不告警%s"
                                  % (diff_pct, tolerance * 100, span_days,
                                     RECONCILE_WARMUP_DAYS, gap_note)})
        else:
            alerts.append({"level": "warn",
                           "msg": "L5 与 L2 偏差 %.2f%% 超过阈值 %.0f%% —— 可能口径变化或接口静默降级%s"
                                  % (diff_pct, tolerance * 100, gap_note)})
        worst = max(by_day, key=lambda x: abs(x["diff"]), default=None)
        if worst and worst["diff"]:
            alerts.append({"level": "info",
                           "msg": "偏差最大的一天：%s（L5 %.2f / L2 %.2f）"
                                  % (worst["date"], worst["l5"], worst["l2"])})
    return {"status": "ok", "reason": "可对账窗口 %d 天%s"
                                      % (span_days,
                                         "（起始日 %s 为 L2 首个采样日，无跨天基线）"
                                         % l2_start if baseline_gap_days else ""),
            "window": [window[0], window[-1]],
            "l2_start": l2_start,
            "l5_total": round(l5_sum, 4), "l2_total": round(l2_sum, 4),
            "diff": diff, "diff_pct": diff_pct, "by_day": by_day,
            "income_artifacts": income_artifacts,
            "baseline_gap_days": baseline_gap_days,
            "alerts": alerts}


def checkin_status_ok(checkin_dates, today=None):
    """判断今日签到状态（L4 独立性检查用）。"""
    today = today or datetime.now().strftime("%Y-%m-%d")
    return today in set(checkin_dates or [])


def sources_health(l5_meta, l6_meta, reconcile_result, l2_sample_points=0, checkin_ok=None):
    """数据源健康徽章（三态显式）。每个源都给 {status, label, detail}。

    status ∈ {"ok", "degraded", "missing", "no_baseline", "low_precision"}
    —— 显式三态是刻意的：数据源不可用时**绝不静默跳过**，页面必须能看见。
    """
    out = {}
    if l5_meta is None:
        out["l5"] = {"status": "missing", "label": "未同步",
                     "detail": "请求流水未同步；可运行 sync 补齐（可回溯至 2026-05）"}
    elif l5_meta.get("error"):
        out["l5"] = {"status": "missing", "label": "抓取失败", "detail": str(l5_meta["error"])}
    else:
        deg = bool(l5_meta.get("clamped_window"))
        out["l5"] = {"status": "degraded" if deg else "ok",
                     "label": "降级" if deg else "正常",
                     "detail": "窗口 %s ~ %s（%s 天），共 %s 条%s"
                               % (l5_meta.get("window_start", "?")[:10],
                                  l5_meta.get("window_end", "?")[:10],
                                  l5_meta.get("window_days", "?"),
                                  l5_meta.get("stored", l5_meta.get("fetched", "?")),
                                  "；窗口已收敛至 ≤31 天" if deg else "")}
    if l6_meta is None:
        out["l6"] = {"status": "missing", "label": "未获取",
                     "detail": "包生命周期未获取（有效期内 / 已过期均失败）"}
    else:
        errs = l6_meta.get("partial_errors") or []
        vm = l6_meta.get("valid") or {}
        em = l6_meta.get("expired") or {}
        got = (vm.get("shown") is not None) or (em.get("shown") is not None)
        detail = ("有效期内 %s 条 / 已过期 %s 条"
                  % (vm.get("shown", "-"), em.get("shown", "-"))) if got \
            else "包明细接口不可用"
        if errs:
            detail += "；失败：%s" % "；".join(str(e)[:80] for e in errs)
        out["l6"] = {"status": "degraded" if errs else "ok",
                     "label": "部分失败" if errs else "正常", "detail": detail}
    st = (reconcile_result or {}).get("status")
    out["l2"] = {"status": st or "no_baseline",
                 "label": {"ok": "已对账", "no_baseline": "无基准",
                           "low_precision": "精度不足"}.get(st, "未知"),
                 "detail": "%s；采样点 %d" % ((reconcile_result or {}).get("reason", "-"),
                                              l2_sample_points)}
    if checkin_ok is not None:
        out["l4"] = {"status": "ok" if checkin_ok else "degraded",
                     "label": "今日已签" if checkin_ok else "今日未签",
                     "detail": "签到数据为本地独立来源，不受服务端接口变动影响"}
    return out


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

    每项：{session_id, title, model, updated_at(ms), credit(该会话消耗积分),
           n_calls, request_ids: {requestId: credit}}
    `request_ids` 的 key **就是服务端 requestId**（2026-09-10 实测命中），
    因此可把服务端流水（L5）与本地会话库（L1）按 requestId 精确缝合。
    覆盖最近约 17 天（本地库仅保留近期会话，会被客户端滚动清理）。返回空列表表示无数据或库不可读。
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
        req_ids = {}
        if cj:
            try:
                j = json.loads(cj)
                credit = sum(float(v) for v in j.values())
                n_calls = len(j)
                req_ids = {str(k): float(v) for k, v in j.items()}
            except Exception:
                pass
        usage.append({
            "session_id": sid,
            "title": title or "",
            "model": model or "",
            "updated_at": upd,
            "credit": round(credit, 4),
            "n_calls": n_calls,
            "request_ids": req_ids,
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

    粒度说明：快照 key 精确到分钟，同一分钟内重复采样会互相覆盖（取最后一次）。
    采集点（2026-09-10 核实）：运行 `record` 命令，以及**每一次刷新工作台**
    —— serve.py 的 /api/data 与 /dashboard_data.js 都会实时调用本函数所在的
    build_dashboard_data。此前注释所称"自动化任务每小时跑一次"目前**并不存在**
    （本机 automations 为空），如需保证采样密度应另行创建定时任务。
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
        # L6 字段（2026-09-10 新增）：每条采样都带状态与到期时间，
        # 使「包状态时序」随 L2 采样自然积累 —— 这是 L6 唯一无法事后回补的维度。
        entry["cycle_end"] = p.get("cycle_end", "")
        entry["last_status"] = "可用" if p.get("available") else (
            "已过期" if p.get("expired") else "已用完")
        daily[stamp] = {"used": round(p["used"], 4), "remain": round(p["remain"], 4),
                        "status": entry["last_status"],
                        "cycle_end": p.get("cycle_end", "")}
    _save_json(USAGE_HISTORY_FILE, hist)
    return hist


def compute_usage_daily(hist):
    """从逐分钟 used 历史计算每日消耗（逐包差值，免疫签到/裂变/过期）。

    历史天 = Σ(各包 used 跨天增量)；今天 = Σ(各包 used 日内增量，last - first)。
    返回日期连续的 [{date, credit}]。

    ⚠️ **已知缺陷（2026-09-10 实测定位）**：某包**首次出现采样**的当天，
    若它已到账并被立即使用（used 从 0 跳到 size），跨天基线取不到 → 该增量会被
    误算成「消耗」。实测 9/09 因此虚增 6 个新包共 ~322 分（L2 534.40 vs L5 517.33，
    其中 6 条 `0 → 100` 纯属到账）。
    **因此本函数只应作为兜底口径**：消耗展示优先 L5（`req_daily`），
    本函数供余额曲线与对账基准使用，`reconcile` 会把该伪增量单独归因（`income_artifacts`）。
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


def build_dashboard_data(token, uid, account, sync_days=None):
    """构建工作台数据 dict。返回 (data, err)。供 render 命令与 serve.py 复用。

    分层口径（2026-09-10 重构）：
      权威层 L5 请求流水 / L6 包生命周期 —— 主导消耗与浪费展示
      观测层 L2 逐包采样 / L1 本地会话库 —— 骨架、预测、**对账基准**
      L4 签到 —— 独立
    任一新增源失败**不连累整页**：降级为该块为空 + 健康徽章标红，其余照常渲染。
    """
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

    # ===== 权威层 L5：请求流水（同步 + 归档；失败不影响整页）=====
    l5_rows, l5_meta = [], None
    try:
        want = sync_days if sync_days is not None else REQ_MAX_WINDOW_DAYS
        l5_rows, l5_meta, l5_err = sync_requests(token, uid, want)
        if l5_err:
            l5_meta = {"error": l5_err}
            l5_rows = []
    except Exception as e:               # 兜底：绝不因 L5 抛异常而白屏
        l5_meta = {"error": "L5 同步异常: %s" % e}
        l5_rows = []
    l5_rows = l5_rows or []

    # ===== 权威层 L6：包生命周期（失败不影响整页）=====
    try:
        l6_lists, l6_meta, l6_err = api_packages_both(token, uid)
        if l6_err:
            l6_meta = {"partial_errors": [l6_err]}
            l6_lists = {"valid": [], "expired": []}
    except Exception as e:
        l6_meta = {"partial_errors": ["L6 异常: %s" % e]}
        l6_lists = {"valid": [], "expired": []}
    l6_valid = parse_pkg_lifecycle((l6_lists or {}).get("valid"), "valid")
    l6_expired = parse_pkg_lifecycle((l6_lists or {}).get("expired"), "expired")
    waste_auth = compute_waste_authoritative(l6_expired)

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

    # 精确消耗（本地 session_usage 表）—— 留守观测层，用于会话明细与预测
    usage = load_usage()
    usage_total = round(sum(u["credit"] for u in usage), 4)
    usage_daily_local = usage_daily(usage)
    usage_today = sum(d["credit"] for d in usage_daily_local if d["date"] == today_str)

    # 观测层 L2：逐包采样（自积累，用于余额曲线与**对账基准**）
    usage_hist = accumulate_usage(packages)
    udaily_l2 = compute_usage_daily(usage_hist)
    uhourly_l2 = compute_usage_hourly(usage_hist)

    # ===== 口径切换：L5 优先，L2 兜底 =====
    udaily_l5 = req_daily(l5_rows)
    l5_ok = bool(udaily_l5)
    udaily = udaily_l5 if l5_ok else udaily_l2
    usage_source = "L5" if l5_ok else "L2"
    if l5_ok:
        # 用 L5 已覆盖的日期替换 L2 的对应天（L5 精确到请求），L5 未覆盖的天保留 L2
        merged = {d["date"]: d["credit"] for d in udaily_l2}
        merged.update({d["date"]: d["credit"] for d in udaily_l5})
        dates = sorted(merged)
        cur = datetime.strptime(dates[0], "%Y-%m-%d")
        end_d = datetime.strptime(dates[-1], "%Y-%m-%d")
        udaily = []
        while cur <= end_d:
            k = cur.strftime("%Y-%m-%d")
            udaily.append({"date": k, "credit": round(merged.get(k, 0.0), 4)})
            cur += timedelta(days=1)
    uhourly = req_hourly(l5_rows) if l5_ok else uhourly_l2
    today_used_l5 = compute_today_used_l5(l5_rows, today_str)
    today_used_l2 = compute_today_used(usage_hist)
    today_used = today_used_l5 if l5_ok else today_used_l2

    # ===== 双通路对账（在 income_events 就绪后执行，见下方 ledger 段）=====
    l2_points = sum(len(e.get("daily", {})) for e in usage_hist.get("packages", {}).values())

    # ===== L1 ⋈ L5：用 requestId 把会话标题缝到请求流水上 =====
    session_titles = {}
    for u in usage:
        for rid in (u.get("request_ids") or []):
            session_titles[rid] = u
    requests_view = []
    for r in l5_rows:
        rid = str(r.get("requestId") or "")
        sess = session_titles.get(rid)
        requests_view.append({
            "request_id": rid,
            "time": (r.get("requestTime") or ""),
            "credit": _num(r.get("credit")),
            "model": r.get("model") or "-",
            "client": r.get("client") or "-",
            "agent_purpose": r.get("agentPurpose") or "",
            "session_title": (sess or {}).get("title") or "",
            # 提示词：**仅运行时存在于内存**，落盘零残留（file:// 模式该字段为空字符串）
            "input": r.get("input") or "",
            "has_input": bool((r.get("input") or "").strip()),
        })

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

    # ===== 双通路对账（此处 income_events 已就绪，可区分「到账伪增量」与真实偏差）=====
    l2_start = None
    for e in usage_hist.get("packages", {}).values():
        for s in e.get("daily", {}):
            if l2_start is None or s[:10] < l2_start:
                l2_start = s[:10]
    rec = reconcile(l5_rows, usage_hist, income_events=income_events, l2_start=l2_start)
    health = sources_health(l5_meta, l6_meta, rec, l2_points,
                            checkin_status_ok(st.get("checkin_dates"), today_str))

    # ---- 预测：近 7 天日均消耗 + 预计可用天数 ----
    # 用统一的 udaily（L5 优先、L2 兜底）而非本地库：L5 覆盖 30+ 天且免疫
    # 「包到账被误算成消耗」的伪增量（见 compute_usage_daily 的已知缺陷说明）。
    # 同时剔除今日（未完天）以免日均被低估。
    hist_days = [d["credit"] for d in udaily if d["date"] < today_str]
    recent7 = hist_days[-7:]
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
        "today_used_l5": today_used_l5,
        "today_used_l2": today_used_l2,
        "usage_total": usage_total,
        "usage_daily": udaily,
        "usage_daily_l2": udaily_l2,
        "usage_source": usage_source,
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
        "waste_authoritative": waste_auth,
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
        # ---- 新增（v1.3.0）----
        "requests": requests_view,                        # L5 请求级明细（input 仅运行时）
        "requests_meta": dict(l5_meta or {}, source=usage_source),
        "model_breakdown": req_breakdown(l5_rows, "model"),
        "client_breakdown": req_breakdown(l5_rows, "client"),
        "purpose_breakdown": req_breakdown(l5_rows, "agentPurpose"),
        "packages_lifecycle": {
            "valid": l6_valid,
            "expired": l6_expired,
            "meta": l6_meta,
        },
        "reconcile": rec,
        "sources_health": health,
    }
    return data, None


def strip_prompts(data):
    """剥离所有提示词字段，供**落盘**通道（render / dashboard_data.js / inline）使用。

    「不落盘提示词」是硬约束：`input` 只在 HTTP 响应（serve.py 实时生成）中存在，
    render 写出的任何文件都必须先过本函数。返回浅拷贝后的安全副本。
    """
    out = dict(data)
    rows = []
    for r in data.get("requests") or []:
        r2 = dict(r)
        r2["input"] = ""          # 置空而非删键，前端可统一按空串处理
        r2["has_input"] = False
        rows.append(r2)
    out["requests"] = rows
    out["requests_meta"] = dict(data.get("requests_meta") or {}, prompts_stripped=True)
    return out


def cmd_render(token, uid, account):
    data, err = build_dashboard_data(token, uid, account)
    if err:
        print("查询失败：", err)
        return 1
    data = strip_prompts(data)          # 落盘前强制剥离提示词（硬约束）
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


# ---------- 请求级明细归档（L5，2026-09-10 新增）----------
# 设计要点：
#   1) 只归档「消耗侧」白名单字段，requestId 为唯一键，幂等 upsert；
#   2) **提示词 input/inputTrunc 永不落盘** —— 只在当次进程内存中存在；
#   3) JSONL 逐行存储（非整体 JSON），全量重写用临时文件 + os.replace 保证原子性；
#   4) 服务端可回溯至 2026-05（窗口宽度 ≤31 天即可），因此历史可事后回补。
def _sanitize_request(row, fetched_at=None):
    """落盘用的瘦身：仅保留白名单字段；input/inputTrunc 一律丢弃。"""
    out = {k: row.get(k) for k in REQ_STORE_FIELDS if k in row}
    if row.get("fetched_at"):
        out["fetched_at"] = row["fetched_at"]
    if fetched_at:
        out["fetched_at"] = fetched_at
    return out


def load_requests():
    """读取本地请求流水归档，返回 {requestId: row}（同一 id 后写覆盖先写）。"""
    out = {}
    if not os.path.exists(REQUESTS_HISTORY_FILE):
        return out
    try:
        with open(REQUESTS_HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                rid = str(r.get("requestId") or "")
                if rid:
                    out[rid] = r
    except Exception:
        pass
    return out


def save_requests(rows):
    """全量重写 JSONL（原子写）。落盘前强制剥离 input/inputTrunc。返回写入条数。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    items = list(rows.values()) if isinstance(rows, dict) else list(rows)
    clean = [_sanitize_request(r) for r in items if r.get("requestId")]
    clean.sort(key=_req_sort_key)
    tmp = REQUESTS_HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for r in clean:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, REQUESTS_HISTORY_FILE)   # 原子替换，避免半截文件
    return len(clean)


def _req_sort_key(r):
    return (r.get("requestTime") or "", str(r.get("requestId") or ""))


def sync_requests(token, uid, days=REQ_MAX_WINDOW_DAYS, page_size=REQ_PAGE_SIZE):
    """抓取最近 days 天请求流水，与本地归档按 requestId 合并。

    返回 (merged_rows, meta, err)：
      merged_rows —— 全量，按时间倒序。**历史行无 input；本次抓到的行带 input，仅存在于内存**。
      meta        —— api_request_usage 的 meta + {stored, fresh}
    """
    today = datetime.now().date()
    start = today - timedelta(days=max(1, int(days)) - 1)
    fresh, meta, err = api_request_usage(token, uid, start, today, page_size)
    if err:
        return None, None, err

    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stored = load_requests()
    fresh_by_id = {}
    for r in fresh:
        rid = str(r.get("requestId") or "")
        if rid:
            fresh_by_id[rid] = r

    # 归档：白名单字段（无 input）；已存在的行保留首次见到的 fetched_at
    merged_store = dict(stored)
    new_count = 0
    for rid, r in fresh_by_id.items():
        if rid not in stored:
            new_count += 1
        rec = _sanitize_request(r, fetched_at)
        prev = stored.get(rid) or {}
        rec["fetched_at"] = prev.get("fetched_at") or fetched_at
        merged_store[rid] = rec
    save_requests(merged_store)

    # 展示用：历史行 + 本次行（本次行保留 input，仅在内存中）
    display = dict(merged_store)
    display.update(fresh_by_id)
    rows = sorted(display.values(), key=_req_sort_key, reverse=True)

    meta = dict(meta or {})
    meta.update({"stored": len(merged_store), "fresh": len(fresh_by_id), "new": new_count})
    return rows, meta, None


def cmd_sync(token, uid, days=REQ_MAX_WINDOW_DAYS):
    """同步并归档请求级消耗明细（不落盘提示词）。"""
    rows, meta, err = sync_requests(token, uid, days)
    if err:
        print("同步失败：", err)
        return 1
    print("请求流水已同步：%s ~ %s（窗口 %d 天）"
          % (meta["window_start"], meta["window_end"], meta["window_days"]))
    print("  服务端返回 %d 条；本次新增 %d 条；本地归档共 %d 条"
          % (meta["fetched"], meta.get("new", 0), meta["stored"]))
    if meta.get("clamped_end"):
        print("  注意：end 已收敛到今日（未来日期服务端会静默返回 total:0）")
    if meta.get("clamped_window"):
        print("  注意：查询区间已收敛到 ≤%d 天（超窗服务端会静默剥离 input/agentPurpose）"
              % REQ_MAX_WINDOW_DAYS)
    print("  归档文件：%s（不含提示词文本）" % REQUESTS_HISTORY_FILE)
    return 0


def cmd_requests(limit=20):
    """查看本地归档的请求流水（离线可读；归档本身不含提示词）。"""
    rows = sorted(load_requests().values(), key=_req_sort_key, reverse=True)
    if not rows:
        print("本地尚无归档，请先运行：python workbuddy_credits.py sync")
        return 0
    print("本地请求流水归档共 %d 条（%s）" % (len(rows), REQUESTS_HISTORY_FILE))
    print("%-19s %8s %-24s %-14s %s" % ("时间", "积分", "模型", "客户端", "用途"))
    for r in rows[:max(1, int(limit))]:
        print("%-19s %8s %-24s %-14s %s" % (
            (r.get("requestTime") or "")[:19],
            r.get("credit"),
            (r.get("model") or "-")[:24],
            (r.get("client") or "-")[:14],
            r.get("agentPurpose") or "-",
        ))
    return 0


def cmd_packages(status_kind="valid", limit=50):
    """查看积分包生命周期明细（有效期内 / 已过期），带权威浪费汇总。"""
    login = load_login()
    if not login:
        print("错误：未找到 WorkBuddy 登录态文件，请先登录 WorkBuddy 客户端。")
        return 2
    token, uid, _account, _path = login
    status = PKG_STATUS_EXPIRED if status_kind == "expired" else PKG_STATUS_VALID
    raw, meta, err = api_packages(token, uid, status=status)
    if err:
        print("查询失败：", err)
        return 1
    kind = "expired" if status_kind == "expired" else "valid"
    items = parse_pkg_lifecycle(raw, kind)
    title = "已过期" if kind == "expired" else "有效期内"
    print("积分包明细（%s）：%d 条，其中分片包已过滤 %d 条"
          % (title, len(items), meta.get("filtered_slice", 0)))
    if meta.get("unknown_codes"):
        print("  警告：忽略未知包码 %s" % meta["unknown_codes"])
    if meta.get("partial_errors"):
        print("  警告：部分来源失败 %s" % meta["partial_errors"])
    print()
    if kind == "expired":
        w = compute_waste_authoritative(items)
        print("权威浪费统计：已过期 %d 个，其中 %d 个有剩余额度，浪费合计 %.2f 积分"
              % (w["expired_count"], w["wasted_count"], w["wasted_total"]))
        print("  按到期月：%s" % (w["by_month"] or "-"))
        print("  从未使用：%d 个，合计 %.2f 积分"
              % (w["never_used_count"], w["never_used_total"]))
        print()
    print("%-30s %9s %9s %9s %-8s %s"
          % ("名称", "总量", "已用", "剩余", "状态", "到期"))
    for i in sorted(items, key=lambda x: x.get("exp_ts") or 0, reverse=True)[:max(1, int(limit))]:
        print("%-30s %9.2f %9.2f %9.2f %-8s %s"
              % (i["name"][:28], i["size"], i["used"], i["remain"],
                 i["status_label"], (i["cycle_end"] or "-")[:19]))
    return 0


def cmd_waste():
    """权威过期浪费报告（基于 L6，不再推算）。"""
    login = load_login()
    if not login:
        print("错误：未找到 WorkBuddy 登录态文件，请先登录 WorkBuddy 客户端。")
        return 2
    token, uid, _account, _path = login
    raw, meta, err = api_packages(token, uid, status=PKG_STATUS_EXPIRED)
    if err:
        print("查询失败：", err)
        return 1
    items = parse_pkg_lifecycle(raw, "expired")
    w = compute_waste_authoritative(items)
    print("过期浪费分析（权威口径，来源 /get-user-resource-{paid,free}-packages）")
    print("  已过期包总数：%d" % w["expired_count"])
    print("  产生浪费的包：%d 个" % w["wasted_count"])
    print("  浪费积分合计：%.2f" % w["wasted_total"])
    print("  从未被使用：%d 个，合计 %.2f 积分"
          % (w["never_used_count"], w["never_used_total"]))
    print()
    print("  按到期月：")
    for m, v in (w["by_month"] or {}).items():
        print("    %s  %.2f" % (m, v))
    if w["items"]:
        print()
        print("  浪费最多的 10 个包：")
        for i in w["items"][:10]:
            print("    %-28s 剩余 %8.2f / 总量 %8.2f  到期 %s"
                  % (i["name"][:26], i["remain"], i["size"], (i["cycle_end"] or "-")[:10]))
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
    if args and args[0] in ("status", "checkin", "record", "analyze", "render",
                            "expire-check", "usage", "sync", "requests",
                            "packages", "waste"):
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
        if cmd == "sync":
            days = REQ_MAX_WINDOW_DAYS
            if "--days" in args:
                i = args.index("--days")
                if i + 1 < len(args):
                    days = int(args[i + 1])
            sys.exit(cmd_sync(token, uid, days))
        if cmd == "requests":
            limit = 20
            if "--limit" in args:
                i = args.index("--limit")
                if i + 1 < len(args):
                    limit = int(args[i + 1])
            sys.exit(cmd_requests(limit))
        if cmd == "packages":
            kind = "valid"
            if "--expired" in args:
                kind = "expired"
            elif "--valid" in args:
                kind = "valid"
            limit = 50
            if "--limit" in args:
                i = args.index("--limit")
                if i + 1 < len(args):
                    limit = int(args[i + 1])
            sys.exit(cmd_packages(kind, limit))
        if cmd == "waste":
            sys.exit(cmd_waste())

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
