#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
积分工作台启动器（通用，随 skill 分发）

用途：
  1. 默认（无参数）：启动服务（若未运行 / 版本过旧 / **取数异常**则接管）+ 打开系统浏览器
     —— 供「双击 VBS」场景使用，服务以独立进程（DETACHED）常驻，脱离 WorkBuddy 桌面端
  2. --restart：跳过「复用判断」，**无条件重启**服务（版本相同也重启）
     —— 用户遇到异常时的自救入口（默认行为只在「版本不符 / 取数异常」时才重启）
  3. --gen-vbs：探测本机 pythonw 路径，生成双击启动器 VBS 到专门目录
     —— 供 Agent 首次打开工作台时顺带生成（幂等），告知用户以后可双击启动
  4. --create-shortcut <desktop|startmenu>：创建快捷方式到桌面 / 开始菜单「所有应用」
     —— 供 Agent 对话入口（复用 shortcut.py）

设计要点：
  - 零硬编码：serve.py 用 __file__ 同级定位；pythonw 用 sys.executable 同目录推导
  - VBS 生成到 ~/.workbuddy/launchers/（本地专属，不进 skill 源码、不随更新覆盖、不打进分发包）
  - 「端口占用」不等于「服务可用」：旧进程会一直占着 8090，若只看端口就会误判为
    「已在运行」，导致永远打开旧版页面。故改为「端口 + 版本」双校验（见 _ensure_running）。
  - **「版本相同」也不等于「可用」**：实测服务在跑、桥却已失效，故版本相同时再看一眼
    `/api/health`，取数异常即自动重启（见 _probe_health）。
  - **依赖落点必须稳**：Playwright 装在 `~/.workbuddy/workbuddy-credits-data/.venv`（技能
    数据目录，**不受 WorkBuddy 客户端更新影响**）；找不到就**自动创建并安装**（见
    _ensure_playwright_env）。内置 Python 会随客户端更新被替换（实测连装进去的包一起清掉），
    依赖装在那里下次更新必然失效。
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
SERVE = os.path.join(HERE, "serve.py")
MANIFEST = os.path.join(SKILL_DIR, "manifest.yaml")
sys.path.insert(0, HERE)

from shortcut import create_shortcut  # noqa: E402
PORT = 8090
URL = "http://127.0.0.1:%d" % PORT
VBS_DIR = os.path.join(os.path.expanduser("~"), ".workbuddy", "launchers")
VBS_PATH = os.path.join(VBS_DIR, "start-credits-dashboard.vbs")

# 技能数据目录（与 workbuddy_credits.DATA_DIR 同源）—— **WorkBuddy 客户端更新不碰它**，
# 故持久 venv 建在这里：内置 Python 会随客户端更新被替换（实测更新把原目录改名
# `.deleting.*`，连装进去的 Playwright 一并清掉），往那里装依赖下次更新必然失效。
DATA_DIR = os.path.join(os.path.expanduser("~"), ".workbuddy", "workbuddy-credits-data")
PERSIST_VENV = os.path.join(DATA_DIR, ".venv")
# 锁版本 + 国内镜像：可复现（不吃 PyPI 最新版漂移）、国内下载快。
PLAYWRIGHT_SPEC = "playwright==1.58.0"
PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
# 依赖探测表达式：走真实调用链（sync_api → greenlet），而非只 import playwright，
# 以免「base 换大版本、旧 ABI 依赖还在」时假就绪（详见 _python_has_playwright）。
_PW_PROBE = "from playwright.sync_api import sync_playwright"

# pythonw 无控制台，stdout/stderr 为 None，重定向到 devnull 避免 print 崩溃
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")


def _find_pythonw():
    """从当前解释器推导无窗口解释器：同目录 pythonw.exe，无则降级用当前解释器。"""
    if sys.platform == "win32":
        cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.exists(cand):
            return cand
        return sys.executable
    return sys.executable


