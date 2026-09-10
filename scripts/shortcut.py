#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
创建 Windows 快捷方式（.lnk），指向积分工作台双击启动器（VBS）。

设计要点：
  - 零第三方依赖：通过 subprocess 调用系统自带 PowerShell 完成 .lnk 创建，
    不依赖 pywin32，跨机器（任何带 PowerShell 的 Windows）可用。
  - 仅支持 Windows；其他平台返回 not_supported。
  - 目标位置：desktop（桌面）/ startmenu（开始菜单「所有应用」）。
  - 快捷方式指向 wscript.exe 运行 VBS，与「双击 VBS」行为一致。

供两处复用：
  1. serve.py 的 /api/create-shortcut 端点（工作台页面按钮入口）
  2. launcher.py 的 --create-shortcut 子命令（Agent 对话入口）
"""

import os
import subprocess
import sys
import tempfile

DEFAULT_VBS = os.path.join(
    os.path.expanduser("~"), ".workbuddy", "launchers", "start-credits-dashboard.vbs"
)
DEFAULT_NAME = "WorkBuddy 积分工作台"

# 图标：skill 的 assets/workbuddy-logo.ico（多尺寸青柠图标）。
# 通过 __file__ 同级目录推导，不写死本机路径，跨机器通用。
# 用 abspath 规范化 `..`，输出绝对路径——`.lnk` 的 IconLocation 在解析相对路径时会
# 以快捷方式所在目录（桌面 / 开始菜单）为基准，导致跨目录找不到图标。
_HERE = os.path.dirname(os.path.abspath(__file__))
_ICON = os.path.abspath(os.path.join(_HERE, "..", "assets", "workbuddy-logo.ico"))

# PowerShell 脚本：内容纯 ASCII。
# 参数通过环境变量（WB_SC_*）传入——避免 PowerShell `-File` 模式把含空格路径
# 在命令行按空格截断的问题（用户路径如 `E:\AI Era\...` 之前就因此吃掉了图标）。
_PS = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Target      = $env:WB_SC_TARGET
$VbsPath     = $env:WB_SC_VBS
$DisplayName = $env:WB_SC_NAME
$IconPath    = $env:WB_SC_ICON
if (-not $Target)      { throw 'WB_SC_TARGET not set' }
if (-not $VbsPath)     { throw 'WB_SC_VBS not set' }
if (-not $DisplayName) { throw 'WB_SC_NAME not set' }
if ($Target -eq 'desktop') {
    $dir = [Environment]::GetFolderPath('Desktop')
} elseif ($Target -eq 'startmenu') {
    $dir = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
} else {
    throw "unknown target: $Target"
}
if (-not (Test-Path $VbsPath)) { throw "VBS not found: $VbsPath" }
$lnkPath = Join-Path $dir ($DisplayName + '.lnk')
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = Join-Path $env:WINDIR 'System32\wscript.exe'
$lnk.Arguments = '"' + $VbsPath + '"'
$lnk.WorkingDirectory = Split-Path $VbsPath
# 图标：仅当文件存在且扩展名为 .ico 才设置（Windows 限制快捷方式图标必须是 .ico）。
if ($IconPath -and (Test-Path $IconPath) -and ([IO.Path]::GetExtension($IconPath) -ieq '.ico')) {
    $lnk.IconLocation = $IconPath + ',0'
}
$lnk.Save()
Write-Output $lnkPath
'''


def _find_vbs(vbs_path=None):
    if vbs_path and os.path.exists(vbs_path):
        return vbs_path
    return DEFAULT_VBS


def create_shortcut(target, vbs_path=None, display_name=DEFAULT_NAME):
    """创建 .lnk 快捷方式。返回 (ok: bool, message: str)。"""
    if sys.platform != "win32":
        return False, "仅支持 Windows（当前平台：%s）" % sys.platform
    if target not in ("desktop", "startmenu"):
        return False, "未知目标位置：%s（可选 desktop / startmenu）" % target

    vbs = _find_vbs(vbs_path)
    if not os.path.exists(vbs):
        return False, "启动器 VBS 不存在：%s（请先运行 launcher.py --gen-vbs 生成）" % vbs

    fd, tmp = tempfile.mkstemp(suffix=".ps1", prefix="wb_shortcut_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write(_PS)
        # 通过环境变量把含空格路径传给 PowerShell，避开命令行参数解析。
        env = os.environ.copy()
        env["WB_SC_TARGET"] = target
        env["WB_SC_VBS"] = vbs
        env["WB_SC_NAME"] = display_name
        env["WB_SC_ICON"] = _ICON if _ICON else ""
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", tmp],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, env=env,
        )
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        if r.returncode == 0:
            return True, out or "已创建快捷方式"
        return False, err or out or ("创建失败（退出码 %d）" % r.returncode)
    except FileNotFoundError:
        return False, "未找到 PowerShell，无法创建快捷方式"
    except subprocess.TimeoutExpired:
        return False, "创建快捷方式超时"
    except Exception as e:  # noqa: BLE001
        return False, "创建失败：%r" % e
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


if __name__ == "__main__":
    # 直接运行：python shortcut.py [desktop|startmenu]
    t = sys.argv[1] if len(sys.argv) > 1 else "desktop"
    ok, msg = create_shortcut(t)
    print(msg)
    sys.exit(0 if ok else 1)
