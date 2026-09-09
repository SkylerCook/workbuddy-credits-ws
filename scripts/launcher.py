#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
积分工作台启动器（通用，随 skill 分发）

用途：
  1. 默认（无参数）：启动服务（若未运行）+ 打开系统浏览器
     —— 供「双击 VBS」场景使用，服务以独立进程（DETACHED）常驻，脱离 WorkBuddy 桌面端
  2. --gen-vbs：探测本机 pythonw 路径，生成双击启动器 VBS 到专门目录
     —— 供 Agent 首次打开工作台时顺带生成（幂等），告知用户以后可双击启动

设计要点：
  - 零硬编码：serve.py 用 __file__ 同级定位；pythonw 用 sys.executable 同目录推导
  - VBS 生成到 ~/.workbuddy/launchers/（本地专属，不进 skill 源码、不随更新覆盖、不打进分发包）
"""

import os
import socket
import subprocess
import sys
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
SERVE = os.path.join(HERE, "serve.py")
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


def _port_in_use():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", PORT))
        s.close()
        return True
    except Exception:
        return False


def _ensure_running():
    """若服务未运行则 DETACHED 启动并等待就绪。返回是否已在/已成功运行。"""
    if _port_in_use():
        return True
    if not os.path.exists(SERVE):
        print("找不到 serve.py：%s" % SERVE, file=sys.stderr)
        return False
    subprocess.Popen([_find_pythonw(), SERVE, str(PORT)], **_detach_kwargs())
    for _ in range(10):
        time.sleep(0.3)
        if _port_in_use():
            return True
    return False


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


def main():
    if "--gen-vbs" in sys.argv[1:]:
        return gen_vbs()
    return start_and_open()


if __name__ == "__main__":
    sys.exit(main())
