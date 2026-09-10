#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
积分工作台 skill 更新器（随 skill 分发）

用途：把**已安装**的本 skill 更新到 GitHub 仓库最新版本，并重启工作台服务
      （服务是 DETACHED 常驻进程，不重启不会加载新代码——这是必须的一步）。

用法：
    python update.py                 # 检查并更新；若工作台服务在跑则自动重启
    python update.py --check         # 只检查是否有更新，不改动任何文件、不重启
    python update.py --no-restart    # 更新后不重启工作台服务
    python update.py --repo <url>    # 指定仓库地址
    python update.py --proxy <url>   # 通过指定代理访问 GitHub（默认直连）

更新方式（自动检测）：
    - 安装目录含 .git 且有 git 命令 → git pull --ff-only（增量、可回滚）
    - 否则 → 下载 <repo>/archive/refs/heads/<branch>.zip 解压覆盖（合并式）
      注意：GitHub 直连在国内常不可达，必要时用 --proxy。

安全：
    - 用户数据在 ~/.workbuddy/workbuddy-credits-data/（skill 目录**外**），更新不影响
    - 仅覆盖仓库内文件，**不删除**本地已有文件（如生成物 dashboard_data.js）
    - 重启前会校验监听进程确为 python，避免误杀其它占用端口的程序
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
DEFAULT_REPO = "https://github.com/SkylerCook/workbuddy-credits-ws"
DEFAULT_BRANCH = "main"
PORT = 8090
# 解压覆盖时跳过的顶层目录（与 skill 运行无关）
IGNORE_TOP = {".git", "dist"}

_PROXY_KEYS = (
    "http_proxy", "https_proxy", "all_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
)


# ---------------------------------------------------------------- 读取版本

def read_version(d):
    """从 manifest.yaml 读取 version（不引入 yaml 依赖）。"""
    mf = Path(d) / "manifest.yaml"
    if not mf.exists():
        return "?"
    for line in mf.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s.startswith("version:"):
            return s.split(":", 1)[1].strip().strip("\"'")
    return "?"


# ---------------------------------------------------------------- git 方式

def has_git_dir(d):
    return (Path(d) / ".git").exists() and shutil.which("git") is not None


def _git(args, proxy="", cwd=None):
    """执行 git 命令：清掉环境代理，用 -c 显式指定（默认直连）。"""
    env = os.environ.copy()
    for k in _PROXY_KEYS:
        env.pop(k, None)
    cmd = [
        "git",
        "-c", "http.proxy=%s" % (proxy or ""),
        "-c", "https.proxy=%s" % (proxy or ""),
    ] + args
    return subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )


def git_head(d, proxy=""):
    return _git(["rev-parse", "HEAD"], proxy, cwd=d).stdout.strip()


def git_check(d, proxy=""):
    """(是否有更新, 落后提交数, 错误信息)"""
    r = _git(["fetch", "--quiet"], proxy, cwd=d)
    if r.returncode != 0:
        return False, 0, (r.stderr or r.stdout).strip()
    up = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], proxy, cwd=d)
    if up.returncode != 0 or not up.stdout.strip():
        return False, 0, "未配置上游分支（upstream）"
    r2 = _git(["rev-list", "--count", "HEAD..@{u}"], proxy, cwd=d)
    try:
        behind = int(r2.stdout.strip())
    except ValueError:
        behind = 0
    return behind > 0, behind, ""


def git_update(d, proxy=""):
    """(成功, 错误信息, 变更文件列表, 旧 HEAD, 新 HEAD)"""
    before = git_head(d, proxy)
    r = _git(["pull", "--ff-only"], proxy, cwd=d)
    after = git_head(d, proxy)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip(), [], before, after
    changed = []
    if before and after and before != after:
        d2 = _git(["diff", "--name-only", before, after], proxy, cwd=d)
        changed = [x.strip() for x in d2.stdout.splitlines() if x.strip()]
    return True, "", changed, before, after


# ---------------------------------------------------------------- zip 方式

def zip_url(repo, branch):
    return "%s/archive/refs/heads/%s.zip" % (repo.rstrip("/"), branch)


def download(url, dest, proxy=""):
    handler = urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {}
    )
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(url, headers={"User-Agent": "workbuddy-credits-updater"})
    with opener.open(req, timeout=90) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def fetch_zip(url, proxy, tmpdir):
    """下载并解压，返回顶层目录（Path）。"""
    zpath = os.path.join(tmpdir, "pkg.zip")
    download(url, zpath, proxy)
    if not zipfile.is_zipfile(zpath):
        raise RuntimeError("下载内容不是有效 zip（可能是代理拦截页或 404）")
    with zipfile.ZipFile(zpath) as z:
        z.extractall(tmpdir)
    roots = [p for p in Path(tmpdir).iterdir() if p.is_dir()]
    if not roots:
        raise RuntimeError("zip 结构异常：未找到顶层目录")
    return roots[0]


def zip_apply(d, src):
    """合并式覆盖：只覆盖仓库内文件，不删除本地多余文件。返回覆盖文件数。"""
    count = 0
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        if rel.parts and rel.parts[0] in IGNORE_TOP:
            continue
        dst = Path(d) / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
        count += 1
    return count


# ---------------------------------------------------------------- 服务重启

def listener_pids(port):
    pids = set()
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=20,
            ).stdout
            for line in out.splitlines():
                if "LISTENING" not in line.upper():
                    continue
                if (":%d" % port) not in line:
                    continue
                tok = line.split()
                if tok and tok[-1].isdigit():
                    pids.add(int(tok[-1]))
        else:
            out = subprocess.run(
                ["lsof", "-ti", "tcp:%d" % port, "-sTCP:LISTEN"],
                capture_output=True, text=True, errors="replace", timeout=20,
            ).stdout
            pids.update(int(x) for x in out.split() if x.strip().isdigit())
    except Exception:
        pass
    return pids


