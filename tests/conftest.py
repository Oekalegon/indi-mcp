import asyncio
import contextlib

import pytest

from indi_mcp import db, event_streams


@pytest.fixture(autouse=True)
async def _reset_event_streams_record_worker():
    """Reset `event_streams`'s durable-write queue/worker (INDIMCP-59) around every test.

    Centralized here, once, rather than duplicated in every test file that (directly or
    transitively, e.g. via `indi_messaging`/`script_runs`) triggers a publish — same
    reasoning as `_default_db_path_is_a_tmp_file` below. Unlike the several per-file
    `_messages`/`_scripts`/`_subscribers`/`_background_tasks` reset fixtures already
    scattered across the suite, `_record_queue`/`_record_worker_task` specifically can't
    just be cleared: an `asyncio.Queue` binds to the event loop of its first `get`/`put`
    call, and pytest-asyncio gives each test its own function-scoped loop — reusing a
    queue/worker left over from a previous test would either raise (wrong loop) or, worse,
    hang forever (`drain()`'s `queue.join()` waiting on a worker task bound to an already-
    closed loop, which can never call `task_done()` in the new one). Resetting to `None`
    before the test lets the next real publish lazily recreate both fresh, on the loop
    that's actually running; cancelling the worker after lets its loop close cleanly
    instead of pytest-asyncio warning about a pending task.
    """
    event_streams._record_queue = None
    event_streams._record_worker_task = None
    yield
    if event_streams._record_worker_task is not None:
        event_streams._record_worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await event_streams._record_worker_task
    event_streams._record_queue = None
    event_streams._record_worker_task = None


@pytest.fixture(autouse=True)
def _default_db_path_is_a_tmp_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect `db.connect`'s default path to a per-test tmp file, everywhere.

    `event_streams.publish_message_event`/`publish_script_event` durably
    record every event to the database at the *default* path
    (`event_log.record_event` is called with no explicit `db_path`, since
    they have no way to know which test is calling them) — see
    `event_streams._schedule_record` (INDIMCP-15). Without this, any test
    anywhere in the suite that triggers a publish (most of
    `test_indi_messaging.py`/`test_script_runs.py`/`test_server.py`, not
    just `test_event_log.py`) would silently create/write to a real
    `indi_mcp.sqlite3` file in the working directory as a side effect,
    rather than staying confined to `tmp_path` like every other test's
    file I/O already does (`frame_store`/`rig_store`/`observatory_store`
    tests all pass an explicit `db_path`/`directory`; this module-level
    default is the one path nothing routes through a fixture yet).
    """
    monkeypatch.setenv(db.DB_PATH_ENV, str(tmp_path / "indi_mcp.sqlite3"))
