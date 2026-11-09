# -*- coding: utf-8 -*-
"""一份源码 → 两个角色文件（教师端 / 学生端）。

用法：
    python build_dual.py <app.src.html> [--out 输出目录] [--name 前缀] [--no-server]

源码里用行注释标记角色的作用域，构建时按目标角色裁剪：

    /* #if T */        // 只有教师端要的代码
    /* #endif */
    <!-- #if S -->      <!-- 只有学生端要的 HTML -->
    <!-- #endif -->
    /* #if DEMO */      // 只在源码页保留（构建产物一律删掉）

设计意图：**不允许出现「两份文件各一份代码」**。AI识百草那类项目后来出现的
「教师端里塞着一份旧版学生端」问题，根源就是两份文件被各自手工改过。
这里只有一份源码，两个产物都是它的裁剪，永远不会分叉。
"""
import argparse
import io
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
ASSETS = os.path.join(SKILL, 'assets')

RE_IF = re.compile(r'^(?:/\*+|<!--|//)?\s*#if\s+([A-Za-z]+)\s*(?:\*/|-->)?$')
RE_END = re.compile(r'^(?:/\*+|<!--|//)?\s*#endif\s*(?:\*/|-->)?$')


def cut(text, role):
    """按 #if 标记裁剪；role 为空串时只保留 DEMO 块（源码预览用）。"""
    out, stack = [], []
    for line in text.split('\n'):
        s = line.strip()
        m = RE_IF.match(s)
        if m:
            tag = m.group(1).upper()
            stack.append(tag == role or (tag == 'DEMO' and role == ''))
            continue
        if RE_END.match(s):
            if stack:
                stack.pop()
            continue
        if all(stack):
            out.append(line)
    if stack:
        raise SystemExit('标记没有闭合，剩余 %d 层' % len(stack))
    return '\n'.join(out)


def build(src, role, label, outdir, name):
    text = io.open(src, encoding='utf-8').read()
    body = cut(text, role)
    body = body.replace('__ROLE_LABEL__', label)
    body = body.replace("window.__ROLE__ = '';", "window.__ROLE__ = '%s';" % role)
    if '__ROLE_LABEL__' in body or "window.__ROLE__ = '';" in body:
        raise SystemExit('占位符没有被替换，检查源码里的 __ROLE_LABEL__ / window.__ROLE__')
    for tag in ('#if ', '#endif'):
        if tag in body:
            raise SystemExit('产物里还残留标记 %r' % tag)
    path = os.path.join(outdir, name)
    io.open(path, 'w', encoding='utf-8', newline='\n').write(body)
    return path, len(body.encode('utf-8'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('--out', default='')
    ap.add_argument('--name', default='')
    ap.add_argument('--no-server', action='store_true')
    a = ap.parse_args()

    src = os.path.abspath(a.src)
    if not os.path.isfile(src):
        raise SystemExit('找不到源码：%s' % src)
    outdir = os.path.abspath(a.out or os.path.join(os.path.dirname(src), 'dist'))
    os.makedirs(outdir, exist_ok=True)
    stem = a.name or os.path.splitext(os.path.basename(src))[0]
    if stem.endswith('.src'):
        stem = stem[:-4]

    print('源码 %s' % os.path.basename(src))
    jobs = [
        ('T', '教师端', '%s-教师端.html' % stem),
        ('S', '学生端', '%s_学生端.html' % stem),
    ]
    made = []
    for role, label, fname in jobs:
        p, n = build(src, role, label, outdir, fname)
        made.append(p)
        print('  %-22s %7.1f KB' % (fname, n / 1024.0))

    # qr.js 是二维码编码器（零依赖）：页面用 <script src="qr.js"> 引，
    # 产物必须和 HTML 放在同一目录，少拷一个文件二维码就画不出来。
    need = ['qr.js']
    if not a.no_server:
        need += ['sync_server.py', 'start_server.bat']
    for f in need:
        s = os.path.join(ASSETS, f)
        if os.path.isfile(s):
            shutil.copyfile(s, os.path.join(outdir, f))
            print('  %-22s 已复制' % f)
        else:
            raise SystemExit('assets 里缺 %s，产物不完整' % f)

    # 引了 qr.js 就必须真的在目录里，否则教师端二维码静默变白框
    for role, label, fname in jobs:
        p = os.path.join(outdir, fname)
        if 'src="qr.js"' in io.open(p, encoding='utf-8').read():
            if not os.path.isfile(os.path.join(outdir, 'qr.js')):
                raise SystemExit('%s 引用了 qr.js 但产物目录里没有' % fname)

    print('\n输出目录：%s' % outdir)
    print('启动上课：双击 start_server.bat，学生机开 http://本机IP:8099/s 或扫二维码')


if __name__ == '__main__':
    main()