def _detach_kwargs():
    """独立进程启动参数：Windows 用 DETACHED+无窗口，Unix 用 setsid。"""
    if sys.platform == "win32":
        return {
            "creationflags": subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW,
            "close_fds": True,
        }
    return {"start_new_session": True, "close_fds": True}


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_hidden(args):
    """跑一条系统命令并取 stdout：不弹窗，中文系统按 mbcs 解码（best-effort）。"""
    kw = {
        "capture_output": True,
        "text": True,
        "encoding": "mbcs" if sys.platform == "win32" else "utf-8",
        "errors": "ignore",
    }
    if sys.platform == "win32":
        kw["creationflags"] = _NO_WINDOW
    try:
        return subprocess.run(args, **kw)
    except Exception:
        return None


def _port_in_use():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", PORT))
        s.close()
        return True
    except Exception:
        return False


# 本机回环直连：不要走系统代理（与 serve.py / 技能网络约定保持一致）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _desired_version():
    """当前技能版本，与 serve.py 的 wc.VERSION 同源（manifest.yaml）。"""
    try:
        with open(MANIFEST, "r", encoding="utf-8") as f:
            m = re.search(r"^version:\s*[\"']?([0-9][^\"'\s]*)", f.read(), re.M)
        return m.group(1) if m else None
    except Exception:
        return None


def _probe_version(timeout=1.5):
    """探已运行服务的版本号。
    返回 'x.y.z'；服务是旧版（没有 /api/version 路由）或探测失败则返回 None。"""
    try:
        with _OPENER.open(URL + "/api/version", timeout=timeout) as r:
            if getattr(r, "status", 200) != 200:
                return None
            return (json.loads(r.read().decode("utf-8")) or {}).get("version")
    except Exception:
        return None


def _probe_health(timeout=3):
    """探服务健康状态（/api/health）。拿不到返回 None。

    为什么要它：「版本相同」不等于「服务可用」—— 实测服务在跑、桥的页面却已失效，
    取数全挂；此时若只按版本复用，用户会「以为重启了、其实没重启」。
    """
    try:
        with _OPENER.open(URL + "/api/health", timeout=timeout) as r:
            if getattr(r, "status", 200) != 200:
                return None
            return json.loads(r.read().decode("utf-8")) or {}
    except Exception:
        return None


def _port_owner_pid():
    """占用监听端口的进程 PID（解析 netstat，不依赖 psutil）。"""
    r = _run_hidden(["netstat", "-ano"])
    if not r or not r.stdout:
        return None
    suffix = ":%d" % PORT
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].endswith(suffix):
                try:
                    return int(parts[4])
                except ValueError:
                    continue
    return None


