"""Change digest: what the sync actually did to HubSpot over a window.

Rolls up the per-event audit results (``action``, ``changed``) stored on the
events table into counts per record type and action, the fields that changed
most, the records touched, and anything that failed. Ids only, no personal
data: the from/to values on each event are deliberately not surfaced here.
"""
import json
import time
from collections import Counter, defaultdict
from typing import Optional

from app import db

MAX_RECORDS = 500
ID_STAMPS = {"tigerbay_id", "tigerbay_customer_id", "tigerbay_agent_id", "source_last_modified"}
WINDOWS = {"today": "Today", "yesterday": "Yesterday", "7d": "Last 7 Days", "30d": "Last 30 Days"}


def window(name: str, now: Optional[float] = None) -> tuple[float, float]:
    """Return (since, until) epoch bounds for a preset, local midnight aligned."""
    now = now or time.time()
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    if name == "yesterday":
        return midnight - 86400, midnight
    if name == "7d":
        return midnight - 6 * 86400, now
    if name == "30d":
        return midnight - 29 * 86400, now
    return midnight, now


def _entity_of(j: dict, row_entity: str) -> str:
    ent = j.get("entity") or row_entity
    return {"agent": "agency"}.get(ent, ent) if j.get("object") == "companies" else ent


def _walk(j: dict, row_entity: str):
    """Yield the audit dicts in an event result: the top-level one plus any staff
    synced inline under an agency event."""
    if j.get("action"):
        yield _entity_of(j, row_entity), j
    for s in j.get("staff") or []:
        if isinstance(s, dict) and s.get("action"):
            yield "staff", s
    if isinstance(j.get("company"), dict) and j["company"].get("action"):
        yield "agency", j["company"]


def build(since: float, until: float) -> dict:
    conn = db.connect()
    try:
        done = conn.execute(
            "SELECT id, received_at, processed_at, source, entity, event, entity_id, status, result"
            " FROM events WHERE status IN ('done','skipped') AND processed_at>=? AND processed_at<? ORDER BY processed_at",
            (since, until)).fetchall()
        failed = conn.execute(
            "SELECT id, received_at, source, entity, event, entity_id, status, attempts,"
            " substr(coalesce(last_error,''),1,300) AS last_error"
            " FROM events WHERE status IN ('failed','unparsed') AND received_at>=? AND received_at<? ORDER BY id",
            (since, until)).fetchall()
        reverted = conn.execute(
            "SELECT COUNT(*) FROM events WHERE status='reverted' AND processed_at>=? AND processed_at<?",
            (since, until)).fetchone()[0]
        received = conn.execute(
            "SELECT source, COUNT(*) AS n FROM events WHERE received_at>=? AND received_at<? GROUP BY source",
            (since, until)).fetchall()
        sweeps = conn.execute(
            "SELECT id, kind, started_at, finished_at, ok, summary FROM sweeps WHERE started_at>=? AND started_at<? ORDER BY id",
            (since, until)).fetchall()
    finally:
        conn.close()

    actions: dict = defaultdict(Counter)          # entity -> action -> n
    fields_updated: Counter = Counter()           # field -> n (updates only)
    fields_created: Counter = Counter()
    records: list[dict] = []
    dry_run = 0
    for r in done:
        try:
            j = json.loads(r["result"]) if r["result"] else {}
        except ValueError:
            j = {}
        if not isinstance(j, dict):
            continue
        for ent, a in _walk(j, r["entity"]):
            act = a["action"]
            changed = [c for c in (a.get("changed") or []) if c != "source_last_modified"]
            if act == "update" and changed and set(changed) <= ID_STAMPS:
                act = "stamped"          # only TigerBay id stamps written: not a data change
            actions[ent][act] += 1
            if a.get("dry_run"):
                dry_run += 1
            if act == "create":
                fields_created.update(changed)
            elif act in ("update", "update-after-conflict"):
                fields_updated.update(changed)
            if act not in ("noop", "stamped"):
                records.append({"event_id": r["id"], "when": r["processed_at"], "source": r["source"],
                                "entity": ent, "tigerbay_id": a.get("tigerbay_id", r["entity_id"]),
                                "hubspot_id": a.get("hubspot_id"), "action": act, "changed": changed,
                                "note": a.get("note")})
    totals = Counter()
    for c in actions.values():
        totals.update(c)
    return {
        "since": since, "until": until,
        "received": {r["source"]: r["n"] for r in received},
        "processed": len(done),
        "totals": dict(totals),
        "actions": {k: dict(v) for k, v in actions.items()},
        "fields_updated": dict(fields_updated.most_common()),
        "fields_created": dict(fields_created.most_common()),
        "records": records[-MAX_RECORDS:],
        "records_total": len(records),
        "failures": [dict(r) for r in failed],
        "sweeps": [{**dict(s), "summary": _load(s["summary"])} for s in sweeps],
        "dry_run": dry_run,
        "reverted": reverted,
    }


def _load(s):
    try:
        return json.loads(s) if s else None
    except ValueError:
        return None


def one_liner(d: dict, label: str) -> str:
    """Plain-text summary for the Teams post: counts and ids only."""
    t = d["totals"]
    parts = []
    for act, word in (("create", "created"), ("update", "updated"), ("update-after-conflict", "updated (existing email)"),
                      ("archive", "archived"), ("stamped", "id-stamped only"), ("skipped", "skipped")):
        if t.get(act):
            parts.append(f"{t[act]} {word}")
    noop = t.get("noop", 0)
    head = f"beachsync {label}: " + (", ".join(parts) if parts else "no HubSpot changes")
    head += f"; {noop} checked with no change" if noop else ""
    bits = []
    per = d["actions"]
    for ent in ("customer", "staff", "agency"):
        c = per.get(ent) or {}
        n = sum(v for k, v in c.items() if k not in ("noop", "stamped"))
        if n:
            bits.append(f"{ent} {n}")
    if bits:
        head += f" ({', '.join(bits)})"
    if d["fields_updated"]:
        top = list(d["fields_updated"].items())[:5]
        head += ". Top fields: " + ", ".join(f"{k} {n}" for k, n in top)
    if d["failures"]:
        head += f". {len(d['failures'])} FAILED/unparsed needing attention"
    if d["dry_run"]:
        head += f". NOTE {d['dry_run']} were dry-run (not written)"
    if d["reverted"]:
        head += f". {d['reverted']} earlier writes were reverted and are not counted"
    return head + ". Detail: https://beachsync.bctuk.com/digest"
