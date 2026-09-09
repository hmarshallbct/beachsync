from app import db
from app.config import settings
from app.sync import sync_agent, sync_customer


def test_customer_create_then_noop_then_update(tb, hs, ctx):
    tb.add_customer(1000)
    r = sync_customer(1000, "created", ctx)
    assert r["action"] == "create"
    hid = r["hubspot_id"]
    rec = hs.objects["contacts"][hid]["properties"]
    assert rec["email"] == "jane1000@example.com" and rec["tigerbay_customer_id"] == "1000"
    assert rec["tigerbay_id"] == "1000" and rec["brand_channels"] == "Direct"
    assert rec["original_data_source"] == "Tigerbay" and rec["lifecyclestage"] == "lead"
    assert "source_created_date" in rec and "source_last_modified" in rec
    assert db.get_map("customer", 1000)["hubspot_id"] == hid

    r2 = sync_customer(1000, "modified", ctx)
    assert r2["action"] == "noop" and r2["hubspot_id"] == hid

    tb.customers[1000]["Surname"] = "Smith"
    tb.customers[1000]["DoNotEmail"] = True
    r3 = sync_customer(1000, "modified", ctx)
    assert r3["action"] == "update" and set(r3["changed"]) == {"lastname", "cancel_from_email"}
    assert hs.objects["contacts"][hid]["properties"]["lastname"] == "Smith"
    # only the changed properties were sent, brand_channels/lifecycle never re-sent
    last = [c for c in hs.log if c[0] == "update"][-1]
    assert set(last[3]) == {"lastname", "cancel_from_email", "source_last_modified"}


def test_customer_matches_existing_hubspot_contact_by_email(tb, hs, ctx):
    tb.add_customer(5)
    hs.create("contacts", {"email": "jane5@example.com", "firstname": "J", "phone": "999"})
    r = sync_customer(5, "created", ctx)
    assert r["action"] == "update"
    p = hs.objects["contacts"][r["hubspot_id"]]["properties"]
    assert p["tigerbay_customer_id"] == "5" and p["firstname"] == "Jane"
    assert p["phone"] == "01225000000"  # HubSpot had '999' (digits differ) so TigerBay wins


def test_customer_matches_legacy_export_row_by_tigerbay_id(tb, hs, ctx):
    """Rows from the Aug-2026 export carry tigerbay_id but no unique key, and may have a stale email."""
    tb.add_customer(211, EmailAddress="new@example.com")
    hs.create("contacts", {"email": "old@example.com", "firstname": "Philip", "tigerbay_id": "211",
                           "brand_channels": "Direct"})
    # an agent-staff row with the same number as its AGENCY id must not be confused with it
    hs.create("contacts", {"email": "staff@agency.example", "tigerbay_id": "211", "brand_channels": "Travel Agent"})
    r = sync_customer(211, "modified", ctx)
    assert r["action"] == "update"
    p = hs.objects["contacts"][r["hubspot_id"]]["properties"]
    assert p["email"] == "new@example.com" and p["tigerbay_customer_id"] == "211" and p["firstname"] == "Jane"
    assert hs.objects["contacts"]["1001"]["properties"]["email"] == "staff@agency.example"


def test_customer_does_not_adopt_contact_bound_to_other_id(tb, hs, ctx):
    tb.add_customer(6, EmailAddress="shared@example.com")
    hs.create("contacts", {"email": "shared@example.com", "tigerbay_customer_id": "999", "lastname": "Other"})
    r = sync_customer(6, "created", ctx)
    assert r["action"] == "skipped" and "shared email" in r["note"]
    assert hs.objects["contacts"]["1000"]["properties"]["lastname"] == "Other"


def test_shared_mailbox_different_person_is_skipped(tb, hs, ctx):
    tb.add_customer(524, Forename="Leslie", Surname="Barker", EmailAddress="sarcher@bctuk.com")
    hs.create("contacts", {"email": "sarcher@bctuk.com", "firstname": "Sarah", "lastname": "Archer"})
    r = sync_customer(524, "modified", ctx)
    assert r["action"] == "skipped" and r["hubspot_id"] == "1000"
    assert hs.objects["contacts"]["1000"]["properties"]["firstname"] == "Sarah"


def test_customer_does_not_adopt_travel_agent_row(tb, hs, ctx):
    tb.add_customer(27409, Forename="Natalie", Surname="Taylor", EmailAddress="natalie@hays.example")
    hs.create("contacts", {"email": "natalie@hays.example", "firstname": "Natalie", "lastname": "Taylor",
                           "brand_channels": "Travel Agent", "tigerbay_id": "1397"})
    r = sync_customer(27409, "modified", ctx)
    assert r["action"] == "skipped"
    assert hs.objects["contacts"]["1000"]["properties"]["tigerbay_id"] == "1397"


