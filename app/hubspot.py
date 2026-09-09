"""HubSpot CRM v3 client — only the operations the sync needs.

Design notes (verified against developers.hubspot.com, Sept 2026):
  * Auth: ``Authorization: Bearer <private app token>``. Scopes needed:
    crm.objects.contacts.read/write, crm.objects.companies.read/write,
    crm.schemas.contacts.write, crm.schemas.companies.write.
  * Lookups use direct id-property GETs (``/{value}?idProperty=...``), never the
    search API: search is eventually consistent and capped at 5 req/s.
  * Rate limit: a private app gets 100-190 requests per rolling 10 s. We keep a
    local token bucket under that and honour 429 + Retry-After.
  * The v3 paths keep working under HubSpot's 2026 date-based versioning; set
    HUBSPOT_BASE_URL/HUBSPOT_API_PREFIX to migrate later.
"""
import logging
import re
import threading
import time
from collections import deque
from typing import Any, Optional

import requests

from app.config import settings

log = logging.getLogger("beachsync.hubspot")

CONTACTS = "contacts"
COMPANIES = "companies"
# Association type ids (HubSpot defined): contact -> company "primary" = 1
ASSOC_CONTACT_TO_COMPANY_PRIMARY = 1


class HubSpotError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, retryable: bool = True,
                 body: Any = None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.body = body


class RateLimiter:
    """Sliding-window limiter: at most ``limit`` calls per ``window`` seconds."""

    def __init__(self, limit: int, window: float = 10.0):
        self.limit = max(1, limit)
        self.window = window
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] > self.window:
                    self._calls.popleft()
                if len(self._calls) < self.limit:
                    self._calls.append(now)
                    return
                wait = self.window - (now - self._calls[0]) + 0.05
            time.sleep(max(wait, 0.05))


