#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
积分工作台启动器（通用，随 skill 分发）

用途：
  1. 默认（无参数）：启动服务（若未运行 / 版本过旧则接管）+ 打开系统浏览器
     —— 供「双击 VBS」场景使用，服务以独立进程（DETACHED）常驻，脱离 WorkBuddy 桌面端
  2. --gen-vbs：探测本机 pythonw 路径，生成双击启动器 VBS 到专门目录
     —— 供 Agent 首次打开工作台时顺带生成（幂等），告知用户以后可双击启动
  3. --create-shortcut <desktop|startmenu>：创建快捷方式到桌面 / 开始菜单「所有应用」
     —— 供 Agent 对话入口（复用 shortcut.py）

设计要点：
  - 零硬编码：serve.py 用 __file__ 同级定位；pythonw 用 sys.executable 同目录推导
  - VBS 生成到 ~/.workbuddy/launchers/（本地专属，不进 skill 源码、不随更新覆盖、不打进分发包）
  - 「端口占用」不等于「服务可用」：旧进程会一直占着 8090，若只看端口就会误判为
    「已在运行」，导致永远打开旧版页面。故改为「端口 + 版本」双校验（见 _ensure_running）。
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


def _python_has_playwright(exe):
    """探测某解释器是否装了 Playwright（浏览器桥的硬依赖）。"""
    if not exe:
        return False
    if (os.path.sep in exe or ":" in exe) and not os.path.exists(exe):
        return False
    try:
        kw = {"capture_output": True, "timeout": 60}
        if sys.platform == "win32":
            kw["creationflags"] = _NO_WINDOW
        return subprocess.run([exe, "-c", "import playwright"], **kw).returncode == 0
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


def _candidate_interpreters():
    """按优先级列出候选解释器。

    `python.exe` 排在 `pythonw.exe` 前面 —— 带控制台的解释器里 Playwright 跑得更稳，
    出错也更容易落进日志。
    """
    seen, out = set(), []

    def add(p):
        if p and p not in seen:
            seen.add(p)
            out.append(p)

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


def _notify(msg):
    """双击场景没有控制台 —— 弹系统提示框，否则用户只看到「转圈」而无从得知原因。"""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, "积分工作台 · 启动失败", 0x30)
            return
        except Exception:
            pass
    print(msg, file=sys.stderr)


def _spawn():
    """拉起 serve.py。返回 (是否成功, 所用解释器路径 或 失败说明)。"""
    exe = _find_serve_python()
    if not exe:
        return False, ("未找到安装了 Playwright 的 Python 解释器 —— 浏览器桥依赖它取数。\n\n"
                       "请在命令行执行一次：\n    pip install playwright\n\n"
                       "（浏览器直接用系统 Chrome，无需再下载内核）")
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


def _ensure_running():
    """确保 8090 上跑的是**当前版本**的服务，返回是否可用。

    为什么不能只看端口：旧进程会一直占着 8090，只判断「端口被占用」就会误判为
    「已在运行」，于是启动器每次都打开旧版页面 —— 表现为页面是新前端、接口却全 404
    （面板报红）。这里改为「端口 + 版本」双校验：版本不符则结束旧进程再拉起新版。
    """
    want = _desired_version()
    cur = _probe_version()

    if cur and (want is None or cur == want):
        return True                      # 已是最新版，直接复用

    if _port_in_use():
        if cur is None:
            print("检测到旧版本服务正占用 %d，正在重启到当前版本…" % PORT)
        else:
            print("本地服务版本 %s ≠ 当前技能 %s，正在重启…" % (cur, want))
        if not _stop_stale_server():
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
    pythonw = _find_pythonw()
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


def start_and_open():
    """默认行为：起服务 + 打开系统浏览器。"""
    ok = _ensure_running()
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
    return start_and_open()


if __name__ == "__main__":
    sys.exit(main())