def test_no_email_not_created_but_existing_updated(tb, hs, ctx):
    tb.add_customer(9, EmailAddress="", contacts=[])
    assert sync_customer(9, "created", ctx)["action"] == "skipped"
    hs.create("contacts", {"firstname": "Old", "lastname": "Name", "tigerbay_customer_id": "9"})
    assert sync_customer(9, "modified", ctx)["action"] == "update"


def test_customer_archived_flags_not_deletes(tb, hs, ctx, monkeypatch):
    tb.add_customer(7)
    r = sync_customer(7, "created", ctx)
    tb.customers[7]["Archived"] = True
    r2 = sync_customer(7, "archived", ctx)
    assert r2["action"] == "update"
    assert hs.objects["contacts"][r["hubspot_id"]]["properties"]["is_archived"] == "TRUE"
    assert not hs.objects["contacts"][r["hubspot_id"]]["archived"]

    monkeypatch.setattr(settings, "hubspot_archive_action", "delete")
    r3 = sync_customer(7, "archived", ctx)
    assert r3["action"] == "archive"
    assert hs.objects["contacts"][r["hubspot_id"]]["archived"]
    assert db.get_map("customer", 7) is None


def test_customer_missing_is_skipped(tb, hs, ctx):
    assert sync_customer(404, "modified", ctx)["action"] == "skipped"


def test_staff_creates_company_and_associated_contact(tb, hs, ctx, monkeypatch):
    monkeypatch.setattr(settings, "sync_agent_companies", True)
    tb.add_agency(500, name="Best Travel")
    tb.add_staff(501, 500)
    r = sync_agent(501, "created", ctx)
    assert r["action"] == "create" and r["company"]["action"] == "create"
    cid = r["company"]["hubspot_id"]
    assert hs.objects["companies"][cid]["properties"]["tigerbay_id"] == "500"
    assert ("contacts", r["hubspot_id"], cid) in hs.associations
    p = hs.objects["contacts"][r["hubspot_id"]]["properties"]
    assert p["tigerbay_agent_id"] == "501" and p["company"] == "Best Travel"
    assert p["tigerbay_id"] == "500" and p["agency_name"] == "Best Travel" and p["brand_channels"] == "Travel Agent"

    # second run: everything is a no-op, association re-asserted idempotently
    r2 = sync_agent(501, "modified", ctx)
    assert r2["action"] == "noop" and r2["company"]["action"] == "noop"


def test_staff_default_no_company_matches_legacy_row_by_email(tb, hs, ctx):
    tb.add_agency(500, name="Best Travel")
    tb.add_staff(501, 500, email="mike@best.example")
    hs.create("contacts", {"email": "mike@best.example", "tigerbay_id": "500", "brand_channels": "Travel Agent",
                           "agency_name": "Best Travel"})
    r = sync_agent(501, "modified", ctx)
    assert r["action"] == "update" and "company" not in r and not hs.objects["companies"]
    p = hs.objects["contacts"][r["hubspot_id"]]["properties"]
    assert p["tigerbay_agent_id"] == "501" and p["abta_reference"] == "P500"


def test_agency_event_syncs_company_and_all_staff(tb, hs, ctx, monkeypatch):
    monkeypatch.setattr(settings, "sync_agent_companies", True)
    tb.add_agency(500)
    tb.add_staff(501, 500, email="a@best.example")
    tb.add_staff(502, 500, name="Sue Green", email="b@best.example")
    r = sync_agent(500, "modified", ctx)
    assert r["object"] == "companies" and r["action"] == "create"
    assert [s["action"] for s in r["staff"]] == ["create", "create"]
    assert len(hs.objects["contacts"]) == 2


def test_agency_event_without_companies_still_syncs_staff(tb, hs, ctx):
    tb.add_agency(500)
    tb.add_staff(501, 500, email="a@best.example")
    r = sync_agent(500, "modified", ctx)
    assert r["action"] == "noop" and not hs.objects["companies"]
    assert r["staff_summary"] == {"create": 1} and len(hs.objects["contacts"]) == 1


def test_dry_run_writes_nothing(tb, hs):
    from app.sync import SyncContext
    tb.add_customer(1)
    r = sync_customer(1, "created", SyncContext(tb=tb, hs=hs, dry_run=True))
    assert r["action"] == "create" and r["dry_run"] and not hs.objects["contacts"]
    assert db.get_map("customer", 1) is None


