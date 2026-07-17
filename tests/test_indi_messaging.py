from collections import deque
from collections.abc import Iterator
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


class _FakeMember:
    """A minimal stand-in for indipyclient's `NumberMember`/`TextMember`/etc."""

    def __init__(self, value: str, min_: str = "0", max_: str = "0") -> None:
        self.membervalue = value
        self.min = min_
        self.max = max_


class _FakeVector:
    """A minimal stand-in for indipyclient's `Vector`.

    `data` maps member name to member object (as the real `Vector` does);
    `__getitem__` returns the member's value, matching `Vector.__getitem__`.
    """

    def __init__(self, members: dict[str, _FakeMember], state: str = "Ok") -> None:
        self.data = members
        self.state = state

    def __getitem__(self, membername: str) -> str:
        return self.data[membername].membervalue


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
    assert indi_event is not None
    assert indi_event["state"] is indi_messaging.PropertyState.OK


def test_to_indi_event_converts_set_vector_to_property_update() -> None:
    event = _make_event(
        setNumberVector, vectorname="CCD_EXPOSURE", data={"CCD_EXPOSURE_VALUE": "5.0"}
    )

    indi_event = _to_indi_event(event)

    assert indi_event is not None
    assert indi_event["kind"] == "propertyUpdate"
    assert indi_event["type"] == "number"
    assert indi_event["elements"] == {"CCD_EXPOSURE_VALUE": "5.0"}


def test_to_indi_event_converts_del_property() -> None:
    event = _make_event(delProperty, message="gone")

    indi_event = _to_indi_event(event)

    assert indi_event is not None
    assert indi_event["kind"] == "propertyDeleted"
    assert indi_event["type"] is None
    assert indi_event["message"] == "gone"


def test_to_indi_event_converts_message() -> None:
    event = _make_event(Message, vectorname=None, message="hello")

    indi_event = _to_indi_event(event)

    assert indi_event is not None
    assert indi_event["kind"] == "message"
    assert indi_event["message"] == "hello"


def test_to_indi_event_ignores_unrecognised_event() -> None:
    event = MagicMock()

    assert _to_indi_event(event) is None


async def test_start_messaging_connects_and_starts_streaming(mocks: Mocks) -> None:
    status = await indi_messaging.start_messaging(host="pi.local", port=7625)

    mocks.client.asyncrun.assert_called_once()
    assert status == {"running": True, "host": "pi.local", "port": 7625}


