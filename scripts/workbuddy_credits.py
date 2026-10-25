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
import re
import sys
import threading
import time
import sqlite3
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, date
from concurrent.futures import ThreadPoolExecutor

# ---------- 版本号 ----------
# 权威来源是 manifest.yaml（打包 / 发布 / 工作台展示共用同一处，避免多处漂移）。
_SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_version():
    """从 manifest.yaml 读 version。只做单行解析，不引入 yaml 依赖。"""
    try:
        with open(os.path.join(_SKILL_DIR, "manifest.yaml"), "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("version:"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return "unknown"


VERSION = _read_version()

# ---------- 接口配置 ----------
API_BILLING_BASE = "https://copilot.tencent.com/billing/meter"
API_GET_RESOURCE = "/get-user-resource"
API_REQUEST_USAGE = "/get-user-request-usage"
API_PKG_PAID = "/get-user-resource-paid-packages"
API_PKG_FREE = "/get-user-resource-free-packages"

API_CHECKIN_BASE = "https://www.codebuddy.cn/v2/billing/meter"
API_CHECKIN_STATUS = "/checkin-activity-status"
API_DAILY_CHECKIN = "/daily-checkin"

# 账号信息（昵称 / 角色）。**注意不在 `/billing/meter` 前缀下** —— 是站点级路径，
# 所以桥模式下直接用完整路径请求。旧版从本机登录态文件的 `account` 段读，
# 凭据加密后改为走这个接口。
ACCOUNT_PATH = "/console/account"

# CapacityType -> 分类
#   4 = 个人体验版（套餐基础用量）；1 = 权益赠送包（运营裂变包）；其余 = 加量包/其他
TYPE_TRIAL = 4
TYPE_GIFT = 1
TYPE_LABEL = {TYPE_TRIAL: "套餐用量", TYPE_GIFT: "权益赠送包"}

# 套餐档位：订阅型包（CapacityType=4）PackageName 的中文关键词 → 英文档位。
# 中文四档：体验版(免费)/标准版/高级版/旗舰版 ↔ Free/Plus/Pro/Max。
# 按通用英文直觉 Pro > Plus：标准版→Plus、高级版/进阶版→Pro。
# 顺序即优先级（最具体的 Max/旗舰排最前；"pro+"/"promonplus" 须先于 "pro" 判定，
# 否则 Pro+ 包月会误命中标准版 Plus 的 "pro"）。
PLAN_TIER_KEYWORDS = [
    ("Max",  ("旗舰", "flagship", "max")),
    ("Pro",  ("高级", "进阶", "advanced", "plus", "pro+", "promonplus")),
    ("Plus", ("标准", "pro", "promon", "proyear", "青年", "youth")),
    ("Free", ("体验", "免费", "试用", "free", "trial")),
]


def plan_tier(package_name):
    """订阅型包名 → 档位（Free/Pro/Plus/Max）。识别不出返回空串。"""
    if not package_name:
        return ""
    low = str(package_name).lower()
    for tier, kws in PLAN_TIER_KEYWORDS:
        for kw in kws:
            if kw in low:
                return tier
    return ""

# 快照数据目录（独立于 skill 目录，避免 skill 更新/删除影响历史数据）
DATA_DIR = os.path.join(os.path.expanduser("~"), ".workbuddy", "workbuddy-credits-data")
SNAPSHOT_FILE = os.path.join(DATA_DIR, "snapshots.jsonl")
CHECKIN_HISTORY_FILE = os.path.join(DATA_DIR, "checkin_history.json")
INCOME_EVENTS_FILE = os.path.join(DATA_DIR, "income_events.json")
USAGE_HISTORY_FILE = os.path.join(DATA_DIR, "usage_history.json")
REQUESTS_HISTORY_FILE = os.path.join(DATA_DIR, "requests_history.jsonl")
# 套餐/订阅型批次的消耗累积（L6 结构上不含 CapacityType=4，且其 used 每周期归零，
# 只能本地按 resource_id 记「本期最大值 + 已结束周期合计」）
SUITE_USAGE_FILE = os.path.join(DATA_DIR, "suite_usage.json")

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
HOLIDAYS_FILE = os.path.join(SKILL_DIR, "assets", "holidays.json")


def _read_holidays_file():
    """读取 assets/holidays.json 原文。文件缺失 / 损坏时返回空 dict（不抛）。"""
    try:
        with open(HOLIDAYS_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def load_holiday_calendar():
    """读取内置的中国法定节假日 / 调休上班日（assets/holidays.json）。

    用途**只有一个**：判断某个日期是否属于「用户不会打开工作台的日子」，
    好把到期提醒顺延到他会看到的时候。**不参与任何消耗口径计算**
    （原因见 dev-notes「工作日历」一节：用工作日口径算"还能用多少天"会算错）。

    数据缺失（如次年安排尚未公布）时返回空表 —— 前端会降级为"只判周六周日"，
    不会误判成"天天上班"。
    """
    return _read_holidays_file().get("years") or {}


def holiday_data_status(today=None):
    """节假日数据自身的「够不够用」状态 —— 供 UI 提醒用户更新。

    为什么需要：这份表**每年都要更新**（国务院办公厅通常在前一年 11 月公布次年安排），
    而漏更新的后果是**静默降级**（只剩周六周日判断，识别不出长假与调休），
    用户不会察觉。所以到点主动提醒一次，而不是指望人记得。

    需要哪几年：**当年** 恒需；进入 **11 月**后连**次年**一起需要
    （按惯例此时次年安排已公布，再没有就是漏更新了）。

    返回（纯数据，不做是否展示的判断 —— 那取决于用户本地开关，属前端职责）：
        {"updated_at": str, "years": [int...], "needed": [int...],
         "missing": [int...], "stale": bool}
    """
    raw = _read_holidays_file()
    have = []
    for k in (raw.get("years") or {}).keys():
        try:
            have.append(int(k))
        except (TypeError, ValueError):
            continue
    have.sort()

    d = today or date.today()
    needed = [d.year]
    if d.month >= 11:
        needed.append(d.year + 1)
    missing = [y for y in needed if y not in have]

    return {
        "updated_at": raw.get("updated_at") or "",
        "years": have,
        "needed": needed,
        "missing": missing,
        "stale": bool(missing),
    }


# ---------- 取数通道 ----------
# 2026-09-26 起：客户端凭据已**加密落盘**（`$wbEncrypted` + AES-GCM 封套，密钥在客户端内），
# 直连 API 的 `Authorization: Bearer` 路径失效。默认改走**浏览器桥**：借官网（workbuddy.cn）
# 已登录的浏览器会话取数 —— HttpOnly cookie 始终留在浏览器内，本进程不接触明文凭据。
# 置 WORKBUDDY_TRANSPORT=direct 可退回旧的直连实现（仅在凭据恢复明文时有意义）。
TRANSPORT = os.environ.get("WORKBUDDY_TRANSPORT", "bridge").strip().lower()
BRIDGE_PREFIX = "/billing/meter"   # 桥模式下所有接口都挂在这个前缀下（换域名后路径同名）

# 会话失效标记：桥返回 401/403 时置位，供 UI 提示「点此重新登录」
SESSION_STATE = {"expired": False, "at": None, "reason": ""}

# 过期标志的粘性时长（秒）。粘性标志的初衷是会话已过期时避免反复撞墙（每次真实
# 调用都要等网关 401 才回来，浪费几秒且无意义）。但让它**永久**粘住，会话在浏览器侧
# 恢复（换浏览器/被续期）后页面也不会自愈。故加 TTL：到期后放行一次真实重试，
# 成功则清标志、失败则重新计时 —— 失败态不应比失败本身更持久。
SESSION_EXPIRED_TTL = 60


def _session_short_circuit():
    """是否该因「会话过期」短路当前请求（粘性标志 + TTL）。"""
    if not SESSION_STATE["expired"]:
        return False
    at = SESSION_STATE.get("at")
    if at and time.time() - at > SESSION_EXPIRED_TTL:
        return False   # 到期：放行，给一次真实重试的机会（成功会在 _bridge_call 里清标志）
    return True


_SESSION_LOGGED_OK = False   # 本进程内「会话有效」只记一次，避免刷屏

# 健康状态：供**启动器**判断「服务在跑、但取数异常」（区别于「服务是否在跑」）。
# 只记**非会话类**失败 —— 会话过期是「需重新登录」的正常状态，不算服务故障。
_HEALTH = {"last_ok_at": None, "last_err_at": None, "last_err": ""}


def health_status():
    """服务 + 桥的健康状态（供 /api/health 与启动器判断是否强制重启）。"""
    ok_at = _HEALTH["last_ok_at"]
    err_at = _HEALTH["last_err_at"]
    # 「有错误、且错误晚于（或没有）成功」= 取数异常 → 启动器应重启服务
    degraded = bool(err_at) and (not ok_at or err_at > ok_at)
    return {
        "ok": True,
        "version": VERSION,
        "last_ok_at": ok_at,
        "last_err_at": err_at,
        "last_err": (_HEALTH["last_err"] or "")[:200],
        "degraded": degraded,
        # L1 本地会话库读取实况：排查「面板空着」时先看这里，别猜
        "l1": dict(_L1_DIAG),
        # 本地数据文件（快照 / 到账事件 / 签到历史）读取实况：error 非空即文件损坏
        "files": {k: dict(v) for k, v in _FILE_DIAG.items()},
    }


def _log_session(event, detail=""):
    """会话事件日志（print → serve.log）。

    目的是**累积「cookie 到期 vs 实际失效」的观测数据**：cookie 上写 7 天，但实测
    服务端会话可能更早失效（9-27 还成功、9-28 就 401）；多记几次就能看出真实规律。
    """
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        exp_s, left = "-", "-"
        try:
            info = get_bridge().session_info() or {}
            exp = info.get("expires_at")
            if exp:
                exp_s = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp / 1000.0))
            left = info.get("days_left", "-")
        except Exception:
            pass
        print("[session] %s | cookie到期=%s | 剩余=%s天 | %s | %s"
              % (ts, exp_s, left, event, detail))
    except Exception:
        pass


_BRIDGE = None
_BRIDGE_LOCK = threading.Lock()


def bridge_enabled():
    return TRANSPORT == "bridge"