def _is_python_process(pid):
    r = _run_hidden(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"])
    return "python" in (r.stdout or "").lower() if r else False


def _stop_stale_server():
    """结束占用 8090 的旧服务进程。

    只对 python 系进程动手 —— 端口被别的程序占用时绝不误杀，交由用户处理，
    返回 False 让调用方给出明确提示。
    """
    pid = _port_owner_pid()
    if not pid:
        return False
    if not _is_python_process(pid):
        print("端口 %d 被非 python 进程占用（PID %d），未自动处理。" % (PORT, pid),
              file=sys.stderr)
        return False
    _run_hidden(["taskkill", "/PID", str(pid), "/F"])
    for _ in range(24):
        time.sleep(0.25)
        if not _port_in_use():
            return True
    return not _port_in_use()


def _volatile_rank(exe):
    """解释器「易失性」排序键：落在 WorkBuddy 内置目录的排后面。

    内置 Python 会随客户端更新被替换/重置（实测更新把原目录改名 `.deleting.*`，
    连装进去的包一并清掉）。而 venv 的 `pyvenv.cfg` 会记下 base 的 `home` ——
    base 落在这种目录时，客户端更新后 venv 就会断链（必须重建）。
    所以优先用「路径稳定」的解释器做 base。
    """
    low = (exe or "").lower().replace("/", "\\")
    if "\\binaries\\python\\" in low or "\\resources\\vendor\\python" in low:
        return 1
    return 0


def _python_has_playwright(exe):
    """探测某解释器是否装了 Playwright（浏览器桥的硬依赖）。

    探测走**真实调用链** `sync_api`（会加载 greenlet 等 C 扩展），而不是只
    `import playwright`：后者在「venv 的 base 已换成另一大版本的 Python、但
    site-packages 里旧 ABI 的 greenlet 还在」时会**假就绪** —— 探测通过，
    真正起桥时才 ImportError（页面表现为面板一直转圈）。
    """
    if not exe:
        return False
    if (os.path.sep in exe or ":" in exe) and not os.path.exists(exe):
        return False
    try:
        kw = {"capture_output": True, "timeout": 60}
        if sys.platform == "win32":
            kw["creationflags"] = _NO_WINDOW
        return subprocess.run([exe, "-c", _PW_PROBE], **kw).returncode == 0
    except Exception:
        return False


def _registry_pythons():
    """从注册表列出已安装 Python 的 python.exe（Windows；失败返回空表）。"""
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except Exception:
        return []
    out = []
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in (r"SOFTWARE\Python\PythonCore",
                    r"SOFTWARE\WOW6432Node\Python\PythonCore"):
            try:
                with winreg.OpenKey(root, sub) as k:
                    for i in range(winreg.QueryInfoKey(k)[0]):
                        try:
                            ver = winreg.EnumKey(k, i)
                            with winreg.OpenKey(k, ver + r"\InstallPath") as ik:
                                out.append(os.path.join(
                                    winreg.QueryValueEx(ik, "")[0], "python.exe"))
                        except Exception:
                            continue
            except Exception:
                continue
    return out


def _venv_python(venv_dir):
    """venv 内的 python 解释器路径（跨平台）。"""
    if sys.platform == "win32":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def _candidate_interpreters():
    """按优先级列出候选解释器。

    **持久 venv 最优先** —— 它建在技能数据目录下，不受 WorkBuddy 客户端更新影响；
    而内置 Python 会随客户端更新被替换（实测连装在里面的包一起清掉）。
    `python.exe` 排在 `pythonw.exe` 前面 —— 带控制台的解释器里 Playwright 跑得更稳，
    出错也更容易落进日志。
    """
    seen, out = set(), []

    def add(p):
        if p and p not in seen:
            seen.add(p)
            out.append(p)

    add(_venv_python(PERSIST_VENV))      # 0) 持久 venv（数据目录，最稳）
    d = os.path.dirname(sys.executable)
    add(os.path.join(d, "python.exe"))
    add(os.path.join(d, "pythonw.exe"))
    add(sys.executable)
    for name in ("python", "pythonw", "python3"):
        try:
            add(shutil.which(name))
        except Exception:
            pass
    for base in _registry_pythons():
        add(base)
        add(os.path.join(os.path.dirname(base), "pythonw.exe"))
    return out


def _find_serve_python():
    """挑一个**装了 Playwright** 的解释器来跑 serve.py。

    为什么需要这一步：双击启动器时，launcher 往往由 WorkBuddy 内置 pythonw 执行，
    而 Playwright 多数装在系统 Python 里 —— 直接沿用内置解释器会让浏览器桥起不来，
    页面表现为「三个面板一直转圈」。所以这里主动探测，而不是想当然用 sys.executable。
    """
    for exe in _candidate_interpreters():
        if _python_has_playwright(exe):
            return exe
    return None


def _find_launcher_pythonw():
    """挑一个装了 Playwright 的解释器对应的 pythonw，用于**双击启动器 VBS**。

    为什么不用 sys.executable 推导：VBS 通常在 Agent 首次打开工作台时生成，那一刻
    launcher 由 WorkBuddy 内置 pythonw 执行 —— 若照搬内置 pythonw 进 VBS，双击就会先
    启动内置解释器（无 Playwright）再让 launcher 去探测系统解释器，多一层冷启动与探测。
    这里直接选装了 Playwright 的解释器（默认即「本机那个」），让 VBS 一步到位用对解释器。
    """
    exe = _find_serve_python()
    if not exe:
        return None
    d = os.path.dirname(exe)
    pw = os.path.join(d, "pythonw.exe")
    return pw if os.path.exists(pw) else exe


def _notify(msg, title="积分工作台 · 启动失败"):
    """双击场景没有控制台 —— 弹系统提示框，否则用户只看到「转圈」而无从得知原因。"""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, title, 0x40)
            return
        except Exception:
            pass
    print(msg, file=sys.stderr)


