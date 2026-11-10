#!/usr/bin/env python3
"""Read-only Codex model-route checker.

Scans ~/.codex/logs_2.sqlite (Rust tracing spans) and rollout sessions and prints:
  * what model each turn was SENT with (client side) + reasoning effort
  * which turns are internal shadow-selection turns (NOT your conversation)
  * any server-side x-codex-safety-buffering-* / faster-model headers ever logged
  * whether response.model / served_model exists anywhere in the DB

It prints only span metadata (model / effort / turn id / headers). It never prints
or stores conversation text. Everything is opened read-only.

IMPORTANT: these logs prove which model the CLIENT requested, NOT which model the
server actually executed. To prove the latter you must capture the upstream SSE
response.model / response.created.response.model, or the openai-model /
x-openai-model response headers, e.g. via a local relay that logs them.

NOTE: x-codex-safety-buffering-enabled / -faster-model headers are NOT evidence
of degradation. They are a fallback-chain eligibility declaration that also
appears on healthy responses ("The headers only announce eligibility"). Treat
them as background only.

Usage:
  python model_route_check.py
  python model_route_check.py --date 2026-11-07 --date 2026-11-08
"""
import argparse
import collections
import datetime
import glob
import io
import json
import os
import re
import sqlite3

try:
    import zstandard as zstd
except ImportError:
    zstd = None

BJ = datetime.timezone(datetime.timedelta(hours=8))
SPAN_MODEL = re.compile(r'model=(["\w.\-]+)')
TURNID = re.compile(r'(?:turn\.id|turn_id)=([\w\-]+)')
EFF = re.compile(r'codex\.turn\.reasoning_effort=([\w]+)')
MODELISH = re.compile(r'gpt-[\w.\-]+|codex-auto-review', re.I)
HDRKEY = re.compile(r'safety[_-]?buffer|faster[_-]?model|response\.model|served_model|'
                    r'x-openai-model|openai-model|engine_ids|timing_metrics|'
                    r'ModelReroute|safety_buffering', re.I)
XHDR = re.compile(r'x-codex-[\w\-]+')


