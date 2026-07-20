"""Persisting captured frames as files on the INDI Device, with metadata in SQLite.

Two halves, per `docs/Design.md#frame-storage-metadata`: the frame bytes
themselves (FITS files etc.) live as plain files under a frames directory,
while a `frames` row in the shared local database (`db.connect`) tracks
which run produced each one, which device captured it, when, and whether
the Client Computer has confirmed receiving it yet.

This module is the storage layer only — `save_frame`/`list_frames`/
`get_frame_metadata`/`confirm_frame_transfer`/`delete_frame`/
`purge_transferred_frames` are plain functions, not MCP tools. Draining a
BLOB out of `indi_messaging` and calling `save_frame` with the result is
`script_engine`'s `capture_frame` step handler (INDIMCP-37, built);
exposing these as MCP tools, plus a `frame://{frameId}` resource for the
bytes themselves, is INDIMCP-11 (also built) — mirroring how `rig_store`'s
plain functions are wired up as `@mcp.tool()`s separately in `server.py`.

Neither `delete_frame` nor `purge_transferred_frames` is ever called by
anything in this module or `script_engine` — the Raspberry Pi's storage is
small enough that frames need cleaning up, but per
`docs/Design.md#frame-storage-metadata` ("Cleanup ... is tied to the
frame's own lifecycle ... not to a fixed time window"), that cleanup is a
client-initiated action (an explicit MCP tool, INDIMCP-11) taken once the
Client Computer has confirmed it safely has its own copy
(`confirm_frame_transfer`) — never an automatic sweep run by the server
itself, which could delete a frame before it's actually been retrieved.
`purge_transferred_frames` only ever bulk-applies the same
already-transferred check `delete_frame` enforces on its own; it isn't a
separate, looser deletion path.

**Every function here is synchronous and blocking** — `save_frame` does a
plain `Path.write_bytes` (a captured FITS frame can be tens of MB) plus a
synchronous `sqlite3` connect/execute/commit, and the read/update
functions do the same minus the file write. Called directly from
`asyncio`-driven code (an `@mcp.tool()` handler, `script_engine`'s
`capture_frame` step), that would stall the single event loop this whole
server runs on — every other device's messaging stream and every other
script run's pause/cancel/progress polling included — for as long as the
write takes. Callers in async code MUST wrap every call here in
`asyncio.to_thread(...)`, exactly as `server.py` already does for
`rig_store.save_rig`/`observatory_store.save_observatory`:

    metadata = await asyncio.to_thread(
        frame_store.save_frame, data, device=device, extension=extension, run_id=run_id
    )
"""

import logging
import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

from indi_mcp import db

logger = logging.getLogger(__name__)

__all__ = [
    "FRAMES_DIR_ENV",
    "FrameMetadata",
    "FrameNotFoundError",
    "FrameNotTransferredError",
    "confirm_frame_transfer",
    "delete_frame",
    "get_frame_metadata",
    "get_frame_path",
    "list_frames",
    "purge_transferred_frames",
    "save_frame",
]

FRAMES_DIR_ENV = "INDI_MCP_FRAMES_DIR"
_DEFAULT_FRAMES_DIR = Path("frames")


class FrameMetadata(TypedDict):
    """A `frames` row, minus `path` — the shape `docs/Design.md`'s `list_frames`/
    `get_frame_metadata` tools (INDIMCP-11) expose to the client. `path` is deliberately
    excluded: it's an internal server-side detail (see `get_frame_path`), not something a
    client needs or should be able to infer the server's filesystem layout from.
    """

    frameId: str
    runId: str | None
    device: str
    sizeBytes: int
    capturedAt: str
    transferredAt: str | None


class FrameNotFoundError(Exception):
    """Raised when a `frameId` doesn't match any row in the `frames` table."""


class FrameNotTransferredError(Exception):
    """Raised by `delete_frame` when `frame_id` hasn't been confirmed transferred yet and
    `require_transferred` wasn't overridden."""


