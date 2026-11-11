# -*- coding: utf-8 -*-
"""课堂内网同步服务器（零依赖 · 只用 Python 3 标准库）

干三件事：
  1. 把本目录静态托管出去 —— 学生用 http://<教师机IP>:8099/s 打开就自动内网直连
  2. GET  /s、/t、/m               短地址：学生端 / 教师端 / 手机遥控台
  3. GET  /events?channel=<频道>   SSE 下行
  4. POST /send                    JSON 广播给同频道其他人
  5. POST /api/llm                 学情 AI 分析：转发到厂商，三家协议归一化成一种 SSE

与页面里 Sync 对象逐字对齐：
  POST /send  body  {"channel":"…","data":"<消息JSON字符串>","sender":"<发送者ID>"}
  SSE 每帧     data: {"sender":"…","data":"<消息JSON字符串>"}

用法：
  python sync_server.py [--port 8099] [--dir .]
  双击 start_server.bat 亦可。Ctrl+C 停止。
"""
import argparse
import json
import os
import queue
import re
import select
import socket
import sys
import threading
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 8099
MAX_BODY = 2 * 1024 * 1024
HEARTBEAT = 15.0
QUEUE_MAX = 300

_subs = {}
_lock = threading.Lock()
_counter = {"msg": 0}
ROOT = [BASE]           # 静态根目录，main() 里可改


def _join(ch):
    q = queue.Queue(maxsize=QUEUE_MAX)
    with _lock:
        _subs.setdefault(ch, []).append(q)
    return q


def _leave(ch, q):
    with _lock:
        lst = _subs.get(ch)
        if lst and q in lst:
            lst.remove(q)
        if lst is not None and not lst:
            _subs.pop(ch, None)


def _fanout(ch, payload):
    with _lock:
        targets = list(_subs.get(ch) or ())
    ok = 0
    for q in targets:
        try:
            q.put_nowait(payload)
            ok += 1
        except queue.Full:
            pass
    return ok


def _chan_stat():
    with _lock:
        return {k: len(v) for k, v in _subs.items() if v}


def _pick(keyword):
    """在根目录里按关键字找 HTML（'学生端' / '教师端'）。"""
    try:
        for f in sorted(os.listdir(ROOT[0])):
            if f.lower().endswith('.html') and keyword in f:
                return f
    except Exception:
        pass
    return ''


