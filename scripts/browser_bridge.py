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

# 登录模式启动参数：窗口回到屏幕内、正常大小，方便用户操作。
# ⚠️ 必须显式给 --window-position：持久化 profile 会「记住」静默窗口的屏幕外坐标
# （-32000,-32000），若这里不写坐标，Chrome 恢复 profile 位置 → 登录窗仍弹在屏幕外。
LAUNCH_ARGS_VISIBLE = [
    "--no-first-run",
    "--no-default-browser-check",
    "--window-position=80,80",
    "--window-size=1100,820",
]

# 浏览器候选：**优先 Microsoft Edge**（Windows 默认自带，绝大多数机器零安装），回退 Google Chrome。
# channel 是 Playwright 的「预装浏览器别名」：msedge=Microsoft Edge、chrome=Google Chrome，
# 都不是 Playwright 自带的 Chromium（后者才需要 `playwright install` 下载内核）。
# Edge 同为 Chromium 内核，Playwright 官方支持 channel="msedge"，桥的会话机制对它一样成立。
BROWSER_CHANNELS = ["msedge", "chrome"]

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
    """浏览器 profile 的基础位置（与其他用户数据同处，避免被临时目录清理误伤）。"""
    return os.path.join(os.path.expanduser("~"), ".workbuddy",
                        "workbuddy-credits-data", "browser_profile")


# Chrome 与 Edge 对**同一个 user-data-dir** 的 cookie 加密互不兼容（各自持有/轮换
# os_crypt 密钥，跨应用解密失败）—— 混用同一目录会让会话「凭空消失」：
# 实测 2026-09-26，早上在 Chrome 窗口登录，下午桥切到 Edge（v1.7.4 Edge 优先）
# 接管同一 profile 后会话即失效。故 **profile 按浏览器分目录**，各存各的 cookie；
# 切换浏览器后首次需重新登录一次（新目录无 cookie），稳定后互不影响。
# chrome 沿用无后缀的基础目录（兼容历史 Chrome 用户的已有 profile）。
CHANNEL_PROFILE_SUFFIX = {"msedge": "-msedge", "chrome": ""}


def profile_dir_for(channel, base=None):
    """某浏览器专用 profile 目录。"""
    suffix = CHANNEL_PROFILE_SUFFIX.get(channel, "-" + channel)
    return (base or default_profile_dir()) + suffix


def _data_dir():
    return os.path.dirname(default_profile_dir())


LAST_CHANNEL_FILE = os.path.join(_data_dir(), "browser_channel.txt")


def read_last_channel():
    """上次**登录成功**所用浏览器的 channel（保证 cookie 连续性；读到即最优先）。"""
    try:
        with open(LAST_CHANNEL_FILE, "r", encoding="utf-8") as f:
            v = f.read().strip()
        return v if v in CHANNEL_PROFILE_SUFFIX else ""
    except Exception:
        return ""


def write_last_channel(channel):
    try:
        os.makedirs(os.path.dirname(LAST_CHANNEL_FILE), exist_ok=True)
        with open(LAST_CHANNEL_FILE, "w", encoding="utf-8") as f:
            f.write(channel)
    except Exception:
        pass


def channel_candidates():
    """launch 的候选顺序：上次登录成功的浏览器最优先（cookie 连续），其余按默认序。"""
    last = read_last_channel()
    return ([last] if last else []) + [c for c in BROWSER_CHANNELS if c != last]


