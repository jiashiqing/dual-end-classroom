# -*- coding: utf-8 -*-
"""学情 · AI 分析 —— 真浏览器专项（不需要真 Key）

本地起一个假的 OpenAI 兼容上游（SSE 逐帧吐 markdown），页面里选「自定义接口」
指向它，于是「页面 → /api/llm → 上游 → 流式回包 → 渲染」整条链路都是真跑，
只有最后那一段换成了假模型。

查 7 件事：
  1. 学情页有「🤖 AI 分析」，点开铺满视口
  2. 控件齐全（厂商 / 模型 / Key / 接口地址 / 补充说明 / 开始 / 状态）
  3. 流式：中途能抓到半截文字（不是等全部到齐才渲染）
  4. 结束后是渲染过的 HTML（加粗变 <b>、列表变 <li>）
  5. prompt 带了「每关通过人数」「学生明细」和真实姓名
  6. 上游 401 → 面板红条，状态收到「出错」，不卡在「分析中」
  7. Esc 能关；1366×768 下结果区有高度、整页不滚

依赖：websocket-client（`pip install websocket-client`）；缺了会跳过，不算失败。
用法：python scripts/probe_ai.py --dir dist [--w 1600 --h 900]
"""
import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import websocket
except ImportError:  # noqa: BLE001
    print('跳过：没装 websocket-client（pip install websocket-client）')
    sys.exit(0)

EDGE = [r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
        r'C:\Program Files\Microsoft\Edge\Application\msedge.exe']
HERE = os.path.dirname(os.path.abspath(__file__))

OK, BAD = [], []
LAST = {'body': None}

CHUNKS = [
    '**总评**：全班 24 人，第 3 关是卡点，', '项目设计环节参与度偏低。\n\n',
    '- 第 1 关 24 人全过，没有门槛\n',
    '- 第 3 关只剩 8 人，比上一关少 12 人\n',
    '- 第 5 关只有 3 人走完\n\n',
    '**卡点诊断**：\n',
    '- 该关抽象度高，学生卡在「为什么能看出特征」\n\n',
    '**下一步建议**：\n',
    '- 下节课先花 10 分钟补这一个点\n',
    '- 让第 2 组上台演示他们的做法\n',
    '- 给卡住的学生发一张小卡\n\n',
    '**值得表扬**：第 1 组的张同学 8 分钟打通前两关。',
]
ANSWER = ''.join(CHUNKS)


def ok(m):
    OK.append(m)
    print('  ok   ' + m)


def bad(m):
    BAD.append(m)
    print('  BAD  ' + m)


class FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _send(self, code, raw):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith('/last'):
            return self._send(200, json.dumps({'body': LAST['body']}, ensure_ascii=False).encode())
        return self._send(404, b'{"err":"no"}')

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(n).decode('utf-8'))
        LAST['body'] = body
        if 'bad' in (self.headers.get('Authorization') or ''):
            return self._send(401, json.dumps(
                {'error': {'message': 'Invalid API key provided'}}).encode())
        if not body.get('stream'):
            return self._send(200, json.dumps(
                {'choices': [{'message': {'content': ANSWER}}]}, ensure_ascii=False).encode())
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        for c in CHUNKS:
            frame = ('data: ' + json.dumps({'choices': [{'delta': {'content': c}}]},
                                           ensure_ascii=False) + '\n\n').encode()
            self.wfile.write(b'%x\r\n' % len(frame) + frame + b'\r\n')
            self.wfile.flush()
            time.sleep(0.12)
        end = b'data: [DONE]\n\n'
        self.wfile.write(b'%x\r\n' % len(end) + end + b'\r\n' + b'0\r\n\r\n')
        self.wfile.flush()


