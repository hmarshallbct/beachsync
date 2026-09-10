"""Revert what a drift sweep wrote to HubSpot, using the before-values stored on
each event's audit result.

    python -m app.revert_sweep --since "2026-09-10 04:00"            # dry run: counts + sample
    python -m app.revert_sweep --since "2026-09-10 04:00" --live     # PATCH the old values back

For every ``source=sweep`` event with ``action=update`` in the window, each
changed field is set back to its ``from`` value (blank if it was empty), except:

* the TigerBay id stamps (``tigerbay_id``, ``tigerbay_customer_id``,
  ``tigerbay_agent_id``) and ``is_archived`` are kept (``--all`` reverts them too);
* a field that a later non-sweep event (a real TigerBay webhook) also changed is
  left at the webhook's value.

Batch PATCH, 100 records per call, at the configured HubSpot rate. Each reverted
event gets ``status='reverted'`` (excluded from the dashboard and digest counts)
and ``reverted_at`` on its result, so a re-run skips it.
"""
import argparse
import json
import logging
import sys
import time
from collections import Counter, defaultdict

from app import db
from app.hubspot import HubSpotError, client

log = logging.getLogger("revert")
KEEP = {"tigerbay_id", "tigerbay_customer_id", "tigerbay_agent_id", "is_archived", "source_last_modified"}


def _ts(s: str) -> float:
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M"))


def plan(since: float, until: float, revert_all: bool, shared_only: bool = False) -> list[dict]:
    """One item per HubSpot record. When several sweep events wrote to the same
    HubSpot record (a person who is both a customer and agent staff), the
    EARLIEST event's before-value wins for each field: that is the pre-sweep
    state. ``shared_only`` re-plans just those multi-event records, ignoring
    ``reverted_at`` (used to repair a first pass that processed them in order)."""
    items = _plan_events(since, until, revert_all, ignore_reverted=shared_only)
    by_hs: dict = defaultdict(list)
    for it in items:
        by_hs[(it["object"], it["hubspot_id"])].append(it)
    out = []
    for lst in by_hs.values():
        if shared_only and len(lst) < 2:
            continue
        lst.sort(key=lambda i: i["event_id"])
        merged = dict(lst[0])
        merged["properties"] = dict(lst[0]["properties"])
        merged["event_ids"] = [i["event_id"] for i in lst]
        for it in lst[1:]:
            for f, v in it["properties"].items():
                merged["properties"].setdefault(f, v)
        out.append(merged)
    out.sort(key=lambda i: i["event_id"])
    return out


def _plan_events(since: float, until: float, revert_all: bool, ignore_reverted: bool = False) -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, entity, entity_id, processed_at, result FROM events WHERE source='sweep' AND status IN ('done','reverted')"
            " AND processed_at>=? AND processed_at<? ORDER BY processed_at", (since, until)).fetchall()
        later = conn.execute(
            "SELECT entity, entity_id, processed_at, result FROM events WHERE source<>'sweep' AND status='done'"
            " AND processed_at>=?", (since,)).fetchall()
    finally:
        conn.close()
    # fields changed by later real events, per record: {(entity,id): {field: processed_at}}
    touched: dict = defaultdict(dict)
    for r in later:
        try:
            j = json.loads(r["result"] or "{}")
        except ValueError:
            continue
        for f in j.get("changed") or []:
            touched[(r["entity"], r["entity_id"])][f] = max(touched[(r["entity"], r["entity_id"])].get(f, 0), r["processed_at"])
    out = []
    for r in rows:
        try:
            j = json.loads(r["result"] or "{}")
        except ValueError:
            continue
        if j.get("action") != "update" or not j.get("hubspot_id") or j.get("dry_run"):
            continue
        if j.get("reverted_at") and not ignore_reverted:
            continue
        props, skipped = {}, []
        for f, d in (j.get("diff") or {}).items():
            if not revert_all and f in KEEP:
                continue
            if touched[(r["entity"], r["entity_id"])].get(f, 0) > r["processed_at"]:
                skipped.append(f)
                continue
            props[f] = "" if d.get("from") is None else str(d["from"])
        if props:
            out.append({"event_id": r["id"], "entity": r["entity"], "tigerbay_id": r["entity_id"],
                        "object": j.get("object", "contacts"), "hubspot_id": str(j["hubspot_id"]),
                        "properties": props, "kept_from_webhook": skipped})
    return out


def apply(items: list[dict], batch: int = 100) -> dict:
    hs = client()
    ok = failed = 0
    errors = []
    by_obj: dict = defaultdict(list)
    for it in items:
        by_obj[it["object"]].append(it)
    for obj, lst in by_obj.items():
        for i in range(0, len(lst), batch):
            chunk = lst[i:i + batch]
            body = {"inputs": [{"id": it["hubspot_id"], "properties": it["properties"]} for it in chunk]}
            try:
                hs.request("POST", f"/crm/v3/objects/{obj}/batch/update", json=body)
            except HubSpotError as exc:
                # batch rejected wholesale (one bad id fails the lot): fall back to one by one
                log.warning("batch failed (%s); retrying individually", exc)
                for it in chunk:
                    try:
                        hs.update(obj, it["hubspot_id"], it["properties"])
                    except HubSpotError as exc2:
                        failed += 1
                        errors.append({"event_id": it["event_id"], "hubspot_id": it["hubspot_id"], "error": str(exc2)})
                        continue
                    ok += 1
                    for eid in it.get("event_ids", [it["event_id"]]):
                        _mark(eid)
                continue
            ok += len(chunk)
            for it in chunk:
                for eid in it.get("event_ids", [it["event_id"]]):
                    _mark(eid)
            log.info("reverted %d/%d", ok + failed, len(items))
    return {"ok": ok, "failed": failed, "errors": errors}


def _mark(event_id: int) -> None:
    with db.tx() as conn:
        conn.execute("UPDATE events SET status='reverted', result=json_set(result,'$.reverted_at',?) WHERE id=?",
                     (time.time(), event_id))


def main(argv=None) -> int:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="local time 'YYYY-MM-DD HH:MM'")
    ap.add_argument("--until", default=None, help="local time 'YYYY-MM-DD HH:MM' (default now)")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--all", action="store_true", help="also revert TigerBay id stamps and is_archived")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--shared-only", action="store_true",
                    help="re-plan only HubSpot records written by several sweep events, ignoring reverted_at")
    ap.add_argument("--out", default=None, help="write the plan/result JSON here")
    args = ap.parse_args(argv)
    items = plan(_ts(args.since), _ts(args.until) if args.until else time.time(), args.all, args.shared_only)
    if args.limit:
        items = items[:args.limit]
    fields = Counter(f for it in items for f in it["properties"])
    kept = Counter(f for it in items for f in it["kept_from_webhook"])
    summary = {"records": len(items), "fields": dict(fields.most_common()), "kept_from_webhook": dict(kept),
               "per_entity": dict(Counter(it["entity"] for it in items))}
    print(json.dumps(summary, indent=1))
    for it in items[:5]:
        print("sample:", it["entity"], it["tigerbay_id"], "->", it["hubspot_id"], sorted(it["properties"]))
    if args.out:
        with open(args.out, "w") as fh:
            json.dump({"summary": summary, "items": items}, fh)
    if not args.live:
        print("DRY RUN: nothing written. Add --live to apply.")
        return 0
    res = apply(items)
    print(json.dumps({k: v for k, v in res.items() if k != "errors"}))
    for e in res["errors"][:20]:
        print("error:", e)
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
