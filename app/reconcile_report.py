"""Read-only reconciliation report: every TigerBay customer / agent-staff record vs HubSpot.

    python -m app.reconcile_report [--out /data/reconcile.csv] [--customers-to N] [--threads 8]

Writes one CSV row per differing field (plus one row per TigerBay record with no
HubSpot match), and a .summary.json next to it. Nothing is written to HubSpot or
TigerBay. HubSpot is read once via the list endpoint into an in-memory index so the
run costs ~400 HubSpot calls regardless of size; TigerBay is walked with a thread pool.
"""
import argparse
import csv
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from app import mapping
from app.config import settings
from app.hubspot import CONTACTS, client as hs_client
from app.tigerbay import NotFound, TigerBayError, client as tb_client

log = logging.getLogger("beachsync.reconcile")

INDEX_PROPS = sorted(set(mapping.CUSTOMER_CONTACT_FIELDS + mapping.STAFF_CONTACT_FIELDS +
                         ["brand_channels", "tigerbay_customer_id", "tigerbay_agent_id", "tigerbay_id", "email"]))


class HubSpotIndex:
    def __init__(self):
        self.by_customer_id: dict[str, dict] = {}
        self.by_agent_id: dict[str, dict] = {}
        self.by_legacy_customer: dict[str, list] = {}
        self.by_email: dict[str, list] = {}
        self.total = 0

    def load(self, hs):
        after = None
        while True:
            params = {"limit": 100, "properties": ",".join(INDEX_PROPS)}
            if after:
                params["after"] = after
            data = hs.request("GET", f"/crm/v3/objects/{CONTACTS}", params=params) or {}
            for rec in data.get("results") or []:
                self.add(rec)
            after = ((data.get("paging") or {}).get("next") or {}).get("after")
            if self.total % 5000 < 100:
                log.info("hubspot index: %s contacts", self.total)
            if not after:
                break
        log.info("hubspot index loaded: %s contacts", self.total)

    def add(self, rec):
        self.total += 1
        p = rec.get("properties") or {}
        cid = mapping.normalise(p.get("tigerbay_customer_id"))
        if cid:
            self.by_customer_id[cid] = rec
        aid = mapping.normalise(p.get("tigerbay_agent_id"))
        if aid:
            self.by_agent_id[aid] = rec
        legacy = mapping.normalise(p.get("tigerbay_id"))
        if legacy and mapping.normalise(p.get("brand_channels")) != "Travel Agent":
            self.by_legacy_customer.setdefault(legacy, []).append(rec)
        email = mapping.normalise(p.get("email")).lower()
        if email:
            self.by_email.setdefault(email, []).append(rec)

    def find_customer(self, tb_id: int, desired: dict):
        rec = self.by_customer_id.get(str(tb_id))
        if rec:
            return rec, "tigerbay_customer_id"
        cands = self.by_legacy_customer.get(str(tb_id)) or []
        if len(cands) == 1:
            return cands[0], "tigerbay_id"
        if len(cands) > 1:
            return None, f"ambiguous tigerbay_id ({len(cands)} rows)"
        return self._by_email(desired, "customer", "tigerbay_customer_id", tb_id)

    def find_staff(self, tb_id: int, desired: dict):
        rec = self.by_agent_id.get(str(tb_id))
        if rec:
            return rec, "tigerbay_agent_id"
        return self._by_email(desired, "staff", "tigerbay_agent_id", tb_id)

    def _by_email(self, desired: dict, entity: str, unique_prop: str, tb_id: int):
        email = desired.get("email", "")
        if not email:
            return None, "no email"
        cands = self.by_email.get(email.lower()) or []
        if len(cands) == 1:
            cur = cands[0]["properties"]
            bound = mapping.normalise(cur.get(unique_prop))
            if bound and bound != str(tb_id):
                return None, f"shared email: bound to other id {bound}"
            channel = mapping.normalise(cur.get("brand_channels"))
            if (entity == "customer" and channel == "Travel Agent") or (entity == "staff" and channel == "Direct"):
                return None, f"shared email: HubSpot row is {channel}"
            if not mapping.names_compatible(desired, cur):
                return None, f"shared email: HubSpot name is {cur.get('firstname', '')} {cur.get('lastname', '')}".strip()
            return cands[0], "email"
        if len(cands) > 1:
            return None, f"ambiguous email ({len(cands)} rows)"
        return None, "no match"


