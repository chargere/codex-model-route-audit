#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读扫描 Codex rollout，提取每次任务的首段 assistant 回复，按社区
「鹈鹕测试」措辞指纹判据打标。

用法:
  python pelican_fingerprint_check.py                      # 默认最近两天
  python pelican_fingerprint_check.py --date 2026-11-09    # 可重复传
  python pelican_fingerprint_check.py --date 2026-11-09 --html "C:/path/to/pelican.html"

判据来源（2026-09 社区，见 output/内嵌SVG指纹核查报告.md）:
  降智侧: 首段出现「内嵌 SVG」「内联 SVG」              -> 90% 疑似被路由到 GPT-4o（非 100%）
          首段出现「循环骑行 / 连续循环 / 循环运动」等   -> 社区称基本 100%
          首段出现「好的，为您生成了内嵌 SVG 和 CSS」     -> 4o 特征性套话
  满血侧: 首段出现「踩踏」「沿途风景」「背景移动」        -> 满血 Astra

重要: 这只是「输出形式层的模型身份指纹」，是强辅助证据，不能替代上游
      SSE response.model / openai-model / x-openai-model 这类硬证据。
      成品「画得像不像」是 noisy 的，不要拿它当判据。

只读保证: 不写任何文件，不打印用户 prompt 全文，只打印 assistant 侧文本的
          截断窗口（默认 200 字符）。
