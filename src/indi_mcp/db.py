"""The INDI Device's shared local SQLite database.

One embedded database file for this device's operational data — not one
per concern — per `docs/Design.md`'s "Event log" and "Frame storage
metadata" sections: frame metadata (`frame_store`, INDIMCP-10) and the
(separate, not-yet-built) event log (INDIMCP-15) are both short-retention/
long-retention *tables* in this one file, not separate databases. A single
Raspberry Pi running one MCP server process is a single-writer, mostly-
local workload, so SQLite in WAL mode (not a separate database service)
is enough — see Design.md's "Why SQLite, not Postgres" for the full
reasoning.

This module only owns *opening a connection* to that shared file — each
table's own schema (`CREATE TABLE IF NOT EXISTS ...`) is created by the
module that owns it (e.g. `frame_store`), not here, so this module doesn't
need to know about every table that will ever live in the database.
"""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["DB_PATH_ENV", "connect"]

DB_PATH_ENV = "INDI_MCP_DB_PATH"
_DEFAULT_DB_PATH = Path("indi_mcp.sqlite3")


def _db_path(path: Path | None) -> Path:
    return path if path is not None else Path(os.environ.get(DB_PATH_ENV, _DEFAULT_DB_PATH))


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open a connection to the shared local database, closing it on exit.

    `path` overrides `INDI_MCP_DB_PATH` (default `indi_mcp.sqlite3` in the
    working directory) — mainly so tests can point at a `tmp_path` file
    instead of monkeypatching the environment, matching `rig_store`'s
    `directory` parameter. A fresh connection per call rather than one
    long-lived module-global connection: this is a low-frequency, short-
    lived-transaction workload (a frame saved every exposure, a status row
    updated occasionally), so per-call connection overhead is negligible,
    and it sidesteps any stale-connection-across-tests/reconnect concerns a
    cached global connection would raise.
    """
    resolved = _db_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(resolved)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
    finally:
        conn.close()
