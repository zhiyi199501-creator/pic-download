#!/usr/bin/env python3
"""Simple web UI for image downloader."""

from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from download_images import default_output_dir, is_zhihu_site, iter_download

app = Flask(__name__)


def format_request_error(exc: requests.RequestException, url: str = "") -> str:
    raw = str(exc)
    lowered = raw.lower()

    if "知乎拒绝访问" in raw:
        return raw
    if "nameresolutionerror" in lowered or "nodename nor servname provided" in lowered:
        return (
            "域名解析失败（DNS）。请检查网址是否正确，或稍后重试。"
            "如果其他网站也失败，请检查本机网络/DNS 设置。"
        )
    if "proxyerror" in lowered or "unable to connect to proxy" in lowered:
        return "代理连接失败。请关闭系统代理后重试，或切换网络。"
    if "403" in lowered or (hasattr(exc, "response") and getattr(exc.response, "status_code", None) == 403):
        if is_zhihu_site(url):
            return (
                "知乎拒绝访问（403）。请先在浏览器登录知乎，打开该文章后复制 Cookie，"
                "粘贴到下方 Cookie 输入框再下载。"
            )
        return "目标网站拒绝访问（403）。可能需要登录、Cookie 或特定请求头。"

    return f"下载失败：{exc}"


def resolve_output_dir(output: str) -> Path:
    if output.strip():
        return Path(output.strip()).expanduser().resolve()
    return default_output_dir().resolve()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/download", methods=["POST"])
def api_download():
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url", "")).strip()
    output = str(payload.get("output", "")).strip()
    cookie = str(payload.get("cookie", "")).strip()

    if not url:
        return jsonify({"type": "error", "message": "请输入网址。"}), 400

    @stream_with_context
    def generate():
        try:
            for event in iter_download(
                url=url,
                output=output or None,
                timeout=60,
                cookie=cookie or None,
            ):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except requests.RequestException as exc:
            yield json.dumps(
                {"type": "error", "message": format_request_error(exc, url=url)},
                ensure_ascii=False,
            ) + "\n"
        except Exception as exc:  # noqa: BLE001
            yield json.dumps(
                {"type": "error", "message": f"发生异常：{exc}"},
                ensure_ascii=False,
            ) + "\n"

    return Response(
        generate(),
        mimetype="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    payload = request.get_json(silent=True) or {}
    path_value = str(payload.get("path", "")).strip()
    url = str(payload.get("url", "")).strip()
    output = str(payload.get("output", "")).strip()

    if path_value:
        target = Path(path_value).expanduser().resolve()
    elif url or output:
        target = resolve_output_dir(output)
    else:
        return jsonify({"ok": False, "message": "缺少目录路径。"}), 400

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return jsonify({"ok": False, "message": f"无法创建目录：{exc}"}), 400

    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["open", str(target)], check=True)
        elif system == "Windows":
            os.startfile(str(target))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(target)], check=True)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": f"打开目录失败：{exc}"}), 500

    return jsonify({"ok": True, "path": str(target)})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True, threaded=True)
