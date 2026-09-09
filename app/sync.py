"""The sync itself: fetch a TigerBay profile, diff it against HubSpot, apply.

Entry points: ``sync_customer(id, event)`` and ``sync_agent(id, event)``. Both are
idempotent — replaying an event is always safe — and return a dict describing
what happened, which is stored on the event row for auditing.
"""
import logging
import time
from typing import Optional

from app import db, mapping
from app.config import settings
from app.hubspot import (ASSOC_CONTACT_TO_COMPANY_PRIMARY, COMPANIES, CONTACTS, Conflict, HubSpotClient,
                         HubSpotError)
from app.tigerbay import NotFound, TigerBayClient

log = logging.getLogger("beachsync.sync")


class SharedEmail(Exception):
    """The TigerBay record's email belongs to a different person in HubSpot."""
    def __init__(self, hubspot_id: str, email: str):
        super().__init__(f"email {email} belongs to HubSpot contact {hubspot_id} with a different name")
        self.hubspot_id, self.email = hubspot_id, email


class SyncContext:
    def __init__(self, tb: Optional[TigerBayClient] = None, hs: Optional[HubSpotClient] = None,
                 dry_run: Optional[bool] = None, preserve_email: bool = False,
                 fanout_parent: Optional[int] = None, inline_fanout: bool = False):
        from app import hubspot, tigerbay
        self.tb = tb or tigerbay.client()
        self.hs = hs or hubspot.client()
        self.dry_run = settings.effective_dry_run() if dry_run is None else dry_run
        # Email policy (decision 2026-09-08): a TigerBay change arriving by webhook from
        # today onwards updates the HubSpot email; a backfill/reconciliation of existing
        # records leaves a differing HubSpot email alone (fills it only when empty).
        self.preserve_email = preserve_email
        # Agency events fan out to every staff member. In the worker this is done by
        # queueing one child event per staff member (so a 1,700-staff agency does not
        # block the queue for minutes); reconcile/preview keep it inline.
        self.fanout_parent = fanout_parent
        self.inline_fanout = inline_fanout


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _find_record(ctx: SyncContext, obj: str, entity: str, tb_id: int, unique_prop: Optional[str],
                 email: str, fields: list[str], legacy_filter: Optional[tuple] = None,
                 desired: Optional[dict] = None) -> Optional[dict]:
    """Locate the HubSpot record for a TigerBay entity.

    Order: local id map -> unique TigerBay-id property -> legacy ``tigerbay_id`` from the
    Aug-2026 export (search, exact match, must be unambiguous) -> email (contacts only).
    Each hit is verified with a direct GET so a stale map never causes a wrong update.
    """
    props = list(dict.fromkeys(fields + ([unique_prop] if unique_prop else []) + (["email"] if obj == CONTACTS else [])))
    m = db.get_map(entity, tb_id)
    if m:
        rec = ctx.hs.get_by_id(obj, m["hubspot_id"], props)
        if rec and not rec.get("archived"):
            return rec
        db.delete_map(entity, tb_id)
    if unique_prop:
        rec = ctx.hs.get_by_property(obj, unique_prop, str(tb_id), props)
        if rec:
            return rec
    if legacy_filter:
        prop, value, extra = legacy_filter
        rec = ctx.hs.search_one(obj, prop, value, props, extra)
        if rec:
            bound = mapping.normalise(rec.get("properties", {}).get(unique_prop)) if unique_prop else ""
            if not bound or bound == str(tb_id):
                return rec
    if obj == CONTACTS and email:
        rec = ctx.hs.get_by_property(obj, "email", email, props)
        if rec:
            # Only adopt an email match that is not already bound to a different TigerBay id.
            cur = rec.get("properties", {})
            bound = mapping.normalise(cur.get(unique_prop)) if unique_prop else ""
            if bound and bound != str(tb_id):
                log.warning("HubSpot contact %s (%s) already bound to %s=%s; not adopting for %s",
                            rec.get("id"), email, unique_prop, bound, tb_id)
                raise SharedEmail(str(rec.get("id")), email)
            # An email-only match must look like the same person, and a customer must not
            # adopt an agent-staff row (or vice versa) just because the mailbox is shared.
            channel = mapping.normalise(cur.get("brand_channels"))
            wrong_channel = (entity == "customer" and channel == "Travel Agent") or \
                            (entity == "staff" and channel == "Direct")
            if desired is not None and (wrong_channel or not mapping.names_compatible(desired, cur)):
                raise SharedEmail(str(rec.get("id")), email)
            return rec
    return None


