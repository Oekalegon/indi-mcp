import sqlite3
import threading
import time
from pathlib import Path

import pytest

from indi_mcp import db


def test_connect_creates_parent_directory_and_db_file(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "indi_mcp.sqlite3"

    with db.connect(db_path) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()

    assert db_path.exists()


def test_connect_reuses_the_same_file_across_calls(tmp_path: Path) -> None:
    db_path = tmp_path / "indi_mcp.sqlite3"

    with db.connect(db_path) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO t (value) VALUES ('a')")
        conn.commit()

    with db.connect(db_path) as conn:
        rows = conn.execute("SELECT value FROM t").fetchall()

    assert [row["value"] for row in rows] == ["a"]


def test_connect_row_factory_allows_column_access_by_name(tmp_path: Path) -> None:
    db_path = tmp_path / "indi_mcp.sqlite3"

    with db.connect(db_path) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO t (value) VALUES ('a')")
        conn.commit()
        row = conn.execute("SELECT * FROM t").fetchone()

    assert row["value"] == "a"


def test_connect_defaults_to_env_var_when_no_path_given(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "from-env.sqlite3"
    monkeypatch.setenv(db.DB_PATH_ENV, str(db_path))

    with db.connect() as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()

    assert db_path.exists()


def test_connect_waits_out_a_concurrent_writer_instead_of_failing_immediately(
    tmp_path: Path,
) -> None:
    """A second writer blocked behind another connection's write lock (INDIMCP-83: reproduced
    live as `sqlite3.OperationalError: database is locked` between `frame_store.save_frame` and
    the event log's writer, on the Pi's slower SD-card I/O) should retry until the lock clears
    rather than raising immediately, as long as it clears within `db`'s busy-timeout.
    """
    db_path = tmp_path / "indi_mcp.sqlite3"
    with db.connect(db_path) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
        conn.commit()

    holder = sqlite3.connect(db_path, timeout=0.0, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO t (value) VALUES ('holder')")

    def release_after_delay() -> None:
        time.sleep(0.2)
        holder.commit()
        holder.close()

    releaser = threading.Thread(target=release_after_delay)
    releaser.start()
    try:
        with db.connect(db_path) as conn:
            conn.execute("INSERT INTO t (value) VALUES ('waiter')")
            conn.commit()
    finally:
        releaser.join()

    with db.connect(db_path) as conn:
        values = {row["value"] for row in conn.execute("SELECT value FROM t").fetchall()}
    assert values == {"holder", "waiter"}


def test_connect_still_fails_once_the_busy_timeout_is_exceeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The busy-timeout bounds the wait — a lock held longer than that still surfaces as
    `OperationalError`, it isn't waited out forever.
    """
    monkeypatch.setattr(db, "_BUSY_TIMEOUT_SECONDS", 0.05)
    db_path = tmp_path / "indi_mcp.sqlite3"
    with db.connect(db_path) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
        conn.commit()

    holder = sqlite3.connect(db_path, timeout=0.0, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO t (value) VALUES ('holder')")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"), db.connect(db_path) as conn:
            conn.execute("INSERT INTO t (value) VALUES ('waiter')")
            conn.commit()
    finally:
        holder.commit()
        holder.close()
