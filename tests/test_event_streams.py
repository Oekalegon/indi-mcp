import asyncio

import pytest

from indi_mcp import event_log, event_streams


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


async def test_read_messages_returns_newest_first_and_filters_by_device() -> None:
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


async def test_read_scripts_returns_newest_first_and_filters_by_run_id() -> None:
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


async def test_read_messages_buffer_is_bounded() -> None:
    for i in range(event_streams._MAX_BUFFERED_EVENTS + 10):
        event_streams.publish_message_event({"kind": "message", "device": None, "i": i})

    events = event_streams.read_messages()["events"]
    assert len(events) == event_streams._MAX_BUFFERED_EVENTS
    assert events[0]["i"] == event_streams._MAX_BUFFERED_EVENTS + 9


async def test_publishing_a_message_notifies_the_unscoped_and_device_scoped_subscribers() -> None:
    session = _FakeSession()
    event_streams.subscribe(event_streams.messages_uri(None), session)
    event_streams.subscribe(event_streams.messages_uri("CCD Simulator"), session)
    event_streams.subscribe(event_streams.messages_uri("Telescope Simulator"), session)

    event_streams.publish_message_event({"kind": "message", "device": "CCD Simulator"})
    await asyncio.sleep(0)

    assert sorted(session.updated) == sorted(["indi://messages", "indi://messages/CCD%20Simulator"])


async def test_publishing_a_script_event_notifies_the_unscoped_and_run_scoped_subscribers() -> None:
    session = _FakeSession()
    event_streams.subscribe(event_streams.scripts_uri(None), session)
    event_streams.subscribe(event_streams.scripts_uri("run-1"), session)

    event_streams.publish_script_event({"kind": "scriptStarted", "runId": "run-1"})
    await asyncio.sleep(0)

    assert sorted(session.updated) == sorted(["indi://scripts", "indi://scripts/run-1"])


async def test_publishing_does_not_notify_subscribers_of_a_different_scope() -> None:
    session = _FakeSession()
    event_streams.subscribe(event_streams.messages_uri("Telescope Simulator"), session)

    event_streams.publish_message_event({"kind": "message", "device": "CCD Simulator"})
    await asyncio.sleep(0)

    assert session.updated == []


def test_messages_uri_percent_encodes_a_device_name_containing_reserved_characters() -> None:
    """A literal `/` in a device name must not add an extra path segment — the resource
    template `indi://messages/{device}` (see server.py) only matches a single segment, so an
    unencoded `/` would make that device's scoped stream unreachable via `resources/read`."""
    assert event_streams.messages_uri("CCD/Sub") == "indi://messages/CCD%2FSub"


def test_scripts_uri_percent_encodes_a_run_id_containing_reserved_characters() -> None:
    assert event_streams.scripts_uri("run/1") == "indi://scripts/run%2F1"


@pytest.mark.parametrize(
    "uri",
    [
        "indi://messages",
        "indi://scripts",
        "indi://messages/CCD%20Simulator",
        "indi://scripts/run-1",
        event_streams.messages_uri("CCD/Sub"),
        event_streams.scripts_uri("run/1"),
    ],
)
def test_is_subscribable_uri_accepts_every_shape_this_module_publishes_to(uri: str) -> None:
    assert event_streams.is_subscribable_uri(uri) is True


@pytest.mark.parametrize(
    "uri",
    [
        "indi://message",  # typo: missing the trailing 's'
        "indi://script",
        "frame://foo",
        "indi://messages/",
        "indi://scripts/",
        "indi://messages/CCD/Sub",  # unencoded '/' splits into two segments
        "indi://scripts/run/1",
        "",
    ],
)
def test_is_subscribable_uri_rejects_anything_else(uri: str) -> None:
    assert event_streams.is_subscribable_uri(uri) is False


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


async def test_publish_message_event_durably_records_to_the_event_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every published event is also persisted (INDIMCP-15), not just buffered/notified —
    this is what actually lets a reconnecting client catch up, per `docs/Design.md#event-log`."""
    calls: list[tuple] = []

    def fake_record_event(stream, payload, *, device, run_id, db_path=None) -> None:
        calls.append((stream, payload, device, run_id))

    monkeypatch.setattr(event_log, "record_event", fake_record_event)

    event = {"kind": "message", "device": "CCD Simulator"}
    event_streams.publish_message_event(event)
    await asyncio.sleep(0.05)

    assert calls == [("messages", event, "CCD Simulator", None)]


async def test_publish_script_event_durably_records_to_the_event_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []

    def fake_record_event(stream, payload, *, device, run_id, db_path=None) -> None:
        calls.append((stream, payload, device, run_id))

    monkeypatch.setattr(event_log, "record_event", fake_record_event)

    event = {"kind": "scriptStarted", "runId": "run-1"}
    event_streams.publish_script_event(event)
    await asyncio.sleep(0.05)

    assert calls == [("scripts", event, None, "run-1")]


async def test_publish_message_event_records_durably_even_with_no_subscribers() -> None:
    """Unlike live notification (skipped when nobody's subscribed), durable persistence exists
    to serve a client that reconnects *later* — it must always happen."""
    assert event_streams._subscribers == {}

    event_streams.publish_message_event({"kind": "message", "device": "CCD Simulator"})
    await asyncio.sleep(0.05)

    assert event_log.get_events("messages")[0]["device"] == "CCD Simulator"


async def test_a_failed_durable_write_does_not_raise_or_break_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A locked/corrupt database file must not take down message/script publishing — the live
    in-memory buffer and notifications are the primary path; the durable log is a best-effort
    addition on top, not a hard dependency."""

    def fake_record_event(*args, **kwargs) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(event_log, "record_event", fake_record_event)

    event_streams.publish_message_event({"kind": "message", "device": None})
    await asyncio.sleep(0.05)

    assert event_streams.read_messages()["events"] == [{"kind": "message", "device": None}]