def _frames_dir(directory: Path | None) -> Path:
    if directory is not None:
        return directory
    return Path(os.environ.get(FRAMES_DIR_ENV, _DEFAULT_FRAMES_DIR))


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the `frames` table/indexes if they don't already exist, per Design.md's sketch."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS frames (
            id INTEGER PRIMARY KEY,
            frame_id TEXT UNIQUE NOT NULL,
            run_id TEXT,
            device TEXT NOT NULL,
            path TEXT NOT NULL,
            size_bytes INTEGER,
            captured_at TEXT NOT NULL,
            transferred_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_frames_run_id ON frames (run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_frames_captured_at ON frames (captured_at)")


def _now() -> str:
    """The current UTC time, ISO 8601, always including microseconds.

    `datetime.isoformat()` on its own omits the fractional-seconds
    component whenever `microsecond == 0`, which makes two `captured_at`
    values of different lengths — plain lexicographic comparison (as
    `list_frames`'s `since` filter and `ORDER BY captured_at` both do)
    would then depend on '+' sorting before '.' in ASCII to stay
    chronological. Forcing `%f` always present keeps every value the same
    length, so that comparison is straightforwardly correct instead of
    relying on that coincidence.
    """
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _row_to_metadata(row: sqlite3.Row) -> FrameMetadata:
    return {
        "frameId": row["frame_id"],
        "runId": row["run_id"],
        "device": row["device"],
        "sizeBytes": row["size_bytes"],
        "capturedAt": row["captured_at"],
        "transferredAt": row["transferred_at"],
    }


def save_frame(
    data: bytes,
    *,
    device: str,
    extension: str,
    run_id: str | None = None,
    directory: Path | None = None,
    db_path: Path | None = None,
) -> FrameMetadata:
    """Write `data` to a new file under the frames directory and record its metadata.

    `extension` should include the leading dot (e.g. `.fits`) and comes
    from the capture itself (an INDI BLOB's own format string) rather than
    being guessed here. The stored file name is a fresh `frameId`
    (`uuid4`) plus `extension` — not derived from `device`/timestamp — so
    concurrent captures across devices/runs can never collide, mirroring
    `run_id`'s own `uuid4` generation in `script_runs.start_script`.
    `run_id` is `None` for a frame captured ad hoc (outside any script
    run), per Design.md's schema sketch.

    If the metadata insert fails after the file has already been written
    (a full SD card, a locked/corrupt database file), the just-written
    file is deleted before the exception propagates — otherwise it would
    be left on disk with no `frames` row ever pointing to it: invisible to
    `list_frames`/`get_frame_metadata` and never cleaned up.
    """
    frame_id = str(uuid.uuid4())
    resolved_dir = _frames_dir(directory)
    resolved_dir.mkdir(parents=True, exist_ok=True)
    path = resolved_dir / f"{frame_id}{extension}"
    path.write_bytes(data)
    captured_at = _now()
    try:
        with db.connect(db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                "INSERT INTO frames (frame_id, run_id, device, path, size_bytes, captured_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (frame_id, run_id, device, str(path), len(data), captured_at),
            )
            conn.commit()
    except Exception:
        path.unlink(missing_ok=True)
        raise
    logger.info("Saved frame %s (%d bytes) from %s to %s", frame_id, len(data), device, path)
    return {
        "frameId": frame_id,
        "runId": run_id,
        "device": device,
        "sizeBytes": len(data),
        "capturedAt": captured_at,
        "transferredAt": None,
    }


def _get_row(frame_id: str, db_path: Path | None) -> sqlite3.Row:
    with db.connect(db_path) as conn:
        _ensure_schema(conn)
        row = conn.execute("SELECT * FROM frames WHERE frame_id = ?", (frame_id,)).fetchone()
    if row is None:
        raise FrameNotFoundError(f"no frame found for frameId {frame_id!r}")
    return row


def get_frame_metadata(frame_id: str, *, db_path: Path | None = None) -> FrameMetadata:
    """Return the metadata for `frame_id`. Raises `FrameNotFoundError` if unknown."""
    return _row_to_metadata(_get_row(frame_id, db_path))


def get_frame_path(frame_id: str, *, db_path: Path | None = None) -> Path:
    """Return the on-disk path for `frame_id`'s file. Raises `FrameNotFoundError` if unknown.

    Internal-only — see `FrameMetadata` for why `path` itself is never
    returned to a client. This is for INDIMCP-11's `frame://{frameId}`
    resource handler to actually read the frame's bytes.
    """
    return Path(_get_row(frame_id, db_path)["path"])


def list_frames(
    *,
    run_id: str | None = None,
    device: str | None = None,
    since: str | None = None,
    transferred: bool | None = None,
    db_path: Path | None = None,
) -> list[FrameMetadata]:
    """Query frame metadata, most recently captured first, with optional filters.

    Extends `docs/Design.md`'s `list_frames` tool filters (`runId`/
    `device`/`since`/`transferredOnly`) at the store layer: `transferred`
    is a tri-state (`None` = no filter, `True` = only transferred frames,
    `False` = only *not yet* transferred ones) rather than the doc
    sketch's one-directional `transferredOnly` boolean, since knowing
    what's still waiting to be transferred (e.g. before deciding whether
    it's safe to run `purge_transferred_frames`) is just as useful as
    seeing what's already done. INDIMCP-11 wraps this as the actual MCP
    tool. `since` is compared lexicographically against `captured_at`'s
    ISO 8601 UTC text, which sorts the same as chronological order.
    """
    clauses: list[str] = []
    params: list[str] = []
    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(run_id)
    if device is not None:
        clauses.append("device = ?")
        params.append(device)
    if since is not None:
        clauses.append("captured_at >= ?")
        params.append(since)
    if transferred is True:
        clauses.append("transferred_at IS NOT NULL")
    elif transferred is False:
        clauses.append("transferred_at IS NULL")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db.connect(db_path) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            f"SELECT * FROM frames {where} ORDER BY captured_at DESC", params
        ).fetchall()
    return [_row_to_metadata(row) for row in rows]


