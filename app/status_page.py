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


# Display names for internal action codes.
ACTION_LABELS = {"noop": "no change", "create": "created", "update": "updated", "skipped": "skipped",
                 "archive": "archived", "update-after-conflict": "updated (existing email)", "error": "error"}


def _action(a) -> str:
    return ACTION_LABELS.get(str(a), str(a))


def _dot(status: str) -> str:
    tone = {"done": "pass", "failed": "fail", "unparsed": "fail", "pending": "warn", "processing": "warn"}.get(status, "idle")
    return f'<span class="bc-status"><span class="bc-dot bc-dot--{tone}"></span><span class="bc-status-label">{html.escape(status)}</span></span>'


def _pill(text: str, tone: str) -> str:
    return f'<span class="bc-pill bc-pill--{tone}">{html.escape(text)}</span>'


def _shell_open(active: str, crumb: str, service_tone: str, service_label: str) -> str:
    e = html.escape
    rows = [("status", "/status", "Status"), ("events", "/events", "Events"), ("sweeps", "/sweeps", "Sync Reports")]
    nav = "".join(f'<a class="bc-nav-row{" bc-nav-row--on" if k == active else ""}" href="{href}"'
                  f'{" aria-current=page" if k == active else ""}>{label}</a>' for k, href, label in rows)
    return ('<div class="bc-shell"><aside class="bc-sidebar">'
            '<a class="bc-brand" href="/status"><img src="/static/logos/logo-shell-white.svg" alt="" width="26" height="31">'
            '<span class="bc-brand-name">Beachsync</span></a><div class="bc-brand-rule"></div>'
            f'<p class="bc-kicker bc-nav-group">Sync</p>{nav}'
            '</aside>'
            '<div class="bc-main-col"><header class="bc-band">'
            f'<span class="bc-crumb-kicker">Beachsync</span><span class="bc-crumb-sep">/</span><span class="bc-crumb-title">{e(crumb)}</span>'
            f'<div class="bc-band-right"><span class="bc-status"><span class="bc-dot bc-dot--{service_tone}"></span>'
            f'<span class="bc-band-note">{e(service_label)}</span></span><span class="bc-band-div"></span>'
            f'<span class="bc-band-note">{e(time.strftime("%a %d %b · %H:%M"))}</span></div></header><main class="bc-page">')


def service_state(worker_alive: bool):
    dry = settings.effective_dry_run()
    paused = db.paused()
    tone = "pass" if worker_alive and not dry else ("warn" if worker_alive else "fail")
    label = "Worker running" if worker_alive else "Worker down"
    if worker_alive and dry:
        label = "Dry run"
    if paused:
        tone, label = "fail", "Sync paused"
    return tone, label, dry, paused


