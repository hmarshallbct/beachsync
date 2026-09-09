"""Background worker: drains the event queue, one event at a time, with backoff."""
import logging
import threading
import time
from typing import Optional

from app import db
from app.config import settings
from app.hubspot import HubSpotError
from app.sync import SyncContext, sync_event
from app.tigerbay import TigerBayError

log = logging.getLogger("beachsync.worker")


def backoff(attempt: int) -> float:
    return min(3600.0, 15.0 * (2 ** max(0, attempt - 1)))


def process_one(ev: dict, ctx: Optional[SyncContext] = None) -> str:
    """Process a claimed event row. Returns the final status."""
    if ctx is None and ev.get("source") == "backfill":
        ctx = SyncContext(preserve_email=True)
    try:
        result = sync_event(ev["entity"], int(ev["entity_id"]), ev["event"], ctx)
    except (TigerBayError, HubSpotError) as exc:
        retryable = getattr(exc, "retryable", True)
        if retryable and ev["attempts"] < settings.max_attempts:
            delay = backoff(ev["attempts"])
            db.finish_event(ev["id"], "pending", error=str(exc), retry_in=delay)
            log.warning("event %s attempt %s failed (%s); retry in %.0fs", ev["id"], ev["attempts"], exc, delay)
            return "pending"
        db.finish_event(ev["id"], "failed", error=str(exc))
        log.error("event %s failed permanently: %s", ev["id"], exc)
        return "failed"
    except Exception as exc:  # noqa: BLE001 - never let the worker thread die
        log.exception("event %s crashed", ev["id"])
        if ev["attempts"] < settings.max_attempts:
            db.finish_event(ev["id"], "pending", error=f"{type(exc).__name__}: {exc}", retry_in=backoff(ev["attempts"]))
            return "pending"
        db.finish_event(ev["id"], "failed", error=f"{type(exc).__name__}: {exc}")
        return "failed"
    status = "skipped" if result.get("action") == "skipped" else "done"
    db.finish_event(ev["id"], status, result=result)
    log.info("event %s %s: %s %s -> %s (%s)", ev["id"], status, ev["entity"], ev["entity_id"],
             result.get("action"), ",".join(result.get("changed") or []) or "-")
    return status


def drain(ctx: Optional[SyncContext] = None, max_events: Optional[int] = None) -> int:
    n = 0
    while max_events is None or n < max_events:
        ev = db.claim_next_event()
        if ev is None:
            break
        process_one(ev, ctx)
        n += 1
    return n


class Worker(threading.Thread):
    def __init__(self, ctx: Optional[SyncContext] = None):
        super().__init__(name="beachsync-worker", daemon=True)
        self._stop = threading.Event()
        self._ctx = ctx
        self.last_tick = 0.0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("worker started (poll %.1fs)", settings.worker_poll_seconds)
        db.requeue_stale_processing(older_than=0)
        while not self._stop.is_set():
            self.last_tick = time.time()
            try:
                if drain(self._ctx, max_events=50) == 0:
                    self._stop.wait(settings.worker_poll_seconds)
            except Exception:  # noqa: BLE001
                log.exception("worker loop error")
                self._stop.wait(5)
