#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
积分工作台 skill 更新器（随 skill 分发）

用途：把**已安装**的本 skill 更新到最新版本，并重启工作台服务
      （服务是 DETACHED 常驻进程，不重启不会加载新代码——这是必须的一步）。

用法：
    python update.py                      # 检查并更新；若工作台服务在跑则自动重启
    python update.py --check              # 只检查是否有更新，不改动任何文件、不重启
    python update.py --force              # 版本号相同也重新安装一遍
    python update.py --source release     # 强制走 GitHub Releases 包
    python update.py --source archive     # 强制走源码包覆盖（git clone → zip）
    python update.py --no-restart         # 更新后不重启工作台服务
    python update.py --repo <url>         # 指定仓库地址
    python update.py --proxy <url>        # 通过指定代理访问 GitHub（默认直连）
    python update.py --token <token>      # GitHub API 令牌（也读 GITHUB_TOKEN / GH_TOKEN）

更新来源（--source auto，默认）：
    - 安装目录含 .git 且有 git 命令 → Git 增量（git pull --ff-only，可回滚）
    - 复制安装（无 .git）           → GitHub Releases 包（推荐）

GitHub Releases 流程（推荐路径）：
    GET /repos/<owner>/<repo>/releases/latest          ← api.github.com
      → 比对 tag 与本地 manifest.yaml 的 version
      → 下载 workbuddy-credits-v<ver>.zip
      → 用同 Release 的 SHA256SUMS.txt 校验 sha256（有则强制校验）
      → 合并式覆盖安装 → 重启服务
    下载走 **API 资产端点**（api.github.com → 302 → release-assets.githubusercontent.com），
    失败再退回 browser_download_url（github.com）。实测国内 github.com 会间歇性不可达，
    而 api.github.com 与 CDN 稳定，故 API 端点优先。
    任意一步失败自动退回源码包路径（git clone --depth 1 → codeload zip），
    故国内网络下即使某个域名不通也能更新。

安全：
    - 用户数据在 ~/.workbuddy/workbuddy-credits-data/（skill 目录**外**），更新不影响
    - 仅覆盖仓库内文件，**不删除**本地已有文件（如生成物 dashboard_data.js）
    - 重启前会校验监听进程确为 python，避免误杀其它占用端口的程序
    - sha256 校验失败**拒绝安装**，不留半成品
    - 下载内置 3 次重试（国内直连偶发连接重置 WinError 10054）
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
DEFAULT_REPO = "https://github.com/SkylerCook/workbuddy-credits-ws"
DEFAULT_BRANCH = "main"
PORT = 8090
# 解压覆盖时跳过的顶层目录（与 skill 运行无关）
IGNORE_TOP = {".git", "dist", "__pycache__"}
# Release 资产里的校验和文件名（不区分大小写）
SUMS_NAMES = {"sha256sums.txt", "sha256sum.txt", "checksums.txt", "sha256sums"}

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


def ver_key(s):
    parts = re.findall(r"\d+", s or "")
    return tuple(int(x) for x in parts)


def cmp_version(local, remote):
    """'same' / 'newer'（远端更新）/ 'older'（远端更旧）/ 'unknown'"""
    a, b = ver_key(local), ver_key(remote)
    if not a or not b:
        return "unknown"
    if a == b:
        return "same"
    return "newer" if b > a else "older"


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


# ---------------------------------------------------------------- 下载 / 解压

def download(url, dest, proxy="", attempts=3, token="", accept=""):
    """下载到本地。国内访问 GitHub 偶发连接重置/超时，故内置重试。"""
    last = None
    for i in range(attempts):
        try:
            handler = urllib.request.ProxyHandler(
                {"http": proxy, "https": proxy} if proxy else {}
            )
            opener = urllib.request.build_opener(handler)
            headers = {"User-Agent": "workbuddy-credits-updater"}
            if accept:
                headers["Accept"] = accept
            if token:
                headers["Authorization"] = "Bearer %s" % token
            req = urllib.request.Request(url, headers=headers)
            with opener.open(req, timeout=90) as resp, open(dest, "wb") as f:
                shutil.copyfileobj(resp, f)
            return
        except Exception as e:  # noqa: BLE001 - 网络异常种类多，统一重试
            last = e
            if i < attempts - 1:
                time.sleep(2 * (i + 1))
    raise last


