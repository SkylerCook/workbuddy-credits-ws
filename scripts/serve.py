#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
serve.py —— 积分工作台本地服务：提供可刷新的工作台。

用法：
  python serve.py [端口]          # 默认 8090
  浏览器打开 http://127.0.0.1:8090

接口：
  GET /                     工作台页面
  GET /api/version          版本号（瞬时，不打上游）
  GET /api/update           是否有新版本（**只读缓存**；联网在后台线程，缓存 1h）
  GET /api/overview         概览：KPI / 图表 / 账本 / 会话 / 批次 / 签到
  GET /api/requests         消耗明细：请求级大表 + 构成 + 对账
  GET /api/lifecycle        包生命周期：有效期内 / 已过期 + 权威浪费
  GET /api/data             全量（兼容旧调用方与静态导出）
  GET /api/checkin          执行签到（幂等）
  GET /api/relogin          会话过期时弹出浏览器窗口重新登录（最长阻塞约 5 分钟）
  GET /api/create-shortcut  创建桌面 / 开始菜单快捷方式
  GET /dashboard_data.js    静态数据（**已剥离提示词**，供 file:// 模式）

性能设计（2026-09-13）：
  首屏慢的根因是「四路服务端数据串行往返」（实测 3.84s，纯 CPU 仅 0.03s）。
  两道改造把它降到 ≈0.85s：
    1. 四源并发拉取（见 workbuddy_credits.fetch_sources）
    2. 进程内 SourceHub：TTL 缓存 + single-flight，页面同时发四个面板请求时
       同一个源只会真正打一次上游，而不是三份
  ?fresh=1 强制重取（面板级刷新只重取该面板拥有的源，见 PANEL_FRESH_KEYS）。

