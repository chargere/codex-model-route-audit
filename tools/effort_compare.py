"""只读：按 reasoning effort 档位对比本机实测 reasoning_output_tokens。

只取数值与配置字段，不打印、不存储任何对话正文。
用于回答「medium 和 xhigh 到底差多少」。

Usage:
    python effort_compare.py [session_dir]
"""
import collections
import glob
import io
import json
import os
import re
import statistics
import sys

try:
    import zstandard as zstd
except ImportError:
    zstd = None

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.expanduser("~"), ".codex", "sessions")
STEP = 518
BOUNDARIES = {516 + STEP * n for n in range(0, 20)}


def iter_lines(path):
    if path.endswith(".zst"):
        if zstd is None:
            return
        with open(path, "rb") as fh:
            with zstd.ZstdDecompressor().stream_reader(fh) as rd:
                with io.TextIOWrapper(rd, encoding="utf-8", errors="replace") as txt:
                    yield from txt
    else:
        with open(path, encoding="utf-8", errors="replace") as fh:
            yield from fh


def main():
    plain = glob.glob(os.path.join(ROOT, "**", "*.jsonl"), recursive=True)
    packed = glob.glob(os.path.join(ROOT, "**", "*.jsonl.zst"), recursive=True)
    files = sorted(set(plain + packed))
    if not files:
        print("no session files under", ROOT)
        return

    rows = []  # (date, model, effort, reasoning_output_tokens)
    for path in files:
        name = os.path.basename(path)
        m = re.search(r"(\d{4}-\d{2}-\d{2})T", name)
        date = m.group(1) if m else "?"
        cur_model, cur_effort = "?", "?"
        for line in iter_lines(path):
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type")
            p = d.get("payload") or {}
            if not isinstance(p, dict):
                continue
            if t == "turn_context":
                if isinstance(p.get("model"), str):
                    cur_model = p["model"]
                for key in ("effort", "reasoning_effort", "model_reasoning_effort"):
                    if isinstance(p.get(key), str):
                        cur_effort = p[key]
                        break
            if t == "token_usage_record":
                u = p.get("usage") or {}
                if isinstance(u, dict):
                    r = u.get("reasoning_output_tokens")
                    if isinstance(r, int) and not isinstance(r, bool):
                        rows.append((date, cur_model, cur_effort, r))

    print("session files =", len(files), "| token records =", len(rows))
    print("zstd support  =", "yes" if zstd else "no")
    print()

    def summarize(label, vals):
        if not vals:
            print("  %-28s (no data)" % label)
            return
        s = sorted(vals)
        nz = [v for v in s if v > 0]
        ge = len([v for v in s if v >= 516])
        bd = len([v for v in s if v in BOUNDARIES])
        print("  %-28s n=%4d  mean=%7.1f  med=%6.0f  p90=%6d  max=%6d  >=516=%3d(%4.0f%%)  518边界=%3d"
              % (label, len(s), sum(s) / len(s), statistics.median(s),
                 s[min(len(s) - 1, int(len(s) * 0.9))], max(s),
                 ge, 100.0 * ge / len(s), bd))
        if nz:
            print("  %-28s   非零样本 n=%d  mean=%.1f  med=%.0f" %
                  ("", len(nz), sum(nz) / len(nz), statistics.median(nz)))

    print("=== 按 effort 汇总（全部模型）===")
    by_effort = collections.defaultdict(list)
    for _, _, e, r in rows:
        by_effort[e].append(r)
    for e in sorted(by_effort):
        summarize(e, by_effort[e])

    print()
    print("=== 按 model + effort ===")
    by_both = collections.defaultdict(list)
    for _, m, e, r in rows:
        by_both[(m, e)].append(r)
    for (m, e) in sorted(by_both):
        summarize("%s / %s" % (m, e), by_both[(m, e)])

    print()
    print("=== 按日期 + effort（近期）===")
    by_day = collections.defaultdict(list)
    for d, _, e, r in rows:
        by_day[(d, e)].append(r)
    for (d, e) in sorted(by_day)[-14:]:
        summarize("%s / %s" % (d, e), by_day[(d, e)])

    print()
    print("=== 每个 effort 的 token 值分布 Top 12 ===")
    for e in sorted(by_effort):
        c = collections.Counter(by_effort[e])
        print(" ", e, "->", c.most_common(12))


if __name__ == "__main__":
    main()