def _apply(ctx: SyncContext, obj: str, entity: str, tb_id: int, desired: dict, existing: Optional[dict],
           create_extra: Optional[dict] = None, associations: Optional[list] = None) -> dict:
    """Create or update. Returns an audit dict with the HubSpot id and changed keys."""
    current = existing.get("properties") if existing else None
    changes = mapping.diff(desired, current)
    if ctx.preserve_email and current and mapping.normalise(current.get("email")) and "email" in changes:
        del changes["email"]
    out = {"object": obj, "entity": entity, "tigerbay_id": tb_id, "dry_run": ctx.dry_run}
    if existing:
        hs_id = str(existing["id"])
        out.update(hubspot_id=hs_id, action="update" if changes else "noop", changed=sorted(changes),
                   diff={k: {"from": (current or {}).get(k), "to": v} for k, v in sorted(changes.items())})
        if changes and not ctx.dry_run:
            changes["source_last_modified"] = _today()
            ctx.hs.update(obj, hs_id, changes)
        if not ctx.dry_run:
            db.put_map(entity, tb_id, obj, hs_id, desired.get("email"), mapping.fingerprint(desired))
        return out

    # New record
    props = {**changes, **(create_extra or {}), "source_created_date": _today(),
             "source_last_modified": _today()}
    out.update(action="create", changed=sorted(changes))
    if ctx.dry_run:
        out["hubspot_id"] = None
        out["diff"] = {k: {"from": None, "to": v} for k, v in sorted(props.items())}
        return out
    try:
        created = ctx.hs.create(obj, props, associations)
        hs_id = str(created["id"])
    except Conflict as c:
        # The email already exists (created since our lookup, or the direct lookup missed
        # it). Take the record over only if it looks like the same person.
        hs_id = c.existing_id
        rec = ctx.hs.get_by_id(obj, hs_id, ["firstname", "lastname", "brand_channels"]) or {}
        if not mapping.names_compatible(desired, rec.get("properties") or {}):
            out.update(action="skipped", hubspot_id=hs_id,
                       note=f"shared email: HubSpot contact {hs_id} has a different name")
            return out
        out["action"] = "update-after-conflict"
        ctx.hs.update(obj, hs_id, props)
    out["hubspot_id"] = hs_id
    db.put_map(entity, tb_id, obj, hs_id, desired.get("email"), mapping.fingerprint(desired))
    return out


def _archive_or_flag(ctx: SyncContext, obj: str, entity: str, tb_id: int, desired: dict,
                     existing: Optional[dict]) -> dict:
    if existing is None:
        return {"object": obj, "entity": entity, "tigerbay_id": tb_id, "action": "noop",
                "note": "archived in TigerBay; no HubSpot record to update"}
    if settings.hubspot_archive_action == "delete":
        out = {"object": obj, "entity": entity, "tigerbay_id": tb_id, "hubspot_id": str(existing["id"]),
               "action": "archive", "dry_run": ctx.dry_run}
        if not ctx.dry_run:
            ctx.hs.archive(obj, str(existing["id"]))
            db.delete_map(entity, tb_id)
        return out
    desired = {**desired, "is_archived": "TRUE"}
    return _apply(ctx, obj, entity, tb_id, desired, existing)


def _archive_missing(ctx: SyncContext, obj: str, entity: str, tb_id: int, unique_prop: str,
                     fields: list[str]) -> dict:
    """An 'archived' event whose record TigerBay no longer serves (404): flag whatever
    HubSpot record we can still identify by id, without touching any other field."""
    existing = _find_record(ctx, obj, entity, tb_id, unique_prop, "", fields)
    out = _archive_or_flag(ctx, obj, entity, tb_id, {}, existing)
    out["note"] = "record not returned by TigerBay; archived flag applied from id only"
    return out


# --- customers ---------------------------------------------------------------------

