#!/usr/bin/env python3
"""Read-only served-model probe for the local Codex /responses relay.

This module NEVER modifies request or response bytes, headers, model ids, or
request timing. It only observes a copy of bytes that have ALREADY been
forwarded downstream, and appends structured JSON lines to a local log file.
It opens no sockets of its own and performs no network I/O.

Enabled from relay.py with:

    --probe-log <path>            enable read-only recording
    --observe-only                no retry / no SSE buffering / no inspection

When --probe-log is empty the Probe is a complete no-op, so the vendored
relay behaves byte-for-byte exactly as upstream.

Log format: one JSON object per line, with a "kind" field:
    HEADERS    upstream response headers relevant to model routing
    SSE_MODEL  a model id found in an SSE event (created / completed / ...)
    TRUNCATED  probe stopped parsing a huge stream; forwarding unaffected
    VERDICT    one-line conclusion for the request

Read a log back with:

    python model_probe.py --read <path>
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import threading
import time
import zlib

# Headers that carry a REAL served-model signal.
MODEL_HEADERS = ("openai-model", "x-openai-model")

# Context only. These are NOT swap evidence.
CONTEXT_HEADERS = (
    "x-codex-primary-used-percent",
    "x-codex-turn-state",
    "x-codex-safety-buffering-enabled",
    "x-codex-safety-buffering-faster-model",
    "x-oai-request-id",
    "openai-request-id",
    "x-request-id",
    "timing-metrics",
)

CONTEXT_NOTE = (
    "x-codex-safety-buffering-* are fallback-chain ELIGIBILITY declarations, "
    "NOT evidence that a swap happened; healthy astra responses carry them too. "
    "x-codex-primary-used-percent is a quota gauge, not a model id."
)

MODEL_EVENTS = (
    "response.created",
    "response.in_progress",
    "response.queued",
    "response.completed",
    "response.failed",
)


def extract_requested_model(body, content_encoding=""):
    """Best-effort read of the client's requested model id. Never raises.

    Handles identity / gzip / deflate / zstd request bodies. Returns "" when
    the body cannot be decoded -- a missing requested model only weakens the
    verdict, it never blocks or mutates forwarding.
    """
    if not body or len(body) > 64 << 20:
        return ""
    enc = (content_encoding or "").lower()
    try:
        if enc in ("", "identity"):
            raw = body
        elif enc == "gzip":
            raw = gzip.decompress(body)
        elif enc == "deflate":
            raw = zlib.decompress(body)
        elif enc == "zstd":
            try:
                from compression import zstd
                raw = zstd.decompress(body)
            except Exception:
                import zstandard
                raw = zstandard.ZstdDecompressor().decompress(
                    body, max_output_size=64 << 20)
        else:
            return ""
        doc = json.loads(raw)
    except Exception:  # noqa: BLE001 - observation must never break the relay
        return ""
    if not isinstance(doc, dict):
        return ""
    return str(doc.get("model") or "")


class _SSEFrames:
    """Chunk-oriented SSE data-frame extractor (LF and CRLF). Read-only."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, chunk):
        self.buf.extend(chunk)
        frames = []
        while True:
            idx = self.buf.find(b"\n\n")
            step = 2
            if idx < 0:
                idx = self.buf.find(b"\r\n\r\n")
                step = 4
            if idx < 0:
                break
            frame = bytes(self.buf[:idx])
            del self.buf[:idx + step]
            data = []
            for line in frame.replace(b"\r\n", b"\n").split(b"\n"):
                if line.startswith(b"data:"):
                    value = line[5:]
                    data.append(value[1:] if value.startswith(b" ") else value)
            if data:
                frames.append(b"\n".join(data))
        # Bound memory if a stream never terminates a frame.
        if len(self.buf) > 1 << 20:
            del self.buf[:len(self.buf) - 65536]
        return frames


