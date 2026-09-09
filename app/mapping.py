"""TigerBay profile -> HubSpot property mapping.

Everything HubSpot receives is produced here, so this is the file to edit when a
field should map differently. ``CONTACT_PROPERTIES``/``COMPANY_PROPERTIES`` list
the custom properties the service creates in HubSpot on start-up (idempotent).
"""
import hashlib
import json
import re
from typing import Any, Optional

# --- properties the service needs to exist in HubSpot ------------------------------
# The HubSpot portal already carries a TigerBay export (Aug 2026) with its own
# property set, so the mapping below targets THOSE properties:
#   tigerbay_id (string)  = TigerBay customer Id on customers; the AGENCY id on agent-staff
#   title, county, agency_name, abta_reference, brand_channels, original_data_source,
#   is_archived (TRUE/FALSE), cancel_from_email / cancel_from_mailing (TRUE/FALSE), source_created_date,
#   source_last_modified
# The only additions are two unique-value keys so a record can be looked up directly
# by TigerBay id (tigerbay_id is neither unique nor typed, and is the agency id on staff).
CONTACT_PROPERTIES: list[dict] = [
    {"name": "tigerbay_customer_id", "label": "TigerBay Customer ID", "type": "number",
     "fieldType": "number", "hasUniqueValue": True},
    {"name": "tigerbay_agent_id", "label": "TigerBay Agent Staff ID", "type": "number",
     "fieldType": "number", "hasUniqueValue": True},
]
COMPANY_PROPERTIES: list[dict] = []   # companies use the existing tigerbay_id / county / abta_reference

# Properties we own on a HubSpot record: these are the ones we read back and diff.
# date_of_birth and country are deliberately NOT synced (decision 2026-09-08: leave
# them as they are in HubSpot).
CUSTOMER_CONTACT_FIELDS = [
    "email", "firstname", "lastname", "title", "phone", "mobilephone", "address", "city", "county",
    "zip", "tigerbay_id", "tigerbay_customer_id", "cancel_from_email", "cancel_from_mailing", "is_archived",
]
STAFF_CONTACT_FIELDS = [
    "email", "firstname", "lastname", "title", "phone", "mobilephone", "address", "city", "county",
    "zip", "agency_name", "abta_reference", "tigerbay_id", "tigerbay_agent_id", "is_archived",
]
AGENCY_COMPANY_FIELDS = [
    "name", "phone", "address", "city", "county", "zip", "tigerbay_id", "abta_reference",
]

# Set once, when the record is first created; never diffed afterwards.
CUSTOMER_CREATE_DEFAULTS = {"brand_channels": "Direct", "original_data_source": "Tigerbay"}
STAFF_CREATE_DEFAULTS = {"brand_channels": "Travel Agent", "original_data_source": "Tigerbay"}

# Fields that, when TigerBay is blank, we do NOT blank in HubSpot (someone may have
# enriched them there). Everything else is mirrored exactly, including clearing.
NEVER_CLEAR = {"phone", "mobilephone", "address", "city", "county", "zip", "country", "date_of_birth",
               "title", "agency_name", "abta_reference", "name", "firstname", "lastname"}
# Compared on digits only, so '07796 050100.' == '07796050100' (no cosmetic churn).
PHONE_FIELDS = {"phone", "mobilephone"}
# Opt-outs only ever propagate one way: TigerBay can set an opt-out, never clear one
# that HubSpot already holds (a HubSpot opt-out may have come from an unsubscribe link).
OPT_OUT_FIELDS = {"cancel_from_email", "cancel_from_mailing"}
# Compared case-insensitively (the export wrote TRUE/FALSE; HubSpot may show true/false).
CASE_INSENSITIVE = {"email", "is_archived", "cancel_from_email", "cancel_from_mailing"}

# Keep both sides comparable: ISO-3166 alpha-3 -> country name for the common ones.
COUNTRY_NAMES = {
    "GBR": "United Kingdom", "GB": "United Kingdom", "UK": "United Kingdom",
    "IRL": "Ireland", "USA": "United States", "US": "United States",
    "FRA": "France", "DEU": "Germany", "ESP": "Spain", "ITA": "Italy", "NLD": "Netherlands",
    "BEL": "Belgium", "CHE": "Switzerland", "AUT": "Austria", "PRT": "Portugal", "MUS": "Mauritius",
    "SYC": "Seychelles", "MDV": "Maldives", "ARE": "United Arab Emirates", "AUS": "Australia",
    "CAN": "Canada", "NZL": "New Zealand", "ZAF": "South Africa", "JEY": "Jersey", "GGY": "Guernsey",
    "IMN": "Isle of Man",
}