def download_asset(asset, dest, proxy="", token="", attempts=3):
    """下载 Release 资产，返回实际使用的 URL。

    优先 API 资产端点（api.github.com → 302 → CDN）：实测国内 github.com 会间歇性
    不可达，而 api.github.com 与 release-assets.githubusercontent.com 稳定。
    两者各自重试，全部失败才抛错。
    """
    candidates = []
    if asset.get("url"):
        candidates.append((asset["url"], "application/octet-stream"))
    if asset.get("browser"):
        candidates.append((asset["browser"], ""))
    if not candidates:
        raise RuntimeError("资产 %s 没有可下载地址" % asset.get("name"))
    last = None
    for url, accept in candidates:
        try:
            download(url, dest, proxy, attempts=attempts, token=token, accept=accept)
            return url
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def unpack(zpath, tmpdir):
    """解压 zip 并返回顶层目录（Path）。"""
    if not zipfile.is_zipfile(zpath):
        raise RuntimeError("下载内容不是有效 zip（可能是代理拦截页或 404）")
    with zipfile.ZipFile(zpath) as z:
        z.extractall(tmpdir)
    roots = [p for p in Path(tmpdir).iterdir()
             if p.is_dir() and not p.name.startswith("_")]
    if not roots:
        raise RuntimeError("zip 结构异常：未找到顶层目录")
    return roots[0]


def fetch_zip(url, proxy, tmpdir):
    """下载并解压，返回顶层目录（Path）。"""
    zpath = os.path.join(tmpdir, "pkg.zip")
    download(url, zpath, proxy)
    return unpack(zpath, tmpdir)


def zip_apply(d, src):
    """合并式覆盖：只覆盖仓库内文件，不删除本地多余文件。返回覆盖文件数。"""
    count = 0
    for p in sorted(src.rglob("*")):
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


# ---------------------------------------------------------------- 源码包路径

def zip_url(repo, branch):
    return "%s/archive/refs/heads/%s.zip" % (repo.rstrip("/"), branch)


def _clone_into(dest, repo, branch, proxy):
    """浅克隆到 dest（走 github.com，比 codeload archive 稳定）。"""
    r = _git(["clone", "--depth", "1", "--branch", branch, repo, str(dest)], proxy)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())


def obtain_source(repo, branch, proxy, base_tmp):
    """获取最新源码目录，返回 (src_path, 方式说明)。

    优先 git 浅克隆，无 git 或克隆失败时退回下载 zip。
    """
    clone_dir = Path(base_tmp) / "_clone"
    if shutil.which("git") is not None:
        try:
            _clone_into(clone_dir, repo, branch, proxy)
            return clone_dir, "git clone --depth 1"
        except Exception:
            shutil.rmtree(clone_dir, ignore_errors=True)
    return fetch_zip(zip_url(repo, branch), proxy, base_tmp), "zip 下载"


# ---------------------------------------------------------------- GitHub Releases

def repo_slug(repo):
    """从仓库地址提取 owner/name；无法解析返回空串。"""
    s = (repo or "").strip().rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    for pre in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if s.lower().startswith(pre):
            s = s[len(pre):]
    parts = [p for p in s.split("/") if p]
    if len(parts) < 2:
        return ""
    return "%s/%s" % (parts[-2], parts[-1])


def _api_get(url, proxy="", token="", timeout=60):
    handler = urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {}
    )
    opener = urllib.request.build_opener(handler)
    headers = {
        "User-Agent": "workbuddy-credits-updater",
        "Accept": "application/vnd.github+json",
    }
    if token:
        headers["Authorization"] = "Bearer %s" % token
    req = urllib.request.Request(url, headers=headers)
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def latest_release(repo, proxy="", token=""):
    """取最新 Release 信息；失败抛异常（附可读原因）。"""
    slug = repo_slug(repo)
    if not slug:
        raise RuntimeError("无法解析仓库地址：%s" % repo)
    url = "https://api.github.com/repos/%s/releases/latest" % slug
    try:
        data = _api_get(url, proxy, token)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("仓库 %s 尚无 Release（或仓库不存在）" % slug)
        if e.code in (403, 429):
            raise RuntimeError("GitHub API 访问受限（%d），可用 --token 提高限额" % e.code)
        raise RuntimeError("GitHub API 返回 %d" % e.code)
    tag = (data.get("tag_name") or "").strip()
    assets = [{
        "name": a.get("name") or "",
        "url": a.get("url") or "",                        # API 资产端点（优先）
        "browser": a.get("browser_download_url") or "",   # github.com 直链（兜底）
        "size": a.get("size") or 0,
    } for a in (data.get("assets") or [])]
    return {
        "slug": slug,
        "tag": tag,
        "version": tag.lstrip("vV"),
        "published": (data.get("published_at") or "")[:10],
        "notes": (data.get("body") or "").strip(),
        "html_url": data.get("html_url") or "",
        "assets": assets,
    }


def pick_asset(assets, prefer):
    """按优先级选资产：先精确名，再约定名，最后第一个 .zip。"""
    for want in prefer:
        for a in assets:
            if a["name"] == want:
                return a
    for a in assets:
        if a["name"].lower().endswith(".zip"):
            return a
    return None


