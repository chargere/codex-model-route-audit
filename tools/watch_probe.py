"""只读：把 relay 运行日志与 probe JSONL 合并成一张 served-model 判读表。

不联网、不写配置、不打印对话正文；只读两个本地日志文件。
用于复测时逐发核对「界面选的档位」和「服务端实际模型」。

Usage:
    python watch_probe.py
    python watch_probe.py --since "2026-11-10 12:55"
"""
import argparse
import collections
import json
import os
import re

RELAY = os.path.join(os.path.expanduser("~"), ".codex", "relay-1110.log")
PROBE = os.path.join(os.path.expanduser("~"), ".codex", "model_probe.log")

POST_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\d+)\] POST /responses .*?"
    r"\|\s*model=([^\s]+)\s+effort=([^\s]+)"
)


def load_relay(path):
    out, raw = {}, []
    if not os.path.exists(path):
        return out, raw
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.rstrip()
        if "POST /responses" in line:
            raw.append(line)
        m = POST_RE.match(line)
        if m:
            ts, port, model, effort = m.groups()
            out[port] = {"ts": ts, "port": port, "model": model, "effort": effort}
    return out, raw


def load_probe(path):
    reqs = collections.defaultdict(lambda: {"headers": {}, "sse": [], "verdict": None})
    if not os.path.exists(path):
        return reqs
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        req = d.get("req", "?")
        m = re.match(r"p(\d+)_", req)
        port = m.group(1) if m else req
        k = d.get("kind")
        if k == "HEADERS":
            reqs[port]["headers"] = d.get("context") or {}
        elif k == "SSE_MODEL":
            reqs[port]["sse"].append((d.get("phase"), d.get("model")))
        elif k == "VERDICT":
            reqs[port]["verdict"] = d
    return reqs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="", help='只显示该时刻之后的请求，如 "2026-11-10 12:55"')
    ap.add_argument("--relay", default=RELAY)
    ap.add_argument("--probe", default=PROBE)
    a = ap.parse_args()

    relay, raw = load_relay(a.relay)
    probe = load_probe(a.probe)
    ports = sorted(set(relay) | set(probe),
                   key=lambda p: (relay.get(p, {}).get("ts", ""), p))

    print("relay POST /responses 行数 =", len(raw), "| probe 请求数 =", len(probe))
    print()
    hdr = "%-19s %-6s %-16s %-8s %-16s %-7s %-8s %s" % (
        "时间", "port", "请求模型", "effort", "实际served", "判定", "字节", "request-id")
    print(hdr)
    print("-" * 110)

    n, bad = 0, []
    for p in ports:
        r = relay.get(p, {})
        q = probe.get(p, {})
        v = q.get("verdict") or {}
        ts = v.get("ts") or r.get("ts") or "?"
        if a.since and ts < a.since:
            continue
        n += 1
        served = v.get("served") or "-"
        verdict = v.get("verdict") or "NO_DATA"
        rid = (q.get("headers") or {}).get("x-oai-request-id", "-")
        print("%-19s %-6s %-16s %-8s %-16s %-7s %-8s %s" % (
            ts, p, r.get("model", "?"), r.get("effort", "?"), served, verdict,
            v.get("stream_bytes", "-"), rid[:12] if rid != "-" else "-"))
        if verdict not in ("MATCH", "SERVED", "NO_DATA", "-") or v.get("mid_stream_mismatch"):
            bad.append((p, verdict, served, v.get("mid_stream_mismatch")))

    print()
    print("显示请求数 =", n)
    eff = collections.Counter()
    for p in ports:
        r = relay.get(p, {})
        v = probe.get(p, {}).get("verdict") or {}
        ts = v.get("ts") or r.get("ts") or "?"
        if a.since and ts < a.since:
            continue
        eff[(r.get("effort", "?"), v.get("verdict", "NO_DATA"))] += 1
    print("按 (effort, 判定) 汇总 =", dict(eff))

    if bad:
        print()
        print("!! 重点看：")
        for p, vd, sv, mm in bad:
            print("   port", p, "判定", vd, "served", sv, "mid_stream_mismatch", mm)

    print()
    print("提示：x-codex-safety-buffering-* 只是 fallback 链资格声明，不是换模型证据。")


if __name__ == "__main__":
    main()
