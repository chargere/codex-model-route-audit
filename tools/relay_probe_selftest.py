#!/usr/bin/env python3
"""Local self-test for the read-only served-model tap.

NO NETWORK. NO RELAY SERVER. NO CONFIG WRITES. NO ~/.codex TOUCHED.
All logs go to a temporary file under this script's own directory.

Covers:
  1. disabled probe is a complete no-op (no file created)
  2. requested-model extraction (identity / gzip / deflate, zstd if available)
  3. byte transparency of _stream_back with and without the probe
  4. served-model verdicts: MATCH / SWAP / SERVED / NO_SIGNAL
  5. CRLF SSE framing
  6. --observe-only fast path in attempt(): zero buffering, body untouched
  7. normal mode still inspects the SSE head (regression check)
  8. --observe-only CLI wiring forces max_attempts=1
  9. --read summary rendering
"""

import contextlib
import gzip
import importlib
import io
import json
import os
import sys
import tempfile
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
# Self-contained archive: prefer the vendored relay shipped alongside this
# repo, fall back to a sibling upstream checkout when developing locally.
CANDIDATES = [
    os.path.normpath(os.path.join(HERE, "..", "vendor", "relay")),
    os.path.normpath(os.path.join(HERE, "..", "..", "codex-overload-fix", "relay")),
]
RELAY_DIR = next((p for p in CANDIDATES if os.path.isfile(
    os.path.join(p, "relay.py"))), CANDIDATES[0])
sys.path.insert(0, RELAY_DIR)

model_probe = importlib.import_module("model_probe")
relay = importlib.import_module("relay")

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [OK]   %s" % name)
    else:
        FAIL.append((name, detail))
        print("  [FAIL] %s  %s" % (name, detail))


def sse(events, sep=b"\n\n"):
    out = bytearray()
    for event in events:
        out += b"data: " + json.dumps(
            event, ensure_ascii=False).encode("utf-8") + sep
    return bytes(out)


def created(model):
    return {"type": "response.created",
            "response": {"id": "resp_1", "model": model, "output": []}}


def delta(text="..."):
    return {"type": "response.output_text.delta", "delta": text}


def completed(model):
    return {"type": "response.completed",
            "response": {"id": "resp_1", "model": model,
                         "usage": {"total_tokens": 10}}}


# ---------------------------------------------------------------- fakes ----
class FakeSock:
    def settimeout(self, *a):
        pass

    def shutdown(self, *a):
        pass

    def close(self):
        pass


class FakeResp:
    def __init__(self, chunks, status=200, headers=None):
        self._chunks = list(chunks)
        self.status = status
        self._headers = headers or [
            ("Content-Type", "text/event-stream"),
            ("openai-model", "gpt-6-astra"),
        ]
        self.read_calls = 0
        self.closed = False

    def getheaders(self):
        return list(self._headers)

    def getheader(self, key, default=""):
        for k, v in self._headers:
            if k.lower() == key.lower():
                return v
        return default

    def read1(self, n):
        self.read_calls += 1
        return self._chunks.pop(0) if self._chunks else b""

    def read(self, n=None):
        self.read_calls += 1
        return self._chunks.pop(0) if self._chunks else b""

    def close(self):
        self.closed = True


class FakeConn:
    def __init__(self, resp):
        self.sock = FakeSock()
        self._resp = resp
        self.closed = False

    def connect(self):
        pass

    def request(self, *a, **k):
        pass

    def getresponse(self):
        return self._resp

    def close(self):
        self.closed = True


class NoopWatch:
    def __init__(self, *a, **k):
        self.reason = None
        self.committed = False
        self.sock = None

    def commit(self):
        self.committed = True

    def close(self):
        pass


class FakeWfile:
    def __init__(self):
        self.data = bytearray()

    def write(self, b):
        self.data.extend(b)
        return len(b)

    def flush(self):
        pass


class FakeHandler:
    def __init__(self):
        self.wfile = FakeWfile()
        self.close_connection = False
        self.sent = []

    def send_response(self, code, *a):
        self.sent.append(("status", code))

    def send_header(self, k, v):
        self.sent.append((k, v))

    def end_headers(self):
        pass


