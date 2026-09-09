"""Server-rendered status dashboard (read-only, ids only, no personal data)."""
import html
import json
import time
from collections import defaultdict

from app import db
from app.config import settings

LINKS = ('<link rel="stylesheet" href="/static/css/fonts.css"><link rel="stylesheet" href="/static/css/tokens.css">'
         '<link rel="stylesheet" href="/static/css/dashboard.css">')


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


def _dot(status: str) -> str:
    tone = {"done": "pass", "failed": "fail", "unparsed": "fail", "pending": "warn", "processing": "warn"}.get(status, "idle")
    return f'<span class="bc-status"><span class="bc-dot bc-dot--{tone}"></span><span class="bc-status-label">{html.escape(status)}</span></span>'


def _pill(text: str, tone: str) -> str:
    return f'<span class="bc-pill bc-pill--{tone}">{html.escape(text)}</span>'


def render(worker_alive: bool, worker_tick: float) -> str:
    d = db.dashboard()
    c = d["counts"]
    e = html.escape
    dry = settings.effective_dry_run()
    service_tone = "pass" if worker_alive and not dry else ("warn" if worker_alive else "fail")
    service_label = "worker running" if worker_alive else "worker down"
    if worker_alive and dry:
        service_label = "dry run"
    out = [f"<title>beachsync · status</title>{LINKS}",
           '<div class="bc-shell"><aside class="bc-sidebar">'
           '<a class="bc-brand" href="/status"><img src="/static/logos/logo-shell-white.svg" alt="" width="26" height="31">'
           '<span class="bc-brand-name">beachsync</span></a><div class="bc-brand-rule"></div>'
           '<p class="bc-kicker bc-nav-group">Sync</p>'
           '<a class="bc-nav-row bc-nav-row--on" href="/status" aria-current="page">Status</a>'
           '<div class="bc-sidebar-foot"><div class="bc-sidebar-hair"></div>'
           '<p class="bc-sidebar-user">TigerBay → HubSpot<br>Beachcomber Tours</p></div></aside>',
           '<div class="bc-main-col"><header class="bc-band">'
           '<span class="bc-crumb-kicker">Sync</span><span class="bc-crumb-sep">/</span><span class="bc-crumb-title">Status</span>'
           f'<div class="bc-band-right"><span class="bc-status"><span class="bc-dot bc-dot--{service_tone}"></span>'
           f'<span class="bc-band-note">{e(service_label)}</span></span><span class="bc-band-div"></span>'
           f'<span class="bc-band-note">{e(time.strftime("%a %d %b · %H:%M"))}</span></div></header>',
           '<main class="bc-page">',
           '<div class="bc-page-head"><span class="bc-kicker bc-kicker--page">Profile sync</span><h1 class="bc-h1">TigerBay → HubSpot</h1>'
           '<p class="bc-intro">Customer and agent-staff profiles, kept in step by TigerBay webhooks with a nightly new-id sweep and a daily drift repair. Refreshes every minute.</p></div>']

    # hero figures
    heros = [("Pending", c.get("pending", 0), f"oldest {c.get('oldest_pending_age_s') or 0}s"),
             ("Failed", c.get("failed", 0), "need attention"),
             ("Unparsed", c.get("unparsed", 0), "webhook shape"),
             ("Mapped", c.get("mapped", 0), "TigerBay → HubSpot ids")]
    out.append('<div class="bc-herostats">')
    for label, val, sub in heros:
        out.append(f'<div class="bc-herostat"><span class="bc-kicker">{label}</span><span class="bc-fig bc-fig--35">{val}</span>'
                   f'<span class="bc-herostat-sub">{e(sub)}</span></div>')
    out.append("</div>")
    out.append('<div class="bc-statband">'
               f'<div class="bc-statcell"><span>Service</span><strong>{_dot_word(service_tone, service_label)}</strong></div>'
               f'<div class="bc-statcell"><span>Writes to HubSpot</span><strong>{_dot_word("warn" if dry else "pass", "dry run" if dry else "live")}</strong></div>'
               f'<div class="bc-statcell"><span>Last webhook</span><strong>{e(_ago(c.get("last_webhook_at")))}</strong></div>'
               f'<div class="bc-statcell"><span>Last sync</span><strong>{e(_ago(c.get("last_done_at")))}</strong></div></div>')

    a = d["actions_7d"]
    if a:
        out.append('<section class="bc-section"><div class="bc-section-head"><h2 class="bc-h2">Outcomes</h2><span class="bc-meta">last 7 days</span></div>'
                   '<table class="bc-grid"><tr>' + "".join(f"<th>{e(k)}</th>" for k in sorted(a)) + "</tr><tr>"
                   + "".join(f'<td class="num" style="text-align:left">{a[k]}</td>' for k in sorted(a)) + "</tr></table></section>")

    per_day: dict = defaultdict(lambda: defaultdict(int))
    for r in d["days"]:
        per_day[r["d"]][f'{r["source"]} · {r["status"]}'] += r["n"]
        per_day[r["d"]]["_total"] += r["n"]
    if per_day:
        mx = max(v["_total"] for v in per_day.values()) or 1
        out.append('<section class="bc-section"><div class="bc-section-head"><h2 class="bc-h2">Events per day</h2><span class="bc-meta">last 14 days</span></div>'
                   '<table class="bc-grid"><tr><th style="width:120px">Day</th><th style="width:300px">Total</th><th>Breakdown</th></tr>')
        for day in sorted(per_day, reverse=True):
            v = per_day[day]
            br = ", ".join(f"{k} {n}" for k, n in sorted(v.items()) if k != "_total")
            out.append(f'<tr><td class="day">{e(day)}</td><td class="total"><span class="bc-total"><span class="bc-bar" style="width:{max(6, int(200 * v["_total"] / mx))}px"></span>'
                       f'<span class="bc-fig">{v["_total"]}</span></span></td><td class="mute">{e(br)}</td></tr>')
        out.append("</table></section>")

    out.append('<section class="bc-section"><div class="bc-section-head"><h2 class="bc-h2">Needs attention</h2><span class="bc-meta">failed and unparsed</span></div>')
    if d["problems"]:
        out.append('<table class="bc-grid"><tr><th>#</th><th>When</th><th>Source</th><th>Entity</th><th>Event</th><th>Id</th><th>Status</th><th>Tries</th><th>Error / body</th></tr>')
        for p in d["problems"]:
            out.append(f'<tr><td class="mute">{p["id"]}</td><td class="when">{_t(p["received_at"])}</td><td>{e(p["source"])}</td><td>{e(p["entity"])}</td>'
                       f'<td>{e(p["event"])}</td><td>{p["entity_id"] or ""}</td><td>{_dot(p["status"])}</td><td>{p["attempts"]}</td>'
                       f'<td><code>{e(p["last_error"] or p["raw_body"])}</code></td></tr>')
        out.append("</table>")
    else:
        out.append('<p class="bc-empty">Nothing failed or unparsed.</p>')
    out.append("</section>")

    out.append('<section class="bc-section"><div class="bc-section-head"><h2 class="bc-h2">Recent events</h2><span class="bc-meta">latest 40</span></div>'
               '<table class="bc-grid"><tr><th>#</th><th>Received</th><th>Source</th><th>Entity</th><th>Event</th><th>Id</th><th>Status</th><th>Result</th></tr>')
    for r in d["recent"]:
        res = ""
        if r["result"]:
            try:
                j = json.loads(r["result"])
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
        out.append(f'<tr><td class="mute">{r["id"]}</td><td class="when">{_t(r["received_at"])}</td><td>{e(r["source"])}</td><td>{e(r["entity"])}</td>'
                   f'<td>{e(r["event"])}</td><td>{r["entity_id"] or ""}</td><td>{_dot(r["status"])}</td><td class="mute">{e(res)}</td></tr>')
    out.append("</table></section>")
    out.append('<div class="bc-foot"><span class="bc-meta">beachsync · Beachcomber Tours</span>'
               '<img src="/static/logos/logo-wordmark-navy.svg" alt="Beachcomber Tours"></div>')
    out.append("</main></div></div>")
    return "".join(out)


def _dot_word(tone: str, word: str) -> str:
    return f'<span class="bc-status"><span class="bc-dot bc-dot--{tone}"></span><span class="bc-status-label">{html.escape(word)}</span></span>'
