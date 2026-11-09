# -*- coding: utf-8 -*-
"""同步链路冒烟测试：起服务 → 两个学生订阅 → 广播 → 跨频道隔离 →（可选）断开回收。

用法：
    python smoke_sync.py --dir <产物目录> [--port 8099] [--reclaim]

全程只用标准库，不需要浏览器。--reclaim 会额外等 ~18 秒，验证掉线客户端被回收
（这一步别省：不做探活的话，一节课下来僵死订阅会把线程和队列占满）。
"""
import argparse
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRV = os.path.join(os.path.dirname(HERE), 'assets', 'sync_server.py')

fail = []


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_up(port, timeout=12):
    end = time.time() + timeout
    while time.time() < end:
        try:
            c = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
            c.request('GET', '/healthz')
            if c.getresponse().status == 200:
                c.close()
                return True
            c.close()
        except Exception:
            time.sleep(0.2)
    return False


def publish(port, chan, sender, obj):
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=8)
    body = json.dumps({'channel': chan, 'sender': sender,
                       'data': json.dumps(obj, ensure_ascii=False)})
    c.request('POST', '/send', body=body, headers={'Content-Type': 'application/json'})
    r = c.getresponse()
    j = json.loads(r.read().decode('utf-8'))
    c.close()
    return j


def health(port):
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    c.request('GET', '/healthz')
    j = json.loads(c.getresponse().read().decode('utf-8'))
    c.close()
    return j


def subscribe(port, chan, bucket, until):
    try:
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=60)
        c.request('GET', '/events?channel=' + chan)
        r = c.getresponse()
        if r.status != 200:
            bucket.append({'_status': r.status})
            return
        buf = b''
        while time.time() < until:
            ch = r.read(1)
            if not ch:
                break
            buf += ch
            while b'\n\n' in buf:
                frame, buf = buf.split(b'\n\n', 1)
                for line in frame.split(b'\n'):
                    if line.startswith(b'data: '):
                        bucket.append(json.loads(line[6:].decode('utf-8')))
        c.close()
    except Exception as e:
        bucket.append({'_err': str(e)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', required=True)
    ap.add_argument('--port', type=int, default=0)
    ap.add_argument('--reclaim', action='store_true')
    a = ap.parse_args()
    d = os.path.abspath(a.dir)
    if not os.path.isdir(d):
        raise SystemExit('目录不存在：%s' % d)
    port = a.port or free_port()

    proc = subprocess.Popen([sys.executable, SRV, '--dir', d, '--port', str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        if not wait_up(port):
            print('服务没起来。输出：')
            proc.kill()
            print((proc.stdout.read() or b'').decode('utf-8', 'replace')[:2000])
            raise SystemExit(1)
        print('服务已就绪 127.0.0.1:%d' % port)

        # 1) 静态托管
        for path, kw in (('/s', '学生端'), ('/t', '教师端')):
            c = http.client.HTTPConnection('127.0.0.1', port, timeout=20)
            c.request('GET', path)
            r = c.getresponse()
            n = len(r.read())
            print('  %-4s %d  %d 字节' % (path, r.status, n))
            if r.status != 200 or n < 2000:
                fail.append('%s 没取到文件（%d, %d 字节）' % (path, r.status, n))
            c.close()

        # 2) 双订阅 + 广播
        chan, other = 'de_v1_1234', 'de_v1_9999'
        A, B, C = [], [], []
        until = time.time() + 6
        ts = [threading.Thread(target=subscribe, args=(port, chan, A, until), daemon=True),
              threading.Thread(target=subscribe, args=(port, chan, B, until), daemon=True),
              threading.Thread(target=subscribe, args=(port, other, C, until), daemon=True)]
        for t in ts:
            t.start()
        time.sleep(1.0)
        r1 = publish(port, chan, 'uT', {'t': 'hb', 'open': [1, 2], 'sec': 900})
        r2 = publish(port, chan, 'uS', {'t': 'prog', 'id': 's1', 'done': 2})
        r3 = publish(port, other, 'uX', {'t': 'noise'})
        print('  广播返回人数：%d / %d / 跨频道 %d' % (r1.get('n', -1), r2.get('n', -1), r3.get('n', -1)))
        for t in ts:
            t.join()
        print('  A 收到 %d 条，B 收到 %d 条，旁听频道 %d 条' % (len(A), len(B), len(C)))
        for tag, got in (('A', A), ('B', B)):
            if len(got) != 2:
                fail.append('%s 收到 %d 条，应为 2 条（%s）' % (tag, len(got), got))
            senders = sorted(x.get('sender') for x in got)
            if senders != ['uS', 'uT']:
                fail.append('%s 的 sender 不对：%s' % (tag, senders))
            if got and json.loads(got[0]['data']).get('t') != 'hb':
                fail.append('%s 首条不是 hb' % tag)
            if any(x.get('sender') == 'uX' for x in got):
                fail.append('%s 收到了别的频道的消息（隔离失败）' % tag)
        # 旁听频道只该收到自己那条，且不该收到主频道的
        if len(C) != 1 or C[0].get('sender') != 'uX':
            fail.append('旁听频道应只收到 uX 一条，实际：%s' % C)

        # 3) 断开回收
        if a.reclaim:
            print('  等待 18 秒看重连/回收 …')
            time.sleep(18)
            h = health(port)
            print('  healthz：%s' % h)
            if h.get('channels'):
                fail.append('掉线客户端没被回收：%s' % h['channels'])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=6)
        except Exception:
            proc.kill()

    print('-' * 60)
    if fail:
        print('未通过：')
        for x in fail:
            print('  -', x)
        raise SystemExit(1)
    print('同步链路通过')


if __name__ == '__main__':
    main()
