"""自动签到：由 Windows 计划任务每天触发，开关在工作台里按需打开。

为什么要另起一个"宿主"而不是在服务进程里挂个定时器？

    服务有闲时看门狗：**工作台页面全部关闭约 75 秒后，服务自己退出**
    （见 serve._idle_watchdog，v1.7.13 收紧过）。定时器活在服务进程里，
    就意味着"用户关掉页面 = 自动签到失效"，而它不会报任何错 ——
    用户只会看到开关开着、却永远没签上。计划任务与工作台开不开无关，
    是唯一能"真的自动"的宿主，代价是系统里多一个任务条目。

三条硬约束（都是踩出来的）：

1. **pythonw 下 `sys.stdout` 是 `None`** —— 任何 `print` 都会 `AttributeError`
   把进程崩掉；而计划任务里的崩溃是**完全静默**的（任务计划程序只留一个退出码，
   没有窗口、没有 traceback）。故入口第一件事就是把输出接管到日志文件。
2. **计划任务与工作台抢同一个浏览器 profile** —— 桥用的是按浏览器分目录的
   **持久化** profile（见 browser_bridge.profile_dir_for）。同一 profile 被两个
   Chrome 抢会起不来 / 会话失效。所以：**服务在跑就借它的 `/api/checkin`**
   （它已经热好了桥），只有服务没在跑才自己拉 Chrome。
3. **`schtasks /XML` 只认 UTF-16 带 BOM** —— 写成 UTF-8 会报"任务 XML 格式
   不正确"，错误信息完全不指向编码，很容易去查语法。

失败重试不在这里做：任务 XML 配了 `RestartOnFailure`（10 分钟 × 3），
只在**退出码非零**时触发。所以"签到失败"必须 `exit(1)`，"已签/成功"必须 `exit(0)`
—— 否则要么白白重试，要么错过重试。

用法：
    python auto_checkin.py run                  # 计划任务调用：签一次，结果写日志
    python auto_checkin.py install --time 09:30 # 注册计划任务（默认 09:30）
    python auto_checkin.py uninstall            # 删除计划任务
    python auto_checkin.py status               # 打印状态 JSON
"""

import io
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import workbuddy_credits as wc  # noqa: E402

TASK_NAME = "WorkBuddyCreditsAutoCheckin"
DEFAULT_TIME = "09:30"

CONF_FILE = os.path.join(wc.DATA_DIR, "auto_checkin.json")
LOG_FILE = os.path.join(wc.DATA_DIR, "auto_checkin_log.json")
RUN_LOG = os.path.join(wc.DATA_DIR, "auto_checkin_run.log")
HISTORY_KEEP = 60


# ---------------------------------------------------------------- 输出接管

def _takeover_stdout():
    """把 stdout/stderr 接到日志文件。

    必须最先调用：`pythonw` 下两者都是 `None`，而本文件与本文件调用的
    `wc.cmd_checkin` 都会 print。不接管就会在**第一行 print** 处崩掉，
    且崩得无影无踪（这正是"自动签到开关开着却从没签成"最可能的死法）。
    """
    try:
        os.makedirs(wc.DATA_DIR, exist_ok=True)
        if os.path.exists(RUN_LOG) and os.path.getsize(RUN_LOG) > 512 * 1024:
            try:
                os.replace(RUN_LOG, RUN_LOG + ".1")
            except Exception:
                pass
        f = open(RUN_LOG, "a", encoding="utf-8", buffering=1)
        sys.stdout = f
        sys.stderr = f
    except Exception:
        try:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
            sys.stderr = sys.stdout
        except Exception:
            pass


# ---------------------------------------------------------------- 配置 / 日志

def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else default
    except Exception:
        return default


