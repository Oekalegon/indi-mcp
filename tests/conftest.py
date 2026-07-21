import pytest

from indi_mcp import db


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
