"""Safety net for missed webhooks.

    python -m app.sweep new-ids     # nightly: queue TigerBay records created since the last id we saw
    python -m app.sweep drift       # daily: reconcile everything and REPORT what differs (writes nothing)
    python -m app.sweep drift --queue   # ...and queue the drifted records for re-sync (deliberate backfill only)

new-ids: TigerBay ids are sequential, so anything above the highest customer /
staff id this service has handled is a record whose 'created' webhook we never
got. Customers are probed by id; staff are found via each agency's staff list.

drift: runs the reconciliation report and records, per matched record, whether
it differs from TigerBay. It is REPORT-ONLY by default: the HubSpot data was
hand-curated and must only change through real TigerBay webhooks. On
2026-09-10 the first queued run overwrote ~14k curated records and had to be
reverted (app.revert_sweep). ``--queue`` re-enables queueing (source=sweep, so
an existing HubSpot email is preserved) for a deliberate one-off backfill.
"""
import argparse
import csv
import json
import logging
import sys
import time

from app import db
from app.config import settings
from app.tigerbay import NotFound, client as tb_client

log = logging.getLogger("beachsync.sweep")


def new_customer_ids(tb, start: int, max_gap: int = 50) -> list[int]:
    """Probe upward from start+1; stop after max_gap consecutive 404s."""
    found, misses, cid = [], 0, start
    while misses < max_gap:
        cid += 1
        try:
            tb.customer(cid)
            found.append(cid)
            misses = 0
        except NotFound:
            misses += 1
    return found


def new_staff_ids(tb, start: int) -> list[int]:
    found = []
    for agency in tb.agents():
        for st in tb.agent_staff(int(agency["ID"])):
            if int(st["Id"]) > start:
                found.append(int(st["Id"]))
    return sorted(set(found))


def sweep_new_ids() -> dict:
    tb = tb_client()
    out = {}
    last_c = db.max_seen_id("customer")
    ids = new_customer_ids(tb, last_c)
    db.enqueue_many("customer", "created", ids, source="sweep")
    out["customers"] = {"from": last_c, "queued": ids}
    last_s = db.max_seen_id("agent")
    ids = new_staff_ids(tb, last_s)
    db.enqueue_many("agent", "created", ids, source="sweep")
    out["staff"] = {"from": last_s, "queued": ids}
    return out


def sweep_drift(report_path: str, queue: bool = False) -> dict:
    from app.reconcile_report import main as report
    report(["--out", report_path, "--threads", "8"])
    to_queue = {"customer": set(), "agent": set()}
    summary: dict = {}
    try:
        with open(report_path.rsplit(".", 1)[0] + ".summary.json") as fh:
            summary = json.load(fh)
    except (OSError, ValueError):
        pass
    with open(report_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["would_update"] == "yes" and row["field"] != "<no hubspot record>":
                to_queue["customer" if row["entity"] == "customer" else "agent"].add(int(row["tigerbay_id"]))
    if queue:
        for entity, ids in to_queue.items():
            db.enqueue_many(entity, "modified", sorted(ids), source="sweep")
    return {"customer": len(to_queue["customer"]), "agent": len(to_queue["agent"]), "queued": queue,
            "report": summary, "drifted_ids" if not queue else "queued_ids": {k: sorted(v)[:500] for k, v in to_queue.items()}}


def main(argv=None) -> int:
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["new-ids", "drift"])
    ap.add_argument("--report", default=f"/data/drift-{time.strftime('%Y-%m-%d')}.csv")
    ap.add_argument("--queue", action="store_true",
                    help="drift only: queue the drifted records for re-sync (default is report-only)")
    args = ap.parse_args(argv)
    sid = db.start_sweep(args.what)
    try:
        result = sweep_new_ids() if args.what == "new-ids" else sweep_drift(args.report, queue=args.queue)
    except Exception as exc:  # noqa: BLE001
        db.finish_sweep(sid, ok=False, error=f"{type(exc).__name__}: {exc}")
        raise
    db.finish_sweep(sid, ok=True, summary=result, report_path=args.report if args.what == "drift" else None)
    print(json.dumps({k: v for k, v in result.items() if k != "report"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
