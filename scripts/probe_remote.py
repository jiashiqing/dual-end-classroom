# -*- coding: utf-8 -*-
"""手机遥控台专项探针：一个二维码同时管「加分 · 逐关开放 · 计时」。

真浏览器 + 真 server，三端同时在线（教师端 1366×768 / 手机 430×932 / 学生端）：
  1. 「加分」页的大码和「控制台 · 课堂运行」的小码解出来是同一个 /m?code=…
  2. 手机上点第 3 关 → 教师端 T.open 里有 3，学生端 S.open 跟着解锁
  3. 全部关闭 / 全部开启 → 学生端跟着锁 / 全开
  4. 手机上开始 / 暂停 / +5 / 归零 → 教师端 sec、run 真的变，手机和教师端数字对得上
  5. 1366×768 控制台里遥控块不溢出；手机上不横滚、计时条吸顶

用法：python scripts/probe_remote.py --dir <构建输出目录>
"""
import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

import websocket

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if not os.path.isfile(EDGE):
    EDGE = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)

OK, BAD = [], []


def ok(msg):
    OK.append(msg)
    print("  ok   " + msg)


def bad(msg):
    BAD.append(msg)
    print("  FAIL " + msg)


def chk(cond, msg):
    (ok if cond else bad)(msg)


class Browser:
    def __init__(self, port, profile, url, w=1366, h=768):
        self.proc = subprocess.Popen([
            EDGE, '--headless=new', '--remote-debugging-port=%d' % port,
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


def decode_qr(data_url):
    try:
        import numpy as np
        import cv2
    except Exception:  # noqa: BLE001
        return ""
    if not data_url or ',' not in data_url:
        return ""
    raw = base64.b64decode(data_url.split(',', 1)[1])
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return ""
    det = cv2.QRCodeDetector()
    for im in (img, cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)):
        txt, _, _ = det.detectAndDecode(im)
        if txt:
            return txt
    return ""


def qr_data(sel):
    return ("""(() => { const cv = document.querySelector('%s');
        if (!cv || cv.width < 10) return '';
        return cv.toDataURL('image/png'); })()""" % sel)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default=os.path.join(SKILL, 'dist'))
    a = ap.parse_args()
    dist = os.path.abspath(a.dir)
    shots = os.path.join(HERE, 'shots')
    os.makedirs(shots, exist_ok=True)

    port, web = free_port(), free_port()
    srv = subprocess.Popen([sys.executable, os.path.join(dist, 'sync_server.py'),
                            '--port', str(web), '--dir', dist],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.6)
    base = 'http://127.0.0.1:%d' % web
    tmp = os.environ.get('TEMP', '.')
    prof = [os.path.join(tmp, 'prof_rm_s%d' % i) for i in range(3)]
    for p in prof:
        shutil.rmtree(p, ignore_errors=True)

    t = Browser(port, prof[0], base + '/t')
    m = s = None
    try:
        if not wait(lambda: t.js('typeof T !== "undefined" && !!T.open')):
            raise SystemExit('教师端没起来')

        t.js("""(() => {
          localStorage.clear();
          T.stu={}; T.gpts={}; T.award={fin:[],lv:{}};
          T.cfg.groups=8; T.cfg.mins=[12,12,12,12,12]; T.cfg.title='双端课堂';
          T.open=[1]; T.sec=720; T.run=false; T._tlv=1;
          document.querySelector('#t-tabs button[data-tab="ctrl"]').click();
          T.cv('run'); T.render(); T.tick();
          return 1; })()""")
        time.sleep(0.9)
        code = t.js('T.code')
        print('课堂码 %s' % code)

        # ---------------------------------------------------------------- 1
        print('\n[1] 教师端两张码是同一个地址')
        # 加分页那张要切过去才画（切页签时 paintQRm 补一次）
        t.js("document.querySelector('#t-tabs button[data-tab=\"score\"]').click(); 1")
        time.sleep(0.4)
        big_tip = t.js("(document.getElementById('qrMTip')||{}).getAttribute('title')")
        t.js("document.querySelector('#t-tabs button[data-tab=\"ctrl\"]').click(); 1")
        time.sleep(0.4)
        small_tip = t.js("(document.getElementById('qrSTip')||{}).getAttribute('title')")
        chk(('/m?code=' + str(code)) in (big_tip or ''), '加分页大码指向 /m?code · %r' % big_tip)
        chk(('/m?code=' + str(code)) in (small_tip or ''), '课堂运行小码指向 /m?code · %r' % small_tip)
        shown = t.js("""(() => {
          const b = document.getElementById('qrBoxS');
          const r = b.getBoundingClientRect();
          return { w: Math.round(r.width), vis: getComputedStyle(b).display !== 'none',
                   cw: document.getElementById('qrCvS').width };
        })()""")
        chk(shown.get('vis') is True and (shown.get('w') or 0) >= 80,
            '小码在运行视图里真的画出来了（%s×%s）' % (shown.get('w'), shown.get('w')))
        qr_s = decode_qr(t.js(qr_data('#qrCvS')))
        if qr_s:
            chk(('/m?code=' + str(code)) in qr_s, '小码解出来就是遥控台地址 · %s' % qr_s)
            t.js("document.querySelector('#t-tabs button[data-tab=\"score\"]').click(); 1")
            time.sleep(0.4)
            qr_b = decode_qr(t.js(qr_data('#qrCvM')))
            chk(qr_b == qr_s, '大码和小码内容完全一致（扫哪个都一样）')
            t.js("document.querySelector('#t-tabs button[data-tab=\"ctrl\"]').click(); 1")
            time.sleep(0.3)
        else:
            print('  --   二维码解码跳过（没装 opencv）')
        t.shot(os.path.join(shots, 'skill_ctrl_remote.png'))

        # ---------------------------------------------------------------- 2
        print('\n[2] 手机 /m 打开遥控台')
        with urllib.request.urlopen(base + '/m?code=%s' % code, timeout=5) as resp:
            body = resp.read().decode('utf-8')
        chk(resp.status == 200 and '课堂遥控台' in body, '/m 打开的是遥控台（%d 字节）' % len(body))
        chk('逐关开放' in body and 'bRun' in body, '页面里同时有逐关开放和计时')

        m = Browser(free_port(), prof[1], base + '/m?code=%s' % code, w=430, h=932)
        time.sleep(1.5)
        chk(bool(wait(lambda: m.js("document.querySelectorAll('#lvRow .lv').length") == 5, 8)),
            '手机端画出 5 个关卡按钮')
        chk(bool(wait(lambda: '已连上' in str(m.js("(document.getElementById('net')||{}).textContent")), 8)),
            '手机端显示已连上教师机')
        r = m.js("""(() => ({
            cards: document.querySelectorAll('#list .c').length,
            hint: (document.getElementById('lvHint')||{}).textContent,
            clk: (document.getElementById('clk')||{}).textContent,
            on: [...document.querySelectorAll('#lvRow .lv.on')].map(e => +e.dataset.lv),
        }))()""")
        chk(r.get('cards') == 8, '手机端画出 8 张加分卡 · 实得 %s' % r.get('cards'))
        chk(r.get('hint') == '已开放 1 / 5 关', '手机显示已开放 1/5 关 · 实际 %r' % r.get('hint'))
        chk(r.get('on') == [1], '只有第 1 关是亮的 · 实得 %s' % r.get('on'))
        chk(r.get('clk') != '--:--', '手机拿到了计时（12:00）· 实际 %r' % r.get('clk'))
        m.shot(os.path.join(shots, 'skill_remote_phone.png'))

        # ---------------------------------------------------------------- 3
        print('\n[3] 手机放行第 3 关 → 教师端 + 学生端')
        s = Browser(free_port(), prof[2], base + '/s?g=8')
        time.sleep(1.6)
        s.js("""(() => { document.getElementById('j-code').value = '%s';
          document.getElementById('j-name').value = '张三';
          document.getElementById('j-go').click(); return 1; })()""" % code)
        chk(bool(wait(lambda: s.js("document.getElementById('pg-student').classList.contains('on')"), 8)),
            '学生端报到进课堂')

        m.js("""(() => { document.querySelector('#lvRow .lv[data-lv="3"]').click(); return 1; })()""")
        chk(bool(wait(lambda: t.js("T.open.indexOf(3) >= 0"), 6)),
            '手机点第 3 关 → 教师端 T.open 里有 3 · 实际 %s' % t.js('T.open.join(",")'))
        chk(bool(wait(lambda: s.js("S.open.indexOf(3) >= 0"), 8)),
            '学生端跟着解锁 · 实际 %s' % s.js('S.open.join(",")'))
        chk(bool(wait(lambda: m.js(
            """document.querySelector('#lvRow .lv[data-lv="3"]').classList.contains('on')"""), 6)),
            '手机上的第 3 关变亮（ctl 广播回来了）')
        r = m.js("(document.getElementById('lvHint')||{}).textContent")
        chk(r == '已开放 2 / 5 关', '手机上的计数同步成 2/5 · 实际 %r' % r)

        m.js("""(() => { document.querySelector('#lvRow .lv[data-lv="3"]').click(); return 1; })()""")
        chk(bool(wait(lambda: t.js("T.open.indexOf(3) < 0"), 6)),
            '再点一下 → 第 3 关收回 · 实际 %s' % t.js('T.open.join(",")'))
        chk(bool(wait(lambda: s.js("S.open.indexOf(3) < 0"), 8)), '学生端也跟着锁回去')

        m.js("document.getElementById('bNone').click(); 1")
        chk(bool(wait(lambda: t.js("T.open.length === 0"), 6)), '手机「全部关闭」→ 教师端一关都不开')
        # 技能版学生端对「空放行」有兜底：回落成 [1]，避免新装机一进来一关都点不开
        chk(bool(wait(lambda: s.js("S.open.length === 1 && S.open[0] === 1"), 8)),
            '学生端回落到第 1 关（空放行的兜底，不是 bug）· 实际 %s' % s.js('S.open.join(",")'))
        m.js("document.getElementById('bAll').click(); 1")
        chk(bool(wait(lambda: t.js("T.open.length === 5"), 6)), '手机「全部开启」→ 教师端 5 关全开')
        chk(bool(wait(lambda: s.js("S.open.length === 5"), 8)), '学生端 5 关全开')

        # ---------------------------------------------------------------- 4
        print('\n[4] 手机控制计时')
        sec0 = t.js('T.sec')
        m.js("document.getElementById('bRun').click(); 1")
        chk(bool(wait(lambda: t.js('T.run') is True, 6)), '手机点开始 → 教师端在跑')
        chk(bool(wait(lambda: '暂停' in str(m.js("(document.getElementById('bRun')||{}).textContent")), 6)),
            '手机按钮变成「⏸ 暂停」')
        time.sleep(2.4)
        sec1 = t.js('T.sec')
        chk(sec1 < sec0, '教师端秒数真的在减（%s → %s）' % (sec0, sec1))
        d1 = m.js("""(() => { const s = document.getElementById('clk').textContent.split(':');
            return +s[0] * 60 + +s[1]; })()""")
        chk(abs((d1 or 0) - sec1) <= 2, '手机上的数字和教师端对得上（差 %ss）' % abs((d1 or 0) - sec1))

        m.js("document.getElementById('bRun').click(); 1")
        chk(bool(wait(lambda: t.js('T.run') is False, 6)), '手机再点 → 暂停')
        paused = t.js('T.sec')
        time.sleep(2.2)
        chk(t.js('T.sec') == paused, '暂停后秒数不再变（%s）' % paused)

        m.js("document.getElementById('bAdd').click(); 1")
        chk(bool(wait(lambda: t.js('T.sec') == paused + 300, 6)),
            '手机点 +5 → 教师端 +300 秒（%s → %s）' % (paused, t.js('T.sec')))
        m.js("document.getElementById('bZero').click(); 1")
        chk(bool(wait(lambda: t.js('T.sec') == t.js('T.lvSec(T.curLv())'), 6)),
            '手机点归零 → 回到本关时长（%s 秒）' % t.js('T.sec'))
        chk(t.js('T.sec') == 720, '全开时当前关 = 第 5 关，12 分钟 = 720 秒')
        chk(t.js('T.run') is False, '归零后处于暂停态')
        m.shot(os.path.join(shots, 'skill_remote_phone_ctl.png'))

        # ---------------------------------------------------------------- 5
        print('\n[5] 布局：1366 不溢出、手机不横滚')
        r = t.js("""(() => {
          const card = document.querySelector('#cvRun .card');
          const mqr = document.getElementById('mqrBox');
          const cr = card.getBoundingClientRect(), mr = mqr.getBoundingClientRect();
          return { cardBottom: Math.round(cr.bottom), mqrBottom: Math.round(mr.bottom),
                   mqrH: Math.round(mr.height),
                   overflow: card.scrollHeight - card.clientHeight };
        })()""")
        chk((r.get('mqrH') or 0) >= 90, '1366 下遥控块高度正常（%spx）' % r.get('mqrH'))
        chk((r.get('mqrBottom') or 9999) <= (r.get('cardBottom') or 0) + 1,
            '遥控块没被顶出卡片（%s / %s）' % (r.get('mqrBottom'), r.get('cardBottom')))
        chk((r.get('overflow') or 0) <= 1, '卡片没有内容溢出（overflow %spx）' % r.get('overflow'))

        r2 = m.js("""(() => {
          const de = document.documentElement;
          window.scrollTo(0, 600);
          return { sw: de.scrollWidth, iw: window.innerWidth, sy: Math.round(window.scrollY) };
        })()""")
        chk((r2.get('sw') or 0) <= (r2.get('iw') or 0) + 1,
            '手机上没有横向滚动（%s / %s）' % (r2.get('sw'), r2.get('iw')))
        chk((r2.get('sy') or 0) > 100, '手机页面能往下滚（scrollY=%s）' % r2.get('sy'))
        top = m.js("Math.round(document.getElementById('bar').getBoundingClientRect().top)")
        chk(top is not None and abs(top) <= 1, '计时条吸顶（滚动后 top=%s）' % top)
        m.js('window.scrollTo(0, 0); 1')

        # ---------------------------------------------------------------- 6
        print('\n[6] 学生端不该出现遥控入口')
        raw = open(os.path.join(dist, 'app_学生端.html'), 'rb').read().decode('utf-8', 'ignore')
        chk('遥控台' not in raw and 'bRun' not in raw, '学生端没有遥控台代码')
        chk('mlv' not in raw and 'mtimer' not in raw, '学生端没有遥控协议')
    finally:
        for b in (m, s):
            if b:
                b.kill()
        t.kill()
        srv.terminate()

    print('-' * 60)
    print('%d 项通过，%d 项失败' % (len(OK), len(BAD)))
    if BAD:
        for b in BAD:
            print('  · ' + b)
        sys.exit(1)


if __name__ == '__main__':
    main()