def sums_asset(assets):
    for a in assets:
        if a["name"].lower() in SUMS_NAMES:
            return a
    return None


def parse_sums(path):
    """解析 SHA256SUMS：{文件名: sha256}"""
    out = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out[parts[-1].lstrip("*").strip()] = parts[0].strip().lower()
    return out


def release_fetch(rel, proxy, token, tmpdir):
    """下载 Release 的 zip 资产并校验，返回 (zip 路径, 资产名, 校验说明)。"""
    ver = rel["version"]
    asset = pick_asset(rel["assets"], (
        "workbuddy-credits-v%s.zip" % ver,
        "workbuddy-credits.zip",
    ))
    if not asset:
        raise RuntimeError("Release %s 没有可用的 zip 资产" % rel["tag"])

    zpath = os.path.join(tmpdir, "release.zip")
    download_asset(asset, zpath, proxy, token)

    note = "未提供校验文件，已跳过"
    sums = sums_asset(rel["assets"])
    if sums:
        spath = os.path.join(tmpdir, "_SHA256SUMS.txt")
        download_asset(sums, spath, proxy, token)
        want = parse_sums(spath).get(asset["name"])
        if not want:
            note = "校验文件中无 %s 条目，已跳过" % asset["name"]
        else:
            got = sha256_file(zpath)
            if got != want:
                raise RuntimeError(
                    "sha256 校验失败，已中止安装\n    期望 %s\n    实际 %s" % (want, got)
                )
            note = "sha256 校验通过（%s…）" % got[:16]
    return zpath, asset["name"], note