"""
import os
import re
import io
import sys
import glob
import json
import argparse
import datetime

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

try:
    import zstandard as zstd
except Exception:
    zstd = None

CODEX = os.path.join(os.path.expanduser('~'), '.codex')

# 强判据：描述动画「循环」的词（社区称基本 100%）
STRONG = re.compile(r'(循环骑行|连续循环|循环的?骑行|循环运动|循环动画|循环播放|'
                    r'连续骑行动画|为您生成了)', re.I)
# 弱判据：内嵌 / 内联 SVG（社区称约 90%，确实有人命中但没被路由）
WEAK = re.compile(r'(内嵌\s*SVG|内联\s*SVG|内嵌SVG|内联SVG)', re.I)
# 裸「循环」，供人工确认，不直接下结论
ANY_LOOP = re.compile(r'循环', re.I)
# 满血侧
UP = re.compile(r'(踩踏|沿途风景|背景移动|踏板|脚蹬)', re.I)
# 判断是否像鹈鹕测试回复
PELICANISH = re.compile(r'(鹈鹕|SVG|pelican|HTML|动画)', re.I)


def iter_lines(path):
    if path.endswith('.zst'):
        if zstd is None:
            print('  (skip %s: zstandard not installed)' % os.path.basename(path))
            return
        with open(path, 'rb') as fh:
            with zstd.ZstdDecompressor().stream_reader(fh) as rd:
                with io.TextIOWrapper(rd, encoding='utf-8', errors='replace') as txt:
                    yield from txt
    else:
        with open(path, encoding='utf-8', errors='replace') as fh:
            yield from fh


def texts_from_line(line):
    """只抽 assistant 侧文本，返回 [(phase, text)]"""
    try:
        d = json.loads(line)
    except Exception:
        return []
    p = d.get('payload') or {}
    out = []
    typ = d.get('type')
    if typ == 'response_item' and isinstance(p, dict) and p.get('type') == 'message':
        if p.get('role') == 'assistant':
            for c in (p.get('content') or []):
                if isinstance(c, dict) and c.get('type') in ('output_text', 'text'):
                    t = c.get('text')
                    if isinstance(t, str):
                        out.append((p.get('phase') or 'final', t))
    elif typ == 'event_msg' and isinstance(p, dict) and p.get('type') == 'item_completed':
        for c in (p.get('content') or []):
            if isinstance(c, dict) and c.get('type') == 'Text':
                t = c.get('text')
                if isinstance(t, str):
                    out.append((p.get('phase') or 'commentary', t))
    return out


def ts_of(line):
    try:
        d = json.loads(line)
        return d.get('timestamp') or (d.get('payload') or {}).get('timestamp') or ''
    except Exception:
        return ''


def verdict(text):
    strong = STRONG.findall(text)
    weak = WEAK.findall(text)
    up = UP.findall(text)
    loop_only = bool(ANY_LOOP.search(text)) and not strong
    if not PELICANISH.search(text):
        return '非鹈鹕测试回复，判据不适用', strong, weak, up, loop_only
    if strong:
        return '强疑似降智（社区称基本 100%）', strong, weak, up, loop_only
    if weak and up:
        return '混合信号：降智侧+满血侧同时命中，必须复测', strong, weak, up, loop_only
    if weak:
        return '疑似降智（非 100%，需复测）', strong, weak, up, loop_only
    if up:
        return '满血侧', strong, weak, up, loop_only
    return '无指纹命中（不表态）', strong, weak, up, loop_only


def scan_session(path, maxwin=200):
    first_ts = last_ts = ''
    opening = None
    seen = set()
    for ln, line in enumerate(iter_lines(path), 1):
        t = ts_of(line)
        if t:
            if not first_ts:
                first_ts = t
            last_ts = t
        for phase, txt in texts_from_line(line):
            key = txt[:160]
            if key in seen:
                continue
            seen.add(key)
            if opening is None:
                opening = (ln, t, phase, txt)
    if opening is None:
        return None
    ln, t, phase, txt = opening
    v, strong, weak, up, loop_only = verdict(txt)
    return dict(first_ts=first_ts, last_ts=last_ts, line=ln, ts=t,
                phase=phase, text=txt[:maxwin].replace('\n', ' '),
                verdict=v, strong=strong, weak=weak, up=up, loop_only=loop_only)


def scan_html(path):
    print('=' * 72)
    print('HTML 代码形态检查:', path)
    if not os.path.exists(path):
        print('  MISSING')
        return
    txt = open(path, encoding='utf-8', errors='replace').read()
    print('  bytes = %d | lines = %d' % (len(txt.encode('utf-8')), len(txt.splitlines())))
    for kw in ['csv', '内嵌', '内联', 'embedded', 'inline', '<table',
               'split(', 'path d=', '<script']:
        n = len(re.findall(re.escape(kw), txt, re.I))
        if n:
            print('  %-12s %d' % (kw, n))
    n_csv = len(re.findall(r'csv', txt, re.I))
    print('  CSV 字面命中 = %d  -> %s' %
          (n_csv, '有内嵌 CSV 数据形态' if n_csv else '未发现内嵌 CSV 数据形态'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', action='append', default=[],
                    help='YYYY-MM-DD，可重复；默认今天+昨天')
    ap.add_argument('--html', default=None, help='可选：额外检查一个生成的 HTML')
    ap.add_argument('--maxwin', type=int, default=200)
    a = ap.parse_args()

    if a.html:
        scan_html(a.html)
        print()

    dates = a.date or [(datetime.date.today() - datetime.timedelta(days=i)).isoformat()
                       for i in (0, 1)]
    print('#' * 72)
    print('# 首段措辞指纹扫描 | 日期:', ', '.join(dates))
    print('# 判据: 内嵌SVG/内联SVG=疑似(非100%) | 循环*=强疑似 | 踩踏/沿途风景/背景移动=满血侧')
    print('#' * 72)

    total = 0
    for ds in dates:
        y, m, d = ds.split('-')
        pat = os.path.join(CODEX, 'sessions', y, m, d, '*.jsonl*')
        files = sorted(glob.glob(pat))
        print('=' * 72)
        print('%s -> %d 个会话文件' % (ds, len(files)))
        for f in files:
            r = scan_session(f, a.maxwin)
            if r is None:
                print('  [无 assistant 文本] %s' % os.path.basename(f))
                continue
            total += 1
            print('-' * 72)
            print('  文件 : %s' % os.path.basename(f))
            print('  时间 : %s -> %s' % (r['first_ts'][:19], r['last_ts'][:19]))
            print('  首段 : L%d %s [%s]' % (r['line'], r['ts'][:19], r['phase']))
            print('         %s' % r['text'])
            print('  判定 : %s' % r['verdict'])
            if r['strong'] or r['weak'] or r['up'] or r['loop_only']:
                print('  命中 : 强=%s 弱=%s 满血=%s 裸循环=%s' %
                      (r['strong'], r['weak'], r['up'], r['loop_only']))
    print('=' * 72)
    print('共扫描 %d 个含 assistant 文本的会话。' % total)
    print('提醒: 指纹只是行为层辅助证据；坐实换模型仍需上游 SSE response.model /')
    print('      openai-model / x-openai-model（见 output/model_route_check.py）。')


if __name__ == '__main__':
    main()
