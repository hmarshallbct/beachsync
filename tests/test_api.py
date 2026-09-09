import base64

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def basic(u="tb", p="secret"):
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}


def test_webhook_requires_auth(client):
    assert client.post("/webhooks/tigerbay/customer/created", json={"Id": 1}).status_code == 401
    assert client.post("/webhooks/tigerbay/customer/created", json={"Id": 1}, headers=basic("tb", "no")).status_code == 401


def test_webhook_basic_auth_queues_event(client):
    r = client.post("/webhooks/tigerbay/customer/created", json={"Id": 22926}, headers=basic())
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["entity"] == "customer" and body["event"] == "created" and body["id"] == 22926
    ev = db.get_event(body["event_id"])
    assert ev["status"] == "pending" and ev["entity_id"] == 22926


def test_webhook_header_auth_and_body_event(client):
    r = client.post("/webhooks/tigerbay/agent", json={"EventType": "Archived", "AgentStaffId": 27610},
                    headers={"X-Webhook-Token": "hdr-token"})
    assert r.status_code == 202
    assert r.json()["event"] == "archived" and r.json()["id"] == 27610


def test_webhook_unparseable_is_stored_and_400(client):
    r = client.post("/webhooks/tigerbay/customer/modified", content=b"hello", headers=basic())
    assert r.status_code == 400
    ev = db.get_event(r.json()["event_id"])
    assert ev["status"] == "unparsed" and ev["raw_body"] == "hello"


def test_webhook_get_probe(client):
    assert client.get("/webhooks/tigerbay/customer/created", headers=basic()).status_code == 200


def test_health_and_admin(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/admin/events").status_code == 401
    r = client.post("/admin/sync/customer/5", headers={"X-Admin-Token": "admin-token"})
    assert r.status_code == 200
    evs = client.get("/admin/events", headers={"X-Admin-Token": "admin-token"}).json()["events"]
    assert evs[0]["source"] == "replay" and evs[0]["entity_id"] == 5


def test_admin_dismiss(client):
    h = {"X-Admin-Token": "admin-token"}
    r = client.post("/webhooks/tigerbay/customer/modified", content=b"garbage", headers=basic())
    eid = r.json()["event_id"]
    assert client.post(f"/admin/events/{eid}/dismiss", headers=h).status_code == 200
    assert db.get_event(eid)["status"] == "dismissed"
    assert client.post(f"/admin/events/{eid}/dismiss", headers=h).status_code == 409


def test_status_page(client):
    client.post("/webhooks/tigerbay/customer/created", json={"Id": 22926}, headers=basic())
    r = client.get("/status")
    assert r.status_code == 200 and "beachsync" in r.text and "22926" not in r.text   # events moved to /events
    ev = client.get("/events").text
    assert "22926" in ev and "pending" in ev
    assert "22926" not in client.get("/events?status=done").text
    assert "22926" in client.get("/events?source=webhook").text
    assert client.get("/status.json").json()["counts"]["pending"] == 1


def test_kill_switch(client, tb, hs, monkeypatch):
    from app import worker as w
    from app.sync import SyncContext
    ctx = SyncContext(tb=tb, hs=hs, dry_run=False, inline_fanout=True)
    tb.add_customer(1)
    # pause: no auth, reason stored, queue still accepts and nothing is processed
    r = client.post("/status/pause", data={"reason": "testing"}, follow_redirects=False)
    assert r.status_code == 303 and db.paused()["value"] == "testing"
    assert client.post("/webhooks/tigerbay/customer/created", json={"Id": 1}, headers=basic()).status_code == 202
    assert w.drain(ctx) == 0 and db.list_events()[0]["status"] == "pending"
    assert "Sync is paused" in client.get("/status").text
    assert client.get("/health").json()["paused"]["value"] == "testing"
    # resume needs the admin token
    r = client.post("/status/resume", data={"token": "wrong"}, follow_redirects=False)
    assert r.headers["location"].endswith("resume=denied") and db.paused()
    r = client.post("/status/resume", data={"token": "admin-token"}, follow_redirects=False)
    assert r.status_code == 303 and db.paused() is None
    assert w.drain(ctx) == 1 and db.list_events()[0]["status"] == "done"