def _s(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v).strip()


def _email(v: Any) -> str:
    e = _s(v).lower()
    return e if "@" in e else ""


def _bool(v: Any) -> str:
    """TRUE/FALSE, matching the convention of the existing export (is_archived etc.)."""
    if isinstance(v, str):
        return "TRUE" if v.strip().lower() in ("1", "true", "yes") else "FALSE"
    return "TRUE" if v else "FALSE"


def _phone(v: Any) -> str:
    s = _s(v)
    return s if re.search(r"\d{5,}", s) else ""


def _country(v: Any) -> str:
    s = _s(v)
    return COUNTRY_NAMES.get(s.upper(), s)


def _date(v: Any) -> str:
    """TigerBay sends '1985-07-03T10:03:00'; HubSpot date_of_birth is a free string, keep YYYY-MM-DD."""
    s = _s(v)
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if not m or m.group(1).startswith("0001"):
        return ""
    return m.group(1)


def _address_lines(c: dict) -> str:
    # TigerBay lines often carry their own trailing comma ("57 High Street,"); strip
    # so joining does not produce "57 High Street,, Hadleigh,".
    parts = [_s(c.get(k)).strip(" ,") for k in ("Address0", "Address1", "Address2", "Address3")]
    return ", ".join(p for p in parts if p)


def _placeholder(v: str, key: str) -> str:
    """Preproduction data carries literal placeholders ('housename', 'townCity'); ignore those."""
    return "" if v.lower() == key.lower() else v


def primary_contact(contacts: list[dict]) -> Optional[dict]:
    if not contacts:
        return None
    for c in contacts:
        if _s(c.get("Type")).lower() == "primary":
            return c
    return contacts[0]


def split_name(name: str) -> tuple[str, str]:
    """'Melanie Harper - Travel Consultant' -> ('Melanie', 'Harper'); 'Mark Ferrier (Clydebank)' -> ('Mark', 'Ferrier')."""
    name = _s(name)
    name = re.sub(r"\s*\(.*?\)\s*", " ", name)          # drop parenthesised suffixes
    name = re.split(r"\s+[-–—|/]\s+|,\s*", name)[0]        # drop ' - job title', ', branch'
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        return "", ""
    parts = name.split()
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


def _contact_block(c: Optional[dict], prefer_business: bool) -> dict:
    """Address/phone properties from a TigerBay contact record."""
    if not c:
        return {}
    if prefer_business:
        phone = _phone(c.get("BusinessLandline")) or _phone(c.get("PersonalLandline"))
        mobile = _phone(c.get("BusinessMobile")) or _phone(c.get("PersonalMobile"))
    else:
        phone = _phone(c.get("PersonalLandline")) or _phone(c.get("BusinessLandline"))
        mobile = _phone(c.get("PersonalMobile")) or _phone(c.get("BusinessMobile"))
    return {
        "phone": phone,
        "mobilephone": mobile,
        "address": _placeholder(_address_lines(c), "housename"),
        "city": _placeholder(_s(c.get("TownCity")), "townCity"),
        "county": _placeholder(_s(c.get("County")), "county"),
        "zip": _placeholder(_s(c.get("PostCode")), "postcode"),
    }


# --- public mappers -------------------------------------------------------------

def map_customer(profile: dict) -> dict:
    """TigerBay customer profile -> HubSpot contact properties (all strings)."""
    cu = profile["customer"]
    pc = primary_contact(profile.get("contacts") or [])
    email = _email(cu.get("EmailAddress")) or (_email(pc.get("PersonalEmail")) if pc else "") \
        or (_email(pc.get("BusinessEmail")) if pc else "")
    props = {
        "email": email,
        "firstname": _s(cu.get("Forename")) or (_s(pc.get("Forename")) if pc else ""),
        "lastname": _s(cu.get("Surname")) or (_s(pc.get("Surname")) if pc else ""),
        "title": _s(cu.get("Title")),
        "tigerbay_id": _s(cu.get("Id")),
        "tigerbay_customer_id": _s(cu.get("Id")),
        "cancel_from_email": _bool(cu.get("DoNotEmail")),
        "cancel_from_mailing": _bool(cu.get("DoNotMail")),
        "is_archived": _bool(cu.get("Archived")),
    }
    props.update(_contact_block(pc, prefer_business=False))
    return props


