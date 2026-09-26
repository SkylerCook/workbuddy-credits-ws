# -*- coding: utf-8 -*-
"""浏览器桥：借已登录的 workbuddy.cn 浏览器会话取数。

为什么需要它
------------
WorkBuddy 客户端已把登录凭据**加密落盘**（`$wbEncrypted` + AES-GCM 封套，密钥在客户端内），
本技能再也拿不到明文 token，直连 `copilot.tencent.com` 的 `Authorization: Bearer` 路径失效。
改为**借用官网会话**：让浏览器带着自己的 HttpOnly `session` cookie 去发请求，
本模块只接收 JSON —— **不接触、不解析、不落盘任何明文凭据**。

三条不可推翻的约束（都来自实测）
--------------------------------
1. **绝不用 headless**：同一 profile 混用 headful/headless 会让会话**凭空消失**
   （cookie 没了、所有接口 401）。"无头"用 `--window-position=-32000,-32000` 实现
2. **Playwright sync API 有线程亲和性**：必须在同一线程内创建与使用
   ⇒ 桥跑在**专用线程**里，其他线程通过队列提交请求（actor 模式）
3. **会话 7 天绝对过期、不滑动续期**：过期后必须请用户重新登录，不能静默失败

用法
----
    bridge = BrowserBridge()
    res = bridge.call("/billing/meter/get-user-resource", {})
    if res["ok"]:
        accounts = res["data"]["data"]["Response"]["Data"]["Accounts"]
    elif res["kind"] == "session_expired":
        bridge.relogin()          # 弹窗请用户登录
"""
import os
import queue
import threading
import time
from concurrent.futures import Future

DEFAULT_BASE = "https://www.workbuddy.cn"
LANDING_PATH = "/profile/plans-usage"

# 会话探针：拿它判断"登录态是否可用"。选它是因为返回体小、只读、无副作用。
SESSION_PROBE = "/billing/meter/get-user-resource-summary"

# 静默模式启动参数。**绝不要去掉 window-position** —— headful 内核 + 屏幕外坐标：
# 用户看不到窗口，但保留了 headful 的会话兼容性（headless 会丢会话，实测）。
LAUNCH_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--window-position=-32000,-32000",
    "--window-size=1024,768",
]

# 登录模式启动参数：窗口回到屏幕内、正常大小，方便用户操作
LAUNCH_ARGS_VISIBLE = [
    "--no-first-run",
    "--no-default-browser-check",
    "--window-size=1100,820",
]

JS_CALL = """async ([path, body, method]) => {
    const m = method || 'POST';
    const r = await fetch(path, {
        method: m,
        headers: {'Content-Type': 'application/json'},
        body: (m === 'GET' || body === undefined) ? undefined : JSON.stringify(body),
        credentials: 'include',
    });
    const text = await r.text();
    let js = null;
    try { js = JSON.parse(text); } catch (e) {}
    return {status: r.status, json: js, head: text.slice(0, 200)};
}"""

# 批量版：一次 evaluate 里用 Promise.all 并发发多个请求。
# 为什么需要：Python 侧（fetch_sources）是并发的，但本 worker 是单线程 ——
# 不攒批就会把「四源并发」退化成排队（实测刷新 3s+，攒批后 ≈0.6s）。
JS_CALL_MANY = """async (reqs) => {
    return await Promise.all(reqs.map(async ([path, body, method]) => {
        const m = method || 'POST';
        try {
            const r = await fetch(path, {
                method: m,
                headers: {'Content-Type': 'application/json'},
                body: (m === 'GET' || body === undefined) ? undefined : JSON.stringify(body),
                credentials: 'include',
            });
            const text = await r.text();
            let js = null;
            try { js = JSON.parse(text); } catch (e) {}
            return {status: r.status, json: js, head: text.slice(0, 200)};
        } catch (e) {
            return {status: -1, json: null, head: String(e).slice(0, 200)};
        }
    }));
}"""

