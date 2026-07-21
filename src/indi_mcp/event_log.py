"""The durable SQLite event log backing `indi://messages`/`indi://scripts` catch-up.

Per `docs/Design.md#event-log`: every `kind`-tagged event `event_streams`
publishes (messaging-layer and scripting-layer alike) is also written to an
`events` table in the shared local database (`db.connect`) — this is what
actually lets a client that was offline catch up, rather than the live
`resources/subscribe` channel alone (that one is best-effort/live-only, see
`event_streams`). Retention is short: events older than **1 day** are
purged (`purge_old_events`), since this table exists to bridge reconnects
and short-term history, not as permanent storage — captured frames have
their own, separate, much-longer-retention storage (`frame_store`).

**Every function here is synchronous and blocking** — a plain `sqlite3`
connect/execute/commit — matching `frame_store`'s own contract exactly (see
its module docstring for the full reasoning). `event_streams` is the only
caller on the hot path (a `propertyUpdate` for a "chatty" device can arrive
many times a second, per `docs/Design.md#event-streams`), and it schedules
`record_event` on a worker thread via `asyncio.to_thread` rather than
calling it directly, so a slow SD-card write never stalls the event loop
that every other device's messaging and every other script run's
pause/cancel/progress polling also depends on.
"""

import asyncio
import json
import logging
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, TypedDict

from indi_mcp import db

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_RETENTION",
    "PURGE_INTERVAL",
    "EventRecord",
    "Stream",
    "get_events",
    "purge_old_events",
    "record_event",
    "run_purge_loop",
]

Stream = Literal["messages", "scripts"]

DEFAULT_RETENTION = timedelta(days=1)
"""How long a durable event is kept before `purge_old_events` deletes it, per Design.md."""

PURGE_INTERVAL = timedelta(hours=1)
"""How often `run_purge_loop` sweeps for expired events, per Design.md ("e.g. hourly")."""


class EventRecord(TypedDict):
    """One `events` row, as returned by `get_events` — the original event plus its log metadata.

    `payload` is the same `kind`-tagged dict `event_streams.publish_message_event`/
    `publish_script_event` received (an `indi_messaging.IndiEvent` or a
    `script_runs.ScriptRunStatus`), decoded back from the stored JSON text.
    """

    id: int
    stream: Stream
    device: str | None
    runId: str | None
    occurredAt: str
    payload: Mapping


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the `events` table/indexes if they don't already exist, per Design.md's sketch."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            stream TEXT NOT NULL,
            device TEXT,
            run_id TEXT,
            occurred_at TEXT NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    # `idx_events_occurred_at` serves `purge_old_events`'s `DELETE ... WHERE occurred_at < ?`
    # (no `stream` filter there — it purges across both streams at once). `get_events` always
    # filters `stream` (a required parameter), so it's served by the composite index below
    # instead, which covers both that filter and the `ORDER BY occurred_at` in one index.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON events (occurred_at)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_stream_occurred_at ON events (stream, occurred_at)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_run_id ON events (run_id)")


def _now() -> str:
    """The current UTC time, ISO 8601, always including microseconds.

    Matches `frame_store._now`'s exact reasoning: forcing `%f` always
    present keeps every `occurred_at` value the same length, so the plain
    lexicographic comparisons `get_events`'s `since` filter and
    `purge_old_events`'s cutoff both do stay chronologically correct
    instead of depending on `datetime.isoformat()`'s microsecond-omitted
    case coincidentally still sorting right.
    """
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _row_to_record(row: sqlite3.Row) -> EventRecord:
    return {
        "id": row["id"],
        "stream": row["stream"],
        "device": row["device"],
        "runId": row["run_id"],
        "occurredAt": row["occurred_at"],
        "payload": json.loads(row["payload"]),
    }


