"""只读审计：确认 2026-11-08 11:00 起冷号期间是否产生过 Codex turn 请求。

只做三件事，全是只读：
 1. 以 mode=ro 打开 ~/.codex/logs_2.sqlite，统计 cutoff 之后的日志行 / 含 model= 的 turn 行
 2. 递归列出 ~/.codex/sessions 下最近修改的会话文件
 3. 打印事实，结论由人下

不写文件、不发网络请求、不接触模型推理、不读取任何对话正文。
"""
import os
import sqlite3
import datetime

TZ = datetime.timezone(datetime.timedelta(hours=8))
CUT = datetime.datetime(2026, 11, 8, 11, 0, 0, tzinfo=TZ)
CUT_EPOCH = CUT.timestamp()


def fmt(ts):
    try:
        return datetime.datetime.fromtimestamp(float(ts), TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


print("cutoff =", CUT.strftime("%Y-%m-%d %H:%M:%S %z"), "| epoch =", CUT_EPOCH)
print()

db = os.path.expanduser("~/.codex/logs_2.sqlite")
print("=== logs_2.sqlite ===")
print("size =", os.path.getsize(db), "bytes | mtime =", fmt(os.path.getmtime(db)))
uri = "file:" + db.replace(os.sep, "/") + "?mode=ro"
con = sqlite3.connect(uri, uri=True)
tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print("tables =", tables)

for t in tables:
    cols = [r[1] for r in con.execute('PRAGMA table_info("%s")' % t)]
    tsc = next((c for c in cols if c.lower() in ("ts", "timestamp", "time", "timestamp_ns")), None)
    if not tsc:
        print("  --", t, "| no ts col | cols =", cols[:10])
        continue
    mx = con.execute('SELECT MAX("%s") FROM "%s"' % (tsc, t)).fetchone()[0]
    if mx is None:
        print("  --", t, "| empty | cols =", cols[:10])
        continue
    raw = float(mx)
    if raw > 1e17:
        div, unit = 1e9, "ns"
    elif raw > 1e14:
        div, unit = 1e6, "us"
    elif raw > 1e11:
        div, unit = 1e3, "ms"
    else:
        div, unit = 1, "s"
    cut = CUT_EPOCH * div
    n = con.execute('SELECT COUNT(*) FROM "%s" WHERE "%s" >= ?' % (t, tsc), (cut,)).fetchone()[0]
    print("  --", t, "| ts unit =", unit, "| max_ts =", mx, "(" + fmt(raw / div) + ")",
          "| rows_after_cutoff =", n)
    print("       cols =", cols[:12])
    if n:
        for c in cols:
            if c.lower() in ("body", "message", "text", "log", "feedback_log_body", "fields", "payload", "name", "span"):
                try:
                    nm = con.execute(
                        'SELECT COUNT(*) FROM "%s" WHERE "%s" >= ? AND "%s" LIKE ?' % (t, tsc, c),
                        (cut, "%model=%"),
                    ).fetchone()[0]
                    if nm:
                        print("       turn-ish rows (%s LIKE '%%model=%%') = %d" % (c, nm))
                except Exception:
                    pass

print()
print("=== cutoff 之后 logs.target 分布（Top 30）===")
try:
    for name, c in con.execute(
        'SELECT target, COUNT(*) FROM "logs" WHERE ts >= ? GROUP BY target ORDER BY COUNT(*) DESC LIMIT 30',
        (CUT_EPOCH,),
    ).fetchall():
        print("   %6d  %s" % (c, name))
except Exception as e:
    print("   err:", e)

print()
print("=== cutoff 之后关键词命中计数（不打印正文）===")
for kw in ("codex.turn", "shadow_selection", "response.model", "gpt-6-", "reasoning_effort",
           "ModelReroute", "at capacity", "overload", "models_cache", "engine_ids", "codex-auto-review"):
    try:
        n = con.execute(
            'SELECT COUNT(*) FROM "logs" WHERE ts >= ? AND feedback_log_body LIKE ?',
            (CUT_EPOCH, "%" + kw + "%"),
        ).fetchone()[0]
        print("   %-24s %d" % (kw, n))
    except Exception as e:
        print("   %-24s err %s" % (kw, e))

print()
print("=== cutoff 之后按小时分布 ===")
for h, c in con.execute(
    'SELECT CAST(ts/3600 AS INTEGER)*3600, COUNT(*) FROM "logs" WHERE ts >= ? GROUP BY 1 ORDER BY 1',
    (CUT_EPOCH,),
).fetchall():
    print("   %s  %d" % (fmt(h), c))

print()
print("=== sessions ===")
root = os.path.expanduser("~/.codex/sessions")
hits = []
for dp, dn, fn in os.walk(root):
    for f in fn:
        fp = os.path.join(dp, f)
        try:
            hits.append((os.path.getmtime(fp), fp))
        except OSError:
            pass
hits.sort(reverse=True)
after = [h for h in hits if h[0] >= CUT_EPOCH]
print("total files =", len(hits), "| files modified after cutoff =", len(after))
for m, fp in hits[:15]:
    print("   ", fmt(m), os.path.relpath(fp, root))
