from app import db
from app.config import settings
from app.hubspot import HubSpotError
from app.worker import drain


def test_worker_processes_and_records_result(tb, hs, ctx):
    tb.add_customer(1)
    db.enqueue_event("customer", "created", 1)
    assert drain(ctx) == 1
    ev = db.list_events()[0]
    assert ev["status"] == "done" and '"action": "create"' in ev["result"]


def test_worker_retries_then_fails(tb, hs, ctx, monkeypatch):
    tb.add_customer(1)

    def boom(*a, **k):
        raise HubSpotError("HubSpot down", status=503, retryable=True)
    monkeypatch.setattr(hs, "get_by_property", boom)
    monkeypatch.setattr(settings, "max_attempts", 2)
    db.enqueue_event("customer", "created", 1)
    drain(ctx)
    ev = db.list_events()[0]
    assert ev["status"] == "pending" and ev["attempts"] == 1 and ev["next_attempt_at"] > 0
    # force it due again
    with db.tx() as conn:
        conn.execute("UPDATE events SET next_attempt_at=0")
    drain(ctx)
    ev = db.list_events()[0]
    assert ev["status"] == "failed" and "HubSpot down" in ev["last_error"]
    assert db.retry_event(ev["id"])
    assert db.list_events()[0]["status"] == "pending"


def test_non_retryable_fails_immediately(tb, hs, ctx, monkeypatch):
    tb.add_customer(1)

    def boom(*a, **k):
        raise HubSpotError("bad token", status=401, retryable=False)
    monkeypatch.setattr(hs, "get_by_property", boom)
    db.enqueue_event("customer", "created", 1)
    drain(ctx)
    assert db.list_events()[0]["status"] == "failed"


def test_skipped_status(tb, hs, ctx):
    db.enqueue_event("customer", "modified", 999)
    drain(ctx)
    assert db.list_events()[0]["status"] == "skipped"