def render_sweeps(worker_alive: bool) -> str:
    e = html.escape
    tone, label, _, _ = service_state(worker_alive)
    out = [f"<title>Beachsync · Sync Reports</title>{LINKS}", _shell_open("sweeps", "Sync Reports", tone, label),
           '<div class="bc-page-head"><span class="bc-kicker bc-kicker--page">Safety Net</span><h1 class="bc-h1">Sync Reports</h1>'
           '<p class="bc-intro">The nightly new-id sweep queues TigerBay records whose <em>created</em> webhook never arrived. '
           'The daily drift sweep reconciles every record against HubSpot and re-queues any that differ (missed <em>modified</em> webhooks). '
           'Each run is listed with what it found and how the queued events turned out.</p></div>']
    sweeps = db.list_sweeps()
    if not sweeps:
        out.append('<section class="bc-section"><p class="bc-empty">No reports yet. Nightly at 02:40 (new ids) and daily at 04:00 (drift).</p></section>')
    for sw in sweeps:
        sm = sw.get("summary") or {}
        dur = f'{int((sw["finished_at"] or time.time()) - sw["started_at"]) // 60}m {int((sw["finished_at"] or time.time()) - sw["started_at"]) % 60}s'
        if sw["ok"] is None:
            state = _dot_word("warn", "Running")
        elif sw["ok"]:
            state = _dot_word("pass", "Completed")
        else:
            state = _dot_word("fail", "Failed")
        kind = "New-ID Sweep" if sw["kind"] == "new-ids" else "Drift Sweep"
        out.append(f'<section class="bc-section"><div class="bc-section-head"><h2 class="bc-h2">{kind}</h2>'
                   f'<span class="bc-meta">{e(_t(sw["started_at"]))} · {dur} · #{sw["id"]}</span></div>')
        cells = [("Run", state)]
        if sw["kind"] == "new-ids" and sm:
            cq, sq = sm.get("customers", {}).get("queued", []), sm.get("staff", {}).get("queued", [])
            cells += [("Customers Found", f'<span class="bc-fig">{len(cq)}</span>'),
                      ("Staff Found", f'<span class="bc-fig">{len(sq)}</span>'),
                      ("Scanned From", f'customer {sm.get("customers", {}).get("from")} · staff {sm.get("staff", {}).get("from")}')]
        elif sw["kind"] == "drift" and sm:
            rep = sm.get("report") or {}
            cells += [("Customers Re-queued", f'<span class="bc-fig">{sm.get("customer", 0)}</span>'),
                      ("Staff Re-queued", f'<span class="bc-fig">{sm.get("agent", 0)}</span>'),
                      ("Checked", f'customers {rep.get("customers", {}).get("checked", "?")} · staff {rep.get("staff", {}).get("checked", "?")}')]
        elif sw["error"]:
            cells += [("Error", f"<code>{e(sw['error'][:200])}</code>")]
        out.append('<div class="bc-statband bc-statband--inline">' + "".join(
            f'<div class="bc-statcell"><span>{k}</span><strong>{v}</strong></div>' for k, v in cells) + "</div>")
        if sw.get("outcomes"):
            oc = sw["outcomes"]
            out.append('<table class="bc-grid"><tr>' + "".join(f"<th>{e(_action(k))}</th>" for k in sorted(oc)) + "</tr><tr>"
                       + "".join(f'<td class="num" style="text-align:left">{oc[k]}</td>' for k in sorted(oc)) + "</tr></table>")
        if sw["kind"] == "drift" and sm.get("report", {}).get("field_diff_counts"):
            fd = sm["report"]["field_diff_counts"]
            top = sorted(fd.items(), key=lambda kv: -kv[1])[:12]
            out.append('<p class="bc-meta" style="margin:14px 0 6px">Fields That Differed (top 12)</p><table class="bc-grid"><tr>'
                       + "".join(f"<th>{e(k)}</th>" for k, _ in top) + "</tr><tr>"
                       + "".join(f'<td class="num" style="text-align:left">{n}</td>' for _, n in top) + "</tr></table>")
        ids = (sm.get("customers", {}).get("queued") if sw["kind"] == "new-ids" else None)
        if ids:
            out.append(f'<p class="bc-meta" style="margin-top:12px">Customer ids: {e(", ".join(map(str, ids[:60])))}'
                       + (" …" if len(ids) > 60 else "") + "</p>")
        if sw["kind"] == "new-ids" and sm.get("staff", {}).get("queued"):
            sids = sm["staff"]["queued"]
            out.append(f'<p class="bc-meta">Staff ids: {e(", ".join(map(str, sids[:60])))}' + (" …" if len(sids) > 60 else "") + "</p>")
        if sw["kind"] == "drift" and sw["report_path"] and sw["ok"]:
            out.append(f'<p class="bc-meta" style="margin-top:12px"><a class="bc-link" href="/sweeps/{sw["id"]}/report">Download the Diff CSV</a> '
                       '(contains names and emails; LAN only)</p>')
        out.append("</section>")
    out.append('<div class="bc-foot"><span class="bc-meta">Beachsync · Beachcomber Tours</span>'
               '<img src="/static/logos/logo-wordmark-navy.svg" alt="Beachcomber Tours"></div></main></div></div>')
    return "".join(out)