def iter_lines(path):
    if path.endswith('.zst'):
        if zstd is None:
            return
        with open(path, 'rb') as fh:
            with zstd.ZstdDecompressor().stream_reader(fh) as rd:
                with io.TextIOWrapper(rd, encoding='utf-8', errors='replace') as t:
                    yield from t
    else:
        with open(path, encoding='utf-8', errors='replace') as fh:
            yield from fh


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codex-dir', default=os.path.join(os.path.expanduser('~'), '.codex'))
    ap.add_argument('--date', action='append', help='YYYY-MM-DD (Beijing), repeatable')
    ap.add_argument('--max-turns', type=int, default=80)
    args = ap.parse_args()

    db = os.path.join(args.codex_dir, 'logs_2.sqlite')
    if not os.path.isfile(db):
        print('logs_2.sqlite not found:', db)
        return
    c = sqlite3.connect('file:' + db.replace('\\', '/') + '?mode=ro', uri=True)
    mn, mx = c.execute('select min(ts), max(ts) from logs').fetchone()
    unit = 1000 if int(mx) > 1e12 else 1

    def to_bj(v):
        return datetime.datetime.fromtimestamp(int(v) / unit, datetime.timezone.utc).astimezone(BJ)

    print('DB:', db)
    print('log coverage (Beijing): %s  ~  %s' % (
        to_bj(mn).strftime('%Y-%m-%d %H:%M:%S'), to_bj(mx).strftime('%Y-%m-%d %H:%M:%S')))
    print('rows:', c.execute('select count(*) from logs').fetchone()[0])

    today = datetime.datetime.now(BJ).date()
    dates = args.date or [(today - datetime.timedelta(days=i)).isoformat() for i in (1, 0)]
    print('dates:', ', '.join(dates))

    for day in dates:
        d = datetime.date.fromisoformat(day)
        lo = int(datetime.datetime(d.year, d.month, d.day, tzinfo=BJ).timestamp()) * unit
        hi = lo + 86400 * unit
        rows = list(c.execute(
            'select ts, target, feedback_log_body from logs '
            'where cast(ts as integer) >= ? and cast(ts as integer) < ? '
            'order by cast(ts as integer)', (lo, hi)))
        print('\n===== %s : %d log rows =====' % (day, len(rows)))

        turns = {}
        per_model = collections.Counter()
        for ts, target, body in rows:
            body = body or ''
            ms = set(m.strip('"') for m in SPAN_MODEL.findall(body))
            if not ms:
                continue
            mt = TURNID.search(body)
            me = EFF.search(body)
            tg = str(target)
            tid = mt.group(1)[:13] if mt else '?'
            eff = me.group(1) if me else '-'
            for m in ms:
                per_model[m.lower()] += 1
                k = (tid, m.lower(), eff)
                r = turns.setdefault(k, {'first': ts, 'last': ts, 'n': 0,
                                         'shadow': False, 'targets': collections.Counter()})
                r['last'] = ts
                r['n'] += 1
                r['targets'][tg[:40]] += 1
                if 'shadow_selection' in tg or 'step_state' in tg:
                    r['shadow'] = True

        print('-- model mentions (row counts):',
              ', '.join('%s=%d' % kv for kv in per_model.most_common()))
        print('-- turn-level ledger (CLIENT-SIDE requested model):')
        items = sorted(turns.items(), key=lambda kv: kv[1]['first'])
        for (tid, m, eff), r in items[:args.max_turns]:
            flag = ' [INTERNAL shadow turn - NOT your conversation]' if (
                r['shadow'] and m == 'gpt-6-luna' and eff == 'low' and r['n'] <= 30) else ''
            print('   %s ~ %s  %-13s %-22s %-7s rows=%-4d %s%s' % (
                to_bj(r['first']).strftime('%H:%M:%S'), to_bj(r['last']).strftime('%H:%M:%S'),
                tid, m, eff, r['n'], r['targets'].most_common(1)[0][0], flag))
        if len(items) > args.max_turns:
            print('   ... %d more turn groups (raise --max-turns)' % (len(items) - args.max_turns))

        # rollout cross-check
        sd = os.path.join(args.codex_dir, 'sessions', d.strftime('%Y/%m/%d'))
        print('-- rollout turn_context cross-check:')
        found = False
        for f in sorted(glob.glob(os.path.join(sd, '*.jsonl*'))):
            tc = []
            for line in iter_lines(f):
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                if j.get('type') == 'turn_context':
                    tc.append(j.get('payload', {}).get('model'))
            if tc:
                found = True
                print('   %s -> %s' % (os.path.basename(f)[:52],
                                      ', '.join('%s x%d' % (m, tc.count(m))
                                                for m in dict.fromkeys(tc))))
        if not found:
            print('   (no rollout turn_context for this date)')

    print('\n===== server-side evidence scan over the WHOLE DB =====')
    hits = collections.defaultdict(collections.Counter)
    hdr_samples = {}
    for ts, body in c.execute('select ts, feedback_log_body from logs'):
        body = body or ''
        for k in set(x.lower() for x in HDRKEY.findall(body)):
            hits[to_bj(ts).strftime('%m-%d')][k] += 1
        if 'safety-buffering' in body and 'faster-model' in body:
            key = to_bj(ts).strftime('%Y-%m-%d %H:%M:%S')
            if key not in hdr_samples:
                vals = dict(re.findall(
                    r'"(x-codex-safety-buffering-[\w\-]+|x-codex-primary-used-percent|'
                    r'x-codex-plan-type)"\s*:\s*"([^"]{0,60})"', body))
                hdr_samples[key] = vals
    total = sum(sum(v.values()) for v in hits.values())
    if not hits:
        print('  no safety-buffering / faster-model / response.model / served_model at all')
    for day in sorted(hits):
        print('  %s  %s' % (day, '  '.join('%s=%d' % kv for kv in hits[day].most_common())))
    print('  total hits:', total)
    for t, vals in sorted(hdr_samples.items()):
        print('  @%s  %s' % (t, json.dumps(vals, ensure_ascii=False)))

    print('\n===== verdict =====')
    print('  These logs show ONLY the model the client requested.')
    real = sum(v.get('response.model', 0) + v.get('served_model', 0) +
               v.get('openai-model', 0) + v.get('x-openai-model', 0) +
               v.get('engine_ids', 0) + v.get('timing_metrics', 0)
               for v in hits.values())
    print('  real served-model signal hits (response.model / openai-model / engine_ids): %d' % real)
    print('  If that number is 0, you CANNOT prove which model the server executed.')
    print('  x-codex-safety-buffering-* = eligibility declaration, NOT proof of a swap.')
    print('  Capture upstream SSE response.model or openai-model headers (relay / MITM) to close the gap.')


if __name__ == '__main__':
    main()
