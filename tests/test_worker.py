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


def test_supervisor_restarts_dead_worker(monkeypatch):
    from app.worker import Supervisor, Worker
    sup = Supervisor()
    sup.worker = Worker()          # never started => is_alive() False, like a crashed thread
    started = []
    monkeypatch.setattr(Worker, "start", lambda self: started.append(self))
    monkeypatch.setattr(sup._stop, "wait", lambda t: len(started) >= 2)   # stop after one restart
    sup.run()
    assert len(started) == 2       # initial start + one restart
    assert sup.restarts and sup.worker is started[-1]


def test_agency_event_fans_out_to_child_events_and_dedupes(tb, hs):
    from app.sync import SyncContext
    ctx = SyncContext(tb=tb, hs=hs, dry_run=False)          # worker-style: fan-out via queue
    tb.add_agency(500)
    tb.add_staff(501, 500, email="a@best.example")
    tb.add_staff(502, 500, name="Sue Green", email="b@best.example")
    # TigerBay sends every event twice
    db.enqueue_event("agent", "modified", 500)
    db.enqueue_event("agent", "modified", 500)
    assert drain(ctx, max_events=1) == 1
    evs = {e["id"]: e for e in db.list_events()}
    parent = min(evs)
    assert evs[parent]["status"] == "done" and '"staff_queued": 2' in evs[parent]["result"]
    assert evs[parent + 1]["status"] == "superseded"
    children = [e for e in evs.values() if e["source"] == "fanout"]
    assert len(children) == 2 and all(e["parent_event_id"] == parent for e in children)
    drain(ctx)
    assert len(hs.objects["contacts"]) == 2
    assert all(e["status"] == "done" for e in db.list_events() if e["source"] == "fanout")


def test_archived_event_is_never_superseded(tb, hs, ctx):
    tb.add_customer(1)
    db.enqueue_event("customer", "modified", 1)
    db.enqueue_event("customer", "archived", 1)
    drain(ctx, max_events=1)
    statuses = {e["event"]: e["status"] for e in db.list_events()}
    assert statuses == {"modified": "done", "archived": "pending"}


def test_bulk_retry_failed(tb, hs, ctx, monkeypatch):
    tb.add_customer(1)
    monkeypatch.setattr(hs, "get_by_property", lambda *a, **k: (_ for _ in ()).throw(HubSpotError("x", status=401, retryable=False)))
    db.enqueue_event("customer", "created", 1); db.enqueue_event("customer", "created", 1)
    drain(ctx)
    assert db.retry_failed() == 2 and all(e["status"] == "pending" for e in db.list_events())