def map_staff(profile: dict) -> dict:
    """TigerBay agent record with Type=Staff -> HubSpot contact properties.

    Follows the existing export's convention: ``tigerbay_id`` holds the AGENCY id and
    ``agency_name`` / ``abta_reference`` describe the agency; the staff member's own
    TigerBay id goes in the unique ``tigerbay_agent_id``.
    """
    st = profile["agent"]
    pc = primary_contact(profile.get("contacts") or [])
    parent = profile.get("parent") or {}
    first, last = split_name(st.get("Name"))
    if pc and _s(pc.get("Forename")):
        first, last = _s(pc.get("Forename")), _s(pc.get("Surname"))
    email = _email(st.get("Email")) or (_email(pc.get("BusinessEmail")) if pc else "") \
        or (_email(pc.get("PersonalEmail")) if pc else "") or _email(st.get("Reference"))
    agency_id = st.get("GroupId") or parent.get("ID") or parent.get("Id")
    props = {
        "email": email,
        "firstname": first,
        "lastname": last,
        "title": _s(pc.get("Title")) if pc else "",
        "agency_name": _s(parent.get("Name")),
        "abta_reference": _s(parent.get("Reference")),
        "tigerbay_id": _s(agency_id) if agency_id else "",
        "tigerbay_agent_id": _s(st.get("ID") or st.get("Id")),
        # A staff member is archived if they are, OR if their whole agency is.
        "is_archived": _bool(bool(st.get("IsArchived")) or bool(parent.get("IsArchived"))),
    }
    block = _contact_block(pc, prefer_business=True)
    # Staff rarely carry their own address in TigerBay; the export used the agency's,
    # so fall back to the agency's primary contact field by field.
    agency_block = _contact_block(primary_contact(profile.get("parent_contacts") or []), prefer_business=True)
    for k, v in agency_block.items():
        if not block.get(k):
            block[k] = v
    props.update(block)
    return props


def map_agency(agent: dict, contacts: list[dict]) -> dict:
    """TigerBay agent record with Type=Agent -> HubSpot company properties."""
    pc = primary_contact(contacts or [])
    props = {
        "name": _s(agent.get("Name")),
        "tigerbay_id": _s(agent.get("ID") or agent.get("Id")),
        "abta_reference": _s(agent.get("Reference")),
    }
    block = _contact_block(pc, prefer_business=True)
    block.pop("mobilephone", None)
    props.update(block)
    return props


# --- diffing ----------------------------------------------------------------------

def normalise(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    s = str(value).strip()
    return s


def diff(desired: dict, current: Optional[dict]) -> dict:
    """Return only the properties whose value differs from what HubSpot holds.

    ``current`` is HubSpot's ``properties`` map (or None for a new record).
    A blank desired value is skipped for NEVER_CLEAR fields; otherwise a blank
    clears the HubSpot value.
    """
    if current is None:
        return {k: v for k, v in desired.items() if normalise(v) != ""}
    changes = {}
    for key, want in desired.items():
        want_n = normalise(want)
        have_n = normalise(current.get(key))
        if key in CASE_INSENSITIVE:
            want_n, have_n = want_n.lower(), have_n.lower()
        if key in PHONE_FIELDS and _digits(want_n) == _digits(have_n):
            continue
        if want_n == have_n:
            continue
        if want_n == "" and key in NEVER_CLEAR:
            continue
        if key in OPT_OUT_FIELDS and have_n.lower() == "true":
            continue
        changes[key] = want
    return changes


def _digits(v: str) -> str:
    return re.sub(r"\D", "", v or "")


def names_compatible(desired: dict, current: dict) -> bool:
    """Guard for matches made on email alone: shared mailboxes (a Hays branch address,
    a family email) must not have one person's record overwritten with another's.
    Compatible = HubSpot has no name, or last names agree, or first names agree."""
    hf, hl = normalise(current.get("firstname")).lower(), normalise(current.get("lastname")).lower()
    df, dl = normalise(desired.get("firstname")).lower(), normalise(desired.get("lastname")).lower()
    if not hf and not hl:
        return True
    if hl and dl and (hl == dl or hl in dl or dl in hl):
        return True
    if hf and df and (hf == df or hf.split()[0] == df.split()[0]):
        return True
    if hf and df and not hl and (len(hf) == 1 and df.startswith(hf)):   # an initial
        return True
    return False


def fingerprint(props: dict) -> str:
    return hashlib.sha256(json.dumps(props, sort_keys=True).encode()).hexdigest()[:32]