def lan_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    for probe in (("10.255.255.255", 1), ("223.5.5.5", 53)):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(probe)
            ips.add(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    return sorted(i for i in ips if not i.startswith("127.") and i != "0.0.0.0")


def best_lan_ip(ips=None):
    """从几个网卡地址里挑一个最可能被学生机访问到的。

    一台 Windows 经常同时挂着：真实网卡 192.168.x、VMware/Hyper-V 虚拟网卡 192.168.x、
    WSL 的 172.x、还有没连上 DHCP 时的 169.254.x。按「教室里最像的那个」排序：
    192.168 → 10. → 172.16-31 → 其它 → 169.254（自动私有地址，基本等于没网）。
    """
    ips = list(lan_ips() if ips is None else ips)
    def score(ip):
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        if re.match(r"^172\.(1[6-9]|2\d|3[01])\.", ip):
            return 2
        if ip.startswith("169.254."):
            return 9
        return 3
    return sorted(ips, key=lambda i: (score(i), i))[0] if ips else ""


# ======================================================================
# /api/llm —— 学情 AI 分析的大模型转发
# ----------------------------------------------------------------------
# 为什么必须走服务端转发，而不是页面里直接 fetch 厂商接口：
#   1. CORS：OpenAI / 智谱 / 豆包等都不允许浏览器跨域直呼，页面里 fetch 直接被拦；
#   2. 浏览器直连会把 Key 暴露在页面上下文里，也绕不过某些厂商对 Referer 的限制。
# 本机服务器转发一次，页面只跟同源打交道，Key 也只发给自己这台机器。
#
# 协议：POST /api/llm  {mode, base, model, key, system, prompt, stream}
#   mode = openai（含 DeepSeek/Kimi/通义/GLM/豆包/硅基流动/Ollama…）
#        | anthropic | gemini
# 回包：text/event-stream，每帧 data: {"t":"增量文字"}
#       出错 data: {"err":"..."} ；结束 data: {"done":1,"chars":N}
# ======================================================================

LLM_MAX_BODY = 512 * 1024        # 提示词最多 512 KB，够塞一整班明细
LLM_TIMEOUT = 240                # 推理模型可能要几十秒，别用默认的 30 秒掐断
LLM_MAX_TOKENS = 2000

# 只允许 https，本机（Ollama / LM Studio / one-api 网关）才放行 http。
# 不这么做等于给局域网开了一个任意 http 请求的代理，学生也能拿它去打内网设备。
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def llm_url(base, mode, model, stream):
    u = urlparse(base or "")
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("接口地址不合法：%s" % (base or "（空）"))
    if u.scheme == "http" and u.hostname not in LOOPBACK_HOSTS:
        raise ValueError("为安全起见只允许 https 接口；本机模型可用 http://127.0.0.1:端口")
    b = (base or "").rstrip("/")
    if mode == "anthropic":
        return b + "/messages"
    if mode == "gemini":
        verb = ":streamGenerateContent?alt=sse" if stream else ":generateContent"
        return b + "/models/" + quote(model or "") + verb
    # openai 兼容：base 可能写成 https://api.deepseek.com，也可能带 /v1
    return b + "/chat/completions"


def llm_headers(mode, key, stream):
    h = {"Content-Type": "application/json"}
    if stream:
        h["Accept"] = "text/event-stream"
    if mode == "anthropic":
        # anthropic-version 是必填；browser-access 头是官方给前端直呼的开关，
        # 这里虽然走服务端，但有些网关会照着这个头做校验，留着无害。
        h["x-api-key"] = key
        h["anthropic-version"] = "2023-06-01"
        h["anthropic-dangerous-direct-browser-access"] = "true"
    elif mode == "gemini":
        h["x-goog-api-key"] = key
    elif key:
        h["Authorization"] = "Bearer " + key
    return h


def llm_payload(mode, model, system, prompt, stream):
    if mode == "anthropic":
        return {"model": model, "system": system, "max_tokens": LLM_MAX_TOKENS,
                "temperature": 0.6, "stream": stream,
                "messages": [{"role": "user", "content": prompt}]}
    if mode == "gemini":
        # Gemini 没有独立的 system 字段（1.5 之后有 systemInstruction，但各家代理
        # 支持不一），直接把系统提示拼在正文前面最稳。
        return {"contents": [{"role": "user",
                              "parts": [{"text": system + "\n\n" + prompt}]}],
                "generationConfig": {"temperature": 0.6,
                                     "maxOutputTokens": LLM_MAX_TOKENS}}
    return {"model": model, "stream": stream, "temperature": 0.6,
            "max_tokens": LLM_MAX_TOKENS,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}]}


def llm_text(mode, obj):
    """从一帧里抠出增量文字。三种协议各一个分支，其余一律返回空串。"""
    if not isinstance(obj, dict):
        return ""
    if mode == "anthropic":
        if obj.get("type") == "content_block_delta":
            return (obj.get("delta") or {}).get("text") or ""
        parts = obj.get("content")            # 非流式回包
        if isinstance(parts, list):
            return "".join(p.get("text") or "" for p in parts if isinstance(p, dict))
        return ""
    if mode == "gemini":
        cands = obj.get("candidates") or []
        if not cands:
            return ""
        parts = ((cands[0] or {}).get("content") or {}).get("parts") or []
        return "".join(p.get("text") or "" for p in parts if isinstance(p, dict))
    choices = obj.get("choices") or []
    if not choices:
        return ""
    c = choices[0] or {}
    d = c.get("delta") or c.get("message") or {}
    return d.get("content") or ""


_OPENERS = {}