async def test_start_messaging_polls_until_connected(
    mocks: Mocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(indi_messaging, "_STARTUP_POLL_INTERVAL", 0.001)
    statuses: Iterator[indi_messaging.MessagingStatus] = iter(
        [
            {"running": False, "host": "pi.local", "port": 7625},
            {"running": False, "host": "pi.local", "port": 7625},
            {"running": True, "host": "pi.local", "port": 7625},
        ]
    )
    calls = 0

    async def fake_get_status() -> indi_messaging.MessagingStatus:
        nonlocal calls
        calls += 1
        return next(statuses)

    monkeypatch.setattr(indi_messaging, "get_status", fake_get_status)

    status = await indi_messaging.start_messaging(host="pi.local", port=7625)

    assert calls == 3
    assert status == {"running": True, "host": "pi.local", "port": 7625}


async def test_start_messaging_returns_not_running_if_poll_times_out(
    mocks: Mocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(indi_messaging, "_STARTUP_POLL_TIMEOUT", 0.05)
    monkeypatch.setattr(indi_messaging, "_STARTUP_POLL_INTERVAL", 0.01)

    async def fake_get_status() -> indi_messaging.MessagingStatus:
        return {"running": False, "host": "pi.local", "port": 7625}

    monkeypatch.setattr(indi_messaging, "get_status", fake_get_status)

    status = await indi_messaging.start_messaging(host="pi.local", port=7625)

    assert status == {"running": False, "host": "pi.local", "port": 7625}


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


def test_list_devices_rejects_when_not_started() -> None:
    with pytest.raises(RuntimeError, match="not started"):
        indi_messaging.list_devices()


async def test_list_devices_returns_known_device_names(mocks: Mocks) -> None:
    mocks.client.data = {"CCD Simulator": MagicMock(), "Telescope Simulator": MagicMock()}
    await indi_messaging.start_messaging()

    assert indi_messaging.list_devices() == ["CCD Simulator", "Telescope Simulator"]


async def test_get_property_values_returns_current_member_values(mocks: Mocks) -> None:
    vector = _FakeVector({"CCD_MAX_X": _FakeMember("6248"), "CCD_MAX_Y": _FakeMember("4176")})
    device = MagicMock()
    device.data = {"CCD_INFO": vector}
    mocks.client.data = {"CCD Simulator": device}
    await indi_messaging.start_messaging()

    values = indi_messaging.get_property_values("CCD Simulator", "CCD_INFO")

    assert values == {"CCD_MAX_X": "6248", "CCD_MAX_Y": "4176"}


async def test_get_property_values_returns_none_for_unknown_device(mocks: Mocks) -> None:
    mocks.client.data = {}
    await indi_messaging.start_messaging()

    assert indi_messaging.get_property_values("Nonexistent", "CCD_INFO") is None


async def test_get_property_values_returns_none_for_an_undefined_property(mocks: Mocks) -> None:
    device = MagicMock()
    device.data = {}
    mocks.client.data = {"CCD Simulator": device}
    await indi_messaging.start_messaging()

    assert indi_messaging.get_property_values("CCD Simulator", "CCD_INFO") is None


async def test_get_property_state_returns_current_vector_state(mocks: Mocks) -> None:
    vector = _FakeVector({"CCD_EXPOSURE_VALUE": _FakeMember("5.0")}, state="Busy")
    device = MagicMock()
    device.data = {"CCD_EXPOSURE": vector}
    mocks.client.data = {"CCD Simulator": device}
    await indi_messaging.start_messaging()

    state = indi_messaging.get_property_state("CCD Simulator", "CCD_EXPOSURE")

    assert state == "Busy"
    assert state is indi_messaging.PropertyState.BUSY


async def test_get_property_state_falls_back_to_the_raw_string_for_an_unknown_state(
    mocks: Mocks,
) -> None:
    vector = _FakeVector({"CCD_EXPOSURE_VALUE": _FakeMember("5.0")}, state="SomethingNew")
    device = MagicMock()
    device.data = {"CCD_EXPOSURE": vector}
    mocks.client.data = {"CCD Simulator": device}
    await indi_messaging.start_messaging()

    state = indi_messaging.get_property_state("CCD Simulator", "CCD_EXPOSURE")

    assert state == "SomethingNew"
    assert not isinstance(state, indi_messaging.PropertyState)


async def test_get_property_state_returns_none_for_unknown_device(mocks: Mocks) -> None:
    mocks.client.data = {}
    await indi_messaging.start_messaging()

    assert indi_messaging.get_property_state("Nonexistent", "CCD_EXPOSURE") is None


async def test_get_property_state_returns_none_for_an_undefined_property(mocks: Mocks) -> None:
    device = MagicMock()
    device.data = {}
    mocks.client.data = {"CCD Simulator": device}
    await indi_messaging.start_messaging()

    assert indi_messaging.get_property_state("CCD Simulator", "CCD_EXPOSURE") is None


async def test_get_property_range_returns_the_members_min_and_max(mocks: Mocks) -> None:
    vector = _FakeVector({"FOCUS_ABSOLUTE_POSITION": _FakeMember("100", min_="0", max_="50000")})
    device = MagicMock()
    device.data = {"ABS_FOCUS_POSITION": vector}
    mocks.client.data = {"Focuser Simulator": device}
    await indi_messaging.start_messaging()

    focus_range = indi_messaging.get_property_range(
        "Focuser Simulator", "ABS_FOCUS_POSITION", "FOCUS_ABSOLUTE_POSITION"
    )

    assert focus_range == (0.0, 50000.0)


async def test_get_property_range_returns_none_for_an_undefined_member(mocks: Mocks) -> None:
    device = MagicMock()
    device.data = {"ABS_FOCUS_POSITION": _FakeVector({})}
    mocks.client.data = {"Focuser Simulator": device}
    await indi_messaging.start_messaging()

    focus_range = indi_messaging.get_property_range(
        "Focuser Simulator", "ABS_FOCUS_POSITION", "FOCUS_ABSOLUTE_POSITION"
    )

    assert focus_range is None


async def test_messaging_client_buffers_recognised_events() -> None:
    buf: deque = deque(maxlen=10)
    client = _MessagingClient.__new__(_MessagingClient)
    client._buffer = buf

    event = _make_event(defNumberVector, vectorname="EQUATORIAL_EOD_COORD")
    await client.rxevent(event)

    assert len(buf) == 1
    assert buf[0]["kind"] == "propertyDefinition"