def _load_bridge_module():
    """导入 browser_bridge（与本文件同目录）。兼容 scripts/ 不在 sys.path 的场景。"""
    try:
        import browser_bridge
        return browser_bridge
    except Exception:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_bridge.py")
        spec = importlib.util.spec_from_file_location("wb_browser_bridge", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def get_bridge():
    """惰性创建浏览器桥单例 —— 进程内复用同一个浏览器。

    注意是**懒加载**：首次调用才启动 Chrome（约 3~5s），之后每次取数仅 0.1~0.3s。
    """
    global _BRIDGE
    if _BRIDGE is None:
        with _BRIDGE_LOCK:
            if _BRIDGE is None:
                Bridge = _load_bridge_module().BrowserBridge
                _BRIDGE = Bridge()
    return _BRIDGE


def bridge_close():
    """进程退出前关闭浏览器（配合 atexit）。"""
    global _BRIDGE
    if _BRIDGE is not None:
        try:
            _BRIDGE.close()
        except Exception:
            pass
        _BRIDGE = None


def bridge_relogin(wait_seconds=300):
    """弹出**可见**窗口请用户重新登录。返回 (ok, message)。"""
    global _SESSION_LOGGED_OK
    ok, msg = get_bridge().relogin(wait_seconds=wait_seconds)
    # ⚠️ **无论成败都清「过期」标志**：relogin 已尝试过，会话状态可能已变（用户
    # 实际登录成功、但探针因页面跳转没测到 → 返回 False）。若不清，前端兜底探测
    # /api/overview 会被粘性标志短路成 session_expired → **页面不刷新**（实测）。
    # 清掉后交由真实调用重新判定：有效则前端刷新，无效则再次置位（用户可再登）。
    SESSION_STATE.update(expired=False, at=None, reason="")
    if ok:
        _SESSION_LOGGED_OK = True   # 刚登录，视为本进程已记过「有效」
        _log_session("重新登录成功", "")
    else:
        _log_session("重新登录未完成", msg or "")
    return ok, msg


def bridge_session_info():
    """桥模式下读取浏览器会话的到期时间（**只读元信息**）。非桥模式返回 None。"""
    if not bridge_enabled():
        return None
    try:
        info = get_bridge().session_info()
    except Exception:
        return None
    if not info:
        return None
    info["kind"] = "browser"
    return info


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


# 最近一次 load_login() 失败的原因（供 UI / CLI 给出可操作提示）
AUTH_FAILURE = {"kind": None, "reason": ""}


def load_login():
    """读取本机登录态，返回 (token, uid, account, path)；不可用时返回 None。

    ⚠️ 2026-09-26 起：WorkBuddy 客户端把凭据**加密落盘**了 —— `auth.accessToken`
    等字段从明文字符串变成 AES-GCM 封套：
        {"$wbEncrypted": 1,
         "envelope": "<base64 of {suite, keyId, nonce, authTag, ciphertext}>"}
    密钥在客户端内，本技能拿不到明文。此时**明确记录原因并返回 None**，
    绝不把 dict 当成 token 传下去（否则会在 `"Bearer " + token` 处抛 TypeError，
    表现为三个面板一起报「can only concatenate str (not "dict") to str」）。
    调用方若要区分「未登录」与「凭据已加密」，用 auth_diagnosis()。
    """
    if bridge_enabled():
        # 桥模式：凭据由浏览器持有，本进程不需要（也不应持有）明文 token。
        # 返回占位三元组 —— 这样所有上层调用方（build_dashboard_data / cmd_* / serve.py）
        # 都无需感知「通道差异」，改动被收敛在传输层这一处。
        return "", "", {"nickname": "浏览器会话", "type": ""}, "<browser-bridge>"
    seen_encrypted = False
    for p in auth_candidates():
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            auth = d.get("auth") or {}
            token = auth.get("accessToken", "")
            uid = (d.get("account") or {}).get("uid", "")
            if isinstance(token, dict) or isinstance(uid, dict):
                seen_encrypted = True
                continue
            if not token or not uid:
                continue
            return token, uid, d.get("account") or {}, p
        except Exception:
            continue
    if seen_encrypted:
        AUTH_FAILURE["kind"] = "encrypted"
        AUTH_FAILURE["reason"] = (
            "WorkBuddy 客户端已把登录凭据加密落盘（accessToken 为 AES-GCM 封套，字段标记 "
            "$wbEncrypted）。本技能无法在不解密的前提下读取明文凭据 —— 这不是接口被封，"
            "而是凭据保护升级。详见 README「凭据与登录态」一节。")
    else:
        AUTH_FAILURE["kind"] = AUTH_FAILURE["kind"] or "missing"
        AUTH_FAILURE["reason"] = AUTH_FAILURE["reason"] or "未找到登录态文件（请先登录 WorkBuddy 客户端）"
    return None


def auth_diagnosis():
    """返回 (status, message)：`ok` / `encrypted` / `missing`。

    为什么要单列 `encrypted`：客户端加密凭据后，技能**不是**「用户没登录」，
    而是「登录态存在但读不出明文」—— 两者给用户的提示与后续动作完全不同。
    """
    if bridge_enabled():
        if _session_short_circuit():
            return "session_expired", (SESSION_STATE.get("reason")
                                       or "浏览器会话已过期，请重新登录")
        return "ok", ""
    if load_login():
        return "ok", ""
    kind = AUTH_FAILURE.get("kind") or "missing"
    return kind, AUTH_FAILURE.get("reason") or ""


# ---------- 通用 API 调用 ----------
_OPENER = None


def _direct_opener():
    """返回显式直连的 opener：不挂任何代理，忽略系统与环境代理解析。

    为什么必须显式传空代理表（不能用裸 urlopen）：CPython 的 urllib 把
    「进程首次请求时的代理配置」冻结在模块级全局 opener 里
    （urllib/request.py 的 _opener，build_opener 每进程只执行一次；
    ProxyHandler.__init__ 在构造那一刻就调用 getproxies() 并固化结果）。
    于是常驻进程一旦在代理开启时发过请求，之后代理软件退出、端口关闭，
    它仍会向那个死端口发 CONNECT，报 WinError 10061 且**永不自愈**。

    本技能默认直连（与 update.py 的既定约定一致），传 ProxyHandler({})
    会让 urllib 跳过默认的 ProxyHandler，从根上消除上述隐患。
    """
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _OPENER


def _friendly_net_error(e, url=""):
    """把底层网络异常翻译成用户可操作的提示语。"""
    msg = str(e)
    errno = getattr(e, "errno", None)
    if errno is None:
        errno = getattr(getattr(e, "reason", None), "errno", None)
    host = ""
    try:
        netloc = urllib.parse.urlsplit(url).netloc
        host = netloc.split("@")[-1].split(":")[0] if netloc else ""
    except Exception:
        pass
    where = ("（目标 %s）" % host) if host else ""
    low = msg.lower()
    if errno == 10061 or "10061" in msg:
        return ("连接被拒绝%s：对方未接受连接。本技能默认直连、不使用代理；"
                "若你开过代理软件，请确认它仍在运行，或稍后重试。" % where)
    if errno == 10060 or "timed out" in low or "timeout" in low:
        return "连接超时%s：网络不通或对方无响应，请稍后重试。" % where
    if errno in (11001, 11004) or "getaddrinfo" in low:
        return "域名解析失败%s：请检查网络与 DNS 设置。" % where
    if isinstance(e, urllib.error.URLError):
        return "网络请求失败%s：%s" % (where, msg)
    return msg


def _run_parallel(tasks, max_workers=8):
    """并发执行 tasks（[(key, fn), ...]），返回 {key: (value, exception_or_None)}。

    仅做编排：每个 fn 自身的返回值 / 异常原样收集。
    设计前提是「各源相互独立」——一路抛异常只写进自己的槽位，不影响其余，
    与 build_dashboard_data 既有的「任一源失败不连累整页」降级策略一致。

    为什么值得并发：工作台首屏的耗时几乎全在网络往返（实测 4 路串行 3.84s，
    纯 CPU 装配仅 0.03s）。并发后首屏由「各源之和」变成「最慢的一路」。
    """
    out = {}
    if not tasks:
        return out
    workers = max(1, min(len(tasks), max_workers))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn): key for key, fn in tasks}
        for fut, key in futs.items():
            try:
                out[key] = (fut.result(), None)
            except Exception as e:                      # noqa: BLE001 —— 兜底收集
                out[key] = (None, e)
    return out


def _bridge_call(path, body=None):
    """走浏览器桥取数。返回 (data, err)，与直连版 `_api_call` 同构。

    桥模式下所有接口都挂在同一个前缀下 —— 换域名后路径同名（实测 `copilot.tencent.com`
    与 `codebuddy.cn/v2` 的差异**不再存在**），故 `base` 参数被忽略。
    """
    try:
        bridge = get_bridge()
    except Exception as e:
        return None, "浏览器桥不可用: %s" % e
    res = bridge.call(BRIDGE_PREFIX + path, body)
    if res["ok"]:
        _HEALTH["last_ok_at"] = time.time()
        if SESSION_STATE["expired"]:
            # 会话在浏览器侧已恢复（自愈）：清除粘性标志，让后续请求不再短路
            SESSION_STATE.update(expired=False, at=None, reason="")
        global _SESSION_LOGGED_OK
        if not _SESSION_LOGGED_OK:
            _SESSION_LOGGED_OK = True
            _log_session("会话有效（首次取数成功）", "")
        return res["data"], None
    if res["kind"] == "session_expired":
        SESSION_STATE.update(expired=True, at=time.time(), reason=res.get("error") or "")
        _log_session("会话失效（服务端 401）", res.get("error") or "")
        return None, "浏览器会话已过期，请在工作台点「重新登录」后重试"
    # 非会话类失败（桥异常 / 页面失效等）→ 记入健康状态，供启动器判断「取数是否异常」
    _HEALTH["last_err_at"] = time.time()
    _HEALTH["last_err"] = res.get("error") or ""
    return None, res.get("error") or "浏览器桥调用失败"


