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
    2. 进程内 SourceHub：TTL 缓存 + single-flight，页面同时发三个面板请求时
       同一个源只会真正打一次上游，而不是三份
  ?fresh=1 强制重取（面板级刷新只重取该面板拥有的源，见 PANEL_FRESH_KEYS）。

安全：仅监听 127.0.0.1，数据不出本机。提示词只存在于 /api/* 的实时响应中，
      任何落盘通道（dashboard_data.js / render 产物）都必须先过 strip_prompts()。
"""

import atexit
import json
import os
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

    为什么需要：页面首屏会**同时**发 3 个面板请求，而三个面板共用底层数据源
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
        return {
            "ok": True, "already": True,
            "streak_days": st.get("streak_days"),
            "daily_credit": st.get("daily_credit"),
            "message": "今日已签到（连续 %s 天，每日 +%s 积分）"
                       % (st.get("streak_days", "?"), st.get("daily_credit", "?")),
        }
    data, err = wc.api_daily_checkin(token, uid)
    if err:
        return {"ok": False, "error": "签到失败：%s" % err}
    # 签到改变了 L4 状态，清缓存让后续请求拿到新值
    HUB.invalidate("checkin")
    return {
        "ok": True, "already": False,
        "credit": data.get("credit"),
        "streak_days": data.get("streak_days"),
        "message": "签到成功：+%s 积分，连续 %s 天"
                   % (data.get("credit", "?"), data.get("streak_days", "?")),
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
        elif path == "/api/update":
            # 是否有新版本：**只读缓存**（联网在后台线程做，结果缓存 1h）。
            # 本接口永不阻塞、不修改任何本地文件 —— 升级由用户显式跑 update.py。
            self._send_json(wc.update_status())
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
        elif path in ("/api/overview", "/api/requests", "/api/lifecycle"):
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


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    # 进程退出时关掉浏览器桥（懒加载启动的那个 Chrome，窗口在屏幕外）
    atexit.register(wc.bridge_close)
    with socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler) as httpd:
        print("积分工作台服务已启动：http://127.0.0.1:%d  （Ctrl+C 停止）" % port)
        print("  版本 %s ｜ 面板接口：/api/overview、/api/requests、/api/lifecycle"
              % wc.VERSION)
        print("  取数通道：%s" % (
            "浏览器桥（首次取数时启动浏览器，窗口在屏幕外；会话约 7 天过期）"
            if wc.bridge_enabled() else "直连 API"))
        # 预热：后台拉起浏览器 + 预取四源，免得首屏干等 3~5s 冷启动
        threading.Thread(target=_warmup, name="wb-warmup", daemon=True).start()
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
