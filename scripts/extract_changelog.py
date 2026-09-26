#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 README.md 的「更新日志」里提取指定版本的段落，供 CI 写入 GitHub Release 正文。

为什么需要：`gh release create --generate-notes` 只会列出 PR / commit ——
本项目是直接 push 到 main（无 PR），自动 notes 便只剩一行 "Full Changelog"，
Release 页面等于没有更新说明。README 的更新日志一直有人工维护，直接取它最省事、
也不会两处内容漂移。

用法：
  python scripts/extract_changelog.py v1.4.3           # 打印该版本的 Markdown 正文
  python scripts/extract_changelog.py 1.4.3 --check    # 只校验存在性（缺失则 exit 1）
  python scripts/extract_changelog.py v1.7.2 --since v1.4.3
                                                       # 打印 v1.4.3 之后到 v1.7.2 的**全部**
                                                       # 版本段（用于一次发版涵盖多批改动）

`--since` 解决的是这个问题：本项目节奏是「改完先攒着、验收后再提交 / 发版」，
一个 tag 常涵盖**多个**版本段。若只取单个段，Release 正文就会漏掉前面的批次；
而靠人在 README 里写「本版包含 X~Y」既繁琐、又必然过期。
改用**已发布的 tag 历史**当范围下界（CI 里 `git describe --tags --abbrev=0 HEAD^`），
范围自动准确、无需人工维护。

约定：README 中的版本段落标题形如 `### v1.4.3 — 摘要`，正文截至下一个
`### ` / `## ` 标题之前。
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
README = os.path.join(os.path.dirname(HERE), "README.md")

# 版本段标题：`### v1.2.3 …` 或 `### 1.2.3 …`
_VER_HEAD = r"^###\s+v?%s(?:\s|$|—|-)"
# 是否是一个「版本段」（用来区分更新日志里的版本段与其它 ### 小标题）
_IS_VER = re.compile(r"^###\s+v?\d")


def _ver_head(version):
    v = (version or "").lstrip("vV").strip()
    return re.compile(_VER_HEAD % re.escape(v)) if v else None


def extract(version, readme=README, since=None):
    """返回该版本（或 `since` 之后到该版本之间所有版本段）的 Markdown。

    - `since=None`（默认）：只取该版本一段（原行为）
    - 指定 `since`：从该版本往下取到 `since` 段**之前**为止（`since` 已发布，不含）
    README 里找不到该版本则返回 None。
    """
    head = _ver_head(version)
    if head is None:
        return None
    try:
        with open(readme, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return None

    start = next((i for i, ln in enumerate(lines) if head.match(ln)), None)
    if start is None:
        return None
    stop = _ver_head(since) if since else None

    body = [lines[start]]          # 连标题一起带上：Release 页面能直接看到版本摘要
    for ln in lines[start + 1:]:
        if ln.startswith("## "):
            break                              # 更新日志章节结束
        if ln.startswith("### "):
            if not _IS_VER.match(ln):
                break                          # 非版本段的 ### → 结构意外，就此打住
            # 单段模式：下一个版本段即结束（保持原行为）
            # 范围模式：只有到达下界才结束（下界版本本身不重复输出）
            if stop is None or stop.match(ln):
                break
            body.append(ln)
            continue
        body.append(ln)
    text = "\n".join(body).strip()
    return text or None


def count_versions(text):
    """粗略统计一段 Markdown 里有几个版本段（用于 --check 汇报）。"""
    return len([ln for ln in (text or "").splitlines() if _IS_VER.match(ln)])


def main(argv):
    since = None
    positional = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--since":
            since = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            continue
        if a.startswith("--"):
            i += 1
            continue
        positional.append(a)
        i += 1

    if not positional:
        print("用法：python scripts/extract_changelog.py <vX.Y.Z> [--since <已发布版本>] [--check]",
              file=sys.stderr)
        return 2
    body = extract(positional[0], since=since)
    if body is None:
        print("README 更新日志里找不到版本 %s 的段落" % positional[0], file=sys.stderr)
        return 1
    if "--check" in argv:
        n = count_versions(body)
        scope = ("（含 %d 个版本段，自 %s 之后）" % (n, since)) if since else ""
        print("OK：%s 的说明共 %d 字符%s" % (positional[0], len(body), scope))
        return 0
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
