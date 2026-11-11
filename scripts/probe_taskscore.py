# -*- coding: utf-8 -*-
"""举手抢答 / 加分独立页 / 手机加分台 / 学生端本课任务 —— 四项新功能专项。

  1. 大屏那块从「举手求助」改成「举手抢答」：按第一次举手的先后编号，
     放下再举要排到队尾；老师点一下就把他划掉，并通知学生
  2. 「小组加分」从课堂运行搬到独立页签，卡片带 −1/+1/+5 与二维码
  3. 手机扫码进 /m?code=xxxx 能加分，且真的落到教师端账上（跨进程链路）
  4. 学生端：进课堂弹一次本课任务，关掉后顶栏「任务」随时再开，任何页都在

用法：python scripts/probe_taskscore.py --dir dist
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
ROOT = os.path.dirname(HERE)
PY = sys.executable

OK, BAD = [], []


def ok(msg):
    OK.append(msg)
    print("  ok   " + msg)


def bad(msg):
    BAD.append(msg)
    print("  BAD  " + msg)


def chk(cond, msg):
    (ok if cond else bad)(msg)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Browser:
    def __init__(self, port, profile, url, w=1366, h=768):
        self.proc = subprocess.Popen([
            EDGE, "--headless=new", f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}", f"--window-size={w},{h}",
            "--hide-scrollbars", "--no-first-run", "--no-default-browser-check", url,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ws_url = None
        for _ in range(80):
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=2) as r:
                    for t in json.load(r):
                        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                            ws_url = t["webSocketDebuggerUrl"]
                            break
            except Exception:
                pass
            if ws_url:
                break
            time.sleep(0.25)
        if not ws_url:
            raise SystemExit("浏览器没起来：%d" % port)
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self._id = 0
        self.cdp("Runtime.enable")

    def cdp(self, method, params=None):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self._id:
                if "error" in m:
                    raise RuntimeError("%s: %s" % (method, m["error"]))
                return m.get("result", {})

    def js(self, expr):
        r = self.cdp("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                          "awaitPromise": True})
        if r.get("exceptionDetails"):
            return {"__err": str(r["exceptionDetails"])[:300]}
        return r.get("result", {}).get("value")

    def shot(self, path):
        d = self.cdp("Page.captureScreenshot", {"format": "png"})
        with open(path, "wb") as f:
            f.write(base64.b64decode(d["data"]))

    def kill(self):
        try:
            self.ws.close()
        except Exception:
            pass
        self.proc.terminate()


def wait(cond, timeout=8.0, step=0.2):
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
    except Exception:
        return ""
    raw = base64.b64decode(data_url.split(",", 1)[1])
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return ""
    det = cv2.QRCodeDetector()
    for im in (img, cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)):
        txt, _, _ = det.detectAndDecode(im)
        if txt:
            return txt
    return ""


def _healthy(base):
    try:
        with urllib.request.urlopen(base + "/healthz", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--port", type=int, default=0)
    a = ap.parse_args()
    d = os.path.abspath(a.dir)
    port = a.port or free_port()
    base = "http://127.0.0.1:%d" % port

    print("启动服务器（%s）… 端口 %d" % (d, port))
    srv = subprocess.Popen([PY, os.path.join(d, "sync_server.py"), "--port", str(port), "--dir", d],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not wait(lambda: _healthy(base), 12):
        srv.terminate()
        raise SystemExit("服务器没起来")

    tmp = os.environ.get("TEMP", "/tmp")
    prof = [os.path.join(tmp, "prof_ts_" + n) for n in ("T", "S", "M")]
    for p in prof:
        shutil.rmtree(p, ignore_errors=True)

    t = s = m = None
    try:
        t = Browser(free_port(), prof[0], base + "/t")
        wait(lambda: t.js("typeof T !== 'undefined'"))

        # ---------------------------------------------------------- 1 抢答
        print("\n[1] 大屏：举手抢答（按先后编号）")
        title = t.js("(document.querySelector('#t-help') && document.querySelector('#t-help').parentNode.querySelector('h4').textContent)")
        chk((title or "").strip() == "举手抢答", "面板标题是「举手抢答」· 实际 %r" % (title or "").strip())

        t.js("""(() => {
          ['h1','h2','h3'].forEach((id, i) => T.onMsg({t:'help', id:id, name:'同学'+(i+1), g:1, lv:1, help:true}));
          return 1; })()""")
        time.sleep(0.4)
        r = t.js("""[...document.querySelectorAll('#t-help .hp[data-id]')].map(e => ({
            id: e.dataset.id, no: (e.querySelector('.hno')||{}).textContent}))""")
        chk([x.get("id") for x in r] == ["h1", "h2", "h3"],
            "三个人按举手先后排：h1 h2 h3 · 实际 %s" % [x.get("id") for x in r])
        chk([x.get("no") for x in r] == ["1", "2", "3"],
            "序号是 1/2/3 · 实际 %s" % [x.get("no") for x in r])

        t.js("T.onMsg({t:'help', id:'h2', name:'同学2', g:1, lv:1, help:false}); 1")
        time.sleep(0.2)
        t.js("T.onMsg({t:'help', id:'h2', name:'同学2', g:1, lv:1, help:true}); 1")
        time.sleep(0.3)
        r = t.js("[...document.querySelectorAll('#t-help .hp[data-id]')].map(e => e.dataset.id)")
        chk(r == ["h1", "h3", "h2"], "放下再举排到队尾（h1 h3 h2）· 实际 %s" % r)

        t.js("document.querySelector('#t-help .hp[data-id]').click(); 1")
        time.sleep(0.3)
        r = t.js("({help: !!T.stu.h1.help, left: document.querySelectorAll('#t-help .hp[data-id]').length})")
        chk(r.get("help") is False and r.get("left") == 2, "点一下把他从队列划掉（剩 2 个）· 实际 %s" % r)

        # ---------------------------------------------------- 2 加分独立页
        print("\n[2] 加分页：独立页签 + 二维码")
        tabs = t.js("[...document.querySelectorAll('#t-tabs button')].map(b => b.dataset.tab)")
        chk(tabs == ["board", "data", "score", "ctrl"],
            "页签 大屏/学情/加分/控制台 · 实际 %s" % tabs)

        t.js("document.querySelector('#t-tabs button[data-tab=\"score\"]').click(); 1")
        time.sleep(0.6)
        r = t.js("""(() => {
          const g = document.getElementById('t-groups');
          return {on: document.getElementById('tab-score').classList.contains('on'),
                  cards: document.querySelectorAll('#t-groups .gcard').length,
                  ops: document.querySelectorAll('#t-groups .ops button').length,
                  inCtrl: !!document.querySelector('#cvRun #t-groups'),
                  over: g ? g.scrollHeight - g.clientHeight : -999}; })()""")
        chk(r.get("on") is True, "点「加分」切到加分页")
        chk(r.get("cards") == 8, "8 组 = 8 张卡 · 实际 %s" % r.get("cards"))
        chk(r.get("ops") == 24, "每张卡 3 个按钮（−1/+1/+5）× 8 · 实际 %s" % r.get("ops"))
        chk(r.get("inCtrl") is False, "课堂运行页里已经没有组卡（搬家成功）")
        chk((r.get("over") or 0) <= 0, "8 张卡一屏放得下 · 溢出 %s px" % r.get("over"))

        t.js("""(() => { const c = [...document.querySelectorAll('#t-groups .gcard')]
            .find(x => x.querySelector('.gt span').textContent.indexOf('3') >= 0);
          c.querySelector('button[data-act="5"]').click(); return 1; })()""")
        time.sleep(0.4)
        r = t.js("""({manual: (T.gpts[3]||{}).manual, total: T.gTotal(3),
                     wall: (() => { const rows=[...document.querySelectorAll('#wall .wrow')];
                       const row = rows.find(x => (x.querySelector('.wg')||{}).textContent.indexOf('3组')===0);
                       return row ? row.querySelector('.wpts').textContent.trim() : ''; })()})""")
        chk(r.get("manual") == 5, "点 +5 → 第 3 组教师分 5 · 实际 %s" % r.get("manual"))
        chk((r.get("wall") or "") == "5", "大屏进度墙积分列同步成 5 · 实际 %r" % r.get("wall"))

        tip = t.js("(document.getElementById('qrMTip')||{}).getAttribute('title')")
        chk("/m?code=" in (tip or ""), "二维码地址指向 /m?code=… · 实际 %r" % tip)
        code = t.js("T.code")
        qr = t.js("(() => { const cv = document.getElementById('qrCvM'); return cv && cv.width > 10 ? cv.toDataURL('image/png') : ''; })()")
        txt = decode_qr(qr) if qr else ""
        if txt:
            chk("/m?code=" + str(code) in txt, "二维码解出来确实是加分台地址 · %s" % txt)
        else:
            print("  --   二维码解码跳过（没装 opencv）")
        t.shot(os.path.join(d, "_score_page.png"))

        # ---------------------------------------------------- 3 手机加分台
        print("\n[3] 手机加分台 /m?code=%s" % code)
        with urllib.request.urlopen(base + "/m?code=%s" % code, timeout=5) as resp:
            body = resp.read().decode("utf-8")
        chk(resp.status == 200 and "小组加分台" in body, "/m 能打开加分台（%d 字节）" % len(body))

        m = Browser(free_port(), prof[2], base + "/m?code=%s" % code, w=430, h=932)
        time.sleep(1.4)
        got = wait(lambda: m.js("document.querySelectorAll('#list .c').length") == 8, 10)
        r = m.js("""(() => ({n: document.querySelectorAll('#list .c').length,
            net: (document.getElementById('net')||{}).textContent,
            g3: (() => { const c = [...document.querySelectorAll('#list .c')]
                 .find(x => x.dataset.g === '3'); return c ? c.querySelector('.n').textContent : ''; })()}))()""")
        chk(bool(got), "手机端连上并画出 8 组 · 实际 %s 组" % r.get("n"))
        chk("已连上" in (r.get("net") or ""), "手机端状态显示已连上 · %r" % r.get("net"))
        chk((r.get("g3") or "") == "5", "手机端第 3 组显示 5 分 · 实际 %r" % r.get("g3"))
        m.shot(os.path.join(d, "_score_phone.png"))

        m.js("""(() => { const c = [...document.querySelectorAll('#list .c')]
            .find(x => x.dataset.g === '2');
          c.querySelector('button[data-d="1"]').click(); return 1; })()""")
        landed = wait(lambda: (t.js("(T.gpts[2] && T.gpts[2].manual) || 0")) == 1, 8)
        chk(bool(landed), "手机点 +1 → 教师端第 2 组教师分变 1 · 实际 %s"
            % t.js("(T.gpts[2] && T.gpts[2].manual) || 0"))
        back = wait(lambda: (m.js("""(() => { const c = [...document.querySelectorAll('#list .c')]
            .find(x => x.dataset.g === '2'); return c ? c.querySelector('.n').textContent : ''; })()""")) == "1", 8)
        chk(bool(back), "分数回到手机端（教师端广播 gpts）")

        # ---------------------------------------------------- 4 学生端任务
        print("\n[4] 学生端：本课任务弹窗")
        s = Browser(free_port(), prof[1], base + "/s?g=8")
        time.sleep(1.6)
        s.js("""(() => { document.getElementById('j-code').value = '%s';
          document.getElementById('j-name').value = '张三';
          document.getElementById('j-go').click(); return 1; })()""" % code)
        joined = wait(lambda: s.js("document.getElementById('pg-student').classList.contains('on')"), 8)
        chk(bool(joined), "学生端报到进课堂")

        r = s.js("document.getElementById('s-task-mask').classList.contains('on')")
        chk(r is False, "没发布任务时不弹窗")
        r = s.js("document.getElementById('s-task-nav').hidden")
        chk(r is True, "没任务时顶栏「任务」不显示")

        t.js("""(() => { T.cfg.task = { title:'让摄像头认识三种手势',
            text:'1. 打开第 3 关\\n2. 采集三种手势各 30 张\\n3. 训练后自测 20 次\\n\\n卡住了先举手。' };
          T.persist(); T.hb(); T.paintTaskNow(); return 1; })()""")
        popped = wait(lambda: s.js("document.getElementById('s-task-mask').classList.contains('on')"), 8)
        chk(bool(popped), "发布后学生端自动弹窗（进课堂弹一次）")
        r = s.js("""({h: (document.getElementById('s-task-title')||{}).textContent,
                     b: (document.getElementById('s-task-text')||{}).textContent,
                     br: document.querySelectorAll('#s-task-text br').length,
                     nav: document.getElementById('s-task-nav').hidden})""")
        chk(r.get("h") == "让摄像头认识三种手势", "弹窗标题跟老师写的一致 · %r" % r.get("h"))
        chk("采集三种手势各 30 张" in (r.get("b") or ""), "弹窗正文完整（含换行内容）")
        chk(r.get("nav") is False, "顶栏「任务」出现（关掉后随时能再看）")
        s.shot(os.path.join(d, "_task_pop.png"))

        s.js("document.getElementById('s-task-x').click(); 1")
        time.sleep(0.25)
        chk(s.js("document.getElementById('s-task-mask').classList.contains('on')") is False, "点 ✕ 能关掉")

        s.js("document.getElementById('s-task-nav').click(); 1")
        time.sleep(0.25)
        chk(s.js("document.getElementById('s-task-mask').classList.contains('on')") is True, "顶栏「任务」能再打开")
        s.js("document.body.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true})); 1")
        time.sleep(0.25)
        chk(s.js("document.getElementById('s-task-mask').classList.contains('on')") is False, "Esc 也能关")

        # 老师清空 → 弹窗自己收；再点只提示
        t.js("(() => { T.cfg.task = { title:'', text:'' }; T.persist(); T.hb(); return 1; })()")
        cleared = wait(lambda: s.js("document.getElementById('s-task-nav').hidden"), 8)
        chk(bool(cleared), "老师清空任务 → 顶栏「任务」收起")
        if s.js("document.getElementById('s-task-mask').classList.contains('on')"):
            s.js("document.getElementById('s-task-nav').click(); 1")
            time.sleep(0.25)
            chk(s.js("document.getElementById('s-task-mask').classList.contains('on')") is False,
                "清空后不会再弹空窗")

        # 改内容 → 再弹一次
        t.js("(() => { T.cfg.task = { title:'第二版任务', text:'换成第二个任务试试' }; T.persist(); T.hb(); return 1; })()")
        again = wait(lambda: s.js("document.getElementById('s-task-mask').classList.contains('on')"), 8)
        chk(bool(again), "老师改任务内容 → 学生端再弹一次")
        r = s.js("(document.getElementById('s-task-title')||{}).textContent")
        chk(r == "第二版任务", "弹的是新标题 · %r" % r)

        # ---------------------------------------------------- 5 1366 布局
        print("\n[5] 1366×768 下的加分页与大屏")
        try:
            t.js("document.getElementById('s-task-mask') && 0")
            t.cdp("Emulation.setDeviceMetricsOverride",
                  {"width": 1366, "height": 768, "deviceScaleFactor": 1, "mobile": False})
            time.sleep(0.6)
            t.js("document.querySelector('#t-tabs button[data-tab=\"score\"]').click(); 1")
            time.sleep(0.4)
            r = t.js("""(() => {
                const g = document.getElementById('t-groups');
                const last = [...g.children].pop();
                return {docH: document.documentElement.scrollHeight,
                        vh: window.innerHeight,
                        over: g.scrollHeight - g.clientHeight,
                        clip: last ? Math.round(last.getBoundingClientRect().bottom
                              - g.getBoundingClientRect().bottom) : -999}; })()""")
            chk(r.get("docH", 9999) <= r.get("vh", 0) + 1, "1366×768 整页不滚（%s / %s）"
                % (r.get("docH"), r.get("vh")))
            chk((r.get("over") or 0) <= 0, "加分页组卡区不溢出 · %s px" % r.get("over"))
            chk((r.get("clip") or 0) <= 1, "最后一张卡没被切 · %s px" % r.get("clip"))
            t.shot(os.path.join(d, "_score_1366.png"))
            t.cdp("Emulation.clearDeviceMetricsOverride")
        except Exception as e:
            print("  --   1366 量尺寸跳过：%s" % e)

        # 学生端不该出现教师端的东西
        raw = io_read(os.path.join(d, [f for f in os.listdir(d) if "学生端" in f][0]))
        for kw in ("t-groups", "renderGroups", "qrCvM", "tkSave", "helpOK"):
            chk(kw not in raw, "学生端没有教师端标识：%s" % kw)

    finally:
        for b in (t, s, m):
            if b:
                b.kill()
        srv.terminate()

    print("\n" + "-" * 60)
    print("%d 项通过，%d 项失败" % (len(OK), len(BAD)))
    if BAD:
        for x in BAD:
            print("  ! " + x)
        sys.exit(1)


def io_read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    main()
