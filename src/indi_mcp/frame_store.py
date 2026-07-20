"""Persisting captured frames as files on the INDI Device, with metadata in SQLite.

Two halves, per `docs/Design.md#frame-storage-metadata`: the frame bytes
themselves (FITS files etc.) live as plain files under a frames directory,
while a `frames` row in the shared local database (`db.connect`) tracks
which run produced each one, which device captured it, when, and whether
the Client Computer has confirmed receiving it yet.

This module is the storage layer only — `save_frame`/`list_frames`/
`get_frame_metadata`/`confirm_frame_transfer` are plain functions, not MCP
tools. Draining a BLOB out of `indi_messaging` and calling `save_frame`
with the result is `script_engine`'s `capture_frame` step handler
(INDIMCP-37); exposing `list_frames`/`get_frame_metadata`/
`confirm_frame_transfer` as MCP tools, plus a `frame://{frameId}` resource
for the bytes themselves, is INDIMCP-11 — neither is built yet, mirroring
how `rig_store`'s plain functions are wired up as `@mcp.tool()`s
separately in `server.py`.
"""

import logging
import os
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from indi_mcp import db

logger = logging.getLogger(__name__)

__all__ = [
    "FRAMES_DIR_ENV",
    "FrameMetadata",
    "FrameNotFoundError",
    "confirm_frame_transfer",
    "get_frame_metadata",
    "get_frame_path",
    "list_frames",
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
    return datetime.now(tz=UTC).isoformat()


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
    """
    frame_id = str(uuid.uuid4())
    resolved_dir = _frames_dir(directory)
    resolved_dir.mkdir(parents=True, exist_ok=True)
    path = resolved_dir / f"{frame_id}{extension}"
    path.write_bytes(data)
    captured_at = _now()
    with db.connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO frames (frame_id, run_id, device, path, size_bytes, captured_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (frame_id, run_id, device, str(path), len(data), captured_at),
        )
        conn.commit()
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
    transferred_only: bool = False,
    db_path: Path | None = None,
) -> list[FrameMetadata]:
    """Query frame metadata, most recently captured first, with optional filters.

    Mirrors `docs/Design.md`'s `list_frames` tool filters (`runId`/
    `device`/`since`/`transferredOnly`) at the store layer; INDIMCP-11
    wraps this as the actual MCP tool. `since` is compared lexicographically
    against `captured_at`'s ISO 8601 UTC text, which sorts the same as
    chronological order.
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
    if transferred_only:
        clauses.append("transferred_at IS NOT NULL")
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