def _save_json(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def load_conf():
    d = _load_json(CONF_FILE, {})
    out = {
        "enabled": bool(d.get("enabled")),
        "time": str(d.get("time") or DEFAULT_TIME),
        "task": d.get("task") or TASK_NAME,
        "python": d.get("python") or "",
        "updated_at": d.get("updated_at") or "",
    }
    if not _valid_time(out["time"]):
        out["time"] = DEFAULT_TIME
    return out


def save_conf(conf):
    conf = dict(conf)
    conf["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return _save_json(CONF_FILE, conf)


def _valid_time(s):
    try:
        h, m = str(s).split(":")
        return 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except Exception:
        return False


def _norm_time(s):
    """'9:5' / '09:05' / '9:05' → '09:05'；非法返回 DEFAULT_TIME。"""
    if not _valid_time(s):
        return DEFAULT_TIME
    h, m = str(s).split(":")
    return "%02d:%02d" % (int(h), int(m))


def load_log():
    d = _load_json(LOG_FILE, {})
    hist = d.get("history")
    if not isinstance(hist, list):
        hist = []
    return {"last": d.get("last") if isinstance(d.get("last"), dict) else None,
            "history": hist[-HISTORY_KEEP:]}


def append_log(rec):
    d = load_log()
    d["last"] = rec
    d["history"] = (d["history"] + [rec])[-HISTORY_KEEP:]
    _save_json(LOG_FILE, d)
    return d


# ---------------------------------------------------------------- 解释器

def _has_playwright(exe):
    """该解释器能不能跑浏览器桥（硬依赖）。

    这一步不能省：计划任务一旦注册成"没有 Playwright 的解释器"，
    表现是**每天准时失败**，而开关看起来一切正常。
    """
    try:
        r = subprocess.run([exe, "-c", "import playwright"],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def _candidates():
    """按优先级列出候选解释器（pythonw 优先 —— 计划任务不能弹控制台窗口）。"""
    out = []

    def add(p):
        if p and os.path.exists(p) and p not in out:
            out.append(p)

    d = os.path.dirname(sys.executable)
    add(os.path.join(d, "pythonw.exe"))
    add(os.path.join(d, "python.exe"))
    for name in ("pythonw", "python", "python3"):
        add(shutil.which(name))
    # 常见安装位置兜底（注册表/venv 不再探测 —— 有检测 Playwright 这一步就够筛了）
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")):
        if not base:
            continue
        for py in ("Python313", "Python312", "Python311", "Python310"):
            add(os.path.join(base, "Programs", py, "pythonw.exe"))
            add(os.path.join(base, py, "pythonw.exe"))
    return out


def pick_python(force=None):
    """挑一个装了 Playwright 的解释器。返回 (path, checked) —— checked 是试过的清单。"""
    cands = ([force] if force else []) + _candidates()
    checked = []
    for exe in cands:
        if not exe or not os.path.exists(exe):
            continue
        checked.append(exe)
        if _has_playwright(exe):
            return exe, checked
    return None, checked


# ---------------------------------------------------------------- 任务 XML

# 不要往 <Settings> 里加 `DisallowStartOnRemoteAppSession` / `UseUnifiedSchedulingEngine`：
# 实测本机 Task Scheduler 解析器**直接拒收**这两个节点
# （「任务 XML 包含意外节点。(38,7):DisallowStartOnRemoteAppSession」），
# 补上配套节点也一样报错。它们只影响远程 App 会话，跟定时签到无关。
# 校验办法（schtasks 被安全软件拦时也能用）：
#   PowerShell > Register-ScheduledTask -TaskName Probe -Xml ([xml](Get-Content -Raw -Encoding Unicode 该xml)).OuterXml
# 成功的判据是 `REGISTER-OK`；报错会精确到「行列:节点名」。
_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>workbuddy-credits</Author>
    <Description>WorkBuddy 积分工作台 · 自动签到（每天 {time}，关机或未登录错过则下次登录后补跑）。由工作台开启/关闭。</Description>
    <URI>\\{task}</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT15M</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT10M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{args}</Arguments>
      <WorkingDirectory>{cwd}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _next_boundary(time_str):
    """下一次 time_str 的本地时刻（今天未过用今天，否则明天）。"""
    h, m = (int(x) for x in time_str.split(":"))
    now = datetime.now()
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if t <= now:
        t = t + timedelta(days=1)
    return t.strftime("%Y-%m-%dT%H:%M:%S")


def build_xml(python_exe, time_str, task_name=TASK_NAME):
    from xml.sax.saxutils import escape
    script = os.path.join(HERE, "auto_checkin.py")
    return _XML.format(
        time=time_str,
        task=escape(task_name),
        start=_next_boundary(time_str),
        command=escape(python_exe),
        # 脚本路径可能含空格（本机就是 "AI Era"）→ 必须自己加引号；
        # Command 由 XML 元素界定，不要再套一层引号。
        args=escape('"%s" run' % script),
        cwd=escape(HERE),
    )


# ---------------------------------------------------------------- schtasks

# 最近一次 schtasks **没能启动**的原因（""=启动过，rc 才是 schtasks 的返回值）。
# 为什么要单独记：`CreateProcess` 失败（被安全软件/系统策略拦截、组件缺失）时
# 我们只能自己造一个返回值，若与 schtasks 真的返回非零混为一谈，界面就会把
# 「工具起不来」写成「注册被拒」——用户照着「权限不足」去折腾，方向完全错。
_LAUNCH_ERR = ""


def _schtasks_exe():
    """schtasks 的绝对路径，找不到才退回裸名。

    为什么不直接传 "schtasks"：那是靠 PATH 解析，而 PATH 是**可以被父进程改写**
    的（某些工具会把自己的垫片目录插到最前面）。一旦被插队，解析到的可能是
    一个同名壳程序，行为完全不受控 —— 报出来的错还会误导人去查权限。
    系统目录里的那个才是任务计划程序本身。
    """
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    sys32 = os.path.join(root, "System32", "schtasks.exe")
    # 32 位进程访问 System32 会被重定向到 SysWOW64（无 schtasks），要走 Sysnative
    sysnative = os.path.join(root, "Sysnative", "schtasks.exe")
    for p in (sys32, sysnative):
        if os.path.isfile(p):
            return p
    return "schtasks"


def _run_schtasks(args, timeout=40):
    """跑 schtasks，返回 (rc, 输出文本)。输出在中文系统是 GBK。

    rc == -1 表示 **schtasks 自己没启动起来**（此时输出文本是我们生成的说明，
    不是 schtasks 的原话）；其余情况 rc 都是 schtasks 的真实退出码。
    """
    global _LAUNCH_ERR
    _LAUNCH_ERR = ""
    try:
        r = subprocess.run([_schtasks_exe()] + args, capture_output=True,
                           timeout=timeout, creationflags=_no_window())
        raw = (r.stdout or b"") + (r.stderr or b"")
        text = ""
        for enc in ("gbk", "utf-8", "utf-16"):
            try:
                text = raw.decode(enc)
                break
            except Exception:
                continue
        return r.returncode, text.strip()
    except FileNotFoundError:
        _LAUNCH_ERR = "missing"
        return -1, "找不到 schtasks.exe（系统组件缺失，或 PATH 被改写）"
    except PermissionError as e:
        _LAUNCH_ERR = "blocked"
        # 只说「起不来」和系统原话 —— 「不是权限不足」这类解释归 hint，
        # 否则界面上同一句话会出现两遍（原始信息 + 提示），反而更难读。
        return -1, "无法启动 schtasks.exe：%s" % e
    except Exception as e:
        _LAUNCH_ERR = "blocked"
        return -1, "调用 schtasks 失败：%s" % e


def _fail_payload(action, rc, out):
    """把一次 schtasks 失败翻译成「用户能照着做」的话。

    rc == -1 与 rc != 0 必须分开说：前者是这个工具根本没跑起来，跟任务定义、
    权限、XML 全无关；后者才是 schtasks 的判定（拒绝访问 / XML 不合法 / …）。
    """
    if rc == -1:
        if _LAUNCH_ERR == "missing":
            return {"error": "%s失败：系统里找不到 schtasks.exe" % action,
                    "detail": out,
                    "hint": "自动签到依赖 Windows 任务计划程序。请确认系统组件完整，"
                            "或在系统设置里把 schtasks.exe 从拦截名单中移除。"}
        return {"error": "%s失败：schtasks.exe 被拦截，无法启动" % action,
                "detail": out,
                "hint": "这一步跟权限无关 —— 是 schtasks.exe 本身没被允许运行。"
                        "请在安全软件（或系统「命令安全 / 程序黑名单」）里放行 schtasks.exe "
                        "后重试；工作台的手动签到不受影响。"}
    return {"error": "%s失败（schtasks 返回 %s）" % (action, rc),
            "detail": out,
            "hint": "若提示「拒绝访问」，多为系统策略限制；可先用「立即试跑一次」之外的"
                    "手动签到，或把工作台以当前用户身份重新启动后再开启。"}


def _no_window():
    if sys.platform != "win32":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def task_state(task_name=TASK_NAME):
    """计划任务是否存在。返回 (exists, error)，**exists=None 表示查询失败**。

    为什么不直接 `return rc == 0`：schtasks 查询失败（权限、被安全软件拦、
    schtasks 不可用）与"任务确实不存在"返回的都是非零 —— 混成一个 bool，
    就会把「查不到」画成「不存在」，让用户以为开关没生效。
    这正是本项目反复踩的那类坑（v1.7.23 全量排查），新功能不能再犯。
    """
    if sys.platform != "win32":
        return False, ""
    rc, out = _run_schtasks(["/Query", "/TN", task_name])
    if rc == 0:
        return True, ""
    # rc == -1：schtasks 没起来，此时 out 里**可能**含有「找不到」字样（组件缺失），
    # 绝不能拿去做关键词匹配 —— 那会把「工具不可用」判成「任务不存在」。
    if rc == -1:
        return None, out
    low = (out or "").lower()
    if ("cannot find" in low or "找不到" in out or "不存在" in out
            or "no such" in low or "does not exist" in low):
        return False, ""
    return None, (out or ("schtasks 返回 %s" % rc))


def task_exists(task_name=TASK_NAME):
    """仅用于"要删它"这类场合：查不到（None）也当作不存在处理。"""
    return task_state(task_name)[0] is not False


def install(time_str=None, python_exe=None):
    """注册（或覆盖）计划任务。返回 (ok, payload)。"""
    if sys.platform != "win32":
        return False, {"error": "自动签到只支持 Windows（依赖任务计划程序）"}
    conf = load_conf()
    t = _norm_time(time_str or conf["time"])
    exe = python_exe or conf.get("python") or None
    if not exe:
        exe, checked = pick_python()
        if not exe:
            return False, {
                "error": "没找到装了 Playwright 的 Python 解释器，自动签到无法注册。",
                "detail": "试过：" + ("；".join(checked) or "（无候选）"),
                "hint": "用装了 Playwright 的系统 Python 跑一次工作台启动器，再回来开启。",
            }
    elif not _has_playwright(exe):
        return False, {"error": "指定的解释器没有 Playwright：%s" % exe}

    xml_path = os.path.join(wc.DATA_DIR, "auto_checkin_task.xml")
    try:
        os.makedirs(wc.DATA_DIR, exist_ok=True)
        # UTF-16 带 BOM —— schtasks 只认这个（见文件头约束 3）
        with open(xml_path, "w", encoding="utf-16") as f:
            f.write(build_xml(exe, t))
    except Exception as e:
        return False, {"error": "写任务 XML 失败：%s" % e}

    rc, out = _run_schtasks(["/Create", "/TN", TASK_NAME, "/XML", xml_path, "/F"])
    if rc != 0:
        return False, _fail_payload("注册计划任务", rc, out)
    conf.update({"enabled": True, "time": t, "python": exe, "task": TASK_NAME})
    save_conf(conf)
    return True, {"enabled": True, "time": t, "python": exe, "task": TASK_NAME}


def uninstall():
    """关闭自动签到：先把配置关掉，再尽力删任务。

    顺序很重要：**配置关闭是用户要的效果，删任务只是清理**。
    若先删任务、删失败就整体报错，用户会卡在「关不掉」——而他真正要的
    （别再自动签了）其实只要写配置就能达到。

    任务查询失败（None）时也**不**盲目调用删除：那会在权限不足的环境里
    必然失败，把一次「已关闭」误报成「关闭失败」。改为关闭 + 明确告知
    「无法确认任务是否已删除」。
    """
    exists, qerr = task_state()
    warning = ""
    if exists is True:
        rc, out = _run_schtasks(["/Delete", "/TN", TASK_NAME, "/F"])
        if rc != 0:
            return False, _fail_payload("删除计划任务", rc, out)
    elif exists is None:
        warning = ("无法确认计划任务是否已删除（%s）。开关已关闭，"
                   "但若该任务仍然存在，系统可能继续按旧时间触发签到。" % (qerr or "查询失败"))
    conf = load_conf()
    conf["enabled"] = False
    conf["python"] = conf.get("python") or ""
    save_conf(conf)
    payload = {"enabled": False}
    if warning:
        payload["warning"] = warning
    return True, payload


def _next_run_at(time_str, enabled):
    if not enabled:
        return None
    return _next_boundary(time_str)


def run_task_now():
    """手动触发一次计划任务 —— 让用户当场验证「真链路」。

    为什么需要它：注册成功 ≠ 能跑起来。解释器路径、脚本路径、任务定义三者
    任一不对，任务计划程序都会安静地失败，而用户要等到**第二天**才发现。
    `schtasks /Run` 走的是与定时触发完全相同的动作定义。
    """
    if sys.platform != "win32":
        return False, {"error": "自动签到只支持 Windows"}
    if not task_exists():
        return False, {"error": "计划任务不存在，请先开启自动签到"}
    rc, out = _run_schtasks(["/Run", "/TN", TASK_NAME])
    if rc != 0:
        return False, _fail_payload("触发计划任务", rc, out)
    return True, {"message": "已触发，约 10 秒后看「上次运行」"}


def status():
    """给工作台/CLI 用的完整状态。"""
    conf = load_conf()
    log = load_log()
    exists, qerr = task_state()
    # 配置说开着、任务却没了（用户手动删了 / 系统还原 / 换了电脑）——
    # 这种"开关显示开启但实际不会运行"必须暴露出来，而不是让用户以为万事大吉。
    drift = bool(conf["enabled"] and exists is False)
    # 查不到时给一句「为什么查不到、下一步做什么」——「查询失败」四个字用户没法
    # 行动，只会以为功能坏了，然后去关安全软件或者干脆放弃。
    qhint = ""
    if exists is None:
        if "被拦截" in (qerr or "") or "无法启动" in (qerr or ""):
            qhint = ("这一步跟权限无关：schtasks.exe 本身没被允许运行。工作台里手动签到"
                     "不受影响；要自动签到，需先把 schtasks.exe 从拦截名单里放行。")
        elif "找不到 schtasks" in (qerr or ""):
            qhint = "系统里没有 schtasks.exe，无法使用计划任务；手动签到不受影响。"
        else:
            qhint = "计划任务状态未知，开关可能没有真正生效。可点「保存」重试注册。"
    return {
        "supported": sys.platform == "win32",
        # enabled 表达**用户意图**（配置），verified 表达**系统实况**。
        # 两者分开，才能区分「没开」「开了但任务丢了」「开了但查不到」三种情况。
        "enabled": bool(conf["enabled"]),
        "verified": exists is True,
        "task_exists": exists,                     # True / False / **None=查不到**
        "task_query_error": qerr,
        "task_hint": qhint,
        "task_name": TASK_NAME,
        "time": conf["time"],
        "python": conf.get("python") or "",
        "next_run": _next_run_at(conf["time"], exists is True),
        "drift": drift,
        "drift_hint": ("配置里是开启的，但系统里找不到这个计划任务 —— 可能被手动删除或系统还原过。"
                       "点一次「保存」可重新注册。") if drift else "",
        "last": log.get("last"),
        "history": log.get("history", [])[-10:],
    }


# ---------------------------------------------------------------- 执行签到

def _service_port():
    """活着的服务端口（优先读运行标记，它带 port）。没有服务返回 None。"""
    try:
        st = _load_json(os.path.join(wc.DATA_DIR, "serve_state.json"), {})
    except Exception:
        st = {}
    port = st.get("port")
    pid = st.get("pid")
    if port and pid:
        try:
            import urllib.request
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/version" % int(port), timeout=3) as r:
                if r.status == 200:
                    return int(port)
        except Exception:
            return None
    return None


def _http_json(url, timeout=120):
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def run_once(trigger="task"):
    """签一次。返回 (ok, rec)。ok=False 时进程应 exit(1) 让任务重试。"""
    rec = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ts": time.time(),
        "trigger": trigger,
    }

    # ① 服务在跑 → 借它的桥（避免两个 Chrome 抢同一个 profile，见约束 2）
    port = _service_port()
    if port:
        rec["via"] = "service"
        try:
            r = _http_json("http://127.0.0.1:%d/api/checkin" % port)
        except Exception as e:
            # 探测时服务还活着、调用时刚好退出（或该连接被拒）→ **落到独立路径**。
            # 不能因为"借不到桥"就判失败：那样要等 10 分钟的重试，甚至拖到第二天。
            r = None
            rec["service_error"] = "%s: %s" % (type(e).__name__, e)
        if r is not None:
            if r.get("ok"):
                rec["result"] = "skipped" if r.get("already") else "success"
                rec["message"] = r.get("message") or ""
                rec["credit"] = r.get("credit")
                rec["real_streak_days"] = r.get("real_streak_days")
                append_log(rec)
                return True, rec
            # 服务明确回了业务失败（会话过期等）→ 独立跑也一样会失败，不再重试路径
            rec["result"] = "failed"
            rec["message"] = r.get("error") or "签到失败（原因未知）"
            append_log(rec)
            return False, rec

    # ② 服务没在跑 → 自己走桥（签到逻辑复用 CLI 的 cmd_checkin，
    #    不另写一份 —— 文案与「连续 N 天」口径都跟工作台一致）
    rec["via"] = "direct"
    try:
        login = wc.load_login()
        if not login:
            kind, reason = wc.auth_diagnosis()
            rec["result"] = "failed"
            rec["message"] = reason or "未找到登录态，请先登录 WorkBuddy 客户端"
            rec["auth_status_kind"] = kind
            append_log(rec)
            return False, rec
        token, uid = login[0], login[1]

        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            rc = wc.cmd_checkin(token, uid)
        finally:
            sys.stdout = old
            try:
                wc.bridge_close()   # 关掉本进程拉起的 Chrome，不留后台残留
            except Exception:
                pass
        msg = buf.getvalue().strip()
        # 桥/会话自己的日志行（`[session] cookie到期=...`）也会进 stdout ——
        # 只留 cmd_checkin 那句结果，否则弹窗「上次运行」里会混进一串排障噪音
        lines = [l.strip() for l in msg.splitlines()
                 if l.strip() and not l.strip().startswith("[")]
        rec["message"] = lines[-1] if lines else msg
        rec["raw_log"] = msg
        if rc == 0:
            rec["result"] = "skipped" if "已签到" in rec["message"] else "success"
            append_log(rec)
            return True, rec
        rec["result"] = "failed"
        append_log(rec)
        return False, rec
    except Exception as e:
        rec["result"] = "failed"
        rec["message"] = "异常：%s: %s" % (type(e).__name__, e)
        append_log(rec)
        return False, rec


# ---------------------------------------------------------------- 入口

def main(argv):
    cmd = (argv[1] if len(argv) > 1 else "status").lower()
    if cmd == "run":
        _takeover_stdout()
        ok, rec = run_once("task")
        try:
            print("[auto-checkin] %s %s %s" % (rec.get("at"), rec.get("result"), rec.get("message")))
        except Exception:
            pass
        return 0 if ok else 1
    if cmd == "install":
        _takeover_stdout()
        t = DEFAULT_TIME
        for i, a in enumerate(argv):
            if a == "--time" and i + 1 < len(argv):
                t = argv[i + 1]
        ok, payload = install(t)
        print(json.dumps(payload, ensure_ascii=False))
        return 0 if ok else 1
    if cmd == "uninstall":
        _takeover_stdout()
        ok, payload = uninstall()
        print(json.dumps(payload, ensure_ascii=False))
        return 0 if ok else 1
    # status：给 CLI 看，也允许被 serve 直接调用
    print(json.dumps(status(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise
    except Exception:
        # 兜底：入口处的意外也不能静默（计划任务里没人看得到 traceback）
        try:
            _takeover_stdout()
            import traceback
            traceback.print_exc()
        except Exception:
            pass
        sys.exit(1)