class Probe:
    """One instance per /responses request.

    Every public method is side-effect-free with respect to the relayed bytes:
    feed() is called only AFTER the same bytes have been written downstream.
    """

    MAX_PROBE_BYTES = 8 << 20

    def __init__(self, log_path, req, body=b"", content_encoding=""):
        self.enabled = bool(log_path)
        self.req = req
        self.log_path = os.path.expanduser(log_path) if log_path else ""
        if self.enabled:
            parent = os.path.dirname(os.path.abspath(self.log_path))
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError:
                pass
        self.requested_model = (
            extract_requested_model(body, content_encoding) if self.enabled else ""
        )
        self._lock = threading.Lock()
        self._frames = _SSEFrames()
        self.header_model = ""
        self.header_context = {}
        self.created_model = ""
        self.completed_model = ""
        self.stream_bytes = 0
        self.truncated = False
        self.finished = False

    def _emit(self, kind, **fields):
        if not self.enabled:
            return
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": kind,
            "req": self.req,
        }
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False, sort_keys=True)
        try:
            with self._lock:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError:
            pass

    def note_headers(self, headers, mode="", status=None):
        """Called once per attempt, with the upstream response headers."""
        if not self.enabled:
            return
        low = {}
        for key, value in (headers or {}).items():
            low.setdefault(str(key).lower(), str(value))
        model = ""
        for key in MODEL_HEADERS:
            if low.get(key):
                model = low[key]
                break
        context = {}
        for key in CONTEXT_HEADERS:
            if key in low:
                value = low[key]
                # turn-state is a long opaque token: keep only its length.
                context[key] = (
                    "len=%d" % len(value)
                    if key == "x-codex-turn-state" else value[:256]
                )
        self.header_model = model
        self.header_context = context
        self._emit(
            "HEADERS", mode=mode, status=status,
            header_model=model or None, context=context, note=CONTEXT_NOTE,
        )

    def feed(self, chunk):
        """Observe a copy of bytes AFTER they were written downstream."""
        if not self.enabled or not chunk:
            return
        self.stream_bytes += len(chunk)
        if self.stream_bytes > self.MAX_PROBE_BYTES:
            if not self.truncated:
                self.truncated = True
                self._emit(
                    "TRUNCATED", bytes=self.stream_bytes,
                    note="probe parsing stopped; forwarding is unaffected",
                )
            return
        for payload in self._frames.feed(chunk):
            self._inspect(payload)

    def _inspect(self, payload):
        try:
            evt = json.loads(payload)
        except (ValueError, TypeError):
            return
        if not isinstance(evt, dict):
            return
        kind = evt.get("type")
        resp = evt.get("response")
        resp = resp if isinstance(resp, dict) else {}
        model = resp.get("model") or evt.get("model")
        if not model:
            return
        model = str(model)
        tm = resp.get("timing_metrics") or evt.get("timing_metrics") or {}
        engines = tm.get("engine_ids") if isinstance(tm, dict) else None
        if kind in MODEL_EVENTS and kind != "response.completed":
            if not self.created_model:
                self.created_model = model
                self._emit(
                    "SSE_MODEL", phase=kind, model=model,
                    engine_ids=engines or None,
                )
        elif kind == "response.completed":
            self.completed_model = model
            self._emit(
                "SSE_MODEL", phase="completed", model=model,
                engine_ids=engines or None,
            )

    def finish(self):
        """Idempotent. Always emits a verdict, even if the stream errored."""
        if not self.enabled or self.finished:
            return
        self.finished = True
        served = (
            self.header_model or self.completed_model
            or self.created_model or ""
        )
        if not served:
            verdict = "NO_SIGNAL"
            note = (
                "上游既没给 model 响应头，SSE 里也没有 response.model；"
                "本次无法判定真实模型"
            )
        elif self.requested_model and served != self.requested_model:
            verdict = "SWAP"
            note = (
                "请求 %s，服务端实际 %s —— 第二型静默换模型证据"
                % (self.requested_model, served)
            )
        elif self.requested_model:
            verdict = "MATCH"
            note = "服务端实际 %s，与请求一致" % served
        else:
            verdict = "SERVED"
            note = (
                "服务端实际 %s（请求侧 model 未解析，请对照 config.toml）"
                % served
            )
        mismatch = bool(
            self.created_model and self.completed_model
            and self.created_model != self.completed_model
        )
        self._emit(
            "VERDICT", verdict=verdict,
            requested=self.requested_model or None,
            served=served or None,
            header_model=self.header_model or None,
            sse_created=self.created_model or None,
            sse_completed=self.completed_model or None,
            mid_stream_mismatch=mismatch,
            stream_bytes=self.stream_bytes,
            note=note,
        )


def read_log(path):
    """Print a compact per-request summary of a probe JSONL log."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    by_req = {}
    for row in rows:
        by_req.setdefault(row.get("req", "?"), []).append(row)
    width = 118
    fmt = "%-19s %-16s %-18s %-18s %-11s %s"
    print("probe log: %s" % path)
    print("(%d records / %d requests)" % (len(rows), len(by_req)))
    print("-" * width)
    print(fmt % ("时间", "req", "请求模型", "实际 served", "判定", "备注"))
    print("-" * width)
    for req, items in by_req.items():
        verdict = next(
            (x for x in items if x.get("kind") == "VERDICT"), {}
        )
        print(fmt % (
            items[0].get("ts", ""),
            str(req)[:16],
            (verdict.get("requested") or "-")[:18],
            (verdict.get("served") or "-")[:18],
            verdict.get("verdict", "?"),
            (verdict.get("note") or "")[:52],
        ))
    print("-" * width)
    print("提示：" + CONTEXT_NOTE)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--read", default="", help="汇总一份 probe JSONL 日志")
    args = p.parse_args()
    if args.read:
        read_log(os.path.expanduser(args.read))
        return 0
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