START_TIMEOUT = 120      # 浏览器冷启动 + 首次导航的容忍时间（实测 3~5s，给足余量）
CALL_TIMEOUT = 90        # 单次接口调用的容忍时间


def default_profile_dir():
    """浏览器 profile 的默认位置（与其他用户数据同处，避免被临时目录清理误伤）。"""
    return os.path.join(os.path.expanduser("~"), ".workbuddy",
                        "workbuddy-credits-data", "browser_profile")


class BrowserBridge:
    """专用线程持有 Playwright，对外提供同步的 `call()`。

    懒加载：`call()` 首次被调用时才启动浏览器；之后复用同一个上下文；
    `close()` 或进程退出时关掉。整条链路里凭据始终留在浏览器内。
    """

    def __init__(self, profile_dir=None, base_url=DEFAULT_BASE, logger=None):
        self.profile_dir = profile_dir or default_profile_dir()
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")
        self.log = logger or (lambda msg: None)

        self._q = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()   # 保护"启动 worker"这一临界区
        self._ready = threading.Event()
        self._start_error = None
        self._closed = False
        self._login_mode = False        # 是否处于"可见窗口"登录模式

    # ---------------- 对外 API ----------------

    def probe(self, timeout=START_TIMEOUT):
        """探测会话是否可用。返回 True/False（不抛异常）。"""
        try:
            res = self.call(SESSION_PROBE, {}, timeout=timeout)
        except Exception as e:
            self.log("桥探测异常: %s" % e)
            return False
        return res["kind"] == "ok"

    def call(self, path, body=None, timeout=CALL_TIMEOUT, method="POST"):
        """调用接口。method 支持 POST（默认）/ GET。

        返回统一结构：

            {"ok": bool, "status": int, "data": dict|None,
             "error": str|None, "kind": str}

        kind ∈ ok / session_expired / api_error / http_error / bad_json / bridge_error
        """
        if self._closed:
            return self._fail("bridge_error", "桥已关闭")
        try:
            self._ensure_worker()
        except Exception as e:
            return self._fail("bridge_error", "浏览器启动失败: %s" % e)

        fut = Future()
        self._q.put(("call", (path, body, method), fut))
        try:
            raw = fut.result(timeout=timeout)
        except Exception as e:
            return self._fail("bridge_error", "桥调用失败: %s" % e)
        return self._interpret(raw)

    def relogin(self, wait_seconds=300, poll=3):
        """弹出一个**可见**的浏览器窗口请用户登录；成功后自动回到静默模式。

        返回 (ok, message)。仅在会话过期时调用。
        """
        if self._closed:
            return False, "桥已关闭"
        self.log("会话过期，请用户重新登录")
        try:
            self._ensure_worker()
        except Exception as e:
            return False, "浏览器启动失败: %s" % e

        fut = Future()
        self._q.put(("relogin", {"wait_seconds": wait_seconds, "poll": poll}, fut))
        try:
            return fut.result(timeout=wait_seconds + START_TIMEOUT + 60)
        except Exception as e:
            return False, "重新登录失败: %s" % e

    def session_info(self, timeout=30):
        """读取会话 cookie 的到期信息（**只读元信息**，不含任何凭据值）。

        用于工作台显示「会话还剩 X 天」。取不到时返回 None（不抛异常）。
        """
        if self._closed:
            return None
        # 桥未启动时**不为了读 cookie 而拉起浏览器** —— 否则每次打开页面都会
        # 白白多等一次冷启动。取数之后桥自然已在运行，那时读才是有意义的。
        if not (self._thread and self._thread.is_alive()):
            return None
        fut = Future()
        self._q.put(("session_info", None, fut))
        try:
            return fut.result(timeout=timeout)
        except Exception:
            return None

    def close(self):
        """关闭浏览器并结束 worker 线程。可重复调用。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._thread and self._thread.is_alive():
                self._q.put(("stop", None, None))
                self._thread.join(timeout=15)
        self.log("桥已关闭")

    def status(self):
        """给 UI 用的轻量状态。"""
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "login_mode": self._login_mode,
            "profile_dir": self.profile_dir,
            "base_url": self.base_url,
        }

    # ---------------- 内部：结果判定 ----------------

    @staticmethod
    def _fail(kind, error):
        return {"ok": False, "status": 0, "data": None, "error": error, "kind": kind}

    @staticmethod
    def _interpret(raw):
        if not isinstance(raw, dict):
            return BrowserBridge._fail("bridge_error", "桥返回了非预期结构")
        st = raw.get("status")
        js = raw.get("json")

        # 网关层拒绝：openresty 直接返 HTML 401/403（不是业务 JSON）
        if st in (401, 403):
            return {"ok": False, "status": st, "data": None,
                    "error": "会话未授权（HTTP %s）" % st, "kind": "session_expired"}

        if js is None:
            # 200 却拿不到 JSON：多半被重定向到了登录页（HTML）
            head = (raw.get("head") or "").strip()
            kind = "session_expired" if head.lstrip().lower().startswith("<") else "bad_json"
            return {"ok": False, "status": st or 0, "data": None,
                    "error": "非 JSON 响应（HTTP %s）" % st, "kind": kind}

        code = js.get("code")
        if code not in (0, None):
            return {"ok": False, "status": st or 0, "data": js,
                    "error": "code=%s msg=%s" % (code, js.get("msg")), "kind": "api_error"}

        if st != 200:
            return {"ok": False, "status": st, "data": js,
                    "error": "HTTP %s" % st, "kind": "http_error"}

        return {"ok": True, "status": 200, "data": js, "error": None, "kind": "ok"}

    # ---------------- 内部：worker 线程 ----------------

    def _ensure_worker(self):
        with self._lock:
            # 上次启动就失败、且线程已退出 → 直接复报，**不反复重试**。
            # 否则并发调用者会各自 spawn 一个新 worker、各自等满 START_TIMEOUT，
            # 页面表现为长时间「转圈」而不是快速失败。
            if self._start_error is not None and not (self._thread and self._thread.is_alive()):
                raise self._start_error

            need_wait = False
            if not (self._thread and self._thread.is_alive()):
                self._ready.clear()
                self._start_error = None
                self._thread = threading.Thread(target=self._run, name="wb-bridge", daemon=True)
                self._thread.start()
                need_wait = True
            elif not self._ready.is_set():
                need_wait = True     # 另一个线程正在启动，等它就绪（否则请求会石沉大海）

        if need_wait and not self._ready.wait(timeout=START_TIMEOUT):
            raise RuntimeError("浏览器启动超时（%ss）" % START_TIMEOUT)
        if self._start_error:
            raise self._start_error

    def _launch(self, p, visible):
        """启动持久化上下文并停在落地页。返回 (ctx, page)。"""
        ctx = p.chromium.launch_persistent_context(
            self.profile_dir, headless=False, channel="chrome",
            args=LAUNCH_ARGS_VISIBLE if visible else LAUNCH_ARGS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(self.base_url + LANDING_PATH,
                      wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            self.log("落地页导航异常（可继续）: %s" % e)
        return ctx, page

    def _run(self):
        """worker 主循环。**所有 Playwright 调用都发生在本线程。**"""
        try:
            try:
                from playwright.sync_api import sync_playwright
            except Exception as e:
                # ⚠️ 必须在 return 之前让 _ready 就位，否则调用方会白等满 START_TIMEOUT
                # （页面表现就是「三个面板一直转圈」，而不是给出有用的错误）。
                self._start_error = RuntimeError(
                    "当前 Python 解释器未安装 Playwright（%s）。浏览器桥依赖它："
                    "pip install playwright（浏览器用系统 Chrome，无需再下载内核）" % e)
                return
            with sync_playwright() as p:
                ctx, page = self._launch(p, visible=False)
                self._ready.set()
                self._worker_loop(p, ctx, page)
                try:
                    ctx.close()
                except Exception:
                    pass
        except Exception as e:
            self._start_error = e
        finally:
            self._ready.set()

    def _worker_loop(self, p, ctx, page):
        while True:
            item = self._q.get()
            if item is None:
                return
            if item[0] == "stop":
                return

            # ---- 攒批 ----
            # 把此刻**已经在队列里**的请求一并取走，稍后用一次 evaluate 并发发出去。
            # 单请求时走 JS_CALL（少一次封装开销），多请求时走 JS_CALL_MANY。
            batch = [item]
            while True:
                try:
                    nxt = self._q.get_nowait()
                except queue.Empty:
                    break
                if nxt[0] == "stop":
                    self._q.put(nxt)      # 放回，下一轮再收尾
                    break
                batch.append(nxt)

            calls, others = [], []
            for (action, payload, fut) in batch:
                (calls if action == "call" else others).append((action, payload, fut))

            # 非 call 的动作（relogin / session_info）逐个处理；relogin 会结束本 worker
            for (action, payload, fut) in others:
                if fut is not None and not fut.set_running_or_notify_cancel():
                    continue
                if action == "session_info":
                    fut.set_result(self._collect_session(ctx))
                elif action == "relogin":
                    # 必须先关掉静默上下文：profile 是独占锁定的，
                    # 不释放就用可见窗口重开会直接启动失败。
                    try:
                        ctx.close()
                    except Exception:
                        pass
                    fut.set_result(self._relogin_flow(p, payload))
                    return
                else:
                    fut.set_exception(ValueError("未知动作: %s" % action))

            if not calls:
                continue
            for (_a, _pl, fut) in calls:
                if fut is not None:
                    fut.set_running_or_notify_cancel()

            if len(calls) == 1:
                path, body, method = calls[0][1]
                try:
                    calls[0][2].set_result(page.evaluate(JS_CALL, [path, body, method]))
                except Exception as e:
                    calls[0][2].set_exception(e)
                continue

            args = [[pl[0], pl[1], pl[2]] for (_a, pl, _f) in calls]
            try:
                results = page.evaluate(JS_CALL_MANY, args)
            except Exception as e:
                for (_a, _pl, fut) in calls:
                    if not fut.done():
                        fut.set_exception(e)
                continue
            for (_a, _pl, fut), res in zip(calls, results):
                if not fut.done():
                    fut.set_result(res)

    @staticmethod
    def _collect_session(ctx):
        """从浏览器取会话 cookie 的到期元信息（只读，不取值）。"""
        out = {"cookies": [], "expires_at": None, "days_left": None, "expired": False}
        try:
            for c in ctx.cookies():
                nm = c.get("name")
                if nm in ("session", "session_2") and "workbuddy" in c.get("domain", ""):
                    out["cookies"].append({"name": nm, "expires": c.get("expires")})
        except Exception:
            return out
        exps = [c["expires"] for c in out["cookies"] if c.get("expires") not in (None, -1)]
        if exps:
            newest = max(exps)
            out["expires_at"] = int(newest * 1000)   # 与旧 auth.expiresAt 同为毫秒
            out["days_left"] = round((newest - time.time()) / 86400.0, 1)
            out["expired"] = newest < time.time()
        return out

    def _relogin_flow(self, p, payload):
        """用可见窗口请用户登录，轮询会话探针直到可用或超时。返回 (ok, msg)。"""
        wait_seconds = int(payload.get("wait_seconds", 300))
        poll = float(payload.get("poll", 3))
        self._login_mode = True
        ctxv = None
        try:
            ctxv, pv = self._launch(p, visible=True)
            deadline = time.time() + wait_seconds
            while time.time() < deadline:
                try:
                    r = pv.evaluate(JS_CALL, [SESSION_PROBE, {}])
                    j = r.get("json")
                    if r.get("status") == 200 and isinstance(j, dict) and j.get("code") == 0:
                        return True, "登录成功"
                except Exception:
                    pass
                time.sleep(poll)
            return False, "等待登录超时（%ds）" % wait_seconds
        except Exception as e:
            return False, "登录流程异常: %s" % e
        finally:
            self._login_mode = False
            if ctxv is not None:
                try:
                    ctxv.close()
                except Exception:
                    pass
