"""Server-rendered status dashboard (read-only, ids only, no personal data)."""
import html
import json
import time
from collections import defaultdict

from app import db
from app.config import settings

CSS = """
body{font:14px/1.4 system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1c1e21}
header{background:#0f2f4a;color:#fff;padding:14px 22px;display:flex;justify-content:space-between;align-items:center}
header h1{font-size:18px;margin:0}main{padding:18px 22px;max-width:1200px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.tile{background:#fff;border-radius:8px;padding:12px 14px;box-shadow:0 1px 2px rgba(0,0,0,.08)}
.tile b{display:block;font-size:22px;margin-top:2px}.tile small{color:#666}
.ok{color:#1a7f37}.bad{color:#b42318}.warn{color:#b54708}
table{border-collapse:collapse;width:100%;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 2px rgba(0,0,0,.08);margin-bottom:20px}
th,td{padding:6px 10px;text-align:left;border-bottom:1px solid #eee;font-size:13px;vertical-align:top}th{background:#f0f2f5}
h2{font-size:15px;margin:18px 0 8px}.bar{display:inline-block;height:10px;background:#3b82f6;vertical-align:middle;margin-right:6px}
code{font-size:12px;background:#f0f2f5;padding:1px 4px;border-radius:3px}.muted{color:#888}
"""


def _ago(ts):
    if not ts:
        return "never"
    d = int(time.time() - ts)
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h {d % 3600 // 60}m ago"
    return f"{d // 86400}d ago"


def _t(ts):
    return time.strftime("%d %b %H:%M:%S", time.localtime(ts)) if ts else ""


def render(worker_alive: bool, worker_tick: float) -> str:
    d = db.dashboard()
    c = d["counts"]
    e = html.escape
    dry = settings.effective_dry_run()
    tiles = [
        ("Service", '<span class="ok">worker running</span>' if worker_alive else '<span class="bad">WORKER DOWN</span>',
         f"tick {_ago(worker_tick)}"),
        ("Writes", '<span class="warn">DRY RUN</span>' if dry else '<span class="ok">live</span>', "HubSpot"),
        ("Last webhook", e(_ago(c.get("last_webhook_at"))), "from TigerBay"),
        ("Last sync", e(_ago(c.get("last_done_at"))), "event completed"),
        ("Pending", str(c.get("pending", 0)), f"oldest {c.get('oldest_pending_age_s') or 0}s"),
        ("Failed", f'<span class="{"bad" if c.get("failed") else "ok"}">{c.get("failed", 0)}</span>', "need attention"),
        ("Unparsed", f'<span class="{"bad" if c.get("unparsed") else "ok"}">{c.get("unparsed", 0)}</span>', "webhook shape"),
        ("Mapped", str(c.get("mapped", 0)), "TigerBay→HubSpot ids"),
    ]
    out = [f"<title>beachsync status</title><style>{CSS}</style>",
           '<header><h1>beachsync · TigerBay → HubSpot</h1>'
           f'<span>{e(time.strftime("%a %d %b %Y %H:%M"))} · auto-refresh 60s</span></header><main>',
           '<div class="tiles">']
    for label, val, sub in tiles:
        out.append(f'<div class="tile"><small>{label}</small><b>{val}</b><small>{e(sub)}</small></div>')
    out.append("</div>")

    # actions last 7 days
    a = d["actions_7d"]
    if a:
        out.append("<h2>Outcomes, last 7 days</h2><table><tr>" + "".join(f"<th>{e(k)}</th>" for k in sorted(a)) + "</tr><tr>"
                   + "".join(f"<td>{a[k]}</td>" for k in sorted(a)) + "</tr></table>")

    # per-day
    per_day: dict = defaultdict(lambda: defaultdict(int))
    for r in d["days"]:
        per_day[r["d"]][f'{r["source"]}/{r["status"]}'] += r["n"]
        per_day[r["d"]]["_total"] += r["n"]
    if per_day:
        mx = max(v["_total"] for v in per_day.values()) or 1
        out.append("<h2>Events per day, last 14 days</h2><table><tr><th>Day</th><th>Total</th><th>Breakdown (source/status)</th></tr>")
        for day in sorted(per_day, reverse=True):
            v = per_day[day]
            br = ", ".join(f"{k} {n}" for k, n in sorted(v.items()) if k != "_total")
            out.append(f'<tr><td>{e(day)}</td><td><span class="bar" style="width:{int(200 * v["_total"] / mx)}px"></span>{v["_total"]}</td><td class="muted">{e(br)}</td></tr>')
        out.append("</table>")

    # problems
    out.append("<h2>Needs attention (failed / unparsed)</h2>")
    if d["problems"]:
        out.append("<table><tr><th>#</th><th>When</th><th>Source</th><th>Entity</th><th>Event</th><th>Id</th><th>Status</th><th>Attempts</th><th>Error / body</th></tr>")
        for p in d["problems"]:
            out.append(f'<tr><td>{p["id"]}</td><td>{_t(p["received_at"])}</td><td>{e(p["source"])}</td><td>{e(p["entity"])}</td>'
                       f'<td>{e(p["event"])}</td><td>{p["entity_id"] or ""}</td><td class="bad">{e(p["status"])}</td><td>{p["attempts"]}</td>'
                       f'<td><code>{e(p["last_error"] or p["raw_body"])}</code></td></tr>')
        out.append("</table>")
    else:
        out.append('<p class="ok">none</p>')

    # recent
    out.append("<h2>Recent events</h2><table><tr><th>#</th><th>Received</th><th>Source</th><th>Entity</th><th>Event</th><th>Id</th><th>Status</th><th>Result</th></tr>")
    for r in d["recent"]:
        res = ""
        if r["result"]:
            try:
                j = json.loads(r["result"]) if r["result"].endswith("}") else None
            except ValueError:
                j = None
            if j:
                res = j.get("action", "")
                if j.get("changed"):
                    res += ": " + ", ".join(j["changed"][:8]) + ("…" if len(j["changed"]) > 8 else "")
                if j.get("staff_queued") is not None:
                    res += f" (queued {j['staff_queued']} staff)"
                if j.get("note"):
                    res += f" — {j['note']}"
            else:
                res = r["result"][:120]
        elif r["last_error"]:
            res = r["last_error"]
        cls = {"done": "ok", "failed": "bad", "unparsed": "bad", "pending": "warn", "processing": "warn"}.get(r["status"], "muted")
        out.append(f'<tr><td>{r["id"]}</td><td>{_t(r["received_at"])}</td><td>{e(r["source"])}</td><td>{e(r["entity"])}</td>'
                   f'<td>{e(r["event"])}</td><td>{r["entity_id"] or ""}</td><td class="{cls}">{e(r["status"])}</td><td class="muted">{e(res)}</td></tr>')
    out.append("</table></main>")
    return "".join(out)