def llm_open(url, data, headers, timeout):
    """发请求。本机地址（Ollama / 本地网关）一律直连，走代理会连不上自己；
    外网地址沿用系统代理设置（有些学校/单位只有代理才出得去，浏览器能开页面就说明代理是通的）。"""
    host = (urlparse(url).hostname or "").lower()
    loop = host in LOOPBACK_HOSTS or host.startswith("127.")
    k = "direct" if loop else "env"
    if k not in _OPENERS:
        _OPENERS[k] = (urllib.request.build_opener(urllib.request.ProxyHandler({}))
                       if loop else urllib.request.build_opener())
    return _OPENERS[k].open(
        urllib.request.Request(url, data=data, method="POST", headers=headers), timeout=timeout)


def llm_err_text(raw):
    """把上游的错误回包压成一行人话（各家格式不一样，能取到什么取什么）。"""
    try:
        o = json.loads(raw)
    except Exception:
        return raw.strip()[:300]
    if isinstance(o, dict):
        e = o.get("error")
        if isinstance(e, dict):
            return str(e.get("message") or e.get("type") or e)[:300]
        if e:
            return str(e)[:300]
        if o.get("message"):
            return str(o["message"])[:300]
    return raw.strip()[:300]


PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>课堂同步服务器</title>
<style>
body{margin:0;padding:34px 22px;background:#f2f6f3;color:#17281f;
 font-family:"Microsoft YaHei","PingFang SC",sans-serif}
