"""Undo duplicate contacts a drift sweep created for customers that already had a
curated HubSpot contact.

    python -m app.fix_sweep_duplicates --since "2026-09-10 04:00" --report /data/drift-2026-09-10.csv
    python -m app.fix_sweep_duplicates --since "2026-09-10 04:00" --report /data/drift-2026-09-10.csv --live

For each ``source=sweep`` customer event with ``action=create`` in the window,
the drift CSV says which existing contact the reconcile had matched (by legacy
``tigerbay_id``). Live mode, per customer:

1. move the sweep-created duplicate to the HubSpot recycle bin;
2. stamp ``tigerbay_customer_id`` on the curated contact and point the local id
   map at it, so future webhooks match the curated record;
3. mark the create event ``status='reverted'``.

When the curated contact is already bound to a *sibling* TigerBay id (TigerBay
holds several customer records for one person and the reconcile matched them all
to this contact), the duplicate is still removed and the map still points at the
curated contact, but the stamp is left as it is. Anything else is skipped and
reported.
"""
import argparse
import csv
import json
import logging
import sys
import time

from app import db
from app.hubspot import HubSpotError, client

log = logging.getLogger("fixdup")


def _ts(s: str) -> float:
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M"))


def plan(since: float, until: float, report: str) -> tuple[list[dict], list[dict]]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, entity_id, result FROM events WHERE source='sweep' AND status='done' AND entity='customer'"
            " AND processed_at>=? AND processed_at<? AND json_extract(result,'$.action')='create'", (since, until)).fetchall()
    finally:
        conn.close()
    curated, siblings = {}, {}
    with open(report, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["entity"] == "customer" and r["hubspot_id"]:
                curated.setdefault(int(r["tigerbay_id"]), (r["hubspot_id"], r["match_method"]))
                siblings.setdefault(r["hubspot_id"], set()).add(str(r["tigerbay_id"]))
    hs = client()
    items, skipped = [], []
    for r in rows:
        j = json.loads(r["result"])
        tb = r["entity_id"]
        dup = str(j.get("hubspot_id") or "")
        cur = curated.get(tb)
        base = {"event_id": r["id"], "tigerbay_id": tb, "duplicate": dup}
        if not dup or not cur or cur[0] == dup:
            skipped.append({**base, "why": "no curated match in report"})
            continue
        rec = hs.get_by_id("contacts", cur[0], ["tigerbay_id", "tigerbay_customer_id", "email"])
        props = (rec or {}).get("properties") or {}
        if not rec:
            skipped.append({**base, "why": "curated contact gone"}); continue
        sib = siblings.get(cur[0], set())
        bound = str(props.get("tigerbay_customer_id") or "")
        legacy = str(props.get("tigerbay_id") or "")
        stamp = True
        if (bound and bound != str(tb)) or legacy != str(tb):
            # The curated contact is bound to another TigerBay customer id. Fine if that id is a
            # sibling record of the same person (the reconcile matched both to this contact):
            # remove the duplicate and map this id to the contact too, but leave the stamp alone.
            if (bound or legacy) in sib and str(tb) in sib:
                stamp = False
            else:
                skipped.append({**base, "why": f"curated bound to unrelated id {bound or legacy}"}); continue
        if not hs.get_by_id("contacts", dup, ["email"]):
            skipped.append({**base, "why": "duplicate already gone"}); continue
        items.append({**base, "curated": cur[0], "curated_email": props.get("email"), "match": cur[1],
                      "stamp": stamp, "sibling_of": bound or legacy if not stamp else None})
    return items, skipped


def apply(items: list[dict]) -> dict:
    hs = client()
    ok, errors = 0, []
    for it in items:
        try:
            hs.archive("contacts", it["duplicate"])
            if it["stamp"]:
                hs.update("contacts", it["curated"], {"tigerbay_customer_id": str(it["tigerbay_id"])})
        except HubSpotError as exc:
            errors.append({**it, "error": str(exc)})
            continue
        db.put_map("customer", it["tigerbay_id"], "contacts", it["curated"], it.get("curated_email"), None)
        with db.tx() as conn:
            conn.execute("UPDATE events SET status='reverted', result=json_set(result,'$.reverted_at',?,'$.duplicate_removed',1)"
                         " WHERE id=?", (time.time(), it["event_id"]))
        ok += 1
        log.info("fixed %d/%d", ok + len(errors), len(items))
    return {"ok": ok, "failed": len(errors), "errors": errors}


def main(argv=None) -> int:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", default=None)
    ap.add_argument("--report", required=True)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    items, skipped = plan(_ts(args.since), _ts(args.until) if args.until else time.time(), args.report)
    print(json.dumps({"to_fix": len(items), "stamp_curated": sum(1 for i in items if i["stamp"]),
                      "sibling_no_stamp": sum(1 for i in items if not i["stamp"]), "skipped": len(skipped)}))
    for s in skipped:
        print("skip:", s)
    for it in items[:3]:
        print("sample:", {k: it[k] for k in ("tigerbay_id", "duplicate", "curated", "match")})
    if args.out:
        with open(args.out, "w") as fh:
            json.dump({"items": items, "skipped": skipped}, fh)
    if not args.live:
        print("DRY RUN: nothing changed. Add --live to apply.")
        return 0
    res = apply(items)
    print(json.dumps({k: v for k, v in res.items() if k != "errors"}))
    for e in res["errors"]:
        print("error:", e)
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
