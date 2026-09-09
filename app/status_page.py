"""Server-rendered status dashboard (read-only, ids only, no personal data)."""
import html
import json
import time
from collections import defaultdict

from app import db
from app.config import settings

import base64
import os

_STATIC = os.path.join(os.path.dirname(__file__), "static")


def _img(name: str) -> str:
    try:
        with open(os.path.join(_STATIC, name), "rb") as fh:
            return "data:image/png;base64," + base64.b64encode(fh.read()).decode()
    except OSError:
        return ""


# Styling follows Beachcheck's report.css: Montserrat body, Lora headings, navy
# ink on a soft gradient, frosted header card, white panels, uppercase eyebrows,
# pill status badges, Beachcomber Tours footer. Everything centred at --content-max.
CSS = """
@import url("https://fonts.googleapis.com/css2?family=Lora:wght@600;700&family=Montserrat:wght@400;500;600;700;800&display=swap");
:root{--ink-strong:#163047;--ink:#294760;--ink-soft:#59728a;--surface-strong:rgba(255,255,255,.96);--surface-muted:rgba(246,248,251,.92);
--border:rgba(22,48,71,.12);--brand-navy:#15324c;--brand-blue:#1e5278;--brand-sky:#7cc0da;--brand-sand:#dec58c;--brand-mint:#9fd3ba;
--danger-bg:rgba(160,52,52,.08);--danger-text:#8c3232;--shadow-sm:0 10px 22px rgba(18,36,54,.08);--shadow-md:0 18px 40px rgba(18,36,54,.1);--content-max:1200px}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;color:var(--ink);font-family:"Montserrat",Arial,sans-serif;font-size:14px;line-height:1.45;
background:radial-gradient(circle at top left,rgba(124,192,218,.2),transparent 30%),radial-gradient(circle at 90% 10%,rgba(222,197,140,.22),transparent 26%),linear-gradient(180deg,#f5f7fb 0%,#edf2f7 45%,#f7f8fb 100%)}
h1,h2{color:var(--ink-strong);font-family:"Lora",Georgia,serif;font-weight:700;letter-spacing:-.03em;margin:0}
.app-header{padding:24px 20px 0}
.app-header-inner{align-items:center;background:rgba(255,255,255,.74);backdrop-filter:blur(18px);border:1px solid rgba(255,255,255,.72);border-radius:24px;box-shadow:var(--shadow-sm);display:flex;gap:18px;justify-content:space-between;margin:0 auto;max-width:var(--content-max);padding:16px 20px}
.app-brand{display:flex;align-items:center;gap:16px}.app-brand img{height:40px;width:auto;display:block}
.app-brand-name{font-family:"Lora",Georgia,serif;font-size:1.5rem;font-weight:700;letter-spacing:-.03em;color:var(--ink-strong)}
.app-brand-sub{color:var(--ink-soft);font-size:.8rem;letter-spacing:.04em;display:block}
.header-meta{color:var(--ink-soft);font-size:.8rem;text-align:right}
.page-shell{margin:0 auto;max-width:var(--content-max);padding:28px 24px 48px;display:grid;gap:22px}
.eyebrow{color:var(--brand-blue);font-size:.72rem;font-weight:800;letter-spacing:.22em;text-transform:uppercase;margin-bottom:10px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px}
.tile{background:var(--surface-strong);border:1px solid var(--border);border-radius:18px;box-shadow:var(--shadow-sm);padding:16px 18px;text-align:center;display:grid;gap:6px}
.tile .label{color:var(--ink-soft);font-size:.72rem;font-weight:800;letter-spacing:.16em;text-transform:uppercase}
.tile .value{color:var(--ink-strong);font-family:"Lora",Georgia,serif;font-size:1.7rem;line-height:1.1}
.tile .sub{color:var(--ink-soft);font-size:.8rem}
.panel{background:var(--surface-strong);border:1px solid var(--border);border-radius:18px;box-shadow:var(--shadow-sm);padding:22px 24px}
.panel h2{font-size:1.15rem;margin-bottom:14px}
table{border-collapse:collapse;width:100%}
th{color:var(--brand-blue);font-size:.7rem;font-weight:800;letter-spacing:.14em;text-transform:uppercase;text-align:left;padding:8px 10px;border-bottom:1px solid var(--border);background:var(--surface-muted)}
td{padding:8px 10px;border-bottom:1px solid var(--border);font-size:.85rem;vertical-align:top}
tr:last-child td{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums}
.badge{border-radius:999px;display:inline-flex;align-items:center;font-size:.68rem;font-weight:800;letter-spacing:.12em;padding:4px 10px;text-transform:uppercase;white-space:nowrap}
.badge-success{background:rgba(159,211,186,.28);color:#2b7050}.badge-danger{background:var(--danger-bg);color:var(--danger-text)}
.badge-gold{background:rgba(222,197,140,.28);color:#8d6a20}.badge-light{background:rgba(30,82,120,.1);color:var(--brand-blue)}.badge-muted{background:rgba(21,50,76,.05);color:rgba(21,50,76,.45)}
.bar{display:inline-block;height:9px;border-radius:999px;background:linear-gradient(90deg,var(--brand-blue),var(--brand-sky));vertical-align:middle;margin-right:8px}
.muted{color:var(--ink-soft)}code{font-family:ui-monospace,Menlo,monospace;font-size:.78rem;background:var(--surface-muted);padding:2px 6px;border-radius:6px}
.empty{color:#2b7050;font-weight:600}
.app-footer{padding:0 20px 28px}
.app-footer-inner{align-items:center;background:var(--surface-strong);border:1px solid var(--border);border-radius:18px;box-shadow:var(--shadow-sm);display:flex;gap:16px;justify-content:space-between;margin:0 auto;max-width:var(--content-max);padding:18px 22px}
.app-footer-note{color:var(--ink-soft);font-size:.8rem;letter-spacing:.04em}.app-footer img{height:26px;width:auto;opacity:.9;display:block}
@media (max-width:640px){.app-header-inner,.app-footer-inner{flex-direction:column;text-align:center}.header-meta{text-align:center}}
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


def _badge(status: str) -> str:
    cls = {"done": "success", "failed": "danger", "unparsed": "danger", "pending": "gold", "processing": "gold",
           "skipped": "light", "superseded": "muted", "dismissed": "muted"}.get(status, "muted")
    return f'<span class="badge badge-{cls}">{html.escape(status)}</span>'


def render(worker_alive: bool, worker_tick: float) -> str:
    d = db.dashboard()
    c = d["counts"]
    e = html.escape
    dry = settings.effective_dry_run()
    tiles = [
        ("Service", '<span class="badge badge-success">worker running</span>' if worker_alive
         else '<span class="badge badge-danger">worker down</span>', f"tick {_ago(worker_tick)}"),
        ("Writes", '<span class="badge badge-gold">dry run</span>' if dry else '<span class="badge badge-success">live</span>', "to HubSpot"),
        ("Last webhook", e(_ago(c.get("last_webhook_at"))), "from TigerBay"),
        ("Last sync", e(_ago(c.get("last_done_at"))), "event completed"),
        ("Pending", str(c.get("pending", 0)), f"oldest {c.get('oldest_pending_age_s') or 0}s"),
        ("Failed", str(c.get("failed", 0)), "need attention"),
        ("Unparsed", str(c.get("unparsed", 0)), "webhook shape"),
        ("Mapped", str(c.get("mapped", 0)), "TigerBay → HubSpot"),
    ]
    out = [f"<title>beachsync status</title><style>{CSS}</style>",
           '<header class="app-header"><div class="app-header-inner"><div class="app-brand">'
           f'<img src="{_img("nautilus.png")}" alt=""><div><span class="app-brand-name">beachsync</span>'
           '<span class="app-brand-sub">TigerBay → HubSpot profile sync</span></div></div>'
           f'<div class="header-meta">{e(time.strftime("%A %d %B %Y, %H:%M"))}<br>refreshes every minute</div></div></header>',
           '<main class="page-shell">', '<section><div class="eyebrow">Right now</div><div class="tiles">']
    for label, val, sub in tiles:
        out.append(f'<div class="tile"><span class="label">{label}</span><span class="value">{val}</span><span class="sub">{e(sub)}</span></div>')
    out.append("</div></section>")

    a = d["actions_7d"]
    if a:
        out.append('<section class="panel"><h2>Outcomes, last 7 days</h2><table><tr>'
                   + "".join(f"<th>{e(k)}</th>" for k in sorted(a)) + "</tr><tr>"
                   + "".join(f'<td class="num">{a[k]}</td>' for k in sorted(a)) + "</tr></table></section>")

    per_day: dict = defaultdict(lambda: defaultdict(int))
    for r in d["days"]:
        per_day[r["d"]][f'{r["source"]}/{r["status"]}'] += r["n"]
        per_day[r["d"]]["_total"] += r["n"]
    if per_day:
        mx = max(v["_total"] for v in per_day.values()) or 1
        out.append('<section class="panel"><h2>Events per day, last 14 days</h2><table><tr><th>Day</th><th>Total</th><th>Breakdown (source / status)</th></tr>')
        for day in sorted(per_day, reverse=True):
            v = per_day[day]
            br = ", ".join(f"{k} {n}" for k, n in sorted(v.items()) if k != "_total")
            out.append(f'<tr><td>{e(day)}</td><td><span class="bar" style="width:{max(6, int(200 * v["_total"] / mx))}px"></span>{v["_total"]}</td><td class="muted">{e(br)}</td></tr>')
        out.append("</table></section>")

    out.append('<section class="panel"><h2>Needs attention</h2>')
    if d["problems"]:
        out.append("<table><tr><th>#</th><th>When</th><th>Source</th><th>Entity</th><th>Event</th><th>Id</th><th>Status</th><th>Attempts</th><th>Error / body</th></tr>")
        for p in d["problems"]:
            out.append(f'<tr><td>{p["id"]}</td><td>{_t(p["received_at"])}</td><td>{e(p["source"])}</td><td>{e(p["entity"])}</td>'
                       f'<td>{e(p["event"])}</td><td>{p["entity_id"] or ""}</td><td>{_badge(p["status"])}</td><td class="num">{p["attempts"]}</td>'
                       f'<td><code>{e(p["last_error"] or p["raw_body"])}</code></td></tr>')
        out.append("</table>")
    else:
        out.append('<p class="empty">Nothing failed or unparsed.</p>')
    out.append("</section>")

    out.append('<section class="panel"><h2>Recent events</h2><table><tr><th>#</th><th>Received</th><th>Source</th><th>Entity</th><th>Event</th><th>Id</th><th>Status</th><th>Result</th></tr>')
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
                if j.get("superseded_by"):
                    res = f"superseded by #{j['superseded_by']}"
            else:
                res = r["result"][:120]
        elif r["last_error"]:
            res = r["last_error"]
        out.append(f'<tr><td>{r["id"]}</td><td>{_t(r["received_at"])}</td><td>{e(r["source"])}</td><td>{e(r["entity"])}</td>'
                   f'<td>{e(r["event"])}</td><td>{r["entity_id"] or ""}</td><td>{_badge(r["status"])}</td><td class="muted">{e(res)}</td></tr>')
    out.append("</table></section></main>")
    out.append('<footer class="app-footer"><div class="app-footer-inner"><span class="app-footer-note">beachsync · Beachcomber Tours</span>'
               f'<img src="{_img("tours.png")}" alt="Beachcomber Tours"></div></footer>')
    return "".join(out)