class HubSpotClient:
    def __init__(self, token: Optional[str] = None, base_url: Optional[str] = None,
                 timeout: Optional[float] = None, session: Optional[requests.Session] = None,
                 rate_per_10s: Optional[int] = None):
        self.token = token if token is not None else settings.hubspot_token
        self.base_url = (base_url or settings.hubspot_base_url).rstrip("/")
        self.timeout = timeout or settings.hubspot_timeout
        self.session = session or requests.Session()
        self.limiter = RateLimiter(rate_per_10s or settings.hubspot_rate_per_10s)

    # --- transport -----------------------------------------------------------
    def request(self, method: str, path: str, params: Optional[dict] = None,
                json: Optional[dict] = None, ok404: bool = False) -> Any:
        if not self.token:
            raise HubSpotError("HUBSPOT_ACCESS_TOKEN not configured", retryable=False)
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        for attempt in range(4):
            self.limiter.acquire()
            try:
                resp = self.session.request(method, url, params=params, json=json, headers=headers,
                                            timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt == 3:
                    raise HubSpotError(f"HubSpot request failed: {exc}") from exc
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else 10.0
                except ValueError:
                    delay = 10.0
                body = _safe_json(resp)
                if isinstance(body, dict) and body.get("policyName") == "DAILY":
                    raise HubSpotError("HubSpot daily rate limit reached", status=429, body=body)
                log.warning("HubSpot 429; sleeping %.1fs", delay)
                time.sleep(min(delay, 60))
                continue
            if resp.status_code == 404 and ok404:
                return None
            if resp.status_code >= 500 and attempt < 3:
                time.sleep(1.5 * (attempt + 1))
                continue
            if not 200 <= resp.status_code < 300:
                body = _safe_json(resp)
                msg = body.get("message") if isinstance(body, dict) else resp.text[:300]
                raise HubSpotError(f"HubSpot {method} {path} returned {resp.status_code}: {msg}",
                                   status=resp.status_code, retryable=resp.status_code >= 500, body=body)
            if resp.status_code == 204 or not resp.content:
                return None
            return _safe_json(resp)
        raise HubSpotError(f"HubSpot {method} {path}: retries exhausted")

    # --- objects ---------------------------------------------------------------
    def get_by_property(self, obj: str, id_property: str, value: str, properties: list[str]) -> Optional[dict]:
        if value == "":
            return None
        return self.request("GET", f"/crm/v3/objects/{obj}/{requests.utils.quote(str(value), safe='')}",
                            params={"idProperty": id_property, "properties": ",".join(properties)},
                            ok404=True)

    def get_by_id(self, obj: str, hs_id: str, properties: list[str]) -> Optional[dict]:
        return self.request("GET", f"/crm/v3/objects/{obj}/{hs_id}",
                            params={"properties": ",".join(properties)}, ok404=True)

    def search_one(self, obj: str, prop: str, value: str, properties: list[str],
                   extra_filters: Optional[list] = None) -> Optional[dict]:
        """Exact-match search on a non-unique property (eventually consistent; used only
        as a fallback for records that pre-date this service)."""
        if value == "":
            return None
        filters = [{"propertyName": prop, "operator": "EQ", "value": str(value)}] + (extra_filters or [])
        data = self.request("POST", f"/crm/v3/objects/{obj}/search",
                            json={"filterGroups": [{"filters": filters}], "properties": properties, "limit": 2})
        results = (data or {}).get("results") or []
        return results[0] if len(results) == 1 else None

    def search_all(self, obj: str, filters: list, properties: list[str], max_results: int = 10000) -> list[dict]:
        """Page through every match of an exact-filter search (eventually consistent)."""
        out: list[dict] = []
        after = None
        while len(out) < max_results:
            body: dict[str, Any] = {"filterGroups": [{"filters": filters}], "properties": properties, "limit": 100}
            if after:
                body["after"] = after
            data = self.request("POST", f"/crm/v3/objects/{obj}/search", json=body) or {}
            out.extend(data.get("results") or [])
            after = ((data.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break
        return out

    def create(self, obj: str, properties: dict, associations: Optional[list] = None) -> dict:
        body: dict[str, Any] = {"properties": properties}
        if associations:
            body["associations"] = associations
        try:
            return self.request("POST", f"/crm/v3/objects/{obj}", json=body)
        except HubSpotError as exc:
            # 409 "Contact already exists. Existing ID: 123" — hand back the id so the
            # caller can fall back to an update.
            if exc.status == 409:
                existing = existing_id_from_conflict(exc)
                if existing:
                    raise Conflict(existing) from exc
            raise

    def update(self, obj: str, hs_id: str, properties: dict) -> dict:
        return self.request("PATCH", f"/crm/v3/objects/{obj}/{hs_id}", json={"properties": properties})

    def archive(self, obj: str, hs_id: str) -> None:
        self.request("DELETE", f"/crm/v3/objects/{obj}/{hs_id}", ok404=True)

    def associate(self, from_obj: str, from_id: str, to_obj: str, to_id: str, type_id: int) -> None:
        self.request("PUT", f"/crm/v4/objects/{from_obj}/{from_id}/associations/{to_obj}/{to_id}",
                     json=[{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": type_id}])

    # --- properties (schema bootstrap) --------------------------------------------
    def existing_properties(self, obj: str) -> set[str]:
        data = self.request("GET", f"/crm/v3/properties/{obj}")
        return {p["name"] for p in (data or {}).get("results", [])}

    def ensure_properties(self, obj: str, wanted: list[dict], group: Optional[str] = None) -> list[str]:
        """Create any missing custom properties. Returns names created."""
        group = group or ("contactinformation" if obj == CONTACTS else "companyinformation")
        have = self.existing_properties(obj)
        created = []
        for spec in wanted:
            if spec["name"] in have:
                continue
            body = {"groupName": group, **spec}
            self.request("POST", f"/crm/v3/properties/{obj}", json=body)
            created.append(spec["name"])
            log.info("Created HubSpot %s property %s", obj, spec["name"])
        return created


class Conflict(HubSpotError):
    def __init__(self, existing_id: str):
        super().__init__(f"HubSpot record already exists: {existing_id}", status=409, retryable=False)
        self.existing_id = existing_id


def existing_id_from_conflict(exc: HubSpotError) -> Optional[str]:
    msg = ""
    if isinstance(exc.body, dict):
        msg = str(exc.body.get("message", ""))
    m = re.search(r"Existing ID:\s*(\d+)", msg or str(exc))
    return m.group(1) if m else None


def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


_client: Optional[HubSpotClient] = None


def client() -> HubSpotClient:
    global _client
    if _client is None:
        _client = HubSpotClient()
    return _client
