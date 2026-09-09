import os
import sys

os.environ.setdefault("DB_PATH", "")  # overridden per-test
os.environ["WEBHOOK_BASIC_USER"] = "tb"
os.environ["WEBHOOK_BASIC_PASSWORD"] = "secret"
os.environ["WEBHOOK_HEADER_VALUE"] = "hdr-token"
os.environ["ADMIN_TOKEN"] = "admin-token"
os.environ["WORKER_ENABLED"] = "false"
os.environ["HUBSPOT_ACCESS_TOKEN"] = ""
os.environ["TIGERBAY_BASE_URL"] = "https://tb.invalid/nimble"
os.environ["TIGERBAY_CLIENT_ID"] = "x"
os.environ["TIGERBAY_CLIENT_SECRET"] = "y"

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from app import config  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    monkeypatch.setattr(config.settings, "db_path", path)
    yield path


class FakeTigerBay:
    """In-memory TigerBay with the real resource shapes."""

    def __init__(self):
        self.customers = {}
        self.customer_contacts = {}
        self.agents = {}
        self.agent_contacts = {}
        self.calls = []

    def add_customer(self, cid, contacts=None, **over):
        rec = {"Id": cid, "BundleReference": f"{cid}-1110", "Title": "Mrs", "Forename": "Jane",
               "Surname": "Doe", "Gender": "Female", "Username": "u", "EmailAddress": f"jane{cid}@example.com",
               "DateOfBirth": "1985-07-03T10:03:00", "Tags": "", "AgentId": 0, "DoNotEmail": False,
               "DoNotMail": False, "Reference": str(cid), "ExternalReference": "", "TypeId": 0,
               "Deceased": False, "Archived": False, "Links": []}
        rec.update(over)
        self.customers[cid] = rec
        self.customer_contacts[cid] = contacts if contacts is not None else [{
            "Id": 1, "Title": "Mrs", "Forename": "Jane", "Surname": "Doe", "Address0": "1 High St",
            "Address1": "", "Address2": "", "Address3": "", "TownCity": "Bath", "County": "Somerset",
            "PostCode": "BA1 1AA", "Country": "GBR", "PersonalMobile": "07700900001",
            "PersonalLandline": "01225000000", "PersonalEmail": f"jane{cid}@example.com",
            "BusinessLandline": "", "BusinessMobile": "", "BusinessEmail": "", "Type": "Primary"}]
        return rec

    def add_agency(self, aid, name="Best Travel", contacts=None, **over):
        rec = {"ID": aid, "BundleReference": f"{aid}-1109", "Name": name, "Reference": f"P{aid}",
               "IsArchived": False, "CommissionMode": 0, "BillingType": "Credit", "Type": "Agent",
               "VatNumber": "GB123", "Tags": "", "DefaultCurrencyCode": "GBP", "ExternalReference": "",
               "GroupId": 63, "Links": []}
        rec.update(over)
        self.agents[aid] = rec
        self.agent_contacts[aid] = contacts or [{
            "Id": 9, "Title": "", "Forename": "", "Surname": "", "Address0": "Unit 2", "Address1": "Park Rd",
            "Address2": "", "Address3": "", "TownCity": "Leeds", "County": "", "PostCode": "LS1 1AA",
            "Country": "GBR", "PersonalMobile": "", "PersonalLandline": "", "PersonalEmail": "",
            "BusinessLandline": "01130000000", "BusinessMobile": "", "BusinessEmail": "info@best.example",
            "Type": "Primary"}]
        return rec

    def add_staff(self, sid, agency_id, name="Mike Brown", email="mike@best.example", **over):
        rec = {"ID": sid, "BundleReference": f"{sid}-1109", "Name": name, "Reference": email,
               "IsArchived": False, "CommissionMode": 0, "BillingType": "NotSet", "Type": "Staff",
               "VatNumber": "", "Tags": "", "DefaultCurrencyCode": "XXX", "ExternalReference": "",
               "GroupId": agency_id, "Links": [], "Email": email, "CanConfirm": True}
        rec.update(over)
        self.agents[sid] = rec
        self.agent_contacts.setdefault(sid, [])
        return rec

    # --- API surface used by app.sync --------------------------------------
    def _nf(self, what):
        from app.tigerbay import NotFound
        raise NotFound(what)

    def customer(self, cid):
        self.calls.append(("customer", cid))
        return self.customers.get(cid) or self._nf(f"customer {cid}")

    def customer_contacts_(self, cid):
        return self.customer_contacts.get(cid, [])

    def customer_profile(self, cid):
        return {"customer": self.customer(cid), "contacts": self.customer_contacts.get(cid, [])}

    def agent(self, aid):
        self.calls.append(("agent", aid))
        return self.agents.get(aid) or self._nf(f"agent {aid}")

    def agent_contacts_(self, aid):
        return self.agent_contacts.get(aid, [])

    def agent_staff(self, aid):
        return [{"Id": a["ID"], "Name": a["Name"], "Reference": a["Reference"], "Email": a.get("Email"),
                 "AgentId": aid, "CanConfirm": a.get("CanConfirm", False)}
                for a in self.agents.values() if a["Type"] == "Staff" and a["GroupId"] == aid]

    def agents_(self):
        return [a for a in self.agents.values() if a["Type"] == "Agent"]

    def agent_profile(self, aid):
        rec = self.agent(aid)
        out = {"agent": rec, "contacts": self.agent_contacts.get(aid, []), "parent": None, "parent_contacts": []}
        if rec["Type"] == "Staff" and rec.get("GroupId") in self.agents:
            out["parent"] = self.agents[rec["GroupId"]]
            out["parent_contacts"] = self.agent_contacts.get(rec["GroupId"], [])
        return out


