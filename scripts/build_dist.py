#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包发布物（本地与 GitHub Actions 共用，零第三方依赖）。

用法：
    python scripts/build_dist.py                   # 按 manifest.yaml 的版本打包到 dist/
    python scripts/build_dist.py --out dist        # 指定输出目录
    python scripts/build_dist.py --print-version   # 只打印版本号（供 CI 校验 tag）

产物：
    dist/workbuddy-credits-v<version>.zip    版本化包（Release 主资产）
    dist/workbuddy-credits.zip               同名稳定包（releases/latest/download 永久直链）
    dist/SHA256SUMS.txt                      两者的 sha256 校验和

设计要点：
    - **白名单式收集**：只打包 SKILL.md / manifest.yaml / README.md / dashboard.html /
      .gitignore / .gitattributes / assets/ / references/ / scripts/*.py（排除本脚本自身）
      → 用户数据（dashboard_data.js、dashboard_inline.html）、.git、dist、__pycache__
        以及用户自建的其它文件永远不会进包
    - zip 内顶层目录固定为 workbuddy-credits/，解压即得到可用的 skill 目录
    - 文本文件统一以 **LF** 写入 zip（与 .gitattributes 的 eol=lf 一致）
      → 打包结果不依赖本地检出状态，Windows 的 core.autocrlf 也影响不到产物
    - 打包前**自动清理 dist/ 内其它版本的 zip**，避免目录里堆旧包
    - 条目按名称排序、时间戳统一取当次 git 提交时间
      → 同一次提交、相同 Python/zlib 版本下可复现出相同字节
        （zlib 版本不同时压缩流可能不同，故跨环境不保证字节一致；
          sha256 以随包发布的 SHA256SUMS.txt 为准）
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TOP_DIR = "workbuddy-credits"          # zip 内顶层目录名（须与安装目录同名）
STABLE_NAME = "workbuddy-credits.zip"  # 稳定文件名（latest 直链用）

# 顶层单文件
INCLUDE_FILES = (
    "SKILL.md",
    "manifest.yaml",
    "README.md",
    "dashboard.html",
    ".gitignore",
    ".gitattributes",
)
# 顶层目录（递归收集）
INCLUDE_DIRS = ("assets", "references", "scripts")
# 打包时排除（相对顶层目录的）文件名 / 目录名 / 后缀
EXCLUDE_NAMES = {"build_dist.py", ".DS_Store", "Thumbs.db"}
EXCLUDE_DIRS = {"__pycache__"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".log")

# 视为文本的后缀（打包时统一转 LF）；不在表内的一律按二进制原样写入
TEXT_SUFFIX = {
    ".md", ".yaml", ".yml", ".html", ".htm", ".css", ".js", ".mjs", ".cjs",
    ".py", ".txt", ".json", ".xml", ".svg", ".ini", ".cfg", ".toml", ".tsv", ".csv",
    ".gitignore", ".gitattributes", ".editorconfig",
}
VERSIONED_RE = re.compile(r"^workbuddy-credits-v.+\.zip$")

ZIP_EPOCH_MIN = 315532800  # 1980-01-01，zip 格式时间戳下限


# ------------------------------------------------------------------ 版本号

def read_version(d=ROOT):
    """从 manifest.yaml 读取 version（不引入 yaml 依赖）。"""
    mf = Path(d) / "manifest.yaml"
    if not mf.exists():
        raise SystemExit("✗ 未找到 manifest.yaml（%s）" % mf)
    text = mf.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^version:\s*(\S+)", text, re.M)
    if not m:
        raise SystemExit("✗ manifest.yaml 中未找到 version 字段")
    return m.group(1).strip().strip("\"'")


# ------------------------------------------------------------------ 收集文件

def collect():
    """返回 (文件列表, 缺失的白名单项)。白名单缺项只警告，不失败。"""
    files, missing = [], []
    for name in INCLUDE_FILES:
        p = ROOT / name
        (files if p.is_file() else missing).append(p)
    for d in INCLUDE_DIRS:
        base = ROOT / d
        if not base.is_dir():
            missing.append(base)
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel_parts = p.relative_to(base).parts
            if any(part in EXCLUDE_DIRS for part in rel_parts):
                continue
            if p.name in EXCLUDE_NAMES or p.suffix.lower() in EXCLUDE_SUFFIX:
                continue
            files.append(p)
    return sorted(set(files)), missing


def commit_time():
    """当次提交时间（epoch）。非 git 环境退回 SOURCE_DATE_EPOCH / 固定值。"""
    try:
        r = subprocess.run(
            ["git", "log", "-1", "--format=%ct"], cwd=ROOT,
            capture_output=True, text=True, timeout=15,
        )
        ts = int(r.stdout.strip())
        if ts > 0:
            return max(ts, ZIP_EPOCH_MIN)
    except Exception:
        pass
    try:
        return max(int(os.environ.get("SOURCE_DATE_EPOCH", "")), ZIP_EPOCH_MIN)
    except ValueError:
        return ZIP_EPOCH_MIN


# ------------------------------------------------------------------ 打包

def is_text_file(p: Path) -> bool:
    """按后缀/文件名判定是否文本（宁缺勿滥，未命中即当二进制原样处理）。"""
    return p.suffix.lower() in TEXT_SUFFIX or p.name.lower() in TEXT_SUFFIX


def read_for_zip(p: Path) -> bytes:
    """读取待打包字节。文本统一为 LF，使产物不受本地检出状态影响
    （Windows 默认 core.autocrlf=true 会把检出文件变成 CRLF）。"""
    data = p.read_bytes()
    if b"\r\n" in data and is_text_file(p):
        data = data.replace(b"\r\n", b"\n")
    return data


def clean_stale(out: Path, keep: Path, quiet=False):
    """删除 out 目录内其它版本的 workbuddy-credits-v*.zip（稳定包与校验文件保留）。"""
    removed = []
    for f in sorted(out.glob("workbuddy-credits-v*.zip")):
        if f.name == keep.name or not VERSIONED_RE.match(f.name):
            continue
        try:
            f.unlink()
            removed.append(f.name)
        except OSError as e:
            print("  ! 清理失败 %s：%s" % (f.name, e))
    if removed and not quiet:
        print("已清理 %d 个旧版本包：%s" % (len(removed), "、".join(removed)))
    return removed


def write_zip(zip_path, files, ts):
    date_time = time.gmtime(ts)[:6]
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in files:
            arc = "%s/%s" % (TOP_DIR, p.relative_to(ROOT).as_posix())
            info = zipfile.ZipInfo(arc, date_time=date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o644 & 0xFFFF) << 16  # 普通文件权限
            z.writestr(info, read_for_zip(p))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ 主流程

def main():
    ap = argparse.ArgumentParser(description="打包 workbuddy-credits 发布物")
    ap.add_argument("--out", default=str(ROOT / "dist"), help="输出目录（默认 dist/）")
    ap.add_argument("--version", default="", help="覆盖版本号（默认取 manifest.yaml）")
    ap.add_argument("--print-version", action="store_true", help="只打印版本号后退出")
    args = ap.parse_args()

    version = args.version or read_version()
    if args.print_version:
        print(version)
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    files, missing = collect()
    if not files:
        print("✗ 未收集到任何文件，检查是否在仓库根目录运行")
        return 1
    for m in missing:
        print("  ! 白名单项不存在，已跳过：%s" % m)

    ts = commit_time()
    versioned = out / ("workbuddy-credits-v%s.zip" % version)
    stable = out / STABLE_NAME

    write_zip(versioned, files, ts)
    stable.write_bytes(versioned.read_bytes())
    removed = clean_stale(out, versioned, quiet=True)

    digest = sha256_file(versioned)
    (out / "SHA256SUMS.txt").write_text(
        "%s  %s\n%s  %s\n" % (digest, versioned.name, digest, stable.name),
        encoding="utf-8",
    )

    packed = versioned.stat().st_size
    raw = sum(p.stat().st_size for p in files)
    print("版本：%s" % version)
    print("文件：%d 个（%s）" % (len(files), "、".join(
        sorted({p.relative_to(ROOT).parts[0] for p in files})
    )))
    print("产物：")
    for f in (versioned, stable, out / "SHA256SUMS.txt"):
        print("  · %-40s %8.1f KB" % (f.name, f.stat().st_size / 1024.0))
    if removed:
        print("旧包：已清理 %d 个（%s）" % (len(removed), "、".join(removed)))
    print("sha256：%s" % digest)
    print("       原始 %.1f MB → 压缩 %.1f KB" % (raw / 1048576.0, packed / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