def _api_call(base, path, token, uid, body=None):
    if bridge_enabled():
        return _bridge_call(path, body)
    url = base + path
    data = json.dumps(body).encode("utf-8") if body is not None else b"{}"
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("X-User-Id", uid)
    req.add_header("User-Agent", "Mozilla/5.0")  # 关键：缺此头返回 403
    try:
        with _direct_opener().open(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, _friendly_net_error(e, url)
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


def api_account_info():
    """取当前账号的昵称与角色。返回 (dict, err)。

    桥模式：`GET /console/account` → `{uid, nickname, uin, type, isAdmin, ...}`
    （`type` 如 `personal`，官网前端把它渲染成「个人版」）。
    直连模式：退化为登录态文件里的 `account` 段。
    """
    if bridge_enabled():
        res = get_bridge().call(ACCOUNT_PATH, method="GET")
        if res["ok"]:
            if SESSION_STATE["expired"]:
                SESSION_STATE.update(expired=False, at=None, reason="")
            return ((res["data"] or {}).get("data") or {}), None
        if res["kind"] == "session_expired":
            SESSION_STATE.update(expired=True, at=time.time(),
                                 reason=res.get("error") or "")
            _log_session("会话失效（账号接口 401）", res.get("error") or "")
            return None, "浏览器会话已过期"
        return None, res.get("error") or "取账号信息失败"
    login = load_login()
    if not login:
        return None, "未登录"
    return (login[2] or {}), None


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

    def _fetch_side(path, code_set):
        """抓取单一路（paid 或 free）的全部页。返回 (items, error_or_None)。

        两路口径独立：各自按本路 TotalCount 收敛，绝不合并总数判定
        （合并会把另一路多出的页算进来，翻页越界直接 HTTP 400）。
        """
        side_items, page, total = [], 1, None
        while True:
            body = {"PageNumber": page, "PageSize": page_size, "IsDisplayTotalInfo": True,
                    "PackageCodes": code_set}
            body.update(filter_body)
            data, err = _api_call(API_BILLING_BASE, path, token, uid, body)
            if err:
                return side_items, "%s: %s" % (path.rsplit("-", 2)[-2], err)
            payload = data.get("data") or {}
            if total is None:
                total = int(payload.get("TotalCount") or 0)
            chunk = payload.get("Accounts") or []
            side_items.extend(chunk)
            if not chunk or (total and page * page_size >= total):
                break
            page += 1
        return side_items, None

    # paid / free 两路并发（各自独立分页）——把 2 次往返压成 1 次的时间
    tasks = []
    if paid:
        tasks.append(("paid", lambda: _fetch_side(API_PKG_PAID, paid)))
    if free:
        tasks.append(("free", lambda: _fetch_side(API_PKG_FREE, free)))

    items, errors = [], []
    res = _run_parallel(tasks, max_workers=2)
    for key in ("paid", "free"):            # 固定顺序拼接，保证输出稳定可复现
        if key not in res:
            continue
        value, exc = res[key]
        if exc is not None:                 # 线程层异常（理论上 _api_call 已兜住）
            errors.append("%s: %s" % (key, exc))
            continue
        side_items, side_err = value
        items.extend(side_items)
        if side_err:
            errors.append(side_err)

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
    两份清单并发拉取（各自内部还要分 paid/free 两路），是首屏最重的一环：
    实测串行 2.02s → 并发后 ≈ 0.55s。
    """
    res = _run_parallel([
        ("valid", lambda: api_packages(token, uid, status=PKG_STATUS_VALID)),
        ("expired", lambda: api_packages(token, uid, status=PKG_STATUS_EXPIRED)),
    ], max_workers=2)

    out, metas, errs = {}, {}, []
    for key in ("valid", "expired"):        # 固定顺序，输出稳定
        value, exc = res.get(key, (None, None))
        if exc is not None:
            errs.append("%s: %s" % (key, exc))
            out[key], metas[key] = [], {"error": str(exc)}
            continue
        items, meta, err = value
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


def _capacity_views(a):
    """解析一条资源的「累计」与「周期」两套容量口径。

    服务端对同一条记录同时返回两组容量字段（实测确认）：
      累计 CapacitySize / CapacityUsed / CapacityRemain
      周期 CycleCapacitySize / CycleCapacityUsed / CycleCapacityRemain
    两组都有 Precise 精确版（字符串），优先取精确版。

    返回 {"cum": (size, used, remain) | None, "cyc": (size, used, remain) | None}；
    一组内任一字段缺失即为 None（**区别于真实的 0**，故不能用 _num 兜成 0）。
    """
    def pick(prefix):
        vals = []
        for base in ("Size", "Used", "Remain"):
            v = None
            for k in (prefix + "Capacity" + base + "Precise",
                      prefix + "Capacity" + base):
                raw = a.get(k)
                if raw is None or raw == "":
                    continue
                try:
                    v = float(raw)
                    break
                except (TypeError, ValueError):
                    continue
            vals.append(v)
        return None if any(x is None for x in vals) else tuple(vals)

    return {"cum": pick(""), "cyc": pick("Cycle")}


def _pick_capacity(a, key):
    """按资源类型选出「当前可用」口径，返回 (size, used, remain, cycle_based, diverged, views)。

    为什么订阅型必须改用周期口径（CapacityType=4，套餐用量）：
      实测同一个资源对象的 `CapacityUsed=0` 而 `CycleCapacityUsed=500`。**周期区间是
      累计区间的子集，累计已用不可能小于周期已用** —— 两者自相矛盾，说明服务端对
      订阅型的累计字段**不随周期消耗更新**，长时间停留在「重置后初值」。
      官网控制台（「套餐与用量」显示的下次权益周期更新时间 = CycleEndTime）按**周期口径**
      渲染，故周期口径才是真实可用的余额。
    其余类型（赠送包/加量包等）两组数值实测一致，沿用累计口径以保持既有语义。

    diverged：两组是否不一致（真分叉），供 UI 如实标注。
    """
    views = _capacity_views(a)
    cum, cyc = views["cum"], views["cyc"]
    diverged = bool(cum and cyc and cum != cyc)
    if key == TYPE_TRIAL and cyc is not None:
        size, used, remain = cyc
        return size, used, remain, True, diverged, views
    if cum is not None:
        size, used, remain = cum
        return size, used, remain, False, diverged, views
    if cyc is not None:                       # 累计缺失时退而用周期
        size, used, remain = cyc
        return size, used, remain, True, diverged, views
    return 0.0, 0.0, 0.0, False, diverged, views


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
        key, label = classify(a)
        size, used, remain, cycle_based, diverged, views = _pick_capacity(a, key)
        cycle_end = a.get("CycleEndTime", "")
        exp_ts = _parse_expire(cycle_end, a.get("DeductionEndTime"))
        expired = exp_ts is not None and exp_ts < now_ts
        packages.append({
            "key": key, "label": label, "name": a.get("PackageName", ""),
            "remain": remain, "size": size, "used": used,
            # 订阅型存在两套口径：生效值 + 两组留档（供 UI 标注与悬浮对照）
            "cycle_based": cycle_based,
            "diverged": diverged,
            "remain_cum": (views["cum"] or (None, None, None))[2],
            "remain_cyc": (views["cyc"] or (None, None, None))[2],
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
    """把 L6 的 Accounts[] 规整成统一结构（容量口径与 parse_accounts 共用 _pick_capacity）。

    status_kind: "valid" / "expired"，用于标注页签归属。
    """
    now_ts = time.time() * 1000
    out = []
    for a in raw_accounts or []:
        ctype = int(_num(a.get("CapacityType")))
        size, used, remain, cycle_based, diverged, _views = _pick_capacity(a, ctype)
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
            "cycle_based": cycle_based, "diverged": diverged,
            "status": st, "status_label": status_label,
            "cycle_end": cycle_end,
            # 到账时刻：累积「包到账」事件、重建余额趋势都要用它。
            # 早先这里没保留 CycleStartTime，导致**已结束的包无法补记到账** ——
            # 总账缺口 2761 分，倒推余额因此在 9 月出现负数（2026-10-24 查）。
            "cycle_start": a.get("CycleStartTime") or "",
            "expired_time": a.get("ExpiredTime"),
            "deduction_end": deduction_end,
            "exp_ts": exp_ts,
            "capacity_type": ctype,
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
    """从请求流水（L5）聚合逐小时消耗（日期 × 小时）。

    ⚠️ 返回结构**必须**与 compute_usage_hourly() 保持一致：[{date, hour, credit}]。
    否则 usage_hourly 字段会在「L5 可用 / 不可用」两条分支下变成不同类型
    （历史上是 dict-grid vs list 的分歧，下游一旦消费就直接崩）。
    """
    cells = {}
    for r in rows or []:
        rt = r.get("requestTime") or ""
        if len(rt) < 13:
            continue
        d, h = rt[:10], rt[11:13]
        try:
            hi = int(h)
        except Exception:
            continue
        if not 0 <= hi <= 23:      # 防御：时间串异常时不要产生非法小时格（24 格热力图会错位）
            continue
        cells[(d, hi)] = round(cells.get((d, hi), 0.0) + _num(r.get("credit")), 4)
    return [{"date": d, "hour": h, "credit": cells[(d, h)]}
            for d, h in sorted(cells)]


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

# 「预计可用天数」的观测窗口与最低日均：
#   窗口 30 天 —— 7 天太短，会被「最近一周恰好没怎么用」这类异常期完全主导。
#     实测 2026-09-26：近 7 天日均 0.59 →「9473 天」；近 30 天日均 77.74 →「72 天」✓
#   下限 1.0 —— 即便在 30 天窗口下，均值若仍低于此值说明样本过稀、除出来的天数会荒谬，
#     此时不估算（页面显示 —），副标题如实给出「有消耗的天数」说明原因。
#   窗口可经**工作台设置页**配置（v1.7.7 起），存 DATA_DIR/config.json；这里的是默认值。
DAILY_AVG_WINDOW_DAYS = 30
DAILY_AVG_WINDOW_MIN = 7        # 可配置下限：再短会被短期异常主导
DAILY_AVG_WINDOW_MAX = 90       # 可配置上限
DAYS_LEFT_MIN_AVG = 1.0

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")


def _read_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def get_avg_window():
    """「预计可用天数」的观测窗口（天）。可经工作台设置页配置，默认 30。"""
    v = _read_config().get("avg_window_days")
    try:
        v = int(v)
    except (TypeError, ValueError):
        return DAILY_AVG_WINDOW_DAYS
    if not (DAILY_AVG_WINDOW_MIN <= v <= DAILY_AVG_WINDOW_MAX):
        return DAILY_AVG_WINDOW_DAYS
    return v


def set_avg_window(days):
    """写入观测窗口配置。返回 (ok, message)。"""
    try:
        v = int(days)
    except (TypeError, ValueError):
        return False, "窗口必须是数字"
    if not (DAILY_AVG_WINDOW_MIN <= v <= DAILY_AVG_WINDOW_MAX):
        return False, "窗口需在 %d~%d 天之间" % (DAILY_AVG_WINDOW_MIN, DAILY_AVG_WINDOW_MAX)
    cfg = _read_config()
    cfg["avg_window_days"] = v
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_FILE)
        return True, "已保存（观测窗口 %d 天）" % v
    except Exception as e:
        return False, "保存失败: %s" % e


# 取数浏览器偏好：auto（自动）/ chrome / msedge。存 config.json 的 browser_channel。
BROWSER_CHANNEL_CHOICES = ("auto", "chrome", "msedge")


def get_browser_channel():
    """用户手动指定的取数浏览器。auto = 自动（Edge 优先、Chrome 回退 + 记住上次登录）。"""
    v = _read_config().get("browser_channel", "auto")
    return v if v in BROWSER_CHANNEL_CHOICES else "auto"


def set_browser_channel(channel):
    """写取数浏览器偏好。返回 (ok, message)。"""
    v = (channel or "auto").strip().lower()
    if v not in BROWSER_CHANNEL_CHOICES:
        return False, "浏览器取值须为 auto / chrome / msedge"
    cfg = _read_config()
    cfg["browser_channel"] = v
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_FILE)
        label = {"auto": "自动", "chrome": "Google Chrome", "msedge": "Microsoft Edge"}.get(v, v)
        return True, "已保存（取数浏览器：%s）" % label
    except Exception as e:
        return False, "保存失败: %s" % e


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

    # L1 本地会话库（workbuddy.db / session_usage）：「最近会话消耗」面板的唯一来源。
    # 早先读失败被静默返回 []，前端只能显示「所选范围内暂无会话记录」——
    # 用户看到的是「你没用量」，实情往往是「没读到」。这里必须把两者分开。
    d = _L1_DIAG
    if not d.get("path"):
        out["l1"] = {"status": "missing", "label": "未读取",
                     "detail": "本次构建未读取本地会话库"}
    elif not d.get("exists"):
        out["l1"] = {"status": "missing", "label": "无本地库",
                     "detail": "找不到 %s —— 客户端可能尚未创建会话库" % d.get("path")}
    elif d.get("sql_error"):
        out["l1"] = {"status": "missing", "label": "读取失败",
                     "detail": "本地会话库打不开（%s）—— 面板空着不代表没有消耗"
                               % str(d.get("sql_error"))[:120]}
    elif not d.get("rows"):
        out["l1"] = {"status": "degraded", "label": "空库",
                     "detail": "本地会话库可读，但 session_usage 尚无记录"}
    else:
        note = ""
        if d.get("open_mode") == "ro_immutable":
            # 只读快照模式：完全绕开 -shm，代价是可能漏掉尚未 checkpoint 的写入
            note = "；只读快照模式（可能少最近几条未落盘的会话）"
        out["l1"] = {"status": "ok", "label": "正常",
                     "detail": "本地会话库 %d 条%s" % (d.get("rows"), note)}
    return out


# ---------- 快照 ----------
def _last_snapshot_stamp():
    """只读文件尾部取最后一条快照的时间戳（不整文件加载）。

    快照文件会随天数无限增长，为了一个「同一分钟别重复写」的判断去 load_snapshots()
    整份读一遍不划算 —— 尾读 8KB 足够覆盖最后一两条。
    """
    try:
        if not os.path.exists(SNAPSHOT_FILE):
            return ""
        with open(SNAPSHOT_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", "ignore")
        for line in reversed([x for x in tail.splitlines() if x.strip()]):
            try:
                return str(json.loads(line).get("time", ""))[:16]
            except Exception:
                continue      # 末行可能是进程被杀时截断的坏行，继续往前找
        return ""
    except Exception:
        return ""


def record_snapshot(packages, checkin=None, dedupe_minute=True):
    """把当前余额落成一条快照；dedupe_minute 时同一分钟不重复写。

    为什么要去重：页面每点一次刷新都会走到这里。一分钟内连点几下，会在时间轴上
    叠出好几个几乎相同的点 —— 既没有信息量，又会让「实测天数」统计虚高
    （按行数算就变成「1 天 5 个采样」）。去重后一天内的采样密度才反映真实行为。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%dT%H:%M")
    if dedupe_minute and _last_snapshot_stamp() == stamp:
        return None
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
    diag = {"path": SNAPSHOT_FILE, "exists": os.path.exists(SNAPSHOT_FILE),
            "lines": 0, "bad_lines": 0, "error": ""}
    _FILE_DIAG["snapshots.jsonl"] = diag
    if not diag["exists"]:
        return []
    snaps = []
    try:
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    snaps.append(json.loads(line))
                except Exception:
                    # 坏行计数：快照是逐行追加的，进程被杀时最后一行常被截断。
                    # 静默跳过会让时间轴凭空少一段，必须留下证据。
                    diag["bad_lines"] += 1
                    continue
    except Exception as e:
        diag["error"] = "%s: %s" % (type(e).__name__, e)
        return []
    diag["lines"] = len(snaps)
    return snaps


# ---------- 本地消耗明细（workbuddy.db / session_usage） ----------
def _usage_db_path():
    return os.path.join(os.path.expanduser("~"), ".workbuddy", "workbuddy.db")


# L1 读取诊断：空列表有**三种完全不同的含义**（库不存在 / 库不可读 / 库里确实没有行），
# 早先一律返回 [] 并被前端渲染成「所选范围内暂无会话记录」—— 把「读不到」伪装成
# 「没有数据」，用户只能看到一张空表，无从判断是自己没用量还是功能坏了。
# 这里记录最后一次读取的真实情况，由 /api/health 与 sources_health 对外暴露。
_L1_DIAG = {"path": "", "exists": False, "rows": 0, "sql_error": "", "checked_at": 0}


def load_usage():
    """读取本地 session_usage 表，返回会话级消耗明细列表。

    每项：{session_id, title, model, updated_at(ms), credit(该会话消耗积分),
           n_calls, request_ids: {requestId: credit}}
    `request_ids` 的 key **就是服务端 requestId**（2026-09-10 实测命中），
    因此可把服务端流水（L5）与本地会话库（L1）按 requestId 精确缝合。
    覆盖最近约 17 天（本地库仅保留近期会话，会被客户端滚动清理）。返回空列表表示无数据或库不可读。
    """
    db = _usage_db_path()
    _L1_DIAG.update({"path": db, "exists": os.path.exists(db),
                     "rows": 0, "sql_error": "", "open_mode": "",
                     "checked_at": int(time.time())})
    if not os.path.exists(db):
        return []

    # 打开策略逐级回退（2026-10-23 实修）：
    # 库是 WAL 模式，`mode=ro` 仍需要能写 `-shm`。若进程对 `~/.workbuddy` 目录没有
    # 写权限（或被沙箱/安全软件拦），SQLite 直接抛 `unable to open database file` ——
    # 明明文件可读、stat 也成功，却一条都读不出来。
    #  ① ro              主路径，最准确（能看到 WAL 里尚未 checkpoint 的最新写入）
    #  ② ro&immutable=1  只读快照：完全不碰 -shm/-wal，代价是可能漏掉最近未合并的写入
    #  ③ 普通连接        最后一搏（不带 mode，走默认读写语义）
    uri_forms = [
        ("ro", "file:" + db.replace("\\", "/") + "?mode=ro", True),
        ("ro_immutable", "file:" + db.replace("\\", "/") + "?mode=ro&immutable=1", True),
        ("plain", db, False),
    ]
    rows = None
    errs = []
    for mode_name, target, uri in uri_forms:
        try:
            con = sqlite3.connect(target, uri=uri, timeout=5)
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
            _L1_DIAG["open_mode"] = mode_name
            break
        except Exception as e:
            # 不再静默吞掉：库被客户端写锁、schema 变更、权限不足都会落到这里，
            # 而这几种情况的修法完全不同 —— 必须让上层看得见原因。
            errs.append("%s→%s: %s" % (mode_name, type(e).__name__, e))
    if rows is None:
        _L1_DIAG["sql_error"] = " | ".join(errs)
        return []

    _L1_DIAG["rows"] = len(rows)

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
    """按 (日期, 小时) 聚合消耗（会话最后更新时间归小时），返回 [{date, hour, credit}]。

    ⚠️ 数据源是 **L1 本地会话库**（`session_usage`），按「会话最后更新时刻」归小时 ——
    一个跨小时的会话会把全部消耗记在最后一次更新的那一小时，且受本地库「包到账被误算成
    消耗」的伪增量影响。`build_dashboard_data()` **已不再用它**：小时维度统一走
    `req_hourly()` / `compute_usage_hourly()`（L5 优先、L2 兜底，按真实请求时间归集）。
    保留此函数仅为兼容可能的外部调用，新代码请勿再使用。
    """
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
# 本地数据文件的读取实况（快照 / 到账事件 / 签到历史）。
# 与 L1 同一类陷阱：这些文件解析失败时会被 `except: pass` 静默跳过，
# 页面上表现为「这段时间没有数据」—— 而真实原因是文件坏了或读不到。
# 这里把最后一次读取的真实情况记下来，由 /api/health 的 files 段对外暴露。
_FILE_DIAG = {}


def _load_json(path, default):
    key = os.path.basename(path)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            _FILE_DIAG[key] = {"path": path, "exists": True, "error": ""}
            return data
        except Exception as e:
            # 不再静默吞掉：坏文件返回默认值，但必须留下证据
            _FILE_DIAG[key] = {"path": path, "exists": True,
                               "error": "%s: %s" % (type(e).__name__, e)}
            return default
    _FILE_DIAG[key] = {"path": path, "exists": False, "error": ""}
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


# 历史出现过的「签到达成奖励额」（每日一份）。当前档位由接口 daily_credit 提供；
# 历史档位在此登记，避免「活动调整奖励额后按新档位回溯」时漏掉旧记录。
# 实测：2026-05-20 ~ 06-30 为 150/日，其余时期 100/日 —— 两档日期互不重叠，互为补集。
CHECKIN_CREDIT_ALIASES = (150.0,)


def derive_checkin_dates(raw_accounts, daily_credit):
    """从资源包记录**反推签到日期**。

    依据：签到奖励在服务端以「散包到账」形式出现（`PackageName` 为运营裂变包、
    每日一份、size 等于当日奖励额）—— 与 `build_ledger()` 识别签到奖励包的判据一致。
    `CycleStartTime` 即到账当天，取日期部分即为签到日。

    为什么需要它：签到状态接口的 `checkin_dates` **只含当前活动周期**，本地累积又从
    「第一次调用」才开始。两者都拿不到更早的历史 —— 而包记录是**服务端权威且跨设备**的，
    能恢复到「签到奖励包还在」的整个时期（实测可回溯到 2026-04-03）。

    参数 raw_accounts：L6 的原始 Accounts[]（传入时请**同时包含 valid 与 expired**，历史最全）。
    返回升序日期列表（YYYY-MM-DD）。
    """
    try:
        base = float(daily_credit or 0)
    except (TypeError, ValueError):
        base = 0.0
    wanted = {base} | set(CHECKIN_CREDIT_ALIASES)
    wanted.discard(0.0)
    if not wanted:
        return []
    out = set()
    for a in raw_accounts or []:
        size = _num(a.get("CapacitySizePrecise") or a.get("CapacitySize"))
        if not any(abs(size - w) < 1e-6 for w in wanted):
            continue
        d = (a.get("CycleStartTime") or "")[:10]
        if len(d) == 10 and d[4] == "-" and d[7] == "-":
            out.add(d)
    return sorted(out)


def compute_real_streak(dates, today=None):
    """**真实连续签到天数**（可跨活动周期）——基于本地长期累积的签到日期自算。

    为什么不能直接用接口的 `streak_days`：它统计的是**当前活动周期内**的签到天数
    （实测 `checkin_dates` 的起点与 `start_time` 完全重合，长度也等于 `streak_days`），
    活动周期一换就归零重算 —— 不等于用户实际连续签了多少天。
    本地 `checkin_history.json` 会持续累积（每次取签到状态都把当期日期并入），
    因此可以跨周期回溯。

    规则：从今天往前逐日回溯；若今天还没签，则从昨天起算（连续尚未中断）。
    返回 0 表示昨天与今天都没签（连续已断）。

    ⚠️ 局限：完全依赖本地累积 —— 换机/重装后存档为空会从 0 起算，属**低估**而非高估。
    当回溯恰好停在存档最早一天时，返回值应理解为「至少这么多天」。
    """
    if not dates:
        return 0
    have = set(dates)
    cur = datetime.strptime(today, "%Y-%m-%d") if today else datetime.now()
    if cur.strftime("%Y-%m-%d") not in have:
        cur = cur - timedelta(days=1)
    n = 0
    while cur.strftime("%Y-%m-%d") in have:
        n += 1
        cur = cur - timedelta(days=1)
    return n


def today_date():
    """当天日期字符串（YYYY-MM-DD，本地时区）—— 所有"今天"的判断请走这里。"""
    return datetime.now().strftime("%Y-%m-%d")


def real_streak_days(checkin_dates=None, extra_dates=None, today=None, merged=None):
    """**「真实连续」签到天数 —— 全项目唯一口径入口。**

    谁必须用：① 工作台「今日签到」卡（`build_dashboard_data`）；
              ② 签到前后弹框（`serve.do_checkin` 的 message / 前端未签到引导框）。
    两处若各写各的，就会出现"弹框说一个数、卡片说另一个数"的自相矛盾 ——
    2026-10-16 实测就是如此：弹框用接口 `streak_days`（**活动周期内**，周期一换归零重算），
    卡片用 `compute_real_streak`（**跨周期真实连续**），于是同屏两个"连续 N 天"对不上。

    参数：
        checkin_dates：接口（status）当期 `checkin_dates`；
        extra_dates  ：额外已知签到日期（L6 包记录反推的跨周期历史，见 `derive_checkin_dates`）；
        today        ：默认取本机当天；若"今天刚签"，请把今天写进 checkin_dates；
        merged       ：已合并好的日期全集（调用方已调过 `accumulate_checkin` 时传入，
                       避免重复读写存档）。
    """
    if merged is None:
        merged = accumulate_checkin(
            list(checkin_dates or []) + list(extra_dates or []))
    return compute_real_streak(merged, today or today_date())


def _label_by_capacity(ctype):
    """按 CapacityType 反查分类名（L6 已解析的包里没有 label，需要它兜底）。"""
    try:
        return TYPE_LABEL.get(int(ctype), "加量包/其他")
    except Exception:
        return "加量包/其他"


def accumulate_income(packages, extra=()):
    """对比 ResourceId 记录新包到账事件，返回全部到账事件。

    extra：额外的包清单，通常是 **L6 的已结束包**。L3（资源接口）只返回
    「有效期内的包」，已过期 / 已用完的包它从来没见过 —— 那些包的到账就此
    永久丢失。实测总账因此缺 2761 分，倒推余额在 9 月出现负数（2026-10-24）。
    L6 保留了已结束包（且带 CycleStartTime），所以在这里一并补录。
    """
    events = _load_json(INCOME_EVENTS_FILE, {"events": []})
    known = set(e.get("resource_id") for e in events["events"])
    changed = False
    for p in list(packages) + list(extra or ()):
        rid = p.get("resource_id")
        if not rid or rid in known:
            continue
        ev = {
            "resource_id": rid,
            "name": p.get("name") or "",
            "label": p.get("label") or _label_by_capacity(p.get("capacity_type")),
            "size": p.get("size") or 0,
            "income_time": p.get("cycle_start") or "",
        }
        # 没有到账时刻就不入库：放不上时间轴，留着只会把文件撑大、
        # 还让「到账事件数」虚高（实测 L6 的包全部不带 CycleStartTime，
        # 一次性补进来 100 条空时间条目，已清理）。等接口给出该字段再补。
        if not ev["income_time"]:
            continue
        events["events"].append(ev)
        known.add(rid)
        changed = True
    if changed:
        _save_json(INCOME_EVENTS_FILE, events)
    return events["events"]


def accumulate_suite_usage(packages, l6_rows=None):
    """把「套餐/订阅型」批次的消耗累积成本地历史（v1.7.29）。

    为什么必须单独记账 —— 两个硬约束叠在一起：

      ① L6（包明细接口）**有意过滤 CapacityType=4**，套餐那批结构上就不在里面
         （见 dev-notes「数据分层与口径」）。所以累计消耗天然漏掉套餐包的消耗。
      ② 套餐包的 `used` 只保留**本周期**，周期一换就归零。所以即使放开过滤、
         把它硬塞进 L6，看到的也只是「本月用掉多少」，历史照样拿不回来 ——
         那是假解决方案，还会让已经闭合的等式破掉（套餐额度是周期额度，
         不是永久额度，不属于同一个池子）。

    可行解只有一个：自己攒。按 `resource_id` 存「本期已用最大值」+「已结束周期的
    历史累计」，每次取数时增量推进：

      - 本期 used > 记录最大值 → 更新最大值（正常消耗中）
      - `cycle_end` 变了 → 进入新周期：上期最大值整段并入 consumed，本期重置
      - 批次从接口消失后记录保留，总量**只增不减**

    这套逻辑对服务端两种实现都成立：若每月新建 resource_id，每个 id 只出现一次
    （consumed=0、max_used=本期用量）；若同一 id 续期，则靠 cycle_end 判定换期。
    两种情况求和口径一致，所以不必猜服务端用的是哪种。

    ⚠ 只能从现在往前攒，install 之前的套餐消耗永久缺失 —— 这是数据源决定的，
      不是实现取巧。返回里的 `since` 会如实告诉用户从哪天开始有记录。

    返回 {"total", "items", "tracked", "since", "error"}
    """
    l6_ids = set(str(r.get("resource_id") or "") for r in (l6_rows or []))
    store = _load_json(SUITE_USAGE_FILE, {"version": 1, "items": {}})
    items = store.setdefault("items", {})
    now = time.strftime("%Y-%m-%dT%H:%M:%S")

    for p in packages or []:
        # 只记订阅型：普通包的 used 本身就是累计值，L6 里已经有了，
        # 在这里再记一遍就是**双计**（实测 L6 的 200 个批次全是 CapacityType=1）
        if not p.get("cycle_based"):
            continue
        rid = str(p.get("resource_id") or "")
        if not rid:
            continue
        # 双保险：万一将来 L6 覆盖到某个订阅包，以 L6 为准，这里跳过
        if rid in l6_ids:
            continue

        cur = round(float(p.get("used") or 0), 4)
        ce = str(p.get("cycle_end") or "")
        rec = items.get(rid) or {"name": "", "label": "", "size": 0.0,
                                 "consumed": 0.0, "max_used": 0.0,
                                 "cycle_end": "", "first_seen": now}
        if rec.get("cycle_end") and ce and ce != rec["cycle_end"]:
            # 换期：上一周期走到头的 max_used 是它最终的已用量，整段转历史
            rec["consumed"] = round(float(rec.get("consumed") or 0)
                                    + float(rec.get("max_used") or 0), 4)
            rec["max_used"] = cur
            rec["cycle_switch_count"] = int(rec.get("cycle_switch_count") or 0) + 1
        else:
            # 同周期内取最大值：used 只增不减，但服务端偶发回退时不能跟着退
            rec["max_used"] = max(float(rec.get("max_used") or 0), cur)

        rec["cycle_end"] = ce or rec.get("cycle_end") or ""
        rec["name"] = p.get("name") or rec.get("name") or ""
        rec["label"] = p.get("label") or rec.get("label") or ""
        rec["size"] = round(float(p.get("size") or 0), 4)
        rec["remain"] = round(float(p.get("remain") or 0), 4)
        rec["last_seen"] = now
        items[rid] = rec

    _save_json(SUITE_USAGE_FILE, store)

    detail, total = [], 0.0
    for rid, r in items.items():
        used = round(float(r.get("consumed") or 0) + float(r.get("max_used") or 0), 4)
        total += used
        detail.append({
            "resource_id": rid,
            "name": r.get("name") or "",
            "label": r.get("label") or "",
            "size": r.get("size") or 0,
            "consumed": round(float(r.get("consumed") or 0), 4),
            "current": round(float(r.get("max_used") or 0), 4),
            "used": used,
            "cycle_end": r.get("cycle_end") or "",
            "last_seen": r.get("last_seen") or "",
        })
    detail.sort(key=lambda x: (-x["used"], x["cycle_end"]))
    firsts = sorted(v.get("first_seen") or "" for v in items.values())
    return {
        "total": round(total, 4),
        "items": detail,
        "tracked": len(items),
        # 记账起算日：如实告知「从这天起才准」，避免用户以为它能覆盖全历史
        "since": firsts[0][:10] if firsts and firsts[0] else "",
        "error": "",
    }


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
                    h = None
                    try:
                        h = int(s[11:13])
                    except (ValueError, IndexError):
                        h = None
                    if h is not None and 0 <= h <= 23:   # 同 req_hourly：拒绝非法小时
                        cells[(s[:10], h)] = cells.get((s[:10], h), 0.0) + delta
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


# ---------- 余额趋势（重建 + 实测双层）----------
TREND_DAYS = 70            # 默认回溯窗口
TREND_L5_WINDOW = REQ_MAX_WINDOW_DAYS   # L5 只能稳定覆盖的窗口：窗口内「没记录」= 真没花


def build_balance_trend(current_remain, l6_expired, income_events, l5_rows,
                        snapshots=None, days=TREND_DAYS, l5_meta=None):
    """把「现在的余额」往回推成每日余额曲线。

    为什么是倒推而不是正推：L6 的包没有到账时间（`CycleStartTime` 服务端不返回），
    正推就得靠猜起点；而**余额是守恒的** —— 昨天的余额 = 今天的余额 + 今天消耗
    − 今天到账 + 今天作废，四项里其余三项都有精确记录，只有「今天的余额」需要锚点，
    而它就是当前可用剩余。倒推的唯一误差来源是**消耗记录的缺口**，那正是我们要标出来的。

    三类日期分开标注，绝不混为一条平滑曲线：
      snapshot —— 有真实快照（实测值，误差 0）
      recon    —— 由守恒式推出，且当天消耗数据在 L5 可覆盖窗口内
      gap      —— 在窗口之外、当天又没有任何记录：**不能当成「没花」**，图上断开

    返回 dict，直接给前端用。
    """
    from datetime import date as _date, timedelta as _td

    snapshots = snapshots or []
    today = _date.today()
    days = max(2, int(days or TREND_DAYS))
    day_list = [today - _td(days=days - 1 - i) for i in range(days)]
    axis = [d.isoformat() for d in day_list]

    # 三类日变动。key 一律是 YYYY-MM-DD，避免时区/格式差异导致对不上。
    consume, income, waste = {}, {}, {}
    for r in l5_rows or []:
        t = str(r.get("requestTime") or r.get("time") or "")
        if len(t) >= 10:
            consume[t[:10]] = consume.get(t[:10], 0.0) + float(r.get("credit") or 0)
    for e in income_events or []:
        t = str(e.get("income_time") or "")
        if len(t) >= 10 and not e.get("time_missing"):
            income[t[:10]] = income.get(t[:10], 0.0) + float(e.get("size") or 0)
    for p in l6_expired or []:
        t = str(p.get("expired_time") or "")
        w = float(p.get("wasted") or 0)
        if len(t) >= 10 and w > 0:
            waste[t[:10]] = waste.get(t[:10], 0.0) + w

    # 实测快照：同一天多条时取最后一条（那就是当天下班时的真实余额）
    snap_by_day = {}
    for s in snapshots:
        d = s.get("date") or str(s.get("time") or "")[:10]
        if d:
            snap_by_day[d] = float(s.get("total_remain") or 0)

    stable_from = (today - _td(days=max(1, TREND_L5_WINDOW) - 1)).isoformat()
    today_s = today.isoformat()

    # 「哪天被真正抓取覆盖过」—— 判缺口的唯一依据。
    # 不能拿「最近 31 天」当代理：L5 窗口说的是**能查到多远**，不是**查过没有**。
    # 本地只在打开工作台时才抓一次，每抓一次覆盖该时刻往前 31 天；
    # 把这些区间求并，落在并集里的日期才「有据可查」。
    # 反例：早先用「近 31 天一律可信」，结果国庆那串平坦上涨被当成缺口；
    # 而它其实是真实的 —— 假期没跑任务，每天只有签到 +100。
    fetched_days = set()
    for r in l5_rows or []:
        fa = str(r.get("fetched_at") or "")[:10]
        if len(fa) == 10:
            fetched_days.add(fa)
    covered_days = set()
    for fa in fetched_days:
        try:
            f = _date.fromisoformat(fa)
        except Exception:
            continue
        for i in range(max(1, TREND_L5_WINDOW)):
            covered_days.add((f - _td(days=i)).isoformat())

    # 本次抓取的窗口本身也是覆盖证据 —— 哪怕那几天一条记录都没有：
    # 接口的 meta 已明确 window_start ~ 今天 是查过的，查过且为空 = 真的没花。
    _ws = str((l5_meta or {}).get("window_start") or "")[:10]
    if len(_ws) == 10 and not (l5_meta or {}).get("error"):
        try:
            _d0 = _date.fromisoformat(_ws)
            _d = today
            while _d >= _d0:
                covered_days.add(_d.isoformat())
                _d -= _td(days=1)
        except Exception:
            pass

    # 倒推：先落到今天，再逐日往过去走
    vals, kinds = {}, {}
    cur = float(current_remain or 0)
    # 负余额是不可能的。一旦倒推到负数，说明那之前有**到账记录缺失**
    # （L3 只给有效期内的包，已结束包到账没被记过，实测缺 2761 分）。
    # 误差会一路往前累积，所以第一个负值出现的日期及其更早的日期全部不可信 ——
    # 截断不画，比画一条穿到地下的线诚实得多。
    cut_at = None
    for d in reversed(axis):
        if cut_at is not None:
            kinds[d] = "unreliable"
            vals[d] = None
            continue
        if d in snap_by_day:
            # 实测值优先 —— 它同时把此前累积的推算误差「截断」在这里
            cur = snap_by_day[d]
            kinds[d] = "snapshot"
        elif d > today_s:
            kinds[d] = "future"
        elif d in covered_days:
            # 那天在本地抓取覆盖范围内：没有消耗记录就是**真没花**，可以推
            kinds[d] = "recon"
        else:
            # 没被抓取覆盖过，且当天也没有任何消费记录 → 不知道花了没花。
            # ⚠ 判据只认「覆盖 + 消耗」，不认 income / waste：到账与作废来自
            # 本地累积文件，不受抓取窗口限制，它们有记录不能证明消耗数据完整。
            kinds[d] = "gap"
        if cur < -0.005:
            # 这一天已经是负数：它自己也不可信，从它开始截断
            cut_at = d
            kinds[d] = "unreliable"
            vals[d] = None
            cur = 0.0
            continue
        vals[d] = round(cur, 4)
        # 走到前一天：余额 = 今天余额 + 今天消耗 − 今天到账 + 今天作废
        cur = cur + consume.get(d, 0.0) - income.get(d, 0.0) + waste.get(d, 0.0)

    points = []
    for d in axis:
        k = kinds[d]
        points.append({
            "date": d,
            # gap / unreliable 日给 null，让折线真的断开 —— 给个延续值就等于把
            # 「没数据」画成了「没变化」，那是最容易骗人的一种平滑
            "remain": None if k in ("gap", "unreliable", "future") else vals[d],
            "kind": k,
            "consume": round(consume.get(d, 0.0), 4),
            "income": round(income.get(d, 0.0), 4),
            "waste": round(waste.get(d, 0.0), 4),
        })

    events = []
    for d, amt in income.items():
        if amt > 0 and vals.get(d) is not None:
            events.append({"date": d, "type": "income",
                           "amount": round(amt, 4), "label": "到账"})
    for d, amt in waste.items():
        if amt > 0 and vals.get(d) is not None:
            events.append({"date": d, "type": "waste",
                           "amount": round(amt, 4), "label": "过期作废"})
    events.sort(key=lambda e: (e["date"], e["type"]))

    cov = {"snapshot": 0, "recon": 0, "gap": 0, "unreliable": 0, "future": 0}
    for d in axis:
        cov[kinds[d]] = cov.get(kinds[d], 0) + 1

    first_ok = next((p["date"] for p in points if p["remain"] is not None), "")
    last_ok = next((p["date"] for p in reversed(points) if p["remain"] is not None), "")

    note = ("实测 %d 天 · 推算 %d 天" % (cov.get("snapshot", 0), cov.get("recon", 0)))
    if cov.get("gap", 0):
        note += " · 缺口 %d 天" % cov["gap"]
    if cov.get("unreliable", 0):
        note += " · 更早 %d 天推算出现负余额，已截断" % cov["unreliable"]
    note += "（缺口与截断处不连线；窗口外无记录 ≠ 没消耗）"

    return {
        "points": points,
        "events": events,
        "current": round(float(current_remain or 0), 4),
        "coverage": {
            "window_days": days,
            "snapshot_days": cov.get("snapshot", 0),
            "recon_days": cov.get("recon", 0),
            "gap_days": cov.get("gap", 0),
            "unreliable_days": cov.get("unreliable", 0),
            "first": first_ok,
            "last": last_ok,
            "cut_at": cut_at or "",
            "stable_from": stable_from,
            "l5_window_days": TREND_L5_WINDOW,
        },
        # 前端顶部直接显示这句，免得用户以为曲线全程都是实测
        "note": note,
    }



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
        # 「连续」统一走 real_streak_days()（真实连续、跨活动周期），
        # 与工作台卡片和签到弹框同口径 —— 别再用接口的当期 streak_days。
        print("今日已签到，无需重复。已连续签到 %s 天，今日 +%s 积分。"
              % (real_streak_days(st.get("checkin_dates")),
                 st.get("daily_credit", "?")))
        return 0
    data, err = api_daily_checkin(token, uid)
    if err:
        print("签到失败：", err)
        return 1
    today = today_date()
    print("签到成功：本次 +%s 积分 | 已连续签到 %s 天"
          % (data.get("credit", "?"), real_streak_days([today], today=today)))
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
    L.append("【签到效率】每日可得 %s 积分 | 已连续签到 %s 天 | 今日已签：%s | 活动：%s"
            % (st.get("daily_credit", "?"), real_streak_days(st.get("checkin_dates")),
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


def fetch_sources(token, uid, sync_days=None, hub=None, fresh=()):
    """并发拉取四个服务端数据源，返回 {key: (payload, err)}。

    hub 非空时走进程内缓存 + single-flight（常驻服务用，见 serve.py 的 SourceHub）；
    为 None 时直接并发拉取（CLI 一次性调用）。

    键与形状：
      resource -> accounts（list）      错误为硬失败，外层据此整体返回错误
      checkin  -> st（dict）
      l5       -> {"rows": [...], "meta": {...}}
      l6       -> {"lists": {...}, "meta": {...}}

    fresh：需要**强制重取**的源键集合（其余走缓存）。面板级刷新靠它实现
    ——只刷新自己那一两个源，而不是把四路全部重打。
    """
    want = sync_days if sync_days is not None else REQ_MAX_WINDOW_DAYS

    def _resource():
        return api_get_resource(token, uid)

    def _checkin():
        return api_checkin_status(token, uid)

    def _l5():
        rows, meta, err = sync_requests(token, uid, want)
        return {"rows": rows, "meta": meta}, err

    def _l6():
        lists, meta, err = api_packages_both(token, uid)
        return {"lists": lists, "meta": meta}, err

    loaders = {"resource": _resource, "checkin": _checkin, "l5": _l5, "l6": _l6}
    if hub is None:
        tasks = list(loaders.items())
    else:
        tasks = [(k, (lambda kk=k, f=loaders[k]: hub.get(kk, f, fresh=(kk in fresh))))
                 for k in loaders]

    raw = _run_parallel(tasks)
    out = {}
    for key, (value, exc) in raw.items():
        if exc is not None:
            out[key] = (None, str(exc))
        elif value is None:
            out[key] = (None, "源未返回数据")
        else:
            out[key] = value
    return out


# ---------- 面板切片（供 serve.py 的分面板接口）----------
# 「每个面板独立异步请求 + 局部刷新」的后端配套：每个面板只返回自己渲染需要的键，
# 把「概览」从 248 行请求流水 + 140 行包明细里解放出来（首屏不必等大表序列化），
# 也让每个面板能独立刷新、独立失败，而不是一处慢就整页白屏。
_PANEL_KEYS = {
    # 概览：账号 / KPI 卡 / 摘要 / 收支统计 / 图表 / 账本 / 会话 / 批次 / 签到 / 健康
    "overview": (
        "version", "generated_at", "nickname", "account_type", "auth_status",
        "plan_tier",       # 套餐档位（Free/Pro/Plus/Max），从订阅型包 PackageName 映射
        "browser_channel", # 取数浏览器偏好（auto/chrome/msedge）
        "summary", "waste", "waste_authoritative",
        "total_remain", "avail_count", "total_used", "today_used",
        # 累计消耗三口径（见 build_dashboard_data 里的注释）：卡片用 total_used_all，
        # 「按客户端」的覆盖率提示用 total_used_flow / flow_cover
        "total_used_all", "total_used_items", "total_used_scope", "l6_gap",
        # 套餐/订阅型批次：L6 不含、used 每周期归零，靠本地记账累积
        "total_used_suite", "total_used_grand", "suite_usage",
        "total_used_flow", "flow_cover",
        "today_used_l5", "today_used_l2", "usage_source", "days_left", "daily_avg",
        "active_days_30d", "daily_avg_window",
        "checkin", "usage_daily", "income_daily", "ledger", "usage_sessions",
        "expiry_list", "capacity_note", "sources_health",
        "holidays",        # 到期提醒要按工作日顺延，前端需要这份日历
        "holiday_meta",    # 该日历覆盖到哪一年（前端据此提醒「该更新次年数据了」）
    ),
    # 消耗明细：请求级大表 + 三项构成 + 对账；顺带回带 KPI 卡依赖的 L5 口径字段
    "requests": (
        "version", "generated_at",
        "requests", "requests_meta",
        "model_breakdown", "client_breakdown", "purpose_breakdown",
        "reconcile", "usage_daily", "usage_source",
        "today_used", "today_used_l5", "today_used_l2",
        # 「按客户端」区块要写清「这只是能追溯到请求的那部分」—— 必须有占比与天数
        "total_used_all", "total_used_items", "total_used_flow", "flow_cover",
        "sources_health",
    ),
    # 积分批次与生命周期：待用清单（L3 批次）+ 两张 L6 大表 + 权威浪费
    # expiry_list / holidays：面板已合并，待用清单页签的数据与假期标记必须随该面板一起来，
    # 否则面板先到、概览后到时「待用清单」会空着（早期就踩过这种跨面板依赖）。
    "lifecycle": (
        "version", "generated_at",
        "packages_lifecycle", "waste_authoritative",
        "expiry_list", "holidays", "holiday_meta",
        "sources_health",
    ),
    # 余额趋势：实测快照 + 守恒倒推。只带这一块，避免把概览的账本/图表一起拖来
    "balance": (
        "version", "generated_at", "balance_trend",
        "sources_health",
    ),
}

# 各面板「自己拥有」的源：局部刷新时只强制重取这些源，其余沿用缓存。
# 概览拥有 L3/L4；L5、L6 分别由消耗明细、包生命周期拥有 —— 点哪个面板刷新哪个源。
PANEL_FRESH_KEYS = {
    "overview": ("resource", "checkin"),
    "requests": ("l5",),
    # 该面板现在同时展示「待用清单（L3）」与两张 L6 表 —— 刷新它要两个源一起取
    "lifecycle": ("l6", "resource"),
    # 趋势要用到三边：当前余额（L3）、日消耗（L5）、作废事件（L6）
    "balance": ("l5", "l6", "resource"),
}


def build_panel(panel, token, uid, account, hub=None, fresh=()):
    """构建单个面板的 payload。返回 (data, err)。

    降级语义与 build_dashboard_data 一致：某源不可用时该块为空 + 健康徽章标红，
    其余面板照常渲染。
    """
    if panel not in _PANEL_KEYS:
        return None, "未知面板：%s" % panel
    data, err = build_dashboard_data(token, uid, account, hub=hub, fresh=fresh)
    if err:
        return None, err
    out = {k: data[k] for k in _PANEL_KEYS[panel] if k in data}
    out["panel"] = panel
    return out, None


def build_dashboard_data(token, uid, account, sync_days=None, hub=None, fresh=()):
    """构建工作台数据 dict。返回 (data, err)。供 render 命令与 serve.py 复用。

    分层口径（2026-09-10 重构）：
      权威层 L5 请求流水 / L6 包生命周期 —— 主导消耗与浪费展示
      观测层 L2 逐包采样 / L1 本地会话库 —— 骨架、预测、**对账基准**
      L4 签到 —— 独立
    任一新增源失败**不连累整页**：降级为该块为空 + 健康徽章标红，其余照常渲染。

    性能（2026-09-13）：四源改为**并发**拉取。首屏耗时从「四路之和」（实测 3.84s）
    降为「最慢的一路」（实测 ≈0.8s）；纯装配 CPU 仅 0.03s，不是瓶颈。
    hub/fresh 供常驻服务复用缓存与做面板级局部刷新。
    """
    # 昵称 / 角色：桥模式下凭据不在本机，调用方传来的 account 是占位值 ——
    # 从站点接口 /console/account 取真实信息（失败不连累整页，退回占位）。
    if bridge_enabled() and (not account or account.get("nickname") in (None, "", "浏览器会话")):
        info, _aerr = api_account_info()
        if info:
            account = dict(info)

    src = fetch_sources(token, uid, sync_days, hub=hub, fresh=fresh)

    accounts, res_err = src.get("resource") or (None, "资源接口未返回")
    if res_err or not accounts:
        return None, res_err or "积分资源接口返回为空"

    packages = parse_accounts(accounts)
    groups = summarize(packages)
    waste = compute_waste(packages)
    snaps = load_snapshots()
    balance, net_change = build_timeline(snaps)

    st, _ck_err = src.get("checkin") or (None, None)
    st = st or {}

    # ===== 余额快照：每成功取一次数记一条（同一分钟去重）=====
    # 这一段是「余额趋势」的数据地基。在此之前 record_snapshot() 只被 CLI 的 record
    # 命令调用，工作台从来没写过 —— snapshots.jsonl 一直不存在，build_timeline()
    # 拿到空数组、余额时间线永远是空的（2026-10-24 补上）。
    # 写失败**不能连累整页**：快照是旁路记录，主数据已经拿到了。
    try:
        _snap = record_snapshot(packages, st or None)
        if _snap is not None:
            snaps = load_snapshots()          # 刚写进去的这条也要进时间线
            balance, net_change = build_timeline(snaps)
    except Exception as _se:
        _FILE_DIAG.setdefault("snapshots.jsonl", {})["write_error"] = \
            "%s: %s" % (type(_se).__name__, _se)

    # ===== 权威层 L5：请求流水（并发已就绪；失败不影响整页）=====
    l5_rows, l5_meta = [], None
    l5_payload, l5_err = src.get("l5") or (None, "L5 未拉取")
    if l5_err or not l5_payload:
        l5_rows = []
        l5_meta = {"error": l5_err or "L5 未返回数据"}
    else:
        l5_rows = l5_payload.get("rows") or []
        l5_meta = l5_payload.get("meta") or {}

    # ===== 权威层 L6：包生命周期（失败不影响整页）=====
    l6_payload, l6_err = src.get("l6") or (None, "L6 未拉取")
    if not l6_payload:
        l6_lists = {"valid": [], "expired": []}
        l6_meta = {"partial_errors": [l6_err or "L6 未返回数据"]}
    else:
        l6_lists = l6_payload.get("lists") or {"valid": [], "expired": []}
        l6_meta = dict(l6_payload.get("meta") or {})
        if l6_err:
            l6_meta["partial_errors"] = list(l6_meta.get("partial_errors") or []) + [str(l6_err)]
    l6_valid = parse_pkg_lifecycle((l6_lists or {}).get("valid"), "valid")
    l6_expired = parse_pkg_lifecycle((l6_lists or {}).get("expired"), "expired")
    waste_auth = compute_waste_authoritative(l6_expired)

    # 权威浪费的**可信度戳**（2026-10-23 补）：
    # compute_waste_authoritative([]) 返回的是一个「字段齐全但全零」的对象。
    # 前端拿它渲染 KPI，就会把「包明细接口没读到」显示成
    # 「已过期损失 0.00 · 已过期未用完 0 批 · 服务端权威」—— 比空表更糟：
    # 它带着权威的口吻给出了一个假的零。数字本身分不出「真为零」还是「没算」，
    # 所以必须在这里显式标注这一份统计到底有没有算过。
    _emeta = (l6_meta.get("expired") or {}) if isinstance(l6_meta, dict) else {}
    _expired_ok = (bool(_emeta) and _emeta.get("shown") is not None
                   and not (_emeta.get("partial_errors") or []))
    waste_auth["degraded"] = not _expired_ok
    if _expired_ok:
        waste_auth["error"] = ""
    else:
        _werrs = list(_emeta.get("partial_errors") or []) \
                 + list(((l6_meta or {}).get("partial_errors") or []))
        if l6_err:
            _werrs.append(l6_err)
        waste_auth["error"] = ("；".join(str(e)[:120] for e in _werrs)
                               or "包明细接口未返回「已过期」清单")

    # 到期批次（含已过期，按到期时间排序）
    ordered = sorted(packages, key=lambda x: (x["cycle_end"] == "", x["cycle_end"] or ""))
    expiry_list = [
        {"label": p["label"], "name": p["name"], "remain": p["remain"],
         "size": p["size"], "used": p["used"], "cycle_end": p["cycle_end"],
         "cycle_based": p.get("cycle_based", False),
         "diverged": p.get("diverged", False),
         "remain_cum": p.get("remain_cum"),
         # exp_ts：到期毫秒时间戳。前端「积分批次」表据此标「临近到期」——
         # 与 compute_waste 的 at_risk **用同一个字段**，保证两处口径不会漂移。
         "exp_ts": p["exp_ts"],
         "status": "可用" if p["available"] else ("已过期" if p["expired"] else "已用完")}
        for p in ordered
    ]

    # 口径说明：订阅型（套餐用量）改用本周期口径后，若某些包两组口径不一致，
    # 这里给出「若改用累计口径会多算多少」—— 便于页面对账与自证。
    diverged_pkgs = [p for p in packages if p.get("diverged")]
    capacity_note = {
        "cycle_based_count": sum(1 for p in packages if p.get("cycle_based")),
        "diverged_count": len(diverged_pkgs),
        "delta_if_cumulative": round(sum(
            (_num(p.get("remain_cum")) - p["remain"]) for p in diverged_pkgs), 4),
    }

    # 套餐档位：取订阅型包（套餐用量）的 PackageName 映射到 Free/Pro/Plus/Max。
    plan_tier_val = ""
    for p in packages:
        if p.get("cycle_based"):
            plan_tier_val = plan_tier(p.get("name", ""))
            if plan_tier_val:
                break

    # ---------- 累计消耗：三个口径，用途不同，禁止混用 ----------
    # 2026-10-25 校准。起因：页面「累计消耗」卡显示 6240.37，「按客户端」合计 7125.06，
    # 两者差 884.69，用户无法判断哪个是准的 —— 因为**两个都不是累计消耗**：
    #
    #   total_used       仅「当前仍有效」批次（L3 视角，40 个包）的已用。
    #                    已结束的 160 个包不计入 → 拿它当累计会漏掉 2/3。
    #                    它是「当前手上这批还能用多久」的分母，不是历史总消耗。
    #   total_used_flow  只有「有请求流水」那 35 天能拆到每次调用的部分。
    #                    L5 只保 31 天窗口，更早的调用永远拉不回来 → 天生不全。
    #                    它只能用来做构成拆分（按模型/客户端/用途），不能当总量。
    #   total_used_all   全部批次（有效 + 已结束）的已用合计 = **真正的累计消耗**。
    #                    满足闭合式：总容量 = 已用 + 可用余额 + 过期作废
    #                    （实测 30311.00 = 19014.10 + 3679.56 + 7617.34，误差 0）。
    #
    # 优先取 L6（含已结束包，且两表同一次取数、口径自洽）；L6 不可用时退回 L3 并标注。
    l6_used_valid = round(sum(_num(p.get("used")) for p in l6_valid), 4)
    l6_used_expired = round(sum(_num(p.get("used")) for p in l6_expired), 4)
    total_used = round(sum(p["used"] for p in packages), 4)
    if l6_valid or l6_expired:
        total_used_all = round(l6_used_valid + l6_used_expired, 4)
        total_used_scope = "L6"          # 全批次视角，含已结束
    else:
        total_used_all = total_used
        total_used_scope = "L3"          # 降级：只有有效批次，累计被低估
    # 「已结束」不是「作废」—— 这个歧义会让用户以为已结束的批次全是浪费。
    # 实测 160 个已结束批次里，98 个是**用光**了才结束（浪费 0），
    # 只有 62 个是**没用完就过期**（这部分才有作废）。拆开给前端解释清楚。
    _exp_used_up = [p for p in l6_expired if _num(p.get("remain")) <= 0.0001]
    _exp_wasted = [p for p in l6_expired if _num(p.get("remain")) > 0.0001]
    total_used_items = {
        "valid": l6_used_valid if l6_valid else total_used,
        "expired": l6_used_expired,
        "valid_count": len(l6_valid),
        "expired_count": len(l6_expired),
        # 已结束批次的两种结束方式（供悬浮分解说明）
        "expired_used_up_count": len(_exp_used_up),
        "expired_used_up_used": round(sum(_num(p.get("used")) for p in _exp_used_up), 4),
        "expired_wasted_count": len(_exp_wasted),
        "expired_wasted_used": round(sum(_num(p.get("used")) for p in _exp_wasted), 4),
        "expired_wasted_amount": round(sum(_num(p.get("remain")) for p in _exp_wasted), 4),
        # 闭合式的另外两项也取 L6 —— 必须同源。
        # 混用 L3 的 total_remain 会凭空多出 500：L3 有「CodeBuddy个人体验版」
        # （500 分、月底到期，CapacityType=4 套餐用量），而 L6 **有意过滤 CapacityType=4**，
        # 套餐那批结构上就不在它的覆盖里，两套数一减就差 500。
        # 结论：L3 余额含体验版（首页该显示它），但闭合只能在同一数据源内做。
        "remain": round(sum(_num(p.get("remain")) for p in l6_valid), 4),
        "wasted": round(sum(_num(p.get("remain")) for p in l6_expired), 4),
        "capacity_valid": round(sum(_num(p.get("size")) for p in l6_valid), 4),
        "capacity_expired": round(sum(_num(p.get("size")) for p in l6_expired), 4),
        "total_capacity": round(sum(_num(p.get("size")) for p in l6_valid)
                                + sum(_num(p.get("size")) for p in l6_expired), 4),
    }
    # L3 与 L6 的余额差（实测 = 体验版包 500）。>0 表示有批次不在包明细接口内，
    # 闭合式要用 items 自己的 remain，这个差值单独说明，不能让用户自己去发现。
    total_remain_l3 = round(sum(g["remain_sum"] for g in groups), 4)
    l6_gap = {
        "diff": round(total_remain_l3 - total_used_items["remain"], 4),
        "l3_remain": total_remain_l3,
        "l6_remain": total_used_items["remain"],
    }
    # 套餐/订阅型批次的消耗：L6 结构上不含（CapacityType=4 被有意过滤），
    # 且它的 used 每周期归零 → 只能本地按 resource_id 攒。见 accumulate_suite_usage。
    suite = accumulate_suite_usage(packages, list(l6_valid) + list(l6_expired))
    total_used_suite = suite["total"]
    # 用户问的「实际用掉多少」= L6 全部批次已用 + 套餐本地记账。
    # 只有前者参与闭合式（套餐是周期额度，不属于同一个池子）。
    total_used_grand = round(total_used_all + total_used_suite, 4)
    # 「可追溯到单次调用」的部分 = L5 归档流水合计。它天生不全（窗口 31 天），
    # 所以必须连同「覆盖几天、占累计多少」一起给出，否则用户会把流水合计当总量 ——
    # 这正是 7125.06 vs 6240.37 那次困惑的直接来源。
    total_used_flow = round(sum(_num(r.get("credit")) for r in (l5_rows or [])), 4)
    _flow_days = sorted(set(str(r.get("requestTime") or "")[:10]
                            for r in (l5_rows or []) if r.get("requestTime")))
    flow_cover = {
        "days": len(_flow_days),
        "first": _flow_days[0] if _flow_days else "",
        "last": _flow_days[-1] if _flow_days else "",
        "credit": total_used_flow,
        "pct": round(total_used_flow / total_used_all * 100, 1) if total_used_all else 0.0,
        # 无法追溯的部分：只有包级记账，拆不出模型/客户端
        "untraceable": round(total_used_all - total_used_flow, 4),
    }
    today_str = today_date()

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

    # 小时维度热度数据：与 usage_hourly **同源同构**（L5 优先、L2 兜底）。
    # 早先用 usage_heatmap(usage)（L1 本地会话库，按「会话最后更新时间」归小时），
    # 与 usage_daily / usage_hourly 的口径不一致，且受本地库伪增量影响 —— 已废弃。
    heatmap = uhourly

    # ---- 自积累账本（签到合并 + 包到账对比 ResourceId）----
    # 签到日期三源合并（由近及远）：① 接口当期 checkin_dates ② 本地累积存档
    # ③ 从资源包反推 —— ③ 是关键：接口只给当期，本地累积从首次调用才开始，
    #    只有服务端的包记录能跨设备回溯更早的历史（实测可到 2026-04-03）。
    daily_credit_val = st.get("daily_credit", 0) or 0
    checkin_dates_raw = st.get("checkin_dates", [])
    pkg_checkin = derive_checkin_dates(
        (l6_lists.get("valid") or []) + (l6_lists.get("expired") or []), daily_credit_val)
    accumulated_checkin = accumulate_checkin(
        list(checkin_dates_raw) + list(pkg_checkin))
    # 真实连续签到天数（跨活动周期）：接口的 streak_days 只算当期、换周期即归零重算，
    # 故用合并后的签到日期自算，供「今日签到」卡展示。
    # 走 real_streak_days() 是刻意的 —— 签到弹框用同一个入口，两处口径才不会漂。
    real_streak = real_streak_days(checkin_dates_raw, pkg_checkin, today_str,
                                   merged=accumulated_checkin)
    # extra 传 L6 两张表：已经结束的包 L3 从来看不见，到账只能靠这里补
    income_events = accumulate_income(packages, extra=list(l6_valid) + list(l6_expired))
    # 余额趋势：实测快照 + 守恒倒推（口径与「缺口不连线」的取舍见 build_balance_trend）
    balance_trend = build_balance_trend(
        round(sum(g["remain_sum"] for g in groups), 4),
        l6_expired, income_events, l5_rows, snapshots=snaps, l5_meta=l5_meta)
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

    # ---- 预测：近 N 天日均消耗 + 预计可用天数 ----
    # 用统一的 udaily（L5 优先、L2 兜底）而非本地库：L5 覆盖 30+ 天且免疫
    # 「包到账被误算成消耗」的伪增量（见 compute_usage_daily 的已知缺陷说明）。
    # 同时剔除今日（未完天）以免日均被低估。
    hist_days = [d["credit"] for d in udaily if d["date"] < today_str]
    window = get_avg_window()
    recent = hist_days[-window:]
    daily_avg = round(sum(recent) / len(recent), 4) if recent else 0.0
    active_days = sum(1 for c in recent if c > 0)   # 有消耗的天数（解释日均为何偏低）
    total_remain_val = round(sum(g["remain_sum"] for g in groups), 4)
    # 日均过低时**不予估算**：见 DAYS_LEFT_MIN_AVG 的说明。页面此时显示 —
    # 并在副标题如实给出「仅 N 天有消耗」，让用户明白是样本稀而非数据缺失。
    days_left = (round(total_remain_val / daily_avg, 1)
                 if daily_avg >= DAYS_LEFT_MIN_AVG else None)

    # ---- 建议（规则引擎）----
    advice = []
    if waste["at_risk_count"] > 0:
        advice.append("你有 %d 个批次将在 %d 天内到期（合计 %.2f 积分），建议优先消耗"
                      % (waste["at_risk_count"], waste["soon_days"], waste["at_risk"]))
    if days_left is not None:
        advice.append("按近 %d 天日均消耗 %.2f 积分，剩余积分约可用 %.1f 天"
                      % (window, daily_avg, days_left))
    advice.append("坚持每日签到（每日 +%s 积分），每月约可补 %d 积分"
                  % (daily_credit_val, int(float(daily_credit_val) * 30)))
    if waste["wasted"] > 0:
        advice.append("已有 %.2f 积分因过期浪费，建议及时使用临期积分" % waste["wasted"])

    # ---- 使用摘要（一段综合文字）----
    summary_parts = []
    if daily_avg > 0:
        summary_parts.append("近 %d 天日均消耗 %.1f 积分" % (window, daily_avg))
    if days_left is not None:
        summary_parts.append("按当前节奏，剩余积分约可用 %.1f 天" % days_left)
    if waste["at_risk_count"] > 0:
        summary_parts.append("%d 个批次 %d 天内到期（%.2f 积分）"
                             % (waste["at_risk_count"], waste["soon_days"], waste["at_risk"]))
    summary_parts.append("连续签到 %d 天，每日 +%s 积分"
                         % (real_streak, daily_credit_val))
    summary = "，".join(summary_parts) + "。"

    # 今日已使用：直接沿用上面「L5 优先、L2 兜底」的结果（与 usage_source 标注一致）。
    #
    # ⚠️ 不要在这里再调 compute_today_used() 覆盖 —— 曾经就是这么写的，导致：
    #   ① 口径错位：KPI 显示的是 L2 采样值，副标题却按 usage_source 标成「服务端流水口径」
    #   ② 系统性低估：L2 逐包差值只统计「今日**首个采样点**之后」的增量，而采样点由页面
    #      刷新/服务请求产生 —— 当天第一次打开工作台之前的消耗没有基准，全被漏掉。
    #      实测 2026-09-16：L5 说 9.66，L2 只算出 0.74（首个采样点 13:12，此前消耗无基准）。
    #   ③ L5 才是服务端流水、精确到请求，本就是既定主口径（见 usage_source 与对账设计）。

    data = {
        "version": VERSION,                 # 技能版本（来自 manifest.yaml，工作台页头展示）
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "nickname": account.get("nickname", "-"),
        "account_type": account.get("type", ""),
        "plan_tier": plan_tier_val,         # 套餐档位（Free/Pro/Plus/Max），识别不出为空
        "browser_channel": get_browser_channel(),   # 取数浏览器偏好（auto/chrome/msedge）
        "groups": [
            {"label": g["label"], "avail_count": g["avail_count"],
             "remain_sum": g["remain_sum"], "total_count": g["total_count"]}
            for g in groups
        ],
        "total_remain": round(sum(g["remain_sum"] for g in groups), 4),
        "capacity_note": capacity_note,     # 订阅型口径切换的说明（含"若用累计会多算多少"）
        "total_used": total_used,          # 仅当前有效批次（L3 视角）
        "total_used_all": total_used_all,  # 全部批次（含已结束）= 真正的累计消耗
        "total_used_items": total_used_items,
        "total_used_scope": total_used_scope,
        "l6_gap": l6_gap,                  # L3 余额与 L6 的差（体验版等不在明细里的批次）
        "total_used_flow": total_used_flow,  # 可追溯到单次调用的部分（天生不全）
        "flow_cover": flow_cover,
        "total_used_suite": total_used_suite,   # 套餐/订阅型（本地记账）
        "total_used_grand": total_used_grand,   # L6 已用 + 套餐 = 真正实际用掉
        "suite_usage": suite,
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
        "active_days_30d": active_days,
        "daily_avg_window": window,
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
            # streak_days：**活动周期内**的签到天数（接口口径，周期切换即归零重算）
            "streak_days": st.get("streak_days", 0),
            # real_streak_days：**真实连续**签到天数（跨周期，由本地累积自算）
            "real_streak_days": real_streak,
            "local_history_days": len(accumulated_checkin),
            "daily_credit": st.get("daily_credit", 0),
            "activity_name": st.get("activity_name", ""),
            "theme_name": st.get("theme_name", ""),
            "checkin_dates": st.get("checkin_dates", []),
            "week_progress": st.get("week_progress", []),
            "start_time": st.get("start_time", ""),
            "end_time": st.get("end_time", ""),
        },
        "balance_timeline": balance,
        "balance_trend": balance_trend,
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
        # 法定节假日 / 调休表：供前端把「到期提醒」顺延到用户会打开工作台的日子。
        # 纯数据下发，判断逻辑在前端（提醒本来就是"打开工作台"这个前端行为触发的）。
        "holidays": load_holiday_calendar(),
        # 这份表自身「够不够用」——到点提醒用户更新次年数据（漏更新会静默降级）
        "holiday_meta": holiday_data_status(),
    }
    return data, None


# ---------- 版本检查（工作台提示「有新版本」）----------
# 为什么查在**服务端**而不是浏览器：① 浏览器直打 api.github.com 会受 CORS 与代理影响；
# ② 结果缓存到数据目录，多页面 / 多刷新只联网一次。
# 为什么只提示、不提供「一键更新」按钮：**改本地文件必须由用户显式触发** ——
# 升级动作是 `python update.py`（Release 包 + sha256 校验 + 自动重启服务）。
# 这里的职责仅是「让用户知道有新版本」，所以它永远不改任何本地文件。
UPDATE_REPO = "https://github.com/SkylerCook/workbuddy-credits-ws"   # 与 update.py 的 DEFAULT_REPO 一致
UPDATE_CHECK_FILE = os.path.join(DATA_DIR, "update_check.json")
UPDATE_CHECK_TTL = 24 * 3600      # 检查成功：24 小时内不重复联网（有「检查更新」按钮可主动绕过）
# 为什么从 12h 缩到 1h（2026-09-26）：12h 窗口意味着「新版本发布后，用户最多 12 小时
# 看不到提示」——发布前刚检查过一次（结论 same）就会被缓存住。GitHub 匿名限流 60 次/时，
# TTL=1h 每小时最多联网 1 次，远在预算内；发现延迟从 12h 降到 1h。
UPDATE_CHECK_FAIL_TTL = 1800      # 检查失败：30 分钟内不重试（避免每次开页面都卡网）

_update_lock = threading.Lock()
_update_checking = False


def _read_update_cache():
    try:
        with open(UPDATE_CHECK_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_update_cache(status):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = UPDATE_CHECK_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
        os.replace(tmp, UPDATE_CHECK_FILE)
    except Exception:
        pass


def refresh_update_status():
    """联网检查一次并写缓存。**任何异常都转成 state='error'**（绝不向上抛）。

    取数与版本比较直接复用 `update.py`（`latest_release` / `cmp_version`）——
    否则会出现两套版本比较逻辑，早晚对不上。
    """
    st = {"checked_at": int(time.time()), "local": VERSION}
    try:
        import update as upd
        rel = upd.latest_release(UPDATE_REPO)
        state = upd.cmp_version(VERSION, rel.get("version") or "")
        st.update({
            "state": state if state in ("newer", "same", "older") else "unknown",
            "remote": (rel.get("version") or "").lstrip("vV"),
            "tag": rel.get("tag") or "",
            "published": rel.get("published") or "",
            "url": rel.get("html_url") or "",
            "notes": (rel.get("notes") or "")[:2000],
        })
    except Exception as e:
        st.update(state="error", reason="%s: %s" % (type(e).__name__, e))
    _write_update_cache(st)
    return st


def _update_cache_stale(st):
    ts = st.get("checked_at") or 0
    ttl = UPDATE_CHECK_FAIL_TTL if st.get("state") == "error" else UPDATE_CHECK_TTL
    return (time.time() - ts) > ttl


def _rebase_update_cache(st):
    """用**当前进程版本**重新校准缓存的 local/state。

    升级后服务会重启：进程 VERSION 已是新版本，但 `update_check.json` 里的 local
    还是旧进程写的旧值、state 停在 'newer' —— 直接返回会让前端在「已升级」后仍
    显示「有新版本」。故每次读缓存后用 VERSION 重算 state（本地比较，不联网）。
    """
    if not st or st.get("state") == "error":
        return st
    remote = st.get("remote") or ""
    if remote:
        try:
            import update as upd
            state = upd.cmp_version(VERSION, remote)
        except Exception:
            return st
        st = dict(st)
        st["local"] = VERSION
        st["state"] = state if state in ("newer", "same", "older") else "unknown"
    return st


def update_status(trigger=True):
    """版本状态。**立即返回**（可能来自缓存）；缓存过期时在**后台线程**刷新。

    关键约束：**联网绝不发生在请求线程里** —— 否则一次 GitHub 不通就会让页面卡住。
    本次先给缓存（可能是空的），下次访问就有新结果；服务启动时也会预热一次。
    """
    global _update_checking
    st = _rebase_update_cache(_read_update_cache())
    if trigger and _update_cache_stale(st):
        with _update_lock:
            if not _update_checking:
                _update_checking = True

                def _bg():
                    global _update_checking
                    try:
                        refresh_update_status()
                    finally:
                        _update_checking = False

                threading.Thread(target=_bg, name="wb-update-check", daemon=True).start()
    return st


def force_update_check():
    """强制后台联网检查（**绕过缓存 TTL**）。返回 True 表示已触发刷新。

    用户点「检查更新」时调用 —— 区别于 `update_status` 的「缓存过期才刷」。
    联网仍在后台线程（约束不变），绝不在请求线程里发生。
    """
    global _update_checking
    with _update_lock:
        if _update_checking:
            return False   # 已有检查在进行，别重复触发
        _update_checking = True

        def _bg():
            global _update_checking
            try:
                refresh_update_status()
            finally:
                _update_checking = False

        threading.Thread(target=_bg, name="wb-update-force", daemon=True).start()
    return True


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
        # 用正则而非固定串：静态标签带 async 等属性时也要能被认出来，
        # 否则替换失败会静默产出一个「依赖外部 data.js」的假单文件（分享出去打不开）。
        inline_html, n_sub = re.subn(
            r'<script src="dashboard_data\.js"[^>]*></script>',
            lambda _m: '<script>\n' + data_js + '\n</script>',
            html_content, count=1)
        if n_sub != 1:
            raise RuntimeError("未在 dashboard.html 中找到 dashboard_data.js 脚本标签（替换 0 处）")
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
    for rid, r in fresh_by_id.items():
        # 本次抓到的行要保留 input（仅内存），但 fetched_at 必须回填：
        # 归档行有它、本次行原本没有。而 fetched_at 是「这次抓取覆盖了哪段时间」
        # 的唯一证据 —— 余额趋势靠它区分「真没花」和「没抓到」。
        # 早先直接 display.update(fresh_by_id)，本次抓到的行全丢了 fetched_at，
        # 于是 31 天窗口内的历史突然变成「没抓过」，曲线被切成一串假缺口（2026-10-24 修）。
        rec = dict(r)
        rec["fetched_at"] = (merged_store.get(rid) or {}).get("fetched_at") or fetched_at
        display[rid] = rec
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
