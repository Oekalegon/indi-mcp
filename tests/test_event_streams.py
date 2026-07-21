import asyncio

import pytest

from indi_mcp import event_streams


class _FakeSession:
    """A minimal stand-in for `mcp.server.session.ServerSession`."""

    def __init__(self, *, fail: bool = False) -> None:
        self.updated: list[str] = []
        self._fail = fail

    async def send_resource_updated(self, uri) -> None:
        if self._fail:
            raise RuntimeError("connection dropped")
        self.updated.append(str(uri))


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    event_streams._messages.clear()
    event_streams._scripts.clear()
    event_streams._subscribers.clear()
    event_streams._background_tasks.clear()


def test_read_messages_returns_newest_first_and_filters_by_device() -> None:
    event_streams.publish_message_event({"kind": "message", "device": "CCD Simulator"})
    event_streams.publish_message_event({"kind": "message", "device": "Telescope Simulator"})

    assert event_streams.read_messages() == {
        "events": [
            {"kind": "message", "device": "Telescope Simulator"},
            {"kind": "message", "device": "CCD Simulator"},
        ]
    }
    assert event_streams.read_messages("CCD Simulator") == {
        "events": [{"kind": "message", "device": "CCD Simulator"}]
    }


def test_read_scripts_returns_newest_first_and_filters_by_run_id() -> None:
    event_streams.publish_script_event({"kind": "scriptStarted", "runId": "run-1"})
    event_streams.publish_script_event({"kind": "scriptStarted", "runId": "run-2"})

    assert event_streams.read_scripts() == {
        "events": [
            {"kind": "scriptStarted", "runId": "run-2"},
            {"kind": "scriptStarted", "runId": "run-1"},
        ]
    }
    assert event_streams.read_scripts("run-1") == {
        "events": [{"kind": "scriptStarted", "runId": "run-1"}]
    }


def test_read_messages_buffer_is_bounded() -> None:
    for i in range(event_streams._MAX_BUFFERED_EVENTS + 10):
        event_streams.publish_message_event({"kind": "message", "device": None, "i": i})

    events = event_streams.read_messages()["events"]
    assert len(events) == event_streams._MAX_BUFFERED_EVENTS
    assert events[0]["i"] == event_streams._MAX_BUFFERED_EVENTS + 9


async def test_publishing_a_message_notifies_the_unscoped_and_device_scoped_subscribers() -> None:
    session = _FakeSession()
    event_streams.subscribe("indi://messages", session)
    event_streams.subscribe("indi://messages/CCD Simulator", session)
    event_streams.subscribe("indi://messages/Telescope Simulator", session)

    event_streams.publish_message_event({"kind": "message", "device": "CCD Simulator"})
    await asyncio.sleep(0)

    assert sorted(session.updated) == sorted(["indi://messages", "indi://messages/CCD%20Simulator"])


async def test_publishing_a_script_event_notifies_the_unscoped_and_run_scoped_subscribers() -> None:
    session = _FakeSession()
    event_streams.subscribe("indi://scripts", session)
    event_streams.subscribe("indi://scripts/run-1", session)

    event_streams.publish_script_event({"kind": "scriptStarted", "runId": "run-1"})
    await asyncio.sleep(0)

    assert sorted(session.updated) == sorted(["indi://scripts", "indi://scripts/run-1"])


async def test_publishing_does_not_notify_subscribers_of_a_different_scope() -> None:
    session = _FakeSession()
    event_streams.subscribe("indi://messages/Telescope Simulator", session)

    event_streams.publish_message_event({"kind": "message", "device": "CCD Simulator"})
    await asyncio.sleep(0)

    assert session.updated == []


async def test_unsubscribe_stops_further_notifications() -> None:
    session = _FakeSession()
    event_streams.subscribe("indi://messages", session)
    event_streams.unsubscribe("indi://messages", session)

    event_streams.publish_message_event({"kind": "message", "device": None})
    await asyncio.sleep(0)

    assert session.updated == []
    assert "indi://messages" not in event_streams._subscribers


async def test_unsubscribe_of_an_unknown_uri_or_session_is_a_no_op() -> None:
    session = _FakeSession()
    event_streams.unsubscribe("indi://messages", session)

    event_streams.subscribe("indi://messages", session)
    event_streams.unsubscribe("indi://messages", _FakeSession())

    assert "indi://messages" in event_streams._subscribers


async def test_a_subscriber_that_fails_to_notify_is_dropped() -> None:
    """A dropped connection shouldn't keep failing forever on every future event — this is a
    best-effort channel (see `docs/Design.md#event-streams`), so a broken subscriber is removed
    rather than retried."""
    failing = _FakeSession(fail=True)
    healthy = _FakeSession()
    event_streams.subscribe("indi://messages", failing)
    event_streams.subscribe("indi://messages", healthy)

    event_streams.publish_message_event({"kind": "message", "device": None})
    await asyncio.sleep(0)

    assert healthy.updated == ["indi://messages"]
    assert failing not in event_streams._subscribers.get("indi://messages", set())