def render(worker_alive: bool, worker_tick: float, resume_denied: bool = False) -> str:
    d = db.dashboard()
    c = d["counts"]
    e = html.escape
    dry = settings.effective_dry_run()
    paused = db.paused()
    service_tone = "pass" if worker_alive and not dry else ("warn" if worker_alive else "fail")
    service_label = "Worker running" if worker_alive else "Worker down"
    if worker_alive and dry:
        service_label = "Dry run"
    if paused:
        service_tone, service_label = "fail", "Sync paused"
    out = [f"<title>Beachsync · Status</title>{LINKS}", _shell_open("status", "Status", service_tone, service_label),
           '<div class="bc-page-head"><span class="bc-kicker bc-kicker--page">Profile Sync</span><h1 class="bc-h1">TigerBay → HubSpot</h1>'
           '<p class="bc-intro">Customer and agent-staff profiles, kept in step by TigerBay webhooks with a nightly new-id sweep and a daily drift repair. Refreshes every minute.</p></div>']

    # kill switch
    if paused:
        out.append('<div class="bc-switch bc-switch--off"><div><span class="bc-kicker">Kill Switch</span>'
                   f'<h2 class="bc-h2">Sync Is Paused</h2><p class="bc-intro">Since {e(_t(paused["updated_at"]))} · {e(paused["value"])}. '
                   'Webhooks are still being received and queued; nothing is written to HubSpot until resumed.</p>'
                   + ('<p class="bc-intro bc-danger">Admin token not accepted.</p>' if resume_denied else '') + '</div>'
                   '<form method="post" action="/status/resume" class="bc-switch-form">'
                   '<input class="bc-input" type="password" name="token" placeholder="Admin token" autocomplete="off" required>'
                   '<button class="bc-btn bc-btn--gold" type="submit">Resume Sync</button></form></div>')
    else:
        out.append('<div class="bc-switch"><div><span class="bc-kicker">Kill Switch</span>'
                   '<h2 class="bc-h2">Sync Is Running</h2><p class="bc-intro">Pausing stops all writes to HubSpot immediately. '
                   'Webhooks keep queueing, so nothing is lost; resuming needs the admin token.</p></div>'
                   '<form method="post" action="/status/pause" class="bc-switch-form" onsubmit="return confirm(\'Pause the TigerBay → HubSpot sync?\')">'
                   '<input class="bc-input" type="text" name="reason" placeholder="Reason (optional)" maxlength="200">'
                   '<button class="bc-btn bc-btn--danger" type="submit">Pause Sync</button></form></div>')

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
               f'<div class="bc-statcell"><span>Writes to HubSpot</span><strong>{_dot_word("warn" if dry else "pass", "Dry run" if dry else "Live")}</strong></div>'
               f'<div class="bc-statcell"><span>Last Webhook</span><strong>{e(_ago(c.get("last_webhook_at")))}</strong></div>'
               f'<div class="bc-statcell"><span>Last Sync</span><strong>{e(_ago(c.get("last_done_at")))}</strong></div></div>')

    a = d["actions_7d"]
    if a:
        out.append('<section class="bc-section"><div class="bc-section-head"><h2 class="bc-h2">Outcomes</h2><span class="bc-meta">last 7 days</span></div>'
                   '<table class="bc-grid"><tr>' + "".join(f"<th>{e(_action(k))}</th>" for k in sorted(a)) + "</tr><tr>"
                   + "".join(f'<td class="num" style="text-align:left">{a[k]}</td>' for k in sorted(a)) + "</tr></table></section>")

    per_day: dict = defaultdict(lambda: defaultdict(int))
    for r in d["days"]:
        per_day[r["d"]][f'{r["source"]} · {r["status"]}'] += r["n"]
        per_day[r["d"]]["_total"] += r["n"]
    if per_day:
        mx = max(v["_total"] for v in per_day.values()) or 1
        out.append('<section class="bc-section"><div class="bc-section-head"><h2 class="bc-h2">Events Per Day</h2><span class="bc-meta">last 14 days</span></div>'
                   '<table class="bc-grid"><tr><th style="width:120px">Day</th><th style="width:300px">Total</th><th>Breakdown</th></tr>')
        for day in sorted(per_day, reverse=True):
            v = per_day[day]
            br = ", ".join(f"{k} {n}" for k, n in sorted(v.items()) if k != "_total")
            out.append(f'<tr><td class="day">{e(day)}</td><td class="total"><span class="bc-total"><span class="bc-bar" style="width:{max(6, int(200 * v["_total"] / mx))}px"></span>'
                       f'<span class="bc-fig">{v["_total"]}</span></span></td><td class="mute">{e(br)}</td></tr>')
        out.append("</table></section>")

    out.append('<section class="bc-section"><div class="bc-section-head"><h2 class="bc-h2">Needs Attention</h2><span class="bc-meta">failed and unparsed</span></div>')
    if d["problems"]:
        out.append('<table class="bc-grid"><tr><th>#</th><th>When</th><th>Source</th><th>Entity</th><th>Event</th><th>Id</th><th>Status</th><th>Tries</th><th>Error / Body</th></tr>')
        for p in d["problems"]:
            out.append(f'<tr><td class="mute">{p["id"]}</td><td class="when">{_t(p["received_at"])}</td><td>{e(p["source"])}</td><td>{e(p["entity"])}</td>'
                       f'<td>{e(p["event"])}</td><td>{p["entity_id"] or ""}</td><td>{_dot(p["status"])}</td><td>{p["attempts"]}</td>'
                       f'<td><code>{e(p["last_error"] or p["raw_body"])}</code></td></tr>')
        out.append("</table>")
    else:
        out.append('<p class="bc-empty">Nothing failed or unparsed.</p>')
    out.append("</section>")

    out.append('<div class="bc-foot"><span class="bc-meta">Beachsync · Beachcomber Tours</span>'
               '<img src="/static/logos/logo-wordmark-navy.svg" alt="Beachcomber Tours"></div>')
    out.append("</main></div></div>")
    return "".join(out)


