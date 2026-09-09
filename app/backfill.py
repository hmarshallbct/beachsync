"""One-off / periodic full reconciliation.

    python -m app.backfill customers [--from ID] [--to ID]   # scans customer ids
    python -m app.backfill agents                            # every agency + its staff
    python -m app.backfill all

TigerBay's customer list endpoint ignores paging, so customers are walked by id
(404s are skipped). Runs are idempotent: unchanged records are no-ops in HubSpot.
Each record is queued as an event (source=backfill) and processed by the worker,
so a running service picks them up; with --inline they are processed here.
"""
import argparse
import logging
import sys

from app import db
from app.config import settings

log = logging.getLogger("beachsync.backfill")


def queue_customers(start: int, end: int) -> int:
    n = 0
    for cid in range(start, end + 1):
        db.enqueue_event("customer", "modified", cid, source="backfill")
        n += 1
    return n


def queue_agents() -> int:
    from app.tigerbay import client
    n = 0
    for a in client().agents():
        db.enqueue_event("agent", "modified", int(a["ID"]), source="backfill")
        n += 1
    return n


def max_customer_id() -> int:
    """Probe upward from the highest id in the 1000-row sample the list endpoint returns."""
    from app.tigerbay import NotFound, client
    tb = client()
    sample = tb.get("/sales/customers", params={"query": ""})
    hi = max((int(c["Id"]) for c in sample), default=0)
    step, misses = 500, 0
    probe = hi
    while misses < 3:
        probe += step
        try:
            tb.customer(probe)
            hi, misses = probe, 0
        except NotFound:
            misses += 1
    return hi + step * 3


def main(argv=None) -> int:
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("what", choices=["customers", "agents", "all"])
    p.add_argument("--from", dest="start", type=int, default=1)
    p.add_argument("--to", dest="end", type=int, default=None)
    p.add_argument("--inline", action="store_true", help="process here instead of leaving to the worker")
    args = p.parse_args(argv)
    total = 0
    if args.what in ("customers", "all"):
        end = args.end or max_customer_id()
        total += queue_customers(args.start, end)
        log.info("queued customers %s..%s", args.start, end)
    if args.what in ("agents", "all"):
        total += queue_agents()
    log.info("queued %s events", total)
    if args.inline:
        from app.worker import drain
        log.info("processed %s events", drain())
    return 0


if __name__ == "__main__":
    sys.exit(main())