def diff_rows(entity: str, tb_id: int, agency_id: str, desired: dict, rec: Optional[dict], how: str):
    """Rows for the CSV. would_update reflects what a backfill (preserve-email) would do."""
    name = f"{desired.get('firstname', '')} {desired.get('lastname', '')}".strip()
    base = {"entity": entity, "tigerbay_id": tb_id, "tigerbay_agency_id": agency_id, "name": name,
            "tigerbay_email": desired.get("email", ""), "match_method": how}
    if rec is None:
        if how.startswith("shared email") or how.startswith("ambiguous"):
            would = "no (skipped: " + how + ")"
        elif not desired.get("email"):
            would = "no (no email; not created)"
        else:
            would = "create"
        return [{**base, "hubspot_id": "", "hubspot_email": "", "field": "<no hubspot record>",
                 "hubspot_value": "", "tigerbay_value": "", "would_update": would}]
    cur = rec.get("properties") or {}
    base.update(hubspot_id=rec.get("id"), hubspot_email=cur.get("email") or "")
    rows = []
    for key, want in sorted(desired.items()):
        want_n, have_n = mapping.normalise(want), mapping.normalise(cur.get(key))
        if key in mapping.CASE_INSENSITIVE:
            eq = want_n.lower() == have_n.lower()
        else:
            eq = want_n == have_n
        if eq:
            continue
        if key in mapping.PHONE_FIELDS and mapping._digits(want_n) == mapping._digits(have_n):
            continue
        if want_n == "" and key in mapping.NEVER_CLEAR:
            would = "no (never clear)"
        elif key in mapping.OPT_OUT_FIELDS and have_n.lower() == "true":
            would = "no (HubSpot opt-out kept)"
        elif key == "email" and have_n:
            would = "no (existing email kept on backfill)"
        else:
            would = "yes"
        rows.append({**base, "field": key, "hubspot_value": have_n, "tigerbay_value": want_n, "would_update": would})
    return rows


def main(argv=None) -> int:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/data/reconcile.csv")
    ap.add_argument("--customers-from", type=int, default=1)
    ap.add_argument("--customers-to", type=int, default=None)
    ap.add_argument("--skip-customers", action="store_true")
    ap.add_argument("--skip-staff", action="store_true")
    ap.add_argument("--threads", type=int, default=8)
    args = ap.parse_args(argv)

    hs, tb = hs_client(), tb_client()
    index = HubSpotIndex()
    index.load(hs)

    fields = ["entity", "tigerbay_id", "tigerbay_agency_id", "hubspot_id", "match_method", "name",
              "hubspot_email", "tigerbay_email", "field", "hubspot_value", "tigerbay_value", "would_update"]
    lock = threading.Lock()
    summary = {"customers": {"checked": 0, "missing_in_tigerbay": 0, "matched": 0, "no_match": 0, "with_diffs": 0, "errors": 0},
               "staff": {"checked": 0, "matched": 0, "no_match": 0, "with_diffs": 0, "errors": 0},
               "field_diff_counts": {}, "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    seen_staff: set[int] = set()

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()

        def emit(kind: str, rows: list[dict], matched: bool):
            with lock:
                s = summary[kind]
                s["checked"] += 1
                s["matched" if matched else "no_match"] += 1
                real = [r for r in rows if r["field"] != "<no hubspot record>"]
                if real:
                    s["with_diffs"] += 1
                for r in rows:
                    summary["field_diff_counts"][r["field"]] = summary["field_diff_counts"].get(r["field"], 0) + 1
                    writer.writerow(r)
                if s["checked"] % 1000 == 0:
                    fh.flush()
                    log.info("%s: %s", kind, s)

        def do_customer(cid: int):
            try:
                profile = tb.customer_profile(cid)
            except NotFound:
                with lock:
                    summary["customers"]["missing_in_tigerbay"] += 1
                return
            except TigerBayError as exc:
                with lock:
                    summary["customers"]["errors"] += 1
                log.warning("customer %s: %s", cid, exc)
                return
            desired = mapping.map_customer(profile)
            rec, how = index.find_customer(cid, desired)
            emit("customers", diff_rows("customer", cid, "", desired, rec, how), rec is not None)

        def do_agency(agency: dict):
            aid = int(agency["ID"])
            try:
                staff = tb.agent_staff(aid)
                parent_contacts = tb.agent_contacts(aid)
            except TigerBayError as exc:
                log.warning("agency %s: %s", aid, exc)
                return
            for st in staff:
                sid = int(st["Id"])
                with lock:
                    if sid in seen_staff:
                        continue
                    seen_staff.add(sid)
                try:
                    rec_tb = tb.agent(sid)
                    rec_tb.setdefault("Email", st.get("Email"))
                    rec_tb.setdefault("CanConfirm", st.get("CanConfirm"))
                    profile = {"agent": rec_tb, "contacts": tb.agent_contacts(sid), "parent": agency,
                               "parent_contacts": parent_contacts}
                except TigerBayError as exc:
                    with lock:
                        summary["staff"]["errors"] += 1
                    log.warning("staff %s: %s", sid, exc)
                    continue
                desired = mapping.map_staff(profile)
                rec, how = index.find_staff(sid, desired)
                emit("staff", diff_rows("staff", sid, str(aid), desired, rec, how), rec is not None)

        with ThreadPoolExecutor(max_workers=args.threads) as pool:
            futures = []
            if not args.skip_customers:
                end = args.customers_to
                if end is None:
                    from app.backfill import max_customer_id
                    end = max_customer_id()
                log.info("customers %s..%s", args.customers_from, end)
                futures += [pool.submit(do_customer, cid) for cid in range(args.customers_from, end + 1)]
            if not args.skip_staff:
                agencies = tb.agents()
                log.info("agencies: %s", len(agencies))
                futures += [pool.submit(do_agency, a) for a in agencies]
            for f in as_completed(futures):
                f.result()

    summary["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    summary["hubspot_contacts_indexed"] = index.total
    with open(args.out.rsplit(".", 1)[0] + ".summary.json", "w") as fh:
        json.dump(summary, fh, indent=1)
    log.info("done: %s", json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
