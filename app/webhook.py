"""Inbound webhook parsing: turn whatever TigerBay POSTs into (entity, event, id).

TigerBay's webhook payload format is not publicly documented, so this is
deliberately tolerant: the entity comes from the URL we give TigerBay, the event
from the URL or the body, and the id is searched for under a range of likely
keys (including nested objects), then the query string, then the URL.
Unparseable deliveries are stored (status ``unparsed``) so they can be inspected
via the admin API and the parser tightened.
"""
import json
import re
from typing import Any, Optional

ENTITIES = {"customer", "client", "customers", "clients", "agent", "agents", "staff", "agentstaff", "agent-staff"}
EVENTS = {"created", "create", "new", "modified", "modify", "updated", "update", "changed",
          "archived", "archive", "deleted", "delete", "unknown"}

ID_KEYS = ("EntityId", "entityId", "entity_id", "ObjectId", "objectId", "CustomerId", "customerId",
           "AgentId", "agentId", "AgentStaffId", "agentStaffId", "StaffId", "staffId", "RecordId",
           "recordId", "Id", "ID", "id", "ItemId", "itemId", "Key", "key")
EVENT_KEYS = ("EventType", "eventType", "event_type", "Event", "event", "Action", "action",
              "Type", "type", "Operation", "operation", "ChangeType", "changeType")
ENTITY_KEYS = ("EntityType", "entityType", "entity_type", "ObjectType", "objectType", "Entity", "entity",
               "Resource", "resource")


def normalise_entity(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    r = raw.strip().lower()
    if r in ("customer", "customers", "client", "clients"):
        return "customer"
    if r in ("agent", "agents", "agency", "agencies", "staff", "agentstaff", "agent-staff", "agent_staff", "trade",
             "agentstaffs", "agentstaffmember"):
        return "agent"
    return None


def normalise_event(raw: Optional[str]) -> str:
    if not raw:
        return "unknown"
    r = raw.strip().lower()
    if r in ("created", "create", "new", "added", "insert", "inserted"):
        return "created"
    if r in ("modified", "modify", "updated", "update", "changed", "change", "edited"):
        return "modified"
    if r in ("archived", "archive", "deleted", "delete", "removed"):
        return "archived"
    return "unknown"


def _to_int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"\d+", s):
            return int(s)
        # BundleReference style "22926-1110" -> 22926
        m = re.fullmatch(r"(\d+)-\d+", s)
        if m:
            return int(m.group(1))
        # a self link ".../sales/customers/22926"
        m = re.search(r"/(?:customers|agents|staff)/(\d+)", s)
        if m:
            return int(m.group(1))
    return None


def _walk(obj: Any, keys: tuple, depth: int = 0) -> Optional[Any]:
    """Breadth-first search for the first of ``keys`` in a nested dict/list."""
    if depth > 4 or obj is None:
        return None
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] not in (None, ""):
                return obj[k]
        for v in obj.values():
            found = _walk(v, keys, depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj[:5]:
            found = _walk(v, keys, depth + 1)
            if found is not None:
                return found
    return None


def parse_body(raw: bytes) -> Any:
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(text)
        if isinstance(data, (int, float)) and not isinstance(data, bool):
            return {"Id": data}
        if isinstance(data, str):
            return {"Id": data} if re.fullmatch(r"\d+", data.strip()) else {"_raw": data}
        return data
    except ValueError:
        pass
    # form-encoded or bare id fallbacks
    if re.fullmatch(r"\d+", text):
        return {"Id": int(text)}
    if "=" in text and "&" in text or "=" in text:
        from urllib.parse import parse_qs
        return {k: v[0] for k, v in parse_qs(text).items()}
    return {"_raw": text}


SUBJECT_RE = re.compile(r"(Customer|AgentStaff|Agent|Agency|Staff)_?(Created|Modified|Updated|Archived|Deleted)", re.I)


def flatten_tigerbay(body: Any) -> Any:
    """TigerBay's real shape (seen 2026-09-09):
        {"subject": "Customers_CustomerModified",
         "data": [{"key": "CustomerId", "value": 2732}, {"key": "Diagnostic-Id", "value": "..."}]}
    Fold the key/value list into plain keys and derive EntityType/EventType from the subject."""
    if not isinstance(body, dict):
        return body
    out = dict(body)
    data = body.get("data") or body.get("Data")
    if isinstance(data, list) and all(isinstance(d, dict) and "key" in d for d in data):
        for d in data:
            out[str(d["key"])] = d.get("value")
    subject = str(body.get("subject") or body.get("Subject") or "")
    m = SUBJECT_RE.search(subject.split("_")[-1]) or SUBJECT_RE.search(subject)
    if m:
        out.setdefault("EntityType", m.group(1))
        out.setdefault("EventType", m.group(2))
    return out


def extract(path_entity: Optional[str], path_event: Optional[str], path_id: Optional[str],
            body: Any, query: dict) -> tuple[Optional[str], str, Optional[int]]:
    body = flatten_tigerbay(body)
    entity = normalise_entity(path_entity)
    if entity is None:
        entity = normalise_entity(_walk(body, ENTITY_KEYS)) or normalise_entity(query.get("entity"))
    event = normalise_event(path_event)
    if event == "unknown":
        event = normalise_event(_walk(body, EVENT_KEYS)) or normalise_event(query.get("event"))
    entity_id = _to_int(path_id)
    if entity_id is None:
        entity_id = _to_int(query.get("id"))
    if entity_id is None:
        # prefer the most specific keys first, then generic Id
        for group in (ID_KEYS[:16], ID_KEYS[16:]):
            found = _walk(body, group)
            entity_id = _to_int(found)
            if entity_id is not None:
                break
    if entity_id is None:
        entity_id = _to_int(_walk(body, ("BundleReference", "bundleReference")))
    if entity_id is None:
        entity_id = _to_int(_walk(body, ("Href", "href", "Url", "url", "Link", "link")))
    # A body carrying the full record may tell us it is archived even if the URL did not.
    if event != "archived" and isinstance(body, dict):
        if body.get("Archived") is True or body.get("IsArchived") is True:
            event = "archived"
    return entity, event, entity_id
