import json
import time

from fastapi.testclient import TestClient

from app import db, digest
from app.main import app


def _done(entity, eid, result, source="webhook", when=None):
    ev = db.enqueue_event(entity, "modified", eid, source=source)
    db.finish_event(ev, "done", result)
    if when is not None:
        with db.tx() as conn:
            conn.execute("UPDATE events SET processed_at=?, received_at=? WHERE id=?", (when, when, ev))
    return ev


def test_digest_rolls_up_actions_fields_and_failures():
    _done("customer", 1, {"entity": "customer", "tigerbay_id": 1, "hubspot_id": "10", "action": "update",
                          "changed": ["phone", "source_last_modified"]})
    _done("customer", 2, {"entity": "customer", "tigerbay_id": 2, "hubspot_id": "11", "action": "update", "changed": ["phone", "city"]})
    _done("customer", 3, {"entity": "customer", "tigerbay_id": 3, "hubspot_id": "12", "action": "noop", "changed": []})
    _done("agent", 40, {"entity": "staff", "tigerbay_id": 40, "hubspot_id": None, "action": "create",
                        "changed": ["email", "firstname"], "dry_run": True}, source="sweep")
    _done("agent", 50, {"entity": "agent", "tigerbay_id": 50, "action": "noop", "staff_queued": 2,
                        "staff": [{"tigerbay_id": 51, "action": "archive", "changed": ["is_archived"]}]})
    # yesterday: excluded from today's window
    _done("customer", 9, {"entity": "customer", "tigerbay_id": 9, "action": "update", "changed": ["zip"]}, when=time.time() - 2 * 86400)
    bad = db.enqueue_event("customer", "modified", 7)
    db.finish_event(bad, "failed", error="boom")

    d = digest.build(*digest.window("today"))
    assert d["totals"] == {"update": 2, "noop": 2, "create": 1, "archive": 1}
    assert d["actions"]["customer"] == {"update": 2, "noop": 1}
    assert d["actions"]["staff"] == {"create": 1, "archive": 1}
    assert d["fields_updated"] == {"phone": 2, "city": 1}          # source_last_modified stripped
    assert d["fields_created"] == {"email": 1, "firstname": 1}
    assert [r["tigerbay_id"] for r in d["records"]] == [1, 2, 40, 51]
    assert d["dry_run"] == 1
    assert [f["id"] for f in d["failures"]] == [bad]
    assert d["received"] == {"webhook": 5, "sweep": 1}

    text = digest.one_liner(d, "yesterday")
    assert text.startswith("beachsync yesterday: 1 created, 2 updated, 1 archived; 2 checked with no change (customer 2, staff 2)")
    assert "Top fields: phone 2, city 1" in text and "1 FAILED" in text and "dry-run" in text

    d7 = digest.build(*digest.window("7d"))
    assert d7["totals"]["update"] == 3


def test_digest_empty_one_liner():
    d = digest.build(*digest.window("yesterday"))
    assert digest.one_liner(d, "yesterday").startswith("beachsync yesterday: no HubSpot changes.")


def test_digest_pages():
    with TestClient(app) as client:
        assert "Nothing was changed" in client.get("/digest").text
        _done("customer", 123, {"entity": "customer", "tigerbay_id": 123, "hubspot_id": "77", "action": "update", "changed": ["mobilephone"]})
        html = client.get("/digest?window=7d").text
        assert "123" in html and "mobilephone" in html and "updated" in html and 'value="7d" selected' in html
        j = client.get("/digest.json?window=today").json()
        assert j["totals"] == {"update": 1} and "1 updated" in j["text"]
        assert client.get("/digest.json?window=nope").status_code == 400
        assert client.get("/digest?window=nope").status_code == 200  # falls back to today