def records(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def verdict_of(path):
    if not os.path.exists(path):
        return {}
    return next((r for r in records(path) if r["kind"] == "VERDICT"), {})


def run_stream_case(tmp, *, requested, header_model, sse_model,
                    sep=b"\n\n", with_sse_model=True, with_probe=True):
    """Drive _stream_back with fakes and return (written_bytes, verdict)."""
    log_path = os.path.join(tmp, "case_%d.jsonl" % len(os.listdir(tmp)))
    events = [created(sse_model)] if with_sse_model else []
    events += [delta(), completed(sse_model) if with_sse_model else
               {"type": "response.completed", "response": {"id": "resp_1"}}]
    body = sse(events, sep=sep)
    headers = [("Content-Type", "text/event-stream"),
               ("x-codex-safety-buffering-enabled", "true"),
               ("x-codex-primary-used-percent", "0"),
               ("x-codex-turn-state", "x" * 780)]
    if header_model:
        headers.append(("openai-model", header_model))
    # split into two chunks to exercise incremental framing
    cut = max(1, len(body) // 2)
    resp = FakeResp([body[:cut], body[cut:]], headers=headers)
    conn = FakeConn(resp)
    conn._relay_watch = NoopWatch()
    probe = None
    if with_probe:
        probe = model_probe.Probe(
            log_path, "selftest",
            json.dumps({"model": requested}).encode(), "")
        probe.note_headers(dict(headers), "stream", 200)
    fh = FakeHandler()
    relay.Handler._stream_back(fh, dict(headers), b"", conn, resp, probe)
    return bytes(fh.wfile.data), body, verdict_of(log_path), log_path


def main():
    print("=" * 74)
    print("relay read-only served-model tap / local self-test (offline)")
    print("=" * 74)

    with tempfile.TemporaryDirectory(prefix="relay_probe_test_") as tmp:

        # 1. disabled probe is a no-op -------------------------------------
        p = model_probe.Probe("", "noop", b'{"model":"x"}', "")
        p.note_headers({"openai-model": "x"}, "stream", 200)
        p.feed(b"data: {}\n\n")
        p.finish()
        check("disabled probe writes nothing / does not raise",
              not os.listdir(tmp) and not p.enabled)

        # 2. requested-model extraction -----------------------------------
        payload = json.dumps({"model": "gpt-6-astra"}).encode()
        cases = [
            ("identity", payload, ""),
            ("gzip", gzip.compress(payload), "gzip"),
            ("deflate", zlib.compress(payload), "deflate"),
        ]
        try:
            import zstandard
            cases.append(("zstd", zstandard.ZstdCompressor().compress(payload),
                          "zstd"))
        except ImportError:
            print("  (zstandard not installed; zstd case skipped)")
        for label, blob, enc in cases:
            got = model_probe.extract_requested_model(blob, enc)
            check("requested model / %s" % label, got == "gpt-6-astra", got)
        check("undecodable body returns empty string",
              model_probe.extract_requested_model(b"\x00\x01", "identity") == "")

        # 3 + 4. byte transparency + verdicts ------------------------------
        written, original, v, log_path = run_stream_case(
            tmp, requested="gpt-6-astra", header_model="gpt-6-astra",
            sse_model="gpt-6-astra")
        check("byte transparency (probe on): written == original SSE",
              written == original, "%d vs %d bytes" % (len(written), len(original)))
        check("MATCH verdict", v.get("verdict") == "MATCH", str(v))
        check("served model = gpt-6-astra", v.get("served") == "gpt-6-astra")
        recs = records(log_path)
        check("HEADERS record carries eligibility caveat",
              any(r["kind"] == "HEADERS" and "ELIGIBILITY" in r.get("note", "")
                  for r in recs))
        check("turn-state kept as length only",
              any(r["kind"] == "HEADERS"
                  and r.get("context", {}).get("x-codex-turn-state") == "len=780"
                  for r in recs))

        written2, original2, _, _ = run_stream_case(
            tmp, requested="gpt-6-astra", header_model="gpt-6-astra",
            sse_model="gpt-6-astra", with_probe=False)
        check("byte transparency (probe off / default relay)",
              written2 == original2)

        _, _, v_swap, _ = run_stream_case(
            tmp, requested="gpt-6-astra", header_model="gpt-6-luna",
            sse_model="gpt-6-luna")
        check("SWAP verdict (astra -> luna)", v_swap.get("verdict") == "SWAP",
              str(v_swap))
        check("SWAP served = gpt-6-luna", v_swap.get("served") == "gpt-6-luna")

        _, _, v_served, _ = run_stream_case(
            tmp, requested="gpt-6-astra", header_model="gpt-6-astra",
            sse_model="gpt-6-astra", with_sse_model=False)
        check("header-only signal still yields a verdict",
              v_served.get("verdict") == "MATCH", str(v_served))

        no_sig = os.path.join(tmp, "no_signal.jsonl")
        p = model_probe.Probe(no_sig, "nosig", b'{"model":"gpt-6-astra"}', "")
        p.note_headers({"content-type": "text/event-stream"}, "stream", 200)
        p.feed(sse([delta()]))
        p.finish()
        check("NO_SIGNAL verdict", verdict_of(no_sig).get("verdict") == "NO_SIGNAL")

        # 5. CRLF framing --------------------------------------------------
        _, _, v_crlf, _ = run_stream_case(
            tmp, requested="gpt-6-astra", header_model="gpt-6-astra",
            sse_model="gpt-6-astra", sep=b"\r\n\r\n")
        check("CRLF SSE framing parsed", v_crlf.get("verdict") == "MATCH",
              str(v_crlf))

        # 6 + 7. attempt() fast path, with fakes (no sockets) --------------
        import argparse
        base_args = dict(
            observe_only=True, upstream_host="chatgpt.com", upstream_port=443,
            upstream_scheme="https", proxy=None, max_attempts=1,
            total_budget=300.0, commit_deadline=120.0, timeout=600.0,
            backoff_base=1.0, backoff_max=8.0, probe_log="", log="",
            identity_key=b"", relay_digest="x",
            upstream_prefix="/backend-api/codex",
        )
        saved = (relay.upstream_connection, relay.ConnectionWatch,
                 relay.WatchedSocket, relay.ARGS)
        try:
            relay.ConnectionWatch = NoopWatch
            relay.WatchedSocket = lambda sock, watch, timeout: sock

            head = sse([created("gpt-6-astra"), delta()])
            resp = FakeResp([head])
            conn = FakeConn(resp)
            relay.upstream_connection = lambda timeout: conn
            relay.ARGS = argparse.Namespace(**base_args)
            result = relay.attempt("/responses", {}, b"{}", 10.0,
                                   time.monotonic() + 10.0)
            check("observe-only returns stream mode", result[0] == "stream",
                  str(result[0]))
            check("observe-only buffers ZERO bytes", result[3] == b"",
                  "%d bytes" % len(result[3]))
            check("observe-only never reads the body inside attempt()",
                  resp.read_calls == 0, "read_calls=%d" % resp.read_calls)
            check("observe-only commits immediately", conn._relay_watch.committed)

            # normal mode still inspects / buffers (regression check)
            resp2 = FakeResp([head])
            conn2 = FakeConn(resp2)
            relay.upstream_connection = lambda timeout: conn2
            relay.ARGS = argparse.Namespace(
                **dict(base_args, observe_only=False, max_attempts=30))
            result2 = relay.attempt("/responses", {}, b"{}", 10.0,
                                    time.monotonic() + 10.0)
            check("normal mode still classifies SSE head",
                  result2[0] == "stream" and len(result2[3]) > 0,
                  "mode=%s prelude=%dB" % (result2[0], len(result2[3])))
            check("normal mode actually read the body", resp2.read_calls >= 1)
        finally:
            (relay.upstream_connection, relay.ConnectionWatch,
             relay.WatchedSocket, relay.ARGS) = saved

        # 8. CLI wiring ----------------------------------------------------
        class FakeServer:
            daemon_threads = False

            def __init__(self, *a, **k):
                pass

            def serve_forever(self):
                pass

        saved_server, saved_argv = relay.LocalRelayServer, sys.argv
        try:
            relay.LocalRelayServer = FakeServer
            probe_log = os.path.join(tmp, "cli_probe.jsonl")
            sys.argv = ["relay.py", "--observe-only", "--probe-log", probe_log,
                        "--port", "0"]
            relay.main()
            check("--observe-only forces max_attempts=1",
                  relay.ARGS.max_attempts == 1,
                  str(relay.ARGS.max_attempts))
            check("--observe-only flag parsed", relay.ARGS.observe_only is True)
            check("--probe-log parsed", relay.ARGS.probe_log == probe_log)
        finally:
            relay.LocalRelayServer, sys.argv = saved_server, saved_argv

        # 9. --read summary ------------------------------------------------
        summary_log = os.path.join(tmp, "summary.jsonl")
        p = model_probe.Probe(summary_log, "r1", b'{"model":"gpt-6-astra"}', "")
        p.note_headers({"openai-model": "gpt-6-luna"}, "stream", 200)
        p.feed(sse([created("gpt-6-luna"), delta(), completed("gpt-6-luna")]))
        p.finish()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            model_probe.read_log(summary_log)
        text = buf.getvalue()
        check("--read renders SWAP", "SWAP" in text)
        check("--read renders served model", "gpt-6-luna" in text)

        # syntax check both files
        import py_compile
        for name in ("relay.py", "model_probe.py"):
            path = os.path.join(RELAY_DIR, name)
            try:
                py_compile.compile(path, doraise=True)
                check("py_compile %s" % name, True)
            except Exception as exc:  # noqa: BLE001
                check("py_compile %s" % name, False, repr(exc))

    print("=" * 74)
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for name, detail in FAIL:
            print("  FAILED: %s -> %s" % (name, detail))
        return 1
    print("全部通过；未联网、未启服务、未写 ~/.codex、未改 config.toml。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
