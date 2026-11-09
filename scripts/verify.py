# -*- coding: utf-8 -*-
"""双端产物体检：角色隔离 / 外链 / 语法 / 标记残留 / 渲染。

用法：
    python verify.py <dist目录或单个html> [--render] [--src <app.src.html>]

检查项
  1. 外链扫描：https?:// 、//cdn 、chrome-extension:// 、@import url(http
  2. 每个 <script> 块过 node --check（找不到 node 就跳过并提示）
  3. 角色隔离：教师端产物里不许出现学生端对象/页面，反之亦然
  4. 标记残留：#if / #endif / __ROLE_LABEL__ / window.__ROLE__ = ''
  5. --render：1366×768 无头截图，PNG 非 0 字节算过
"""
import glob
import io
import os
import re
import subprocess
import sys

NODE_CANDIDATES = [
    r'C:\Users\jiash\.workbuddy\binaries\node\versions\22.22.2-6\node.exe',
]
EDGE_CANDIDATES = [
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
]

STUDENT_ONLY = ['const S = {', "id=\"pg-join\"", "id=\"pg-student\"", 'id="cert-name"', 'S.report(']
TEACHER_ONLY = ['const T = {', 'id="pg-teacher"', 'id="t-tabs"', 'T.init()', 'T.hb(']

FAIL = []


def find(exe, name):
    for p in exe:
        if os.path.isfile(p):
            return p
    from shutil import which
    return which(name)


def check_html(path, node, render):
    name = os.path.basename(path)
    s = io.open(path, encoding='utf-8').read()
    is_teacher = '教师端' in name
    print('=' * 66)
    print('%s  %.1f KB' % (name, len(s.encode('utf-8')) / 1024.0))

    # 1) 外链
    ext = []
    for m in re.finditer(r'(?:src|href)\s*=\s*["\'](https?:)?//[^"\']+', s):
        ext.append(m.group(0)[:90])
    for m in re.finditer(r'@import\s+url\(\s*["\']?https?://[^)]+', s):
        ext.append(m.group(0)[:90])
    ext = sorted(set(ext))
    print('  外链 %d 处%s' % (len(ext), '' if not ext else ' ← 必须本地化'))
    for e in ext[:6]:
        print('     ', e)
    if ext:
        FAIL.append('%s 有外链' % name)

    # 2) 语法
    blocks = re.findall(r'<script[^>]*>(.*?)</script>', s, re.S)
    print('  script 块 %d 个' % len(blocks))
    if node:
        for i, b in enumerate(blocks):
            tmp = os.path.join(os.path.dirname(os.path.abspath(path)), '_chk_%d.js' % i)
            io.open(tmp, 'w', encoding='utf-8').write(b)
            r = subprocess.run([node, '--check', tmp], capture_output=True, text=True)
            ok = r.returncode == 0
            print('    #%d len=%-6d %s' % (i, len(b), 'OK' if ok else 'FAIL'))
            if not ok:
                print('      ', (r.stderr or '')[:300])
                FAIL.append('%s script#%d 语法错' % (name, i))
            try:
                os.remove(tmp)
            except OSError:
                pass
    else:
        print('    跳过（没找到 node）')

    # 3) 角色隔离
    wrong = STUDENT_ONLY if is_teacher else TEACHER_ONLY
    hit = [t for t in wrong if t in s]
    print('  角色隔离：%s' % ('通过' if not hit else '串味 ' + str(hit)))
    if hit:
        FAIL.append('%s 混入了另一端的代码 %s' % (name, hit))

    # 4) 标记残留
    left = []
    if re.search(r'#if\s+[A-Z]', s):
        left.append('#if')
    if '#endif' in s:
        left.append('#endif')
    if '__ROLE_LABEL__' in s:
        left.append('__ROLE_LABEL__')
    if "window.__ROLE__ = '';" in s:
        left.append('ROLE 未注入')
    print('  标记残留：%s' % ('无' if not left else str(left)))
    if left:
        FAIL.append('%s 残留 %s' % (name, left))

    # 5) 渲染
    if render:
        edge = find(EDGE_CANDIDATES, 'msedge')
        if not edge:
            print('  渲染：跳过（没找到 msedge）')
        else:
            png = os.path.join(os.path.dirname(os.path.abspath(path)), '_shot_%s.png' % name[:6])
            prof = os.path.join(os.path.dirname(os.path.abspath(path)), '_prof_%s' % name[:6])
            url = 'file:///' + os.path.abspath(path).replace('\\', '/').replace(' ', '%20')
            cmd = [edge, '--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-first-run',
                   '--disable-extensions', '--user-data-dir=' + prof, '--window-size=1366,768',
                   '--screenshot=' + png, url]
            subprocess.run(cmd, capture_output=True, timeout=120)
            size = os.path.getsize(png) if os.path.exists(png) else 0
            print('  渲染 1366×768：%s（%d 字节）' % ('OK' if size > 10000 else '疑似空白', size))
            if size <= 10000:
                FAIL.append('%s 渲染疑似失败' % name)
            for p in (png,):
                try:
                    os.remove(p)
                except OSError:
                    pass


def main():
    args = [x for x in sys.argv[1:] if not x.startswith('--')]
    render = '--render' in sys.argv
    if not args:
        print('用法：python verify.py <dist目录|html> [--render]')
        raise SystemExit(2)
    target = args[0]
    files = sorted(glob.glob(os.path.join(target, '*.html'))) if os.path.isdir(target) else [target]
    if not files:
        raise SystemExit('没找到 html：%s' % target)
    node = find(NODE_CANDIDATES, 'node')
    for f in files:
        check_html(f, node, render)
    print('=' * 66)
    if FAIL:
        print('未通过 %d 项：' % len(FAIL))
        for x in FAIL:
            print('  -', x)
        raise SystemExit(1)
    print('全部通过')


if __name__ == '__main__':
    main()
