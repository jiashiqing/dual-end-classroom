# -*- coding: utf-8 -*-
"""端到端 UI 测试：用无头浏览器打开学生端，自动报到 + 过第一关，再从订阅端核对上报。

它验证的是「页面里的 JS」这一层，smoke_sync.py 只验证协议层，两者互补。

用法：
    python e2e_ui.py --dir <产物目录> [--port 8100]

前置：Windows + 装了 Edge；没有 Edge 会直接跳过（不算失败）。
说明：SSE 是长连接，加了 --virtual-time-budget 之后虚拟时钟会被冻住，
      页面里 setTimeout 不再推进（实测卡在报到这一步）。所以这里不用 DOM dump，
      改成真时间后台跑、到点杀进程，用「服务器有没有收到 join/prog」来判定。
"""
import argparse
import http.client
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
SRV = os.path.join(SKILL, 'assets', 'sync_server.py')
EDGE = [r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
        r'C:\Program Files\Microsoft\Edge\Application\msedge.exe']
CODE = '4321'

AUTO = """<script>
(function(){
  function q(s){return document.querySelector(s);}
  setTimeout(function(){
    var c=q('#j-code'), n=q('#j-name'), g=q('#j-groups .grp[data-g="3"]'), go=q('#j-go');
    if(!c||!n||!go) return;
    c.value='%s'; n.value='测试甲'; if(g) g.click(); go.click();
    setTimeout(function(){
      var a=q('#s-task .q[data-i="0"] .opt[data-j="0"]'); if(a) a.click();
      var b=q('#s-task .q[data-i="1"] .opt[data-j="1"]'); if(b) b.click();
      var s=q('#s-submit'); if(s) s.click();
    }, 600);
  }, 500);
})();
</script>
"""

got = []


def pick_edge():
    for p in EDGE:
        if os.path.isfile(p):
            return p
    return None


def free_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p


def wait_up(port, timeout=12):
    end = time.time() + timeout
    while time.time() < end:
        try:
            c = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
            c.request('GET', '/healthz')
            ok = c.getresponse().status == 200
            c.close()
            if ok:
                return True
        except Exception:
            time.sleep(0.2)
    return False


def subscribe(port, chan, stop):
    try:
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=60)
        c.request('GET', '/events?channel=' + chan)
        r = c.getresponse()
        buf = b''
        while time.time() < stop:
            ch = r.read(1)
            if not ch:
                break
            buf += ch
            while b'\n\n' in buf:
                frame, buf = buf.split(b'\n\n', 1)
                for line in frame.split(b'\n'):
                    if line.startswith(b'data: '):
                        d = json.loads(line[6:].decode('utf-8'))
                        try:
                            got.append(json.loads(d['data']))
                        except Exception:
                            pass
        c.close()
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', required=True)
    ap.add_argument('--port', type=int, default=0)
    a = ap.parse_args()
    d = os.path.abspath(a.dir)
    stu = ''
    for f in sorted(os.listdir(d)):
        if f.lower().endswith('.html') and '学生端' in f:
            stu = f
            break
    if not stu:
        raise SystemExit('目录里没有学生端 HTML：%s' % d)
    edge = pick_edge()
    if not edge:
        print('跳过：没找到 Edge')
        return

    # 造一个自动操作的副本（不进产物目录，服务端按文件名直接取）
    tmpdir = tempfile.mkdtemp(prefix='de_e2e_')
    src = io.open(os.path.join(d, stu), encoding='utf-8').read()
    auto = src.replace('</body>', AUTO % CODE + '</body>')
    auto_path = os.path.join(d, '_auto_stu.html')
    io.open(auto_path, 'w', encoding='utf-8').write(auto)

    port = a.port or free_port()
    srv = subprocess.Popen([sys.executable, SRV, '--dir', d, '--port', str(port)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    prof = os.path.join(tmpdir, 'prof')
    os.makedirs(prof, exist_ok=True)
    errs = []
    try:
        if not wait_up(port):
            raise SystemExit('服务没起来')
        print('服务 127.0.0.1:%d' % port)

        stop = time.time() + 9
        t = threading.Thread(target=subscribe, args=(port, 'de_v1_' + CODE, stop), daemon=True)
        t.start()
        time.sleep(0.6)

        cmd = [edge, '--headless=new', '--disable-gpu', '--no-first-run', '--disable-extensions',
               '--user-data-dir=' + prof, '--window-size=1366,768',
               'http://127.0.0.1:%d/_auto_stu.html' % port]
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t.join()
        try:
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(p.pid)],
                           capture_output=True, timeout=15)
        except Exception:
            p.kill()
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=6)
        except Exception:
            srv.kill()
        try:
            os.remove(auto_path)
        except OSError:
            pass

    kinds = [m.get('t') for m in got]
    print('订阅端收到 %d 条：%s' % (len(got), kinds))
    join = [m for m in got if m.get('t') == 'join']
    prog = [m for m in got if m.get('t') == 'prog']
    if not join:
        errs.append('没收到 join：学生端报到后没有成功上行')
    else:
        print('  join  → 姓名 %s / 第 %s 组' % (join[0].get('name'), join[0].get('g')))
        if join[0].get('name') != '测试甲':
            errs.append('join 姓名不对：%s' % join[0].get('name'))
        if join[0].get('g') != 3:
            errs.append('join 分组不对：%s' % join[0].get('g'))
    if not prog:
        errs.append('没收到 prog：过关后没有上报进度')
    else:
        print('  prog  → 已过关 %s 关' % prog[-1].get('done'))
        if (prog[-1].get('done') or 0) < 1:
            errs.append('prog 进度没变化：%s' % prog[-1])
    print('-' * 60)
    if errs:
        for e in errs:
            print('  -', e)
        raise SystemExit(1)
    print('端到端通过')


if __name__ == '__main__':
    main()
