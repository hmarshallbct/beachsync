from app import db
from app.sweep import new_customer_ids, new_staff_ids


def test_new_customer_ids_probes_past_gaps(tb):
    for cid in (100, 101, 103, 110):
        tb.add_customer(cid)
    assert new_customer_ids(tb, 100, max_gap=5) == [101, 103]      # 110 is beyond a 5-gap
    assert new_customer_ids(tb, 100, max_gap=10) == [101, 103, 110]


def test_new_staff_ids_above_watermark(tb):
    tb.add_agency(500); tb.add_staff(501, 500); tb.add_staff(507, 500)

    class TB:  # adapter: the fake stores agencies in a dict called `agents`, the client has a method
        def agents(self):
            return [a for a in tb.agents.values() if a["Type"] == "Agent"]

        def agent_staff(self, aid):
            return tb.agent_staff(aid)

    assert new_staff_ids(TB(), 501) == [507]


def test_max_seen_id():
    db.enqueue_event("customer", "created", 31756)
    db.put_map("staff", 28585, "contacts", "1", None, None)
    assert db.max_seen_id("customer") == 31756 and db.max_seen_id("agent") == 28585
