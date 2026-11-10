"""Scan local Codex session logs for 516/1034/1552 reasoning-token truncation.

Read-only. Only numeric token counts and config fields are collected; no message
text is printed or stored.

Usage:
    python codex_516_check.py [session_dir]

Compressed *.jsonl.zst sessions need the `zstandard` package; without it those
files are skipped. Install into an isolated env if needed:
    pip install zstandard
"""
import collections
import glob
import io
import json
import os
import re
import sys

try:
    import zstandard as zstd
except ImportError:
    zstd = None

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.expanduser("~"), ".codex", "sessions")
STEP = 518
BOUNDARIES = {516 + STEP * n for n in range(0, 20)}


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


def main():
    plain = glob.glob(os.path.join(ROOT, '**', '*.jsonl'), recursive=True)
    packed = glob.glob(os.path.join(ROOT, '**', '*.jsonl.zst'), recursive=True)
    files = sorted(set(plain + packed))
    if not files:
        print('no session files found under', ROOT)
        return
    print('zstd support           :', 'yes' if zstd else 'no (compressed sessions skipped)')

    records = []            # (date, model, effort, reasoning_output_tokens)
    models = collections.Counter()
    efforts = collections.Counter()

    for path in files:
        name = os.path.basename(path)
        m = re.search(r'(\d{4}-\d{2}-\d{2})T', name)
        date = m.group(1) if m else '?'
        cur_model, cur_effort = '?', '?'
        for line in iter_lines(path):
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get('type')
            p = d.get('payload') or {}
            if not isinstance(p, dict):
                continue

            if t == 'turn_context':
                if isinstance(p.get('model'), str):
                    cur_model = p['model']
                    models[cur_model] += 1
                for key in ('effort', 'reasoning_effort', 'model_reasoning_effort'):
                    if isinstance(p.get(key), str):
                        cur_effort = p[key]
                        efforts[cur_effort] += 1
                        break

            if t == 'token_usage_record':
                u = p.get('usage') or {}
                if isinstance(u, dict):
                    r = u.get('reasoning_output_tokens')
                    if isinstance(r, int) and not isinstance(r, bool):
                        records.append((date, cur_model, cur_effort, r))

    print('session files scanned :', len(files))
    print('models (client-set)   :', dict(models.most_common(10)))
    print('efforts               :', dict(efforts.most_common(8)))
    print()

    vals = [r for _, _, _, r in records]
    nonzero = [v for v in vals if v > 0]
    ge516 = [v for v in vals if v >= 516]
    exact = collections.Counter(v for v in vals if v in BOUNDARIES)

    print('=== reasoning_output_tokens (per response) ===')
    print('records               :', len(vals))
    print('non-zero              :', len(nonzero))
    print('>=516                 :', len(ge516))
    if vals:
        print('mean                  : %.1f' % (sum(vals) / len(vals)))
    if nonzero:
        s = sorted(nonzero)
        print('p90 (non-zero)        :', s[int(len(s) * 0.9) - 1])
        print('max                   :', max(s))
    print()

    print('=== 518-token boundary hits (516 + n*518) ===')
    if not exact:
        print('none')
    else:
        for v, c in sorted(exact.items()):
            n = (v - 516) // STEP + 1
            print('  %5d  (block %2d)  %4d hits' % (v, n, c))
        total = sum(exact.values())
        print('  TOTAL boundary hits :', total)
        if ge516:
            print('  boundary / >=516   : %.1f%%' % (100.0 * total / len(ge516)))
    print()

    print('=== by model (client-set) ===')
    bymodel = collections.defaultdict(lambda: [0, 0, 0])
    for _, model, _, r in records:
        bymodel[model][0] += 1
        if r >= 516:
            bymodel[model][1] += 1
        if r in BOUNDARIES:
            bymodel[model][2] += 1
    for model, (tot, ge, b) in sorted(bymodel.items(), key=lambda x: -x[1][0]):
        print('  %-20s responses=%4d  >=516=%4d  boundary=%3d  hit-rate=%5.1f%%'
              % (model, tot, ge, b, 100.0 * b / tot if tot else 0))
    print()

    print('=== by date ===')
    bydate = collections.defaultdict(lambda: [0, 0, 0])
    for date, _, _, r in records:
        bydate[date][0] += 1
        if r >= 516:
            bydate[date][1] += 1
        if r in BOUNDARIES:
            bydate[date][2] += 1
    for date in sorted(bydate):
        tot, ge, b = bydate[date]
        print('  %s  responses=%4d  >=516=%4d  boundary=%3d  hit-rate=%5.1f%%'
              % (date, tot, ge, b, 100.0 * b / tot if tot else 0))


if __name__ == '__main__':
    main()