def print_notes(notes, limit=12):
    if not notes:
        return
    lines = [x.rstrip() for x in notes.splitlines()]
    shown = [x for x in lines if x.strip()][:limit]
    print("  发行说明：")
    for x in shown:
        print("    %s" % x)
    if len([x for x in lines if x.strip()]) > limit:
        print("    …（完整内容见 Release 页面）")


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
    ap.add_argument("--force", action="store_true", help="版本号相同也重新安装")
    ap.add_argument("--source", choices=("auto", "release", "git", "archive"),
                    default="auto", help="更新来源（默认 auto）")
    ap.add_argument("--no-restart", action="store_true", help="更新后不重启工作台服务")
    ap.add_argument("--repo", default=DEFAULT_REPO, help="仓库地址")
    ap.add_argument("--branch", default=DEFAULT_BRANCH, help="分支名（源码包方式用）")
    ap.add_argument("--proxy", default="", help="访问 GitHub 的代理（默认直连）")
    ap.add_argument("--token", default=os.environ.get("GITHUB_TOKEN")
                    or os.environ.get("GH_TOKEN") or "",
                    help="GitHub API 令牌（默认读 GITHUB_TOKEN / GH_TOKEN）")
    args = ap.parse_args()

    d = str(SKILL_DIR)
    if not (SKILL_DIR / "SKILL.md").exists():
        print("✗ 未找到 skill 安装目录（%s 下无 SKILL.md）" % d)
        return 1

    pull_mode = has_git_dir(SKILL_DIR)
    has_git_cmd = shutil.which("git") is not None
    old_ver = read_version(d)
    if pull_mode:
        mode_desc = "Git 仓库（增量 pull）"
    elif has_git_cmd:
        mode_desc = "复制安装（Release 包 / git 浅克隆）"
    else:
        mode_desc = "复制安装（Release 包 / zip 下载）"
    source = args.source
    if source == "auto":
        source = "git" if pull_mode else "release"
    print("安装目录：%s" % d)
    print("安装方式：%s" % mode_desc)
    print("当前版本：%s" % old_ver)
    print("")

    # ---------- 只检查 ----------
    if args.check:
        # 以 Release 为版本权威（1 次 API 请求），失败再退回 git/源码包判断
        try:
            rel = latest_release(args.repo, args.proxy, args.token)
            state = cmp_version(old_ver, rel["version"])
            line = "最新版本：%s" % rel["version"]
            if rel["published"]:
                line += "（发布 %s）" % rel["published"]
            print(line)
            if rel["html_url"]:
                print("Release  ：%s" % rel["html_url"])
            print_notes(rel["notes"])
            print("")
            if state == "newer":
                print("● 有更新：%s → %s。运行 `python update.py` 即可更新。" % (old_ver, rel["version"]))
            elif state == "same":
                if args.force:
                    print("✓ 已是最新版本（%s）；已指定 --force，仍会重装。" % old_ver)
                else:
                    print("✓ 已是最新版本（%s）。" % old_ver)
            elif state == "older":
                print("! 本地版本（%s）高于 Release（%s），跳过更新。" % (old_ver, rel["version"]))
            else:
                print("? 版本号无法比较（本地 %s / 远端 %s）。" % (old_ver, rel["version"]))
            if pull_mode:
                has_new, behind, err = git_check(d, args.proxy)
                if err:
                    print("（Git 检查不可用：%s）" % err)
                elif has_new:
                    print("（Git 仓库另有 %d 个未发布提交，可 `git pull` 获取）" % behind)
            return 0
        except Exception as e:
            print("（Release 检查不可用：%s）" % e)
            print("回退到源码包判断…\n")
        if pull_mode:
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
        tmpdir = tempfile.mkdtemp(prefix="wb_upd_")
        try:
            src, how = obtain_source(args.repo, args.branch, args.proxy, tmpdir)
            remote_ver = read_version(src)
            if remote_ver != old_ver:
                print("● 有更新：本地 %s → 远端 %s（方式：%s）。运行 `python update.py` 即可更新。"
                      % (old_ver, remote_ver, how))
            else:
                print("✓ 版本号一致（%s），可能已是最新（方式：%s）。" % (old_ver, how))
            return 0
        except Exception as e:
            print("✗ 检查失败：%s" % e)
            print("  提示：网络不通时请加 --proxy <地址>；或改用 --repo <镜像>。")
            return 1
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ---------- 上游版本（Release 优先，仅用于提示与拦截）----------
    rel = None
    if source == "release":
        try:
            rel = latest_release(args.repo, args.proxy, args.token)
        except Exception as e:
            print("✗ 取 Release 失败：%s" % e)
            print("  提示：网络不通时请加 --proxy <地址>；或改用 --source archive。")
            return 1
    else:
        try:
            rel = latest_release(args.repo, args.proxy, args.token)
        except Exception:
            rel = None

    if rel:
        state = cmp_version(old_ver, rel["version"])
        print("上游版本：%s（%s）" % (rel["version"], rel["tag"]))
        if state == "same" and not args.force:
            print("✓ 已是最新版本（%s），无需更新。加 --force 可强制重装。" % old_ver)
            return 0
        if state == "older" and not args.force:
            print("! 本地版本（%s）高于远端（%s），跳过。加 --force 可强制覆盖。" % (old_ver, rel["version"]))
            return 0
        print("")

    was_running = bool(listener_pids(PORT))

    # ---------- 执行更新（按来源顺序依次尝试，失败自动降级）----------
    tmpdir = tempfile.mkdtemp(prefix="wb_upd_")

    def do_git():
        ok, err, changed, _b, _a = git_update(d, args.proxy)
        if not ok:
            raise RuntimeError((err or "git pull 失败").strip())
        if changed:
            print("已更新 %d 个文件：" % len(changed))
            for f in changed[:20]:
                print("  · %s" % f)
            if len(changed) > 20:
                print("  · …另有 %d 个" % (len(changed) - 20))
        else:
            print("✓ 已是最新，无需更新。")
        return len(changed)

    def do_release():
        if rel is None:
            raise RuntimeError("未能获取 Release 信息（api.github.com 不可达或仓库无 Release）")
        zpath, aname, note = release_fetch(rel, args.proxy, args.token, tmpdir)
        print("下载资产：%s" % aname)
        print("完整性  ：%s" % note)
        src = unpack(zpath, tmpdir)
        n = zip_apply(d, src)
        print("已覆盖 %d 个文件。" % n)
        print_notes(rel["notes"])
        return n

    def do_archive():
        src, how = obtain_source(args.repo, args.branch, args.proxy, tmpdir)
        print("获取方式：%s" % how)
        print("远端版本：%s" % read_version(src))
        n = zip_apply(d, src)
        print("已覆盖 %d 个文件。" % n)
        return n

    if args.source == "auto":
        order = ["git", "release", "archive"] if pull_mode else ["release", "archive"]
    else:
        order = [args.source]
    if "git" in order and not pull_mode:
        shutil.rmtree(tmpdir, ignore_errors=True)
        print("✗ 安装目录不是 Git 仓库（无 .git），无法使用 git 方式；请改用 --source release/archive。")
        return 1

    handlers = {"git": do_git, "release": do_release, "archive": do_archive}
    changed_count, ok_any = 0, False
    try:
        for i, name in enumerate(order):
            try:
                changed_count = handlers[name]()
                ok_any = True
                break
            except Exception as e:  # noqa: BLE001 - 逐级降级
                print("✗ %s 方式失败：%s" % (name, e))
                if i < len(order) - 1:
                    print("  降级到下一个来源…")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if not ok_any:
        print("✗ 全部更新来源均失败。")
        print("  提示：网络不通时加 --proxy <地址>；GitHub API 限流时加 --token <token>；"
              "或改用 --repo <镜像地址>。")
        return 1

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