安全：仅监听 127.0.0.1，数据不出本机。提示词只存在于 /api/* 的实时响应中，
      任何落盘通道（dashboard_data.js / render 产物）都必须先过 strip_prompts()。
"""

import atexit
import json
import os
import signal
import subprocess
import sys
import time
import threading
import http.server
import socketserver
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import workbuddy_credits as wc  # noqa: E402
import auto_checkin as ac  # noqa: E402  —— 自动签到（计划任务）状态与开关
from shortcut import create_shortcut  # noqa: E402

ASSETS_DIR = os.path.join(SKILL_DIR, "assets")
DASHBOARD_HTML = os.path.join(SKILL_DIR, "dashboard.html")

# 源缓存存活秒数。只需覆盖「同一次页面加载的并发扇出」与随手的连击刷新；
# 刻意取小值，避免用户按 F5 后看到明显陈旧的数据。
CACHE_TTL = 10.0

# 静态资源 MIME 映射（缺省走 application/octet-stream，避免被误判成 JS）
MIME = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


def _mime_for(path):
    _, ext = os.path.splitext(path.lower())
    return MIME.get(ext, "application/octet-stream")


class SourceHub:
    """四个服务端数据源的进程内缓存 + single-flight。

    为什么需要：页面首屏会**同时**发 3 个面板请求，而四个面板共用底层数据源
    （概览要 L3/L4，消耗明细要 L5，包生命周期要 L6，且各自装配时都要 L2 基准）。
    没有中枢就是同一份上游数据被打 3 次；有了它，同一时刻只有一个线程真正去取，
    其余等待同一结果。

    - fresh 命中时本次绕过缓存，直接重取（面板级局部刷新用）
    - 失败**不进缓存**：瞬时故障不该被固化 TTL 时长
    """

    def __init__(self, ttl=CACHE_TTL):
        self._ttl = ttl
        self._lock = threading.Lock()
        self._data = {}       # key -> (monotonic_ts, payload)
        self._inflight = {}   # key -> threading.Event

    def get(self, key, loader, fresh=False):
        """返回 (payload, err)。loader() 必须返回 (payload, err)。"""
        with self._lock:
            if not fresh:
                hit = self._data.get(key)
                if hit and (time.monotonic() - hit[0]) < self._ttl:
                    return hit[1], None
            ev = self._inflight.get(key)
            if ev is None:
                ev = threading.Event()
                self._inflight[key] = ev
                owner = True
            else:
                owner = False

        if not owner:
            # 已有同键在飞：等它落地后直接复用（避免重复打上游）
            ev.wait(timeout=120)
            with self._lock:
                hit = self._data.get(key)
            if hit:
                return hit[1], None
            return loader()          # 前者失败/超时：自己补一次

        try:
            payload, err = loader()
            if not err:
                with self._lock:
                    self._data[key] = (time.monotonic(), payload)
            return payload, err
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            ev.set()

    def invalidate(self, *keys):
        with self._lock:
            for k in (keys or tuple(self._data)):
                self._data.pop(k, None)


HUB = SourceHub()

# ---------------------------------------------------------------- 退出观测
# 为什么需要：服务曾出现「运行中静默消失」—— 无退出日志、无 traceback、端口直接释放，
# 事后完全无法判断是「正常退出」还是「被杀」。强杀（TerminateProcess / taskkill /F）
# 不触发 Python 层 atexit、不留任何应用日志。这里用**运行标记文件**把这件事变可见：
#   启动时：若发现上一次的标记仍在 → 上次**没走正常退出**（被强杀 / 崩溃 / 断电）
#   正常退出（看门狗、信号、/api/quit、Ctrl+C）→ 删除标记
# 注意：正常退出**必然**同时留下日志行，故「标记残留」是比日志更强的信号。
_STATE_FILE = "serve_state.json"

# 端口写进运行标记：自动签到（计划任务）要靠它找到"服务是否在跑、在哪个端口"，
# 从而复用已热好的浏览器桥，而不是自己再拉一个 Chrome 去抢同一个 profile。
_PORT = 8090


def _state_path():
    return os.path.join(wc.DATA_DIR, _STATE_FILE)


def _pid_alive(pid):
    """该 pid 是否仍存在（尽力而为；Windows 用 OpenProcess 判 STILL_ACTIVE）。"""
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k = ctypes.windll.kernel32
            h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                ok = k.GetExitCodeProcess(h, ctypes.byref(code))
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                k.CloseHandle(h)
        except Exception:
            return True          # 判不了就保守当作"还活着"，避免误报异常终止
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _fmt_dur(sec):
    try:
        sec = int(sec)
    except Exception:
        return "?"
    if sec < 60:
        return "%d 秒" % sec
    if sec < 3600:
        return "%d 分 %d 秒" % (sec // 60, sec % 60)
    return "%d 小时 %d 分" % (sec // 3600, (sec % 3600) // 60)


def _arm_state():
    """启动时调用：先检查上次标记残留（= 上次异常终止），再写本次标记。"""
    path = _state_path()
    prev = None
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                prev = json.load(f)
    except Exception:
        prev = None
    if isinstance(prev, dict) and prev.get("pid"):
        up = ""
        if prev.get("started_ts"):
            try:
                up = "，存活 %s" % _fmt_dur(time.time() - float(prev["started_ts"]))
            except Exception:
                up = ""
        if _pid_alive(prev.get("pid")):
            print("[state] 上次运行标记仍在，但 pid=%s 还活着（异常场景，建议核查）"
                  % prev.get("pid"))
        else:
            print("[state] ⚠ 上次服务**未走正常退出**：pid=%s 起始=%s 版本=%s%s"
                  % (prev.get("pid"), prev.get("started_at"), prev.get("version"), up))
            print("[state]   退出日志 / 异常栈 / 告别记录三者皆无 ⇒ 多为**被强制终止**"
                  "（taskkill /F、进程被结束、断电）或原生崩溃；正常退出（看门狗、"
                  "Ctrl+C、/api/quit）都会留下对应日志行。")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(),
                       "port": _PORT,
                       "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "started_ts": time.time(),
                       "version": wc.VERSION}, f, ensure_ascii=False)
    except Exception:
        pass


def _disarm_state():
    """正常退出时调用：删除运行标记（`os._exit` 不走 atexit，各退出路径都要显式调）。"""
    try:
        os.remove(_state_path())
    except Exception:
        pass


def _install_signal_handlers():
    """捕获可捕获的终止信号 → 走正常退出（留日志 + 清标记）。"""
    def _on(signum, _frame):
        print("[signal] 收到信号 %s，按正常退出处理…" % signum)
        _shutdown("服务被信号 %s 终止" % signum)
    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _on)
        except Exception:
            pass


def _install_thread_hook():
    """后台线程未捕获异常也落日志（默认只打到 stderr，且没有线程名标注）。"""
    def _hook(args):
        try:
            th = getattr(args.thread, "name", "?") if args.thread else "?"
            print("[thread] 线程 %s 未捕获异常：%s: %s"
                  % (th, getattr(args.exc_type, "__name__", "?"), args.exc_value))
        except Exception:
            pass
    try:
        threading.excepthook = _hook
    except Exception:
        pass


# 页面存活心跳：工作台页面定时发 /api/heartbeat 刷新此值；关闭全部页面后触发退出。
_last_heartbeat = time.time()
_pending_close = 0.0        # 收到页面告别（pagehide）的时间戳；0 = 无待关闭
# ⚠️ 心跳超时兜底已**停用**（v1.7.13）：实测 Edge「睡眠标签页」会让不活跃标签的 JS
# 整体停掉（心跳停、页面却没关），据此判"已关闭"必然误关。仅保留常量作参考。
IDLE_TIMEOUT = 300
BYE_CONFIRM_SECONDS = 75    # 页面告别后的确认期：期间仍有其它标签页心跳则取消关闭


def _shutdown(reason):
    """关掉浏览器桥并结束本进程（os._exit 不走 atexit，桥须显式关）。"""
    print(reason)
    _disarm_state()          # 正常退出 → 清运行标记（os._exit 不触发 atexit）
    try:
        wc.bridge_close()
    except Exception:
        pass
    os._exit(0)


def _idle_watchdog():
    """看门狗：页面**明确关闭**后自动退出后台（服务 + 浏览器桥一并清理）。

    只认**告别路**：页面关闭时 `pagehide` 发 `/api/bye`，进入确认期；确认期内若无
    其它标签页心跳，则判定「最后一个标签页已关」→ 退出（约 75 秒）。

    ⚠️ **不做心跳超时兜底**（v1.7.13 收紧）：实测 Edge「睡眠标签页」（本机阈值 300s）
    会让**不活跃标签的 JS 整体停掉** —— 心跳停了、**页面却没关**；据此判"已关闭"
    必然**误退**（用户切走一会儿回来，服务已经没了）。而"关闭"有可靠信号（pagehide）；
    心跳则无法区分「睡眠 / 冻结」与「关闭」，故退场。心跳仅保留用于告别确认期的判断。
    """
    global _pending_close
    while True:
        time.sleep(15)
        if _pending_close and time.time() - _pending_close > BYE_CONFIRM_SECONDS:
            if _last_heartbeat >= _pending_close:
                _pending_close = 0.0   # 确认期内有其它标签页心跳 → 取消
            else:
                _shutdown("工作台已关闭，自动退出后台…")


def _auth_status(auth_path):
    """读取登录态剩余有效期，供页面提示。只返回到期时间与剩余天数，不暴露 token 明文。

    桥模式下凭据不在本机（在浏览器里），改为读**浏览器会话 cookie 的到期时间** ——
    同样只要元信息，不碰凭据值。桥未启动时返回 None（不为了读 cookie 去拉起浏览器）。
    """
    if wc.bridge_enabled():
        info = wc.bridge_session_info()
        if not info:
            return {"kind": "browser", "note": "浏览器会话（桥未启动）"}
        return info
    try:
        with open(auth_path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    auth = d.get("auth") or {}
    exp = auth.get("expiresAt")
    rexp = auth.get("refreshExpiresAt")
    if not exp:
        return None
    now = int(time.time() * 1000)
    out = {
        "expires_at": exp,
        "days_left": round((exp - now) / 86400000.0, 1),
        "expired": exp < now,
    }
    if rexp:
        out["refresh_expires_at"] = rexp
        out["refresh_days_left"] = round((rexp - now) / 86400000.0, 1)
    return out


def _login():
    login = wc.load_login()
    if not login:
        # 区分「未登录」与「凭据已被客户端加密」—— 两者的用户动作完全不同，
        # 混成一句「请先登录客户端」会让明明已登录的用户一头雾水。
        kind, reason = wc.auth_diagnosis()
        return None, {"error": reason or "未找到 WorkBuddy 登录态，请先登录客户端。",
                      "auth_status_kind": kind}
    return login, None


def _err_payload(err):
    """统一错误响应体。桥模式下若因会话过期失败，带上可操作标记 ——
    前端据此显示「点此重新登录」，而不是丢一句看不懂的错误。"""
    out = {"error": err}
    if wc.bridge_enabled() and wc.SESSION_STATE.get("expired"):
        out["auth_status_kind"] = "session_expired"
        out["can_relogin"] = True
    return out


def build_json(fresh_all=False):
    """全量数据（旧接口 / 静态导出用）。"""
    login, err = _login()
    if err:
        return err
    token, uid, account, auth_path = login
    fresh = ("resource", "checkin", "l5", "l6") if fresh_all else ()
    data, err = wc.build_dashboard_data(token, uid, account, hub=HUB, fresh=fresh)
    if err:
        return _err_payload(err)
    data["auth_status"] = _auth_status(auth_path)
    return data


def build_panel_json(panel, fresh=False):
    """单个面板的数据。fresh=True 时只重取该面板拥有的源（局部刷新）。"""
    login, err = _login()
    if err:
        return err
    token, uid, account, auth_path = login
    keys = wc.PANEL_FRESH_KEYS.get(panel, ()) if fresh else ()
    t0 = time.perf_counter()
    data, err = wc.build_panel(panel, token, uid, account, hub=HUB, fresh=keys)
    if err:
        return _err_payload(err)
    data["auth_status"] = _auth_status(auth_path)
    data["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
    return data


def do_checkin():
    """执行签到（幂等）：已签直接返回，未签则调用签到接口。"""
    login, err = _login()
    if err:
        return {"ok": False, "error": err["error"]}
    token, uid, _account, _path = login
    st, err = wc.api_checkin_status(token, uid)
    if err:
        return {"ok": False, "error": "查询签到状态失败：%s" % err}
    if st.get("today_checked_in"):
        # 弹框里的「连续 N 天」必须与工作台卡片**同口径**（真实连续、可跨活动周期）：
        # 接口 streak_days 只统计当前活动周期、换周期即归零重算，直接拿它做文案会出现
        # 「弹框说 26 天、卡片说 48 天」这类自相矛盾（2026-10-16 实测）。
        # 故统一走 wc.real_streak_days()（与 build_dashboard_data 同一入口）。
        real = wc.real_streak_days(st.get("checkin_dates"))
        return {
            "ok": True, "already": True,
            "streak_days": st.get("streak_days"),          # 接口周期口径，保留给调用方
            "real_streak_days": real,                      # 卡片同口径（真实连续）
            "daily_credit": st.get("daily_credit"),
            "message": "今日已签到（已连续签到 %s 天，每日 +%s 积分）"
                       % (real, st.get("daily_credit", "?")),
        }
    data, err = wc.api_daily_checkin(token, uid)
    if err:
        return {"ok": False, "error": "签到失败：%s" % err}
    # 签到改变了 L4 状态，清缓存让后续请求拿到新值
    HUB.invalidate("checkin")
    # 刚签完 → 今天必然在签到集合里，并入后与卡片口径一致
    today = wc.today_date()
    real = wc.real_streak_days([today], today=today)
    return {
        "ok": True, "already": False,
        "credit": data.get("credit"),
        "streak_days": data.get("streak_days"),
        "real_streak_days": real,
        "message": "签到成功：+%s 积分，已连续签到 %s 天"
                   % (data.get("credit", "?"), real),
    }


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    def _fresh_flag(self, qs):
        v = (qs.get("fresh") or ["0"])[0]
        return v not in ("0", "false", "False", "")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html", "/dashboard.html"):
            if os.path.exists(DASHBOARD_HTML):
                with open(DASHBOARD_HTML, "rb") as f:
                    self._send(f.read(), "text/html; charset=utf-8")
            else:
                self._send(b"dashboard.html not found", "text/plain", 404)
        elif path == "/api/version":
            # 瞬时返回，不打上游：页头版本号要立刻可见
            self._send_json({"version": wc.VERSION, "ok": True})
        elif path == "/api/health":
            # 健康状态：供**启动器**判断「服务在跑、但取数异常」时是否强制重启。
            # 只读内存标志，不联网、不打上游，瞬时返回。
            self._send_json(wc.health_status())
        elif path == "/api/heartbeat":
            # 页面存活心跳：工作台页面定时发；一旦页面全部关闭、超时未收到，
            # 看门狗会自动退出后台（服务 + 浏览器桥一并清理）。
            global _last_heartbeat
            _last_heartbeat = time.time()
            self._send_json({"ok": True})
        elif path == "/api/bye":
            # 页面关闭告别：pagehide 时发送。进入「待关闭」确认期，若确认期内
            # 无其它标签页心跳则退出（精确区分「关闭」与「切后台/冻结」）。
            global _pending_close
            _pending_close = time.time()
            self._send_json({"ok": True})
        elif path == "/api/update":
            # 是否有新版本：**只读缓存**（联网在后台线程做，结果缓存 1h）。
            # 本接口永不阻塞、不修改任何本地文件 —— 升级由用户显式跑 update.py。
            self._send_json(wc.update_status())
        elif path == "/api/update-check":
            # 用户主动「检查更新」：绕过缓存 TTL，后台强制联网刷新一次；前端轮询
            # /api/update 观察 checked_at 变化拿到新结果。
            started = wc.force_update_check()
            self._send_json({"ok": True, "started": started,
                             "message": "正在检查…" if started else "已有检查在进行"})
        elif path == "/api/update-run":
            # 「立即升级」：用户在提示条上点按钮 = **用户显式触发**升级（与"用户自己跑
            # update.py"等价，不违反「改本地文件必须用户触发」的约束）。
            # 实现上以子进程跑 update.py（同解释器；它自带 sha256 校验 + 覆盖文件 +
            # 经 launcher 重启本服务），所以这里只负责"点火"：响应先返回，
            # 升级进度由前端轮询 /api/version 观察（服务被重启时连接拒绝属正常过程）。
            upd_py = os.path.join(HERE, "update.py")
            if not os.path.exists(upd_py):
                self._send_json({"ok": False, "error": "找不到 update.py"}, 500)
            else:
                if sys.platform == "win32":
                    flags = (subprocess.DETACHED_PROCESS
                             | subprocess.CREATE_NEW_PROCESS_GROUP
                             | subprocess.CREATE_NO_WINDOW)
                    kw = {"creationflags": flags, "close_fds": True}
                else:
                    kw = {"start_new_session": True, "close_fds": True}
                subprocess.Popen([sys.executable, upd_py], cwd=HERE, **kw)
                self._send_json({"ok": True,
                                 "message": "升级已开始：服务将自动重启，页面随后自动刷新"})
        elif path == "/api/update-log":
            # 「立即升级」的进度日志：update.py 子进程把全程输出写到该文件（每次运行
            # 覆盖、行缓冲）。服务重启中的空窗期本接口不可达，前端轮询已有兜底。
            p = os.path.join(wc.DATA_DIR, "update_last.log")
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    self._send_json({"ok": True, "log": f.read()[-2000:]})
            except Exception:
                self._send_json({"ok": True, "log": ""})
        elif path in ("/api/overview", "/api/requests", "/api/lifecycle", "/api/balance"):
            panel = path.rsplit("/", 1)[-1]
            self._send_json(build_panel_json(panel, fresh=self._fresh_flag(qs)))
        elif path == "/api/data":
            self._send_json(build_json(fresh_all=self._fresh_flag(qs)))
        elif path == "/api/create-shortcut":
            target = (qs.get("target") or ["desktop"])[0]
            ok, msg = create_shortcut(target)
            self._send_json({"ok": ok, "message": msg}, 200 if ok else 400)
        elif path == "/api/checkin":
            self._send_json(do_checkin())
        elif path == "/api/auto-checkin":
            # 自动签到状态。任务**是否存在要实查**（schtasks /Query），不能只信配置文件：
            # 用户可能在任务计划程序里手动删掉，那时开关若还显示「已开启」，
            # 就又是一次「看着对、实际不发生」—— 正是 v1.7.23 全量排查的那类问题。
            self._send_json(ac.status())
        elif path == "/api/auto-checkin-set":
            want = (qs.get("enable") or [""])[0].lower()
            tm = (qs.get("time") or [""])[0]
            if want in ("1", "true", "on", "yes"):
                ok, payload = ac.install(tm or None)
            elif want in ("0", "false", "off", "no"):
                ok, payload = ac.uninstall()
            else:
                ok, payload = False, {"error": "enable 需为 1 或 0"}
            out = dict(payload)
            out["ok"] = ok
            out["status"] = ac.status()   # 回带新状态，前端不必再问一次
            # 刻意返回 200（即使失败）：成败由 body 的 ok 表达。用 4xx 会在浏览器
            # 控制台留一条红字 "Failed to load resource"，而"开启失败"是本功能
            # **预期内**的一种结果（权限 / 编码 / 解释器），不该看起来像页面报错。
            self._send_json(out)
        elif path == "/api/auto-checkin-run":
            # 「立即试跑一次」：走 schtasks /Run，与定时触发是同一套动作定义 ——
            # 注册成功不等于能跑，这一步让用户当场验证，而不是等第二天发现没签上。
            ok, payload = ac.run_task_now()
            out = dict(payload)
            out["ok"] = ok
            self._send_json(out)
        elif path == "/api/set-config":
            # 写用户配置（现仅有「预计可用天数」的观测窗口）。GET + query，风格与
            # create-shortcut / checkin 一致；写成功后失效缓存，下次取数按新配置算。
            key = (qs.get("key") or [""])[0]
            if key == "avg_window_days":
                ok, msg = wc.set_avg_window((qs.get("value") or [""])[0])
                if ok:
                    HUB.invalidate()
                self._send_json({"ok": ok, "message": msg}, 200 if ok else 400)
            elif key == "browser_channel":
                ok, msg = wc.set_browser_channel((qs.get("value") or [""])[0])
                self._send_json({"ok": ok, "message": msg}, 200 if ok else 400)
            else:
                self._send_json({"ok": False, "error": "未知配置项: %s" % key}, 400)
        elif path == "/api/relogin":
            # 会阻塞到用户登录完成（最长 wait_seconds）。threading server 每请求一线程，
            # 不会拖住其他请求；前端把该请求的超时放宽到 6 分钟。
            ok, msg = wc.bridge_relogin()
            HUB.invalidate()
            self._send_json({"ok": ok, "message": msg,
                             "auth_status": _auth_status(None)}, 200 if ok else 400)
        elif path == "/dashboard_data.js":
            # 落盘/静态通道：必须剥离提示词（与 cmd_render 同一硬约束）
            data = build_json()
            if isinstance(data, dict) and not data.get("error"):
                data = wc.strip_prompts(data)
            body = ("window.CREDITS_DATA = "
                    + json.dumps(data, ensure_ascii=False, indent=2) + ";\n").encode("utf-8")
            self._send(body, "application/javascript; charset=utf-8")
        elif path.startswith("/assets/"):
            name = os.path.basename(path)
            fp = os.path.join(ASSETS_DIR, name)
            if os.path.exists(fp):
                with open(fp, "rb") as f:
                    self._send(f.read(), _mime_for(name))
            else:
                self._send(b"not found", "text/plain", 404)
        else:
            self._send(b"not found", "text/plain", 404)

    def do_POST(self):
        # sendBeacon（pagehide 告别）强制发 POST；其余接口都是 GET + query。
        # 这里只接「页面告别」这一路 POST。
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/bye":
            global _pending_close
            _pending_close = time.time()
            self._send_json({"ok": True})
        elif path == "/api/quit":
            # 本机回环「礼貌停机」：让启动器 / 更新器在重启前，旧服务能走**正常退出**
            #（留一行日志 + 清运行标记），而不是被 taskkill /F 强杀成一团黑 —— 否则
            # 下次启动的「运行标记残留」检查会把这种正常重启误报成「异常终止」。
            self._send_json({"ok": True, "msg": "服务即将退出"})
            threading.Thread(
                target=lambda: (time.sleep(0.3), _shutdown("收到 /api/quit，正常退出…")),
                daemon=True).start()
        else:
            self._send(b"not found", "text/plain", 404)

    def log_message(self, *args):
        pass  # 静默访问日志


def _warmup():
    """预热浏览器桥：服务刚起来时，后台把 Chrome 拉起、四源取一遍填进缓存。

    为什么值得做：桥用的 Chrome **冷启动要 3~5s**，而双击启动器后用户马上就会打开页面 ——
    与其让首屏干等这一段，不如趁用户切窗口的几秒把它热好。
    后台线程，失败静默，不影响服务本身。
    """
    time.sleep(2)                    # 先把端口让出来，确保页面能立刻连上
    try:
        login, err = _login()
        if err:
            return
        token, uid, account, _path = login
        wc.build_dashboard_data(token, uid, account, hub=HUB,
                                fresh=("resource", "checkin", "l5", "l6"))
    except Exception:
        pass


def _setup_log():
    """把服务输出落盘（启动器以无控制台方式拉起，print 本来无处可去）。

    带体积上限：>2MB 滚一轮（serve.log → serve.log.1），避免无限增长。
    **会话事件（登录 / 失效 / cookie 到期）也写进来**，便于分析「会话保持时间」。

    注意：`pythonw.exe`（GUI 子系统，`_spawn` 优先用它起服务）下 `sys.stdout` /
    `sys.stderr` 为 **None** —— 若不接管，任何 `print` 都会 AttributeError 崩掉服务。
    故重定向失败时兜底指向 devnull。
    """
    try:
        log_path = os.path.join(wc.DATA_DIR, "serve.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        if os.path.exists(log_path) and os.path.getsize(log_path) > 2 * 1024 * 1024:
            try:
                os.replace(log_path, log_path + ".1")
            except Exception:
                pass
        f = open(log_path, "a", encoding="utf-8", buffering=1)   # 行缓冲
        sys.stdout = f
        sys.stderr = f
        print("\n===== %s 启动服务 v%s =====（日志：%s）"
              % (time.strftime("%Y-%m-%d %H:%M:%S"), wc.VERSION, log_path))
    except Exception:
        try:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
        except Exception:
            pass


def _release_console():
    """释放本进程的控制台（若有），让那个终端窗口消失。

    为什么必须做：Windows 上以 `python.exe`（console 子系统）启动、且 `CREATE_NO_WINDOW`
    与 `DETACHED_PROCESS` 同用时会**被 Windows 忽略前者**（MSDN），进程因此**得到一个
    控制台**；Win11 默认由 **Windows Terminal** 承载 → 用户看到一个命令行窗口，而
    **关掉它服务就被杀**（实测正是「服务静默消失」的真因：无日志 / 无 traceback）。
    主动 `FreeConsole()`：控制台若只剩本进程附着就会被销毁、窗口随之消失；服务本身
    不依赖控制台（取数走 CDP，输出已落 serve.log）。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        if k.GetConsoleWindow():
            k.FreeConsole()
    except Exception:
        pass


def main():
    global _PORT
    _PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    _setup_log()          # 先落盘日志，之后所有 print 都留痕（含会话事件）
    _release_console()    # 释放被 Windows 分配的控制台（否则会弹出一个终端窗口）
    _arm_state()          # 运行标记：识别「上次是否异常终止」，正常退出时清除；含 port
    atexit.register(_disarm_state)
    _install_signal_handlers()
    _install_thread_hook()
    port = _PORT
    # 进程退出时关掉浏览器桥（懒加载启动的那个 Chrome，窗口在屏幕外）
    atexit.register(wc.bridge_close)
    with socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler) as httpd:
        print("积分工作台服务已启动：http://127.0.0.1:%d  （Ctrl+C 停止）" % port)
        print("  版本 %s ｜ 面板接口：/api/overview、/api/balance、/api/requests、/api/lifecycle"
              % wc.VERSION)
        print("  取数通道：%s" % (
            "浏览器桥（首次取数时启动浏览器，窗口在屏幕外；会话约 7 天过期）"
            if wc.bridge_enabled() else "直连 API"))
        # 预热：后台拉起浏览器 + 预取四源，免得首屏干等 3~5s 冷启动
        threading.Thread(target=_warmup, name="wb-warmup", daemon=True).start()
        # 看门狗：页面全部关闭后自动退出后台（服务 + 浏览器桥）
        threading.Thread(target=_idle_watchdog, name="wb-idle", daemon=True).start()
        # 顺带预热一次版本检查：它也是后台线程，等用户打开页面时缓存通常已就绪
        try:
            wc.update_status(trigger=True)
        except Exception:
            pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止。")


if __name__ == "__main__":
    main()