def confirm_frame_transfer(frame_id: str, *, db_path: Path | None = None) -> FrameMetadata:
    """Set `transferred_at` for `frame_id` to now, confirming the Client Computer has it.

    See `docs/Design.md#retrieving-frames`: `transferred_at` is only ever
    set on explicit client confirmation, never just because the server
    sent the bytes — a network drop mid-transfer must not be recorded as a
    successful one.
    """
    _get_row(frame_id, db_path)  # raises FrameNotFoundError if unknown
    transferred_at = _now()
    with db.connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            "UPDATE frames SET transferred_at = ? WHERE frame_id = ?", (transferred_at, frame_id)
        )
        conn.commit()
    return get_frame_metadata(frame_id, db_path=db_path)


def delete_frame(
    frame_id: str, *, require_transferred: bool = True, db_path: Path | None = None
) -> FrameMetadata:
    """Delete `frame_id`'s file and its `frames` row, returning its metadata as it was just before.

    A manual, client-requested cleanup action, not an automatic sweep —
    see this module's docstring for why. Refuses (`FrameNotTransferredError`)
    to delete a frame whose `transferred_at` isn't set unless
    `require_transferred=False` is passed explicitly — mirroring
    `rig_store.save_rig`'s `overwrite=False` default: a destructive action
    on the actual science data this project exists to capture should be
    safe by default, with the caller having to actively opt out (e.g. an
    operator deliberately discarding a bad/aborted capture that was never
    meant to be transferred) rather than a careless call being able to
    delete a frame the Client Computer never actually got a copy of.
    Raises `FrameNotFoundError` if `frame_id` is unknown.

    Deletes the `frames` row before the file, the opposite order from
    `save_frame`'s own failure handling: if something goes wrong partway
    through, a metadata row surviving with no file behind it (broken for
    `frame://{frameId}` reads) is worse than a file surviving with no row
    pointing to it (just unused disk space, and never returned by
    `list_frames`/`get_frame_metadata` again either way).
    """
    row = _get_row(frame_id, db_path)
    if require_transferred and row["transferred_at"] is None:
        raise FrameNotTransferredError(
            f"frameId {frame_id!r} has not been confirmed transferred yet; "
            "call confirm_frame_transfer first, or pass require_transferred=False "
            "to delete it anyway"
        )
    metadata = _row_to_metadata(row)
    path = Path(row["path"])
    with db.connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute("DELETE FROM frames WHERE frame_id = ?", (frame_id,))
        conn.commit()
    path.unlink(missing_ok=True)
    logger.info("Deleted frame %s (%s)", frame_id, path)
    return metadata


def purge_transferred_frames(
    *, older_than: timedelta, db_path: Path | None = None
) -> list[FrameMetadata]:
    """Delete every transferred frame captured more than `older_than` ago.

    A manual, client-requested bulk cleanup, not a scheduled/automatic
    sweep — see this module's docstring. `older_than` is always an
    explicit argument (e.g. `timedelta(weeks=1)`), never a hardcoded
    default baked in here, since how much local retention makes sense
    depends on the Pi's actual free storage and how often the operator
    downloads frames, not on anything this module can know.

    Age is measured against `capturedAt` (when the frame was taken), not
    `transferredAt` (when the Client Computer confirmed receiving it) —
    "more than a week old" is naturally about the frame's own age. Only
    ever considers frames that are already transferred, matching
    `delete_frame`'s own default safety check: a frame the Client Computer
    hasn't confirmed receiving yet is never eligible, no matter how old.

    Deletes every matching row in one `DELETE ... RETURNING` statement —
    a single connection and round trip, rather than a `SELECT` followed by
    a separate `delete_frame` call (its own row lookup plus its own
    `DELETE`) per frame. This also makes concurrent deletion harmless by
    construction rather than something that needs handling: the `WHERE`
    clause is evaluated once, at execution time, so a frame some other
    caller already deleted in the meantime simply isn't matched — there's
    no separate "was it already gone?" case to catch. Only the files are
    removed one at a time afterward, matching `delete_frame`'s own
    row-then-file ordering: a dangling file nobody points to is a smaller
    problem than a row pointing at a file that's already gone (and a file
    already missing on disk for some other reason is tolerated the same
    way `delete_frame` tolerates it — see `Path.unlink(missing_ok=True)`
    below).

    Returns the metadata of every frame actually deleted by this call (as
    it was just before deletion), most recently captured first — useful
    for a caller that wants to report what was purged.
    """
    cutoff = (datetime.now(tz=UTC) - older_than).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
    with db.connect(db_path) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "DELETE FROM frames WHERE transferred_at IS NOT NULL AND captured_at < ? RETURNING *",
            (cutoff,),
        ).fetchall()
        conn.commit()
    rows.sort(key=lambda row: row["captured_at"], reverse=True)
    for row in rows:
        Path(row["path"]).unlink(missing_ok=True)
        logger.info("Purged frame %s (%s)", row["frame_id"], row["path"])
    return [_row_to_metadata(row) for row in rows]