class Browser:
    def __init__(self, port, profile, url, w=1600, h=900):
        exe = next((e for e in EDGE if os.path.isfile(e)), None)
        if not exe:
            raise SystemExit('没找到 Edge')
        self.proc = subprocess.Popen([
            exe, '--headless=new', '--remote-debugging-port=%d' % port,
            '--user-data-dir=%s' % profile, '--window-size=%d,%d' % (w, h),
            '--hide-scrollbars', '--no-first-run', '--no-default-browser-check', url,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ws_url = None
        for _ in range(80):
            try:
                with urllib.request.urlopen('http://127.0.0.1:%d/json/list' % port, timeout=2) as r:
                    for t in json.load(r):
                        if t.get('type') == 'page' and t.get('webSocketDebuggerUrl'):
                            ws_url = t['webSocketDebuggerUrl']
                            break
            except Exception:  # noqa: BLE001
                pass
            if ws_url:
                break
            time.sleep(0.25)
        if not ws_url:
            raise SystemExit('浏览器没起来：%d' % port)
        self.ws = websocket.create_connection(ws_url, timeout=40)
        self._id = 0
        self.cdp('Runtime.enable')

    def cdp(self, method, params=None):
        self._id += 1
        self.ws.send(json.dumps({'id': self._id, 'method': method, 'params': params or {}}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get('id') == self._id:
                if 'error' in m:
                    raise RuntimeError('%s: %s' % (method, m['error']))
                return m.get('result', {})

    def js(self, expr):
        r = self.cdp('Runtime.evaluate', {'expression': expr, 'returnByValue': True,
                                          'awaitPromise': True})
        if r.get('exceptionDetails'):
            return {'__err': str(r['exceptionDetails'])[:400]}
        return r.get('result', {}).get('value')

    def shot(self, path):
        d = self.cdp('Page.captureScreenshot', {'format': 'png'})
        open(path, 'wb').write(base64.b64decode(d['data']))

    def size(self, w, h):
        self.cdp('Emulation.setDeviceMetricsOverride',
                 {'width': w, 'height': h, 'deviceScaleFactor': 1, 'mobile': False})

    def kill(self):
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass
        self.proc.terminate()


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait(cond, timeout=12.0, step=0.15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return None


SEED = """(() => {
  const names = ['张三','李四','王五','赵六','孙七','周八','吴九','郑十',
                 '冯一','陈二','褚三','卫四','蒋五','沈六','韩七','杨八',
                 '朱九','秦十','尤一','许二','何三','吕四','施五','张六'];
  let n = 0;
  for (let g = 1; g <= 8; g++) for (let k = 0; k < 3; k++) {
    T.onMsg({ t:'join', id:'s'+g+k, name:names[n++], g:g,
              done:(k === 0 ? 5 : (k === 1 ? 3 : 1)), lv:1 });
  }
  T.render();
  return Object.keys(T.stu).length;
})()"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default=os.path.join(HERE, '..', 'dist'))
    ap.add_argument('--w', type=int, default=1600)
    ap.add_argument('--h', type=int, default=900)
    a = ap.parse_args()
    dist = os.path.abspath(a.dir)
    if not os.path.isfile(os.path.join(dist, 'sync_server.py')):
        print('找不到 %s/sync_server.py' % dist)
        return 2

    up_port, sv_port, dbg = free_port(), free_port(), free_port()
    up = ThreadingHTTPServer(('127.0.0.1', up_port), FakeUpstream)
    up.daemon_threads = True
    threading.Thread(target=up.serve_forever, daemon=True).start()

    srv = subprocess.Popen([sys.executable, os.path.join(dist, 'sync_server.py'),
                            '--dir', dist, '--port', str(sv_port)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    prof = os.path.join(dist, '_prof_ai_%d' % int(time.time()))
    b = None
    try:
        def alive():
            try:
                return urllib.request.urlopen(
                    'http://127.0.0.1:%d/healthz' % sv_port, timeout=1).status == 200
            except Exception:  # noqa: BLE001
                return False
        if not wait(alive, 20):
            print('同步服务器没起来')
            return 2

        b = Browser(dbg, prof, 'http://127.0.0.1:%d/t' % sv_port, a.w, a.h)
        if not wait(lambda: b.js("typeof T !== 'undefined' && !!document.getElementById('btnAI')"), 20):
            print('页面没就绪')
            return 2
        b.js(SEED)
        # 切到学情页签（AI 按钮在那儿）
        b.js("document.querySelector('#t-tabs button[data-tab=data]').click(); 1")
        time.sleep(0.4)
        b.shot(os.path.join(dist, '_ai_page.png'))

        r = b.js("""(() => {
          document.getElementById('btnAI').click();
          const ov = document.getElementById('aiOv').getBoundingClientRect();
          return { hidden: document.getElementById('aiOv').hidden,
                   w: Math.round(ov.width), h: Math.round(ov.height),
                   vw: window.innerWidth, vh: window.innerHeight,
                   miss: ['aiProv','aiModel','aiKey','aiBase','aiNote','aiRun','aiState','aiCopy']
                          .filter(id => !document.getElementById(id)).join(','),
                   provs: document.querySelectorAll('#aiProv option').length,
                   defProv: document.getElementById('aiProv').value,
                   defModel: document.getElementById('aiModel').value };
        })()""")
        if r['hidden']:
            bad('AI 面板没打开')
        elif r['w'] == r['vw'] and r['h'] == r['vh']:
            ok('面板铺满视口 %d×%d' % (r['w'], r['h']))
        else:
            bad('面板没铺满：%s' % r)
        if r['miss']:
            bad('缺控件：' + r['miss'])
        else:
            ok('控件齐全')
        if r['provs'] >= 10:
            ok('厂商预设 %d 家（默认 %s · %s）' % (r['provs'], r['defProv'], r['defModel']))
        else:
            bad('厂商预设只有 %d 家' % r['provs'])

        base = 'http://127.0.0.1:%d/v1' % up_port
        b.js("""(() => {
          const p = document.getElementById('aiProv');
          p.value = 'custom'; p.dispatchEvent(new Event('change'));
          document.getElementById('aiBase').value = %s;
          document.getElementById('aiModel').value = 'fake-1';
          document.getElementById('aiKey').value = 'sk-good';
          document.getElementById('aiRun').click();
          return 1;
        })()""" % json.dumps(base))

        half = None
        for _ in range(60):
            st = b.js("({n:(document.getElementById('aiBody').textContent||'').length,"
                      " busy:!!document.querySelector('#aiBody .ai-caret')})")
            if isinstance(st, dict) and st.get('busy') and st.get('n', 0) > 0:
                half = st
                break
            time.sleep(0.08)
        if half:
            ok('流式：中途已显示 %d 字' % half['n'])
        else:
            bad('没抓到流式中间态')

        if not wait(lambda: b.js("document.getElementById('aiState').textContent.indexOf('完成') === 0"), 30):
            bad('分析没跑完：%s' % b.js("document.getElementById('aiState').textContent"))
        else:
            ok('分析完成：' + b.js("document.getElementById('aiState').textContent"))

        r = b.js("""(() => { const box = document.getElementById('aiBody');
          return { li: box.querySelectorAll('li').length, b: box.querySelectorAll('b').length,
                   copyOn: !document.getElementById('aiCopy').disabled }; })()""")
        if r['b'] >= 3 and r['li'] >= 5:
            ok('markdown 渲染：%d 个 <li>、%d 处 <b>' % (r['li'], r['b']))
        else:
            bad('渲染不对：%s' % r)
        if r['copyOn']:
            ok('「复制结果」已解锁')
        else:
            bad('复制按钮还是灰的')

        with urllib.request.urlopen('http://127.0.0.1:%d/last' % up_port, timeout=5) as resp:
            sent = json.load(resp)['body']
        msgs = sent.get('messages') or []
        prompt = msgs[-1]['content'] if msgs else ''
        if '【每关通过人数】' in prompt and '【学生明细】' in prompt and '张三' in prompt:
            ok('prompt 带每关人数 / 学生明细 / 姓名（%d 字）' % len(prompt))
        else:
            bad('prompt 缺内容：%s' % prompt[:120])

        b.size(1366, 768)
        time.sleep(0.6)
        g = b.js("""(() => { const body = document.getElementById('aiBody');
          const ov = document.getElementById('aiOv').getBoundingClientRect();
          return { ovH: Math.round(ov.height), bodyH: body.clientHeight,
                   pageOvf: document.documentElement.scrollHeight - window.innerHeight }; })()""")
        if g['bodyH'] > 120 and g['pageOvf'] <= 1 and g['ovH'] == 768:
            ok('1366×768：结果区 %d 高、整页无滚动' % g['bodyH'])
        else:
            bad('1366×768 几何不对：%s' % g)
        b.shot(os.path.join(dist, '_ai_1366.png'))
        b.size(a.w, a.h)
        time.sleep(0.4)

        b.js("""(() => { document.getElementById('aiKey').value = 'sk-bad';
                 document.getElementById('aiRun').click(); return 1; })()""")
        e = wait(lambda: b.js("(() => { const x = document.querySelector('#aiBody .ai-err');"
                              " return x ? x.textContent : ''; })()"), 20)
        if e and '401' in e:
            ok('上游 401 → 面板红条：' + e.strip()[:40])
        else:
            bad('401 没落到面板：%r' % e)

        # 监听挂在 document 上：真键盘事件是从聚焦元素冒泡上来的，
        # 直接往 window 上 dispatch 不会穿过 document（探针第一版就栽在这）
        b.js("document.body.dispatchEvent(new KeyboardEvent('keydown',"
             "{key:'Escape',bubbles:true}))")
        if b.js("document.getElementById('aiOv').hidden") is True:
            ok('Esc 能关')
        else:
            bad('Esc 关不掉')
    finally:
        if b:
            b.kill()
        srv.terminate()
        up.shutdown()

    print('\n%d 项通过，%d 项失败' % (len(OK), len(BAD)))
    for x in BAD:
        print('  ✗', x)
    return 0 if not BAD else 1


if __name__ == '__main__':
    sys.exit(main())
