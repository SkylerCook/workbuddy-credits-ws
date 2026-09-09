#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
serve.py —— 积分工作台本地服务：提供可刷新的工作台。

用法：
  python serve.py [端口]          # 默认 8090
  浏览器打开 http://127.0.0.1:8090

说明：
  页面「刷新」按钮会请求 /api/data，服务端重新拉取最新积分/消耗数据并返回，
  无需手动运行 render。仅监听 127.0.0.1，数据不出本机。
"""

import json
import os
import sys
import http.server
import socketserver
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import workbuddy_credits as wc  # noqa: E402
from shortcut import create_shortcut  # noqa: E402

ASSETS_DIR = os.path.join(SKILL_DIR, "assets")
DASHBOARD_HTML = os.path.join(SKILL_DIR, "dashboard.html")

# 静态资源 MIME 映射（缺省走 application/octet-stream，避免被误判成 JS）
MIME = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


def _mime_for(path):
    _, ext = os.path.splitext(path.lower())
    return MIME.get(ext, "application/octet-stream")


def build_json():
    login = wc.load_login()
    if not login:
        return {"error": "未找到 WorkBuddy 登录态，请先登录客户端。"}
    token, uid, account, _ = login
    data, err = wc.build_dashboard_data(token, uid, account)
    if err:
        return {"error": err}
    return data


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html", "/dashboard.html"):
            if os.path.exists(DASHBOARD_HTML):
                with open(DASHBOARD_HTML, "rb") as f:
                    self._send(f.read(), "text/html; charset=utf-8")
            else:
                self._send(b"dashboard.html not found", "text/plain", 404)
        elif path == "/api/data":
            data = build_json()
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._send(body, "application/json; charset=utf-8")
        elif path == "/api/create-shortcut":
            qs = parse_qs(urlparse(self.path).query)
            target = (qs.get("target") or ["desktop"])[0]
            ok, msg = create_shortcut(target)
            body = json.dumps({"ok": ok, "message": msg}, ensure_ascii=False).encode("utf-8")
            self._send(body, "application/json; charset=utf-8", 200 if ok else 400)
        elif path == "/dashboard_data.js":
            data = build_json()
            body = ("window.CREDITS_DATA = "
                    + json.dumps(data, ensure_ascii=False, indent=2) + ";\n").encode("utf-8")
            self._send(body, "application/javascript; charset=utf-8")
        elif path.startswith("/assets/"):
            name = os.path.basename(path)
            fp = os.path.join(ASSETS_DIR, name)
            if os.path.exists(fp):
                with open(fp, "rb") as f:
                    self._send(f.read(), _mime_for(name))
            else:
                self._send(b"not found", "text/plain", 404)
        else:
            self._send(b"not found", "text/plain", 404)

    def log_message(self, *args):
        pass  # 静默访问日志


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    with socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler) as httpd:
        print("积分工作台服务已启动：http://127.0.0.1:%d  （Ctrl+C 停止）" % port)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止。")


if __name__ == "__main__":
    main()
