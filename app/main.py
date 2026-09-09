"""beachsync — keeps HubSpot in step with TigerBay customer and agent profiles.

Webhook URLs to configure in TigerBay (one per entity/event, or one per entity):

    POST https://<host>/webhooks/tigerbay/customer/created
    POST https://<host>/webhooks/tigerbay/customer/modified
    POST https://<host>/webhooks/tigerbay/customer/archived
    POST https://<host>/webhooks/tigerbay/agent/created
    POST https://<host>/webhooks/tigerbay/agent/modified
    POST https://<host>/webhooks/tigerbay/agent/archived

``/webhooks/tigerbay/{entity}`` (no event) also works; the event is then read
from the body. Auth: HTTP Basic and/or a shared-secret header, both configured
by env. The receiver only stores the event (202); a worker thread does the sync.
"""
import hmac
import json
import logging
import secrets as _secrets
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import os as _os

from app import db, webhook
from app.config import settings

logging.basicConfig(level=settings.log_level,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("beachsync")

_worker = None
_schema_bootstrap: dict = {"done": False, "error": None, "created": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _worker
    db.connect().close()  # create schema
    if not settings.webhook_auth_configured():
        log.error("No webhook auth configured (WEBHOOK_BASIC_USER/PASSWORD or WEBHOOK_HEADER_VALUE); "
                  "webhook endpoint will refuse all requests")
    if settings.effective_dry_run() and not settings.dry_run:
        log.error("TIGERBAY_BASE_URL is a non-production tenant (anonymised data); forcing DRY_RUN. "
                  "Set ALLOW_WRITES_FROM_NONPROD_TIGERBAY=true to override (not recommended against live HubSpot).")
    if settings.hubspot_token and not settings.effective_dry_run():
        try:
            from app.sync import bootstrap_hubspot_schema
            _schema_bootstrap.update(done=True, created=bootstrap_hubspot_schema())
            log.info("HubSpot schema bootstrap: %s", _schema_bootstrap["created"])
        except Exception as exc:  # noqa: BLE001
            _schema_bootstrap.update(done=False, error=str(exc))
            log.error("HubSpot schema bootstrap failed: %s", exc)
    if settings.worker_enabled:
        from app.worker import Supervisor
        _worker = Supervisor()
        _worker.start()
    yield
    if _worker:
        _worker.stop()


app = FastAPI(title="beachsync", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)


app.mount("/static", StaticFiles(directory=_os.path.join(_os.path.dirname(__file__), "static")), name="static")


# --- auth ----------------------------------------------------------------------

def _basic_ok(request: Request) -> bool:
    if not (settings.webhook_basic_user and settings.webhook_basic_password):
        return False
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        return False
    import base64
    try:
        raw = base64.b64decode(auth[6:].strip()).decode("utf-8")
    except Exception:  # noqa: BLE001
        return False
    user, _, pw = raw.partition(":")
    return hmac.compare_digest(user, settings.webhook_basic_user) and \
        hmac.compare_digest(pw, settings.webhook_basic_password)


def _header_ok(request: Request) -> bool:
    if not settings.webhook_header_value:
        return False
    got = request.headers.get(settings.webhook_header_name, "")
    return bool(got) and hmac.compare_digest(got, settings.webhook_header_value)


def require_webhook_auth(request: Request) -> None:
    """Fail closed. Accepts EITHER a valid basic auth pair OR a valid header token
    (whichever are configured); if both are configured, either one suffices, so
    TigerBay can be set up with just one of them."""
    if not settings.webhook_auth_configured():
        raise HTTPException(503, "webhook auth not configured on server")
    if _basic_ok(request) or _header_ok(request):
        return
    # Diagnostics only: header NAMES and the basic-auth username, never secret values.
    auth = request.headers.get("authorization", "")
    scheme, user = auth.split(" ", 1)[0] if auth else "none", ""
    if auth.lower().startswith("basic "):
        import base64
        try:
            user = base64.b64decode(auth[6:].strip()).decode("utf-8", "replace").partition(":")[0]
        except Exception:  # noqa: BLE001
            user = "<undecodable>"
    log.warning("webhook auth failed from %s: scheme=%s basic_user=%r headers=%s",
                request.headers.get("x-forwarded-for", request.client.host if request.client else "?"),
                scheme, user, sorted(k for k in request.headers.keys()
                                     if k.lower() not in ("authorization", "cookie")))
    raise HTTPException(401, "unauthorised", headers={"WWW-Authenticate": 'Basic realm="beachsync"'})


def require_admin(request: Request) -> None:
    if not settings.admin_token:
        raise HTTPException(503, "ADMIN_TOKEN not configured")
    got = request.headers.get("x-admin-token") or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not got or not hmac.compare_digest(got, settings.admin_token):
        raise HTTPException(401, "unauthorised")


# --- webhook receiver ------------------------------------------------------------

async def _receive(request: Request, entity: Optional[str], event: Optional[str], entity_id: Optional[str]):
    raw = await request.body()
    if len(raw) > 1_000_000:
        raise HTTPException(413, "payload too large")
    body = webhook.parse_body(raw)
    ent, ev, eid = webhook.extract(entity, event, entity_id, body, dict(request.query_params))
    if ent is None:
        raise HTTPException(400, "cannot determine entity (use /webhooks/tigerbay/customer or /agent)")
    keep_headers = {k: v for k, v in request.headers.items()
                    if k.lower() in ("content-type", "user-agent", "x-forwarded-for", "x-real-ip")
                    or k.lower().startswith("x-tigerbay") or k.lower().startswith("x-webhook")}
    row_id = db.enqueue_event(ent, ev, eid, raw.decode("utf-8", errors="replace")[:20000], keep_headers)
    if eid is None:
        log.warning("webhook %s stored as unparsed (no id found): %s", row_id, raw[:300])
        return JSONResponse({"accepted": False, "event_id": row_id, "entity": ent, "event": ev,
                             "error": "no entity id found in path, query or body"}, status_code=400)
    log.info("webhook %s queued: %s %s id=%s", row_id, ent, ev, eid)
    return JSONResponse({"accepted": True, "event_id": row_id, "entity": ent, "event": ev, "id": eid},
                        status_code=202)


@app.post("/webhooks/tigerbay/{entity}/{event}/{entity_id}", dependencies=[Depends(require_webhook_auth)])
async def webhook_full(request: Request, entity: str, event: str, entity_id: str):
    return await _receive(request, entity, event, entity_id)


@app.post("/webhooks/tigerbay/{entity}/{event}", dependencies=[Depends(require_webhook_auth)])
async def webhook_entity_event(request: Request, entity: str, event: str):
    # /webhooks/tigerbay/customer/12345 (numeric second segment) means id, not event
    if event.isdigit():
        return await _receive(request, entity, None, event)
    return await _receive(request, entity, event, None)


@app.post("/webhooks/tigerbay/{entity}", dependencies=[Depends(require_webhook_auth)])
async def webhook_entity(request: Request, entity: str):
    return await _receive(request, entity, None, None)


@app.post("/webhooks/tigerbay", dependencies=[Depends(require_webhook_auth)])
async def webhook_generic(request: Request):
    return await _receive(request, None, None, None)


@app.get("/webhooks/tigerbay/{rest:path}", dependencies=[Depends(require_webhook_auth)])
async def webhook_probe(rest: str):
    """Some webhook UIs 'test' the URL with a GET; answer so the test passes."""
    return {"ok": True, "message": "beachsync webhook endpoint; POST events here"}


# --- health / admin --------------------------------------------------------------

@app.get("/health")
def health():
    c = db.counts()
    worker_ok = bool(_worker and _worker.is_alive() and _worker.worker_alive()) if settings.worker_enabled else True
    return {"status": "ok" if worker_ok else "degraded", "worker_alive": worker_ok,
            "worker_last_tick": _worker.last_tick if _worker else None, "queue": c,
            "dry_run": settings.effective_dry_run(), "dry_run_forced_by_nonprod_tigerbay":
            settings.effective_dry_run() and not settings.dry_run, "hubspot_schema": _schema_bootstrap,
            "webhook_auth_configured": settings.webhook_auth_configured(), "paused": db.paused(),
            "time": time.time()}


@app.get("/status", response_class=HTMLResponse)
def status_page(resume: Optional[str] = None):
    """Read-only dashboard: ids and counts only, no personal data. Reachable on the
    LAN vhost only; the public (Cloudflare) vhost exposes just /webhooks and /health."""
    from app.status_page import render
    alive = bool(_worker and _worker.is_alive() and _worker.worker_alive()) if settings.worker_enabled else True
    body = render(alive, _worker.last_tick if _worker else 0, resume_denied=(resume == "denied"))
    return HTMLResponse('<meta http-equiv="refresh" content="60">' + body)


@app.get("/status.json")
def status_json():
    return db.dashboard()


@app.post("/status/pause")
def status_pause(reason: str = Form("")):
    """Kill switch. Stopping is deliberately unauthenticated on the LAN page: the safe
    direction should be one click. Webhooks keep being accepted and queued."""
    db.set_flag("paused", (reason or "paused from status page").strip()[:200])
    log.warning("SYNC PAUSED: %s", reason)
    return RedirectResponse("/status", status_code=303)


@app.post("/status/resume")
def status_resume(token: str = Form("")):
    """Resuming needs the admin token: it is the direction that writes to HubSpot."""
    if not settings.admin_token or not hmac.compare_digest(token.strip(), settings.admin_token):
        return RedirectResponse("/status?resume=denied", status_code=303)
    db.set_flag("paused", None)
    log.warning("SYNC RESUMED from status page")
    return RedirectResponse("/status", status_code=303)


@app.get("/admin/events", dependencies=[Depends(require_admin)])
def admin_events(status: Optional[str] = None, limit: int = Query(100, le=1000)):
    rows = db.list_events(status, limit)
    for r in rows:
        for k in ("result", "headers"):
            if r.get(k):
                try:
                    r[k] = json.loads(r[k])
                except ValueError:
                    pass
    return {"events": rows}


@app.get("/admin/events/{event_id}", dependencies=[Depends(require_admin)])
def admin_event(event_id: int):
    r = db.get_event(event_id)
    if not r:
        raise HTTPException(404)
    return r


@app.post("/admin/events/{event_id}/retry", dependencies=[Depends(require_admin)])
def admin_retry(event_id: int):
    if not db.retry_event(event_id):
        raise HTTPException(409, "event not in a retryable state")
    return {"ok": True}


@app.post("/admin/events/retry-failed", dependencies=[Depends(require_admin)])
def admin_retry_failed():
    """Re-queue every failed event (after an outage)."""
    return {"requeued": db.retry_failed()}


@app.post("/admin/events/{event_id}/dismiss", dependencies=[Depends(require_admin)])
def admin_dismiss(event_id: int):
    """Acknowledge a failed/unparsed event so it stops counting in the health check."""
    if not db.dismiss_event(event_id):
        raise HTTPException(409, "event not in a dismissable state")
    return {"ok": True}


@app.post("/admin/sync/{entity}/{entity_id}", dependencies=[Depends(require_admin)])
def admin_sync(entity: str, entity_id: int, event: str = "modified"):
    """Queue a manual sync for one TigerBay record (customer or agent/staff id)."""
    ent = webhook.normalise_entity(entity)
    if not ent:
        raise HTTPException(400, "entity must be customer or agent")
    row_id = db.enqueue_event(ent, webhook.normalise_event(event), entity_id, source="replay")
    return {"queued": True, "event_id": row_id}


@app.get("/admin/preview/{entity}/{entity_id}", dependencies=[Depends(require_admin)])
def admin_preview(entity: str, entity_id: int):
    """Dry-run one record right now and return the diff without writing to HubSpot."""
    from app.sync import SyncContext, sync_event
    ent = webhook.normalise_entity(entity)
    if not ent:
        raise HTTPException(400, "entity must be customer or agent")
    try:
        return sync_event(ent, entity_id, "modified", SyncContext(dry_run=True))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc))