class FakeHubSpot:
    """In-memory HubSpot: contacts/companies keyed by id, unique props and email enforced."""

    def __init__(self):
        self.objects = {"contacts": {}, "companies": {}}
        self.next_id = 1000
        self.associations = []
        self.props_created = {"contacts": [], "companies": []}
        self.log = []

    def _find(self, obj, prop, value):
        for hid, rec in self.objects[obj].items():
            if str(rec["properties"].get(prop, "")).lower() == str(value).lower() and not rec["archived"]:
                return rec
        return None

    def get_by_property(self, obj, id_property, value, properties):
        self.log.append(("get_by_property", obj, id_property, value))
        return self._find(obj, id_property, value)

    def search_one(self, obj, prop, value, properties, extra_filters=None):
        self.log.append(("search_one", obj, prop, value))
        hits = []
        for rec in self.objects[obj].values():
            if rec["archived"] or str(rec["properties"].get(prop, "")) != str(value):
                continue
            ok = True
            for f in extra_filters or []:
                have = str(rec["properties"].get(f["propertyName"], ""))
                if f["operator"] == "NEQ" and have == f["value"]:
                    ok = False
            if ok:
                hits.append(rec)
        return hits[0] if len(hits) == 1 else None

    def search_all(self, obj, filters, properties, max_results=10000):
        self.log.append(("search_all", obj, filters))
        hits = []
        for rec in self.objects[obj].values():
            if rec["archived"]:
                continue
            if all(str(rec["properties"].get(f["propertyName"], "")) == str(f["value"]) for f in filters if f["operator"] == "EQ"):
                hits.append(rec)
        return hits

    def get_by_id(self, obj, hs_id, properties):
        self.log.append(("get_by_id", obj, hs_id))
        return self.objects[obj].get(str(hs_id))

    def create(self, obj, properties, associations=None):
        from app.hubspot import Conflict
        self.log.append(("create", obj, dict(properties)))
        if obj == "contacts" and properties.get("email"):
            ex = self._find(obj, "email", properties["email"])
            if ex:
                raise Conflict(ex["id"])
        hid = str(self.next_id)
        self.next_id += 1
        self.objects[obj][hid] = {"id": hid, "properties": dict(properties), "archived": False}
        for a in associations or []:
            self.associations.append((obj, hid, a["to"]["id"]))
        return self.objects[obj][hid]

    def update(self, obj, hs_id, properties):
        self.log.append(("update", obj, hs_id, dict(properties)))
        self.objects[obj][str(hs_id)]["properties"].update(properties)
        return self.objects[obj][str(hs_id)]

    def archive(self, obj, hs_id):
        self.log.append(("archive", obj, hs_id))
        self.objects[obj][str(hs_id)]["archived"] = True

    def associate(self, from_obj, from_id, to_obj, to_id, type_id):
        self.log.append(("associate", from_obj, from_id, to_obj, to_id))
        if (from_obj, from_id, to_id) not in self.associations:
            self.associations.append((from_obj, from_id, to_id))

    def ensure_properties(self, obj, wanted, group=None):
        names = [w["name"] for w in wanted]
        self.props_created[obj] = names
        return names


@pytest.fixture
def tb():
    return FakeTigerBay()


@pytest.fixture
def hs():
    return FakeHubSpot()


@pytest.fixture
def ctx(tb, hs):
    from app.sync import SyncContext
    return SyncContext(tb=tb, hs=hs, dry_run=False, inline_fanout=True)
