#!/usr/bin/env python3
"""把音频变成可扫码播放的二维码。

二维码装不下完整音频文件，因此这里把音频托管在本机，
二维码指向一个自动播放页面。同一 Wi-Fi 下的手机可直接扫码收听。
若要给外网的人听，请用公网 URL 模式，或把本服务暴露到公网。
"""

from __future__ import annotations

import io
import os
import socket
import uuid
from pathlib import Path

import qrcode
from flask import Flask, abort, redirect, render_template_string, request, send_file, url_for
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

ALLOWED_EXT = {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".webm"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024  # 80 MB

INDEX_HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>音频转二维码</title>
  <style>
    :root { color-scheme: light dark; }
    body {
      font-family: system-ui, sans-serif;
      max-width: 720px;
      margin: 40px auto;
      padding: 0 20px;
      line-height: 1.5;
    }
    h1 { font-size: 1.5rem; }
    .card {
      border: 1px solid #8884;
      border-radius: 12px;
      padding: 20px;
      margin: 16px 0;
    }
    label { display: block; margin: 12px 0 6px; font-weight: 600; }
    input[type=text], input[type=file] { width: 100%; box-sizing: border-box; }
    button {
      margin-top: 16px;
      padding: 10px 18px;
      border: 0;
      border-radius: 8px;
      background: #2563eb;
      color: #fff;
      cursor: pointer;
      font-size: 1rem;
    }
    .hint { color: #666; font-size: 0.9rem; }
    .error { color: #b91c1c; }
  </style>
</head>
<body>
  <h1>音频转二维码</h1>
  <p>扫描二维码后打开播放页，即可听到对应音频。二维码里存的是<strong>播放链接</strong>，不是音频文件本身。</p>

  <div class="card">
    <h2>方式一：上传本地音频（局域网扫码）</h2>
    <p class="hint">手机需和这台电脑连同一 Wi-Fi。播放地址：{{ base_url }}</p>
    {% if error %}<p class="error">{{ error }}</p>{% endif %}
    <form method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data">
      <label>选择音频文件</label>
      <input type="file" name="audio" accept="audio/*" required>
      <button type="submit">生成二维码</button>
    </form>
  </div>

  <div class="card">
    <h2>方式二：已有公网音频链接</h2>
    <p class="hint">把音频放到网盘/对象存储/网站上，把可直接打开的链接贴到这里。任意联网手机都能扫。</p>
    <form method="post" action="{{ url_for('from_url') }}">
      <label>音频或播放页 URL</label>
      <input type="text" name="url" placeholder="https://example.com/hello.mp3" required>
      <button type="submit">生成二维码</button>
    </form>
  </div>
</body>
</html>
"""

RESULT_HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>二维码已生成</title>
  <style>
    body {
      font-family: system-ui, sans-serif;
      max-width: 720px;
      margin: 40px auto;
      padding: 0 20px;
      text-align: center;
    }
    img { width: min(320px, 90vw); height: auto; }
    .url { word-break: break-all; color: #444; }
    a { color: #2563eb; }
  </style>
</head>
<body>
  <h1>扫这个码就能听</h1>
  <p><img src="{{ qr_src }}" alt="音频二维码"></p>
  <p class="url">{{ target_url }}</p>
  <p><a href="{{ target_url }}">先在本机试听</a> · <a href="{{ url_for('index') }}">再生成一个</a></p>
</body>
</html>
"""

PLAYER_HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>播放音频</title>
  <style>
    body {
      font-family: system-ui, sans-serif;
      min-height: 100vh;
      display: grid;
      place-items: center;
      margin: 0;
      padding: 24px;
      box-sizing: border-box;
    }
    .box { text-align: center; width: min(480px, 100%); }
    audio { width: 100%; margin-top: 16px; }
  </style>
</head>
<body>
  <div class="box">
    <h1>点击播放</h1>
    <p>部分手机不会自动出声，点一下播放即可。</p>
    <audio controls autoplay src="{{ audio_src }}"></audio>
  </div>
</body>
</html>
"""


def lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def public_base_url() -> str:
    host = request.host.split(":")[0]
    if host in {"127.0.0.1", "localhost"}:
        host = lan_ip()
    port = request.host.split(":")[-1] if ":" in request.host else "5000"
    return f"http://{host}:{port}"


def make_qr_png(data: str) -> bytes:
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


@app.get("/")
def index():
    return render_template_string(
        INDEX_HTML,
        base_url=public_base_url(),
        error=request.args.get("error"),
    )


@app.post("/upload")
def upload():
    file = request.files.get("audio")
    if not file or not file.filename:
        return redirect(url_for("index", error="请选择音频文件"))

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        return redirect(url_for("index", error=f"不支持的格式：{ext}"))

    audio_id = uuid.uuid4().hex
    filename = f"{audio_id}{ext}"
    file.save(UPLOAD_DIR / filename)
    return redirect(url_for("result", audio_id=audio_id, ext=ext.lstrip(".")))


@app.post("/from-url")
def from_url():
    url = (request.form.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return redirect(url_for("index", error="请填写以 http:// 或 https:// 开头的链接"))
    token = uuid.uuid4().hex
    (UPLOAD_DIR / f"{token}.url").write_text(url, encoding="utf-8")
    return redirect(url_for("result_external", token=token))


@app.get("/result/<audio_id>")
def result(audio_id: str):
    ext = request.args.get("ext", "mp3")
    filename = f"{audio_id}.{ext}"
    if not (UPLOAD_DIR / filename).exists():
        abort(404)
    target = f"{public_base_url()}/play/{audio_id}?ext={ext}"
    return render_template_string(
        RESULT_HTML,
        target_url=target,
        qr_src=url_for("qr_image", data=target),
    )


@app.get("/result-ext/<token>")
def result_external(token: str):
    path = UPLOAD_DIR / f"{token}.url"
    if not path.exists():
        abort(404)
    target = path.read_text(encoding="utf-8").strip()
    return render_template_string(
        RESULT_HTML,
        target_url=target,
        qr_src=url_for("qr_image", data=target),
    )


@app.get("/play/<audio_id>")
def play(audio_id: str):
    ext = request.args.get("ext", "mp3")
    filename = f"{audio_id}.{ext}"
    if not (UPLOAD_DIR / filename).exists():
        abort(404)
    return render_template_string(
        PLAYER_HTML,
        audio_src=url_for("audio_file", audio_id=audio_id, ext=ext),
    )


@app.get("/audio/<audio_id>")
def audio_file(audio_id: str):
    ext = request.args.get("ext", "mp3")
    path = UPLOAD_DIR / f"{audio_id}.{ext}"
    if not path.exists():
        abort(404)
    return send_file(path)


@app.get("/qr")
def qr_image():
    data = request.args.get("data", "")
    if not data:
        abort(400)
    return send_file(io.BytesIO(make_qr_png(data)), mimetype="image/png")


if __name__ == "__main__":
    print("打开浏览器访问：http://127.0.0.1:5000")
    print(f"局域网地址：http://{lan_ip()}:5000")
    print("手机扫码时请用局域网地址打开本页，或直接扫生成的码。")
    app.run(host="0.0.0.0", port=5000, debug=False)
