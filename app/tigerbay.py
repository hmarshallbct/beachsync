"""TigerBay Nimble API client (read-only for this service).

Endpoints verified against beachcomber-preproduction.ontigerbay.co.uk/nimble on 2026-09-08:

  POST /security/users/authenticate   form: grant_type=client_credentials&client_id&client_secret
                                       -> {"access_token", "expires_in", "token_type"}
  GET  /sales/customers/{id}           customer profile
  GET  /sales/customers/{id}/contacts  address/phone/email records (Type: Primary | Emergency | ...)
  GET  /sales/customers?emailAddress=  filter (the free-text ``query`` param is ignored upstream)
  GET  /sales/agents                   all agencies (Type "Agent")
  GET  /sales/agents/{id}              an agency OR a staff member (Type "Agent" | "Staff";
                                       staff carry GroupId = parent agency id)
  GET  /sales/agents/{id}/staff        staff members of an agency
  GET  /sales/agents/{id}/contacts     contact records for an agency or staff member
"""
import logging
import threading
import time
from typing import Any, Optional

import requests

from app.config import settings

log = logging.getLogger("beachsync.tigerbay")


class TigerBayError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, retryable: bool = True):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class NotFound(TigerBayError):
    def __init__(self, message: str):
        super().__init__(message, status=404, retryable=False)


class TigerBayClient:
    def __init__(self, base_url: Optional[str] = None, token_url: Optional[str] = None,
                 client_id: Optional[str] = None, client_secret: Optional[str] = None,
                 timeout: Optional[float] = None, session: Optional[requests.Session] = None):
        self.base_url = (base_url or settings.tigerbay_base_url).rstrip("/")
        self.token_url = token_url or settings.tigerbay_token_url or f"{self.base_url}/security/users/authenticate"
        self.client_id = client_id or settings.tigerbay_client_id
        self.client_secret = client_secret or settings.tigerbay_client_secret
        self.timeout = timeout or settings.tigerbay_timeout
        self.session = session or requests.Session()
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_at = 0.0

    # --- auth ---------------------------------------------------------------
    def _fetch_token(self) -> None:
        if not (self.client_id and self.client_secret):
            raise TigerBayError("TigerBay credentials not configured", retryable=False)
        try:
            resp = self.session.post(
                self.token_url,
                data={"grant_type": "client_credentials", "client_id": self.client_id,
                      "client_secret": self.client_secret},
                timeout=self.timeout, allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise TigerBayError(f"TigerBay auth request failed: {exc}") from exc
        if resp.status_code != 200:
            raise TigerBayError(f"TigerBay auth returned {resp.status_code}", status=resp.status_code)
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise TigerBayError("TigerBay auth response missing access_token")
        try:
            expires_in = max(60, int(body.get("expires_in", 3600)))
        except (TypeError, ValueError):
            expires_in = 3600
        self._token = token
        self._expires_at = time.monotonic() + expires_in - 60

    def token(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._token or time.monotonic() >= self._expires_at:
                self._fetch_token()
            return self._token  # type: ignore[return-value]

    # --- transport ------------------------------------------------------------
    def get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{self.base_url}{path}"
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {self.token(force=attempt == 2)}",
                       "Accept": "application/json"}
            try:
                resp = self.session.get(url, params=params, headers=headers,
                                        timeout=self.timeout, allow_redirects=False)
            except requests.RequestException as exc:
                raise TigerBayError(f"TigerBay request failed: {exc}") from exc
            if resp.status_code == 401 and attempt == 1:
                continue
            break
        if resp.status_code == 404:
            raise NotFound(f"TigerBay {path} not found")
        if not 200 <= resp.status_code < 300:
            raise TigerBayError(f"TigerBay {path} returned {resp.status_code}", status=resp.status_code,
                                retryable=resp.status_code >= 500 or resp.status_code == 429)
        try:
            return resp.json()
        except ValueError as exc:
            raise TigerBayError(f"TigerBay {path} returned non-JSON body") from exc

    # --- resources --------------------------------------------------------------
    def customer(self, customer_id: int) -> dict:
        return self.get(f"/sales/customers/{int(customer_id)}")

    def customer_contacts(self, customer_id: int) -> list[dict]:
        try:
            data = self.get(f"/sales/customers/{int(customer_id)}/contacts")
        except NotFound:
            return []
        return data if isinstance(data, list) else []

    def customer_by_email(self, email: str) -> list[dict]:
        data = self.get("/sales/customers", params={"emailAddress": email})
        return data if isinstance(data, list) else []

    def agent(self, agent_id: int) -> dict:
        """An agency (Type=Agent) or a staff member (Type=Staff) — same resource."""
        return self.get(f"/sales/agents/{int(agent_id)}")

    def agent_contacts(self, agent_id: int) -> list[dict]:
        try:
            data = self.get(f"/sales/agents/{int(agent_id)}/contacts")
        except NotFound:
            return []
        return data if isinstance(data, list) else []

    def agent_staff(self, agent_id: int) -> list[dict]:
        try:
            data = self.get(f"/sales/agents/{int(agent_id)}/staff")
        except NotFound:
            return []
        return data if isinstance(data, list) else []

    def agents(self) -> list[dict]:
        data = self.get("/sales/agents")
        return data if isinstance(data, list) else []

    # --- profile bundles (what the sync actually consumes) --------------------
    def customer_profile(self, customer_id: int) -> dict:
        return {"customer": self.customer(customer_id), "contacts": self.customer_contacts(customer_id)}

    def agent_profile(self, agent_id: int) -> dict:
        """Returns {"agent": <record>, "contacts": [...], "parent": <agency record or None>}.

        For a staff member the parent agency is fetched (with its contacts) so the
        HubSpot contact can be associated with the right company.
        """
        rec = self.agent(agent_id)
        out: dict[str, Any] = {"agent": rec, "contacts": self.agent_contacts(agent_id), "parent": None,
                               "parent_contacts": []}
        if (rec.get("Type") or "").lower() == "staff" and rec.get("GroupId"):
            try:
                out["parent"] = self.agent(int(rec["GroupId"]))
                out["parent_contacts"] = self.agent_contacts(int(rec["GroupId"]))
                # CanConfirm / Email only appear on the staff *list* entry, not the record.
                for st in self.agent_staff(int(rec["GroupId"])):
                    if int(st.get("Id", 0)) == int(agent_id):
                        rec.setdefault("Email", st.get("Email"))
                        rec.setdefault("CanConfirm", st.get("CanConfirm"))
                        break
            except NotFound:
                out["parent"] = None
        return out


_client: Optional[TigerBayClient] = None


def client() -> TigerBayClient:
    global _client
    if _client is None:
        _client = TigerBayClient()
    return _client