def record_event(
    stream: Stream,
    payload: Mapping,
    *,
    device: str | None = None,
    run_id: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Durably record one `kind`-tagged event to the `events` table.

    Synchronous and blocking — see this module's docstring. `event_streams`
    is the only intended caller, and only ever from a worker thread
    (`asyncio.to_thread`), never directly from the event loop.
    """
    with db.connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO events (stream, device, run_id, occurred_at, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (stream, device, run_id, _now(), json.dumps(payload)),
        )
        conn.commit()


def get_events(
    stream: Stream,
    *,
    device: str | None = None,
    run_id: str | None = None,
    since: str | None = None,
    db_path: Path | None = None,
) -> list[EventRecord]:
    """Query the durable log for catch-up, oldest first, with optional filters.

    This is the reconnect story from `docs/Design.md#event-log`: a client
    that was offline calls this with `since` set to the last `occurredAt`
    it actually saw (from a prior `get_events` call, or from before it
    dropped off `indi://messages`/`indi://scripts`) to fetch what it
    missed, rather than assuming the live subscription caught everything.
    `since` is inclusive (`occurred_at >= since`): the event at exactly
    `since` is returned again if one exists, rather than being excluded —
    deliberately, since a strict `>` could silently skip a *different*
    event that happens to share the same microsecond-precision timestamp.
    A caller polling repeatedly should dedupe by `id` rather than assume
    no overlap with the previous call. Oldest first (the opposite order
    from `list_messages`/`list_frames`'s newest-first) since catching up
    is naturally about replaying events in the order they happened, not
    about "what just happened" — a client folding these into its own view
    processes them front-to-back.
    """
    clauses = ["stream = ?"]
    params: list[str] = [stream]
    if device is not None:
        clauses.append("device = ?")
        params.append(device)
    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(run_id)
    if since is not None:
        clauses.append("occurred_at >= ?")
        params.append(since)
    where = " AND ".join(clauses)
    with db.connect(db_path) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            f"SELECT * FROM events WHERE {where} ORDER BY occurred_at ASC", params
        ).fetchall()
    return [_row_to_record(row) for row in rows]


def purge_old_events(
    *, older_than: timedelta = DEFAULT_RETENTION, db_path: Path | None = None
) -> int:
    """Delete every event older than `older_than` (Design.md's 1-day retention by default).

    Followed by `PRAGMA incremental_vacuum` to actually reclaim the pages
    the `DELETE` just freed — a no-op unless the database is in
    `auto_vacuum=INCREMENTAL` mode, which `db.connect` sets on every fresh
    database file (see its own comment for why an *existing* file in a
    different mode won't pick this up automatically). Returns the number
    of rows deleted, so `run_purge_loop` can log something useful.
    """
    cutoff = (datetime.now(tz=UTC) - older_than).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
    with db.connect(db_path) as conn:
        _ensure_schema(conn)
        cursor = conn.execute("DELETE FROM events WHERE occurred_at < ?", (cutoff,))
        conn.commit()
        conn.execute("PRAGMA incremental_vacuum")
        conn.commit()
    return cursor.rowcount


async def run_purge_loop(
    *,
    interval: timedelta = PURGE_INTERVAL,
    older_than: timedelta = DEFAULT_RETENTION,
    db_path: Path | None = None,
) -> None:
    """Purge expired events every `interval`, forever, until the task is cancelled.

    Purges once immediately, then repeats every `interval` — so a server
    that's restarted often still gets regular cleanup rather than only ever
    reaching the first purge after a full hour of uptime. Intended to be
    launched once as a background `asyncio.Task` from `server.py`'s
    lifespan (see there), and cancelled on shutdown; the actual (blocking)
    purge runs via `asyncio.to_thread` each cycle so it never stalls the
    event loop the rest of the server depends on. A single failed purge
    (e.g. a locked/corrupt database file) is logged and the loop keeps
    going rather than dying silently — the next cycle gets another chance.
    """
    while True:
        try:
            deleted = await asyncio.to_thread(
                purge_old_events, older_than=older_than, db_path=db_path
            )
            if deleted:
                logger.info("Purged %d expired event(s) from the event log", deleted)
        except Exception:
            logger.exception("Event log purge failed")
        await asyncio.sleep(interval.total_seconds())