def _is_runnable_python(exe):
    """该路径是否是一个能跑起来的 Python 解释器。"""
    if not exe or not os.path.exists(exe):
        return False
    try:
        kw = {"capture_output": True, "timeout": 30}
        if sys.platform == "win32":
            kw["creationflags"] = _NO_WINDOW
        return subprocess.run([exe, "-c", "import sys"], **kw).returncode == 0
    except Exception:
        return False


def _warn_if_volatile(exe):
    """解释器落在「会随客户端更新重置」的目录时告警（不阻断）。"""
    if _volatile_rank(exe):
        print("⚠️ 当前解释器位于 WorkBuddy 内置目录（%s）——该位置会随客户端更新重置，"
              "装在这里的依赖可能失效；建议改用数据目录下的持久 venv。" % exe)


def _ensure_playwright_env():
    """确保有一个装了 Playwright 的解释器；**没有就自动建持久 venv 并安装**。

    返回 (解释器路径 或 None, 失败说明 或 None)。

    为什么建在数据目录：WorkBuddy 内置 Python 会随客户端更新被**替换/重置**（实测更新把
    原目录改名 `.deleting.*`、连装进去的包一起清掉），依赖装在那里**下次更新必然失效**。
    数据目录不受客户端更新影响，是唯一稳定的落点 —— 这样才谈得上「clone 即用」。
    """
    exe = _find_serve_python()
    if exe:
        _warn_if_volatile(exe)
        return exe, None
    # base 选择：**优先路径稳定的解释器**（系统 Python / PATH / 注册表），内置的排最后。
    # venv 的 pyvenv.cfg 会记住 base —— base 越稳定，venv 越不容易在客户端更新后断链。
    runnable = [c for c in _candidate_interpreters() if _is_runnable_python(c)]
    runnable.sort(key=_volatile_rank)        # 稳定排序：同档保持原有优先级
    base = runnable[0] if runnable else None
    if not base:
        return None, ("未找到任何可用的 Python 解释器（连 WorkBuddy 内置的也没有）。\n\n"
                      "请安装 Python 3.10+ 后重试。")
    print("未找到装了 Playwright 的解释器，正在准备持久环境（首次约 1-2 分钟）…")
    _notify("首次启动需要准备依赖（Playwright，约 100MB），请稍候 1–2 分钟。\n\n"
            "完成后工作台会自动打开，无需重复操作。\n"
            "依赖装在：%s\n（该位置不受 WorkBuddy 更新影响）" % PERSIST_VENV,
            "积分工作台 · 首次准备依赖")
    try:
        subprocess.run([base, "-m", "venv", PERSIST_VENV], check=True, timeout=180,
                       capture_output=True, creationflags=_NO_WINDOW)
    except Exception as e:
        return None, "创建持久 venv 失败：%s" % e
    py = _venv_python(PERSIST_VENV)
    try:
        # -U：若 venv 是**就地重建**（base 换了版本），site-packages 里旧 ABI 的依赖
        #（如 greenlet 的 .pyd）需按新解释器重装；不加 -U 时 pip 会因「已满足」而跳过。
        subprocess.run([py, "-m", "pip", "install", "-U", "-i", PIP_MIRROR, PLAYWRIGHT_SPEC],
                       check=True, timeout=900, capture_output=True,
                       creationflags=_NO_WINDOW)
    except Exception as e:
        return None, ("安装 Playwright 失败：%s\n\n可手动执行：\n"
                      "    %s -m pip install -i %s %s" % (e, py, PIP_MIRROR, PLAYWRIGHT_SPEC))
    if _python_has_playwright(py):
        print("持久环境就绪：%s" % py)
        return py, None
    return None, "Playwright 安装后验证未通过：%s" % py


def _spawn():
    """拉起 serve.py。返回 (是否成功, 所用解释器路径 或 失败说明)。

    依赖缺失时**自动准备持久环境**（建 venv + 装 Playwright），而不是只弹「请安装」——
    这样才能做到「clone 即用」。
    """
    exe, err = _ensure_playwright_env()
    if not exe:
        return False, ((err or "未找到可用的 Python 环境")
                       + "\n\n（浏览器直接用系统 Edge 或 Chrome，无需下载 Playwright 内核）")
    subprocess.Popen([exe, SERVE, str(PORT)], **_detach_kwargs())
    return True, exe