def _result_text(r: dict) -> str:
    res = ""
    if r["result"]:
        try:
            j = json.loads(r["result"])
        except ValueError:
            j = None
        if j:
            res = _action(j.get("action", ""))
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
    return res


def _event_rows(rows: list[dict]) -> str:
    e = html.escape
    out = ['<table class="bc-grid"><tr><th>#</th><th>Received</th><th>Source</th><th>Entity</th><th>Event</th><th>Id</th><th>Status</th><th>Result</th></tr>']
    for r in rows:
        out.append(f'<tr><td class="mute">{r["id"]}</td><td class="when">{_t(r["received_at"])}</td><td>{e(r["source"])}</td><td>{e(r["entity"])}</td>'
                   f'<td>{e(r["event"])}</td><td>{r["entity_id"] or ""}</td><td>{_dot(r["status"])}</td><td class="mute">{e(_result_text(r))}</td></tr>')
    out.append("</table>")
    return "".join(out)


STATUSES = ["pending", "processing", "done", "skipped", "failed", "unparsed", "superseded", "dismissed"]
SOURCES = ["webhook", "fanout", "sweep", "backfill", "replay"]


def render_events(worker_alive: bool, status: str = "", source: str = "", limit: int = 200) -> str:
    e = html.escape
    tone, label, _, _ = service_state(worker_alive)
    rows = db.recent_events(limit, status or None, source or None)
    out = [f"<title>Beachsync · Events</title>{LINKS}", _shell_open("events", "Events", tone, label),
           '<div class="bc-page-head"><span class="bc-kicker bc-kicker--page">Queue</span><h1 class="bc-h1">Events</h1>'
           '<p class="bc-intro">Every webhook, fan-out child, sweep and manual replay, newest first. '
           'Ids are TigerBay ids; the result column says what was written to HubSpot.</p></div>',
           '<section class="bc-section"><form method="get" action="/events" class="bc-filter">'
           '<label class="bc-kicker">Status</label><select class="bc-input bc-select" name="status" onchange="this.form.submit()"><option value="">all</option>'
           + "".join(f'<option value="{s_}"{" selected" if s_ == status else ""}>{s_}</option>' for s_ in STATUSES)
           + '</select><label class="bc-kicker">Source</label><select class="bc-input bc-select" name="source" onchange="this.form.submit()"><option value="">all</option>'
           + "".join(f'<option value="{s_}"{" selected" if s_ == source else ""}>{s_}</option>' for s_ in SOURCES)
           + f'</select><span class="bc-meta">showing {len(rows)} of up to {limit}</span></form>']
    out.append(_event_rows(rows) if rows else '<p class="bc-empty">No events match.</p>')
    out.append('</section><div class="bc-foot"><span class="bc-meta">Beachsync · Beachcomber Tours</span>'
               '<img src="/static/logos/logo-wordmark-navy.svg" alt="Beachcomber Tours"></div></main></div></div>')
    return "".join(out)


def _dot_word(tone: str, word: str) -> str:
    return f'<span class="bc-status"><span class="bc-dot bc-dot--{tone}"></span><span class="bc-status-label">{html.escape(word)}</span></span>'