def is_python_proc(pid):
    """确认进程是 python，避免误杀其它占用 8090 的程序。"""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=20,
            ).stdout
            return "python" in out.lower()
        comm = Path("/proc/%d/comm" % pid)
        if comm.exists():
            return "python" in comm.read_text(errors="replace").lower()
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="], capture_output=True,
            text=True, errors="replace", timeout=15,
        ).stdout
        return "python" in out.lower()
    except Exception:
        return False


def stop_service(port):
    killed = []
    for pid in listener_pids(port):
        if not is_python_proc(pid):
            continue
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=20)
            else:
                os.kill(pid, 15)
            killed.append(pid)
        except Exception:
            pass
    return killed


def start_service():
    """复用 launcher 的独立进程启动逻辑（DETACHED 常驻）。"""
    sys.path.insert(0, str(HERE))
    import launcher
    return launcher._ensure_running()


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="更新已安装的 workbuddy-credits skill")
    ap.add_argument("--check", action="store_true", help="只检查是否有更新，不做改动")
    ap.add_argument("--no-restart", action="store_true", help="更新后不重启工作台服务")
    ap.add_argument("--repo", default=DEFAULT_REPO, help="仓库地址")
    ap.add_argument("--branch", default=DEFAULT_BRANCH, help="分支名")
    ap.add_argument("--proxy", default="", help="访问 GitHub 的代理（默认直连）")
    args = ap.parse_args()

    d = str(SKILL_DIR)
    if not (SKILL_DIR / "SKILL.md").exists():
        print("✗ 未找到 skill 安装目录（%s 下无 SKILL.md）" % d)
        return 1

    use_git = has_git_dir(SKILL_DIR)
    old_ver = read_version(d)
    print("安装目录：%s" % d)
    print("安装方式：%s" % ("Git 仓库（增量更新）" if use_git else "复制安装（zip 覆盖）"))
    print("当前版本：%s" % old_ver)
    print("")

    # ---------- 只检查 ----------
    if args.check:
        if use_git:
            has_new, behind, err = git_check(d, args.proxy)
            if err:
                print("✗ 检查失败：%s" % err)
                print("  提示：如本机需代理访问 GitHub，请加 --proxy <地址>；或改用 --repo <镜像>")
                return 1
            if has_new:
                print("● 有更新：落后远端 %d 个提交。运行 `python update.py` 即可更新。" % behind)
            else:
                print("✓ 已是最新版本（%s）。" % old_ver)
            return 0
        # zip 方式：下载后比对版本号
        tmpdir = tempfile.mkdtemp(prefix="wb_upd_")
        try:
            src = fetch_zip(zip_url(args.repo, args.branch), args.proxy, tmpdir)
            remote_ver = read_version(src)
            if remote_ver != old_ver:
                print("● 有更新：本地 %s → 远端 %s。运行 `python update.py` 即可更新。"
                      % (old_ver, remote_ver))
            else:
                print("✓ 版本号一致（%s），可能已是最新。" % old_ver)
            return 0
        except Exception as e:
            print("✗ 检查失败：%s" % e)
            print("  提示：GitHub 直连可能不通，请加 --proxy <地址>。")
            return 1
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ---------- 更新前记录服务状态 ----------
    was_running = bool(listener_pids(PORT))

    # ---------- 执行更新 ----------
    changed_count = 0
    if use_git:
        ok, err, changed, _b, _a = git_update(d, args.proxy)
        if not ok:
            print("✗ 更新失败：%s" % err)
            print("  提示：本地若有未提交改动会阻止 --ff-only 更新，请先处理改动。")
            print("       如网络不通，请加 --proxy <地址>。")
            return 1
        changed_count = len(changed)
        if changed_count:
            print("已更新 %d 个文件：" % changed_count)
            for f in changed[:20]:
                print("  · %s" % f)
            if changed_count > 20:
                print("  · …另有 %d 个" % (changed_count - 20))
        else:
            print("✓ 已是最新，无需更新。")
    else:
        tmpdir = tempfile.mkdtemp(prefix="wb_upd_")
        try:
            src = fetch_zip(zip_url(args.repo, args.branch), args.proxy, tmpdir)
            remote_ver = read_version(src)
            print("远端版本：%s" % remote_ver)
            changed_count = zip_apply(d, src)
            print("已覆盖 %d 个文件。" % changed_count)
        except Exception as e:
            print("✗ 更新失败：%s" % e)
            print("  提示：GitHub 直连可能不通，请加 --proxy <地址>。")
            return 1
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    new_ver = read_version(d)
    print("")
    print("版本：%s → %s" % (old_ver, new_ver))

    # ---------- 重启服务（关键：DETACHED 进程不会自动加载新代码）----------
    if args.no_restart:
        print("服务：已跳过重启（--no-restart）。注意：如服务正在运行，需手动重启才能加载新代码。")
        return 0

    if not was_running:
        print("服务：更新前未在运行，未做改动。下次打开工作台即为新版本。")
        return 0

    killed = stop_service(PORT)
    if not killed:
        print("服务：检测到 %d 端口被占用，但不是 python 进程，未处理。请手动确认。" % PORT)
        return 0
    print("服务：已停止旧进程 %s，正在重启…" % ", ".join(str(p) for p in killed))
    ok = start_service()
    print("服务：%s" % ("已重启（新代码已加载）" if ok else "重启失败，请双击启动器手动启动"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
