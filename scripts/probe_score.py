# -*- coding: utf-8 -*-
"""小组积分 —— 真浏览器专项

按课堂真实时序把学生「推」进来（先报到 done=0，再一关一关往上走），
检查分值、幂等、两榜切换、进度墙积分列、清空归零、学生端隔离。

查 12 件事：
  1. 大屏右栏默认是「小组积分榜」，带「小组 / 个人」切换
  2. 刚报到时一分不发（不能靠「人少」白拿覆盖分）
  3. 第 2 组先全员通关 = 18 分（覆盖 8 + 冲线 10），且排第一
  4. 第 1 组后到 = 15 分，榜首仍是第 2 组（先到先得）
  5. 覆盖分不含最后一关（那一关由冲线奖体现）
  6. 3 人过 3 关 + 1 人过 2 关 = 只拿前两关的 4 分
  7. 重复判定不重复发分（幂等）
  8. 教师 +1 只加 1 分，不按人数乘；个人得分仍各记一笔
  9. 进度墙有「积分」列、冲线组挂「冲」徽标、KPI「当前领先」跟着变
 10. 两榜切换标题与内容都换
 11. 清空课堂积分归零
 12. 学生端没有积分面板（角色隔离）

依赖：websocket-client（缺了会跳过，不算失败）
用法：python scripts/probe_score.py --dir dist [--w 1600 --h 900]
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

try:
    import websocket
except ImportError:  # noqa: BLE001
    print('跳过：没装 websocket-client（pip install websocket-client）')
    sys.exit(0)

EDGE = [r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
        r'C:\Program Files\Microsoft\Edge\Application\msedge.exe']
HERE = os.path.dirname(os.path.abspath(__file__))

N = [0, 0]
BAD = []


def ok(msg):
    N[0] += 1
    print('  ok   ' + msg)


def bad(msg):
    N[1] += 1
    BAD.append(msg)
    print('  FAIL ' + msg)


def chk(cond, msg):
    (ok if cond else bad)(msg)


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default=os.path.join(HERE, '..', 'dist'))
    ap.add_argument('--w', type=int, default=1600)
    ap.add_argument('--h', type=int, default=900)
    a = ap.parse_args()
    dist = os.path.abspath(a.dir)
    if not os.path.isdir(dist):
        raise SystemExit('目录不存在：' + dist)

    sv_port, db_port = free_port(), free_port()
    srv = subprocess.Popen([sys.executable, os.path.join(dist, 'sync_server.py'),
                            '--dir', dist, '--port', str(sv_port)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.6)
    tmp = os.environ.get('TEMP', '.')
    prof = os.path.join(tmp, 'prof_sc_skill')
    shutil.rmtree(prof, ignore_errors=True)
    br = Browser(db_port, prof, 'http://127.0.0.1:%d/t' % sv_port, a.w, a.h)
    try:
        if not wait(lambda: br.js('typeof T !== "undefined" && !!T.stu')):
            raise SystemExit('教师端没起来')

        r = br.js("""(() => {
          const seg = document.querySelectorAll('#t-rank-seg button');
          const onEl = document.querySelector('#t-rank-seg .on');
          return {n: seg.length, title: (document.querySelector('#t-rank-title')||{}).textContent,
                  on: onEl ? onEl.getAttribute('data-rk') : null};
        })()""")
        chk(r.get('n') == 2, '右栏有两个切换按钮 · 实得 %s' % r.get('n'))
        chk(r.get('title') == '小组积分榜', '默认停在小组积分榜 · 实得 %s' % r.get('title'))
        chk(r.get('on') == 'g', '默认选中「小组」· 实得 %s' % r.get('on'))

        # ---- 全班先报到（done=0），再一关一关推
        br.js("""(() => {
          localStorage.clear();
          T.stu = {}; T.gpts = {}; T.award = {fin:[], lv:{}};
          T.cfg.groups = 8; T.rankView = 'g';
          const plan = {1:['a1','a2','a3'], 2:['b1','b2','b3'],
                        3:['c1','c2','c3','c4'], 4:['d1','d2']};
          Object.keys(plan).forEach(g => plan[g].forEach(id =>
            T.onMsg({t:'join', id:id, name:'学' + id, g:+g, done:0, lv:1})));
          T.render(); return Object.keys(T.stu).length;
        })()""")
        time.sleep(0.3)
        r = br.js('T.gTotal(1)+T.gTotal(2)+T.gTotal(3)+T.gTotal(4)')
        chk(r == 0, '刚报到时没有任何自动分 · 实得 %s' % r)

        # ---- 第 2 组先全员通关
        br.js("""(() => { ['b1','b2','b3'].forEach(id =>
          T.onMsg({t:'prog', id:id, g:2, done:5, lv:5})); return 1; })()""")
        time.sleep(0.25)
        r = br.js("({t:T.gTotal(2), f:T.award.fin.slice()})")
        chk(r.get('t') == 18, '第 2 组先冲线 = 18 分（覆盖 8 + 冲线 10）· 实得 %s' % r.get('t'))
        chk(r.get('f') == [2], '冲线顺序记成 [2] · 实得 %s' % r.get('f'))

        # ---- 第 1 组后到
        br.js("""(() => { ['a1','a2','a3'].forEach(id =>
          T.onMsg({t:'prog', id:id, g:1, done:5, lv:5})); return 1; })()""")
        time.sleep(0.25)
        r = br.js("({t:T.gTotal(1), f:T.award.fin.slice()})")
        chk(r.get('t') == 15, '第 1 组后到 = 15 分（覆盖 8 + 冲线 7）· 实得 %s' % r.get('t'))
        chk(r.get('f') == [2, 1], '先到先得，顺序 [2,1] · 实得 %s' % r.get('f'))

        # ---- 覆盖分：3 人过 3 关、1 人卡在 2 关
        br.js("""(() => {
          T.onMsg({t:'prog', id:'c1', g:3, done:3, lv:3});
          T.onMsg({t:'prog', id:'c2', g:3, done:3, lv:3});
          T.onMsg({t:'prog', id:'c3', g:3, done:3, lv:3});
          T.onMsg({t:'prog', id:'c4', g:3, done:2, lv:2});
          return 1; })()""")
        time.sleep(0.25)
        r = br.js("({t:T.gTotal(3), lv:Object.keys(T.award.lv).sort()})")
        chk(r.get('t') == 4, '3 人过 3 关 + 1 人过 2 关 = 4 分（1/2 关各 2 分）· 实得 %s' % r.get('t'))
        chk('3-3' not in r.get('lv', []), '有人没过第 3 关 → 不记 3-3')
        r = br.js("Object.keys(T.award.lv).filter(k => k.endsWith('-5')).length")
        chk(r == 0, '覆盖分不含最后一关（由冲线奖承担）')

        # ---- 幂等
        before = br.js('T.gTotal(1)+T.gTotal(2)+T.gTotal(3)')
        br.js('T.judgeAwards(); T.judgeAwards(); 1')
        after = br.js('T.gTotal(1)+T.gTotal(2)+T.gTotal(3)')
        chk(before == after, '重复判定不重复发分（%s → %s）' % (before, after))

        # ---- 教师分
        br.js('T.addScore(3, 1); 1')
        time.sleep(0.2)
        r = br.js("({t:T.gTotal(3), m:T.gpts[3].manual, s:T.stu.c1.score})")
        chk(r.get('t') == 5, '教师 +1 后第 3 组 = 5 分 · 实得 %s' % r.get('t'))
        chk(r.get('m') == 1, '教师分只记 1（不是 4 人 × 1）· 实得 %s' % r.get('m'))
        chk(r.get('s') == 1, '个人得分仍各记 1 · 实得 %s' % r.get('s'))

        # ---- 进度墙 / KPI
        r = br.js("""(() => {
          const wall = [...document.querySelectorAll('#wall .wrow')];
          const cell = (row, cls) => { const e = row.querySelector('.' + cls); return e ? e.textContent.trim() : null; };
          const g2 = wall.find(r => cell(r, 'wg') && cell(r, 'wg').indexOf('2组') === 0);
          const g1 = wall.find(r => cell(r, 'wg') && cell(r, 'wg').indexOf('1组') === 0);
          return {
            rows: wall.length,
            head: (wall[0] || {}).textContent,
            g2pts: g2 ? cell(g2, 'wpts') : null,
            g2fin: g2 ? !!g2.querySelector('.wfin') : null,
            g1fin: g1 ? !!g1.querySelector('.wfin') : null,
            lead: (document.querySelector('#t-lead') || {}).textContent,
            first: (document.querySelector('#t-rank li') || {}).textContent,
          };
        })()""")
        chk(r.get('rows') == 9, '进度墙 1 表头 + 8 组 · 实得 %s' % r.get('rows'))
        chk('积分' in (r.get('head') or ''), '表头有「积分」列')
        chk(r.get('g2pts') == '18', '第 2 组积分列 = 18 · 实得 %s' % r.get('g2pts'))
        chk(r.get('g2fin') and r.get('g1fin'), '冲线的两组都挂了「冲」徽标')
        chk(r.get('lead') == '第2组', 'KPI 当前领先 = 第2组 · 实得 %s' % r.get('lead'))
        chk('第2组' in (r.get('first') or '') and '18' in (r.get('first') or ''),
            '榜首是第 2 组 18 分 · 实际「%s」' % (r.get('first') or ''))

        # ---- 两榜切换
        br.js("document.querySelector('#t-rank-seg button[data-rk=\"s\"]').click(); 1")
        time.sleep(0.25)
        r = br.js("""({t:(document.querySelector('#t-rank-title')||{}).textContent,
                      first:(document.querySelector('#t-rank li')||{}).textContent})""")
        chk(r.get('t') == '实时排行榜', '切到个人榜标题变了 · 实得 %s' % r.get('t'))
        chk('关' in (r.get('first') or ''), '个人榜显示「N 关」· 实际「%s」' % (r.get('first') or '')[:40])
        br.js("document.querySelector('#t-rank-seg button[data-rk=\"g\"]').click(); 1")
        time.sleep(0.25)

        # ---- 1366 布局
        br.size(1366, 768)
        time.sleep(0.8)
        r = br.js("""(() => {
          const b = document.querySelector('#tab-board');
          const rank = document.querySelector('#t-rank');
          return {docH: document.documentElement.scrollHeight, vh: innerHeight,
                  bOver: Math.round(b.scrollHeight - b.clientHeight),
                  rankOver: Math.round(rank.scrollHeight - rank.clientHeight)};
        })()""")
        chk(r.get('docH', 9999) <= r.get('vh', 0), '1366×768 整页不滚')
        chk(r.get('bOver', 99) <= 1, '大屏页不溢出（%s px）' % r.get('bOver'))
        chk(r.get('rankOver', 99) <= 0, '积分榜不被裁（%s px）' % r.get('rankOver'))
        br.shot(os.path.join(dist, '_score_1366.png'))   # 和 probe_ai 一样，截图丢测试目录

        # ---- 清空
        br.size(a.w, a.h)
        time.sleep(0.4)
        r = br.js("""(() => { T.reset();
          return {g1:T.gTotal(1), g2:T.gTotal(2), fin:T.award.fin.length,
                  lv:Object.keys(T.award.lv).length,
                  rows:document.querySelectorAll('#t-rank li').length}; })()""")
        chk(r.get('g1') == 0 and r.get('g2') == 0, '清空后积分归零')
        chk(r.get('fin') == 0 and r.get('lv') == 0, '清空后冲线/覆盖记录也清掉')

        # ---- 学生端隔离
        st = [f for f in os.listdir(dist) if '学生端' in f]
        if st:
            raw = open(os.path.join(dist, st[0]), 'rb').read().decode('utf-8', 'ignore')
            chk('t-rank-seg' not in raw and 'gpts' not in raw,
                '学生端没有积分面板与 gpts 字段')
    finally:
        br.kill()
        srv.terminate()

    print('-' * 60)
    print('%d 项通过，%d 项失败' % (N[0], N[1]))
    if BAD:
        for b in BAD:
            print('  · ' + b)
        sys.exit(1)


if __name__ == '__main__':
    main()
