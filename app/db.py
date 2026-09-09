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
    processed_at  REAL,
    parent_event_id INTEGER
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
                cols = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
                if "parent_event_id" not in cols:
                    conn.execute("ALTER TABLE events ADD COLUMN parent_event_id INTEGER")
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
                  headers: Optional[dict] = None, source: str = "webhook",
                  parent_event_id: Optional[int] = None) -> int:
    status = "pending" if entity_id is not None else "unparsed"
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO events (received_at, entity, event, entity_id, source, raw_body, headers, status,"
            " parent_event_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), entity, event, entity_id, source, raw_body,
             json.dumps(headers or {}), status, parent_event_id),
        )
        return int(cur.lastrowid)


def enqueue_many(entity: str, event: str, ids: list[int], source: str, parent_event_id: Optional[int] = None) -> int:
    now = time.time()
    with tx() as conn:
        conn.executemany(
            "INSERT INTO events (received_at, entity, event, entity_id, source, raw_body, headers, status,"
            " parent_event_id) VALUES (?,?,?,?,?,'','{}','pending',?)",
            [(now, entity, event, int(i), source, parent_event_id) for i in ids],
        )
    return len(ids)


def supersede_duplicates(entity: str, entity_id: int, before: float, done_event_id: int) -> int:
    """After an event for (entity, id) has been processed against the CURRENT TigerBay
    state, any other pending event for the same record that was received before we
    started is redundant (TigerBay fires every event twice; a burst of edits collapses
    to one sync). An 'archived' event is never superseded by a 'modified' one."""
    with tx() as conn:
        cur = conn.execute(
            "UPDATE events SET status='superseded', processed_at=?, result=? WHERE status='pending'"
            " AND entity=? AND entity_id=? AND received_at<=? AND id<>? AND event<>'archived'",
            (time.time(), json.dumps({"superseded_by": done_event_id}), entity, entity_id, before, done_event_id),
        )
        return cur.rowcount


def retry_failed(limit: int = 10000) -> int:
    with tx() as conn:
        cur = conn.execute(
            "UPDATE events SET status='pending', next_attempt_at=0, attempts=0 WHERE id IN"
            " (SELECT id FROM events WHERE status='failed' ORDER BY id LIMIT ?)", (limit,))
        return cur.rowcount


def max_seen_id(entity: str) -> int:
    """Highest TigerBay id this service has ever handled for an entity (events + id map)."""
    conn = connect()
    try:
        a = conn.execute("SELECT MAX(entity_id) FROM events WHERE entity=?", (entity,)).fetchone()[0] or 0
        ents = ("customer",) if entity == "customer" else ("staff", "agent")
        b = conn.execute(
            f"SELECT MAX(tigerbay_id) FROM hubspot_map WHERE entity IN ({','.join('?' * len(ents))})", ents
        ).fetchone()[0] or 0
        return max(int(a), int(b))
    finally:
        conn.close()


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


def dashboard() -> dict:
    """Read-only aggregates for the /status page. No personal data: ids only."""
    conn = connect()
    try:
        now = time.time()
        days = conn.execute(
            "SELECT date(received_at,'unixepoch','localtime') AS d, source, status, COUNT(*) AS n"
            " FROM events WHERE received_at>=? GROUP BY d, source, status ORDER BY d",
            (now - 14 * 86400,)).fetchall()
        recent = conn.execute(
            "SELECT id, received_at, processed_at, source, entity, event, entity_id, status, attempts,"
            " substr(coalesce(result,''),1,400) AS result, substr(coalesce(last_error,''),1,200) AS last_error"
            " FROM events ORDER BY id DESC LIMIT 40").fetchall()
        problems = conn.execute(
            "SELECT id, received_at, source, entity, event, entity_id, status, attempts,"
            " substr(coalesce(last_error,''),1,300) AS last_error, substr(coalesce(raw_body,''),1,300) AS raw_body"
            " FROM events WHERE status IN ('failed','unparsed') ORDER BY id DESC LIMIT 50").fetchall()
        actions = conn.execute(
            "SELECT json_extract(result,'$.action') AS a, COUNT(*) AS n FROM events"
            " WHERE status IN ('done','skipped') AND received_at>=? GROUP BY a", (now - 7 * 86400,)).fetchall()
        return {"days": [dict(r) for r in days], "recent": [dict(r) for r in recent],
                "problems": [dict(r) for r in problems], "actions_7d": {r["a"] or "?": r["n"] for r in actions},
                "counts": counts()}
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
