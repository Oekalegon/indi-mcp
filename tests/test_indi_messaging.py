from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from indi_mcp import indi_messaging
from indi_mcp.indi_messaging import (
    Message,
    _MessagingClient,
    _to_indi_event,
    defNumberVector,
    defSwitchVector,
    delProperty,
    setNumberVector,
)


def _make_event(cls: type, **attrs: object) -> MagicMock:
    event = MagicMock(spec=cls)
    event.devicename = attrs.get("devicename", "CCD Simulator")
    event.vectorname = attrs.get("vectorname", "CONNECTION")
    event.state = attrs.get("state", "Ok")
    event.message = attrs.get("message", "")
    event.data = attrs.get("data", {"CONNECT": "On", "DISCONNECT": "Off"})
    event.timestamp = attrs.get("timestamp", datetime(2026, 7, 15, tzinfo=UTC))
    return event


@dataclass
class Mocks:
    client: MagicMock


@pytest.fixture(autouse=True)
def mocks(monkeypatch: pytest.MonkeyPatch) -> Mocks:
    indi_messaging._buffer.clear()
    monkeypatch.setattr(indi_messaging, "_client", None)
    monkeypatch.setattr(indi_messaging, "_task", None)

    client = MagicMock()
    client.connected = True
    client.asyncrun = AsyncMock()
    client.send_newVector = AsyncMock()

    def _fake_client(host: str, port: int, buffer: object) -> MagicMock:
        return client

    monkeypatch.setattr(indi_messaging, "_MessagingClient", _fake_client)

    return Mocks(client=client)


def test_to_indi_event_converts_def_vector_to_property_definition() -> None:
    event = _make_event(defSwitchVector)

    indi_event = _to_indi_event(event)

    assert indi_event == {
        "kind": "propertyDefinition",
        "type": "switch",
        "device": "CCD Simulator",
        "name": "CONNECTION",
        "state": "Ok",
        "message": None,
        "elements": {"CONNECT": "On", "DISCONNECT": "Off"},
        "timestamp": "2026-07-15T00:00:00+00:00",
    }


def test_to_indi_event_converts_set_vector_to_property_update() -> None:
    event = _make_event(
        setNumberVector, vectorname="CCD_EXPOSURE", data={"CCD_EXPOSURE_VALUE": "5.0"}
    )

    indi_event = _to_indi_event(event)

    assert indi_event["kind"] == "propertyUpdate"
    assert indi_event["type"] == "number"
    assert indi_event["elements"] == {"CCD_EXPOSURE_VALUE": "5.0"}


def test_to_indi_event_converts_del_property() -> None:
    event = _make_event(delProperty, message="gone")

    indi_event = _to_indi_event(event)

    assert indi_event["kind"] == "propertyDeleted"
    assert indi_event["type"] is None
    assert indi_event["message"] == "gone"


def test_to_indi_event_converts_message() -> None:
    event = _make_event(Message, vectorname=None, message="hello")

    indi_event = _to_indi_event(event)

    assert indi_event["kind"] == "message"
    assert indi_event["message"] == "hello"


def test_to_indi_event_ignores_unrecognised_event() -> None:
    event = MagicMock()

    assert _to_indi_event(event) is None


async def test_start_messaging_connects_and_starts_streaming(mocks: Mocks) -> None:
    status = await indi_messaging.start_messaging(host="pi.local", port=7625)

    mocks.client.asyncrun.assert_called_once()
    assert status == {"running": True, "host": "pi.local", "port": 7625}


async def test_start_messaging_stops_existing_connection_first(mocks: Mocks) -> None:
    await indi_messaging.start_messaging(host="pi.local", port=7625)

    await indi_messaging.start_messaging(host="pi.local", port=7626)

    assert indi_messaging._task is not None


async def test_stop_messaging_cancels_task(mocks: Mocks) -> None:
    await indi_messaging.start_messaging()

    status = await indi_messaging.stop_messaging()

    assert status == {"running": False, "host": "localhost", "port": indi_messaging.INDI_PORT}
    assert indi_messaging._client is None


async def test_get_status_reports_disconnected_state_before_starting() -> None:
    status = await indi_messaging.get_status()

    assert status == {"running": False, "host": "localhost", "port": indi_messaging.INDI_PORT}


def test_list_messages_returns_newest_first_and_filters_by_device() -> None:
    indi_messaging._buffer.appendleft(
        {
            "kind": "message",
            "type": None,
            "device": "A",
            "name": None,
            "state": None,
            "message": "first",
            "elements": None,
            "timestamp": "t1",
        }
    )
    indi_messaging._buffer.appendleft(
        {
            "kind": "message",
            "type": None,
            "device": "B",
            "name": None,
            "state": None,
            "message": "second",
            "elements": None,
            "timestamp": "t2",
        }
    )

    messages = indi_messaging.list_messages()
    assert [m["message"] for m in messages] == ["second", "first"]

    filtered = indi_messaging.list_messages(device="A")
    assert [m["message"] for m in filtered] == ["first"]


def test_list_messages_respects_limit() -> None:
    for i in range(5):
        indi_messaging._buffer.appendleft(
            {
                "kind": "message",
                "type": None,
                "device": None,
                "name": None,
                "state": None,
                "message": str(i),
                "elements": None,
                "timestamp": "t",
            }
        )

    assert len(indi_messaging.list_messages(limit=2)) == 2


async def test_send_property_rejects_when_not_started() -> None:
    with pytest.raises(RuntimeError, match="not started"):
        await indi_messaging.send_property("CCD Simulator", "CONNECTION", {"CONNECT": "On"})


async def test_send_property_rejects_unknown_device(mocks: Mocks) -> None:
    mocks.client.data = {}
    await indi_messaging.start_messaging()

    with pytest.raises(ValueError, match="Unknown INDI device"):
        await indi_messaging.send_property("Nonexistent", "CONNECTION", {"CONNECT": "On"})


async def test_send_property_rejects_unknown_vector(mocks: Mocks) -> None:
    device = MagicMock()
    device.data = {}
    mocks.client.data = {"CCD Simulator": device}
    await indi_messaging.start_messaging()

    with pytest.raises(ValueError, match="Unknown property"):
        await indi_messaging.send_property("CCD Simulator", "CONNECTION", {"CONNECT": "On"})


async def test_send_property_sends_new_vector_and_records_event(mocks: Mocks) -> None:
    vector = MagicMock()
    vector.vectortype = "SwitchVector"
    device = MagicMock()
    device.data = {"CONNECTION": vector}
    mocks.client.data = {"CCD Simulator": device}
    await indi_messaging.start_messaging()

    event = await indi_messaging.send_property("CCD Simulator", "CONNECTION", {"CONNECT": "On"})

    mocks.client.send_newVector.assert_called_once_with(
        "CCD Simulator", "CONNECTION", members={"CONNECT": "On"}
    )
    assert event["kind"] == "propertyCommand"
    assert event["type"] == "switch"
    assert event["device"] == "CCD Simulator"
    assert event["elements"] == {"CONNECT": "On"}
    assert indi_messaging.list_messages()[0] == event


async def test_messaging_client_buffers_recognised_events() -> None:
    buf: deque = deque(maxlen=10)
    client = _MessagingClient.__new__(_MessagingClient)
    client._buffer = buf

    event = _make_event(defNumberVector, vectorname="EQUATORIAL_EOD_COORD")
    await client.rxevent(event)

    assert len(buf) == 1
    assert buf[0]["kind"] == "propertyDefinition"
