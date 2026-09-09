"""SQLite persistence: the inbound event queue and the TigerBay->HubSpot id map.

One connection per call (``connect()``), WAL mode, so the API thread and the
worker thread can both use it. The DB is the durable queue: a webhook is
acknowledged (202) only once its row is committed.
"""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from app.config import settings

_init_lock = threading.Lock()
_initialised: set[str] = set()

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at   REAL    NOT NULL,
    entity        TEXT    NOT NULL,           -- customer | agent
    event         TEXT    NOT NULL,           -- created | modified | archived | unknown
    entity_id     INTEGER,                    -- TigerBay id (NULL if not parseable)
    source        TEXT    NOT NULL DEFAULT 'webhook',   -- webhook | backfill | replay
    raw_body      TEXT,
    headers       TEXT,
    status        TEXT    NOT NULL DEFAULT 'pending',   -- pending | processing | done | failed | unparsed | skipped
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL  NOT NULL DEFAULT 0,
    last_error    TEXT,
    result        TEXT,
    processed_at  REAL
);
CREATE INDEX IF NOT EXISTS ix_events_status_next ON events(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS ix_events_entity ON events(entity, entity_id);

CREATE TABLE IF NOT EXISTS hubspot_map (
    entity        TEXT    NOT NULL,           -- customer | staff | agent
    tigerbay_id   INTEGER NOT NULL,
    hubspot_object TEXT   NOT NULL,           -- contacts | companies
    hubspot_id    TEXT    NOT NULL,
    email         TEXT,
    fingerprint   TEXT,                       -- hash of the last property set we pushed
    updated_at    REAL    NOT NULL,
    PRIMARY KEY (entity, tigerbay_id)
);
"""


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    path = path or settings.db_path
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    if path not in _initialised:
        with _init_lock:
            if path not in _initialised:
                conn.executescript(SCHEMA)
                _initialised.add(path)
    return conn


@contextmanager
def tx(path: Optional[str] = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


# --- events ---------------------------------------------------------------

def enqueue_event(entity: str, event: str, entity_id: Optional[int], raw_body: str = "",
                  headers: Optional[dict] = None, source: str = "webhook") -> int:
    status = "pending" if entity_id is not None else "unparsed"
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO events (received_at, entity, event, entity_id, source, raw_body, headers, status)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), entity, event, entity_id, source, raw_body,
             json.dumps(headers or {}), status),
        )
        return int(cur.lastrowid)


def claim_next_event() -> Optional[dict]:
    """Atomically claim the oldest due pending event. Returns a dict or None."""
    now = time.time()
    with tx() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE status='pending' AND next_attempt_at<=? "
            "ORDER BY id LIMIT 1", (now,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE events SET status='processing', attempts=attempts+1 WHERE id=?", (row["id"],)
        )
        d = dict(row)
        d["attempts"] += 1
        return d


def finish_event(event_id: int, status: str, result: Any = None, error: Optional[str] = None,
                 retry_in: Optional[float] = None) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE events SET status=?, result=?, last_error=?, processed_at=?, next_attempt_at=? WHERE id=?",
            (status, json.dumps(result) if result is not None else None, error, time.time(),
             time.time() + retry_in if retry_in else 0, event_id),
        )


def requeue_stale_processing(older_than: float = 600) -> int:
    """Events left in 'processing' by a crashed worker go back to pending."""
    with tx() as conn:
        cur = conn.execute(
            "UPDATE events SET status='pending' WHERE status='processing' AND received_at<?",
            (time.time() - older_than,),
        )
        return cur.rowcount


def list_events(status: Optional[str] = None, limit: int = 100) -> list[dict]:
    conn = connect()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM events WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_event(event_id: int) -> Optional[dict]:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def retry_event(event_id: int) -> bool:
    with tx() as conn:
        cur = conn.execute(
            "UPDATE events SET status='pending', next_attempt_at=0, attempts=0 WHERE id=? "
            "AND status IN ('failed','done','skipped')", (event_id,)
        )
        return cur.rowcount == 1


def dismiss_event(event_id: int) -> bool:
    """Operator acknowledgement: take a failed/unparsed event out of the alerting counts."""
    with tx() as conn:
        cur = conn.execute(
            "UPDATE events SET status='dismissed', processed_at=? WHERE id=? AND status IN ('failed','unparsed','skipped')",
            (time.time(), event_id))
        return cur.rowcount == 1


def counts() -> dict:
    conn = connect()
    try:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM events GROUP BY status").fetchall()
        out = {r["status"]: r["n"] for r in rows}
        out["mapped"] = conn.execute("SELECT COUNT(*) FROM hubspot_map").fetchone()[0]
        out["last_webhook_at"] = conn.execute(
            "SELECT MAX(received_at) FROM events WHERE source='webhook'").fetchone()[0]
        out["last_done_at"] = conn.execute(
            "SELECT MAX(processed_at) FROM events WHERE status='done'").fetchone()[0]
        out["oldest_pending_age_s"] = None
        row = conn.execute("SELECT MIN(received_at) FROM events WHERE status='pending'").fetchone()
        if row and row[0]:
            out["oldest_pending_age_s"] = round(time.time() - row[0])
        return out
    finally:
        conn.close()


# --- id map ----------------------------------------------------------------

def get_map(entity: str, tigerbay_id: int) -> Optional[dict]:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM hubspot_map WHERE entity=? AND tigerbay_id=?", (entity, tigerbay_id)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def put_map(entity: str, tigerbay_id: int, hubspot_object: str, hubspot_id: str,
            email: Optional[str], fingerprint: Optional[str]) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO hubspot_map (entity, tigerbay_id, hubspot_object, hubspot_id, email, fingerprint, updated_at)"
            " VALUES (?,?,?,?,?,?,?) ON CONFLICT(entity, tigerbay_id) DO UPDATE SET"
            " hubspot_object=excluded.hubspot_object, hubspot_id=excluded.hubspot_id,"
            " email=excluded.email, fingerprint=excluded.fingerprint, updated_at=excluded.updated_at",
            (entity, tigerbay_id, hubspot_object, str(hubspot_id), email, fingerprint, time.time()),
        )


def delete_map(entity: str, tigerbay_id: int) -> None:
    with tx() as conn:
        conn.execute("DELETE FROM hubspot_map WHERE entity=? AND tigerbay_id=?", (entity, tigerbay_id))