def sync_customer(customer_id: int, event: str = "modified", ctx: Optional[SyncContext] = None) -> dict:
    ctx = ctx or SyncContext()
    try:
        profile = ctx.tb.customer_profile(customer_id)
    except NotFound:
        if event == "archived":
            return _archive_missing(ctx, CONTACTS, "customer", customer_id, "tigerbay_customer_id",
                                    mapping.CUSTOMER_CONTACT_FIELDS)
        return {"entity": "customer", "tigerbay_id": customer_id, "action": "skipped",
                "note": "customer not found in TigerBay"}
    desired = mapping.map_customer(profile)
    if not desired["email"] and not (desired["firstname"] or desired["lastname"]):
        return {"entity": "customer", "tigerbay_id": customer_id, "action": "skipped",
                "note": "no email or name; HubSpot requires one"}
    # Legacy export rows: tigerbay_id == customer id, but only on non-agent rows (on
    # agent-staff rows tigerbay_id is the agency id, which could collide numerically).
    legacy = ("tigerbay_id", str(customer_id),
              [{"propertyName": "brand_channels", "operator": "NEQ", "value": "Travel Agent"}])
    try:
        existing = _find_record(ctx, CONTACTS, "customer", customer_id, "tigerbay_customer_id",
                                desired["email"], mapping.CUSTOMER_CONTACT_FIELDS, legacy, desired)
    except SharedEmail as se:
        return {"entity": "customer", "tigerbay_id": customer_id, "action": "skipped",
                "note": f"shared email: {se}", "hubspot_id": se.hubspot_id}
    if existing is None and settings.require_email_for_create and not desired["email"]:
        return {"entity": "customer", "tigerbay_id": customer_id, "action": "skipped",
                "note": "no email address; not creating a name-only contact"}
    archived = profile["customer"].get("Archived") is True or event == "archived"
    if archived:
        return _archive_or_flag(ctx, CONTACTS, "customer", customer_id, desired, existing)
    extra = dict(mapping.CUSTOMER_CREATE_DEFAULTS)
    if settings.hubspot_customer_lifecycle:
        extra["lifecyclestage"] = settings.hubspot_customer_lifecycle
    return _apply(ctx, CONTACTS, "customer", customer_id, desired, existing, create_extra=extra)


# --- agents (agencies + staff) ------------------------------------------------------

def sync_agency(agent: dict, contacts: list[dict], ctx: SyncContext, event: str = "modified") -> dict:
    tb_id = int(agent.get("ID") or agent.get("Id"))
    desired = mapping.map_agency(agent, contacts)
    existing = _find_record(ctx, COMPANIES, "agent", tb_id, None, "", mapping.AGENCY_COMPANY_FIELDS,
                            ("tigerbay_id", str(tb_id), None))
    if agent.get("IsArchived") is True or event == "archived":
        return _archive_or_flag(ctx, COMPANIES, "agent", tb_id, desired, existing)
    return _apply(ctx, COMPANIES, "agent", tb_id, desired, existing)