h1{font-size:22px;margin:0 0 6px}
.sub{color:#66796e;font-size:13px;margin-bottom:22px}
a.card{display:block;text-decoration:none;color:inherit;background:#fff;
 border:1px solid #d9e5dd;border-radius:14px;padding:17px 19px;margin:0 0 13px;
 box-shadow:0 2px 10px rgba(23,40,31,.06)}
a.card:hover{border-color:#16794f;transform:translateY(-1px)}
a.card b{font-size:17px}
a.card p{margin:6px 0 0;color:#66796e;font-size:13px}
.tag{display:inline-block;margin-left:8px;font-size:12px;color:#0f5c3b;
 background:#e4f2ea;border-radius:999px;padding:2px 9px;vertical-align:2px}
.foot{margin-top:20px;color:#8b9a92;font-size:12px;line-height:1.8}
code{background:#e8efe9;border-radius:5px;padding:1px 6px}
</style></head><body>
<h1>课堂同步服务器</h1>
<div class="sub">%s ｜ 在线客户端 %d ｜ 已转发消息 %d</div>
%s
<div class="foot">学生机请用<b>同一局域网</b>打开 /s；教师机请用 <code>http://127.0.0.1:端口/t</code> 打开——<br>
局域网 IP 属于非安全上下文，浏览器会直接禁用摄像头，第 17 页「训练AI」就开不了摄像头。<br>
频道：<code>%s</code></div></body></html>
"""


class _RangeFile:
    """文件的一段，喂给 SimpleHTTPRequestHandler.copyfile()。

    只需要 read(n)。原实现是 shutil.copyfileobj，按 64KB 一块往外拷，
    拷完 left 变成 0 就自然结束，不会读到别的段去。
    """

    def __init__(self, fp, start, length):
        self.fp = fp
        self.left = length
        try:
            self.fp.seek(start)
        except Exception:
            pass

    def read(self, n=-1):
        if self.left <= 0:
            return b""
        if n is None or n < 0:
            n = self.left
        d = self.fp.read(min(n, self.left))
        self.left -= len(d)
        return d

    def close(self):
        try:
            self.fp.close()
        except Exception:
            pass


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ClassRelay/1.0"
    # keep-alive 的空闲连接最多挂 120 秒。学生合上笔记本、拔网线这类「不告而别」，
    # 不设超时这条连接会一直卡在读 request line 上，线程收不回来。
    # 比 SSE 心跳（15 秒）长得多，不会误伤。
    timeout = 120

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT[0], **kw)

    def log_message(self, fmt, *args):
        pass

    # ---------- Range 支持（视频/大图必须） ----------
    # 标准库的 SimpleHTTPRequestHandler 只认「整个文件 200」，不认 Range。
    # 这带来一个很隐蔽的坑：授课第 2 页是视频页（<video preload="metadata">），
    # 浏览器只能拿到 200 + 完整 Content-Length，于是它老实把 43 MB 的开场片整个拉下来，
    # 占着连接不放，期间同源的其他请求全部排队 —— 实测翻到第 3 页要等 11~13 秒。
    # 补上 206 之后，浏览器只要一小段就能拿到 moov、随即断开，翻页回到 100~200 ms。
    _ranges_ok = False

    def end_headers(self):
        if self._ranges_ok:
            self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def send_head(self):
        path = self.translate_path(self.path)
        self._ranges_ok = os.path.isfile(path)
        rng = self.headers.get("Range")
        if rng and self._ranges_ok:
            got = self._send_range(path, rng)
            if got is None:          # 416，响应已经发完，别再发第二个
                return None
            if got is not False:     # 206 + 部分内容
                return got
        return super().send_head()

    def _send_range(self, path, rng):
        """能处理就返回 _RangeFile（或 None 表示 416 已回），处理不了返回 False 走原路。"""
        m = re.match(r"^bytes=(\d*)-(\d*)$", (rng or "").strip())
        if not m:                       # 多段 Range（逗号分开）之类，交回原实现整份返回
            return False
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        a, b = m.group(1), m.group(2)
        if not a and not b:
            return False
        if not a:                       # bytes=-N：最后 N 字节
            n = int(b)
            if n <= 0:
                return False
            start, end = max(0, size - n), size - 1
        else:
            start = int(a)
            end = int(b) if b else size - 1
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            end = min(end, size - 1)
        if end < start:
            return False
        try:
            fp = open(path, "rb")
        except OSError:
            return False
        length = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Content-Length", str(length))
        self.send_header("Last-Modified", self.date_time_string(os.path.getmtime(path)))
        self.end_headers()
        return _RangeFile(fp, start, length)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _netinfo(self):
        """告诉页面「这台机器在外面的地址是什么」。

        端口取真正在听的那个（--port 被占时会自动顺延，不能拿命令行参数糊弄）。
        """
        try:
            port = self.server.server_port or DEFAULT_PORT
        except Exception:
            port = DEFAULT_PORT
        ips = lan_ips()
        ip = best_lan_ip(ips)
        return self._json(200, {
            "ok": True,
            "port": port,
            "ip": ip,
            "ips": ips,
            "studentUrl": ("http://%s:%d/s" % (ip, port)) if ip else "",
            "teacherUrl": ("http://%s:%d/t" % (ip, port)) if ip else "",
            "localUrl": "http://127.0.0.1:%d/t" % port,
        })

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _chunk(self, b):
        self.wfile.write(("%x\r\n" % len(b)).encode("ascii") + b + b"\r\n")
        self.wfile.flush()

    def _peer_gone(self):
        try:
            r, _, _ = select.select([self.connection], [], [], 0)
            if r:
                return self.connection.recv(1, socket.MSG_PEEK) == b""
        except Exception:
            return True
        return False

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._ranges_ok = False        # keep-alive 下同一个 handler 会复用，每单都要重置
        u = urlparse(self.path)
        if u.path == "/events":
            return self._sse((parse_qs(u.query).get("channel") or [""])[0].strip())
        if u.path in ("/", "/index.html"):
            return self._index()
        if u.path == "/healthz":
            return self._json(200, {"ok": True, "channels": _chan_stat()})
        # 教师端课堂码下面那行「学生机输入这个地址」从这儿取：老师机为了开摄像头是用
        # http://127.0.0.1:8099/t 打开的，location.host 是回环地址，直接显示会误导学生。
        if u.path == "/netinfo":
            return self._netinfo()
        # 短地址：内部改写路径直接返回文件（不用 302，避免客户端二次编码中文路径）
        if u.path in ("/s", "/student"):
            f = _pick("学生端")
            if not f:
                return self._json(404, {"ok": False, "err": "未找到学生端 HTML"})
            self.path = "/" + quote(f)
            return super().do_GET()
        if u.path in ("/t", "/teacher"):
            f = _pick("教师端")
            if not f:
                return self._json(404, {"ok": False, "err": "未找到教师端 HTML"})
            self.path = "/" + quote(f)
            return super().do_GET()
        # 手机遥控台：教师端「加分」页和「课堂运行」的二维码都指向这里
        if u.path in ("/m", "/mobile", "/remote"):
            f = _pick("遥控台")
            if not f:
                return self._json(404, {"ok": False, "err": "未找到遥控台 HTML（构建时漏拷了）"})
            self.path = "/" + quote(f)
            return super().do_GET()
        return super().do_GET()

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/llm":
            return self._llm()
        if p != "/send":
            return self._json(404, {"ok": False, "err": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return self._json(411, {"ok": False, "err": "length required"})
        if n > MAX_BODY:
            return self._json(413, {"ok": False, "err": "too large"})
        try:
            d = json.loads(self.rfile.read(n).decode("utf-8"))
            ch = str(d.get("channel") or "").strip()
            data = d.get("data")
            sender = str(d.get("sender") or "")
            if not ch or data is None:
                raise ValueError("channel and data required")
            if not isinstance(data, str):
                data = json.dumps(data, ensure_ascii=False)
        except Exception as e:
            return self._json(400, {"ok": False, "err": "%s" % e})
        out = json.dumps({"sender": sender, "data": data}, ensure_ascii=False)
        n_ok = _fanout(ch, out)
        _counter["msg"] += 1
        return self._json(200, {"ok": True, "n": n_ok})

    def _sse(self, ch):
        if not ch:
            return self._json(400, {"ok": False, "err": "channel required"})
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self._cors()
        self.end_headers()
        q = _join(ch)
        try:
            self._chunk(b"retry: 3000\n\n")
            while True:
                try:
                    item = q.get(timeout=HEARTBEAT)
                except queue.Empty:
                    if self._peer_gone():
                        break
                    self._chunk(b": hb\n\n")
                    continue
                self._chunk(("data: %s\n\n" % item).encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            _leave(ch, q)

    def _index(self):
        host = self.headers.get("Host") or ("localhost:%d" % self.server.server_port)
        conn = sum(_chan_stat().values())
        cards = []
        try:
            files = [f for f in os.listdir(ROOT[0]) if f.lower().endswith(".html")]
        except Exception:
            files = []
        for f in sorted(files, key=lambda x: (0 if "教师端" in x else
                                              1 if "学生端" in x else
                                              2 if "遥控台" in x else 3, x)):
            if "教师端" in f:
                # 教师机必须走本机回环：局域网 IP 是 http 非安全上下文，浏览器会禁用摄像头
                url = "http://127.0.0.1:%d/t" % self.server.server_port
                tag, desc = "教师机打开", "大屏 · 学情 · 加分 · 控制台（摄像头只在本机回环下可用）"
            elif "学生端" in f:
                url, tag, desc = "/s", "学生机打开", "输入课堂码报到 · 闯关 · 证书"
            elif "遥控台" in f:
                url, tag, desc = "/m", "手机遥控", "加分 · 逐关开放 · 计时（教师端两张码都指这里）"
            else:
                url, tag, desc = "/" + quote(f), "", "HTML 页面"
            cards.append('<a class="card" href="%s"><b>%s</b><span class="tag">%s</span>'
                         "<p>%s ｜ 短地址 <code>%s</code></p></a>" % (url, f, tag, desc, url))
        if not cards:
            cards.append("<p>本目录没有 HTML 文件。</p>")
        body = (PAGE % (host, conn, _counter["msg"], "\n".join(cards),
                        json.dumps(_chan_stat(), ensure_ascii=False))).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    # ---------- 学情 AI 分析：转发到厂商 ----------
    def _llm(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return self._json(411, {"ok": False, "err": "length required"})
        if n > LLM_MAX_BODY:
            return self._json(413, {"ok": False, "err": "提示词太大（%d 字节）" % n})
        try:
            req = json.loads(self.rfile.read(n).decode("utf-8"))
            mode = str(req.get("mode") or "openai").strip().lower()
            if mode not in ("openai", "anthropic", "gemini"):
                raise ValueError("mode 只支持 openai / anthropic / gemini")
            base = str(req.get("base") or "").strip()
            model = str(req.get("model") or "").strip()
            key = str(req.get("key") or "").strip()
            system = str(req.get("system") or "")
            prompt = str(req.get("prompt") or "")
            stream = bool(req.get("stream", True))
            if not prompt.strip():
                raise ValueError("prompt 为空")
            if mode != "openai" and not model:
                raise ValueError("缺模型名")
            url = llm_url(base, mode, model, stream)
        except Exception as e:                      # noqa: BLE001
            return self._json(400, {"ok": False, "err": "%s" % e})

        data = json.dumps(llm_payload(mode, model, system, prompt, stream),
                          ensure_ascii=False).encode("utf-8")
        try:
            resp = llm_open(url, data, llm_headers(mode, key, stream), LLM_TIMEOUT)
        except urllib.error.HTTPError as e:
            return self._json(200, {"ok": False,
                                    "err": "上游 %d：%s" % (e.code, llm_err_text(
                                        e.read().decode("utf-8", "replace")))})
        except Exception as e:                      # noqa: BLE001
            return self._json(200, {"ok": False, "err": "连不上 %s（%s）"
                                    % (urlparse(url).hostname or "?", e)})

        if not stream:
            try:
                raw = resp.read().decode("utf-8", "replace")
            finally:
                try:
                    resp.close()
                except Exception:                   # noqa: BLE001
                    pass
            txt = ""
            try:
                txt = llm_text(mode, json.loads(raw))
            except Exception:                       # noqa: BLE001
                for line in raw.splitlines():
                    line = line.strip()
                    if line.startswith("data:"):
                        try:
                            txt += llm_text(mode, json.loads(line[5:].strip()))
                        except Exception:           # noqa: BLE001
                            pass
            if not txt:
                return self._json(200, {"ok": False,
                                        "err": "上游没返回文字：%s" % llm_err_text(raw)})
            return self._json(200, {"ok": True, "text": txt})

        # 流式：三家的 SSE 一律归一化成 data: {"t": "…"}，页面只认这一种
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self._cors()
        self.end_headers()

        def emit(obj):
            self._chunk(("data: %s\n\n" % json.dumps(obj, ensure_ascii=False)).encode("utf-8"))

        chars, bad = 0, ""
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except Exception:                   # noqa: BLE001
                    continue
                if isinstance(obj, dict) and obj.get("error"):
                    bad = llm_err_text(chunk)
                    emit({"err": bad})
                    break
                t = llm_text(mode, obj)
                if t:
                    chars += len(t)
                    emit({"t": t})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            try:
                resp.close()
            except Exception:                       # noqa: BLE001
                pass
            # 一句字都没吐：多半是模型名/Key 不对，别让页面停在「分析中…」
            if not chars and not bad:
                try:
                    emit({"err": "上游没有返回内容——检查模型名、Key 与接口地址"})
                except Exception:                   # noqa: BLE001
                    pass
            try:
                emit({"done": 1, "chars": chars})
            except Exception:                       # noqa: BLE001
                pass
            # 补上 chunked 的结束块：不写这一句，浏览器 fetch 读流会当成「连接被掐断」，
            # 而 http.client 直接抛 IncompleteRead（探针就栽在这）。
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:                       # noqa: BLE001
                pass


class QuietServer(ThreadingHTTPServer):
    """把「对面先挂了」这类异常静音。

    学生机刷新页面、关浏览器、拔网线、合笔记本，都会让 keep-alive 连接突然断掉，
    socketserver 于是抛 ConnectionAborted(10053) / ConnectionReset(10054) /
    BrokenPipe(10058)，默认处理是往 stderr 打一整段 traceback。
    一节课下来窗口里全是红字，老师会以为程序崩了 —— 其实教室里这属于日常。
    真正的故障（端口、权限、磁盘）不是 ConnectionError，照旧原样报出来。
    """
    daemon_threads = True

    def handle_error(self, request, client_address):
        et = sys.exc_info()[0]
        if et is not None and issubclass(et, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def port_in_use(port, host="127.0.0.1"):
    """真的去连一下：连得上就说明有人占着。
    不能只靠 bind 判——Windows 上 SO_REUSEADDR 允许「抢占」已被监听的端口，
    bind 会假成功，两个进程同时监听同一个端口。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.25)
    try:
        return s.connect_ex((host, port)) == 0
    except Exception:
        return False
    finally:
        s.close()


def bind_server(host, port, tries=30):
    """按端口顺延绑定：8099 被占就用 8100、8101……
    机房电脑上 8099 经常被别的工具占着，起不来就整堂课开不了，所以必须自动让路。
    返回 (srv, 实际端口, 是否顺延)。"""
    last = None
    for p in range(port, min(port + tries, 65536)):
        if port_in_use(p):
            last = "端口 %d 已被占用" % p
            continue
        try:
            srv = QuietServer((host, p), Handler)
            return srv, p, (p != port)
        except OSError as e:
            last = e
            if getattr(e, "errno", None) not in (10048, 98, 13, 10013):
                raise
    raise SystemExit("端口 %s ~ %s 都起不来（%s），换个端口或换台机器"
                     % (port, min(port + tries, 65536) - 1, last))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--dir", default="")
    ap.add_argument("--no-fallback", action="store_true", help="端口被占时直接报错，不顺延")
    a = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

    ROOT[0] = os.path.abspath(a.dir) if a.dir else BASE
    if not os.path.isdir(ROOT[0]):
        raise SystemExit("目录不存在：%s" % ROOT[0])

    if a.no_fallback:
        srv = QuietServer((a.host, a.port), Handler)
        port, moved = srv.server_port, False
    else:
        srv, port, moved = bind_server(a.host, a.port)
    srv.daemon_threads = True
    ips = lan_ips()
    stu, tea = _pick("学生端"), _pick("教师端")

    print("")
    print("  课堂同步服务器已启动（Ctrl+C 停止）")
    print("  " + "-" * 54)
    print("  目录  %s" % ROOT[0])
    if moved:
        print("  [!] 端口 %d 被占用，已自动改用 %d" % (a.port, port))
        print("      下面所有地址都用 %d，别照旧记 %d" % (port, a.port))
        print("      如果只是上次的窗口没关，建议先关掉它再重开，")
        print("      否则学生可能连到旧的那个实例。")
    if ips:
        print("  学生机  http://%s:%d/s      ← 让全班输入这个" % (ips[0], port))
        print("  教师机  http://127.0.0.1:%d/t    ← 你自己打开这个（本机回环）" % port)
        print("          ↑ 别用 http://%s:%d/t ：局域网 IP 不是安全上下文，" % (ips[0], port))
        print("            浏览器会直接禁用摄像头，第 17 页「训练AI」就开不了摄像头。")
        print("  首页    http://%s:%d/" % (ips[0], port))
        if len(ips) > 1:
            print("  其他网卡：%s" % ", ".join(ips[1:]))
    else:
        print("  未探测到局域网 IP，用 ipconfig 自己看")
        print("  本机    http://127.0.0.1:%d/" % port)
    print("  " + "-" * 54)
    if stu:
        print("  /s → %s" % stu)
    if tea:
        print("  /t → %s" % tea)
    print("")
    print("  学生端地址一行（可直接抄到黑板）：  http://%s:%d/s"
          % (ips[0] if ips else "127.0.0.1", port))
    print("")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  正在关闭 …")
    finally:
        srv.shutdown()
        srv.server_close()
        print("  已停止。")


if __name__ == "__main__":
    main()