def _wait_ready(want, tries=24):
    """等服务就绪：优先等版本号匹配；拿不到期望版本时退化为等端口可用。"""
    for _ in range(tries):
        time.sleep(0.3)
        if want is None:
            if _port_in_use():
                return True
            continue
        if _probe_version() == want:
            return True
    return _port_in_use()


def _ensure_running(force=False):
    """确保 8090 上跑的是**当前版本、且取数正常**的服务，返回是否可用。

    为什么不能只看端口：旧进程会一直占着 8090，只判断「端口被占用」就会误判为
    「已在运行」，于是启动器每次都打开旧版页面 —— 表现为页面是新前端、接口却全 404
    （面板报红）。这里改为「端口 + 版本」双校验：版本不符则结束旧进程再拉起新版。

    为什么还要查健康（force=False 时）：**版本相同也不等于可用** —— 实测服务在跑、
    桥的页面却已失效（取数全挂，报 Failed to fetch），此时按版本复用会让用户
    「以为重启了、其实没重启」。故版本相同时再看一眼 /api/health：取数异常就强制重启。
    `force=True`（--restart）跳过上述判断，无条件重启。
    """
    want = _desired_version()
    cur = _probe_version()

    if not force and cur and (want is None or cur == want):
        health = _probe_health()
        if not health or not health.get("degraded"):
            return True                  # 版本一致 + 取数正常 → 复用
        print("服务在跑但取数异常（%s），正在重启…"
              % ((health.get("last_err") or "")[:60] or "未知原因"))
    elif force and _port_in_use():
        print("--restart：强制重启服务…")
    elif _port_in_use():
        if cur is None:
            print("检测到旧版本服务正占用 %d，正在重启到当前版本…" % PORT)
        else:
            print("本地服务版本 %s ≠ 当前技能 %s，正在重启…" % (cur, want))

    if _port_in_use() and not _stop_stale_server():
        print("无法自动结束占用 %d 的进程，请手动关闭后重试。" % PORT, file=sys.stderr)
        return False

    if not os.path.exists(SERVE):
        print("找不到 serve.py：%s" % SERVE, file=sys.stderr)
        return False

    ok, info = _spawn()
    if not ok:
        print(info, file=sys.stderr)
        _notify(info)
        return False
    print("服务解释器：%s" % info)
    return _wait_ready(want)


def gen_vbs():
    """生成双击启动器 VBS 到专门目录（幂等，覆盖同名文件）。"""
    if sys.platform != "win32":
        print("双击启动器仅支持 Windows（当前平台：%s）" % sys.platform, file=sys.stderr)
        return 1
    launcher = os.path.abspath(__file__)
    pythonw = _find_launcher_pythonw() or _find_pythonw()
    # VBS 内容纯 ASCII，避免 wscript 按 GBK 读 UTF-8 中文注释导致的编码坑
    vbs = (
        "Set ws = CreateObject(\"WScript.Shell\")\r\n"
        "ws.Run \"\"\"%s\"\" \"\"%s\"\"\", 0, False\r\n" % (pythonw, launcher)
    )
    os.makedirs(VBS_DIR, exist_ok=True)
    with open(VBS_PATH, "w") as f:
        f.write(vbs)
    print("已生成双击启动器：%s" % VBS_PATH)
    print("以后无需打开 WorkBuddy，双击该文件即可启动工作台（可自行发送到桌面或开始菜单）。")
    return 0


def start_and_open(force=False):
    """默认行为：起服务（必要时重启）+ 打开系统浏览器。"""
    ok = _ensure_running(force=force)
    webbrowser.open(URL)
    return 0 if ok else 1


def create_shortcut_cmd(target):
    """创建快捷方式到桌面 / 开始菜单。返回进程退出码。"""
    ok, msg = create_shortcut(target)
    print(msg)
    return 0 if ok else 1


def main():
    args = sys.argv[1:]
    if "--gen-vbs" in args:
        return gen_vbs()
    if "--create-shortcut" in args:
        idx = args.index("--create-shortcut")
        target = args[idx + 1] if idx + 1 < len(args) else "desktop"
        return create_shortcut_cmd(target)
    return start_and_open(force="--restart" in args)


if __name__ == "__main__":
    sys.exit(main())