def sync_staff(profile: dict, ctx: SyncContext, event: str = "modified") -> dict:
    st = profile["agent"]
    tb_id = int(st.get("ID") or st.get("Id"))
    desired = mapping.map_staff(profile)
    if not desired["email"] and not (desired["firstname"] or desired["lastname"]):
        return {"entity": "staff", "tigerbay_id": tb_id, "action": "skipped",
                "note": "no email or name; HubSpot requires one"}

    company_id: Optional[str] = None
    company_result = None
    if settings.sync_agent_companies and profile.get("parent"):
        company_result = sync_agency(profile["parent"], profile.get("parent_contacts") or [], ctx)
        company_id = company_result.get("hubspot_id")

    try:
        existing = _find_record(ctx, CONTACTS, "staff", tb_id, "tigerbay_agent_id", desired["email"],
                                mapping.STAFF_CONTACT_FIELDS, None, desired)
    except SharedEmail as se:
        return {"entity": "staff", "tigerbay_id": tb_id, "action": "skipped",
                "note": f"shared email: {se}", "hubspot_id": se.hubspot_id}
    if existing is None and settings.require_email_for_create and not desired["email"]:
        return {"entity": "staff", "tigerbay_id": tb_id, "action": "skipped",
                "note": "no email address; not creating a name-only contact"}
    if st.get("IsArchived") is True or event == "archived":
        out = _archive_or_flag(ctx, CONTACTS, "staff", tb_id, desired, existing)
    else:
        extra = dict(mapping.STAFF_CREATE_DEFAULTS)
        if desired.get("agency_name"):
            extra["company"] = desired["agency_name"]
        if settings.hubspot_staff_lifecycle:
            extra["lifecyclestage"] = settings.hubspot_staff_lifecycle
        assoc = None
        if company_id and existing is None:
            assoc = [{"to": {"id": company_id}, "types": [
                {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": ASSOC_CONTACT_TO_COMPANY_PRIMARY}]}]
        out = _apply(ctx, CONTACTS, "staff", tb_id, desired, existing, create_extra=extra, associations=assoc)
        if company_id and existing is not None and not ctx.dry_run and out.get("hubspot_id"):
            try:
                ctx.hs.associate(CONTACTS, out["hubspot_id"], COMPANIES, company_id,
                                 ASSOC_CONTACT_TO_COMPANY_PRIMARY)
            except HubSpotError as exc:
                out["association_error"] = str(exc)
    if company_result:
        out["company"] = company_result
    return out


def sync_agent(agent_id: int, event: str = "modified", ctx: Optional[SyncContext] = None) -> dict:
    """Webhook target for the 'agent' entity: works for both a staff member and an agency id."""
    ctx = ctx or SyncContext()
    try:
        profile = ctx.tb.agent_profile(agent_id)
    except NotFound:
        if event == "archived":
            # Unknown whether the id was a staff member or an agency: flag the staff
            # contact with that id, and every staff contact whose agency has that id.
            out = _archive_missing(ctx, CONTACTS, "staff", agent_id, "tigerbay_agent_id",
                                   mapping.STAFF_CONTACT_FIELDS)
            out["agency_staff"] = _archive_agency_staff_in_hubspot(ctx, agent_id)
            return out
        return {"entity": "agent", "tigerbay_id": agent_id, "action": "skipped",
                "note": "agent not found in TigerBay"}
    rec = profile["agent"]
    if (rec.get("Type") or "").lower() == "staff":
        return sync_staff(profile, ctx, event)
    # An agency record: (optionally) sync the company, then every staff member under
    # it — an agency-level change (name, ABTA ref, address) is reflected on each
    # staff contact's agency_name / abta_reference / address fields.
    if settings.sync_agent_companies:
        out = sync_agency(rec, profile.get("contacts") or [], ctx, event)
    else:
        out = {"entity": "agent", "tigerbay_id": agent_id, "action": "noop",
               "note": "agency record; companies not synced (SYNC_AGENT_COMPANIES=false)"}
    # event is passed through: an agency 'archived' archives every staff contact.
    staff_ids = [int(st["Id"]) for st in ctx.tb.agent_staff(agent_id)]
    if ctx.inline_fanout or ctx.dry_run:
        staff_results = []
        for sid in staff_ids:
            try:
                staff_results.append(sync_staff(ctx.tb.agent_profile(sid), ctx, event))
            except (NotFound, HubSpotError) as exc:
                staff_results.append({"tigerbay_id": sid, "action": "error", "error": str(exc)})
        out["staff"] = staff_results
        out["staff_summary"] = {a: sum(1 for r in staff_results if r.get("action") == a)
                                for a in sorted({r.get("action") for r in staff_results})}
    else:
        db.enqueue_many("agent", event, staff_ids, source="fanout", parent_event_id=ctx.fanout_parent)
        out["staff_queued"] = len(staff_ids)
    if event == "archived" or rec.get("IsArchived") is True:
        # Staff TigerBay no longer lists under the agency (or never gave us) still get flagged.
        out["agency_staff"] = _archive_agency_staff_in_hubspot(ctx, agent_id)
    return out


def _archive_agency_staff_in_hubspot(ctx: SyncContext, agency_id: int) -> dict:
    """Flag every HubSpot agent-staff contact whose tigerbay_id (= agency id) matches."""
    rows = ctx.hs.search_all(CONTACTS, [
        {"propertyName": "tigerbay_id", "operator": "EQ", "value": str(agency_id)},
        {"propertyName": "brand_channels", "operator": "EQ", "value": "Travel Agent"},
    ], ["is_archived", "tigerbay_agent_id"])
    flagged, already = [], 0
    for row in rows:
        if mapping.normalise(row.get("properties", {}).get("is_archived")).lower() == "true":
            already += 1
            continue
        flagged.append(str(row["id"]))
        if not ctx.dry_run:
            if settings.hubspot_archive_action == "delete":
                ctx.hs.archive(CONTACTS, str(row["id"]))
            else:
                ctx.hs.update(CONTACTS, str(row["id"]), {"is_archived": "TRUE", "source_last_modified": _today()})
    return {"matched": len(rows), "flagged": len(flagged), "already_archived": already, "dry_run": ctx.dry_run}


def sync_event(entity: str, entity_id: int, event: str, ctx: Optional[SyncContext] = None) -> dict:
    if entity == "customer":
        return sync_customer(entity_id, event, ctx)
    if entity == "agent":
        return sync_agent(entity_id, event, ctx)
    raise ValueError(f"unknown entity {entity!r}")


def bootstrap_hubspot_schema(hs: Optional[HubSpotClient] = None) -> dict:
    from app import hubspot
    hs = hs or hubspot.client()
    return {
        "contacts": hs.ensure_properties(CONTACTS, mapping.CONTACT_PROPERTIES),
        "companies": hs.ensure_properties(COMPANIES, mapping.COMPANY_PROPERTIES),
    }