def test_stale_map_is_reverified(tb, hs, ctx):
    tb.add_customer(8)
    db.put_map("customer", 8, "contacts", "does-not-exist", None, None)
    r = sync_customer(8, "modified", ctx)
    assert r["action"] == "create"
    assert db.get_map("customer", 8)["hubspot_id"] == r["hubspot_id"]


def test_staff_archived_flag_from_record(tb, hs, ctx):
    tb.add_agency(500)
    tb.add_staff(501, 500, email="a@best.example")
    r = sync_agent(501, "created", ctx)
    tb.agents[501]["IsArchived"] = True
    r2 = sync_agent(501, "modified", ctx)   # flag honoured even without an 'archived' event
    assert r2["action"] == "update"
    assert hs.objects["contacts"][r["hubspot_id"]]["properties"]["is_archived"] == "TRUE"
    assert hs.objects["contacts"][r["hubspot_id"]]["properties"]["email"] == "a@best.example"


def test_staff_archived_event_when_tigerbay_no_longer_serves_record(tb, hs, ctx):
    tb.add_agency(500)
    tb.add_staff(501, 500, email="a@best.example")
    r = sync_agent(501, "created", ctx)
    del tb.agents[501]  # TigerBay hides archived staff entirely
    r2 = sync_agent(501, "archived", ctx)
    assert r2["action"] == "update" and r2["changed"] == ["is_archived"]
    assert hs.objects["contacts"][r["hubspot_id"]]["properties"]["is_archived"] == "TRUE"
    # nothing else touched
    assert hs.objects["contacts"][r["hubspot_id"]]["properties"]["firstname"] == "Mike"


def test_customer_archived_event_when_missing_and_unknown(tb, hs, ctx):
    r = sync_customer(4242, "archived", ctx)
    assert r["action"] == "noop"


def test_agency_archived_event_archives_all_staff(tb, hs, ctx):
    tb.add_agency(500)
    tb.add_staff(501, 500, email="a@best.example")
    tb.add_staff(502, 500, name="Sue Green", email="b@best.example")
    sync_agent(500, "modified", ctx)
    assert all(c["properties"]["is_archived"] == "FALSE" for c in hs.objects["contacts"].values())
    # a legacy export row for the same agency that TigerBay's staff list no longer includes
    hs.create("contacts", {"email": "old@best.example", "tigerbay_id": "500", "brand_channels": "Travel Agent"})
    tb.agents[500]["IsArchived"] = True
    r = sync_agent(500, "archived", ctx)
    assert r["staff_summary"] == {"update": 2}
    assert r["agency_staff"]["flagged"] == 1 and r["agency_staff"]["already_archived"] == 2
    assert all(c["properties"]["is_archived"] == "TRUE" for c in hs.objects["contacts"].values())


def test_agency_archived_flag_propagates_on_staff_modified(tb, hs, ctx):
    tb.add_agency(500, IsArchived=True)
    tb.add_staff(501, 500, email="a@best.example")
    r = sync_agent(501, "modified", ctx)
    assert hs.objects["contacts"][r["hubspot_id"]]["properties"]["is_archived"] == "TRUE"


def test_agency_archived_event_when_agency_vanished(tb, hs, ctx):
    hs.create("contacts", {"email": "x@best.example", "tigerbay_id": "500", "brand_channels": "Travel Agent"})
    hs.create("contacts", {"email": "cust@example.com", "tigerbay_id": "500", "brand_channels": "Direct"})
    r = sync_agent(500, "archived", ctx)
    assert r["agency_staff"]["flagged"] == 1
    assert hs.objects["contacts"]["1000"]["properties"]["is_archived"] == "TRUE"
    assert "is_archived" not in hs.objects["contacts"]["1001"]["properties"]  # customer with same number untouched


def test_backfill_preserves_existing_email_but_fills_empty(tb, hs):
    from app.sync import SyncContext
    ctx = SyncContext(tb=tb, hs=hs, dry_run=False, preserve_email=True)
    tb.add_customer(211, EmailAddress="new@example.com")
    hs.create("contacts", {"email": "old@example.com", "tigerbay_id": "211", "brand_channels": "Direct"})
    r = sync_customer(211, "modified", ctx)
    assert "email" not in r["changed"]
    assert hs.objects["contacts"][r["hubspot_id"]]["properties"]["email"] == "old@example.com"
    tb.add_customer(212, EmailAddress="fill@example.com")
    hs.create("contacts", {"firstname": "x", "tigerbay_id": "212", "brand_channels": "Direct"})
    r = sync_customer(212, "modified", ctx)
    assert "email" in r["changed"]
