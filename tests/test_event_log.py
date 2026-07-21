import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from indi_mcp import event_log


def _backdate(db_path: Path, event_id: int, occurred_at: datetime) -> None:
    """Rewrite an event's `occurred_at` directly, bypassing `event_log` (test-only)."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE events SET occurred_at = ? WHERE id = ?",
            (occurred_at.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00"), event_id),
        )
        conn.commit()


def _last_id(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT max(id) FROM events").fetchone()[0]


def test_record_event_and_get_events_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "events.sqlite3"
    payload = {"kind": "message", "device": "CCD Simulator", "message": "hello"}

    event_log.record_event("messages", payload, device="CCD Simulator", db_path=db_path)

    events = event_log.get_events("messages", db_path=db_path)
    assert len(events) == 1
    record = events[0]
    assert record["stream"] == "messages"
    assert record["device"] == "CCD Simulator"
    assert record["runId"] is None
    assert record["payload"] == payload
    assert record["occurredAt"]


def test_get_events_filters_by_stream(tmp_path: Path) -> None:
    db_path = tmp_path / "events.sqlite3"
    event_log.record_event("messages", {"kind": "message"}, db_path=db_path)
    event_log.record_event("scripts", {"kind": "scriptStarted"}, db_path=db_path)

    assert [e["stream"] for e in event_log.get_events("messages", db_path=db_path)] == ["messages"]
    assert [e["stream"] for e in event_log.get_events("scripts", db_path=db_path)] == ["scripts"]


def test_get_events_filters_by_device(tmp_path: Path) -> None:
    db_path = tmp_path / "events.sqlite3"
    event_log.record_event("messages", {"kind": "message"}, device="A", db_path=db_path)
    event_log.record_event("messages", {"kind": "message"}, device="B", db_path=db_path)

    filtered = event_log.get_events("messages", device="A", db_path=db_path)

    assert [e["device"] for e in filtered] == ["A"]


def test_get_events_filters_by_run_id(tmp_path: Path) -> None:
    db_path = tmp_path / "events.sqlite3"
    event_log.record_event("scripts", {"kind": "scriptStarted"}, run_id="run-1", db_path=db_path)
    event_log.record_event("scripts", {"kind": "scriptStarted"}, run_id="run-2", db_path=db_path)

    filtered = event_log.get_events("scripts", run_id="run-1", db_path=db_path)

    assert [e["runId"] for e in filtered] == ["run-1"]


def test_get_events_filters_by_since_and_returns_oldest_first(tmp_path: Path) -> None:
    db_path = tmp_path / "events.sqlite3"
    now = datetime.now(tz=UTC)

    event_log.record_event("messages", {"kind": "message", "i": 1}, db_path=db_path)
    _backdate(db_path, _last_id(db_path), now - timedelta(hours=2))

    event_log.record_event("messages", {"kind": "message", "i": 2}, db_path=db_path)
    _backdate(db_path, _last_id(db_path), now - timedelta(hours=1))

    event_log.record_event("messages", {"kind": "message", "i": 3}, db_path=db_path)
    _backdate(db_path, _last_id(db_path), now)

    since = (now - timedelta(hours=1, minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
    events = event_log.get_events("messages", since=since, db_path=db_path)

    assert [e["payload"]["i"] for e in events] == [2, 3]


def test_purge_old_events_deletes_only_events_older_than_the_cutoff(tmp_path: Path) -> None:
    db_path = tmp_path / "events.sqlite3"
    now = datetime.now(tz=UTC)

    event_log.record_event("messages", {"kind": "message"}, db_path=db_path)
    old_id = _last_id(db_path)
    _backdate(db_path, old_id, now - timedelta(days=2))

    event_log.record_event("messages", {"kind": "message"}, db_path=db_path)
    recent_id = _last_id(db_path)

    deleted = event_log.purge_old_events(older_than=timedelta(days=1), db_path=db_path)

    assert deleted == 1
    remaining_ids = [e["id"] for e in event_log.get_events("messages", db_path=db_path)]
    assert remaining_ids == [recent_id]
    assert old_id not in remaining_ids


def test_purge_old_events_returns_zero_when_nothing_qualifies(tmp_path: Path) -> None:
    db_path = tmp_path / "events.sqlite3"
    event_log.record_event("messages", {"kind": "message"}, db_path=db_path)

    deleted = event_log.purge_old_events(older_than=timedelta(days=1), db_path=db_path)

    assert deleted == 0


def test_purge_old_events_reclaims_space_via_incremental_vacuum(tmp_path: Path) -> None:
    """Not a behavioral assertion on its own — mainly confirms `PRAGMA incremental_vacuum`
    doesn't raise, since it's a no-op unless `auto_vacuum=INCREMENTAL` actually took (see
    `db.connect`'s own comment on why that can silently fail to apply)."""
    db_path = tmp_path / "events.sqlite3"
    now = datetime.now(tz=UTC)
    for _ in range(20):
        event_log.record_event("messages", {"kind": "message", "pad": "x" * 500}, db_path=db_path)
        _backdate(db_path, _last_id(db_path), now - timedelta(days=2))

    deleted = event_log.purge_old_events(older_than=timedelta(days=1), db_path=db_path)

    assert deleted == 20
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2  # 2 = INCREMENTAL


async def test_run_purge_loop_purges_immediately_then_on_each_interval(tmp_path: Path) -> None:
    db_path = tmp_path / "events.sqlite3"
    now = datetime.now(tz=UTC)
    event_log.record_event("messages", {"kind": "message"}, db_path=db_path)
    _backdate(db_path, _last_id(db_path), now - timedelta(days=2))

    task = asyncio.create_task(
        event_log.run_purge_loop(
            interval=timedelta(seconds=0.01), older_than=timedelta(days=1), db_path=db_path
        )
    )
    try:
        await asyncio.sleep(0.05)
        assert event_log.get_events("messages", db_path=db_path) == []
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_run_purge_loop_keeps_going_after_a_failed_purge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single failed purge (e.g. a locked/corrupt database file) must not kill the loop —
    the next cycle should still get a chance to run."""
    calls: list[int] = []

    def _fake_purge(*, older_than: timedelta, db_path: Path | None) -> int:
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return 0

    monkeypatch.setattr(event_log, "purge_old_events", _fake_purge)

    task = asyncio.create_task(event_log.run_purge_loop(interval=timedelta(seconds=0.01)))
    try:
        await asyncio.sleep(0.05)
        assert len(calls) >= 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
