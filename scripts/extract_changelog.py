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

约定：README 中的版本段落标题形如 `### v1.4.3 — 摘要`，正文截至下一个
`### ` / `## ` 标题之前。
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
README = os.path.join(os.path.dirname(HERE), "README.md")


def extract(version, readme=README):
    """返回该版本的更新日志 Markdown；README 里没有该版本则返回 None。"""
    ver = (version or "").lstrip("vV").strip()
    if not ver:
        return None
    try:
        with open(readme, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return None

    head = re.compile(r"^###\s+v?%s(?:\s|$|—|-)" % re.escape(ver))
    start = next((i for i, ln in enumerate(lines) if head.match(ln)), None)
    if start is None:
        return None

    body = [lines[start]]          # 连标题一起带上：Release 页面能直接看到版本摘要
    for ln in lines[start + 1:]:
        if ln.startswith("### ") or ln.startswith("## "):
            break
        body.append(ln)
    text = "\n".join(body).strip()
    return text or None


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print("用法：python scripts/extract_changelog.py <vX.Y.Z> [--check]", file=sys.stderr)
        return 2
    body = extract(args[0])
    if body is None:
        print("README 更新日志里找不到版本 %s 的段落" % args[0], file=sys.stderr)
        return 1
    if "--check" in argv:
        print("OK：%s 的说明共 %d 字符" % (args[0], len(body)))
        return 0
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