class BrowserBridge:
    """专用线程持有 Playwright，对外提供同步的 `call()`。

    懒加载：`call()` 首次被调用时才启动浏览器；之后复用同一个上下文；
    `close()` 或进程退出时关掉。整条链路里凭据始终留在浏览器内。
    """

    def __init__(self, profile_dir=None, base_url=DEFAULT_BASE, logger=None):
        # profile_dir：显式传入则所有浏览器共用（自定义场景，原行为）；
        # 默认按浏览器分目录（见 profile_dir_for —— Chrome/Edge cookie 加密互不兼容）。
        self.profile_dir = profile_dir or default_profile_dir()
        self._custom_profile = profile_dir is not None
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")
        self.log = logger or (lambda msg: None)
        self._channel = ""          # 本次 launch 实际使用的浏览器 channel

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
        actual = self.profile_dir
        if self._channel and not self._custom_profile:
            actual = profile_dir_for(self._channel)   # launch 实际使用的 profile
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "login_mode": self._login_mode,
            "channel": self._channel,
            "profile_dir": actual,
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
        """启动持久化上下文并停在落地页。返回 (ctx, page)。

        候选顺序：**上次登录成功的浏览器最优先**（其 profile 才有有效 cookie），
        其余按默认序（Edge 优先、Chrome 回退）。每个 channel 用**自己的 profile**
        （Chrome/Edge 的 cookie 加密互不兼容，绝不能共用目录）。
        channel 别名不存在会在 launch 阶段抛异常（此时尚未创建进程 / 锁定
        profile），换下一个候选是安全的。
        """
        args = LAUNCH_ARGS_VISIBLE if visible else LAUNCH_ARGS
        last_err = None
        ctx = None
        for ch in channel_candidates():
            try:
                ctx = p.chromium.launch_persistent_context(
                    self.profile_dir if self._custom_profile else profile_dir_for(ch),
                    headless=False, channel=ch, args=args)
                self._channel = ch
                break
            except Exception as e:
                last_err = e
                continue
        if ctx is None:
            raise RuntimeError(
                "未找到可用的浏览器（已尝试 %s）：%s。"
                "请安装 Microsoft Edge 或 Google Chrome。"
                % (" / ".join(channel_candidates()), last_err or "未知错误"))
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
                    "pip install playwright（浏览器用系统 Chrome 或 Edge，无需再下载内核）" % e)
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
        state = {"ctx": ctx, "page": page}   # 重建浏览器后原地替换
        while True:
            item = self._q.get()
            if item is None:
                return
            if item[0] == "stop":
                try:
                    state["ctx"].close()
                except Exception:
                    pass
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

            try:
                self._eval_calls(state["page"], calls)
            except Exception as e:
                if not self._looks_closed(e):
                    for (_a, _pl, fut) in calls:
                        if not fut.done():
                            fut.set_exception(e)
                    continue
                # 浏览器实例被意外关闭（用户手动关窗 / 崩溃 / 被系统回收）：
                # 自动重建静默窗口并重试一次，避免「关一次浏览器之后永远报错」。
                self.log("浏览器实例已失效，自动重建静默窗口: %s" % str(e).strip()[:100])
                try:
                    try:
                        state["ctx"].close()
                    except Exception:
                        pass
                    state["ctx"], state["page"] = self._launch(p, visible=False)
                    self._eval_calls(state["page"], calls)
                except Exception as e2:
                    for (_a, _pl, fut) in calls:
                        if not fut.done():
                            fut.set_exception(e2)

    @staticmethod
    def _looks_closed(e):
        """判断异常是否为「浏览器/页面已关闭」类（触发自动重建）。"""
        s = str(e).lower()
        return ("closed" in s or "target page" in s or "browser has been" in s)

    def _eval_calls(self, page, calls):
        """执行一批 call 并把结果塞回各自的 Future。"""
        if len(calls) == 1:
            path, body, method = calls[0][1]
            calls[0][2].set_result(page.evaluate(JS_CALL, [path, body, method]))
            return
        args = [[pl[0], pl[1], pl[2]] for (_a, pl, _f) in calls]
        results = page.evaluate(JS_CALL_MANY, args)
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
        """用可见窗口请用户登录，轮询 session cookie 直到有效或超时。返回 (ok, msg)。

        判定登录成功**不看「页面发探针请求」** —— 登录后页面可能跳转/导航，
        `page.evaluate` 会反复失败导致误判超时（实测「登录成功但工作台没自动刷新」）。
        改为直接读 session cookie 的到期时间：登录成功后服务端 Set-Cookie 会把它更新为
        「未来 7 天」，与页面状态无关、更可靠。
        """
        wait_seconds = int(payload.get("wait_seconds", 300))
        poll = float(payload.get("poll", 2))
        self._login_mode = True
        ctxv = None
        try:
            ctxv, pv = self._launch(p, visible=True)
            deadline = time.time() + wait_seconds
            while time.time() < deadline:
                try:
                    info = self._collect_session(ctxv)
                    if info.get("expires_at") and not info.get("expired"):
                        # 记住登录用的浏览器：下次 launch 最优先用它，保证 cookie 连续
                        write_last_channel(self._channel)
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
